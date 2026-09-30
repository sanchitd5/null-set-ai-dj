#!/usr/bin/env bash
# Keep the library, stems and atlas at their best in one go:
#   stems -> flac -> labels -> review -> atlas -> export   (python3 -m app.music_brain.maintain)
#
#   ./maintain.sh                      every step
#   ./maintain.sh --dry-run            what each step would do, and the counts; writes nothing
#   ./maintain.sh --steps atlas,export only those steps (always in the order above)
#   ./maintain.sh --jobs 4 --max-minutes 90 --no-network
#
# Run it after adding songs, after learning a set, or weekly. Stop the app first (./start.sh,
# Ctrl+C): stems and flac refuse while the app answers on $PORT (default 8000); the other steps
# run anyway under their own locks. Safe to re-run after a crash: every step is idempotent.
# JSON report on stdout (also saved to data/cache/maintain/<timestamp>.json), progress on
# stderr, and both appended to data/cache/maintain/maintain.log.
set -uo pipefail
cd "$(dirname "$0")"

# .env fills in settings the environment doesn't already set (same rule as start.sh)
if [[ -f .env ]]; then
  while IFS='=' read -r k v; do
    [[ "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -n "${!k+x}" ]] && continue
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    export "$k=$v"
  done < <(grep -v '^[[:space:]]*#' .env)
fi

PY="${PYTHON:-python3}"
LOG_DIR="${AIDJ_CACHE_DIR:-data/cache}/maintain"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/maintain.log"
echo "== $(date '+%F %T') ./maintain.sh $*" >> "$LOG"
export PYTHONUNBUFFERED=1
"$PY" -m app.music_brain.maintain "$@" 2> >(tee -a "$LOG" >&2) | tee -a "$LOG"
exit "${PIPESTATUS[0]}"
