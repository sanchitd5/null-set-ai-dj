"""POST /api/app/crash: the phone's start-up crash report (included by server.py).

When the app cannot start (lib/boot on the phone: an init error, or launches that never got to a
first frame) it saves the error and sends it here from its rescue screen or the next healthy start,
so a crash like parity.16's is readable on the bench instead of "it does not open".

Auth: the SAME device-token login as every other phone route (the auth middleware answers 401 before
this code runs). An open route would let anyone on the internet fill the disk and the log; the phone
has the token in its keystore at boot (the rescue screen reads it before anything else starts), so the
safer choice costs nothing. A phone that is signed out has nothing to report that the owner would
not see on screen anyway.

Body: one JSON object, <= 64 KB (server.py UPLOAD_LIMITS caps the raw body; fields are capped again
here). Stored as one JSON line per report in <cache>/app/crash_reports.jsonl (rotated at 2 MB to
.1); one log line per report. Secrets: bearer tokens, device tokens and JWTs in any text field are
replaced with [redacted] before anything is stored or logged.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.ui.services.auth import TOKEN_PREFIX

log = logging.getLogger("app.crash")
router = APIRouter()

MAX_BODY_BYTES = 64 * 1024            # enforced by server.py UPLOAD_LIMITS (same constant)
MAX_LOG_BYTES = 2 * 1024 * 1024       # the file rotates to .1 past this
CRASH_LOG: Optional[Path] = None      # tests; default <cache>/app/crash_reports.jsonl
_lock = threading.Lock()

_SECRET = [
    (re.compile(r"(?i)(bearer\s+)[^\s'\",;]+"), r"\1[redacted]"),
    (re.compile(re.escape(TOKEN_PREFIX) + r"[A-Za-z0-9_-]{20,}"), "[redacted]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "[redacted]"),   # any JWT
    (re.compile(r"(?i)([?&](?:token|access_token|api_key|password|code|id_token)=)[^&\s'\"]+"), r"\1[redacted]"),
]


def scrub(text: str) -> str:
    for pat, rep in _SECRET:
        text = pat.sub(rep, text)
    return text


class CrashReport(BaseModel):
    kind: str = Field("", max_length=40)            # init_error | zone_error | flutter_error | unfinished_boot | ...
    message: str = Field("", max_length=4000)
    stack: str = Field("", max_length=20000)
    version_name: str = Field("", max_length=64)
    version_code: Optional[int] = Field(None, ge=0, le=10_000_000)
    device: str = Field("", max_length=120)
    at: str = Field("", max_length=40)              # when the phone caught it (ISO 8601, phone clock)
    unfinished_launches: Optional[int] = Field(None, ge=0, le=1000)


def _path() -> Path:
    if CRASH_LOG is not None:
        return CRASH_LOG
    from app.ui import server

    return server.CACHE_DIR / "app" / "crash_reports.jsonl"


def _store(line: str) -> None:
    p = _path()
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_file() and p.stat().st_size > MAX_LOG_BYTES:
            p.replace(p.with_name(p.name + ".1"))
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")


@router.post("/api/app/crash")
def post_crash(report: CrashReport) -> dict[str, Any]:
    row = {k: scrub(v) if isinstance(v, str) else v for k, v in report.model_dump().items()}
    row["received_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _store(json.dumps(row, ensure_ascii=False))
    first = row["message"].splitlines()[0][:160] if row["message"] else ""
    log.info("phone crash report: kind=%s build=%s device=%s msg=%s", row["kind"], row["version_code"], row["device"], first)
    return {"ok": True}
