"""YouTube's related songs of the playing song, for the next-song pool (GRAPHDB G16).

`/api/autopilot/suggest` adds these as a third candidate source (`source: "yt"`) next to the atlas neighbours and the
catalogue. Where the list comes from, in order:

  1. graph.db `yt_related` / `yt_upnext` edges of the song (read through the atlas bridge; the idle expansion keeps
     them above FANOUT_MIN),
  2. the per-video cache (7 days, in memory and in `yt_related_cache.json`),
  3. an on-demand fetch of the video's mix playlist (RD<id>) through yt_guard, paced, in a worker thread. The caller
     waits at most `FETCH_TIMEOUT_S`; a slower fetch keeps running and fills the cache for the next request.

A fetched list is also stored as `yt_edge` user marks (the same store the idle expansion writes), so the graph learns
it at its next import. `kick()` starts the fetch in the background as soon as a song is loaded. Nothing here raises
into a request: every failure is an empty list.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Optional

CACHE_TTL_S = 7 * 86400
FETCH_TIMEOUT_S = 3.0
PACE_S = 5.0                    # least gap between two on-demand YouTube calls of this module
FAIL_RETRY_S = 120.0            # a failed fetch is not tried again for this long
LIMIT = 25                      # mix entries read per fetch (the yt_edge ingest takes 25 per call)
MIN_GRAPH = 5                   # graph rows at or above this are enough, no fetch
MAX_ROWS = 40
CACHE_MAX = 500
MARK_DEVICE = {"device_id": "server", "device_name": "server", "set_id": "live"}

_lock = threading.RLock()
_cache: dict[str, tuple[float, list[dict]]] = {}        # video id -> (at, rows)
_loaded = False
_inflight: dict[str, object] = {}                        # video id -> Future
_failed: dict[str, float] = {}
_last_call = 0.0
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="yt-related")
_kick_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yt-related-kick")


# ---------------------------------------------------------------------------------------------- ids and graph
def _cache_dir() -> Path:
    from app.music_brain.config import CACHE_DIR

    return Path(CACHE_DIR)


def _graph(track_id: str = "", yt_id: str = "") -> Optional[dict]:
    from app.music_brain import atlas_bridge

    return atlas_bridge.graph_view("yt_related", yt_id, track_id, MAX_ROWS)


def video_id(track_id: str, graph: Optional[dict] = None) -> Optional[str]:
    """The YouTube video id of a library track: the downloads metadata (sources.json), else the graph node's yt_id."""
    try:
        from app.ui.services import album_art

        vid = album_art._video_id(track_id)
        if vid:
            return vid
    except Exception:
        pass
    try:
        g = graph if graph is not None else _graph(track_id=track_id)
        return (g or {}).get("yt_id") or None
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------- cache
def _cache_path() -> Path:
    return _cache_dir() / "yt_related_cache.json"


def _load() -> None:
    global _loaded
    with _lock:
        if _loaded:
            return
        _loaded = True
        try:
            for vid, (at, rows) in json.loads(_cache_path().read_text(encoding="utf-8")).items():
                _cache[vid] = (float(at), list(rows))
        except (OSError, ValueError, TypeError):
            pass


def _save() -> None:
    try:
        keep = dict(sorted(_cache.items(), key=lambda kv: kv[1][0])[-CACHE_MAX:])
        tmp = _cache_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(keep), encoding="utf-8")
        tmp.replace(_cache_path())
    except OSError:
        pass


def cached(vid: str) -> Optional[list[dict]]:
    _load()
    with _lock:
        hit = _cache.get(vid)
        return list(hit[1]) if hit and time.time() - hit[0] < CACHE_TTL_S else None


def _remember(vid: str, rows: list[dict]) -> None:
    with _lock:
        _cache.pop(vid, None)
        _cache[vid] = (time.time(), rows)
        while len(_cache) > CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _save()


# ---------------------------------------------------------------------------------------------- fetch
def fetch_mix(vid: str, limit: int = LIMIT) -> list[dict]:
    """The video's mix playlist [{id, title, channel, duration}], the video itself left out. One guarded YouTube call."""
    from app.ui.services import download_service as ds

    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True,
            "playlistend": limit + 1}
    info = ds._guarded_extract(opts, f"https://www.youtube.com/watch?v={vid}&list=RD{vid}")
    out = []
    for e in (info or {}).get("entries") or []:
        i, title = e.get("id"), e.get("title") or ""
        if not i or i == vid or not title:
            continue
        out.append({"id": i, "title": title, "channel": e.get("channel") or e.get("uploader") or "",
                    "duration": e.get("duration")})
        if len(out) >= limit:
            break
    return out


def _rows_of(entries: list[dict]) -> list[dict]:
    """Pool rows {artist, title, source: "yt", yt_id, rank} from mix entries; non-songs and nameless ones dropped."""
    from app.ui.services import download_service as ds
    from app.ui.services.track_identity import clean_identity

    out = []
    for rank, e in enumerate(entries):
        title = str(e.get("title") or "")
        dur = e.get("duration")
        if ds._is_mix(title) or ds._is_non_music(title) or ds._is_live(title):
            continue
        if dur is not None and not (ds.MIN_TRACK_SECS <= dur < ds.MAX_TRACK_SECS):
            continue
        artist, name = clean_identity(title)
        if artist == "Unknown":
            ch = str(e.get("channel") or "")
            artist = ch[: -len("- topic")].strip(" -") if ch.lower().endswith("- topic") else "Unknown"
        if not artist or artist == "Unknown" or not name:
            continue
        out.append({"artist": artist, "title": name, "source": "yt", "yt_id": e["id"], "rank": rank})
    return out


def _store_marks(vid: str, seed: dict, entries: list[dict]) -> None:
    """The list as `yt_edge` marks (seed -> each related song), so the graph learns it at its next import."""
    from app.music_brain.learning import phone_learn

    related = []
    for rank, r in enumerate(_rows_of(entries)[: phone_learn.MAX_YT_EDGES]):
        related.append({"yt": r["yt_id"], "rank": min(rank, 99), "artist": r["artist"], "title": r["title"]})
    if not related or not seed.get("title"):
        return
    d = {"seed": {"yt": vid, "id": seed.get("track_id") or "", "artist": seed.get("artist") or "",
                  "title": seed["title"]}, "related": related}
    phone_learn._yt_edges({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, d, MARK_DEVICE, _cache_dir())


def _fetch_and_remember(vid: str, seed: dict) -> list[dict]:
    """Worker body: pace, one guarded fetch, cache, marks. Failures are remembered briefly and give []."""
    global _last_call
    try:
        with _lock:
            wait = _last_call + PACE_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        with _lock:
            _last_call = time.monotonic()
        entries = fetch_mix(vid)
        rows = _rows_of(entries)
        _remember(vid, rows)
        try:
            _store_marks(vid, seed, entries)
        except Exception:
            pass                                          # the marks are a bonus; the list is already usable
        return rows
    except Exception:                                     # incl. yt_guard.Cooling: no request while YouTube cools
        with _lock:
            _failed[vid] = time.time()
        return []
    finally:
        with _lock:
            _inflight.pop(vid, None)


def _start(vid: str, seed: dict):
    """The fetch future for `vid`, started when none is running and the last try did not just fail."""
    with _lock:
        fut = _inflight.get(vid)
        if fut is not None:
            return fut
        if time.time() - _failed.get(vid, 0.0) < FAIL_RETRY_S:
            return None
        fut = _pool.submit(_fetch_and_remember, vid, seed)
        _inflight[vid] = fut
        return fut


def _kick_now(track_id: str, seed: dict) -> None:
    g = _graph(track_id=track_id)
    vid = video_id(track_id, g)
    if not vid or len(((g or {}).get("rows") or [])) >= MIN_GRAPH or cached(vid) is not None:
        return
    fut = _start(vid, dict(seed, track_id=track_id))
    if fut is not None:
        fut.result()                                       # holds this worker until the fetch is done: no pile-up


def kick(track_id: str, seed: Optional[dict] = None) -> None:
    """Start the fetch in the background for a song just loaded (no wait, no result). Never raises."""
    try:
        _kick_pool.submit(_safe, _kick_now, track_id, dict(seed or {}))
    except Exception:
        pass


def _safe(fn, *a) -> None:
    try:
        fn(*a)
    except Exception:
        pass


# ---------------------------------------------------------------------------------------------- the pool source
def candidates(track_id: str, seed: Optional[dict] = None, timeout: float = FETCH_TIMEOUT_S) -> tuple[list[dict], dict]:
    """(rows, info) for the suggest pool. rows: [{artist, title, source: "yt", yt_id?, track_id?}] best first.
    info: {source: graph | cache | fetch | empty | timeout | cooling | no_id | error, n}: where they came from, for the shadow log.
    Never raises, never waits longer than `timeout` (seconds) for YouTube."""
    seed = dict(seed or {}, track_id=track_id)
    try:
        g = _graph(track_id=track_id)
        grows = [{"artist": r["artist"], "title": r["title"], "source": "yt",
                  **({"yt_id": r["yt_id"]} if r.get("yt_id") else {}),
                  **({"track_id": r["track_id"]} if r.get("track_id") else {})}
                 for r in ((g or {}).get("rows") or [])]
        if len(grows) >= MIN_GRAPH:
            return grows, {"source": "graph", "n": len(grows)}
        vid = video_id(track_id, g)
        if not vid:
            return grows, {"source": "graph" if grows else "no_id", "n": len(grows)}
        hit = cached(vid)
        if hit is not None:
            return _merge(grows, hit), {"source": "cache", "n": len(hit) + len(grows)}
        fut = _start(vid, seed)
        if fut is None:
            return grows, {"source": "cooling", "n": len(grows)}
        try:
            rows = fut.result(timeout=timeout)
        except FutureTimeout:
            return grows, {"source": "timeout", "n": len(grows)}      # the fetch goes on and fills the cache
        return _merge(grows, rows), {"source": "fetch" if rows else "empty", "n": len(rows) + len(grows)}
    except Exception:
        return [], {"source": "error", "n": 0}


def _merge(first: list[dict], second: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in first + second:
        k = (r["artist"].lower(), r["title"].lower())
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out
