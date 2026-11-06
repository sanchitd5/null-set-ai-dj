"""The phone's REST wrappers (app/ui/services/mobile_api.py): library, track, labels, lyrics,
recordings, sync. Server state is patched per test; the real label store is never written."""
import os
import time
from types import SimpleNamespace

import pytest

from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import mobile_api


@pytest.fixture
def lib(monkeypatch, tmp_path):
    songs = {"aaa1": ("Bicep - Glue.mp3", "Bicep - Glue"),
             "bbb2": ("x.WAV", "Daft Punk - One More Time"),
             "ccc3": ("y.flac", "untitled jam"),
             "ddd4": ("z.mp3", "Someone - Live at Somewhere Festival 2019")}
    tracks, names = {}, {}
    for tid, (fn, name) in songs.items():
        p = tmp_path / fn
        p.write_bytes(b"x" * (len(tid) + len(name)))
        tracks[tid], names[tid] = p, name
    tracks["gone5"] = tmp_path / "missing.mp3"
    names["gone5"] = "Missing - Song"
    monkeypatch.setattr(server, "_tracks", tracks)
    monkeypatch.setattr(server, "_track_names", names)
    monkeypatch.setattr(server, "_suggested_genres", {server._genre_key("Glue"): "electronica"})
    monkeypatch.setattr(server, "_suggested_eras", {})
    monkeypatch.setattr(server, "LABELS_PATH", tmp_path / "genre_labels.json")
    monkeypatch.setattr(server, "_labels_dirty", False)
    stems = {"aaa1": {"vocals": str(tmp_path / "vocals.wav"), "drums": "d", "bass": "b", "other": "o"}}
    monkeypatch.setattr(server, "_cached_stems4", lambda tid: stems.get(tid))
    monkeypatch.setattr(mobile_api, "_lyrics_cache", {})
    monkeypatch.setattr(mobile_api, "_lyrics_miss", {})
    return tmp_path


@pytest.fixture
def client():
    return TestClient(server.app)


def test_library_sorted_entries(lib, client):
    r = client.get("/api/mobile/library").json()
    assert r["total"] == 4 and r["offset"] == 0
    assert [t["track_id"] for t in r["tracks"]] == ["aaa1", "bbb2", "ddd4", "ccc3"]   # casefolded name order
    glue = r["tracks"][0]
    assert glue == {"track_id": "aaa1", "display_name": "Bicep - Glue", "artist": "Bicep", "title": "Glue",
                    "ext": ".mp3", "bytes": os.path.getsize(lib / "Bicep - Glue.mp3"), "genre": "electronica",
                    "era": None, "not_a_song": False, "has_stems": True}
    by = {t["track_id"]: t for t in r["tracks"]}
    assert by["bbb2"]["ext"] == ".wav" and by["bbb2"]["has_stems"] is False
    assert by["ccc3"]["artist"] == ""                    # "Unknown" artist -> ""
    assert by["ddd4"]["not_a_song"] is True


def test_library_query_and_paging(lib, client):
    assert [t["track_id"] for t in client.get("/api/mobile/library?q=DAFT").json()["tracks"]] == ["bbb2"]
    assert [t["track_id"] for t in client.get("/api/mobile/library?q=electron").json()["tracks"]] == ["aaa1"]
    r = client.get("/api/mobile/library?offset=1&limit=2").json()
    assert r["total"] == 4 and [t["track_id"] for t in r["tracks"]] == ["bbb2", "ddd4"]
    assert len(client.get("/api/mobile/library?limit=0").json()["tracks"]) == 1          # clamped to 1
    assert len(client.get("/api/mobile/library?limit=99999").json()["tracks"]) == 4      # clamped to 500
    assert client.get("/api/mobile/library?offset=-5").json()["offset"] == 0
    assert client.get("/api/mobile/library?q=" + "a" * 121).status_code == 400


def test_track_entry_and_ids(lib, client):
    assert client.get("/api/mobile/tracks/aaa1").json()["title"] == "Glue"
    assert client.get("/api/mobile/tracks/nope").status_code == 404
    assert client.get("/api/mobile/tracks/gone5").status_code == 404     # file missing
    assert client.get("/api/mobile/tracks/bad.id").status_code == 400
    assert client.get("/api/mobile/tracks/" + "a" * 65).status_code == 400


def test_labels_write_isolated(lib, client):
    from app.music_brain.analysis import genre_labels as gl

    r = client.post("/api/mobile/tracks/bbb2/labels", json={"genre": "  french house ", "era": "1990s"})
    assert r.status_code == 200 and r.json() == {"track_id": "bbb2", "genre": "french house", "era": "1990s"}
    g, e = gl.load(lib / "genre_labels.json")
    key = server._genre_key("One More Time")
    assert g[key] == "french house" and e[key] == "1990s"
    # null leaves unchanged, "" clears
    r = client.post("/api/mobile/tracks/bbb2/labels", json={"genre": None, "era": ""}).json()
    assert r == {"track_id": "bbb2", "genre": "french house", "era": None}
    assert key not in gl.load(lib / "genre_labels.json")[1]
    assert client.get("/api/mobile/tracks/bbb2").json()["genre"] == "french house"


def test_labels_validation(lib, client):
    assert client.post("/api/mobile/tracks/bbb2/labels", json={"genre": "x" * 81}).status_code == 422
    assert client.post("/api/mobile/tracks/bbb2/labels", json={"era": "x" * 41}).status_code == 422
    assert client.post("/api/mobile/tracks/bbb2/labels", json={"genre": "ho\x00use"}).status_code == 400
    assert client.post("/api/mobile/tracks/bbb2/labels", json={"era": "19\n90s"}).status_code == 400
    assert client.post("/api/mobile/tracks/nope/labels", json={"genre": "house"}).status_code == 404
    assert server._suggested_genres.get(server._genre_key("One More Time")) is None


def test_lyrics_asr_from_cached_stem(lib, client, monkeypatch):
    from app.music_brain.analysis import lyrics

    calls = []
    lines = [{"t": 1.0, "text": "hold on", "source": "asr", "conf": 0.9},
             {"t": 5.0, "text": "hold on", "source": "asr", "conf": 0.9},
             {"t": 9.0, "text": "let go", "source": "asr", "conf": 0.9}]

    def for_file(title, vocal, sr, duration=None, vocal_path=None, **kw):
        calls.append((title, vocal, sr, duration, vocal_path))
        return lines

    monkeypatch.setattr(lyrics, "for_file", for_file)
    monkeypatch.setattr(server, "analyze_track", lambda p: SimpleNamespace(duration=200.0))
    monkeypatch.setattr(server, "_load_audio", lambda p, **kw: ("Y", 11025))
    r = client.get("/api/mobile/tracks/aaa1/lyrics").json()
    assert r["source"] == "asr" and r["transcribed"] is True and r["lines"] == lines
    assert r["hooks"][0]["text"] == "hold on" and r["hooks"][0]["count"] == 2
    assert calls == [("Bicep - Glue", "Y", 11025, 200.0, str(lib / "vocals.wav"))]
    client.get("/api/mobile/tracks/aaa1/lyrics")
    assert len(calls) == 1                                          # cached per track


def test_lyrics_no_stems_and_miss_ttl(lib, client, monkeypatch):
    from app.music_brain.analysis import lyrics

    calls = []
    monkeypatch.setattr(lyrics, "for_file", lambda t, v, sr, **kw: calls.append((v, kw.get("vocal_path"))) or [])
    monkeypatch.setattr(server, "analyze_track", lambda p: SimpleNamespace(duration=100.0))
    monkeypatch.setattr(server, "_load_audio", lambda *a, **k: pytest.fail("no stem: nothing to decode"))
    r = client.get("/api/mobile/tracks/bbb2/lyrics").json()
    assert r == {"track_id": "bbb2", "lines": [], "source": None, "transcribed": False, "hooks": []}
    client.get("/api/mobile/tracks/bbb2/lyrics")
    assert calls == [(None, None)]                                  # the miss is remembered
    mobile_api._lyrics_miss["bbb2"] = time.time() - mobile_api.LYRICS_MISS_TTL_S - 1
    client.get("/api/mobile/tracks/bbb2/lyrics")
    assert len(calls) == 2
    assert client.get("/api/mobile/tracks/nope/lyrics").status_code == 404


def test_recordings_newest_first(lib, client, monkeypatch):
    old, new = lib / "r1.webm", lib / "r2.wav"
    old.write_bytes(b"o" * 3)
    new.write_bytes(b"n" * 5)
    os.utime(old, (1_000_000, 1_000_000))
    monkeypatch.setattr(server, "_recordings", {"r1": old, "r2": new, "r3": lib / "nope.webm"})
    r = client.get("/api/mobile/recordings").json()["recordings"]
    assert [x["recording_id"] for x in r] == ["r2", "r1"]
    assert r[0] == {"recording_id": "r2", "url": "/api/recordings/r2", "bytes": 5,
                    "modified": new.stat().st_mtime, "ext": ".wav"}


def test_recording_upload_keeps_wav(client, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "RECORDINGS_CACHE_DIR", tmp_path)
    monkeypatch.setattr(server, "_recordings", {})
    r = client.post("/api/recordings", files={"file": ("mix.wav", b"RIFFfake", "audio/wav")}).json()
    assert server._recordings[r["recording_id"]].suffix == ".wav"


def test_sync_bundle_with_a_broken_part(client, monkeypatch):
    def boom():
        raise RuntimeError("store down")

    parts = {"macros": lambda: {"macros": [{"name": "m"}]}, "studied_sets": lambda: {"sets": []},
             "supermoves": lambda: {"move": "x", "variants": []}, "vetoes": boom,
             "liked": lambda: {"liked": []}, "sessions": lambda: {"sessions": []},
             "samples": lambda: {"samples": []}, "learned_moves": lambda: {"moves": {}}}
    monkeypatch.setattr(mobile_api, "_sync_parts", lambda: parts)
    r = client.get("/api/mobile/sync").json()
    assert r["macros"] == {"macros": [{"name": "m"}]} and r["vetoes"] is None
    assert r["errors"] == {"vetoes": "RuntimeError: store down"}
    assert set(r) == set(parts) | {"errors"}


def test_sync_real_handlers_and_session_cap(client, monkeypatch, tmp_path):
    from app.ui.services import atlas_api, replay_api

    for mod, attr in ((atlas_api, "ATLAS_CACHE_DIR"), (replay_api, "REPLAY_CACHE_DIR"), (server, "CACHE_DIR")):
        monkeypatch.setattr(mod, attr, tmp_path)          # never the real cache
    monkeypatch.setattr(server, "_VETO_MEMO", {})
    monkeypatch.setattr(atlas_api, "_SETS_MEMO", {})
    monkeypatch.setattr(replay_api.history, "sessions", lambda d: [{"session": str(i)} for i in range(40)])
    r = client.get("/api/mobile/sync").json()
    assert len(r["sessions"]["sessions"]) == 30
    for part in ("macros", "studied_sets", "supermoves", "vetoes", "liked", "samples", "learned_moves"):
        assert part in r
    assert set(r["errors"]) <= {"macros", "studied_sets", "supermoves", "vetoes", "liked", "samples", "learned_moves"}


@pytest.mark.parametrize("path", ["/api/mobile/library", "/api/mobile/tracks/aaa1", "/api/mobile/recordings",
                                  "/api/mobile/sync", "/api/mobile/tracks/aaa1/lyrics"])
def test_401_without_token(path):
    server.AUTH_GUARD.limiter.reset()
    try:
        r = TestClient(server.app, auth=False).get(path)
        assert r.status_code == 401
    finally:
        server.AUTH_GUARD.limiter.reset()


def test_401_labels_post():
    server.AUTH_GUARD.limiter.reset()
    try:
        assert TestClient(server.app, auth=False).post("/api/mobile/tracks/aaa1/labels",
                                                       json={"genre": "x"}).status_code == 401
    finally:
        server.AUTH_GUARD.limiter.reset()
