"""Live ear: an audio-native model listens to the master bus during a HOLD LOOP.

The browser (static/live-ear.js) runs a DSP watchdog continuously: it measures
the loop seam (click, phase shift), the loop length against the analysed
downbeats, clipping and low-end clash, and how long the loop has been going.
Once per 8-bar phrase while the hold loop runs (or sooner when the watchdog
flags a problem) it posts those numbers plus the last few seconds of master
audio (16 kHz mono WAV) to /api/live/ear.

This module asks Qwen-Omni (audio in, text out) whether the loop sounds off
and which of a small set of safe moves to make. The model only proposes:
validate() keeps an action only when the loop state allows it, and when the
model is not configured, busy, slow or wrong, rule_decision() answers from
the watchdog numbers alone, so a hold loop never waits on the model.

The watchdog is the detector, the model is the second opinion. Measured
2026-09-27 (Qwen3-Omni-30B-A3B 4-bit, mlx-vlm 0.7.3): ~0.8 s per call, but it
called a loop cut 1.37 bars from mid-beat "seamless" and heard a "stutter" in
a clean clip. So a model "keep" never overrides a seam flag (validate()); the
model's own value is the musical call on a clean loop (fatigue, wash, move).

Grounding (./DJ/): moves stay on the 8-bar phrase grid ([[Phrasing &
Structure]], [[Loops & Beat Jumps]]); a filter wash only REMOVES low end, so
two decks never share sub-bass ([[EQ & Frequency Management]]).

Env:
  OMNI_BASE_URL   OpenAI-compatible endpoint. Default: local mlx-vlm server at
                  http://127.0.0.1:8901/v1, run from its own venv (mlx-vlm needs
                  starlette>=1.0, the app's fastapi pins <0.28):
                    ~/.venvs/mlx-vlm/bin/python -m mlx_vlm.server \
                      --model mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit --port 8901
                  start.sh starts it when that venv exists. Cloud (optional):
                  https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1
  OMNI_MODEL      default mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit
                  (local) or qwen3.8-omni-flash (cloud).
  OMNI_API_KEY    falls back to DASHSCOPE_API_KEY, then "none" (local).
  OMNI_TIMEOUT_S  per call, default 10. A decision later than a phrase is useless.
"""
from __future__ import annotations

import base64
import collections
import os
import statistics
import threading
import time
from typing import Optional

LOCAL_URL = "http://127.0.0.1:8901/v1"
LOCAL_MODEL = "mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit"
CLOUD_MODEL = "qwen3.8-omni-flash"
MAX_AUDIO_BYTES = 1_500_000        # ~45 s of 16 kHz mono 16-bit: far above one clip
MAX_REASON_CHARS = 200

# No "shrink": the hold loop must sound like the song carrying on (up to 32
# bars, dj-mind.js holdLoopSpan); a loop getting shorter sounds stuck.
ACTIONS = ("keep", "move_loop", "filter_wash", "restore")

# Watchdog thresholds. Same numbers as live-ear.js flags(); the browser sends
# the flags, but the rules re-derive them so a stale client cannot skip them.
SEAM_SHIFT_MS = 20.0       # kicks after the wrap land this far off the grid before it
GRID_ERR_MS = 35.0         # loop length vs analysed downbeats; those are ~23 ms frame-quantised
SEAM_CLICK_RATIO = 4.0     # level jump at the wrap vs the loop's median jump
FATIGUE_S = 60.0           # a hold loop older than this starts to bore the floor

# Latencies (s) of recent successful model calls: the wait adapts to what the model
# really takes (median 1.6 s, yet a hang cost the full 10 s and then the rules answered).
# Only the wait changes; the rules fallback and every decision stay as they were.
_lat: "collections.deque[float]" = collections.deque(maxlen=20)
ADAPT_MIN_SAMPLES = 5
ADAPT_FACTOR = 4.0
ADAPT_FLOOR_S = 4.0


def adaptive_timeout(base: float) -> float:
    """The per-call wait: FACTOR x the median of recent good calls, between FLOOR and `base`."""
    if len(_lat) < ADAPT_MIN_SAMPLES:
        return base
    return max(min(base, ADAPT_FLOOR_S), min(base, ADAPT_FACTOR * statistics.median(_lat)))


_busy = threading.Lock()   # one live call at a time; a second is refused, not queued


def config() -> dict:
    key = os.environ.get("OMNI_API_KEY") or os.environ.get("DASHSCOPE_API_KEY") or ""
    base = os.environ.get("OMNI_BASE_URL") or LOCAL_URL   # local first; cloud only when asked for
    cloud = "dashscope" in base or "aliyuncs" in base
    model = os.environ.get("OMNI_MODEL") or (CLOUD_MODEL if cloud else LOCAL_MODEL)
    try:
        timeout = float(os.environ.get("OMNI_TIMEOUT_S", "10"))
    except ValueError:
        timeout = 10.0
    return {"base_url": base, "model": model, "api_key": key or "none", "cloud": cloud,
            "timeout": max(2.0, min(timeout, 30.0)),
            # cloud needs a key; a local server is tried and falls back on error
            "configured": bool(key) or not cloud}


def status() -> dict:
    c = config()
    return {"configured": c["configured"], "model": c["model"], "base_url": c["base_url"],
            "busy": _busy.locked()}


# --------------------------------------------------------------- validation
def check_wav(data: bytes) -> None:
    """Raise ValueError unless `data` is a small RIFF/WAVE file."""
    if len(data) > MAX_AUDIO_BYTES:
        raise ValueError(f"audio clip too large ({len(data)} bytes, max {MAX_AUDIO_BYTES})")
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("audio clip must be a WAV file")


def flags(m: dict) -> list[str]:
    """Problems the watchdog numbers show, most serious first."""
    out = []
    if (m.get("seam_shift_ms") or 0) > SEAM_SHIFT_MS:
        out.append("seam_phase")
    if (m.get("grid_err_ms") or 0) > GRID_ERR_MS:
        out.append("loop_length")
    if (m.get("seam_click_ratio") or 0) > SEAM_CLICK_RATIO:
        out.append("seam_click")
    if (m.get("peak_dbfs") is not None and m["peak_dbfs"] > -0.3) or (m.get("clip_events") or 0) > 0:
        out.append("clipping")
    if m.get("low_clash"):
        out.append("low_clash")
    if (m.get("secs_looping") or 0) > FATIGUE_S:
        out.append("fatigue")
    return out


def allowed(m: dict) -> list[str]:
    """Actions the current loop state permits."""
    acts = ["keep"]
    if m.get("can_move"):
        acts.append("move_loop")
    if not m.get("washed"):
        acts.append("filter_wash")
    elif not flags(m):
        acts.append("restore")   # never bring the EQ back over a flagged problem (it undid the wash)
    return acts


def rule_decision(m: dict) -> dict:
    """Deterministic answer from the watchdog numbers alone."""
    f = flags(m)
    ok = allowed(m)
    seam = [x for x in f if x in ("seam_phase", "loop_length", "seam_click")]
    if seam:
        # Bad seam: re-anchor one phrase earlier (a new seam); if that was
        # tried, a wash masks the seam until the next song is ready.
        act = "move_loop" if "move_loop" in ok and not m.get("moved") else (
            "filter_wash" if "filter_wash" in ok else "keep")
        why = f"{seam[0].replace('_', ' ')} at the loop seam"
    elif "fatigue" in f:
        # New material first (the loop window shifts a phrase), then a wash.
        act = "move_loop" if "move_loop" in ok and not m.get("moved") else (
            "filter_wash" if "filter_wash" in ok else "keep")
        why = f"loop running {int(m.get('secs_looping') or 0)} s: the floor hears the repeat"
    else:
        act, why = "keep", "loop sits clean on the grid"
    return {"verdict": "off" if f else "clean", "issues": f, "action": act,
            "confidence": 1.0, "reason": why, "source": "RULE"}


def validate(raw: dict, m: dict) -> dict:
    """Keep the model's move only when it is a known action the loop allows."""
    rules = rule_decision(m)
    act = str(raw.get("action", "")).strip().lower()
    verdict = str(raw.get("verdict", "")).strip().lower()
    if verdict not in ("clean", "off"):
        verdict = rules["verdict"]
    issues = [str(x)[:40] for x in (raw.get("issues") or []) if isinstance(x, (str, int, float))][:6]
    reason = " ".join(str(raw.get("reason") or "").split())[:MAX_REASON_CHARS]
    try:
        conf = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    if act not in ACTIONS or act not in allowed(m):
        out = dict(rules)
        out["dropped"] = f"model proposed {act or 'nothing'!r}: not allowed now"
        return out
    # A hard seam problem the model called clean is still a problem.
    hard = [x for x in rules["issues"] if x in ("seam_phase", "loop_length", "seam_click")]
    if act == "keep" and hard:
        out = dict(rules)
        out["dropped"] = f"model kept the loop over {hard[0]}"
        return out
    return {"verdict": verdict, "issues": issues or rules["issues"], "action": act,
            "confidence": conf, "reason": reason or rules["reason"], "source": "AI"}


# ------------------------------------------------------------------ the call
SYSTEM = """You are the ears of a live DJ. You hear the last few seconds of the
master output while the playing song is held on a loop, waiting for the next
song. Decide whether the loop sounds off and pick ONE move.

Sounds off means: a click, thump or gap at the loop seam; kicks that stumble
or flam when the loop wraps; a loop cut mid-phrase (vocal or riser chopped);
clipping or distortion; two basslines fighting; or the repeat getting boring.

Moves:
  keep         the loop sounds fine, leave it
  move_loop    re-anchor the loop one 8-bar phrase earlier, next wrap
  filter_wash  pull lows and mids down over 4 bars to hide the repeat
  restore      bring the EQ back after a wash
Use only moves listed in ALLOWED. The watchdog numbers are measured exactly;
use your ears for what numbers cannot tell (is the repeat boring, does the
loop cut a vocal). The goal: the song should sound like it carries on.

Reply with JSON only:
{"verdict":"clean|off","issues":["short tags"],"action":"<move>","confidence":0.0-1.0,"reason":"<one sentence>"}"""


def _user_text(m: dict) -> str:
    keys = ("deck", "bpm", "rate", "loop_bars", "passes", "secs_looping", "section",
            "seam_shift_ms", "grid_err_ms", "seam_click_ratio", "peak_dbfs", "rms_dbfs",
            "clip_events", "low_clash", "washed", "moved")
    lines = [f"{k}: {m[k]}" for k in keys if m.get(k) is not None]
    return ("WATCHDOG\n" + "\n".join(lines) +
            f"\nFLAGS: {', '.join(flags(m)) or 'none'}" +
            f"\nALLOWED: {', '.join(allowed(m))}" +
            ("\nYour last reply was not usable. Reply with ONLY the JSON object, one short "
             "sentence for reason, no prose, no code fences." if m.get("_strict") else ""))


def _ask_omni(c: dict, wav: bytes, m: dict) -> str:
    import openai

    b64 = base64.b64encode(wav).decode()
    # DashScope wants a data URI; OpenAI-style servers (vLLM) take bare base64.
    data = f"data:audio/wav;base64,{b64}" if c["cloud"] else b64
    client = openai.OpenAI(base_url=c["base_url"], api_key=c["api_key"], timeout=c["timeout"],
                           max_retries=0)
    extra = {"modalities": ["text"]} if c["cloud"] else {}
    # Streamed: DashScope only serves Qwen-Omni as a stream; vLLM streams too.
    stream = client.chat.completions.create(
        model=c["model"],
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": data, "format": "wav"}},
                {"type": "text", "text": _user_text(m)},
            ]},
        ],
        temperature=0.2,
        max_tokens=200,
        stream=True,
        extra_body=extra or None,
    )
    parts = []
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
            parts.append(chunk.choices[0].delta.content)
    return "".join(parts)


def decide(wav: Optional[bytes], m: dict) -> dict:
    """Omni decision when possible, rule decision otherwise. Never raises for
    model trouble: the reason lands in `fallback`."""
    from app.ui.services.autopilot_service import _extract_json, failure_reason, note_retry

    c = config()
    if wav is None or not c["configured"]:
        out = rule_decision(m)
        out["fallback"] = "no audio clip" if wav is None else "Qwen-Omni not configured"
        return out
    if not _busy.acquire(blocking=False):
        out = rule_decision(m)
        out["fallback"] = "Qwen-Omni busy with the previous phrase"
        return out
    from app.ui.services import engine, llm_gate, session_log

    shared = shares_text_model(c)
    # One model for everything (start.sh --single-omni): take the gate's LIVE slot
    # (never queued behind a suggest) and hold new text work off mid-loop.
    (llm_gate.gate.note_live if shared else llm_gate.gate.note_ear)()
    t0 = time.monotonic()
    waited = None
    cc = dict(c, timeout=adaptive_timeout(c.get("timeout", 10.0)))
    quality, retried = None, None

    def hear() -> dict:
        """Ask once; on an unusable reply ask ONE more time (stricter), if the wait allows."""
        nonlocal quality, retried
        text = engine.current().ai.ear(cc, wav, m)
        try:
            quality = "ok"
            return _extract_json(text)
        except ValueError:
            quality = failure_reason(text)
            if time.monotonic() - t0 > cc["timeout"] / 2:
                raise
            retried = quality
            note_retry("ear", quality)
            text = engine.current().ai.ear(cc, wav, dict(m, _strict=True))
            try:
                return _extract_json(text)
            except ValueError:
                note_retry("ear", failure_reason(text) + "_gave_up", 2)
                raise

    try:
        if shared:
            with llm_gate.gate.slot(llm_gate.LIVE, wait_timeout=llm_gate.LIVE_WAIT_S) as waited:
                raw = hear()
        else:
            raw = hear()
        res = validate(raw, m)
        res["model"] = c["model"]
        _lat.append(time.monotonic() - t0 - (waited or 0.0))
    except Exception as exc:  # network, auth, bad JSON: rules answer instead
        res = rule_decision(m)
        res["fallback"] = f"Qwen-Omni error: {type(exc).__name__}: {str(exc)[:160]}"
        quality = quality if quality not in (None, "ok") else type(exc).__name__
        print(f"WARNING [ear] {res['fallback']} (rules answered after {time.monotonic() - t0:.1f}s, "
              f"wait was {cc['timeout']:.1f}s)", flush=True)
    finally:
        _busy.release()
    res["latency_seconds"] = round(time.monotonic() - t0, 2)
    session_log.log("ear", latency=res["latency_seconds"], source="rules" if res.get("fallback") else "model", action=res.get("action"),
                    error=res.get("fallback"), precheck=bool(m.get("precheck")),
                    waited=round(waited, 2) if waited is not None else None,
                    timeout=round(cc["timeout"], 2), quality=quality, retried=retried)
    return res


def _server(url: str) -> tuple:
    from urllib.parse import urlparse

    p = urlparse(url or "")
    host = (p.hostname or "").lower()
    return ("127.0.0.1" if host in ("localhost", "::1") else host, p.port)


def shares_text_model(c: dict) -> bool:
    """True when the live ear and the text LLM (OLLAMA_BASE_URL, set by model_runtime)
    are the same local server: then they contend and the ear goes through the gate."""
    if c.get("cloud"):
        return False
    return _server(c.get("base_url", "")) == _server(os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"))
