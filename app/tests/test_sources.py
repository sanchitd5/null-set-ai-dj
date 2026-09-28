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


def test_clip_audio_refuses_an_empty_or_inverted_span(tmp_path):
    for t0, t1 in ((5.0, 5.0), (9.0, 2.0), (-1.0, 3.0)):
        with pytest.raises(ValueError):
            sl.clip_audio(tmp_path / "missing.wav", t0, t1, tmp_path / "clips")
