#!/usr/bin/env python3
"""Local media server for the medialib player.

Serves the player UI and streams indexed mp3/mp4 files with HTTP Range
support (required for seeking in video). Binds to loopback only and
requires a per-launch token so other pages in the browser cannot read
the library.

Also keeps named playlists and favourites in playlists.json and writes
.m3u8 exports. Playlist entries are keyed by absolute path (ids are
reassigned by every reindex) and re-resolved to the live ids on read.
"""

import argparse
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
INDEX_PATH = APP_DIR / "library.json"
PLAYLISTS_PATH = APP_DIR / "playlists.json"
EXPORT_DIR = Path.home() / "Music" / "medialib-exports"

# Reserved playlist name holding the favourites; never shown as a normal list.
FAV_KEY = "__favourites__"
MAX_POST = 4 * 1024 * 1024
MAX_NAME = 80

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
CHUNK = 256 * 1024

mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("video/mp4", ".mp4")


class Library:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.raw = b"{}"
        self.by_id = {}
        self.records = {}
        self.by_path = {}
        self.stems = {}
        self.load()

    def load(self):
        if not self.path.exists():
            raise SystemExit(
                f"No index at {self.path}\nRun: python3 {APP_DIR}/index_media.py"
            )
        data = json.loads(self.path.read_text(encoding="utf-8"))
        with self.lock:
            self.raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.by_id = {f["id"]: f["path"] for f in data["files"]}
            self.records = {f["id"]: f for f in data["files"]}
            # ids churn on every reindex; path is the stable key
            self.by_path = {f["path"]: f["id"] for f in data["files"]}
            # a converted copy streams from "path" but the user's original
            # "source_path" names the same track, so either resolves
            for f in data["files"]:
                src = f.get("source_path")
                if src and src not in self.by_path:
                    self.by_path[src] = f["id"]
            self.stems = {f["id"]: (f.get("stem") or f["name"]) for f in data["files"]}

    def record(self, fid: int):
        with self.lock:
            return self.records.get(fid)

    def resolve(self, fid: int):
        with self.lock:
            return self.by_id.get(fid)

    def id_for(self, path: str):
        with self.lock:
            return self.by_path.get(path)

    def stem_for(self, fid: int) -> str:
        with self.lock:
            return self.stems.get(fid, "")


class Playlists:
    """Named playlists + favourites, persisted as JSON beside the index.

    Every entry stores {"id": int, "path": str}. The id is only a cache:
    index_media.py renumbers everything on each run, so the absolute path
    is the durable key and ids are re-resolved from the live library on
    load and on every read. Writes go through a tmp file + replace.
    """

    def __init__(self, path: Path, library: Library):
        self.path = path
        self.library = library
        self.lock = threading.Lock()
        self.lists = {}
        self.load()

    # -- persistence ---------------------------------------------------
    def load(self):
        raw = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        stored = raw.get("playlists")
        if not isinstance(stored, dict):
            stored = {}
        now = int(time.time())
        lists = {}
        for name, pl in stored.items():
            if not isinstance(pl, dict):
                continue
            name = str(name)[:MAX_NAME]
            lists[name] = {
                "items": self._clean(pl.get("items")),
                "created": int(pl.get("created") or now),
                "updated": int(pl.get("updated") or now),
            }
        lists.setdefault(FAV_KEY, {"items": [], "created": now, "updated": now})
        with self.lock:
            self.lists = lists

    def _clean(self, items):
        """Keep only well-formed entries and refresh their ids by path."""
        out, seen = [], set()
        for it in items or []:
            if isinstance(it, dict):
                path = it.get("path")
            elif isinstance(it, str):
                path = it
            else:
                path = None
            if not isinstance(path, str) or not path or path in seen:
                continue
            seen.add(path)
            fid = self.library.id_for(path)
            out.append({"id": fid if fid is not None else -1, "path": path})
        return out

    def _save(self):
        """Caller holds the lock."""
        data = {
            "version": 1,
            "updated": int(time.time()),
            "note": "entries are keyed by path; id is a cache refreshed on load",
            "playlists": self.lists,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # -- reads ---------------------------------------------------------
    def _expand(self, items):
        out = []
        for it in items:
            fid = self.library.id_for(it["path"])
            out.append({
                "id": fid if fid is not None else -1,
                "path": it["path"],
                "name": os.path.basename(it["path"]),
                "missing": fid is None,
            })
        return out

    def snapshot(self):
        with self.lock:
            fav = self._expand(self.lists[FAV_KEY]["items"])
            lists = [
                {
                    "name": name,
                    "created": pl["created"],
                    "updated": pl["updated"],
                    "items": self._expand(pl["items"]),
                }
                for name, pl in self.lists.items()
                if name != FAV_KEY
            ]
        lists.sort(key=lambda p: p["name"].lower())
        return {
            "reserved": FAV_KEY,
            "favourites": fav,
            "playlists": lists,
            "exportDir": str(EXPORT_DIR),
        }

    # -- writes --------------------------------------------------------
    def _name(self, raw, existing_ok=False):
        name = (raw or "").strip()
        if not name or len(name) > MAX_NAME or name == FAV_KEY:
            raise ValueError("bad playlist name")
        if any(ord(c) < 32 for c in name):
            raise ValueError("bad playlist name")
        return name

    def entries(self, refs):
        """Turn ids / paths / {id,path} objects into stored entries."""
        out, seen = [], set()
        for r in refs or []:
            path = None
            if isinstance(r, bool):
                continue
            if isinstance(r, int):
                path = self.library.resolve(r)
            elif isinstance(r, str):
                path = r
            elif isinstance(r, dict):
                path = r.get("path")
                if not path and isinstance(r.get("id"), int):
                    path = self.library.resolve(r["id"])
            if not isinstance(path, str) or not path or path in seen:
                continue
            seen.add(path)
            fid = self.library.id_for(path)
            out.append({"id": fid if fid is not None else -1, "path": path})
        return out

    def create(self, name):
        name = self._name(name)
        now = int(time.time())
        with self.lock:
            if name in self.lists:
                raise ValueError("a playlist with that name already exists")
            self.lists[name] = {"items": [], "created": now, "updated": now}
            self._save()
        return name

    def rename(self, name, to):
        name, to = self._name(name), self._name(to)
        with self.lock:
            if name not in self.lists:
                raise ValueError("no such playlist")
            if to != name and to in self.lists:
                raise ValueError("a playlist with that name already exists")
            pl = self.lists.pop(name)
            pl["updated"] = int(time.time())
            self.lists[to] = pl
            self._save()
        return to

    def delete(self, name):
        name = self._name(name)
        with self.lock:
            if self.lists.pop(name, None) is None:
                raise ValueError("no such playlist")
            self._save()
        return name

    def set_items(self, name, refs):
        """Replace the whole ordered list — covers reorder and remove."""
        name = self._name(name)
        items = self.entries(refs)
        with self.lock:
            if name not in self.lists:
                raise ValueError("no such playlist")
            self.lists[name]["items"] = items
            self.lists[name]["updated"] = int(time.time())
            self._save()
        return name

    def add(self, name, refs):
        """Append, skipping anything already in the list."""
        name = self._name(name)
        add = self.entries(refs)
        with self.lock:
            if name not in self.lists:
                raise ValueError("no such playlist")
            have = {i["path"] for i in self.lists[name]["items"]}
            new = [i for i in add if i["path"] not in have]
            self.lists[name]["items"].extend(new)
            self.lists[name]["updated"] = int(time.time())
            self._save()
        return len(new)

    def favourite(self, ref, on):
        items = self.entries([ref])
        if not items:
            raise ValueError("unknown file")
        path = items[0]["path"]
        with self.lock:
            fav = self.lists[FAV_KEY]
            kept = [i for i in fav["items"] if i["path"] != path]
            if on:
                kept.append(items[0])
            fav["items"] = kept
            fav["updated"] = int(time.time())
            self._save()
        return on


def slugify(title: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (title or "").strip()).strip("-.")
    return (slug or "playlist")[:60]


def write_m3u(title, entries, durations, library):
    """Write a standard .m3u8 (UTF-8, absolute paths) and return its path."""
    lines = ["#EXTM3U", f"#PLAYLIST:{title}"]
    written = 0
    for e in entries:
        path = e["path"]
        if not path:
            continue
        fid = library.id_for(path)
        if fid is not None:
            # name the file the library actually streams (a converted copy
            # rather than the original the playlist was saved against)
            path = library.resolve(fid) or path
        label = library.stem_for(fid) if fid is not None else os.path.splitext(
            os.path.basename(path))[0]
        secs = durations.get(str(e.get("id")), durations.get(path))
        try:
            secs = int(float(secs))
        except (TypeError, ValueError):
            secs = -1
        lines.append(f"#EXTINF:{secs},{label}")
        lines.append(path)
        written += 1
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = EXPORT_DIR / (slugify(title) + ".m3u8")
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(out)
    return out, written


class Handler(BaseHTTPRequestHandler):
    server_version = "medialib/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers -------------------------------------------------------
    def _deny(self, code=403, msg="forbidden"):
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _deny_host(self):
        return self._deny(421, "bad host")

    def _host_ok(self) -> bool:
        """Reject requests whose Host is not loopback.

        Without this a malicious site can re-point its own hostname at
        127.0.0.1 (DNS rebinding) and reach this server from the browser.
        The token still gates access; this is defence in depth.
        """
        h = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return h in ("127.0.0.1", "localhost", "::1", "")

    def _authed(self, qs) -> bool:
        tok = (qs.get("t") or [None])[0]
        # compare_digest raises TypeError on non-ASCII str, which would kill the
        # request thread before auth is even decided. Compare as bytes.
        if tok:
            try:
                if secrets.compare_digest(tok.encode("utf-8", "surrogatepass"),
                                          self.server.token.encode("utf-8")):
                    return True
            except (TypeError, UnicodeError):
                pass
        ref = self.headers.get("Referer", "")
        return self.server.token in ref

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad Content-Length"})
        # A negative length passes a naive "> cap" test and then
        # rfile.read(-1) drains the socket to EOF, defeating the cap.
        if n < 0 or n > MAX_BODY:
            return self._json(413, {"error": "bad or oversized body"})
        if n > MAX_POST:
            raise ValueError("body too large")
        payload = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("body must be an object")
        return payload

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- routes --------------------------------------------------------
    def do_GET(self):
        self.route(head_only=False)

    def do_HEAD(self):
        self.route(head_only=True)

    def route(self, head_only):
        if not self._host_ok():
            return self._deny_host()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)

        if path == "/":
            if not self._authed(qs):
                return self._deny(403, "missing or bad token")
            return self.serve_static("index.html", head_only)

        if path == "/api/library":
            if not self._authed(qs):
                return self._deny(403, "missing or bad token")
            return self.serve_bytes(
                self.server.library.raw, "application/json; charset=utf-8", head_only
            )

        if path == "/api/external":
            if not self._authed(qs):
                return self._deny(403, "missing or bad token")
            return self.api_external(head_only)

        if path == "/api/playlists":
            if not self._authed(qs):
                return self._deny(403, "missing or bad token")
            body = json.dumps(
                self.server.playlists.snapshot(), ensure_ascii=False
            ).encode("utf-8")
            return self.serve_bytes(body, "application/json; charset=utf-8", head_only)

        if path.startswith("/media/"):
            if not self._authed(qs):
                return self._deny(403, "missing or bad token")
            return self.serve_media(path[len("/media/"):], head_only)

        if path.startswith("/static/"):
            return self.serve_static(path[len("/static/"):], head_only)

        return self._deny(404, "not found")

    def do_POST(self):
        if not self._host_ok():
            return self._deny_host()
        _p = urlparse(self.path)
        if unquote(_p.path) == "/api/external":
            if not self._authed(parse_qs(_p.query)):
                return self._deny(403, "missing or bad token")
            return self.api_external()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)
        if not self._authed(qs):
            return self._deny(403, "missing or bad token")
        if path == "/api/playlists":
            return self.api_playlists()
        if path == "/api/export":
            return self.api_export()
        return self._deny(404, "not found")

    def api_playlists(self):
        pls = self.server.playlists
        try:
            p = self._body()
            action = p.get("action")
            if action == "create":
                pls.create(p.get("name"))
            elif action == "rename":
                pls.rename(p.get("name"), p.get("to"))
            elif action == "delete":
                pls.delete(p.get("name"))
            elif action == "set":
                pls.set_items(p.get("name"), p.get("items"))
            elif action == "add":
                pls.add(p.get("name"), p.get("items"))
            elif action == "favourite":
                ref = p.get("item")
                if ref is None:
                    ref = p.get("id")
                pls.favourite(ref, bool(p.get("on", True)))
            else:
                raise ValueError("unknown action")
        except (ValueError, KeyError, TypeError) as e:
            return self._json({"error": str(e)}, 400)
        except OSError as e:
            return self._json({"error": f"could not save: {e}"}, 500)
        out = pls.snapshot()
        out["ok"] = True
        return self._json(out)

    def api_export(self):
        pls = self.server.playlists
        try:
            p = self._body()
            title = (p.get("title") or "playlist").strip()[:MAX_NAME] or "playlist"
            name = p.get("name")
            if name:
                snap = pls.snapshot()
                if name == snap["reserved"]:
                    entries = snap["favourites"]
                else:
                    found = [x for x in snap["playlists"] if x["name"] == name]
                    if not found:
                        raise ValueError("no such playlist")
                    entries = found[0]["items"]
            else:
                entries = pls.entries(p.get("items"))
            durations = p.get("durations")
            if not isinstance(durations, dict):
                durations = {}
            if not entries:
                raise ValueError("nothing to export")
            out, n = write_m3u(title, entries, durations, self.server.library)
        except (ValueError, KeyError, TypeError) as e:
            return self._json({"error": str(e)}, 400)
        except OSError as e:
            return self._json({"error": f"could not write export: {e}"}, 500)
        return self._json({"ok": True, "path": str(out), "tracks": n})

    def serve_bytes(self, body: bytes, ctype: str, head_only: bool):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass


    def api_external(self, head_only=False):
        """Launch a native player (VLC/mpv) on a track from the index.

        This plays the ORIGINAL file, not the converted copy: VLC and mpv
        decode HEVC natively, so a track works here whether or not the
        background conversion has reached it yet.
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._deny(400, "bad Content-Length")
        if n < 0 or n > 1 << 16:
            return self._deny(413, "bad body")
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
            fid = int(payload.get("id"))
        except (ValueError, TypeError, json.JSONDecodeError):
            return self._deny(400, "bad request")

        rec = self.server.library.record(fid)
        if not rec:
            return self._deny(404, "unknown id")
        # prefer the original: native players handle every codec
        target = rec.get("source_path") or rec.get("path")
        if not Path(target).is_file():
            target = rec.get("path")
        if not target or not Path(target).is_file():
            return self._deny(410, "file no longer on disk")

        want = (payload.get("player") or "vlc").lower()
        exe = shutil.which(want) or shutil.which("vlc") or shutil.which("mpv")
        if not exe:
            return self._json_out(503, {"error": "no native player installed"})
        try:
            subprocess.Popen([exe, target],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, start_new_session=True,
                             env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")})
        except OSError as e:
            return self._json_out(500, {"error": str(e)})
        return self._json_out(200, {"ok": True, "player": Path(exe).name,
                                    "file": Path(target).name})

    def _json_out(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def serve_static(self, rel: str, head_only: bool):
        # confine to STATIC_DIR
        target = (STATIC_DIR / rel).resolve()
        # startswith() on the raw string also matches sibling directories
        # whose name merely begins with "static". Compare path components.
        if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
            return self._deny(404, "not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        return self.serve_bytes(target.read_bytes(), ctype, head_only)

    def serve_media(self, ident: str, head_only: bool):
        try:
            fid = int(ident)
        except ValueError:
            return self._deny(400, "bad id")

        path = self.server.library.resolve(fid)
        if not path:
            return self._deny(404, "unknown id")
        p = Path(path)
        if not p.is_file():
            return self._deny(410, "file no longer on disk")

        size = p.stat().st_size
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range")

        start, end = 0, size - 1
        status = 200
        if rng:
            m = RANGE_RE.match(rng.strip())
            if m:
                s, e = m.group(1), m.group(2)
                if s:
                    start = int(s)
                    if e:
                        end = min(int(e), size - 1)
                else:
                    # suffix range: last N bytes
                    if e:
                        start = max(0, size - int(e))
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if head_only:
            return
        try:
            with p.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    buf = fh.read(min(CHUNK, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass  # user seeked or closed the tab


def pick_port(preferred=0):
    s = socket.socket()
    s.bind(("127.0.0.1", preferred))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    ap.add_argument("--token", default=None)
    ap.add_argument("--print-url", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    library = Library(INDEX_PATH)
    playlists = Playlists(PLAYLISTS_PATH, library)
    port = args.port or pick_port()
    token = args.token or secrets.token_urlsafe(18)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    httpd.library = library
    httpd.playlists = playlists
    httpd.token = token
    httpd.verbose = args.verbose

    url = f"http://127.0.0.1:{port}/?t={token}"
    if args.print_url:
        print(url, flush=True)
    else:
        print(f"medialib serving {library.by_id.__len__()} files at {url}", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
