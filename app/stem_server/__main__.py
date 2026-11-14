"""python -m app.stem_server  (env: STEM_SERVER_TOKEN required, STEM_SERVER_HOST, STEM_PORT).

Routes are relative (no self-URLs), so a reverse proxy may mount the server under a stripped prefix."""

import os
import sys

import uvicorn

from app.stem_server.server import create_app


def canary(argv: list) -> int:
    """python -m app.stem_server canary [N]: the stem cache canary, no token or running server needed."""
    import json
    from pathlib import Path

    from app.music_brain.audio import stem_service
    from app.music_brain.config import CACHE_DIR, DEMUCS_MODEL
    from app.stem_server import canary as cn

    cache = Path(os.environ.get("AIDJ_CACHE_DIR") or CACHE_DIR).expanduser().resolve()
    models = list(dict.fromkeys([os.environ.get("STEM_SERVER_MODEL", DEMUCS_MODEL), stem_service.DEMUCS_MODEL,
                                 stem_service.FAST_MODEL]))
    res = cn.run(cache, models, int(argv[0]) if argv else 5)
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


def main() -> int:
    if sys.argv[1:2] == ["canary"]:
        return canary(sys.argv[2:])
    try:
        app = create_app()
    except RuntimeError as exc:          # no / short token: refuse to start
        print(f"[stem-server] {exc}", file=sys.stderr)
        return 2
    uvicorn.run(app, host=os.environ.get("STEM_SERVER_HOST", "127.0.0.1"),
                port=int(os.environ.get("STEM_PORT", "47830")), log_level="info",
                access_log=False)        # access lines would carry query strings, never headers; kept off anyway
    return 0


if __name__ == "__main__":
    sys.exit(main())
