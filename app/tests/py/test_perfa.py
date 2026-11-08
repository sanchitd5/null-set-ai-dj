"""PERFA: persisted content-hash index, analysis batch endpoint, art mtime map, warm-visible JS check."""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.music_brain import hash_index
from app.music_brain.analysis import analyzer
from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import album_art as aa


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_hash_index_hit_miss_restart(tmp_path, monkeypatch):
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello" * 1000)
    idx_path = tmp_path / "idx" / "hash_index.json"
    idx = hash_index.HashIndex(idx_path)
    assert idx.sha256(f) == _sha(b"hello" * 1000)

    reads = []
    real_open = open

    def spy(p, *a, **k):
        if str(p) == str(f):
            reads.append(1)
        return real_open(p, *a, **k)

    monkeypatch.setattr("builtins.open", spy)
    assert idx.sha256(f) == _sha(b"hello" * 1000)
    assert reads == [], "second ask is a hit: no file read"

    idx.flush()
    idx2 = hash_index.HashIndex(idx_path)           # restart: a new instance reads the file
    assert idx2.sha256(f) == _sha(b"hello" * 1000)
    assert reads == [], "survives a restart: still no file read"

    st = f.stat()
    f.write_bytes(b"world" * 1000)                   # same size, new content
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    assert idx2.sha256(f) == _sha(b"world" * 1000)
    assert reads, "a changed file is re-hashed"


def test_hash_index_corrupt_file_and_threads(tmp_path):
    p = tmp_path / "hash_index.json"
    p.write_text("{not json")
    idx = hash_index.HashIndex(p)
    files = []
    for i in range(8):
        f = tmp_path / f"{i}.bin"
        f.write_bytes(bytes([i]) * 100)
        files.append(f)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as ex:
        got = list(ex.map(idx.sha256, files * 3))
    assert got == [_sha(bytes([i]) * 100) for i in range(8)] * 3
    idx.flush()
    import json

    assert len(json.loads(p.read_text())) == 8


def test_ids_unchanged_by_index(tmp_path, monkeypatch):
    from app.music_brain import config
    from app.music_brain.audio import stem_service
    from app.ui.services import library_service

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    f = tmp_path / "x.mp3"
    f.write_bytes(b"abc" * 99)
    full = _sha(b"abc" * 99)
    assert stem_service.file_hash(f) == full
    assert analyzer._file_hash_impl(f) == full
    assert library_service.file_hash(f) == full[:16]
    hash_index.default().flush()
    assert (tmp_path / "hash_index.json").exists()


# ---- batch endpoint ---------------------------------------------------------------------------
@pytest.fixture
def lib(monkeypatch, tmp_path):
    p = tmp_path / "s.mp3"
    p.write_bytes(b"x")
    monkeypatch.setattr(server, "_tracks", {"aaa1": p, "bbb2": p})
    return p


def test_batch_endpoint(lib, monkeypatch):
    class R:
        def to_dict(self):
            return {"bpm": 120.0}

    monkeypatch.setattr(analyzer, "cached_analysis", lambda p: R())
    c = TestClient(server.app)
    r = c.post("/api/tracks/analysis/batch", json={"ids": ["aaa1", "nope"]})
    assert r.status_code == 200
    body = r.json()
    assert body["aaa1"]["bpm"] == 120.0 and "art_url" in body["aaa1"]
    assert body["nope"] is None
    monkeypatch.setattr(analyzer, "cached_analysis", lambda p: None)
    assert c.post("/api/tracks/analysis/batch", json={"ids": ["aaa1"]}).json() == {"aaa1": None}
    assert c.post("/api/tracks/analysis/batch", json={"ids": [f"i{n}" for n in range(50)]}).status_code == 200
    assert c.post("/api/tracks/analysis/batch", json={"ids": [f"i{n}" for n in range(51)]}).status_code == 413
    assert TestClient(server.app, auth=False).post("/api/tracks/analysis/batch", json={"ids": []}).status_code in (401, 403)


# ---- art mtime map ----------------------------------------------------------------------------
def test_art_url_map(tmp_path, monkeypatch):
    from app.music_brain import config

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    aa.invalidate_art_index()
    d = tmp_path / "art"
    d.mkdir()
    (d / "t1.jpg").write_bytes(b"j")
    (d / "t1.160.jpg").write_bytes(b"j")
    (d / "t2.json").write_text("{}")
    mt = (d / "t1.jpg").stat().st_mtime_ns // 1_000_000
    assert aa.art_url("t1") == f"/api/tracks/t1/art?size=600&v={mt:x}"
    assert aa.art_url("t2") is None and aa.art_url("t1.160") is None and aa.art_url("../x") is None
    calls = []
    real = Path.stat
    monkeypatch.setattr(Path, "stat", lambda self, *a, **k: (calls.append(self), real(self, *a, **k))[1])
    for _ in range(100):
        aa.art_url("t1")
    assert calls == [], "no stat per call once the map is built"
    monkeypatch.undo()
    # the art worker writes a new cover -> the map follows
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    import io
    from PIL import Image

    b = io.BytesIO()
    Image.new("RGB", (700, 700), (1, 2, 3)).save(b, "JPEG")
    assert aa.art_url("t3") is None
    aa._store("t3", b.getvalue(), {"source": "test"})
    assert aa.art_url("t3") and "t3" in aa.art_url("t3")
    aa.invalidate_art_index()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_warm_visible_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).parents[1] / "js" / "warm_visible_check.js")],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout
