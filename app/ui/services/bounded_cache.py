"""A small thread-safe LRU dict for per-track caches. The console used plain module dicts for them
(hook-drop lines with the whole analysis, voiced rows, lyrics...); on a long-running server with
a big library they only ever grew. Same dict calls (get, setdefault, [], in, pop, clear, len), least recently
used entry dropped past `maxsize`."""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Hashable


class BoundedCache:
    def __init__(self, maxsize: int):
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.maxsize = maxsize
        self._d: "OrderedDict[Hashable, Any]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            if key not in self._d:
                return default
            self._d.move_to_end(key)
            return self._d[key]

    def __getitem__(self, key: Hashable) -> Any:
        with self._lock:
            self._d.move_to_end(key)             # KeyError when absent
            return self._d[key]

    def __setitem__(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)

    def setdefault(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
            self._d[key] = default
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)
            return default

    def __contains__(self, key: Hashable) -> bool:
        with self._lock:
            return key in self._d

    def pop(self, key: Hashable, *default: Any) -> Any:
        with self._lock:
            return self._d.pop(key, *default)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)
