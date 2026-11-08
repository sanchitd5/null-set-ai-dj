"""GET /api/config/live: the runtime-tunable dj-config entries (included by server.py).

Starts from the generated defaults (music_brain/generated/dj_config_live.json), applies the overrides
file (CACHE_DIR/config_overrides.json = data/cache/config_overrides.json, {"NAME": number}), and clamps
every value to that entry's min..max. An unknown name or a non-number is ignored; an unreadable or
malformed file is ignored whole and the defaults stand. Behind the same auth as every other route.
Clients (console config-live.js, phone) clamp again against their bundled bounds.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Response

from app.music_brain.config import CACHE_DIR
from app.music_brain.generated import dj_config as DJ

router = APIRouter()
log = logging.getLogger(__name__)

DEFAULTS_PATH = Path(DJ.__file__).with_name("dj_config_live.json")
OVERRIDES_PATH = CACHE_DIR / "config_overrides.json"


def _load_overrides(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        log.warning("config overrides %s ignored: %s", path, err)
        return {}
    if not isinstance(data, dict):
        log.warning("config overrides %s ignored: not an object", path)
        return {}
    return data


def live_config(overrides_path: Optional[Path] = None, defaults_path: Optional[Path] = None) -> dict:
    doc = json.loads((defaults_path or DEFAULTS_PATH).read_text())
    values = {n: dict(e) for n, e in doc["values"].items()}
    for name, v in _load_overrides(overrides_path or OVERRIDES_PATH).items():
        e = values.get(name)
        if e is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            log.warning("config override %r=%r ignored", name, v)
            continue
        e["value"] = min(max(v, e["min"]), e["max"])
    return {"config_hash": doc["config_hash"], "values": values}


@router.get("/api/config/live")
def get_live_config():
    return live_config()


STATIC_JS = Path(__file__).resolve().parents[1] / "static" / "config-live.js"


@router.get("/config-live.js")
def get_config_live_js():
    """The live values as a script, so the console gets them with the page (no XHR at load): the payload as
    window.DJ_CONFIG_SERVER, then static/config-live.js, which applies it. Any error: an empty object."""
    try:
        payload = json.dumps(live_config())
    except Exception:   # noqa: BLE001 - the page must load with the bundled values
        log.exception("config-live.js: live config unavailable")
        payload = "{}"
    try:
        apply_js = STATIC_JS.read_text()
    except OSError:
        apply_js = ""
    return Response(f"window.DJ_CONFIG_SERVER = {payload};\n{apply_js}", media_type="application/javascript",
                    headers={"Cache-Control": "no-store"})
