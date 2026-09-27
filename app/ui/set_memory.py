"""Cross-set song memory, so the next set from the same seed isn't a replay.

Every suggest request carries the current set's played history. This module
records those titles (bare, lower-cased) with a timestamp in a small JSON file
under the cache dir, and hands back the ones played in EARLIER sets (not the
current one) so the prompt can ask for fresh picks. It is a soft avoid: the
model may still pick a remembered song when it is clearly the best fit.

Kept small: MAX_SONGS most recent titles, older than MAX_AGE_DAYS dropped.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Iterable, List

MAX_SONGS = 300
MAX_AGE_DAYS = 14
PROMPT_LIMIT = 40
FAVOURITE_MIN_SONGS = 3   # different remembered songs by one artist -> a favourite

_lock = threading.Lock()


def artist_key(name: str) -> str:
    """"Fred again..", "Fred Again", "FRED AGAIN..." -> "fredagain"."""
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


def _key(title: str) -> str:
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", str(title).lower())
    return " ".join(re.sub(r"[^a-z0-9 \-]+", " ", t).split())


class SetMemory:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict = {}
        try:
            self._data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass  # memory is best-effort, never breaks a suggest call

    def record(self, played: Iterable[str], set_id: str = "") -> None:
        """Remember `played` as heard in set `set_id` ("" = unscoped caller)."""
        now = time.time()
        with _lock:
            for name in played:
                k = _key(name)
                if k:
                    self._data[k] = {"name": str(name)[:120], "t": now, "set": set_id}
            cutoff = now - MAX_AGE_DAYS * 86400
            items = sorted(((k, v) for k, v in self._data.items() if v.get("t", 0) >= cutoff),
                           key=lambda kv: kv[1]["t"], reverse=True)[:MAX_SONGS]
            self._data = dict(items)
            self._save()

    def favourite_artists(self, min_songs: int = FAVOURITE_MIN_SONGS, limit: int = 12) -> List[str]:
        """Artists the listener keeps coming back to: >= min_songs different
        remembered songs credit them. Their songs must not be avoided just for
        having been heard (user: "biased against Fred again.. songs")."""
        from app.ui.track_identity import credited_artists
        counts: dict = {}
        shown: dict = {}
        with _lock:
            names = [v["name"] for v in self._data.values()]
        for name in names:
            for a in {artist_key(x): x for x in credited_artists(str(name))}.items():
                if a[0]:
                    counts[a[0]] = counts.get(a[0], 0) + 1
                    shown.setdefault(a[0], a[1].strip(" .") + (".." if a[1].rstrip().endswith("..") else ""))
        top = sorted((k for k, c in counts.items() if c >= min_songs), key=lambda k: -counts[k])
        return [shown[k] for k in top[:limit]]

    def earlier_sets(self, current: Iterable[str], limit: int = PROMPT_LIMIT,
                     set_id: str = "") -> List[str]:
        """Most recent remembered songs that are NOT in the current set.

        With a set_id, everything this set recorded counts as the current set
        too (the request's history is only its last 30 songs), so a long set's
        own early songs never come back as "heard in an earlier set"."""
        cur = {_key(c) for c in current}
        with _lock:
            items = sorted(self._data.items(), key=lambda kv: kv[1]["t"], reverse=True)
        return [v["name"] for k, v in items
                if k not in cur and not (set_id and v.get("set") == set_id)][:limit]
