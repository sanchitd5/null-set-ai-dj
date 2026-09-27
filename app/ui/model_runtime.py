"""LLM runtime for the AI DJ: MLX on Apple Silicon, Ollama as fallback.

One MLX model now serves BOTH the text-only autopilot suggestion calls and
live_ear's audio-in calls (mlx-community/Qwen3-Omni-30B-A3B-Captioner, ~18GB):
running Instruct (text) and Omni (audio) as two separate processes used to
eat ~36GB RAM. The Captioner is Omni-shaped, so it is served via
`mlx_vlm.server`, not `mlx_lm.server` (`mlx_lm.server` cannot load an Omni
checkpoint). It is served from its own venv when present
(~/.venvs/mlx-vlm/bin/python: mlx-vlm needs starlette>=1.0, the app's fastapi
pins <0.28), falling back to this process's interpreter otherwise.

At server startup (background thread, never blocks the app):
  1. MLX: start `mlx_vlm.server` for MLX_MODEL (default
     mlx-community/Qwen3-Omni-30B-A3B-Captioner) on MLX_PORT unless one is
     already answering, then send one tiny text-only warm-up chat (no
     audio/image field) so the weights are loaded and the first real
     suggestion does not pay the model-load cost.
  2. If MLX is unavailable (not Apple Silicon, mlx-vlm missing, model not
     downloaded, server fails to come up) fall back to Ollama: preload
     OLLAMA_MODEL with keep_alive so it stays resident between songs (Ollama
     re-pinned every 4 min: each chat call resets keep_alive to 5 min).

The chosen endpoint is published through the same env vars the LLM client
already reads at call time (OLLAMA_BASE_URL / AUTOPILOT_MODEL), so callers need
no change. Only publishes once the backend has actually answered. live_ear.py
reuses this same published base_url/model (see its OMNI_BASE_URL/OMNI_MODEL
env fallback) instead of running a second model.

Known tradeoff (accepted): the Captioner is fine-tuned for audio description,
not general structured-JSON instruction-following, so autopilot's strict-JSON
suggestion prompts may come back worse-formed than they did against the
Instruct model. That is expected, not a bug to silently work around here.

Env:
  LLM_BACKEND   auto (default) | mlx | ollama
  MLX_MODEL     mlx-community/Qwen3-Omni-30B-A3B-Captioner
  MLX_PORT      8901            (shared with live_ear.py's OMNI default port)
  MLX_VLM_VENV  ~/.venvs/mlx-vlm/bin/python if present, else this interpreter
  OLLAMA_MODEL  gemma3:27b      (fallback model)
  OLLAMA_URL    http://localhost:11434
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

MLX_MODEL = os.environ.get("MLX_MODEL", "mlx-community/Qwen3-Omni-30B-A3B-Captioner")
MLX_PORT = int(os.environ.get("MLX_PORT", "8901"))
MLX_URL = f"http://127.0.0.1:{MLX_PORT}"
_DEFAULT_VLM_VENV = Path.home() / ".venvs" / "mlx-vlm" / "bin" / "python"
MLX_VLM_PYTHON = os.environ.get("MLX_VLM_VENV") or (
    str(_DEFAULT_VLM_VENV) if _DEFAULT_VLM_VENV.exists() else sys.executable)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:27b")
OLLAMA_KEEP_ALIVE = "2h"
MLX_START_TIMEOUT_S = 600  # first load of a 16 GB model from disk
LOG_PATH = Path(os.environ.get("MLX_LOG", "/tmp/ai-dj-mlx-server.log"))

state = {"backend": None, "model": None, "base_url": None, "ready": False,
         "detail": "starting", "warm_seconds": None}
_proc: Optional[subprocess.Popen] = None
_lock = threading.Lock()


# Local servers only: never route through an HTTP(S)_PROXY from the environment.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url: str, payload: Optional[dict] = None, timeout: float = 5.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with _opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _publish(backend: str, base_url: str, model: str, warm: float) -> None:
    os.environ["OLLAMA_BASE_URL"] = base_url  # name kept: the client reads this
    os.environ["AUTOPILOT_MODEL"] = model
    state.update(backend=backend, base_url=base_url, model=model, ready=True,
                 detail="ready", warm_seconds=round(warm, 1))
    print(f"[model_runtime] {backend} ready: {model} @ {base_url} (warm {warm:.1f}s)", flush=True)


def _mlx_possible() -> Optional[str]:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return "not Apple Silicon"
    if MLX_VLM_PYTHON == sys.executable:
        # No dedicated venv: mlx_vlm must be importable in this interpreter.
        try:
            import mlx_vlm  # noqa: F401
        except ImportError:
            return "mlx-vlm not installed"
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(MLX_MODEL, local_files_only=True)
    except Exception:
        return f"{MLX_MODEL} not downloaded yet"
    return None


def _mlx_up() -> bool:
    try:
        _http_json(f"{MLX_URL}/v1/models", timeout=2)
        return True
    except Exception:
        return False


def _running_mlx_model() -> tuple[Optional[int], Optional[str]]:
    """(pid, --model) of the mlx_vlm.server already listening on MLX_PORT, if any.

    It outlives app restarts, so switching MLX_MODEL would otherwise keep
    serving the old model."""
    try:
        pids = subprocess.run(["lsof", "-ti", f":{MLX_PORT}", "-sTCP:LISTEN"],
                              capture_output=True, text=True, timeout=5).stdout.split()
        for pid in pids:
            args = subprocess.run(["ps", "-o", "args=", "-p", pid],
                                  capture_output=True, text=True, timeout=5).stdout.split()
            if "mlx_vlm.server" in " ".join(args) and "--model" in args:
                return int(pid), args[args.index("--model") + 1]
    except Exception:
        pass
    return None, None


def _stop_foreign_mlx() -> None:
    """Stop a running mlx_vlm.server that serves a different model than MLX_MODEL."""
    pid, model = _running_mlx_model()
    if pid and model and model != MLX_MODEL:
        print(f"[model_runtime] mlx server on :{MLX_PORT} serves {model}, want {MLX_MODEL}: restarting it", flush=True)
        try:
            os.kill(pid, 15)
            for _ in range(30):
                if not _mlx_up():
                    break
                time.sleep(0.5)
        except Exception:
            pass


def _warm_chat(base_url: str, model: str, timeout: float) -> float:
    t0 = time.time()
    _http_json(f"{base_url}/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 4,
        "temperature": 0,
    }, timeout=timeout)
    return time.time() - t0


def _start_mlx() -> bool:
    global _proc
    why = _mlx_possible()
    if why:
        state["detail"] = f"mlx unavailable: {why}"
        return False
    _stop_foreign_mlx()
    if not _mlx_up():
        log = open(LOG_PATH, "ab")
        _proc = subprocess.Popen(
            [MLX_VLM_PYTHON, "-m", "mlx_vlm.server", "--model", MLX_MODEL,
             "--host", "127.0.0.1", "--port", str(MLX_PORT)],
            stdout=log, stderr=subprocess.STDOUT,
            # its own session: the model server outlives app restarts on purpose
            # (a restart must not reload 18 GB). It used to be stopped at exit,
            # and a restart racing the old app's shutdown killed the model the
            # NEW app had just adopted (ECONNREFUSED on :8901).
            start_new_session=True,
        )
        deadline = time.time() + MLX_START_TIMEOUT_S
        state["detail"] = "starting mlx_vlm.server"
        while time.time() < deadline:
            if _proc.poll() is not None:
                state["detail"] = f"mlx_vlm.server exited ({_proc.returncode}); see {LOG_PATH}"
                return False
            if _mlx_up():
                break
            time.sleep(1)
        else:
            state["detail"] = "mlx_vlm.server did not come up in time"
            stop()
            return False
    state["detail"] = "loading mlx model"
    try:
        warm = _warm_chat(f"{MLX_URL}/v1", MLX_MODEL, timeout=MLX_START_TIMEOUT_S)
    except Exception as exc:
        state["detail"] = f"mlx warm-up failed: {exc}"
        return False
    _publish("mlx", f"{MLX_URL}/v1", MLX_MODEL, warm)
    _unload_ollama()  # don't hold a second 17 GB copy of the model in memory
    return True


def _unload_ollama() -> None:
    try:
        _http_json(f"{OLLAMA_URL}/api/generate", {"model": OLLAMA_MODEL, "keep_alive": 0}, timeout=30)
    except Exception:
        pass  # Ollama not running: nothing to free


def _start_ollama() -> bool:
    state["detail"] = "loading ollama model"
    t0 = time.time()
    try:
        # empty prompt = load only; keep_alive keeps it resident between songs
        _http_json(f"{OLLAMA_URL}/api/generate",
                   {"model": OLLAMA_MODEL, "keep_alive": OLLAMA_KEEP_ALIVE}, timeout=600)
    except Exception as exc:
        state["detail"] = f"ollama unavailable: {exc}"
        return False
    _publish("ollama", f"{OLLAMA_URL}/v1", OLLAMA_MODEL, time.time() - t0)
    return True


OLLAMA_REFRESH_S = 4 * 60


def _ollama_keepalive_loop() -> None:
    # Every OpenAI-compatible chat call resets the model's keep_alive to Ollama's
    # default (5 min), so re-pin it more often than that.
    while state.get("backend") == "ollama":
        time.sleep(OLLAMA_REFRESH_S)
        try:
            _http_json(f"{OLLAMA_URL}/api/generate",
                       {"model": OLLAMA_MODEL, "keep_alive": OLLAMA_KEEP_ALIVE}, timeout=600)
        except Exception:
            pass


def _boot() -> None:
    backend = os.environ.get("LLM_BACKEND", "auto").lower()
    if backend in ("auto", "mlx") and _start_mlx():
        return
    if backend in ("auto", "ollama") and _start_ollama():
        threading.Thread(target=_ollama_keepalive_loop, daemon=True).start()
        return
    print(f"[model_runtime] no LLM backend ready: {state['detail']}", flush=True)


def start_background() -> None:
    """Idempotent: boot the LLM backend in a daemon thread."""
    with _lock:
        if state.get("_started"):
            return
        state["_started"] = True
    threading.Thread(target=_boot, name="llm-boot", daemon=True).start()


def stop() -> None:
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _proc.kill()
    _proc = None


def status() -> dict:
    return {k: v for k, v in state.items() if not k.startswith("_")}
