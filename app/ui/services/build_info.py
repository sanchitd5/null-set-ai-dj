"""GET /api/build: which server build is running (included by server.py).

The console (build-watch.js) polls it and shows "New version - reload" once the id changes, so an
open page never runs old JS against a newer server. Behind auth, not on /health: the public probe
stays bare (docs/engineering/AUTH.md).

The id is computed once at import: DJ_BUILD_ID when set, else the checkout's git short sha, else a
per-process token (every restart then reads as a new build, which errs toward offering a reload).
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()
REPO = Path(__file__).resolve().parents[3]


def compute_build_id(environ=os.environ, repo: Path = REPO) -> str:
    env = (environ.get("DJ_BUILD_ID") or "").strip()
    if env:
        return env[:64]
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo), capture_output=True,
                           text=True, timeout=5)
        sha = r.stdout.strip()
        if r.returncode == 0 and sha:
            return sha
    except (OSError, subprocess.SubprocessError):
        pass
    return f"run-{int(time.time())}"


BUILD_ID = compute_build_id()
STARTED = time.time()


@router.get("/api/build")
def get_build():
    return {"build": BUILD_ID, "started": int(STARTED)}
