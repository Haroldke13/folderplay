#!/usr/bin/env python3
"""
codec_check.py -- Pure-stdlib codec/playability audit for the medialib library.

Answers one question per file: "will Chrome on Linux actually play this?"

It does that WITHOUT ffprobe/ffmpeg/mediainfo -- everything is parsed by hand:

  * .mp4 / .m4a / .mov  -> ISO Base Media File Format (ISO/IEC 14496-12) box walk:
        ftyp                       -> major brand + compatible brands
        moov > trak > mdia > hdlr  -> track handler type (vide / soun / ...)
                          > minf > stbl > stsd
                                     -> sample-entry FourCC == the codec
        avcC / hvcC / av1C / esds / dOps / dfLa  -> codec detail (profile/level)
  * .mp3 -> ID3v2 skip, then real MPEG-1/2/2.5 Layer I/II/III frame-sync validation.

Design notes
------------
* Only header bytes are read. Top-level boxes are traversed by seeking; the
  `moov` payload (small, usually < 1 MB even for a 285 MB file) is the only thing
  pulled into memory, and it is size-capped. Whole files are never read.
* Every read is bounds-checked, so truncated/garbage files degrade into a
  diagnostic instead of an exception.
* 64-bit box sizes (size == 1 -> 64-bit largesize) and size == 0 ("box runs to
  EOF") are both handled, as are 16-byte `uuid` extended types.
* faststart: if `moov` sits after the first `mdat`, the file plays fine locally
  but Chrome must fetch the entire file over HTTP before playback begins.

Usage:
    python3 codec_check.py [--json out.json] [--library library.json]
                           [--workers N] [--verify PATH] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, defaultdict

# --------------------------------------------------------------------------
# Chrome-on-Linux support matrix
# --------------------------------------------------------------------------
# Chrome's bundled ffmpeg build ships a deliberately narrow decoder set. These
# tables encode "stock Chrome on Linux, no system codecs, no proprietary extras".

VIDEO_OK = {
    "avc1": "H.264",          # standard H.264, in-band or out-of-band SPS/PPS
    "avc3": "H.264",          # H.264 with inline parameter sets
    "av01": "AV1",
    "vp09": "VP9",
    "vp08": "VP8",
}

VIDEO_BAD = {
    "hev1": "H.265/HEVC (Chrome on Linux has no HEVC decoder)",
    "hvc1": "H.265/HEVC (Chrome on Linux has no HEVC decoder)",
    "dvh1": "Dolby Vision (HEVC-based) -- unsupported",
    "dvhe": "Dolby Vision (HEVC-based) -- unsupported",
    "dav1": "Dolby Vision (AV1-based) -- unsupported profile",
    "vvc1": "H.266/VVC -- unsupported",
    "vvi1": "H.266/VVC -- unsupported",
    "mp4v": "MPEG-4 Part 2 (DivX/Xvid) -- Chrome cannot decode",
    "s263": "H.263 -- Chrome cannot decode",
    "h263": "H.263 -- Chrome cannot decode",
    "vc-1": "VC-1 -- Chrome cannot decode",
    "jpeg": "Motion JPEG in MP4 -- Chrome cannot decode",
    "mjpa": "Motion JPEG A -- Chrome cannot decode",
    "mjpb": "Motion JPEG B -- Chrome cannot decode",
    "ap4h": "Apple ProRes -- Chrome cannot decode",
    "apch": "Apple ProRes -- Chrome cannot decode",
    "apcn": "Apple ProRes -- Chrome cannot decode",
    "rle ": "QuickTime RLE -- Chrome cannot decode",
}

AUDIO_OK = {
    "mp4a": "AAC/MP3 (see esds object type)",  # refined by esds OTI below
    "Opus": "Opus",
    "opus": "Opus",
    "fLaC": "FLAC",
    "flac": "FLAC",
    ".mp3": "MP3",
    "mp3 ": "MP3",
}

AUDIO_BAD = {
    "ac-3": "Dolby Digital AC-3 -- Chrome cannot decode",
    "ec-3": "Dolby Digital Plus E-AC-3 -- Chrome cannot decode",
    "ac-4": "Dolby AC-4 -- Chrome cannot decode",
    "dtsc": "DTS -- Chrome cannot decode",
    "dtse": "DTS Express -- Chrome cannot decode",
    "dtsh": "DTS-HD -- Chrome cannot decode",
    "dtsl": "DTS-HD Lossless -- Chrome cannot decode",
    "alac": "Apple Lossless -- Chrome cannot decode",
    "samr": "AMR narrowband -- Chrome cannot decode",
    "sawb": "AMR wideband -- Chrome cannot decode",
    "sowt": "raw little-endian PCM in MP4 -- Chrome cannot decode",
    "twos": "raw big-endian PCM in MP4 -- Chrome cannot decode",
    "lpcm": "raw PCM in MP4 -- Chrome cannot decode",
    "ulaw": "mu-law PCM -- Chrome cannot decode",
    "alaw": "A-law PCM -- Chrome cannot decode",
    "ima4": "IMA ADPCM -- Chrome cannot decode",
    "mlpa": "Dolby TrueHD -- Chrome cannot decode",
}

# MPEG-4 objectTypeIndication values that can show up inside mp4a's esds box.
ESDS_OTI = {
    0x40: ("AAC", True),                 # MPEG-4 Audio (AAC family)
    0x66: ("AAC Main (MPEG-2)", True),
    0x67: ("AAC LC (MPEG-2)", True),
    0x68: ("AAC SSR (MPEG-2)", True),
    0x69: ("MP3 (MPEG-2 Layer III)", True),
    0x6B: ("MP3 (MPEG-1 Layer III)", True),
    0xA5: ("AC-3", False),
    0xA6: ("E-AC-3", False),
    0xA9: ("DTS", False),
    0xAA: ("DTS-HD", False),
    0xAB: ("DTS-HD", False),
    0xAC: ("DTS Express", False),
    0xDD: ("Vorbis", True),
}

# MPEG-4 Audio Object Types (from the AudioSpecificConfig in esds).
AAC_AOT = {
    1: "AAC Main", 2: "AAC LC", 3: "AAC SSR", 4: "AAC LTP",
    5: "HE-AAC (SBR)", 6: "AAC Scalable", 17: "ER AAC LC",
    23: "ER AAC LD", 29: "HE-AACv2 (PS)", 39: "AAC ELD",
}

# H.264 profile_idc -> (name, chrome_ok). Chrome handles Baseline/Main/High
# (8-bit 4:2:0). The high-bit-depth / 4:2:2 / 4:4:4 profiles are not in its build.
H264_PROFILES = {
    66:  ("Baseline", True),
    77:  ("Main", True),
    88:  ("Extended", False),
    100: ("High", True),
    110: ("High 10", False),
    122: ("High 4:2:2", False),
    244: ("High 4:4:4 Predictive", False),
    44:  ("CAVLC 4:4:4", False),
    83:  ("Scalable Baseline", False),
    86:  ("Scalable High", False),
    118: ("Stereo High (MVC)", False),
    128: ("Multiview High (MVC)", False),
}

# Container magic numbers, used to catch files whose extension lies.
MAGIC_SNIFF = [
    (b"\x1a\x45\xdf\xa3", "matroska/webm"),
    (b"OggS", "ogg"),
    (b"RIFF", "riff/avi/wav"),
    (b"fLaC", "flac"),
    (b"FORM", "aiff"),
    (b"\x00\x00\x01\xba", "mpeg-ps"),
    (b"\x00\x00\x01\xb3", "mpeg-es"),
    (b"%PDF", "pdf"),
    (b"\x89PNG", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF8", "gif"),
    (b"PK\x03\x04", "zip"),
    (b"\x1f\x8b", "gzip"),
    (b"#EXTM3U", "m3u playlist"),
    (b"<!DOCTYPE", "html"),
    (b"<!doctype", "html"),
    (b"<html", "html"),
    (b"<HTML", "html"),
    (b"<?xml", "xml"),
    (b"{", "json"),
]

MAX_MOOV_BYTES = 64 * 1024 * 1024     # refuse to buffer an absurd moov
MAX_TOPLEVEL_BOXES = 4096             # loop guard for corrupt files


# --------------------------------------------------------------------------
# Low-level ISO-BMFF helpers
# --------------------------------------------------------------------------

def _u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def _u64(b, o):
    return struct.unpack_from(">Q", b, o)[0]


def _u16(b, o):
    return struct.unpack_from(">H", b, o)[0]


def _fourcc(b, o):
    """Decode a 4-byte box type. Non-ASCII bytes are escaped, never raise."""
    raw = b[o:o + 4]
    try:
        s = raw.decode("latin-1")
    except Exception:
        return repr(raw)
    # keep it printable; some broken files have binary where a type should be
    return "".join(c if 32 <= ord(c) < 127 else "\\x%02x" % ord(c) for c in s)


def iter_boxes_in_buffer(buf, start=0, end=None):
    """
    Yield (box_type, payload_start, payload_end, header_len) for every box in
    an in-memory buffer. Stops cleanly on truncation or a nonsense size.
    """
    if end is None:
        end = len(buf)
    pos = start
    guard = 0
    while pos + 8 <= end:
        guard += 1
        if guard > MAX_TOPLEVEL_BOXES:
            return
        size = _u32(buf, pos)
        btype = _fourcc(buf, pos + 4)
        hdr = 8
        if size == 1:                      # 64-bit largesize
            if pos + 16 > end:
                return
            size = _u64(buf, pos + 8)
            hdr = 16
        elif size == 0:                    # runs to the end of the container
            size = end - pos
        if btype == "uuid":                # 16-byte extended type follows
            hdr += 16
        if size < hdr or pos + size > end:
            # truncated final box: still surface whatever payload we have
            if pos + hdr <= end:
                yield btype, pos + hdr, end, hdr
            return
        yield btype, pos + hdr, pos + size, hdr
        pos += size


class BoxFile:
    """Seek-based top-level box reader. Reads 16 bytes per box, not the payload."""

    def __init__(self, fh, filesize):
        self.fh = fh
        self.size = filesize

    def toplevel(self):
        """
        Yield dicts describing each top-level box.
        'truncated' marks a box whose declared size runs past EOF.
        """
        pos = 0
        guard = 0
        while pos + 8 <= self.size:
            guard += 1
            if guard > MAX_TOPLEVEL_BOXES:
                return
            self.fh.seek(pos)
            head = self.fh.read(16)
            if len(head) < 8:
                return
            size = _u32(head, 0)
            btype = _fourcc(head, 4)
            hdr = 8
            if size == 1:
                if len(head) < 16:
                    return
                size = _u64(head, 8)
                hdr = 16
            elif size == 0:
                size = self.size - pos
            if btype == "uuid":
                hdr += 16
            truncated = (pos + size) > self.size
            if size < hdr:
                # nonsense size -> corrupt; report and stop
                yield {"type": btype, "offset": pos, "size": size,
                       "hdr": hdr, "truncated": True, "bad_size": True}
                return
            yield {"type": btype, "offset": pos, "size": size,
                   "hdr": hdr, "truncated": truncated, "bad_size": False}
            if truncated:
                return
            pos += size

    def read_at(self, off, n):
        self.fh.seek(off)
        return self.fh.read(n)


# --------------------------------------------------------------------------
# Codec-configuration box parsers
# --------------------------------------------------------------------------

def parse_avcC(buf, s, e):
    """AVCDecoderConfigurationRecord -> profile / level / chroma."""
    if e - s < 4:
        return None
    cfg_ver, profile, compat, level = buf[s], buf[s + 1], buf[s + 2], buf[s + 3]
    name, ok = H264_PROFILES.get(profile, ("profile_idc %d" % profile, False))
    out = {
        "profile_idc": profile,
        "profile": name,
        "level": level / 10.0,
        "constraint_flags": compat,
        "chrome_ok": ok,
        # Chrome's codecs= string form, e.g. avc1.640028
        "codec_string": "avc1.%02X%02X%02X" % (profile, compat, level),
    }
    # High-ish profiles carry an optional trailing chroma_format block.
    if profile in (66, 77, 88):
        # Baseline / Main / Extended are 4:2:0 8-bit by definition -- the avcC
        # chroma extension is absent for them, which is correct, not missing data.
        out["chroma_format"] = "4:2:0"
        out["bit_depth_luma"] = 8
        out["bit_depth_chroma"] = 8
        out["chroma_source"] = "implied by profile"
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        p = s + 5                                   # skip lengthSizeMinusOne
        if p < e:
            nsps = buf[p] & 0x1F
            p += 1
            for _ in range(nsps):
                if p + 2 > e:
                    break
                ln = _u16(buf, p)
                p += 2 + ln
            if p < e:
                npps = buf[p]
                p += 1
                for _ in range(npps):
                    if p + 2 > e:
                        break
                    ln = _u16(buf, p)
                    p += 2 + ln
                if p + 4 <= e:
                    chroma = buf[p] & 0x03
                    out["chroma_format"] = {0: "4:0:0", 1: "4:2:0",
                                            2: "4:2:2", 3: "4:4:4"}.get(chroma)
                    out["bit_depth_luma"] = (buf[p + 1] & 0x07) + 8
                    out["bit_depth_chroma"] = (buf[p + 2] & 0x07) + 8
    return out


def parse_hvcC(buf, s, e):
    """HEVCDecoderConfigurationRecord -> enough to name the profile."""
    if e - s < 13:
        return None
    b0 = buf[s + 1]
    space = (b0 >> 6) & 0x03
    tier = (b0 >> 5) & 0x01
    pidc = b0 & 0x1F
    return {
        "profile_space": space,
        "tier": "high" if tier else "main",
        "profile_idc": pidc,
        "profile": {1: "Main", 2: "Main 10", 3: "Main Still Picture",
                    4: "Range Extensions"}.get(pidc, "profile_idc %d" % pidc),
        "level": buf[s + 12] / 30.0,
        "chrome_ok": False,
    }


def parse_av1C(buf, s, e):
    if e - s < 4:
        return None
    b1 = buf[s + 1]
    return {
        "profile": (b1 >> 5) & 0x07,
        "level": b1 & 0x1F,
        "high_bitdepth": bool(buf[s + 2] & 0x40),
        "chrome_ok": True,
    }


# VP9 chromaSubsampling codes from the VPCodecConfigurationRecord.
VP9_CHROMA = {0: "4:2:0 vertical", 1: "4:2:0 colocated", 2: "4:2:2", 3: "4:4:4"}


def parse_vpcC(buf, s, e):
    """
    VPCodecConfigurationBox (FullBox, 4 bytes of version/flags first):
        u8 profile; u8 level; u4 bitDepth; u3 chromaSubsampling;
        u1 videoFullRangeFlag; u8 colourPrimaries; u8 transferCharacteristics;
        u8 matrixCoefficients; u16 codecInitializationDataSize;
    """
    if e - s < 8:
        return None
    profile = buf[s + 4]
    level = buf[s + 5]
    packed = buf[s + 6]
    out = {
        "profile": profile,
        "level": level,
        "bit_depth": (packed >> 4) & 0x0F,
        "chroma_subsampling": (packed >> 1) & 0x07,
        "chroma": VP9_CHROMA.get((packed >> 1) & 0x07, "unknown"),
        "full_range": bool(packed & 0x01),
    }
    if e - s >= 10:
        out["colour_primaries"] = buf[s + 7]
        out["transfer_characteristics"] = buf[s + 8]
        out["matrix_coefficients"] = buf[s + 9]
    # Chrome decodes VP9 in software via libvpx. Profiles 0 (8-bit 4:2:0) and
    # 2 (10/12-bit 4:2:0) are the universally-supported pair. Profiles 1 and 3
    # (non-4:2:0 chroma) are accepted by libvpx but are NOT reliably enabled in
    # every Chrome build/config, so we mark them as uncertain rather than OK.
    out["chrome_ok"] = True
    if profile in (1, 3):
        out["uncertain"] = ("VP9 profile %d uses non-4:2:0 chroma (%s); libvpx can "
                            "decode it but Chrome does not enable every VP9 profile "
                            "in all builds -- needs a real browser test"
                            % (profile, out["chroma"]))
    return out


def _read_desc_len(buf, p, e):
    """MPEG-4 descriptor length: up to 4 bytes, 7 bits each, MSB = continue."""
    val = 0
    for _ in range(4):
        if p >= e:
            return val, p
        b = buf[p]
        p += 1
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return val, p


def parse_esds(buf, s, e):
    """
    Walk the ES_Descriptor tree far enough to recover:
      objectTypeIndication (which real codec is inside mp4a), and
      the AAC AudioObjectType from the DecoderSpecificInfo.
    """
    p = s + 4                       # FullBox version+flags
    if p >= e or buf[p] != 0x03:    # ES_Descriptor tag
        return None
    p += 1
    _, p = _read_desc_len(buf, p, e)
    if p + 3 > e:
        return None
    flags = buf[p + 2]
    p += 3
    if flags & 0x80:                # streamDependenceFlag
        p += 2
    if flags & 0x40:                # URL_Flag
        if p < e:
            p += 1 + buf[p]
    if flags & 0x20:                # OCRstreamFlag
        p += 2
    if p >= e or buf[p] != 0x04:    # DecoderConfigDescriptor tag
        return None
    p += 1
    dcd_len, p = _read_desc_len(buf, p, e)
    if p >= e:
        return None
    oti = buf[p]
    name, ok = ESDS_OTI.get(oti, ("objectTypeIndication 0x%02X" % oti, False))
    out = {"object_type": oti, "codec": name, "chrome_ok": ok}
    p += 13                         # streamType/bufferSize/max+avg bitrate
    if p < e and buf[p] == 0x05:    # DecoderSpecificInfo tag
        p += 1
        dsi_len, p = _read_desc_len(buf, p, e)
        if p + 2 <= e:
            w = _u16(buf, p)
            aot = (w >> 11) & 0x1F
            if aot == 31 and p + 3 <= e:        # escape: 32 + next 6 bits
                aot = 32 + ((_u32(buf, p) >> 17) & 0x3F)
            out["aac_object_type"] = aot
            out["aac_profile"] = AAC_AOT.get(aot, "AOT %d" % aot)
            sr_idx = (w >> 7) & 0x0F
            sr_table = [96000, 88200, 64000, 48000, 44100, 32000, 24000,
                        22050, 16000, 12000, 11025, 8000, 7350]
            if sr_idx < len(sr_table):
                out["sample_rate"] = sr_table[sr_idx]
            out["channels"] = (w >> 3) & 0x0F
    return out


# --------------------------------------------------------------------------
# stsd / trak walking
# --------------------------------------------------------------------------

def find_box(buf, s, e, want):
    for t, ps, pe, _h in iter_boxes_in_buffer(buf, s, e):
        if t == want:
            return ps, pe
    return None


def parse_stsd(buf, s, e, handler):
    """
    Parse a SampleDescriptionBox. Returns a list of sample entries, each with
    its FourCC plus whatever configuration box we could decode.
    """
    entries = []
    if e - s < 8:
        return entries
    count = _u32(buf, s + 4)
    p = s + 8
    for _ in range(min(count, 32)):          # sane cap
        if p + 8 > e:
            break
        esize = _u32(buf, p)
        fmt = _fourcc(buf, p + 4)
        if esize < 8 or p + esize > e:
            entries.append({"format": fmt, "error": "truncated sample entry"})
            break
        eend = p + esize
        ent = {"format": fmt}

        if handler == "vide":
            # VisualSampleEntry: 8 hdr + 78 fixed bytes, then child boxes
            # 16 (SampleEntry) + 70 = 86 bytes before the child boxes;
            # width/height live at +32/+34.
            body = p + 86
            if p + 36 <= eend:
                ent["width"] = _u16(buf, p + 32)
                ent["height"] = _u16(buf, p + 34)
        elif handler == "soun":
            # AudioSampleEntry: 8 hdr + 20 fixed; QuickTime v1/v2 add more
            # SampleEntry = 8 hdr + 6 reserved + 2 data_ref_index = 16 bytes.
            # AudioSampleEntry then adds 8 reserved + channelcount(2) +
            # samplesize(2) + pre_defined(2) + reserved(2) + samplerate(4) = 20.
            # QuickTime "version" overlays the first 2 reserved bytes at p+16.
            ver = _u16(buf, p + 16) if p + 18 <= eend else 0
            body = p + 36
            if p + 36 <= eend:
                ent["channels"] = _u16(buf, p + 24)
                ent["sample_size"] = _u16(buf, p + 26)
                ent["sample_rate"] = _u32(buf, p + 32) >> 16
            if ver == 1:
                body += 16
            elif ver == 2:
                body += 36
        else:
            body = p + 8 + 8

        # Child configuration boxes (avcC/hvcC/av1C/esds/dOps/dfLa/dac3/...)
        if body < eend:
            for t, cs, ce, _h in iter_boxes_in_buffer(buf, body, eend):
                if t == "avcC":
                    ent["avcC"] = parse_avcC(buf, cs, ce)
                elif t == "hvcC":
                    ent["hvcC"] = parse_hvcC(buf, cs, ce)
                elif t == "av1C":
                    ent["av1C"] = parse_av1C(buf, cs, ce)
                elif t == "vpcC":
                    ent["vpcC"] = parse_vpcC(buf, cs, ce)
                elif t == "esds":
                    ent["esds"] = parse_esds(buf, cs, ce)
                elif t == "dOps":
                    ent["dOps"] = True
                elif t == "dfLa":
                    ent["dfLa"] = True
                elif t in ("dac3", "dec3"):
                    ent["dolby_config"] = t
                elif t == "btrt" and ce - cs >= 12:
                    ent["avg_bitrate"] = _u32(buf, cs + 8)
                elif t in ("sinf", "schi"):
                    ent["encrypted"] = True
        entries.append(ent)
        p = eend
    return entries


def parse_moov(buf):
    """moov payload -> {'tracks': [...], 'flags': {...}}"""
    tracks = []
    info = {"fragmented": False, "n_trak": 0}
    for t, s, e, _h in iter_boxes_in_buffer(buf, 0, len(buf)):
        if t == "mvex":
            info["fragmented"] = True
        if t != "trak":
            continue
        info["n_trak"] += 1
        tr = {"handler": None, "entries": [], "sample_count": None}
        mdia = find_box(buf, s, e, "mdia")
        if not mdia:
            tracks.append(tr)
            continue
        ms, me = mdia
        hdlr = find_box(buf, ms, me, "hdlr")
        if hdlr and hdlr[1] - hdlr[0] >= 12:
            tr["handler"] = _fourcc(buf, hdlr[0] + 8)
        mdhd = find_box(buf, ms, me, "mdhd")
        if mdhd:
            hs, he = mdhd
            try:
                ver = buf[hs]
                if ver == 1 and he - hs >= 36:
                    ts = _u32(buf, hs + 20)
                    dur = _u64(buf, hs + 24)
                elif he - hs >= 24:
                    ts = _u32(buf, hs + 12)
                    dur = _u32(buf, hs + 16)
                else:
                    ts = dur = 0
                if ts:
                    tr["duration_s"] = round(dur / ts, 2)
            except Exception:
                pass
        minf = find_box(buf, ms, me, "minf")
        if not minf:
            tracks.append(tr)
            continue
        stbl = find_box(buf, minf[0], minf[1], "stbl")
        if not stbl:
            tracks.append(tr)
            continue
        stsd = find_box(buf, stbl[0], stbl[1], "stsd")
        if stsd:
            tr["entries"] = parse_stsd(buf, stsd[0], stsd[1], tr["handler"])
        stsz = find_box(buf, stbl[0], stbl[1], "stsz")
        if stsz and stsz[1] - stsz[0] >= 12:
            tr["sample_count"] = _u32(buf, stsz[0] + 8)
        tracks.append(tr)
    info["tracks"] = tracks
    return info


# --------------------------------------------------------------------------
# Per-file analysis
# --------------------------------------------------------------------------

WAV_FORMATS = {0x0001: ("PCM", True), 0x0003: ("IEEE float", True),
               0x0006: ("A-law", True), 0x0007: ("mu-law", True),
               0x0011: ("IMA ADPCM", False), 0x0055: ("MP3 in WAV", True),
               0xFFFE: ("WAVE_FORMAT_EXTENSIBLE", True)}


def probe_wav(path):
    """
    Minimal RIFF/WAVE header read, used only to describe a file whose extension
    lied. Chrome does play WAV/PCM, so knowing the format tells us whether a
    rename alone is enough to fix the file.
    """
    try:
        with open(path, "rb") as fh:
            h = fh.read(64)
        if h[:4] != b"RIFF" or h[8:12] != b"WAVE":
            return None
        i = 12
        while i + 8 <= len(h):
            cid = h[i:i + 4]
            csz = struct.unpack_from("<I", h, i + 4)[0]
            if cid == b"fmt " and i + 8 + 16 <= len(h):
                (fmt, ch, rate, _bps, _align, bits) = struct.unpack_from("<HHIIHH", h, i + 8)
                name, ok = WAV_FORMATS.get(fmt, ("format 0x%04X" % fmt, False))
                return {"wav_format": name, "wav_format_code": fmt,
                        "channels": ch, "sample_rate": rate,
                        "bits_per_sample": bits, "chrome_can_decode": ok}
            i += 8 + csz + (csz & 1)
    except Exception:
        return None
    return None


def sniff_magic(head):
    """Identify a non-ISOBMFF container from its first bytes, if we can."""
    probe = head.lstrip()[:16]
    for magic, name in MAGIC_SNIFF:
        if probe.startswith(magic) or head.startswith(magic):
            return name
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mpeg-audio"
    return None


def analyze_mp4(path, size):
    """Walk an ISO-BMFF file and report codecs, layout and integrity."""
    r = {
        "container": "iso-bmff",
        "major_brand": None,
        "compatible_brands": [],
        "video_codecs": [],
        "audio_codecs": [],
        "other_tracks": [],
        "problems": [],
        "notes": [],
        "moov_found": False,
        "needs_faststart": False,
        "truncated": False,
        "duration_s": None,
    }
    with open(path, "rb") as fh:
        head = fh.read(16)
        if len(head) < 8:
            r["problems"].append("file shorter than one box header")
            r["container"] = "unknown"
            return r
        # Not an ISO-BMFF file at all?
        if _fourcc(head, 4) not in ("ftyp", "moov", "mdat", "free", "skip",
                                    "wide", "styp", "pdin", "junk"):
            other = sniff_magic(head)
            r["container"] = other or "unknown"
            r["problems"].append(
                "not an ISO-BMFF/MP4 file (magic looks like %s)" % (other or "unrecognised"))
            return r

        bf = BoxFile(fh, size)
        moov_off = moov_end = None
        mdat_off = None
        n_moof = n_mdat = 0
        order = []
        for box in bf.toplevel():
            order.append(box["type"])
            if box["truncated"]:
                r["truncated"] = True
                r["problems"].append(
                    "truncated: box '%s' at offset %d declares %d bytes but the file ends at %d"
                    % (box["type"], box["offset"], box["size"], size))
            if box["bad_size"]:
                r["problems"].append(
                    "corrupt box header: '%s' at offset %d has impossible size %d"
                    % (box["type"], box["offset"], box["size"]))
            if box["type"] == "ftyp" and r["major_brand"] is None:
                pay = bf.read_at(box["offset"] + box["hdr"],
                                 min(box["size"] - box["hdr"], 256))
                if len(pay) >= 4:
                    r["major_brand"] = _fourcc(pay, 0)
                if len(pay) >= 8:
                    r["minor_version"] = _u32(pay, 4)
                brands = []
                for i in range(8, len(pay) - 3, 4):
                    brands.append(_fourcc(pay, i))
                r["compatible_brands"] = brands
            elif box["type"] == "moov" and moov_off is None:
                moov_off = box["offset"]
                moov_end = min(box["offset"] + box["size"], size)
            elif box["type"] in ("mdat", "moof") and mdat_off is None:
                mdat_off = box["offset"]
            if box["type"] == "moof":
                n_moof += 1
            elif box["type"] == "mdat":
                n_mdat += 1
        r["box_order"] = order[:12]
        r["n_moof"] = n_moof
        r["n_mdat"] = n_mdat
        if n_mdat == 0 and n_moof == 0:
            r["problems"].append("no media data (no mdat and no moof boxes)")

        if moov_off is None:
            r["problems"].append("no 'moov' atom found -- unplayable/incomplete file")
            return r
        r["moov_found"] = True
        r["moov_offset"] = moov_off
        if mdat_off is not None and moov_off > mdat_off:
            r["needs_faststart"] = True

        moov_len = moov_end - moov_off
        if moov_len > MAX_MOOV_BYTES:
            r["problems"].append("moov atom is implausibly large (%d bytes)" % moov_len)
            return r
        buf = bf.read_at(moov_off, moov_len)
        # strip the moov header so the buffer starts at its payload
        hdr = 8
        if len(buf) >= 8 and _u32(buf, 0) == 1:
            hdr = 16
        buf = buf[hdr:]
        if len(buf) < moov_len - hdr:
            r["problems"].append("moov atom is truncated (read %d of %d bytes)"
                                 % (len(buf), moov_len - hdr))
        try:
            mv = parse_moov(buf)
        except Exception as exc:
            r["problems"].append("moov parse failed: %s: %s" % (type(exc).__name__, exc))
            return r

    r["fragmented"] = mv.get("fragmented", False)
    for tr in mv["tracks"]:
        h = tr.get("handler")
        dur = tr.get("duration_s")
        if dur and (r["duration_s"] is None or dur > r["duration_s"]):
            r["duration_s"] = dur
        for ent in tr["entries"]:
            fmt = ent.get("format")
            rec = {"fourcc": fmt}
            if tr.get("sample_count") == 0 and not mv.get("fragmented"):
                # Non-fragmented file with an empty sample table = genuinely empty.
                rec["empty_track"] = True
            if h == "vide":
                if "width" in ent:
                    rec["dimensions"] = "%dx%d" % (ent["width"], ent["height"])
                if "avcC" in ent and ent["avcC"]:
                    rec.update({k: v for k, v in ent["avcC"].items()
                                if k in ("profile", "level", "chroma_format",
                                         "bit_depth_luma", "codec_string")})
                    rec["chrome_ok"] = ent["avcC"]["chrome_ok"]
                elif "hvcC" in ent and ent["hvcC"]:
                    hv = ent["hvcC"]
                    rec["profile"] = hv["profile"]
                    rec["profile_idc"] = hv["profile_idc"]
                    rec["level"] = hv["level"]
                    rec["tier"] = hv["tier"]
                    rec["codec_string"] = "%s.%d.%X.%s%d" % (
                        fmt, hv["profile_idc"], 1 << hv["profile_idc"],
                        "H" if hv["tier"] == "high" else "L",
                        int(round(hv["level"] * 30)))
                elif "av1C" in ent and ent["av1C"]:
                    av = ent["av1C"]
                    rec["profile"] = "AV1 profile %s" % av["profile"]
                    rec["profile_idc"] = av["profile"]
                    rec["level"] = av["level"]
                    rec["bit_depth_luma"] = 10 if av.get("high_bitdepth") else 8
                    rec["codec_string"] = "av01.%d.%02dM.%02d" % (
                        av["profile"], av["level"],
                        10 if av.get("high_bitdepth") else 8)
                elif "vpcC" in ent and ent["vpcC"]:
                    vc = ent["vpcC"]
                    rec["profile"] = "VP9 profile %s" % vc["profile"]
                    rec["profile_idc"] = vc["profile"]
                    rec["level"] = vc["level"] / 10.0
                    rec["chroma_format"] = vc.get("chroma")
                    rec["bit_depth_luma"] = vc.get("bit_depth")
                    rec["codec_string"] = "vp09.%02d.%02d.%02d" % (
                        vc["profile"], vc["level"], vc.get("bit_depth") or 8)
                    if vc.get("uncertain"):
                        rec["uncertain"] = vc["uncertain"]
                if ent.get("encrypted"):
                    rec["encrypted"] = True
                r["video_codecs"].append(rec)
            elif h == "soun":
                for k in ("channels", "sample_rate"):
                    if k in ent:
                        rec[k] = ent[k]
                if ent.get("esds"):
                    es = ent["esds"]
                    rec["esds_codec"] = es["codec"]
                    rec["esds_oti"] = es["object_type"]
                    rec["chrome_ok"] = es["chrome_ok"]
                    if "aac_profile" in es:
                        rec["profile"] = es["aac_profile"]
                        rec["profile_idc"] = es["aac_object_type"]
                        rec["codec_string"] = "mp4a.%02X.%d" % (
                            es["object_type"], es["aac_object_type"])
                        # Chrome decodes AAC-LC everywhere. SBR (HE-AAC) and PS
                        # (HE-AACv2) are handled by Chromium's bundled ffmpeg.
                        if es["aac_object_type"] in (5, 29):
                            rec["note"] = ("HE-AAC/HE-AACv2 -- Chromium's bundled "
                                           "ffmpeg decodes SBR/PS; at minimum the "
                                           "AAC-LC core layer always decodes")
                    else:
                        rec["codec_string"] = "mp4a.%02X" % es["object_type"]
                if ent.get("dolby_config"):
                    rec["dolby_config"] = ent["dolby_config"]
                if ent.get("encrypted"):
                    rec["encrypted"] = True
                r["audio_codecs"].append(rec)
            else:
                r["other_tracks"].append({"handler": h, "fourcc": fmt})
    if not r["video_codecs"] and not r["audio_codecs"]:
        r["problems"].append("moov contains no audio or video sample entries")
    return r


# --- MP3 -----------------------------------------------------------------

MPEG_BITRATES = {
    # (version_id, layer) -> bitrate table in kbps
    (3, 3): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, -1],
    (3, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, -1],
    (3, 1): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, -1],
    (2, 3): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, -1],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, -1],
    (2, 1): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, -1],
}
MPEG_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
MPEG_VER_NAME = {3: "MPEG-1", 2: "MPEG-2", 0: "MPEG-2.5"}
LAYER_NAME = {3: "Layer III", 2: "Layer II", 1: "Layer I"}


def decode_frame_header(hdr):
    """Decode 4 MPEG audio header bytes -> dict, or None if not a valid frame."""
    if len(hdr) < 4 or hdr[0] != 0xFF or (hdr[1] & 0xE0) != 0xE0:
        return None
    ver = (hdr[1] >> 3) & 0x03            # 3=MPEG1, 2=MPEG2, 0=MPEG2.5, 1=reserved
    layer = (hdr[1] >> 1) & 0x03          # 3=LayerI, 2=LayerII, 1=LayerIII
    if ver == 1 or layer == 0:
        return None
    layer_num = {3: 1, 2: 2, 1: 3}[layer]
    br_i = (hdr[2] >> 4) & 0x0F
    sr_i = (hdr[2] >> 2) & 0x03
    if br_i in (0, 15) or sr_i == 3:
        return None
    key = (3 if ver == 3 else 2, {1: 3, 2: 2, 3: 1}[layer_num])
    bitrate = MPEG_BITRATES[key][br_i]
    srate = MPEG_RATES[ver][sr_i]
    pad = (hdr[2] >> 1) & 0x01
    chan = (hdr[3] >> 6) & 0x03
    if layer_num == 1:
        flen = (12 * bitrate * 1000 // srate + pad) * 4
    else:
        spf = 384 if layer_num == 1 else (1152 if (layer_num == 3 and ver == 3) or layer_num == 2 else 576)
        if layer_num == 3 and ver != 3:
            spf = 576
        flen = spf // 8 * bitrate * 1000 // srate + pad
    return {
        "version": MPEG_VER_NAME[ver],
        "layer": LAYER_NAME[layer_num],
        "bitrate_kbps": bitrate,
        "sample_rate": srate,
        "channel_mode": ["stereo", "joint stereo", "dual channel", "mono"][chan],
        "frame_len": flen,
    }


def analyze_mp3(path, size):
    """
    Confirm a .mp3 really is MPEG audio: skip ID3v2, find sync, then require a
    chain of consecutive valid frames (a single 0xFFEx is far too weak a test --
    random binary hits it constantly).
    """
    r = {"container": "mpeg-audio", "problems": [], "notes": [],
         "audio_codecs": [], "id3v2": False}
    with open(path, "rb") as fh:
        head = fh.read(16)
        if not head:
            r["problems"].append("zero-byte file")
            r["container"] = "empty"
            return r
        off = 0
        if head[:3] == b"ID3":
            r["id3v2"] = True
            r["id3v2_version"] = "2.%d.%d" % (head[3], head[4])
            sz = ((head[6] & 0x7F) << 21) | ((head[7] & 0x7F) << 14) | \
                 ((head[8] & 0x7F) << 7) | (head[9] & 0x7F)
            off = 10 + sz
            if head[5] & 0x10:       # footer present
                off += 10
        else:
            magic = sniff_magic(head)
            if magic and magic != "mpeg-audio":
                r["container"] = magic
                if magic == "riff/avi/wav":
                    w = probe_wav(path)
                    if w:
                        r["wav_details"] = w
                        r["notes"].append(
                            "actual content: WAV %s, %d Hz, %d ch, %d-bit -- Chrome "
                            "%s decode this once it is served as audio/wav"
                            % (w["wav_format"], w["sample_rate"], w["channels"],
                               w["bits_per_sample"],
                               "CAN" if w["chrome_can_decode"] else "still cannot"))
                r["problems"].append(
                    "extension says .mp3 but the bytes are %s -- the HTTP server "
                    "labels it audio/mpeg, so Chrome rejects it before decoding"
                    % magic)
                r["actual_container"] = magic
                return r
            if len(head) >= 8 and _fourcc(head, 4) == "ftyp":
                r["container"] = "iso-bmff"
                r["problems"].append(
                    "extension says .mp3 but the file is an MP4/ISO-BMFF container")
                return r

        if off >= size:
            r["problems"].append("ID3v2 tag claims to extend past the end of the file")
            return r

        # Scan for the first frame sync, allowing for junk/padding after the tag.
        fh.seek(off)
        window = fh.read(min(256 * 1024, size - off))
        found = None
        i = 0
        while i < len(window) - 4:
            if window[i] == 0xFF and (window[i + 1] & 0xE0) == 0xE0:
                fr = decode_frame_header(window[i:i + 4])
                if fr:
                    # Require 3 further consecutive frames at the predicted offsets.
                    ok, p, chain = True, i, [fr]
                    for _ in range(3):
                        p += chain[-1]["frame_len"]
                        if p + 4 > len(window):
                            break
                        nxt = decode_frame_header(window[p:p + 4])
                        if not nxt:
                            ok = False
                            break
                        chain.append(nxt)
                    if ok:
                        found = (i, fr, len(chain))
                        break
            i += 1

        if not found:
            magic = sniff_magic(head)
            r["container"] = magic or "unknown"
            r["problems"].append(
                "no valid MPEG audio frame chain found%s" %
                (" (looks like %s)" % magic if magic else ""))
            return r

        sync_at, fr, nchain = found
        r["first_frame_offset"] = off + sync_at
        r["frames_verified"] = nchain
        if sync_at > 0:
            r["notes"].append("%d bytes of junk between the tag and the first frame"
                              % sync_at)
        # Xing/Info/VBRI marker -> variable bitrate
        tail = window[sync_at:sync_at + 200]
        vbr = ("Xing" if b"Xing" in tail else
               "Info" if b"Info" in tail else
               "VBRI" if b"VBRI" in tail else None)
        r["audio_codecs"].append({
            "fourcc": "mp3",
            "codec": "%s %s" % (fr["version"], fr["layer"]),
            "bitrate_kbps": fr["bitrate_kbps"],
            "sample_rate": fr["sample_rate"],
            "channels": 1 if fr["channel_mode"] == "mono" else 2,
            "channel_mode": fr["channel_mode"],
            "vbr_header": vbr,
            "chrome_ok": fr["layer"] == "Layer III",
        })
        if fr["layer"] != "Layer III":
            r["problems"].append(
                "MPEG %s (not Layer III) -- Chrome only decodes MP3 (Layer III)"
                % fr["layer"])
    return r


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

def verdict(entry, res):
    """
    Roll the parsed facts up into: playable / unplayable / warn, plus reasons.
    """
    reasons = []
    warnings = []
    playable = True

    if entry["size"] == 0:
        return "unplayable", ["zero-byte file"], []

    for p in res.get("problems", []):
        reasons.append(p)
        playable = False

    for v in res.get("video_codecs", []):
        fc = v["fourcc"]
        if fc in VIDEO_BAD:
            playable = False
            reasons.append("video track is %s [%s]" % (VIDEO_BAD[fc], fc))
        elif fc in VIDEO_OK:
            if v.get("chrome_ok") is False:
                playable = False
                reasons.append("H.264 %s profile -- outside Chrome's decoder (%s)"
                               % (v.get("profile"), fc))
        else:
            playable = False
            reasons.append("unrecognised video sample entry '%s'" % fc)
        if v.get("encrypted"):
            playable = False
            reasons.append("video track is DRM-encrypted (sinf/schi present)")
        if v.get("empty_track"):
            warnings.append("video track '%s' has zero samples" % fc)

    for a in res.get("audio_codecs", []):
        fc = a["fourcc"]
        if fc in AUDIO_BAD:
            playable = False
            reasons.append("audio track is %s [%s]" % (AUDIO_BAD[fc], fc))
        elif fc == "mp4a":
            if a.get("chrome_ok") is False:
                playable = False
                reasons.append("mp4a carries %s -- Chrome cannot decode it"
                               % a.get("esds_codec"))
            elif a.get("esds_codec") is None:
                warnings.append("mp4a sample entry has no esds box -- "
                                "cannot confirm the payload is AAC")
        elif fc in AUDIO_OK or fc == "mp3":
            if a.get("chrome_ok") is False:
                playable = False
                reasons.append("audio is %s -- unsupported" % a.get("codec", fc))
        else:
            playable = False
            reasons.append("unrecognised audio sample entry '%s'" % fc)
        if a.get("encrypted"):
            playable = False
            reasons.append("audio track is DRM-encrypted")

    if res.get("needs_faststart"):
        warnings.append("moov atom is at the end of the file -- needs faststart")
    if res.get("truncated"):
        warnings.append("file appears truncated")
    if res.get("fragmented"):
        warnings.append(
            "fragmented MP4 (mvex present, %d moof fragments) -- moov is an init "
            "segment so its sample tables are empty by design; Chrome plays this "
            "from a plain <video src> as long as the codec is supported"
            % res.get("n_moof", 0))
    if (entry["kind"] == "video" and res.get("moov_found")
            and not res.get("video_codecs") and res.get("audio_codecs")):
        warnings.append("audio-only despite .mp4 extension")

    if playable:
        return ("playable" if not warnings else "playable_with_warnings"), reasons, warnings
    return "unplayable", reasons, warnings


# Issue classes. These are deliberately distinct because each implies a
# different remedy, and the player UI should treat them differently:
#   ok                 -- plays, nothing to do
#   cannot_decode      -- Chrome has no decoder for the codec (HEVC, AC-3, ...)
#   container_mismatch -- the bytes are a different format than the extension
#                         claims, so the server sends the wrong Content-Type
#   truncated          -- the file ends mid-box / is incomplete
#   no_moov            -- no moov atom at all; nothing is playable
#   zero_byte          -- empty file
#   needs_faststart    -- DECODES FINE, but moov is at EOF: Chrome must fetch
#                         the whole file before the first frame appears
#   empty_track        -- a track exists but declares zero samples
#   fragmented         -- fragmented MP4 (mvex); plays, worth knowing
#   audio_only_video_ext -- .mp4 with no video track
#   unknown_codec      -- a sample entry fourcc we could not identify
ISSUE_ORDER = ["zero_byte", "no_moov", "truncated", "container_mismatch",
               "cannot_decode", "unknown_codec", "empty_track",
               "needs_faststart", "fragmented", "audio_only_video_ext", "ok"]


def classify(res, reasons, warnings, size):
    """Return the list of issue classes that apply to this file."""
    classes = []
    blob = " ".join(reasons)
    if size == 0:
        classes.append("zero_byte")
    if "no 'moov'" in blob:
        classes.append("no_moov")
    if res.get("truncated") or "truncated" in blob:
        classes.append("truncated")
    if "extension says" in blob or "not an ISO-BMFF" in blob:
        classes.append("container_mismatch")
    if ("Chrome" in blob and "cannot" in blob) or "HEVC" in blob or \
       "outside Chrome's decoder" in blob or "Dolby" in blob:
        classes.append("cannot_decode")
    if "unrecognised" in blob:
        classes.append("unknown_codec")
    if any("zero samples" in w for w in warnings):
        classes.append("empty_track")
    if res.get("needs_faststart"):
        classes.append("needs_faststart")
    if res.get("fragmented"):
        classes.append("fragmented")
    if any("audio-only" in w for w in warnings):
        classes.append("audio_only_video_ext")
    if not classes:
        classes.append("ok")
    return sorted(set(classes), key=lambda c: ISSUE_ORDER.index(c)
                  if c in ISSUE_ORDER else 99)


def flatten(res):
    """
    Collapse the parsed tree into the flat fields the player UI consumes.
    Multi-track files join their codecs with '+' so nothing is hidden.
    """
    vs = res.get("video_codecs", []) or []
    as_ = res.get("audio_codecs", []) or []
    flat = {
        "video_codec": "+".join(v["fourcc"] for v in vs) or None,
        "audio_codec": "+".join(a["fourcc"] for a in as_) or None,
        "video_profile": "+".join(str(v.get("profile")) for v in vs if v.get("profile")) or None,
        "video_level": "+".join(str(v.get("level")) for v in vs if v.get("level") is not None) or None,
        "video_codec_string": "+".join(v["codec_string"] for v in vs if v.get("codec_string")) or None,
        "video_resolution": "+".join(v["dimensions"] for v in vs if v.get("dimensions")) or None,
        "video_chroma": "+".join(str(v["chroma_format"]) for v in vs if v.get("chroma_format")) or None,
        "video_bit_depth": next((v["bit_depth_luma"] for v in vs if v.get("bit_depth_luma")), None),
        "audio_profile": "+".join(str(a.get("profile")) for a in as_ if a.get("profile")) or None,
        "audio_codec_string": "+".join(a["codec_string"] for a in as_ if a.get("codec_string")) or None,
        "audio_sample_rate": next((a.get("sample_rate") for a in as_ if a.get("sample_rate")), None),
        "audio_channels": next((a.get("channels") for a in as_ if a.get("channels")), None),
        "n_video_tracks": len(vs),
        "n_audio_tracks": len(as_),
    }
    # Full Chrome codecs= parameter, e.g. 'avc1.640028, mp4a.40.2'
    parts = [v["codec_string"] for v in vs if v.get("codec_string")]
    parts += [a["codec_string"] for a in as_ if a.get("codec_string")]
    flat["chrome_codecs_param"] = ", ".join(parts) or None
    # 'uncertain' = the parser is genuinely unsure Chrome supports this and a
    # real browser test is needed. Routine explanatory notes go in 'codec_notes'
    # so 'uncertain' stays a meaningful, actionable signal for the player UI.
    unc = [v["uncertain"] for v in vs if v.get("uncertain")]
    unc += [a["uncertain"] for a in as_ if a.get("uncertain")]
    flat["uncertain"] = unc or None
    notes = [v["note"] for v in vs if v.get("note")]
    notes += [a["note"] for a in as_ if a.get("note")]
    flat["codec_notes"] = notes or None
    return flat


def suggest_fix(res, reasons):
    """One actionable sentence per broken file."""
    blob = " ".join(reasons)
    if "HEVC" in blob or "Dolby Vision" in blob:
        return ("remux/transcode video to H.264: "
                "ffmpeg -i IN -c:v libx264 -crf 20 -c:a copy -movflags +faststart OUT.mp4")
    if "riff" in blob or "actually" in blob or "bytes are" in blob:
        return "rename to the real extension (.%s) so the server sends the right Content-Type" % (
            {"riff/avi/wav": "wav", "matroska/webm": "webm", "ogg": "ogg",
             "flac": "flac", "iso-bmff": "m4a"}.get(res.get("actual_container")
                                                   or res.get("container"), "bin"))
    if "AC-3" in blob or "E-AC-3" in blob or "DTS" in blob:
        return "re-encode audio to AAC: ffmpeg -i IN -c:v copy -c:a aac -b:a 192k OUT.mp4"
    if "no 'moov'" in blob or "truncated" in blob:
        return "file is incomplete -- re-download it"
    if "zero-byte" in blob:
        return "empty file -- delete or re-download"
    return "inspect manually"


def codec_signature(res):
    """Short 'video+audio' label used for the summary table."""
    def vlabel(v):
        fc = v["fourcc"]
        p = v.get("profile")
        return "%s(%s)" % (fc, p) if p else fc

    def alabel(a):
        fc = a["fourcc"]
        if fc == "mp4a":
            return "mp4a(%s)" % (a.get("profile") or a.get("esds_codec") or "?")
        if fc == "mp3":
            return a.get("codec", "mp3")
        return fc

    v = "+".join(vlabel(x) for x in res.get("video_codecs", [])) or "-"
    a = "+".join(alabel(x) for x in res.get("audio_codecs", [])) or "-"
    return "%s / %s" % (v, a)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

_progress_lock = threading.Lock()
_done = 0


def analyze_one(entry, total):
    global _done
    path = entry["path"]
    out = {"id": entry.get("id"), "path": path, "name": entry.get("name"),
           "folder": entry.get("folder"), "ext": entry.get("ext"),
           "kind": entry.get("kind"), "size": entry.get("size")}
    try:
        st = os.stat(path)
        size = st.st_size
        out["size_on_disk"] = size
        if size != entry.get("size"):
            out.setdefault("notes", []).append(
                "size changed since indexing (%d -> %d)" % (entry.get("size"), size))
        if size == 0:
            res = {"container": "empty", "problems": ["zero-byte file"]}
        elif entry.get("ext", "").lower() in ("mp3",):
            res = analyze_mp3(path, size)
        else:
            res = analyze_mp4(path, size)
    except FileNotFoundError:
        res = {"container": "missing", "problems": ["file does not exist on disk"]}
    except PermissionError:
        res = {"container": "unreadable", "problems": ["permission denied"]}
    except Exception as exc:
        res = {"container": "error",
               "problems": ["parser crashed: %s: %s" % (type(exc).__name__, exc)]}
    out.update(res)
    st_, reasons, warns = verdict(entry, res)
    out["status"] = st_
    out["reasons"] = reasons
    out["warnings"] = warns
    # --- flat, machine-readable fields for the player UI ------------------
    out.update(flatten(res))
    out["playable_in_chrome"] = (st_ != "unplayable")
    out["needs_faststart"] = bool(res.get("needs_faststart"))
    out["issue_classes"] = classify(res, reasons, warns, out.get("size") or 0)
    out["issue_class"] = out["issue_classes"][0]
    out["reason"] = ("; ".join(reasons) if reasons else None)
    out["warning_text"] = ("; ".join(warns) if warns else None)
    if st_ == "unplayable":
        out["suggested_fix"] = suggest_fix(res, reasons)
    else:
        out["suggested_fix"] = ("ffmpeg -i IN -c copy -movflags +faststart OUT.mp4"
                                if out["needs_faststart"] else None)
    out["codec_signature"] = codec_signature(res)
    with _progress_lock:
        _done += 1
        if _done % 25 == 0 or _done == total:
            sys.stderr.write("\r  scanned %4d / %d" % (_done, total))
            sys.stderr.flush()
    return out


def verify_mode(path):
    """Dump the box tree + parse result for one file, for hexdump cross-checks."""
    size = os.path.getsize(path)
    print("FILE: %s  (%d bytes)" % (path, size))
    with open(path, "rb") as fh:
        bf = BoxFile(fh, size)
        print("\nTop-level boxes:")
        for b in bf.toplevel():
            print("  @%-12d %-6s size=%-12d hdr=%d%s"
                  % (b["offset"], b["type"], b["size"], b["hdr"],
                     "  TRUNCATED" if b["truncated"] else ""))
    print("\nParsed:")
    print(json.dumps(analyze_mp4(path, size), indent=2))


def main():
    ap = argparse.ArgumentParser(
        description="Audit Chrome playability of a media library by parsing "
                    "container metadata in pure Python (no ffprobe needed).")
    ap.add_argument("--library",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "library.json"),
                    help="path to library.json index")
    ap.add_argument("--json", dest="out",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "codec_report.json"),
                    help="where to write the full JSON report")
    ap.add_argument("--workers", type=int, default=24,
                    help="thread-pool size (this is I/O bound)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only scan the first N files (debugging)")
    ap.add_argument("--verify", metavar="PATH",
                    help="dump the box tree of one file and exit")
    args = ap.parse_args()

    if args.verify:
        verify_mode(args.verify)
        return

    with open(args.library) as fh:
        lib = json.load(fh)
    files = lib["files"]
    if args.limit:
        files = files[:args.limit]
    total = len(files)

    sys.stderr.write("Scanning %d files with %d threads...\n" % (total, args.workers))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda e: analyze_one(e, total), files))
    elapsed = time.time() - t0
    sys.stderr.write("\n  done in %.1fs\n" % elapsed)

    results.sort(key=lambda r: (r.get("id") if r.get("id") is not None else 0))

    unplayable = [r for r in results if r["status"] == "unplayable"]
    faststart = [r for r in results if r.get("needs_faststart")]
    warned = [r for r in results if r["status"] == "playable_with_warnings"]
    sig = Counter(r["codec_signature"] for r in results)

    report = {
        "generated": int(time.time()),
        "library": os.path.abspath(args.library),
        "elapsed_seconds": round(elapsed, 2),
        "totals": {
            "scanned": total,
            "playable": sum(1 for r in results if r["status"] == "playable"),
            "playable_with_warnings": len(warned),
            "unplayable": len(unplayable),
            "needs_faststart": len(faststart),
            "bytes": sum(r.get("size") or 0 for r in results),
        },
        "codec_combinations": dict(sig.most_common()),
        "brands": dict(Counter(r.get("major_brand") for r in results
                               if r.get("major_brand")).most_common()),
        "unplayable": [{"path": r["path"], "reasons": r["reasons"],
                        "codec": r["codec_signature"], "size": r.get("size"),
                        "suggested_fix": r.get("suggested_fix")}
                       for r in unplayable],
        "unplayable_by_folder": dict(
            Counter(r["folder"] for r in unplayable).most_common()),
        "needs_faststart": [{"id": r["id"], "path": r["path"],
                             "size": r.get("size"),
                             "moov_offset": r.get("moov_offset"),
                             "codec": r["codec_signature"]} for r in faststart],
        "issue_class_counts": dict(Counter(
            c for r in results for c in r["issue_classes"]).most_common()),
        "uncertain": [{"id": r["id"], "path": r["path"],
                       "codec": r["codec_signature"], "notes": r["uncertain"]}
                      for r in results if r.get("uncertain")],
        # id -> minimal record, so the player can look a file up in O(1)
        "by_id": {str(r["id"]): {
            "playable_in_chrome": r["playable_in_chrome"],
            "needs_faststart": r["needs_faststart"],
            "video_codec": r["video_codec"],
            "audio_codec": r["audio_codec"],
            "issue_class": r["issue_class"],
            "reason": r["reason"],
        } for r in results},
        "files": results,
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1)

    # ---- console summary -------------------------------------------------
    t = report["totals"]
    print("=" * 74)
    print("Chrome playability audit -- %d files, %.2f GB, %.1fs"
          % (t["scanned"], t["bytes"] / 1e9, elapsed))
    print("=" * 74)
    print("  playable                : %d" % t["playable"])
    print("  playable (with warnings): %d" % t["playable_with_warnings"])
    print("  UNPLAYABLE              : %d" % t["unplayable"])
    print("  needs faststart         : %d" % t["needs_faststart"])
    print("\nCodec combinations (video / audio):")
    for k, v in sig.most_common():
        print("  %5d  %s" % (v, k))
    if unplayable:
        print("\nFiles Chrome CANNOT play -- grouped by folder:")
        byfolder = defaultdict(list)
        for r in unplayable:
            byfolder[r["folder"]].append(r)
        shown = 0
        for folder, rows in sorted(byfolder.items(), key=lambda kv: -len(kv[1])):
            print("\n  %s\n    (%d unplayable file%s here)"
                  % (folder, len(rows), "" if len(rows) == 1 else "s"))
            for r in rows:
                if shown >= 30:
                    break
                print("      %s\n          why: %s" % (r["name"], "; ".join(r["reasons"])))
                shown += 1
            if shown >= 30:
                break
        if len(unplayable) > shown:
            print("\n  ... and %d more unplayable files not listed above "
                  "(full list in %s)" % (len(unplayable) - shown, args.out))
        print("\n  Fixes needed:")
        for fix, n in Counter(r.get("suggested_fix") for r in unplayable).most_common():
            print("    %5d x  %s" % (n, fix))
    if faststart:
        print("\nNeeds faststart -- moov atom sits at EOF, so Chrome must download")
        print("the whole file before playback starts (%d file%s):"
              % (len(faststart), "" if len(faststart) == 1 else "s"))
        for r in faststart[:30]:
            print("  %8.1f MB  %s" % ((r.get("size") or 0) / 1e6, r["path"]))
        if len(faststart) > 30:
            print("  ... and %d more" % (len(faststart) - 30))
        print("  fix: ffmpeg -i IN -c copy -movflags +faststart OUT.mp4")
    print("\nFull report: %s" % os.path.abspath(args.out))


if __name__ == "__main__":
    main()
