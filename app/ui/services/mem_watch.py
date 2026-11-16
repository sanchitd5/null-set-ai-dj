"""Process memory watch: RSS for the authenticated /api/health line and a log warning when it passes a
threshold (a leak should show in the log long before the container's memory is gone).

RSS is read from /proc/self/statm (Linux; the server's host), falling back to resource.getrusage (peak,
macOS dev machines). DJ_RSS_WARN_MB (default 6144) is the warning threshold; the sampler thread runs
every DJ_RSS_SAMPLE_S (default 30) seconds and warns again only after RSS grew another 512 MB or 10
minutes passed. The systemd drop-in (deploy/nullset-dj.service.d/memory.conf) is the hard guard.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Dict, Optional

WARN_STEP_MB = 512
WARN_EVERY_S = 600.0

_state = {"peak": 0.0, "last_warn_at": 0.0, "last_warn_mb": 0.0}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def warn_mb() -> float:
    return _env_float("DJ_RSS_WARN_MB", 6144.0)


def rss_mb() -> Optional[float]:
    """Resident set size of this process in MB; None when the platform gives no answer."""
    try:
        with open("/proc/self/statm", "rb") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1048576.0
    except (OSError, ValueError, IndexError):
        pass
    try:
        import resource
        import sys

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss      # bytes on macOS, KB on Linux
        return peak / (1048576.0 if sys.platform == "darwin" else 1024.0)
    except (ImportError, OSError, ValueError):
        return None


def snapshot() -> Dict[str, object]:
    mb = rss_mb()
    if mb is not None:
        _state["peak"] = max(_state["peak"], mb)
    limit = warn_mb()
    return {"rss_mb": None if mb is None else round(mb), "rss_peak_mb": round(_state["peak"]),
            "warn_mb": round(limit), "over": bool(mb is not None and mb > limit)}


def check(log: Callable[[str], None] = print, now: Optional[float] = None) -> bool:
    """One sample: warns (rate-limited) when over the threshold. True when over."""
    snap = snapshot()
    mb = snap["rss_mb"]
    if not snap["over"]:
        return False
    now = time.time() if now is None else now
    if mb - _state["last_warn_mb"] >= WARN_STEP_MB or now - _state["last_warn_at"] >= WARN_EVERY_S:
        _state["last_warn_at"], _state["last_warn_mb"] = now, float(mb)
        log(f"WARNING [memory] RSS {mb} MB is over {snap['warn_mb']} MB (peak {snap['rss_peak_mb']} MB, "
            f"{threading.active_count()} threads): a cache or a job is growing")
    return True


def start(log: Callable[[str], None] = print) -> bool:
    """The sampler thread (one per process)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    _stop.clear()
    period = max(1.0, _env_float("DJ_RSS_SAMPLE_S", 30.0))

    def loop() -> None:
        while not _stop.wait(period):
            check(log)

    _thread = threading.Thread(target=loop, name="mem-watch", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    _stop.set()
