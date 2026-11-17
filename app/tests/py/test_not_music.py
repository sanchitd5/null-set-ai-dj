"""CLEANLIB: not_music detector (title cues, synthetic speech vs music audio), scan, restore, registry, phone list."""
import numpy as np
import pytest

from app.ui.services import not_music as nm

SR = nm.SR


def _music(sec=20, bpm=120):
    t = np.arange(int(SR * sec)) / SR
    beat = 60 / bpm
    kick = np.zeros_like(t)
    for k in np.arange(0, sec, beat):
        i = int(k * SR)
        n = int(0.12 * SR)
        seg = np.arange(min(n, len(t) - i)) / SR
        kick[i:i + len(seg)] += np.sin(2 * np.pi * (60 + 80 * np.exp(-seg * 30)) * seg) * np.exp(-seg * 25)
    chord = sum(np.sin(2 * np.pi * f * t) for f in (220, 277, 330)) * 0.1
    return (kick * 0.8 + chord + np.sin(2 * np.pi * 55 * t) * 0.15).astype(np.float32)


def _speech(sec=20, seed=1):
    rng = np.random.default_rng(seed)
    t = np.arange(int(SR * sec)) / SR
    env = np.zeros_like(t)
    pos = 0.0
    while pos < sec:                                   # words of 0.2-0.5 s with 0.15-0.8 s pauses
        w = rng.uniform(0.2, 0.5)
        i, j = int(pos * SR), int(min(sec, pos + w) * SR)
        env[i:j] = 0.5 + 0.5 * np.abs(np.sin(2 * np.pi * rng.uniform(3, 6) * t[i:j]))
        pos += w + rng.uniform(0.15, 0.8)
    carrier = sum(np.sin(2 * np.pi * f * t) for f in (140, 700, 1400, 2400)) * 0.1 + rng.normal(0, 0.01, len(t))
    return (env * carrier).astype(np.float32)


def test_feats_speech_vs_music():
    fm, fs = nm.feats(_music()), nm.feats(_speech())
    assert fs["pause"] > fm["pause"] + 0.05 and fm["beat"] > fs["beat"]
    assert nm.speech_from_feats(fs) > 0.7 > 0.3 > nm.speech_from_feats(fm)


def test_stem_ratio_speech_has_no_instrumental(tmp_path):
    clips = {"vocals": _speech(), "bass": _speech() * 0.001, "drums": _speech() * 0.001, "other": _speech() * 0.001}
    pcm = lambda p, s, d: clips[p]
    assert nm.stem_instrumental({k: k for k in clips}, 100, pcm) < 0.01
    clips = {"vocals": _speech() * 0.3, "bass": _music(), "drums": _music(), "other": _music()}
    assert nm.stem_instrumental({k: k for k in clips}, 100, pcm) > 0.8


def test_title_cues():
    assert nm.title_cues("Artist - Song Name")[0] == 0
    assert nm.title_cues("Badshah ने Yo Yo Honey Singh के हिट गाने Brown Rang को लिखने का दावा कर अंदर की क्या बात बताई ？ GITN")[0] >= 0.7
    assert nm.title_cues("Rapper Exclusive Interview | Podcast Ep 4")[0] >= 0.65
    assert nm.title_cues("Song", dur=15 * 60)[0] > 0
    assert nm.title_cues("तेरे बिना")[0] == 0                # a short Hindi song title is not talk


def test_judge_needs_audio_and_protects():
    talk = {"score": 0.97, "why": ["no steady beat"], "instr": 0.0}
    song = {"score": 0.0, "why": [], "instr": 0.7}
    assert nm.judge("Podcast interview with someone", None, None)["action"] != "remove"     # title alone never removes
    assert nm.judge("Podcast interview with someone", 300, talk)["action"] == "remove"
    assert nm.judge("Bad News", 200, song)["action"] == "keep"
    assert nm.judge("Plain Title", 200, talk)["action"] == "review"                          # audio alone: review only
    assert nm.judge("Podcast interview with someone", 300, talk, protected=True)["action"] == "review"
    real = {"score": 0.9, "why": [], "instr": 0.5}
    assert nm.judge("Interview podcast reaction", 300, real)["action"] == "keep"           # real instrumental in the stems


def _lib(tmp_path, names):
    tracks = {}
    for tid in names:
        p = tmp_path / f"{tid}.wav"
        p.write_bytes(b"x")
        tracks[tid] = p
    return tracks


@pytest.fixture(autouse=True)
def _nodur(monkeypatch):
    monkeypatch.setattr(nm, "_duration", lambda p: 240.0)
    monkeypatch.setattr(nm, "protected_ids", lambda cache, names: set())


def test_scan_remove_review_restore(tmp_path):
    names = {"talk": "Rapper Exclusive Interview | Podcast", "song": "Artist - Song", "odd": "Plain Title"}
    tracks = _lib(tmp_path, names)
    audio = {"talk": {"score": 0.95, "why": ["x"], "instr": 0.0}, "song": {"score": 0.0, "why": [], "instr": 0.8},
             "odd": {"score": 0.9, "why": ["no steady beat"], "instr": None}}
    r = nm.scan(tracks, names, tmp_path, 1000.0, audio_fn=lambda tid, p: audio[tid])
    assert (r["judged"], r["removed"], r["review"]) == (3, 1, 1)
    assert nm.removed_ids(tmp_path) == ["talk"]
    assert nm.apply_removed(tracks, tmp_path) == 1 and "talk" not in tracks and "song" in tracks
    assert nm.scan(tracks, names, tmp_path, 2000.0, audio_fn=lambda *a: 1 / 0)["judged"] == 0      # judged songs are skipped
    row = nm.restore(tracks, "talk", tmp_path)
    assert row["status"] == "restored" and nm.removed_ids(tmp_path) == []
    nm.scan({"talk": tmp_path / "talk.wav"}, names, tmp_path, 3000.0, audio_fn=lambda *a: audio["talk"], redo=True)
    assert nm.load(tmp_path)["items"]["talk"]["status"] == "restored"                        # a restore is a whitelist
    assert nm.remove(tracks, "odd", tmp_path)["status"] == "removed" and nm.removed_ids(tmp_path) == ["odd"]


def test_check_download(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    talk = lambda path: {"score": 0.95, "why": ["no steady beat"], "instr": None}
    assert "not music" in nm.check_download(p, "Star Interview Podcast", 300, audio_fn=talk)
    assert nm.check_download(p, "Artist - Song", 300, audio_fn=lambda path: {"score": 0.0, "why": [], "instr": None}) is None
    assert nm.check_download(p, "Artist - Song", 300, audio_fn=talk) is None                  # audio alone: scan reviews it


def test_routes_and_bad_ids(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from app.ui import server
    monkeypatch.setattr("app.music_brain.config.CACHE_DIR", tmp_path)
    nm._save(tmp_path, {"items": {"talk": {"title": "T", "score": 0.9, "reasons": ["r"], "at": 1, "status": "removed"}}, "checked": {}})
    j = nm.get_not_music()
    assert j["removed"] == 1 and j["items"][0]["title"] == "T" and j["items"][0]["reason"] == "r"
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    assert "talk" in server.get_library_bad()["ids"]

    class Req:
        headers = {}
    monkeypatch.setattr(server, "_tracks", {})
    assert nm.post_not_music_restore("talk", Req())["item"]["status"] == "restored"
    assert "talk" not in server.get_library_bad()["ids"]
    with pytest.raises(HTTPException) as e:
        nm.post_not_music_restore("nope", Req())
    assert e.value.status_code == 404


def test_real_audio_through_ffmpeg(tmp_path):
    import shutil
    import wave
    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg")
    for name, x in (("m", _music(60)), ("s", _speech(60))):
        with wave.open(str(tmp_path / f"{name}.wav"), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR)
            w.writeframes((x * 20000).astype("<i2").tobytes())
    a_m = nm.audio_cues(tmp_path / "m.wav", None, 60.0)
    a_s = nm.audio_cues(tmp_path / "s.wav", None, 60.0)
    assert a_s["score"] > 0.7 > 0.3 > a_m["score"]
