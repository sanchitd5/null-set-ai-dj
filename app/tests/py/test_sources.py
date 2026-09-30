"""sources: source-recording lookup and learn_transform clip handling (no network, no Demucs)."""
import json

import pytest

from app.music_brain import set_learner as sl
from app.music_brain import sources


def _source(root, sid, song, info="{}"):
    d = root / sid
    d.mkdir(parents=True)
    (d / "info.json").write_text(info, encoding="utf-8")
    (d / "captions.json").write_text(json.dumps({"words": [], "phrases": []}), encoding="utf-8")
    (d / "links.json").write_text(json.dumps([{"song": song, "note": "n"}]), encoding="utf-8")
    return d


def test_source_of_survives_a_corrupt_info_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "SOURCES_DIR", tmp_path)
    _source(tmp_path, "aqu4ezLQEUA", "Fred again.. - Sabrina", info="{truncated")
    got = sources.source_of("Fred again.. - Sabrina")
    assert got["id"] == "aqu4ezLQEUA" and got["note"] == "n"
    assert sources.source_of("Nobody - Nothing") is None


def test_each_target_gets_its_own_clips(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "SOURCES_DIR", tmp_path)
    _source(tmp_path, "aqu4ezLQEUA", "x")
    dirs = []

    def fake_clip(src, t0, t1, out_dir):
        dirs.append(out_dir)
        raise RuntimeError("stop here")       # the rest needs Demucs
    monkeypatch.setattr(sl, "clip_audio", fake_clip)
    for name in ("a.wav", "b.wav"):
        (tmp_path / name).write_bytes(b"x")
        with pytest.raises(RuntimeError):
            sources.learn_transform("aqu4ezLQEUA", str(tmp_path / name), start=10.0, end=20.0)
    assert len(dirs) == 2 and dirs[0] != dirs[1]
    assert all(d.parent == tmp_path / "aqu4ezLQEUA" / "clips" for d in dirs)


def test_caption_words_always_last_a_moment():
    caps = sources.parse_json3({"events": [
        {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "I"}, {"utf8": " am", "tOffsetMs": 400}]},
        {"tStartMs": 1500, "segs": [{"utf8": "\n"}]},                        # no segs worth keeping
        {"tStartMs": 5000, "segs": [{"utf8": "a"}, {"utf8": " party", "tOffsetMs": 600}]}]})   # no duration
    assert [(w["w"], w["t"]) for w in caps["words"]] == [("I", 1.0), ("am", 1.4), ("a", 5.0), ("party", 5.6)]
    assert all(w["end"] > w["t"] for w in caps["words"])
    assert sources.words_between(caps, 5.7, 6.5) == "party"          # was "": ended at 5.0, before it began


def test_semitones_survive_a_pyin_failure(monkeypatch):
    import librosa
    import numpy as np

    def boom(*a, **k):
        raise librosa.util.exceptions.ParameterError("audio buffer is not finite everywhere")
    monkeypatch.setattr(librosa, "pyin", boom)
    assert sources._semitones(np.zeros(4096), np.zeros(4096), 11025) is None


def test_a_re_exported_set_is_cut_again(tmp_path):
    import os
    import shutil
    import numpy as np
    import soundfile as sf
    if not shutil.which("ffmpeg"):
        return
    src = tmp_path / "mix.wav"
    sf.write(src, np.full(44100 * 2, 0.1), 44100)
    first = sl.clip_audio(src, 0.0, 1.0, tmp_path / "clips")
    os.utime(first, (1_000_000, 1_000_000))                       # the cut is older than...
    sf.write(src, np.full(44100 * 2, -0.5), 44100)                 # ...the mix, re-exported in place
    again = sl.clip_audio(src, 0.0, 1.0, tmp_path / "clips")
    assert again == first and float(sf.read(again)[0].mean()) < -0.2           # the new audio, not +0.1


def test_clip_audio_refuses_an_empty_or_inverted_span(tmp_path):
    for t0, t1 in ((5.0, 5.0), (9.0, 2.0), (-1.0, 3.0)):
        with pytest.raises(ValueError):
            sl.clip_audio(tmp_path / "missing.wav", t0, t1, tmp_path / "clips")
