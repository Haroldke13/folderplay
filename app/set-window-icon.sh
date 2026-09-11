#!/usr/bin/env bash
# Replace a window's _NET_WM_ICON with crisp multi-size icon data.
#
# Chrome --app windows derive their window icon from the page favicon and
# downsample it to 16x16, which looks blurry in the taskbar. Setting it once
# is not enough: Chrome applies the favicon icon asynchronously AFTER the
# window maps, clobbering whatever we set first. So we re-apply for a while
# and stop once the value sticks.
#
# Usage: set-window-icon.sh <wm_class> <icon-data-file>
set -u
MATCH="${1:?wm_class}"; DATA="${2:?icon data file}"
command -v xprop >/dev/null 2>&1 || exit 0   # best-effort; never fatal
[ -r "$DATA" ] || exit 0
PAYLOAD="$(cat "$DATA")"

win_ids() {
  xprop -root _NET_CLIENT_LIST 2>/dev/null | tr ',' '\n' | grep -oE '0x[0-9a-f]+'
}
matches() {
  xprop -id "$1" WM_CLASS 2>/dev/null | grep -q "\"$MATCH\""
}
icon_is_ours() {
  # our data starts with 48x48; Chrome's favicon icon is 16x16
  xprop -id "$1" _NET_WM_ICON 2>/dev/null | grep -qE 'Icon \(48 x 48\)'
}

deadline=$(( $(date +%s) + 25 ))
stable=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  found=0; allours=1
  for w in $(win_ids); do
    if matches "$w"; then
      found=1
      icon_is_ours "$w" || allours=0
      xprop -id "$w" -f _NET_WM_ICON 32c -set _NET_WM_ICON "$PAYLOAD" 2>/dev/null
    fi
  done
  if [ "$found" = 1 ] && [ "$allours" = 1 ]; then
    stable=$((stable+1))
    # seen correct on three consecutive passes -> Chrome has stopped fighting
    [ "$stable" -ge 3 ] && exit 0
  else
    stable=0
  fi
  sleep 1
done
exit 0
