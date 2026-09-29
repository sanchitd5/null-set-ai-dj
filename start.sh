#!/usr/bin/env bash
# Start the AI DJ as ONE live process group in this terminal:
#
#   llm  - text model for autopilot picks   (mlx_lm.server,  MLX_MODEL on MLX_PORT)
#   ear  - live ear, audio model            (mlx_vlm.server, OMNI_MODEL on OMNI_PORT)
#   app  - the console + API                 (uvicorn app.ui.server on PORT)
#
# All three are children of this script, not detached background jobs: their
# logs stream here (prefixed [llm] / [ear] / [app], also kept in /tmp), Ctrl+C
# stops all three together, and if one crashes it is restarted while the other
# two keep running.
#
#   ./start.sh                start (replacing any earlier copies), open the browser
#   ./start.sh --no-open      same, without opening a browser tab
#   ./start.sh --dual         two models instead: a separate text model (mlx_lm.server)
#                             for the picks plus the ear's Qwen3-Omni
#   PORT=8010 ./start.sh      use another port (flags combine: --dual --no-open)
#
# DEFAULT is ONE model for everything (the old --single-omni, still accepted): the
# live ear's Qwen3-Omni also makes the autopilot's text decisions, so there is no
# separate text model (~17 GB less RAM) and ear + picks share one server. That server
# batches concurrent requests (mlx-vlm continuous batching), so a short ear call does
# not queue behind a long pick; OMNI_MAX_SEQS (default 6) caps concurrent sequences to
# bound KV-cache memory. Without the mlx-vlm venv the script falls back to --dual.
#
# Settings live in .env (LLM_BACKEND, MLX_MODEL, MLX_PORT, OLLAMA_MODEL,
# OMNI_MODEL, OMNI_PORT, OMNI_MAX_SEQS). The ear is optional: without its venv the
# hold loop uses the DSP rules. Without mlx (not Apple Silicon) the app falls back to Ollama.
set -uo pipefail

cd "$(dirname "$0")"
# .env fills in settings the environment doesn't already set: a variable given
# on the command line (LLM_BACKEND=ollama ./start.sh) always wins over .env.
if [[ -f .env ]]; then
  while IFS='=' read -r k v; do
    [[ "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -n "${!k+x}" ]] && continue
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    export "$k=$v"
  done < <(grep -v '^[[:space:]]*#' .env)
fi

PORT="${PORT:-8000}"
OPEN=1
SINGLE_OMNI=1           # default: one shared omni server; --dual for a separate text model
SINGLE_OMNI_ASKED=0     # --single-omni given explicitly: a missing omni venv is then an error
for arg in "$@"; do
  case "$arg" in
    --no-open) OPEN=0 ;;
    --single-omni) SINGLE_OMNI=1; SINGLE_OMNI_ASKED=1 ;;
    --dual) SINGLE_OMNI=0 ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (see ./start.sh --help)" >&2; exit 2 ;;
  esac
done
PY="${PYTHON:-python3}"
MLX_MODEL="${MLX_MODEL:-mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit}"
MLX_PORT="${MLX_PORT:-8081}"
OMNI_PY="${OMNI_PY:-$HOME/.venvs/mlx-vlm/bin/python}"
OMNI_MODEL="${OMNI_MODEL:-mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit}"
OMNI_PORT="${OMNI_PORT:-8901}"
OMNI_MAX_SEQS="${OMNI_MAX_SEQS:-6}"
# YouTube cookies (Netscape cookies.txt from a logged-in browser) for yt-dlp's bot
# checks: app.music_brain.yt_guard adds them only after a plain request is refused.
# Kept outside the repo; never commit it (it is your YouTube login).
YTDLP_COOKIES_FILE="${YTDLP_COOKIES_FILE:-$HOME/.config/ai-dj/youtube-cookies.txt}"
if [[ -f "$YTDLP_COOKIES_FILE" ]]; then
  if [[ "$(stat -f %Lp "$YTDLP_COOKIES_FILE" 2>/dev/null || stat -c %a "$YTDLP_COOKIES_FILE")" != "600" ]]; then
    chmod 600 "$YTDLP_COOKIES_FILE" 2>/dev/null   # owner-only: it is a login
  fi
  export YTDLP_COOKIES_FILE
  # Fresh cookies: the bot-check guard is off by default (YT_GUARD=on brings back the
  # cooldown circuit breaker and cookies-only-after-a-bot-check).
  export YT_GUARD="${YT_GUARD:-off}"
  if [[ "$YT_GUARD" == "off" ]]; then
    echo "yt-dlp: using cookies from $YTDLP_COOKIES_FILE on every request (yt guard off)"
  else
    echo "yt-dlp: using cookies from $YTDLP_COOKIES_FILE (only after a bot check)"
  fi
else
  unset YTDLP_COOKIES_FILE
  echo "yt-dlp: no cookies file at ~/.config/ai-dj/youtube-cookies.txt (bot checks back off and heal on their own)"
fi
# API request lines are debug: LOG_LEVEL=debug brings them back (with uvicorn's debug).
LOG_LEVEL="${LOG_LEVEL:-info}"
if [[ "$LOG_LEVEL" == "debug" ]]; then UVICORN_ARGS=(--log-level debug); else UVICORN_ARGS=(--log-level info --no-access-log); fi
LOG_APP="${LOG:-/tmp/ai-dj-server.log}"
LOG_LLM="${MLX_LOG:-/tmp/ai-dj-mlx-server.log}"
LOG_EAR="/tmp/ai-dj-omni-server.log"
MAX_RESTARTS=5          # per service, within RESTART_WINDOW_S; then that service stays down
RESTART_WINDOW_S=600

export PYTHONUNBUFFERED=1

if ! "$PY" -m uvicorn --version >/dev/null 2>&1; then
  echo "uvicorn not found for $PY. Install the requirements first:  pip install -r requirements.txt" >&2
  exit 1
fi
command -v ffmpeg >/dev/null 2>&1 || echo "warning: ffmpeg not found - downloads will fail (brew install ffmpeg)" >&2

say() { printf '\033[1m[start]\033[0m %s\n' "$*"; }

# ---- which services run ------------------------------------------------------
RUN_LLM=0
if [[ "${LLM_BACKEND:-auto}" != "ollama" && "$(uname -m)" == "arm64" ]] && "$PY" -c "import mlx_lm" 2>/dev/null; then
  RUN_LLM=1
fi
RUN_EAR=0
[[ -x "$OMNI_PY" ]] && RUN_EAR=1
if (( SINGLE_OMNI && ! RUN_EAR )); then
  if (( SINGLE_OMNI_ASKED )); then
    echo "--single-omni needs the mlx-vlm venv at $OMNI_PY (or set OMNI_PY)" >&2
    exit 1
  fi
  say "no mlx-vlm venv at $OMNI_PY: falling back to --dual (separate text model)"
  SINGLE_OMNI=0
fi
(( SINGLE_OMNI )) && RUN_LLM=0   # the omni server makes the decisions too

# ---- replace earlier copies (detached ones from older start.sh runs) ----------
free_port() {
  local port="$1" pids
  pids="$(lsof -ti "tcp:$port" -sTCP:LISTEN 2>/dev/null || true)"
  [[ -z "$pids" ]] && return
  say "stopping the old process on :$port (pid $pids)"
  kill $pids 2>/dev/null || true
  for _ in $(seq 1 20); do lsof -ti "tcp:$port" -sTCP:LISTEN >/dev/null 2>&1 || return; sleep 0.5; done
  kill -9 $(lsof -ti "tcp:$port" -sTCP:LISTEN 2>/dev/null) 2>/dev/null || true
}
free_port "$PORT"
# --single-omni: also stop an old text model, that RAM is the point of the flag
(( RUN_LLM || SINGLE_OMNI )) && free_port "$MLX_PORT"
(( RUN_EAR )) && free_port "$OMNI_PORT"

# ---- children ----------------------------------------------------------------
PID_LLM=""; PID_EAR=""; PID_APP=""
TAILS=()
STOPPING=0
declare -a HIST_LLM=() HIST_EAR=() HIST_APP=()

start_llm() {
  "$PY" -m mlx_lm.server --model "$MLX_MODEL" --host 127.0.0.1 --port "$MLX_PORT" >>"$LOG_LLM" 2>&1 &
  PID_LLM=$!
  say "llm  pid $PID_LLM  $MLX_MODEL on :$MLX_PORT"
}
start_ear() {
  # from /tmp: mlx-vlm lives in its own venv and must not import this repo's packages
  # --max-num-seqs bounds the continuous-batching batch (each sequence holds KV cache)
  (cd /tmp && exec "$OMNI_PY" -m mlx_vlm.server --model "$OMNI_MODEL" --host 127.0.0.1 --port "$OMNI_PORT" \
      --max-num-seqs "$OMNI_MAX_SEQS") >>"$LOG_EAR" 2>&1 &
  PID_EAR=$!
  say "ear  pid $PID_EAR  $OMNI_MODEL on :$OMNI_PORT (max $OMNI_MAX_SEQS concurrent sequences)"
}
start_app() {
  # The ear always follows this script's omni server. With --single-omni the
  # app's decision LLM points at that same server and model.
  if (( SINGLE_OMNI )); then
    MLX_SUPERVISED=1 MLX_PORT="$OMNI_PORT" MLX_MODEL="$OMNI_MODEL" \
      OMNI_BASE_URL="http://127.0.0.1:$OMNI_PORT/v1" OMNI_MODEL="$OMNI_MODEL" \
      "$PY" -m uvicorn app.ui.server:app --port "$PORT" "${UVICORN_ARGS[@]}" >>"$LOG_APP" 2>&1 &
  else
    MLX_SUPERVISED="$RUN_LLM" OMNI_BASE_URL="http://127.0.0.1:$OMNI_PORT/v1" OMNI_MODEL="$OMNI_MODEL" \
      "$PY" -m uvicorn app.ui.server:app --port "$PORT" "${UVICORN_ARGS[@]}" >>"$LOG_APP" 2>&1 &
  fi
  PID_APP=$!
  say "app  pid $PID_APP  http://localhost:$PORT"
}

# Live console: warnings and errors only (CONSOLE_LEVEL=info shows everything the
# services log; the full logs are always in /tmp). A traceback is shown whole.
CONSOLE_LEVEL="${CONSOLE_LEVEL:-warn}"
stream() {  # live, prefixed view of a service's log
  local name="$1" file="$2" colour="$3"
  : >>"$file"
  tail -n 0 -F "$file" 2>/dev/null | while IFS= read -r line; do
    line="${line//$'\r'/ }"
    if [[ "$CONSOLE_LEVEL" != "info" ]]; then
      if [[ "$line" =~ (WARN|ERROR|CRITICAL|Traceback|Exception|FATAL) ]]; then
        in_tb=0; [[ "$line" == *Traceback* ]] && in_tb=1
      elif (( ${in_tb:-0} )) && [[ "$line" =~ ^[[:space:]] || "$line" =~ ^[A-Za-z_.]+(Error|Exception) ]]; then
        :   # traceback body
      else
        in_tb=0; continue
      fi
    fi
    printf '\033[%sm[%s]\033[0m %s\n' "$colour" "$name" "$line"
  done &
  TAILS+=("$!")
}

cleanup() {
  (( STOPPING )) && return
  STOPPING=1
  echo
  say "stopping all three…"
  for p in $PID_APP $PID_EAR $PID_LLM; do kill "$p" 2>/dev/null || true; done
  for _ in $(seq 1 20); do
    alive=0; for p in $PID_APP $PID_EAR $PID_LLM; do kill -0 "$p" 2>/dev/null && alive=1; done
    (( alive )) || break; sleep 0.5
  done
  for p in $PID_APP $PID_EAR $PID_LLM; do kill -9 "$p" 2>/dev/null || true; done
  for t in "${TAILS[@]:-}"; do [[ -n "$t" ]] && kill "$t" 2>/dev/null || true; done
  pkill -P $$ 2>/dev/null || true
  say "stopped."
}
trap 'cleanup; exit 0' INT TERM
trap cleanup EXIT

stream app "$LOG_APP" "32"
(( RUN_LLM )) && stream llm "$LOG_LLM" "36"
(( RUN_EAR )) && stream ear "$LOG_EAR" "35"

if (( SINGLE_OMNI )); then
  say "single model: $OMNI_MODEL makes the decisions AND runs the live ear (no separate text model)"
else
  (( RUN_LLM )) && start_llm || say "llm: not started (not Apple Silicon / no mlx_lm / LLM_BACKEND=ollama) - the app falls back to Ollama"
fi
(( RUN_EAR )) && start_ear || say "ear: not started (no $OMNI_PY) - the hold loop uses the DSP rules"
start_app

# ---- wait until usable, then open the console --------------------------------
for _ in $(seq 1 60); do
  curl -s --noproxy '*' -o /dev/null -w '%{http_code}' "http://localhost:$PORT/api/recipes" 2>/dev/null | grep -q 200 && break
  kill -0 "$PID_APP" 2>/dev/null || break
  sleep 1
done
say "console up: http://localhost:$PORT   (Ctrl+C here stops everything together)"
if (( OPEN )) && command -v open >/dev/null 2>&1; then open "http://localhost:$PORT"; fi

# ---- supervise: a crashed service restarts, the others keep running ----------
may_restart() {  # $1 = name of the history array
  local now arr=() t; now=$(date +%s)
  eval 'for t in "${'"$1"'[@]:-}"; do [[ -n "$t" ]] && (( now - t < RESTART_WINDOW_S )) && arr+=("$t"); done'
  (( ${#arr[@]} >= MAX_RESTARTS )) && { eval "$1=(\"\${arr[@]}\")"; return 1; }
  arr+=("$now"); eval "$1=(\"\${arr[@]}\")"; return 0
}
check() {  # $1 name, $2 pid var, $3 start fn, $4 history array
  local name="$1" pidvar="$2" pid code
  pid="${!pidvar}"
  [[ -z "$pid" ]] && return
  kill -0 "$pid" 2>/dev/null && return
  wait "$pid" 2>/dev/null; code=$?
  (( STOPPING )) && return
  if may_restart "$4"; then
    say "$name exited (code $code) - restarting it; the other services keep running"
    sleep 2
    "$3"
  else
    say "$name exited (code $code) $MAX_RESTARTS times in ${RESTART_WINDOW_S}s - leaving it down (see its log)"
    printf -v "$pidvar" ''
  fi
}
while :; do
  (( RUN_LLM )) && check llm PID_LLM start_llm HIST_LLM
  (( RUN_EAR )) && check ear PID_EAR start_ear HIST_EAR
  check app PID_APP start_app HIST_APP
  sleep 2
done
