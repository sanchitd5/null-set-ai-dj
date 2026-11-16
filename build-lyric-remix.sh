#!/usr/bin/env bash
# Build lyric remix plans on this machine (the Mac) from the local library. The builder is the private dj-atlas one; this
# script only reaches it through app/music_brain/atlas_bridge.py (git submodule update --init app/music_brain/knowledge).
#
#   ./build-lyric-remix.sh --dry-run                      counts only (songs, candidates, plans already published)
#   ./build-lyric-remix.sh --max-calls 200                judge up to 200 batches with the local model
#   ./build-lyric-remix.sh --backend claudecode --queue pair --max-calls 100
#   ./build-lyric-remix.sh --backend local --queue within  (run the two backends on different queues at once)
#
#   --backend local|claudecode   local: the Qwen3-Omni server ./start.sh runs on :$OMNI_PORT (8901), or the one at
#                                OMNI_SERVER_URL; claudecode: the `claude` CLI logged in on this Mac (cheap haiku class)
#   --queue within|pair|both     within: one song's own lines; pair: lines of two songs; both (default)
#   --max-calls N  --dry-run  --model M  --fallback-local  --only id,id  --cache-dir D  --out F   (see --help below)
#
# Resumable: every judged candidate is remembered under <cache>/lyric_remix/state.json, plans already published are never
# judged again, and an interrupted run continues where it stopped.
# Results: accepted plans go to app/music_brain/knowledge/lyric_remix.json.gz. Getting them to the server:
# docs/engineering/MAC_TOOLS.md ("Publish the Mac's plans").
set -uo pipefail
cd "$(dirname "$0")"

case " $* " in *" -h "*|*" --help "*) HELP=1 ;; *) HELP=0 ;; esac
# honour .env like start.sh (variables already set win)
if [[ -f .env ]]; then
  while IFS='=' read -r k v; do
    [[ "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -n "${!k+x}" ]] && continue
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    export "$k=$v"
  done < <(grep -v '^[[:space:]]*#' .env)
fi
if [[ -n "${PYTHON:-}" ]]; then PY="$PYTHON"; else
  PY=""
  for c in .venv/bin/python venv/bin/python python3 python3.11 python3.10 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "import app" >/dev/null 2>&1; then PY="$c"; break; fi
  done
  [[ -n "$PY" ]] || { echo "no python found (run ./start.sh once to create .venv, or set PYTHON=...)" >&2; exit 1; }
fi

BACKEND=local; DRY=0
prev=""
for arg in "$@"; do
  case "$prev" in --backend) BACKEND="$arg" ;; esac
  case "$arg" in --dry-run) DRY=1 ;; --backend=*) BACKEND="${arg#--backend=}" ;; esac
  prev="$arg"
done

if (( ! HELP && ! DRY )) && [[ "$BACKEND" == "local" ]]; then
  OMNI_PORT="${OMNI_PORT:-8901}"
  OMNI_MODEL="${OMNI_MODEL:-mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit}"
  REMOTE="${OMNI_SERVER_URL:-}"; REMOTE="${REMOTE%/}"; REMOTE="${REMOTE%/v1}"
  [[ -n "$REMOTE" ]] || REMOTE="http://127.0.0.1:$OMNI_PORT"
  if ! curl -s --noproxy '*' -m 5 -o /dev/null "$REMOTE/v1/models"; then
    echo "no model server answers at $REMOTE/v1/models." >&2
    echo "Start it with ./start.sh (it runs the Qwen3-Omni server), or set OMNI_SERVER_URL, or use --backend claudecode." >&2
    exit 1
  fi
  # the same wiring start.sh gives the app: one omni server for the picks and the judge
  export MLX_URL="$REMOTE" MLX_MODEL="$OMNI_MODEL" OMNI_BASE_URL="$REMOTE/v1" OMNI_MODEL
elif (( ! HELP && ! DRY )) && [[ "$BACKEND" == "claudecode" ]]; then
  command -v claude >/dev/null 2>&1 || { echo "the claude CLI is not on PATH (install Claude Code and log in), or use --backend local" >&2; exit 1; }
fi
export PYTHONUNBUFFERED=1
exec "$PY" -m app.music_brain.atlas_bridge lyric-remix "$@"
