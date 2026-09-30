"""Song merge: combos, ranking rules, offline clip, silent-ear rating."""
import json

import numpy as np
import soundfile as sf

from app.music_brain.render import merge

FULL = {"drums": 0.2, "bass": 0.2, "vocals": 0.1, "other": 0.1}


def test_combos_split_every_role_between_the_decks():
    cs = merge.combos()
    assert len(cs) == 14 and {"drums": "a", "bass": "a", "vocals": "b", "other": "b"} in cs
    assert all(len(set(c.values())) == 2 for c in cs)                     # never all-A, never all-B


def test_rank_prefers_a_locked_groove_under_b_song_and_obeys_keys():
    r = merge.rank(FULL, FULL, key_score=1.0)
    assert r[0]["combo"] == {"drums": "a", "bass": "a", "vocals": "b", "other": "b"}
    clash = merge.rank(FULL, FULL, key_score=0.3)
    for x in clash:                                                         # tonal layers from one deck only
        assert len({x["combo"][t] for t in merge.TONAL}) == 1
    rap = merge.rank(FULL, FULL, key_score=0.3, b_rap=True)
    assert any(x["combo"]["vocals"] == "b" and x["combo"]["other"] == "a" for x in rap)   # a rap ignores key


def test_rank_drops_combos_whose_stems_are_silent():
    no_b_vox = dict(FULL, vocals=0.0)
    assert all(x["combo"]["vocals"] == "a" for x in merge.rank(FULL, no_b_vox, key_score=1.0))


def test_render_clip_and_ear(tmp_path):
    sr = 16000
    def stems(name, f):
        out = {}
        for i, r in enumerate(merge.ROLES):
            p = tmp_path / f"{name}_{r}.wav"
            t = np.arange(int(40 * sr)) / sr
            sf.write(p, 0.2 * np.sin(2 * np.pi * (f + 40 * i) * t), sr)
            out[r] = str(p)
        return out
    sa, sb = stems("a", 110), stems("b", 220)
    c = {"drums": "a", "bass": "a", "vocals": "b", "other": "b"}
    y = merge.render_clip(sa, sb, c, 1.0, 2.0, 124.0, 120.0, bars=4)
    assert abs(len(y) - int(4 * 240 / 124 * sr)) <= 1 and 0 < np.max(np.abs(y)) <= 0.95 + 1e-6
    good = lambda system, wav, text: 'sure {"score": 8, "why": "kick and bass lock"}'
    assert merge.ear_rate(merge.wav_bytes(y), merge.label(c), good) == {"score": 8.0, "why": "kick and bass lock"}
    assert merge.ear_rate(b"x", "x", lambda *a: "no json") is None
    assert merge.ear_rate(b"x", "x", lambda *a: '{"score": 42}') is None
    calls = []
    ask = lambda s, w, t: calls.append(t) or '{"score": 7, "why": "ok"}'
    res = merge.audition(sa, sb, [c], 1.0, 2.0, 124.0, 120.0, "k", ask=ask, cache_dir=tmp_path / "c")
    again = merge.audition(sa, sb, [c], 1.0, 2.0, 124.0, 120.0, "k", ask=ask, cache_dir=tmp_path / "c")
    assert res == again and len(calls) == 1 and res[0]["ear"]["score"] == 7.0      # heard once, cached
