# Media Library

A folder-grouped music and video player for Linux desktops. Indexes every
`.mp3` and `.mp4` in your home directory and plays them in a native-feeling
app window.

Built for Lubuntu/LXQt, works on any Linux desktop.

![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## Why this exists

Most Linux media players want you to import a library, install a stack of
codecs, or hand your collection to a daemon. This one does neither. It scans
your disk, groups what it finds by the folders you already organised, and
plays it.

**It installs nothing.** No `pip install`, no `apt install`, no root. The
backend is the Python standard library; the frontend is one HTML file rendered
by the Chrome/Chromium you already have, which supplies the mp3/mp4 codecs.

## Features

- **Folder-grouped sidebar** — your existing directory structure *is* the
  library. Folders show per-type counts, so a folder of 500 clips is one row,
  not 500.
- **Audio and video in one app** — video plays inline; audio plays in a
  bottom player bar.
- **Search** across every filename, plus MP3-only / MP4-only filters.
- **Shuffle, repeat, seek, volume** — all persisted between launches.
- **Proper HTTP range streaming**, so seeking inside a large video is instant
  and the file is never loaded into memory whole.
- **Keyboard driven** — see the shortcut table below.

## Requirements

| Need | Why | Check |
|---|---|---|
| Python 3.8+ | Runs the local server | `python3 --version` |
| Chrome or Chromium | Renders the UI and decodes mp3/mp4 | `which google-chrome chromium` |

Nothing else. No pip packages, no system libraries, no root access.

## Install

```bash
git clone <your-repo-url> medialib
cd medialib
./install.sh
```

The installer verifies your Python version, finds a browser, copies the app to
`~/.local/share/medialib/`, puts the `medialib` command on your `PATH`,
generates a desktop entry with the correct path for your machine, adds a
desktop shortcut, and runs the first index.

It will tell you if `~/.local/bin` is missing from your `PATH` and exactly what
to add.

## Usage

| Command | Does |
|---|---|
| `medialib` | Launch the player window |
| `medialib --reindex` | Rescan `$HOME`, then launch |
| `medialib --scan-only` | Rescan and exit, no window |
| `medialib --serve` | Run the server only; open the printed URL yourself |
| `medialib --help` | Show this list |

You can also launch it from your application menu, or the desktop shortcut.
Right-clicking the desktop icon offers **Rescan media folders**.

### Keyboard shortcuts

| Key | Action |
|---|---|
| `Space` | Play / pause |
| `N` / `P` | Next / previous track |
| `←` / `→` | Seek 10 seconds |
| `↑` / `↓` | Volume |
| `S` | Shuffle |
| `R` | Repeat |
| `/` | Jump to search |
| `Esc` | Clear search |

## How it works

```
  medialib (bash)
      |
      |-- starts --> server.py            127.0.0.1, random port, random token
      |                 |                 - serves static/index.html
      |                 |                 - /api/library  -> library.json
      |                 +---------------> - /media/<id>   -> streams with Range
      |
      +-- opens ---> chrome --app=<url>   chromeless window, own profile
```

`index_media.py` walks `$HOME`, skipping the directories that never hold a real
media collection (`node_modules`, `.venv`, `site-packages`, `.cache`, browser
extension dirs, Trash). It writes `library.json`: every file with a stable
integer id, grouped into folders.

`server.py` serves that index and streams files **by id**, never by a path
supplied in the URL, so a crafted request cannot reach a file that is not in
the index.

### Security model

The server binds to **loopback only** and mints a fresh random token each
launch; every endpoint requires it. It is not reachable from your network.

> **Note:** the app window can play any file listed in the index, which covers
> much of your home directory. That is the point of the app, but it is worth
> understanding before you expose the port in any way. Don't change the bind
> address from `127.0.0.1`.

## Layout

```
.
├── app/
│   ├── index_media.py      # filesystem scanner -> library.json
│   ├── server.py           # stdlib HTTP server with Range support
│   ├── medialib            # launcher
│   └── static/index.html   # the whole UI, one file
├── desktop/
│   └── medialib.desktop.in # templated; install.sh fills in the real path
├── docs/
├── install.sh
├── uninstall.sh
└── LICENSE
```

## Uninstall

```bash
./uninstall.sh
```

Removes the app. **Your media files are never touched.**

## Troubleshooting

**A file shows "could not play this file".**
Chrome on Linux cannot decode H.265/HEVC video or AC-3 audio. Run
`python3 ~/.local/share/medialib/codec_check.py` to list which of your files
are affected, if that tool is present.

**Nothing appears in the library.**
Run `medialib --reindex` and read the count it prints. If it finds 0 files,
your media may live outside `$HOME` — pass `--root`:
`python3 ~/.local/share/medialib/index_media.py --root /media/usb-drive`

**The command isn't found.**
`~/.local/bin` isn't on your `PATH`. Add
`export PATH="$HOME/.local/bin:$PATH"` to `~/.bashrc` and open a new terminal.

**A server was left running.**
`pkill -f 'medialib/server.py'`

## License

MIT — see [LICENSE](LICENSE).
