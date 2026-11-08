"""Persisted content-hash index: (path, size, mtime_ns, inode) -> sha256 hex.

Content-hash track ids are a contract (sha256(bytes)[:16]); this only memoizes the full-file read
so a restart never re-reads unchanged audio. A file whose size, mtime or inode changed is re-hashed.
Stored at CACHE_DIR/hash_index.json, written atomically (tmp + os.replace), one lock for all threads.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional

_FLUSH_S = 2.0       # write-behind: at most one disk write per interval while hashing a cold library


class HashIndex:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._d: Optional[Dict[str, list]] = None
        self._dirty = False
        self._saved = 0.0

    def _load(self) -> Dict[str, list]:
        if self._d is None:
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                self._d = d if isinstance(d, dict) else {}
            except (OSError, ValueError):
                self._d = {}
        return self._d

    def flush(self) -> None:
        with self._lock:
            self._save()

    def _save(self) -> None:
        if not self._dirty or self._d is None:
            return
        tmp = self.path.with_name(f"{self.path.name}.tmp{os.getpid()}.{threading.get_ident()}")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._d), encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
            self._saved = time.monotonic()
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def sha256(self, path) -> str:
        p = Path(path)
        st = p.stat()
        sig = [st.st_size, st.st_mtime_ns, st.st_ino]
        key = str(p)
        with self._lock:
            ent = self._load().get(key)
            if ent and ent[:3] == sig:
                return ent[3]
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
        with self._lock:
            self._load()[key] = sig + [digest]
            self._dirty = True
            if time.monotonic() - self._saved >= _FLUSH_S:
                self._save()
        return digest


_instances: Dict[str, HashIndex] = {}
_inst_lock = threading.Lock()


def default() -> HashIndex:
    from app.music_brain import config

    path = str(Path(config.CACHE_DIR) / "hash_index.json")
    with _inst_lock:
        idx = _instances.get(path)
        if idx is None:
            idx = _instances[path] = HashIndex(Path(path))
        return idx


def sha256_file(path) -> str:
    return default().sha256(path)


def flush_all() -> None:
    with _inst_lock:
        items = list(_instances.values())
    for i in items:
        i.flush()


atexit.register(flush_all)
