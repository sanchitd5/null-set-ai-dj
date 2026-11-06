"""POST /api/tracks/{id}/lyrics/transcribe (lyrics_asr on the cached vocal stem) and GET /api/build
(the console's reload prompt). Server state and the ASR module are patched per test."""
import pytest

from app.music_brain.analysis import lyrics_asr
from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import build_info

DOC = {"language": "en", "model": "large-v3-turbo", "backend": "cuda", "stats": {"kept": 3},
       "lines": [{"t": 1.0, "text": "hold on", "conf": 0.9}]}


@pytest.fixture
def lib(monkeypatch, tmp_path):
    a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    monkeypatch.setattr(server, "_tracks", {"aaa1": a, "bbb2": b})
    stems = {"aaa1": {"vocals": str(tmp_path / "vocals.wav"), "drums": "d", "bass": "b", "other": "o"}}
    monkeypatch.setattr(server, "_cached_stems4", lambda tid: stems.get(tid))
    calls = {"for_stem": [], "release": 0}
    monkeypatch.setattr(lyrics_asr, "cached", lambda p, *a, **k: None)
    monkeypatch.setattr(lyrics_asr, "pick_backend", lambda *a, **k: "cuda")
    monkeypatch.setattr(lyrics_asr, "busy", lambda: False)
    monkeypatch.setattr(lyrics_asr, "for_stem", lambda p, force=False, **k: calls["for_stem"].append((p, force)) or DOC)

    def release():
        calls["release"] += 1

    monkeypatch.setattr(lyrics_asr, "release", release)
    server._hook_drop_cache["aaa1"] = ("old", None)
    server._hook_drop_miss["aaa1"] = 1.0
    yield calls
    server._hook_drop_cache.pop("aaa1", None)
    server._hook_drop_miss.pop("aaa1", None)


@pytest.fixture
def client():
    return TestClient(server.app)


def test_transcribes_the_vocal_stem_and_clears_hook_drop_caches(lib, client, tmp_path):
    r = client.post("/api/tracks/aaa1/lyrics/transcribe")
    assert r.status_code == 200
    assert r.json() == {"track_id": "aaa1", "language": "en", "model": "large-v3-turbo", "backend": "cuda",
                        "stats": {"kept": 3}, "lines": DOC["lines"]}
    assert lib["for_stem"] == [(str(tmp_path / "vocals.wav"), False)] and lib["release"] == 1
    assert "aaa1" not in server._hook_drop_cache and "aaa1" not in server._hook_drop_miss


def test_cached_result_needs_no_backend_and_force_transcribes_again(lib, client, monkeypatch):
    monkeypatch.setattr(lyrics_asr, "cached", lambda p, *a, **k: DOC)
    monkeypatch.setattr(lyrics_asr, "pick_backend", lambda *a, **k: None)
    r = client.post("/api/tracks/aaa1/lyrics/transcribe")
    assert r.status_code == 200 and r.json()["lines"] == DOC["lines"]
    assert lib["for_stem"] == [] and lib["release"] == 0              # the GPU is never touched
    assert client.post("/api/tracks/aaa1/lyrics/transcribe?force=true").status_code == 503


def test_refusals(lib, client, monkeypatch):
    assert client.post("/api/tracks/nope/lyrics/transcribe").status_code == 404
    assert client.post("/api/tracks/bbb2/lyrics/transcribe").status_code == 409     # no vocal stem
    monkeypatch.setattr(lyrics_asr, "busy", lambda: True)
    r = client.post("/api/tracks/aaa1/lyrics/transcribe")
    assert r.status_code == 409 and "another transcription" in r.json()["detail"]
    monkeypatch.setattr(lyrics_asr, "pick_backend", lambda *a, **k: None)
    assert client.post("/api/tracks/aaa1/lyrics/transcribe").status_code == 503
    assert lib["for_stem"] == []


def test_release_runs_even_when_transcription_fails(lib, client, monkeypatch):
    monkeypatch.setattr(lyrics_asr, "for_stem", lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        client.post("/api/tracks/aaa1/lyrics/transcribe")
    assert lib["release"] == 1


def test_transcribe_and_build_need_auth():
    server.AUTH_GUARD.limiter.reset()
    try:
        anon = TestClient(server.app, auth=False)
        assert anon.post("/api/tracks/aaa1/lyrics/transcribe").status_code == 401
        assert anon.get("/api/build").status_code == 401
        assert anon.get("/health").json() == {"ok": True}                # the public probe stays bare
    finally:
        server.AUTH_GUARD.limiter.reset()


def test_build_id(client, monkeypatch):
    r = client.get("/api/build").json()
    assert r["build"] == build_info.BUILD_ID and r["build"] and isinstance(r["started"], int)
    assert build_info.compute_build_id({"DJ_BUILD_ID": " abc123 "}) == "abc123"

    def no_git(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(build_info.subprocess, "run", no_git)
    assert build_info.compute_build_id({}).startswith("run-")                     # no git: per-process token
