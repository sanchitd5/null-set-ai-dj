"""Background download jobs with pollable progress.

POST /api/download/jobs starts a job and returns its id; GET polls it. Each job
runs in a worker thread: download (yt-dlp byte percent) -> register
in the track registry -> analyze (warms the analysis cache, so the autopilot
does not pay for it at transition time) -> length check.

Length check: every song must be 90 s - 9 min (clips / sets are rejected). The
duration is returned so the caller can apply stricter rules (the autopilot's
LONG set mode needs songs of 3 min or more).
"""

from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app.ui import engine

MIN_SONG_SECS = 90
MAX_SONG_SECS = 9 * 60
MAX_JOBS_KEPT = 50
_WORKERS = 2  # parallel downloads; more gets rate-limited / 403'd by YouTube

_executor = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="dl-job")
_jobs: Dict[str, dict] = {}
_lock = threading.Lock()


def _update(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)
            job["updated_at"] = engine.current().host.now()


def get_job(job_id: str) -> Optional[dict]:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def list_jobs() -> List[dict]:
    with _lock:
        return sorted((dict(j) for j in _jobs.values()), key=lambda j: j["created_at"], reverse=True)


def _prune() -> None:
    finished = [j for j in _jobs.values() if j["state"] in ("done", "error")]
    finished.sort(key=lambda j: j["created_at"])
    for j in finished[: max(0, len(_jobs) - MAX_JOBS_KEPT)]:
        _jobs.pop(j["id"], None)


def start_job(
    url: str,
    label: str,
    upload_dir: Path,
    download_fn: Callable,
    register_fn: Callable[[List[Path]], List[dict]],
    analyze_fn: Callable[[str], object],
) -> str:
    """Queue a download. register_fn moves files into the registry and returns
    [{track_id, filename, display_name}]; analyze_fn(track_id) -> TrackAnalysis."""
    host = engine.current().host
    job_id = host.new_id(6)
    now = host.now()
    with _lock:
        _jobs[job_id] = {
            "id": job_id, "url": url, "label": label or url, "state": "queued",
            "stage": "queued", "percent": None, "tracks": [], "error": None,
            "created_at": now, "updated_at": now,
        }
        _prune()

    def _run() -> None:
        tmp_dir = upload_dir / f"_dl_{host.new_id(16)}"
        try:
            _update(job_id, state="running", stage="starting")
            paths = download_fn(
                url, tmp_dir,
                progress=lambda stage, pct=None: _update(job_id, stage=stage, percent=pct),
            )
            _update(job_id, stage="registering", percent=None)
            tracks = register_fn(paths)
            for t in tracks:
                _update(job_id, stage="analyzing", percent=None)
                analysis = analyze_fn(t["track_id"])
                t["duration"] = round(float(getattr(analysis, "duration", 0.0) or 0.0), 1)
                t["bpm"] = round(float(getattr(analysis, "bpm", 0.0) or 0.0), 1)
                if not (MIN_SONG_SECS <= t["duration"] < MAX_SONG_SECS):
                    raise RuntimeError(
                        f"length check failed: {t['duration'] / 60:.1f} min "
                        f"(songs must be {MIN_SONG_SECS // 60}.5-{MAX_SONG_SECS // 60} min)"
                    )
            _update(job_id, state="done", stage="ready", percent=100.0, tracks=tracks)
        except Exception as exc:  # surfaced to the UI, never raised into the server
            _update(job_id, state="error", stage="failed", error=str(exc)[:300])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    host.spawn(_executor, _run)
    return job_id
