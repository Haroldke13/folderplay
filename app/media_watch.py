#!/usr/bin/env python3
"""Watch for new media and keep the library index current.

Three triggers, all in one daemon:
  * downloads   - a browser or downloader finishing a file
  * transfers   - files copied/moved in, including onto a mounted USB stick
  * a periodic sweep - a safety net for anything the watches missed

Uses inotify through ctypes, so there is nothing to install: no pyinotify,
no inotify-tools, no watchdog package. Standard library only.
"""

import ctypes
import ctypes.util
import errno
import os
import select
import struct
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
APP_DIR = HOME / ".local/share/medialib"
STATE = APP_DIR / "watch.state"
LOG = APP_DIR / "watch.log"

MEDIA_EXT = {".mp3", ".mp4", ".m4a", ".mkv", ".mov", ".avi", ".webm", ".flac",
             ".wav", ".ogg", ".opus", ".aac", ".m4v", ".wmv", ".3gp"}
# Partial-download suffixes: ignore until the downloader renames them.
PARTIAL = (".crdownload", ".part", ".partial", ".download", ".tmp", ".!qb", ".opdownload")

WATCH_DIRS = [HOME / "Downloads", HOME / "Videos", HOME / "Music",
              HOME / "Desktop", Path("/media"), Path("/mnt"),
              Path(f"/media/{os.environ.get('USER', '')}")]

IN_CREATE, IN_MOVED_TO, IN_CLOSE_WRITE = 0x100, 0x80, 0x8
IN_DELETE, IN_MOVED_FROM, IN_ISDIR = 0x200, 0x40, 0x40000000
WATCH_MASK = IN_CREATE | IN_MOVED_TO | IN_CLOSE_WRITE | IN_DELETE | IN_MOVED_FROM

SETTLE_SECONDS = 25        # wait for a transfer to finish before reindexing
SWEEP_SECONDS = 3600       # hourly safety net
MAX_WATCHES = 4096


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n"
    try:
        with LOG.open("a") as f:
            f.write(line)
    except OSError:
        pass
    print(line, end="", file=sys.stderr, flush=True)


class Inotify:
    def __init__(self):
        libc_name = ctypes.util.find_library("c") or "libc.so.6"
        self.libc = ctypes.CDLL(libc_name, use_errno=True)
        self.fd = self.libc.inotify_init1(0o4000)      # IN_NONBLOCK
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.paths = {}

    def add(self, path: Path):
        if len(self.paths) >= MAX_WATCHES or not path.is_dir():
            return None
        wd = self.libc.inotify_add_watch(self.fd, str(path).encode(), WATCH_MASK)
        if wd < 0:
            e = ctypes.get_errno()
            if e not in (errno.ENOENT, errno.EACCES, errno.ENOSPC):
                log(f"watch failed on {path}: {os.strerror(e)}")
            return None
        self.paths[wd] = path
        return wd

    def add_tree(self, root: Path, max_depth=4):
        """Watch a directory and its subdirectories, bounded so a huge tree
        cannot exhaust the inotify watch limit."""
        n = 0
        if self.add(root) is not None:
            n += 1
        root_depth = len(root.parts)
        for dirpath, dirnames, _ in os.walk(root, followlinks=False):
            d = Path(dirpath)
            if len(d.parts) - root_depth >= max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [x for x in dirnames if not x.startswith(".")]
            for sub in dirnames:
                if self.add(d / sub) is not None:
                    n += 1
        return n

    def read_events(self):
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            return
        i = 0
        while i + 16 <= len(data):
            wd, mask, _cookie, length = struct.unpack_from("iIII", data, i)
            i += 16
            raw = data[i:i + length].split(b"\x00", 1)[0]
            i += length
            name = raw.decode("utf-8", "replace")
            base = self.paths.get(wd)
            yield (base, name, mask)


def is_media(name: str) -> bool:
    low = name.lower()
    if low.endswith(PARTIAL):
        return False
    return os.path.splitext(low)[1] in MEDIA_EXT


def reindex(reason: str):
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, str(APP_DIR / "index_media.py"), "--quiet"],
                           capture_output=True, text=True, timeout=900)
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"reindex FAILED ({reason}): {e}")
        return
    if r.returncode != 0:
        log(f"reindex FAILED ({reason}): {(r.stderr or '').strip()[:200]}")
        return
    total = "?"
    try:
        import json
        total = json.loads((APP_DIR / "library.json").read_text())["total"]
    except Exception:
        pass
    log(f"reindexed ({reason}) - {total} files, {time.time()-t0:.1f}s")
    STATE.write_text(str(int(time.time())))


def main():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    ino = Inotify()
    total = 0
    for d in WATCH_DIRS:
        if d.is_dir():
            total += ino.add_tree(d)
    log(f"watching {total} directories across {sum(1 for d in WATCH_DIRS if d.is_dir())} roots")

    pending = None          # timestamp of the last interesting event
    last_sweep = time.time()
    poller = select.poll()
    poller.register(ino.fd, select.POLLIN)

    while True:
        events = poller.poll(5000)
        now = time.time()

        if events:
            for base, name, mask in ino.read_events():
                if base is None:
                    continue
                path = base / name
                if mask & IN_ISDIR:
                    # a new directory - e.g. a USB stick just mounted
                    if mask & (IN_CREATE | IN_MOVED_TO):
                        added = ino.add_tree(path)
                        if added:
                            log(f"new directory {path} (+{added} watches)")
                            pending = now
                    continue
                if is_media(name):
                    pending = now
                    log(f"saw {name[:70]}")

        # reindex once the flurry of events has gone quiet
        if pending and (now - pending) >= SETTLE_SECONDS:
            reindex("new files")
            pending = None
            last_sweep = now

        if now - last_sweep >= SWEEP_SECONDS:
            reindex("hourly sweep")
            last_sweep = now


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
