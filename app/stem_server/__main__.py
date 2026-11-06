"""python -m app.stem_server  (env: STEM_SERVER_TOKEN required, STEM_SERVER_HOST, STEM_PORT).

Routes are relative (no self-URLs), so a reverse proxy may mount the server under a stripped prefix."""

import os
import sys

import uvicorn

from app.stem_server.server import create_app


def main() -> int:
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
