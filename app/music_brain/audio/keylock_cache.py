"""Age cap (LRU by mtime) on the regenerable key-locked tempo sets in data/cache/keylock/t*.

Only `t*` dirs count (tempo_key names; the riff sets are hex and stay). Stems in data/cache/stems are never
touched: they cost a Demucs run. A set's last use is the mtime of its dir, bumped by keylock.touch() on
every serve. Run:  python3 -m app.music_brain.audio.keylock_cache [--dry-run|--apply] [--max-gb N]
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional

from app.music_brain.audio import keylock

log = logging.getLogger(__name__)

DEFAULT_MAX_GB = 20.0
MIN_AGE_S = 30 * 60
DEBOUNCE_S = 60.0


def max_bytes_from_env(env=None) -> Optional[int]:
    """KEYLOCK_CACHE_MAX_GB: unset / 0 / not a number -> 20 GB; negative -> None (disabled)."""
    raw = (os.environ if env is None else env).get("KEYLOCK_CACHE_MAX_GB")
    try:
        gb = float(raw)
    except (TypeError, ValueError):
        gb = 0.0
    if gb < 0:
        return None
    return int((gb or DEFAULT_MAX_GB) * 1e9)


def _size(d: Path) -> int:
    total = 0
    try:
        for f in d.iterdir():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def evict_tempo_sets(cache_dir: Path, max_bytes: Optional[int], protect: Iterable[str] = (),
                     min_age_s: float = MIN_AGE_S, apply: bool = True, now: Optional[float] = None) -> dict:
    """Delete whole `t*` sets oldest-first until the total is <= max_bytes. Skips protected keys, `.tmp`
    (in-flight) dirs and sets younger than min_age_s. Returns {before, after, removed: [{key, bytes, age_s}]}."""
    now = time.time() if now is None else now
    protect = set(protect)
    sets = []
    try:
        entries = list(Path(cache_dir).iterdir())
    except OSError:
        entries = []
    for d in entries:
        if not d.name.startswith("t") or d.suffix == ".tmp" or not d.is_dir():
            continue
        try:
            sets.append((d.stat().st_mtime, d, _size(d)))
        except OSError:
            continue
    total = before = sum(s for _, _, s in sets)
    removed: List[dict] = []
    if max_bytes is not None and total > max_bytes:
        for mtime, d, size in sorted(sets, key=lambda x: x[0]):
            if total <= max_bytes:
                break
            age = now - mtime
            if d.name in protect or age < min_age_s:
                continue
            if apply:
                shutil.rmtree(d, ignore_errors=True)
                if d.exists():
                    continue
            total -= size
            removed.append({"key": d.name, "bytes": size, "age_s": round(age)})
    return {"before": before, "after": total, "removed": removed}


_run_lock = threading.Lock()
_last_run = 0.0


def run_async(reason: str = "render") -> None:
    """Debounced (one run per minute), off the caller's thread; quiet unless something is evicted."""
    global _last_run
    now = time.monotonic()
    with _run_lock:
        if now - _last_run < DEBOUNCE_S:
            return
        _last_run = now
    threading.Thread(target=_run, args=(reason,), daemon=True, name="keylock-evict").start()


def _run(reason: str) -> None:
    try:
        cap = max_bytes_from_env()
        with keylock._lock:
            busy = [k for k, v in keylock._jobs.items() if v == "running"]
        res = evict_tempo_sets(keylock.KEYLOCK_DIR, cap, protect=busy)
        for r in res["removed"]:
            log.info("cache_evict key=%s bytes=%d age_s=%d reason=%s", r["key"], r["bytes"], r["age_s"], reason)
            try:
                from app.ui.services import session_log

                session_log.log("cache_evict", key=r["key"], bytes=r["bytes"], age_s=r["age_s"], reason=reason)
            except Exception:
                pass
    except Exception as exc:
        log.warning("keylock cache eviction failed: %s: %s", type(exc).__name__, exc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="print what would be removed (default)")
    g.add_argument("--apply", action="store_true", help="delete")
    ap.add_argument("--max-gb", type=float, help="override KEYLOCK_CACHE_MAX_GB")
    a = ap.parse_args(argv)
    cap = max_bytes_from_env({"KEYLOCK_CACHE_MAX_GB": str(a.max_gb)} if a.max_gb is not None else None)
    if cap is None:
        print("cap disabled (negative)")
        return 0
    res = evict_tempo_sets(keylock.KEYLOCK_DIR, cap, apply=a.apply)
    for r in res["removed"]:
        print(f"{'removed' if a.apply else 'would remove'} {r['key']}  {r['bytes'] / 1e6:.0f} MB  age {r['age_s'] / 3600:.1f} h")
    print(f"total before {res['before'] / 1e9:.2f} GB, after {res['after'] / 1e9:.2f} GB, cap {cap / 1e9:.1f} GB "
          f"({len(res['removed'])} sets, {'applied' if a.apply else 'dry-run'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
