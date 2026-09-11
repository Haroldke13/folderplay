#!/usr/bin/env python3
"""Scan $HOME for mp3/mp4 files and write a folder-grouped library index."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

MEDIA_EXTS = {".mp3": "audio", ".mp4": "video"}

# Directories that never contain a user's real media library.
SKIP_DIR_NAMES = {
    ".git", "node_modules", "__pycache__", ".npm", ".cache",
    "site-packages", ".venv", "venv", "env", ".tox", ".mypy_cache",
    "Extensions", "chrome_profile", ".vscode", ".vscode-server",
    "snap", ".mozilla", ".config", ".gradle", ".m2",
}

HOME = Path.home()
APP_DIR = HOME / ".local/share/medialib"
INDEX_PATH = APP_DIR / "library.json"
MANIFEST_PATH = APP_DIR / "converted_manifest.json"
CONVERTED_ROOT = HOME / "Videos" / "medialib-converted"


def load_manifest():
    """Map original path -> converted path, for conversions that finished."""
    if not MANIFEST_PATH.exists():
        return {}
    try:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    # Finding 6: a poisoned manifest could redirect a library entry at any
    # file on disk. Accept a target only if it is a real, non-symlink file
    # inside the converted tree.
    root = CONVERTED_ROOT.resolve()
    out = {}
    for k, v in raw.items():
        try:
            cand = Path(v)
            if cand.is_symlink() or not cand.is_file():
                continue
            real = cand.resolve(strict=True)
            if real == root or root in real.parents:
                out[k] = str(real)
        except OSError:
            continue
    return out


def should_skip(dirpath: Path, include_trash: bool) -> bool:
    # The converted tree is reached via the manifest, never scanned directly,
    # otherwise every converted video would be listed twice.
    if dirpath == CONVERTED_ROOT or CONVERTED_ROOT in dirpath.parents:
        return True
    parts = dirpath.parts
    if not include_trash and ".local/share/Trash" in dirpath.as_posix():
        return True
    for part in parts:
        if part in SKIP_DIR_NAMES:
            return True
    return False


def scan(root: Path, include_trash: bool, manifest=None):
    manifest = manifest or {}
    files = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        if should_skip(d, include_trash):
            dirnames[:] = []
            continue
        # prune early so os.walk doesn't descend
        dirnames[:] = [
            n for n in dirnames
            if n not in SKIP_DIR_NAMES and not should_skip(d / n, include_trash)
        ]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            kind = MEDIA_EXTS.get(ext)
            if not kind:
                continue
            full = d / name
            # Finding 6: os.walk(followlinks=False) still lists symlinked FILES.
            # A symlink named *.mp3 pointing at a private file would otherwise
            # become streamable through /media.
            if full.is_symlink():
                continue
            try:
                st = full.stat()
            except OSError:
                continue
            rec = {
                "path": str(full),          # what the server streams
                "source_path": str(full),   # where it really lives on disk
                "name": name,
                "stem": os.path.splitext(name)[0],
                "folder": str(d),           # grouped under the ORIGINAL folder
                "kind": kind,
                "ext": ext.lstrip("."),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "converted": False,
            }
            # If this file was converted for playability, stream the copy but
            # keep the original's name and folder so it appears where expected.
            conv = manifest.get(str(full))
            if conv:
                cp = Path(conv)
                try:
                    rec["path"] = str(cp)
                    rec["size"] = cp.stat().st_size
                    rec["converted"] = True
                except OSError:
                    pass
            files.append(rec)
    return files


def build_index(files):
    """Assign stable ids and group into folders."""
    files.sort(key=lambda f: (f["folder"].lower(), f["name"].lower()))
    for i, f in enumerate(files):
        f["id"] = i

    folders = {}
    for f in files:
        folders.setdefault(f["folder"], []).append(f["id"])

    folder_list = []
    for path, ids in folders.items():
        try:
            rel = str(Path(path).relative_to(HOME))
        except ValueError:
            rel = path
        kinds = {files[i]["kind"] for i in ids}
        folder_list.append({
            "path": path,
            "rel": rel,
            "label": os.path.basename(path) or path,
            "count": len(ids),
            "audio": sum(1 for i in ids if files[i]["kind"] == "audio"),
            "video": sum(1 for i in ids if files[i]["kind"] == "video"),
            "kinds": sorted(kinds),
            "bytes": sum(files[i]["size"] for i in ids),
            "ids": ids,
        })

    folder_list.sort(key=lambda x: (-x["count"], x["rel"].lower()))
    return {
        "generated": int(time.time()),
        "home": str(HOME),
        "total": len(files),
        "converted": sum(1 for f in files if f.get("converted")),
        "audio": sum(1 for f in files if f["kind"] == "audio"),
        "video": sum(1 for f in files if f["kind"] == "video"),
        "files": files,
        "folders": folder_list,
    }


def main():
    ap = argparse.ArgumentParser(description="Index mp3/mp4 files into a library.json")
    ap.add_argument("--root", default=str(HOME), help="directory to scan (default: $HOME)")
    ap.add_argument("--include-trash", action="store_true", help="also index files in Trash")
    ap.add_argument("--out", default=str(INDEX_PATH), help="output json path")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not args.quiet:
        print(f"Scanning {root} ...", file=sys.stderr)

    t0 = time.time()
    manifest = load_manifest()
    files = scan(root, args.include_trash, manifest)
    index = build_index(files)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    out.chmod(0o600)   # the index lists every file you own

    if not args.quiet:
        dt = time.time() - t0
        gb = sum(f["size"] for f in files) / 1e9
        if index["converted"]:
            print(f"  {index['converted']} files play from converted copies "
                  f"(originals kept in place)", file=sys.stderr)
        print(
            f"Indexed {index['total']} files "
            f"({index['audio']} mp3, {index['video']} mp4) "
            f"in {len(index['folders'])} folders, {gb:.1f} GB, {dt:.1f}s",
            file=sys.stderr,
        )
        print(f"Wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
