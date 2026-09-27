#!/usr/bin/env bash
# Start the AI DJ console: http://localhost:8000
#
#   ./start.sh            start (or restart) the server, wait until the LLM is warm, open the browser
#   ./start.sh --no-open  same, without opening a browser tab
#   PORT=8010 ./start.sh  use another port
#
# The server boots the LLM itself (app/ui/model_runtime.py): MLX
# (mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit) on Apple Silicon, Ollama as fallback.
# Settings live in .env (LLM_BACKEND, MLX_MODEL, MLX_PORT, OLLAMA_MODEL).
# Logs: /tmp/ai-dj-server.log (server), /tmp/ai-dj-mlx-server.log (MLX).
set -euo pipefail

cd "$(dirname "$0")"
PORT="${PORT:-8000}"
LOG="${LOG:-/tmp/ai-dj-server.log}"
OPEN=1
[[ "${1:-}" == "--no-open" ]] && OPEN=0

PY="${PYTHON:-python3}"
if ! "$PY" -m uvicorn --version >/dev/null 2>&1; then
  echo "uvicorn not found for $PY. Install the requirements first:  pip install -r requirements.txt" >&2
  exit 1
fi
command -v ffmpeg >/dev/null 2>&1 || echo "warning: ffmpeg not found - downloads will fail (brew install ffmpeg)" >&2
[[ -f .env ]] || echo "note: no .env found - using defaults (MLX gemma-3-27b on Apple Silicon, Ollama fallback)" >&2

# Stop a previous server on this port. The bracket keeps pkill from matching
# this script's own command line.
pkill -f "[u]vicorn app.ui.server" 2>/dev/null || true
# wait for the old server to finish shutting down before starting a new one
for _ in $(seq 1 20); do pgrep -f "[u]vicorn app.ui.server" >/dev/null || break; sleep 0.5; done
pkill -9 -f "[u]vicorn app.ui.server" 2>/dev/null || true
sleep 1
PIDS="$(lsof -ti ":$PORT" 2>/dev/null || true)"
if [[ -n "$PIDS" ]]; then
  echo "freeing port $PORT (pids: $PIDS)"
  kill $PIDS 2>/dev/null || true
  sleep 2
  PIDS="$(lsof -ti ":$PORT" 2>/dev/null || true)"
  [[ -n "$PIDS" ]] && kill -9 $PIDS 2>/dev/null || true
fi

# Live ear (app/ui/live_ear.py): Qwen3-Omni on mlx-vlm, in its own venv because
# mlx-vlm needs starlette>=1.0 and the app's fastapi pins <0.28. Optional: the
# hold loop falls back to the DSP rules when this server is not running.
OMNI_PY="${OMNI_PY:-$HOME/.venvs/mlx-vlm/bin/python}"
OMNI_PORT="${OMNI_PORT:-8901}"
OMNI_MODEL="${OMNI_MODEL:-mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit}"
if [[ -x "$OMNI_PY" ]] && ! curl -s --noproxy '*' -m 2 -o /dev/null "http://127.0.0.1:$OMNI_PORT/v1/models"; then
  echo "starting live ear ($OMNI_MODEL) on :$OMNI_PORT  (log: /tmp/ai-dj-omni-server.log)"
  (cd /tmp && nohup "$OMNI_PY" -m mlx_vlm.server --model "$OMNI_MODEL" --host 127.0.0.1 \
     --port "$OMNI_PORT" > /tmp/ai-dj-omni-server.log 2>&1 &)
fi

echo "starting server on http://localhost:$PORT  (log: $LOG)"
nohup "$PY" -m uvicorn app.ui.server:app --port "$PORT" > "$LOG" 2>&1 &
SERVER_PID=$!

# Wait for the HTTP server, then for the LLM warm-up (first MLX load can take
# a minute or two). Local requests bypass any HTTP(S)_PROXY in the shell.
for _ in $(seq 1 60); do
  if curl -s --noproxy '*' -o /dev/null -w '%{http_code}' "http://localhost:$PORT/api/recipes" 2>/dev/null | grep -q 200; then
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "server exited - last log lines:" >&2
    tail -20 "$LOG" >&2
    exit 1
  fi
  sleep 1
done
echo "server up; warming the LLM…"
STATUS=""
for _ in $(seq 1 180); do
  STATUS="$(curl -s --noproxy '*' "http://localhost:$PORT/api/llm/status" 2>/dev/null || true)"
  echo "$STATUS" | grep -q '"ready":true' && break
  sleep 2
done
if echo "$STATUS" | grep -q '"ready":true'; then
  echo "LLM ready: $(echo "$STATUS" | grep -o '"backend":"[^"]*"\|"model":"[^"]*"' | tr '\n' ' ')"
else
  echo "LLM not ready yet (the console still works; AI features wait for it): $STATUS" >&2
fi

echo "AI DJ console: http://localhost:$PORT   (server pid $SERVER_PID; stop with: kill $SERVER_PID)"
if [[ "$OPEN" == 1 ]] && command -v open >/dev/null 2>&1; then
  open "http://localhost:$PORT"
fi
