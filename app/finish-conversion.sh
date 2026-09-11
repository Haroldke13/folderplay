#!/usr/bin/env bash
# Finish converting the remaining HEVC videos, fully detached.
# Survives terminal/session close. Re-run any time; it resumes.
#
#   start : ~/.local/share/medialib/finish-conversion.sh
#   watch : tail -f ~/.local/share/medialib/conversion.log
#   stop  : pkill -f convert_media.py
set -u
APP="$HOME/.local/share/medialib"
LOG="$APP/conversion.log"
LOCK="$APP/conversion.lock"

if [ -e "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "Already running (pid $(cat "$LOCK")). Watch: tail -f $LOG"; exit 0
fi

run() {
  echo "$$" > "$LOCK"
  trap 'rm -f "$LOCK"' EXIT
  {
    echo "=== started $(date) ==="
    # nice so the desktop stays responsive; GPU does the encoding anyway
    nice -n 15 python3 "$APP/convert_media.py" --jobs 2 --crf 28
    echo "=== rebuilding manifest from disk ==="
    python3 - <<'PY'
import json
from pathlib import Path
HOME = Path.home(); OUT = HOME/"Videos"/"medialib-converted"
mp = HOME/".local/share/medialib/converted_manifest.json"
man = json.loads(mp.read_text()) if mp.exists() else {}
for c in OUT.rglob("*.mp4"):
    o = HOME/c.relative_to(OUT)
    if o.is_file(): man[str(o)] = str(c)
t = mp.with_suffix(".json.tmp"); t.write_text(json.dumps(man, indent=2, ensure_ascii=False)); t.replace(mp)
print(f"manifest: {len(man)} entries")
PY
    echo "=== reindexing ==="
    python3 "$APP/index_media.py"
    echo "=== finished $(date) ==="
  } >> "$LOG" 2>&1
}

if [ "${1:-}" = "--foreground" ]; then run; else
  setsid nohup "$0" --foreground </dev/null >/dev/null 2>&1 &
  echo "Conversion running detached. Log: $LOG"
fi
