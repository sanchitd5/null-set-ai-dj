"""Is the app's model server up? Read-only: GET /v1/models, never start, stop or load anything.

A live (`--record`) run talks to the same model the live app does, through the same code
(`autopilot_service._chat_call`, which reads OLLAMA_BASE_URL / AUTOPILOT_MODEL at call time):

  1. OLLAMA_BASE_URL already in the environment (the app publishes it; export it to force a server)
  2. the local MLX text server (start.sh: MLX_PORT, default 8081) or, with `start.sh --single-omni`,
     the Omni server (OMNI_PORT, default 8901), found the way `set_ai.find_model` finds them
  3. Ollama (model_runtime.OLLAMA_URL)

`resolve()` returns the first that answers and publishes it exactly as `model_runtime._publish`
does. If none answers it raises `LLMDown`: the sim stops with that message. It never falls back to
the seeded stub (the stub is the offline / pytest world only) and never starts the server.
`model_runtime.start_background()` is not called anywhere in the sim (the API runs with lifespan off).
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Optional


class LLMDown(RuntimeError):
    """No model server answered. Start the app's model (./start.sh); the sim will not."""


@dataclass
class Endpoint:
    backend: str          # env | mlx | omni | ollama
    base_url: str
    model: str

    @property
    def label(self) -> str:
        """How the baseline names the model that wrote it: "omni-text", "mlx-text" ..."""
        return f"{self.backend}-text"

    def as_meta(self) -> dict:
        return {"backend": self.backend, "base_url": self.base_url, "model": self.model, "label": self.label}


def _models(base_url: str, opener, timeout: float = 3.0) -> list:
    with opener.open(f"{base_url.rstrip('/')}/models", timeout=timeout) as r:
        return json.loads(r.read() or b"{}").get("data") or []


def _pick(models: list, want: Optional[str]) -> Optional[str]:
    """The model id to ask: the one the app is configured for when the server lists it, else a
    loaded one, else the first (asking an unloaded model makes an MLX server load 16 GB)."""
    if not models:
        return None
    ids = [m.get("id") for m in models]
    if want and want in ids:
        return want
    return next((m["id"] for m in models if m.get("loaded")), ids[0])


def ear_up(opener=None) -> bool:
    """Is the live ear's audio model (OMNI_BASE_URL, default the local Omni server) answering? Optional: without
    it the hold loop uses the watchdog rules, in the live app and in the sim."""
    from app.ui.services import live_ear

    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        return bool(_models(live_ear.config()["base_url"], opener))
    except Exception:
        return False


def resolve(opener=None, env=None, publish: bool = True) -> Endpoint:
    env = os.environ if env is None else env
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))   # local servers: no proxy
    mlx = f"http://127.0.0.1:{env.get('MLX_PORT', '8081')}/v1"
    omni = f"http://127.0.0.1:{env.get('OMNI_PORT', '8901')}/v1"
    ollama = f"{env.get('OLLAMA_URL', 'http://localhost:11434').rstrip('/')}/v1"
    want = env.get("AUTOPILOT_MODEL") or None
    from app.ui.services import live_ear

    # the Omni server also lists the text model (unloaded): ask for the Omni model by name, never "the first"
    omni_model = env.get("OMNI_MODEL") or live_ear.LOCAL_MODEL
    candidates = []
    if env.get("OLLAMA_BASE_URL"):
        candidates.append(("env", env["OLLAMA_BASE_URL"], want))
    candidates += [("mlx", mlx, env.get("MLX_MODEL") or want), ("omni", omni, omni_model),
                   ("ollama", ollama, env.get("OLLAMA_MODEL") or want)]
    pin = (env.get("SIM_LLM") or "").lower()          # SIM_LLM=omni|mlx|ollama: a whole panel on one backend
    if pin:
        candidates = [c for c in candidates if c[0] == pin]
        if not candidates:
            raise LLMDown(f"SIM_LLM={pin!r} is not one of env, mlx, omni, ollama")
    tried = []
    for backend, base, model in candidates:
        try:
            got = _pick(_models(base, opener), model)
        except Exception as exc:
            tried.append(f"{base} ({type(exc).__name__})")
            continue
        if got:
            ep = Endpoint(backend, base, got)
            if publish:                       # what model_runtime._publish does; the client reads it per call
                os.environ["OLLAMA_BASE_URL"] = ep.base_url
                os.environ["AUTOPILOT_MODEL"] = ep.model
            return ep
        tried.append(f"{base} (no model loaded)")
    raise LLMDown("the app's model server is not reachable: " + ", ".join(tried) +
                  ". Start it with ./start.sh (the sim never starts or stops it), or replay a fixture with --replay.")
