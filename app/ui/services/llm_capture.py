"""Opt-in capture of every chat_raw call (prompt + reply) for the text-model training corpus.

Off by default. LLM_CAPTURE=1 appends one JSON line per successful call:
{kind, priority, system, user, temperature, max_tokens, reply, quality, ts, tag}
to LLM_CAPTURE_DIR (default <repo>/data/cache/llm_capture/, gitignored), one file per tag.
LLM_CAPTURE_TAG names the set the calls belong to (the harvester sets it per sim seed, so the
corpus can be split by set, never by row); default is the date.

Not under config.CACHE_DIR on purpose: the virtual set points AIDJ_CACHE_DIR at a temp dir it
deletes after the run. Off = one env lookup per call. Never raises into the caller.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_CTX = threading.local()


def note(kind: str, **ctx) -> None:
    """The caller's own inputs for its next calls of `kind` on this thread (a suggest's filter
    arguments, a plan's facts): the judge reruns the app's validators on a teacher's reply with
    them. Attached only to rows of the same kind, so it never leaks onto another call."""
    _CTX.kind, _CTX.ctx = kind, ctx


def enabled() -> bool:
    return os.environ.get("LLM_CAPTURE") == "1"


def capture_dir() -> Path:
    d = os.environ.get("LLM_CAPTURE_DIR")
    return Path(d).expanduser() if d else Path(__file__).resolve().parents[3] / "data" / "cache" / "llm_capture"


def record(*, kind, priority, system, user, temperature, max_tokens, reply, quality) -> None:
    """Append one call. Callers check enabled() first; this never raises."""
    try:
        tag = os.environ.get("LLM_CAPTURE_TAG") or time.strftime("%Y-%m-%d")
        row = {"kind": kind, "priority": priority, "system": system, "user": user,
               "temperature": temperature, "max_tokens": max_tokens, "reply": reply,
               "quality": quality, "ts": round(time.time(), 3), "tag": tag}
        if getattr(_CTX, "kind", None) == kind:
            row["ctx"] = _CTX.ctx
        d = capture_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / (re.sub(r"[^A-Za-z0-9_.-]", "_", tag)[:80] + ".jsonl")
        line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        with _LOCK, open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # capture must never break a model call
        pass
