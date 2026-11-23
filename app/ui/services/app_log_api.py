"""POST /api/app/log and GET /api/app/logs: a device pushes its own log when something went wrong.

The phone/TV app sends a rolling log (its own lines, boot breadcrumbs, its own logcat tail) after an
unfinished boot, an uncaught error, a native crash on the previous run, a black screen or a UI stall,
so the cause can be read on the server instead of needing adb on the device.

Auth: the same device-token login as every other app route (the auth middleware answers 401 first).
Body: one JSON object, gzip allowed (Content-Encoding: gzip). The cap applies to the inflated body
(server.py UPLOAD_LIMITS). Secrets: bearer tokens, device tokens, JWTs and ?token= style values are
replaced with [redacted] before anything is stored.

Storage: <cache>/app/device_logs/<YYYY-MM-DD>/<time>_<incident>.json, one file per incident. The
same incident id is stored once (a retry after a lost answer is answered ok, not stored again).
Retention: at most MAX_FILES incident files and MAX_TOTAL_BYTES in total; the oldest go first.
GET /api/app/logs lists the recent incidents (metadata only); ?id=<incident> returns one in full.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.ui.services.app_crash_api import scrub

log = logging.getLogger("app.log")
router = APIRouter()

MAX_BODY_BYTES = 768 * 1024           # inflated request body (server.py UPLOAD_LIMITS, same value)
MAX_LOG_CHARS = 600_000
MAX_FILES = 200
MAX_TOTAL_BYTES = 100 * 1024 * 1024
LOG_DIR: Optional[Path] = None        # tests; default <cache>/app/device_logs
_lock = threading.Lock()
_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


class DeviceLog(BaseModel):
    incident: str = Field(..., max_length=64)       # unique per incident, made by the app (idempotent retries)
    trigger: str = Field("", max_length=40)         # unfinished_boot | uncaught_error | native_crash | no_first_frame | ui_stall | renderer_error
    message: str = Field("", max_length=4000)
    version_name: str = Field("", max_length=64)
    version_code: Optional[int] = Field(None, ge=0, le=10_000_000)
    abi: str = Field("", max_length=40)
    device: str = Field("", max_length=120)
    android: str = Field("", max_length=40)
    form_factor: str = Field("", max_length=20)     # phone | tv | car
    renderer: str = Field("", max_length=40)        # impeller | opengl | ...
    at: str = Field("", max_length=40)              # phone clock, ISO 8601
    log: str = Field("", max_length=MAX_LOG_CHARS)


def _dir() -> Path:
    if LOG_DIR is not None:
        return LOG_DIR
    from app.ui import server

    return server.CACHE_DIR / "app" / "device_logs"


def _files(root: Path) -> list[Path]:
    return sorted(root.glob("*/*.json"), key=lambda p: (p.parent.name, p.name))


def _find(root: Path, incident: str) -> Optional[Path]:
    return next(iter(root.glob(f"*/*_{incident}.json")), None)


def _prune(root: Path) -> None:
    files = _files(root)
    sizes = {p: p.stat().st_size for p in files}
    total = sum(sizes.values())
    while files and (len(files) > MAX_FILES or total > MAX_TOTAL_BYTES):
        p = files.pop(0)
        total -= sizes[p]
        p.unlink(missing_ok=True)
        try:
            p.parent.rmdir()
        except OSError:
            pass


@router.post("/api/app/log")
def post_log(body: DeviceLog) -> dict[str, Any]:
    if not _ID.match(body.incident):
        raise HTTPException(422, "bad incident id")
    row = {k: scrub(v) if isinstance(v, str) else v for k, v in body.model_dump().items()}
    now = time.gmtime()
    row["received_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", now)
    root = _dir()
    with _lock:
        if _find(root, body.incident):
            return {"ok": True, "duplicate": True}
        d = root / time.strftime("%Y-%m-%d", now)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{time.strftime('%H%M%S', now)}_{body.incident}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        _prune(root)
    log.info("device log: trigger=%s build=%s device=%s incident=%s chars=%d", row["trigger"], row["version_code"], row["device"], body.incident, len(row["log"]))
    return {"ok": True}


@router.get("/api/app/logs")
def list_logs(limit: int = 50, id: str = "") -> Any:
    root = _dir()
    if id:
        p = _find(root, id) if _ID.match(id) else None
        if p is None:
            raise HTTPException(404, "no such incident")
        return json.loads(p.read_text(encoding="utf-8"))
    out = []
    for p in reversed(_files(root)[-max(1, min(limit, 200)):]):
        try:
            row = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({k: v for k, v in row.items() if k != "log"} | {"log_chars": len(row.get("log", ""))})
    return {"incidents": out}
