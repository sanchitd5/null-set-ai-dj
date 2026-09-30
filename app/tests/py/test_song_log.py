"""Per-song AI step log (app/ui/services/song_log.py) + waveform (app/ui/services/song_waveform.py)."""
import json

import numpy as np
import pytest

from app.ui.services import session_log, song_log, song_waveform


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(session_log, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session_log, "SESSION_ID", "2026-09-29_120000")
    monkeypatch.setattr(song_log, "WAVEFORM_CACHE", tmp_path / "wf")
    monkeypatch.setattr(song_log, "render_async", lambda d: None)  # tests render synchronously
    song_log.reset()
    song_log._resolve.update({k: None for k in song_log._resolve})
    song_log.configure(name=lambda tid: f"Artist - {tid}")
    yield
    song_log.reset()


def test_steps_before_during_after_a_song():
    song_log.step("match", "T1", decision="Bass Swap")               # selection, before it plays
    song_log.step("song_start", "T1", deck="a", at_song=0)
    song_log.step("stem-move", deck="a", decision="drums out")      # deck-only: goes to T1
    song_log.step("song_end", "T1", deck="a", at_song=200)
    song_log.step("stem-move", deck="a", decision="late")           # after: no song on deck a
    song_log.step("match", "T1", decision="again")                  # played twice: a new record
    got = song_log.songs()
    assert [s["nn"] for s in got] == [1, 2]
    one = song_log.song(None, 1)
    kinds = [s["kind"] for s in one["steps"]]
    assert kinds == ["match", "song_start", "stem-move", "song_end"]
    assert one["steps"][0]["phase"] == "selection"
    assert one["steps"][2]["phase"] == "playing"
    assert one["meta"]["name"] == "Artist - T1" and one["meta"]["exit_song_s"] == 200
    assert song_log.song(None, 2)["steps"][0]["decision"] == "again"


def test_ingest_bounds_and_bad_input():
    n = song_log.ingest([{"kind": "x", "track_id": "T"}, "junk", {"kind": ""}, {"no": 1},
                         {"kind": "y", "track_id": "T", "t": "bad", "at_song": "bad", "why": "w" * 999}])
    assert n == 2
    st = song_log.song(None, 1)["steps"]
    assert len(st[1]["why"]) <= 300 and st[1]["at_song"] is None
    with pytest.raises(ValueError):
        song_log.songs("../etc")


def test_steps_capped(monkeypatch):
    monkeypatch.setattr(song_log, "MAX_STEPS", 3)
    for i in range(5):
        song_log.step("x", "T", decision=i)
    s = song_log.songs()[0]
    assert s["steps"] == 3
    assert json.loads((song_log.song_dir(None, 1) / "meta.json").read_text())["dropped"] >= 1


def _wav(path, sr=11025, secs=4.0):
    import soundfile as sf
    t = np.arange(int(sr * secs)) / sr
    sf.write(str(path), 0.5 * np.sin(2 * np.pi * 220 * t), sr)
    return path


def test_waveform_shape_png_and_cache(tmp_path, monkeypatch):
    pytest.importorskip("soundfile")
    pytest.importorskip("matplotlib")
    mix = _wav(tmp_path / "mix.wav")
    stems = {"drums": str(_wav(tmp_path / "d.wav")), "vocals": str(_wav(tmp_path / "v.wav"))}
    ana = {"bpm": 120.0, "key": {"camelot": "8A"}, "phrase_boundaries_8bar": [0, 2],
           "sections": [{"label": "intro", "start": 0, "end": 2}], "energy_times": [0, 1, 2],
           "energy_curve": [0.1, 0.5, 0.2], "vocal_active_regions": [[1, 2]]}
    song_log.configure(path=lambda tid: mix, analysis=lambda p: ana, stems=lambda tid: stems)
    song_log.step("song_start", "T", deck="a", at_song=0)
    song_log.step("cue_drop", deck="a", at_song=1.5, decision="drop")
    d = song_log.song_dir(None, 1)
    png = song_log.render_song(d)
    wf = json.loads((d / "waveform.json").read_text())
    assert set(wf["lanes"]) == {"mix", "drums", "vocals"}
    assert wf["key"] == "8A" and wf["bins"] == len(wf["lanes"]["mix"]["peak"])
    assert (d / "waveform.json").stat().st_size < 300_000
    assert png.exists() and png.stat().st_size > 5000
    calls = []
    real = song_waveform.compute
    monkeypatch.setattr(song_waveform, "compute", lambda *a, **k: calls.append(1) or real(*a, **k))
    song_log.render_song(d)
    assert calls == []  # cached per file hash


def test_cli_session_report(capsys):
    from app.music_brain import agent_bridge
    session_log.log("track", event="transition_start")  # the CLI reports the newest logged session
    song_log.step("match", "T1", decision="Echo Out")
    assert agent_bridge.main(["session-report"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["session"] == "2026-09-29_120000" and out["songs"][0]["steps"] == 1


def test_steps_endpoint_bounds():
    from app.tests.py.testclient_compat import TestClient
    from app.ui import server
    c = TestClient(server.app)
    r = c.post("/api/session/steps", json={"steps": [{"kind": "match", "track_id": "T1"}, 5]})
    assert r.status_code == 200 and r.json()["accepted"] == 1
    assert c.post("/api/session/steps", json={"steps": [{"kind": "x"}] * 201}).status_code == 413
    assert c.post("/api/session/steps", json={"steps": "nope"}).status_code == 422
    assert c.get("/api/session/songs").json()["songs"][0]["track_id"] == "T1"
    assert c.get("/api/session/songs/2026-09-29_120000/9").status_code == 404
    assert c.get("/api/session/songs/bad!id/1").status_code == 400
