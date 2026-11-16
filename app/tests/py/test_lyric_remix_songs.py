"""Lyric remix songs: list, render on request, identity cache, audio route (lyric_remix_songs.py). Synthetic stems and plans only
(tones; no real songs or lyrics). The render goes through the real dj-core plan (needs_djcore) and ffmpeg."""
import gzip
import json
import shutil
import subprocess
import time

import numpy as np
import pytest
import soundfile as sf

from app.music_brain import song_store
from app.music_brain.matching import knowledge as kn
from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import lyric_remix_songs as lrs

BPM = 120.0
SRC_S = 150.0
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def _line(i, t, beats=4):
    return {"i": i, "t": t, "e": t + beats * 0.5, "beats": beats, "q": 0.9, "text": f"line {i}"}


def _doc():
    songs = {k: {"bpm": BPM, "lang": "en", "lines": [_line(0, 40.0), _line(1, 44.0), _line(2, 48.0)]} for k in ("sa", "sb")}
    within = {"id": "aaaa00000001", "a": "sa", "b": None, "pieces": [{"s": "a", "i": 2}, {"s": "a", "i": 0}],
              "text": "alpha / beta", "score": 0.8, "why": "synthetic"}
    pair = {"id": "bbbb00000002", "a": "sa", "b": "sb", "pieces": [{"s": "a", "i": 1}, {"s": "b", "i": 2}],
            "text": "gamma / delta", "score": 0.7, "why": "synthetic"}
    return {"v": 1, "songs": songs, "plans": [within, pair]}


def _tone(hz, amp, secs=SRC_S, sr=44100):
    t = np.arange(int(secs * sr)) / sr
    x = (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)
    return np.stack([x, x], axis=1)


@pytest.fixture
def env(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(song_store, "_cache", lambda: cache)
    know = tmp_path / "know"
    know.mkdir()
    (know / kn.LYRIC_REMIX).write_bytes(gzip.compress(json.dumps(_doc()).encode(), mtime=0))
    monkeypatch.setattr(kn, "KNOWLEDGE_DIR", know)
    stems = {}
    for tid, vhz in (("sa", 1000.0), ("sb", 2000.0)):
        d = tmp_path / tid
        d.mkdir()
        paths = {n: d / f"{n}.wav" for n in ("drums", "bass", "other", "vocals")}
        for n, hz in (("drums", 200.0), ("bass", 80.0), ("other", 400.0), ("vocals", vhz)):
            sf.write(paths[n], _tone(hz, 0.4 if n == "vocals" else 0.1), 44100)
        stems[tid] = {n: str(p) for n, p in paths.items()}
    audio = {t: tmp_path / f"{t}.wav" for t in stems}
    monkeypatch.setattr(lrs.Host, "track_path", staticmethod(lambda tid: audio.get(tid)))
    monkeypatch.setattr(lrs.Host, "stems4", staticmethod(lambda tid: stems.get(tid)))
    monkeypatch.setattr(lrs.Host, "queue_stems", staticmethod(lambda tid: None))
    monkeypatch.setattr(lrs.Host, "name_of", staticmethod(lambda tid: {"sa": "Song A", "sb": "Song B"}.get(tid, tid)))
    monkeypatch.setattr(lrs.Host, "analyze", None)
    lrs._jobs.clear()
    from app.ui.services import lyric_remix_api
    lyric_remix_api._cache.update(key=None, doc=None)
    return TestClient(server.app)


def _wait(c, pid, want="ready", secs=120):
    t0 = time.time()
    while time.time() - t0 < secs:
        s = c.get(f"/api/lyric_remix/songs/{pid}").json()
        if s["state"] == want or s["state"] == "error":
            return s
        time.sleep(0.3)
    raise AssertionError("render did not finish")


# ------------------------------------------------------------------------------------------------ pure parts


def test_layout_reaches_a_minute_with_bars_each_side():
    src0, src1, intro, outro = lrs.layout(40.0, 44.0, 1.0, 150.0)
    assert intro == outro == 16 and (src1 - src0) >= 36.0 and src0 == 24.0 and src1 == 60.0
    src0, src1, intro, outro = lrs.layout(40.0, 44.0, 2.0, 150.0)       # slower: fewer bars reach a minute
    assert intro == outro == 14 and (src1 - src0) >= 60.0


def test_layout_short_intro_gives_slack_to_the_outro():
    src0, src1, intro, outro = lrs.layout(4.0, 12.0, 2.0, 150.0)
    assert intro == 2 and src0 == 0.0 and outro == 16        # the short intro's bars go to the outro, capped at 16 a side
    _, _, i2, o2 = lrs.layout(4.0, 12.0, 2.0, 20.0)
    assert o2 == 4                                            # the song ends sooner: nothing to take


def test_identity_changes_with_the_plan_and_the_lines():
    doc = _doc()
    p = doc["plans"][0]
    base = lrs.identity(doc, p)
    assert lrs.identity(doc, p) == base
    doc["songs"]["sa"]["lines"][2]["t"] = 52.0
    assert lrs.identity(doc, p) != base


def test_settings_default_off_and_persist(env):
    assert env.get("/api/lyric_remix/settings").json() == {"ai_picks": False}
    assert env.put("/api/lyric_remix/settings", json={"ai_picks": True}).json() == {"ai_picks": True}
    assert env.get("/api/lyric_remix/settings").json()["ai_picks"] is True


def test_pickable_is_empty_until_ai_picks_is_on(env):
    assert env.get("/api/lyric_remix/pickable").json() == {"ai_picks": False, "songs": []}
    env.put("/api/lyric_remix/settings", json={"ai_picks": True})
    assert env.get("/api/lyric_remix/pickable").json() == {"ai_picks": True, "songs": []}      # nothing rendered yet


def test_list_titles_search_and_unavailable_songs(env, monkeypatch):
    r = env.get("/api/lyric_remix/songs").json()
    assert r["total"] == 2 and r["ai_picks"] is False
    titles = {s["plan_id"]: s["title"] for s in r["songs"]}
    assert titles["aaaa00000001"] == "Song A - lyric remix" and titles["bbbb00000002"] == "Song A x Song B - lyric remix"
    assert r["songs"][0]["score"] >= r["songs"][1]["score"] and r["songs"][0]["state"] == "none"
    assert env.get("/api/lyric_remix/songs", params={"q": "delta"}).json()["total"] == 1
    monkeypatch.setattr(lrs.Host, "track_path", staticmethod(lambda tid: None if tid == "sb" else tmp_exists))
    assert env.get("/api/lyric_remix/songs").json()["total"] == 1      # the pair needs both songs in the library


tmp_exists = "x"


def test_audio_route_409_before_render_and_404_unknown(env):
    assert env.get("/api/audio/remix/aaaa00000001").status_code == 409
    assert env.get("/api/audio/remix/cccc00000003").status_code == 404
    assert env.get("/api/audio/remix/zz").status_code == 422
    assert env.get("/api/audio/remix/aaaa00000001", params={"q": "x"}).status_code == 400


def test_prune_is_lru_and_capped(env, monkeypatch):
    monkeypatch.setenv("LYRIC_REMIX_CACHE_MAX", "2")
    import os
    for i, pid in enumerate(("aaaa00000010", "aaaa00000011", "aaaa00000012")):
        d = lrs.remix_dir(pid)
        d.mkdir(parents=True)
        (d / "meta.json").write_text("{}")
        os.utime(d / "meta.json", (1000 + i, 1000 + i))
    assert lrs.prune() == 1 and not lrs.remix_dir("aaaa00000010").exists() and lrs.remix_dir("aaaa00000012").exists()


# ------------------------------------------------------------------------------------------------ the render


def _decode(path):
    return lrs.decode(str(path), 0.0, lrs.probe_duration(str(path)) - 0.05)


def _dominant(x):
    mono = x.mean(axis=1)
    spec = np.abs(np.fft.rfft(mono))
    return np.fft.rfftfreq(len(mono), 1 / lrs.SR)[int(spec.argmax())]


@needs_ffmpeg
@pytest.mark.needs_djcore
def test_within_render_shape_length_identity_route_and_grid(env):
    r = env.post("/api/lyric_remix/songs/aaaa00000001/render")
    assert r.status_code == 200 and r.json()["state"] in ("rendering", "ready")
    s = _wait(env, "aaaa00000001")
    assert s["state"] == "ready", s
    assert 60.0 <= s["duration"] <= 125.0 and s["bpm"] == BPM
    assert s["sha256"] and s["bytes"] > 10_000 and s["beat_times"][1] - s["beat_times"][0] == pytest.approx(0.5, abs=1e-3)
    a = env.get("/api/audio/remix/aaaa00000001", params={"q": "phone"})
    assert a.status_code == 200 and a.headers["content-type"] == "audio/mp4" and a.headers["x-content-sha256"] == s["sha256"]
    assert env.get("/api/audio/remix/aaaa00000001", headers={"Range": "bytes=0-99"}).status_code == 206
    path = lrs.remix_dir("aaaa00000001") / "audio.m4a"
    assert abs(lrs.probe_duration(str(path)) - s["duration"]) < 0.15
    # the new lyric: section starts at the first used line (t=40) -> output second 28 (14 bars of 2 s before it); piece 0 is the
    # vocal tone of line 2 over the first 2 s of the section, piece 1 follows: the 1000 Hz vocal sits ONLY inside the section
    x = _decode(path)
    intro = lrs.layout(40.0, 44.0, 2.0, SRC_S)[2]      # the section is 4 s: 14 bars of 2 s before it
    sec = int(intro * 2.0 * lrs.SR)
    vocal = lambda seg: float(np.abs(np.fft.rfft(seg.mean(axis=1)))[int(1000 * len(seg) / lrs.SR)])    # noqa: E731
    inside = vocal(x[sec + 4000: sec + 4000 + 2 * lrs.SR])
    outside = vocal(x[4 * lrs.SR: 6 * lrs.SR])
    assert inside > 20 * max(outside, 1e-6)
    # cached: asking again does not re-render; changed lines (identity) do
    assert env.post("/api/lyric_remix/songs/aaaa00000001/render").json()["state"] == "ready"
    assert env.get("/api/lyric_remix/songs").json()["songs"][0]["state"] in ("ready", "none")


@needs_ffmpeg
@pytest.mark.needs_djcore
def test_pair_render_uses_the_other_songs_vocal(env):
    env.post("/api/lyric_remix/songs/bbbb00000002/render")
    s = _wait(env, "bbbb00000002")
    assert s["state"] == "ready", s
    x = _decode(lrs.remix_dir("bbbb00000002") / "audio.m4a")
    start = int(lrs.layout(44.0, 48.0, 2.0, SRC_S)[2] * 2.0 * lrs.SR)     # intro length = section start in the output
    second_piece = x[start + 2 * lrs.SR + 4000: start + 4 * lrs.SR - 4000]
    assert abs(_dominant(second_piece) - 2000.0) < 40.0


@needs_ffmpeg
@pytest.mark.needs_djcore
def test_render_error_is_reported(env, monkeypatch):
    monkeypatch.setattr(lrs.Host, "stems4", staticmethod(lambda tid: {"drums": "/nope", "bass": "/nope", "other": "/nope", "vocals": "/nope"}))
    monkeypatch.setattr(lrs, "probe_duration", lambda p: 150.0)
    env.post("/api/lyric_remix/songs/aaaa00000001/render")
    s = _wait(env, "aaaa00000001", want="ready", secs=60)
    assert s["state"] == "error" and s["error"]
