#!/usr/bin/env python3
"""Batch-convert media that Chrome cannot play into formats it can.

Reads codec_report.json, converts H.265/HEVC video to H.264, and remuxes
"needs faststart" files so they start streaming immediately.

ORIGINALS ARE NEVER MODIFIED OR DELETED. Converted copies are written to a
parallel tree under ~/Videos/medialib-converted/ and recorded in a manifest.

Safe to interrupt (Ctrl+C) and re-run: finished work is skipped.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HOME = Path.home()
APP_DIR = HOME / ".local/share/medialib"
REPORT = APP_DIR / "codec_report.json"
OUT_ROOT = HOME / "Videos" / "medialib-converted"
MANIFEST = APP_DIR / "converted_manifest.json"

stop = False


def on_sigint(signum, frame):
    global stop
    if not stop:
        stop = True
        print("\n[!] Stopping after the in-flight files finish… (Ctrl+C again to force)",
              file=sys.stderr, flush=True)
    else:
        raise KeyboardInterrupt


def need(binary):
    p = shutil.which(binary)
    if not p:
        sys.exit(f"ERROR: {binary} not found. Install it first:\n"
                 f"    sudo apt install -y ffmpeg")
    return p


def out_path_for(src: Path) -> Path:
    """Mirror the source tree under OUT_ROOT, keyed by path relative to HOME."""
    try:
        rel = src.relative_to(HOME)
    except ValueError:
        rel = Path(*src.parts[1:])
    return (OUT_ROOT / rel).with_suffix(".mp4")


def probe_ok(ffprobe: str, path: Path) -> bool:
    """Confirm a produced file really holds a decodable H.264 video track."""
    if not path.exists() or path.stat().st_size < 1024:
        return False
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,nb_frames",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False
        streams = json.loads(r.stdout or "{}").get("streams", [])
        return bool(streams) and streams[0].get("codec_name") == "h264"
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return False


def detect_vaapi(ffmpeg: str) -> str | None:
    """Return a VAAPI device path if hardware H.264 encoding looks usable."""
    dev = "/dev/dri/renderD128"
    if not os.path.exists(dev):
        return None
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=30)
        if "h264_vaapi" not in r.stdout:
            return None
        # Prove it actually initialises; presence of the encoder is not enough.
        t = subprocess.run(
            [ffmpeg, "-v", "error", "-vaapi_device", dev,
             "-f", "lavfi", "-i", "testsrc=size=320x240:duration=0.3:rate=10",
             "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=60)
        return dev if t.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def build_cmd(ffmpeg, src, dst, args, vaapi):
    """ffmpeg invocation. Audio is always re-encoded to AAC-LC, because the
    HE-AACv2 tracks in this library are not reliably decodable in Chrome."""
    base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if vaapi and not args.software:
        return base + [
            "-hwaccel", "vaapi", "-hwaccel_device", vaapi,
            "-hwaccel_output_format", "vaapi", "-i", str(src),
            "-vf", "scale_vaapi=format=nv12",
            "-c:v", "h264_vaapi", "-qp", str(args.crf),
            *(["-c:a", "copy"] if args.copy_audio else ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]),
            "-map_metadata", "0", "-movflags", "+faststart", "-f", "mp4",
            str(dst),
        ]
    return base + [
        "-i", str(src),
        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-crf", str(args.crf), "-preset", args.preset,
        *(["-c:a", "copy"] if args.copy_audio else ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]),
        "-map_metadata", "0", "-movflags", "+faststart", "-f", "mp4",
        str(dst),
    ]


def convert_one(job, ffmpeg, ffprobe, args, vaapi):
    src, dst, mode = job["src"], job["dst"], job["mode"]
    if dst.exists() and probe_ok(ffprobe, dst) and not args.force:
        return {"src": str(src), "dst": str(dst), "status": "skipped", "mode": mode}

    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(".mp4.part")
    part.unlink(missing_ok=True)

    t0 = time.time()
    if mode == "faststart":
        # Container-only fix: copy both streams, just move the moov atom.
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-i", str(src), "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(part)]
    else:
        cmd = build_cmd(ffmpeg, src, part, args, vaapi)

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        part.unlink(missing_ok=True)
        return {"src": str(src), "dst": str(dst), "status": "timeout", "mode": mode}

    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        part.unlink(missing_ok=True)
        return {"src": str(src), "dst": str(dst), "status": "failed", "mode": mode,
                "error": err[-1] if err else f"exit {r.returncode}"}

    if mode == "faststart":
        ok = part.exists() and part.stat().st_size > 1024
    else:
        ok = probe_ok(ffprobe, part)
    if not ok:
        part.unlink(missing_ok=True)
        return {"src": str(src), "dst": str(dst), "status": "verify_failed", "mode": mode}

    part.replace(dst)  # atomic: a partial file is never mistaken for a finished one
    return {"src": str(src), "dst": str(dst), "status": "converted", "mode": mode,
            "seconds": round(time.time() - t0, 1),
            "in_bytes": src.stat().st_size, "out_bytes": dst.stat().st_size}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="list the work, change nothing")
    ap.add_argument("--jobs", type=int, default=3, help="parallel ffmpeg processes (default 3)")
    ap.add_argument("--crf", type=int, default=21, help="quality, lower is better (default 21)")
    ap.add_argument("--preset", default="veryfast", help="x264 preset (default veryfast)")
    ap.add_argument("--limit", type=int, default=0, help="convert at most N files (for testing)")
    ap.add_argument("--software", action="store_true", help="force software encoding")
    ap.add_argument("--copy-audio", action="store_true",
                    help="copy audio instead of re-encoding to AAC-LC (faster, lossless, but relies on Chrome decoding HE-AACv2)")
    ap.add_argument("--force", action="store_true", help="re-convert even if output exists")
    ap.add_argument("--timeout", type=int, default=1800, help="per-file timeout seconds")
    ap.add_argument("--faststart-only", action="store_true", help="only remux faststart files")
    ap.add_argument("--hevc-only", action="store_true", help="only convert HEVC files")
    args = ap.parse_args()

    ffmpeg, ffprobe = need("ffmpeg"), need("ffprobe")
    if not REPORT.exists():
        sys.exit(f"No codec report at {REPORT}\nRun: python3 {APP_DIR}/codec_check.py")
    report = json.loads(REPORT.read_text(encoding="utf-8"))

    # ---- build the job list ----
    jobs, seen = [], set()
    if not args.faststart_only:
        for e in report.get("unplayable", []):
            src = Path(e["path"])
            codec = json.dumps(e.get("codec", "")).lower()
            if "hvc1" not in codec and "hev1" not in codec and "hevc" not in codec:
                continue          # WAV mislabels and other cases are fixed elsewhere
            if not src.is_file() or str(src) in seen:
                continue
            seen.add(str(src))
            jobs.append({"src": src, "dst": out_path_for(src), "mode": "hevc"})
    if not args.hevc_only:
        for e in report.get("needs_faststart", []):
            src = Path(e["path"] if isinstance(e, dict) else e)
            if not src.is_file() or str(src) in seen:
                continue
            seen.add(str(src))
            jobs.append({"src": src, "dst": out_path_for(src), "mode": "faststart"})
    if args.limit:
        jobs = jobs[:args.limit]

    if not jobs:
        print("Nothing to convert.")
        return

    total_in = sum(j["src"].stat().st_size for j in jobs)
    hevc_n = sum(1 for j in jobs if j["mode"] == "hevc")
    fs_n = len(jobs) - hevc_n
    free = shutil.disk_usage(HOME).free
    # H.264 at CRF 21 typically lands near the HEVC source size; budget 1.5x.
    budget = int(total_in * 1.5)

    vaapi = None if args.software else detect_vaapi(ffmpeg)

    print(f"  files to process : {len(jobs)}  ({hevc_n} HEVC re-encode, {fs_n} faststart remux)")
    print(f"  input size       : {total_in/1e9:.2f} GB")
    print(f"  estimated output : ~{budget/1e9:.2f} GB")
    print(f"  free space       : {free/1e9:.2f} GB")
    print(f"  encoder          : {'VAAPI hardware (' + vaapi + ')' if vaapi else 'libx264 software, preset ' + args.preset}")
    print(f"  parallel jobs    : {args.jobs}")
    print(f"  output tree      : {OUT_ROOT}")
    print("  originals        : never modified\n")

    if free < budget:
        sys.exit(f"ERROR: only {free/1e9:.2f} GB free, need roughly {budget/1e9:.2f} GB. Aborting.")
    if args.dry_run:
        for j in jobs[:15]:
            print(f"  [{j['mode']:9}] {j['src'].name[:76]}")
        if len(jobs) > 15:
            print(f"  … and {len(jobs)-15} more")
        print("\nDry run — nothing was changed.")
        return

    signal.signal(signal.SIGINT, on_sigint)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    results, done, t0 = [], 0, time.time()

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(convert_one, j, ffmpeg, ffprobe, args, vaapi): j for j in jobs}
        for fut in as_completed(futures):
            if stop:
                for f in futures:
                    f.cancel()
            try:
                res = fut.result()
            except Exception as e:                      # noqa: BLE001
                j = futures[fut]
                res = {"src": str(j["src"]), "status": "error", "error": str(e)}
            results.append(res)
            done += 1
            el = time.time() - t0
            eta = (el / done) * (len(jobs) - done) if done else 0
            mark = {"converted": "ok", "skipped": "--", "failed": "FAIL",
                    "verify_failed": "BAD", "timeout": "T/O"}.get(res["status"], "??")
            print(f"  [{done:>4}/{len(jobs)}] {mark:>4}  "
                  f"eta {int(eta//60)}m{int(eta%60):02d}s  "
                  f"{Path(res['src']).name[:60]}", flush=True)

    ok = [r for r in results if r["status"] == "converted"]
    sk = [r for r in results if r["status"] == "skipped"]
    bad = [r for r in results if r["status"] not in ("converted", "skipped")]

    manifest = {}
    if MANIFEST.exists():
        try:
            manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
    for r in ok + sk:
        manifest[r["src"]] = r["dst"]
    tmp = MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(MANIFEST)

    inb = sum(r.get("in_bytes", 0) for r in ok)
    outb = sum(r.get("out_bytes", 0) for r in ok)
    print(f"\n  converted {len(ok)}, skipped {len(sk)}, failed {len(bad)}"
          f"  in {int((time.time()-t0)//60)}m{int((time.time()-t0)%60):02d}s")
    if ok:
        print(f"  {inb/1e9:.2f} GB in -> {outb/1e9:.2f} GB out")
    print(f"  manifest: {MANIFEST}")
    if bad:
        print("\n  failures:")
        for r in bad[:20]:
            print(f"    [{r['status']}] {Path(r['src']).name[:64]}"
                  f"{'  ' + r['error'][:70] if r.get('error') else ''}")
        if len(bad) > 20:
            print(f"    … and {len(bad)-20} more")
    print("\n  Next: medialib --reindex")


if __name__ == "__main__":
    main()
