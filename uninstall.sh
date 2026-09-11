#!/usr/bin/env bash
# Media Library — uninstaller. Removes the app; never touches your media files.
set -euo pipefail

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/medialib"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"

echo "This removes the Media Library app from:"
echo "  $APP_DIR"
echo "  $BIN_DIR/medialib"
echo "  $DESKTOP_DIR/medialib.desktop"
echo
echo "Your music and video files are NOT touched."
read -rp "Proceed? [y/N] " a
[[ "$a" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }

rm -rf  "$APP_DIR"
rm -f   "$BIN_DIR/medialib"
rm -f   "$DESKTOP_DIR/medialib.desktop"
rm -f   "$DESK/Media Library.desktop"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
find "$ICON_DIR" -name 'medialib.png' -o -name 'medialib.svg' 2>/dev/null | while read -r i; do rm -f "$i"; done
command -v gtk-update-icon-cache >/dev/null 2>&1 \
  && gtk-update-icon-cache -f -t "$ICON_DIR" 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 \
  && update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
echo "Removed."
