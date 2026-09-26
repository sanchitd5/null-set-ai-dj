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

_lock = threading.Lock()


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

    def record(self, played: Iterable[str]) -> None:
        now = time.time()
        with _lock:
            for name in played:
                k = _key(name)
                if k:
                    self._data[k] = {"name": str(name)[:120], "t": now}
            cutoff = now - MAX_AGE_DAYS * 86400
            items = sorted(((k, v) for k, v in self._data.items() if v.get("t", 0) >= cutoff),
                           key=lambda kv: kv[1]["t"], reverse=True)[:MAX_SONGS]
            self._data = dict(items)
            self._save()

    def earlier_sets(self, current: Iterable[str], limit: int = PROMPT_LIMIT) -> List[str]:
        """Most recent remembered songs that are NOT in the current set."""
        cur = {_key(c) for c in current}
        with _lock:
            items = sorted(self._data.items(), key=lambda kv: kv[1]["t"], reverse=True)
        return [v["name"] for k, v in items if k not in cur][:limit]
