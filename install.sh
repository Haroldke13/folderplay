#!/usr/bin/env bash
# Media Library — installer
# Installs to the current user's home. No root, no system packages.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/medialib"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- preflight ----------
say "Checking requirements"

command -v python3 >/dev/null 2>&1 || die "python3 is required but not installed."
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,8) else 1)' \
  || die "python3 >= 3.8 required (found $PYV)."
echo "    python3 $PYV — ok (standard library only, nothing to pip install)"

BROWSER=""
for c in google-chrome google-chrome-stable chromium chromium-browser brave-browser microsoft-edge; do
  if command -v "$c" >/dev/null 2>&1; then BROWSER="$c"; break; fi
done
if [ -n "$BROWSER" ]; then
  echo "    browser: $BROWSER — ok"
else
  warn "No Chrome/Chromium found."
  warn "The app will still install; run 'medialib --serve' and open the printed"
  warn "URL in any browser that plays mp3/mp4. For the app-window experience:"
  warn "    sudo apt install chromium-browser"
fi

# ---------- install ----------
say "Installing to $APP_DIR"
mkdir -p "$APP_DIR/static" "$BIN_DIR" "$DESKTOP_DIR"
install -m 0644 "$SRC/app/index_media.py"     "$APP_DIR/index_media.py"
install -m 0644 "$SRC/app/server.py"          "$APP_DIR/server.py"
install -m 0644 "$SRC/app/static/index.html"  "$APP_DIR/static/index.html"
install -m 0755 "$SRC/app/medialib"           "$BIN_DIR/medialib"

# Desktop entry is generated so Exec= carries this machine's real path.
install -m 0755 "$SRC/app/set-window-icon.sh" "$APP_DIR/set-window-icon.sh"
install -m 0644 "$SRC/app/icon-wm.dat"        "$APP_DIR/icon-wm.dat"

say "Installing icons"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
if [ -d "$SRC/icons/hicolor" ]; then
  ( cd "$SRC/icons/hicolor" && find . -type f \( -name '*.png' -o -name '*.svg' \) -print0 \
    | while IFS= read -r -d '' f; do
        mkdir -p "$ICON_DIR/$(dirname "$f")"
        install -m 0644 "$f" "$ICON_DIR/$f"
      done )
  command -v gtk-update-icon-cache >/dev/null 2>&1 \
    && gtk-update-icon-cache -f -t "$ICON_DIR" 2>/dev/null || true
  echo "    app icon installed (SVG + 8 PNG sizes)"
else
  warn "icons/ directory missing — the app will fall back to a generic icon."
fi

say "Creating desktop entry"
sed -e "s|@EXEC@|$BIN_DIR/medialib|g" "$SRC/desktop/medialib.desktop.in" \
  > "$DESKTOP_DIR/medialib.desktop"
chmod 0644 "$DESKTOP_DIR/medialib.desktop"
command -v update-desktop-database >/dev/null 2>&1 \
  && update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

# Optional desktop shortcut
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESK" ]; then
  cp "$DESKTOP_DIR/medialib.desktop" "$DESK/Media Library.desktop"
  chmod +x "$DESK/Media Library.desktop"
  gio set "$DESK/Media Library.desktop" metadata::trusted true 2>/dev/null || true
  echo "    shortcut placed on $DESK"
fi

# ---------- first index ----------
say "Indexing your media (this scans \$HOME for .mp3 and .mp4)"
python3 "$APP_DIR/index_media.py" || warn "Indexing failed — run 'medialib --reindex' later."

# ---------- PATH check ----------
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH. Add this to ~/.bashrc:"
     warn "    export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

cat <<DONE

  Media Library installed.

    Launch:      medialib
    Or:          your app menu -> "Media Library"
    Re-scan:     medialib --reindex
    No browser:  medialib --serve   (then open the printed URL)
    Remove:      ./uninstall.sh

DONE
