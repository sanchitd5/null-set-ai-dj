"""learn-set on long sets: parts at tracklist boundaries, a checkpoint per part, and the cleanup
that registers every good song in the library before deleting anything."""
import os
import shutil
import time
from pathlib import Path

import pytest

from app.music_brain import learn_cleanup, set_learner as sl
from app.music_brain.set_import import content_id
from app.tests.py.test_set_learner import _stub_study


# ---------------------------------------------------------------- split plan

def _check_partition(parts, n, starts=None):
    starts = starts or list(range(n))
    owned = sorted(i for p in parts for i in p["own"])
    assert owned == list(range(n))                        # every entry owned exactly once
    for a, b in zip(parts, parts[1:]):
        assert starts[a["hi"]] == starts[b["lo"]]         # one shared slot per boundary (all its layers)
        assert a["t1"] == b["t0"]                         # no gap, no overlap in kept time
        assert b["lo"] not in b["own"]                    # its handover belongs to the earlier part


def test_plan_parts_cuts_at_boundaries_with_one_song_overlap():
    starts = [i * 600.0 for i in range(24)]               # 4 h, a song every 10 min
    parts = sl.plan_parts(starts, 4 * 3600.0, 3600.0)
    assert len(parts) == 4
    _check_partition(parts, 24, starts)
    for p in parts:                                       # a part boundary is midway into a song, never a transition
        if p["t0"]:
            assert p["t0"] - 300.0 in starts


def test_plan_parts_layered_slot_never_split():
    starts = [0.0, 600.0, 1200.0, 1200.0, 1800.0, 2400.0, 3000.0, 3600.0]   # "A x B" at 1200
    parts = sl.plan_parts(starts, 4000.0, 1200.0)
    _check_partition(parts, len(starts), starts)
    assert parts[0]["hi"] == 3 and parts[1]["lo"] == 2     # the shared slot carries both layers
    for p in parts:
        assert not (2 in p["own"]) ^ (3 in p["own"])


@pytest.mark.parametrize("starts,duration,split_s", [
    ([0.0, 300.0, 600.0], 800.0, 120.0),               # under WHOLE_UNDER_S: one clip anyway
    ([0.0, 3000.0], 7200.0, 600.0),                       # two slots: nothing to split
    ([i * 600.0 for i in range(12)], 7200.0, 0.0),        # 0 = never split
    ([i * 600.0 for i in range(12)], 7200.0, 9000.0),     # shorter than the threshold
])
def test_plan_parts_short_or_disabled_is_one_part(starts, duration, split_s):
    parts = sl.plan_parts(starts, duration, split_s)
    assert parts == [{"lo": 0, "hi": len(starts) - 1, "own": list(range(len(starts))),
                      "t0": 0.0, "t1": duration}]


# ---------------------------------------------------------------- split learn, stubbed

TRACKS3 = "0:00 A - One\n1:10 B - Two\n2:20 C - Three"


def _three_song_set(tmp_path, monkeypatch):
    """A synthetic 200 s set of three distinct songs, two bass-swap handovers (A->B at 70 s,
    B->C at 140 s), Demucs, analysis, lyrics and YouTube stubbed like _stub_study."""
    import types
    import numpy as np
    import soundfile as sf
    from app.music_brain import analyzer, lyrics, stem_service
    from app.tests.py.test_set_learner import SR, _place, _song

    st = {k: _song(t, 0.0, seed, seconds=200)[0] for k, t, seed in (("a", "A", 1), ("b", "B", 2), ("c", "C", 3))}
    mix = {n: np.zeros(int(200 * SR)) for n in sl.STEMS}
    for k, start, nxt in (("a", 0, 70), ("b", 70, 140), ("c", 140, 200)):
        into = max(0, start - 10)                        # drums / top enter 10 s before the swap
        for n in ("drums", "other", "vocals"):
            _place(mix[n], st[k][n], into, 0, nxt + 10 - into)
        _place(mix["bass"], st[k]["bass"], start, start - into, nxt - start)

    def write(stems, d):
        d.mkdir(parents=True, exist_ok=True)
        out = {}
        for n, y in stems.items():
            sf.write(d / f"{n}.wav", y, SR)
            out[n] = str(d / f"{n}.wav")
        return out
    set_path = tmp_path / "set.wav"
    sf.write(set_path, sum(mix.values()), SR)
    by_path = {}
    for k in st:
        sf.write(tmp_path / f"{k}.wav", np.zeros(SR) + ord(k) * 1e-4, SR)
        by_path[str(tmp_path / f"{k}.wav")] = write(st[k], tmp_path / f"s{k}")

    def fake_separate(path, **kw):
        path = str(path)
        if path in by_path:
            return types.SimpleNamespace(stems=by_path[path])
        t0, t1 = (float(x) for x in Path(path).stem.split("-"))
        return types.SimpleNamespace(stems=write({n: y[int(t0 * SR):int(t1 * SR)] for n, y in mix.items()},
                                                 tmp_path / f"clip{t0:.0f}"))
    monkeypatch.setattr(stem_service, "separate", fake_separate)
    monkeypatch.setattr(analyzer, "analyze", lambda p: types.SimpleNamespace(bpm=124.0, key=None))
    monkeypatch.setattr(lyrics, "fetch", lambda title, **kw: [])
    monkeypatch.setattr(sl, "SETS_DIR", tmp_path / "sets")
    monkeypatch.setattr(sl, "fetch_set", lambda src: (set_path, "e2e", ""))
    monkeypatch.setattr(sl, "find_or_fetch_song", lambda title, d, download=True, **kw: tmp_path / f"{title[0].lower()}.wav")
    monkeypatch.setattr(sl, "WHOLE_UNDER_S", 0)


def _kinds(rep):
    """The handovers learned. Per-song moves (vocal chops etc.) are compared by
    _no_double below: a part's clip spans a different stretch of the set, so its tempo fit
    and a chance match on synthetic clicks can differ from the unsplit run's."""
    return sorted((o["kind"], o["track_a"], o["track_b"]) for o in rep["observations"]
                  if o["kind"] in sl.TRANSITION_KINDS)


def _no_double(rep):
    keys = [(o["kind"], o["track_a"], round(o["at"], 2)) for o in rep["observations"]]
    assert len(keys) == len(set(keys))


def test_split_learn_equals_unsplit(tmp_path, monkeypatch):
    _three_song_set(tmp_path, monkeypatch)
    whole = sl.learn_set("x", tracklist=TRACKS3, store_path=tmp_path / "w.json", ai=False,
                         split_minutes=0, keep_files=True)
    split = sl.learn_set("x", tracklist=TRACKS3, store_path=tmp_path / "s.json", ai=False,
                         split_minutes=1, keep_files=True)
    assert "parts" not in whole and len(split["parts"]) == 2
    assert _kinds(split) == _kinds(whole)                 # no transition dropped or counted twice
    pairs = {(o["track_a"], o["track_b"]) for o in whole["observations"] if o["kind"] in sl.TRANSITION_KINDS}
    assert pairs == {("A - One", "B - Two"), ("B - Two", "C - Three")}, whole["observations"]
    _no_double(split)
    assert [t["title"] for t in split["tracks"]] == [t["title"] for t in whole["tracks"]]
    assert not any(t["likely_wrong_song"] for t in split["tracks"])
    import json
    timeline = json.loads(Path(split["study_path"]).read_text())["timeline"]
    ts = [r["t"] for r in timeline if r["stem"] == "bass"]
    assert ts and {o["track"] for r in timeline for o in r["owners"]} == {0, 1, 2}   # global song indices
    assert len(ts) == len(set(ts))                         # no set window counted by both parts
    assert set(split) - {"parts"} == set(whole)            # same result shape


def test_killed_between_parts_resumes_at_next_part(tmp_path, monkeypatch):
    _three_song_set(tmp_path, monkeypatch)
    real, calls = sl._study_part, []

    def dies_on_part_2(*a, **kw):
        calls.append(a[3]["lo"])
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(*a, **kw)

    monkeypatch.setattr(sl, "_study_part", dies_on_part_2)
    with pytest.raises(KeyboardInterrupt):
        sl.learn_set("x", tracklist=TRACKS3, store_path=tmp_path / "l.json", ai=False,
                     split_minutes=1, keep_files=True)
    assert (sl.SETS_DIR / "e2e" / "parts" / "part-0.json").is_file()
    calls.clear()
    monkeypatch.setattr(sl, "_study_part", lambda *a, **kw: (calls.append(a[3]["lo"]), real(*a, **kw))[1])
    rep = sl.learn_set("x", tracklist=TRACKS3, store_path=tmp_path / "l.json", ai=False,
                       split_minutes=1, keep_files=True)
    assert calls == [1]                                   # part 1 came from its checkpoint
    assert not (sl.SETS_DIR / "e2e" / "parts").exists()   # study.json written: checkpoints gone
    unsplit = sl.learn_set("x", tracklist=TRACKS3, store_path=tmp_path / "u.json", ai=False,
                           split_minutes=0, keep_files=True)
    assert _kinds(rep) == _kinds(unsplit)


def test_changed_tracklist_discards_old_checkpoint(tmp_path):
    logs = []
    ck = sl._Checkpoint(tmp_path, "k1", logs.append)
    ck.save(0, {"a": 1})
    assert sl._Checkpoint(tmp_path, "k1", logs.append).load(0) == {"a": 1}
    assert sl._Checkpoint(tmp_path, "k2", logs.append).load(0) is None
    assert any("starting over" in m for m in logs)


# ---------------------------------------------------------------- cleanup

def _tidy(tmp_path, sid="S1"):
    sets = tmp_path / "sets"
    stems = tmp_path / "stems"
    (sets / sid / "songs").mkdir(parents=True)
    (sets / sid / "clips").mkdir(parents=True)
    stems.mkdir()
    return learn_cleanup.Tidy(sid, sl.CACHE_DIR, sets, stems)


def _file(p: Path, data: bytes) -> str:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return str(p)


def test_cleanup_registers_before_it_deletes(tmp_path):
    t = _tidy(tmp_path)
    good = _file(t.songs_dir / "A - One.mp3", b"good song")
    wrong = _file(t.songs_dir / "B - Two.mp3", b"wrong song")
    stray = _file(t.songs_dir / "C - Three.mp3", b"never listed")
    part = _file(t.songs_dir / "A - One.f251.webm.part", b"x")
    clip = _file(t.clips_dir / "0.00-80.00.wav", b"clip")
    stem_dir = t.stems_dir / "abc_htdemucs"
    _file(stem_dir / "vocals.wav", b"v")
    moved = t.register([{"title": "A - One", "start": 0.0, "path": good},
                        {"title": "B - Two", "start": 30.0, "path": wrong, "likely_wrong_song": True,
                         "heard_share": 0.1}])
    uploads = sl.CACHE_DIR / "uploads" / f"{content_id(Path(good))}.mp3"
    assert moved == {good: str(uploads)} and uploads.is_file()
    t.sweep(moved, clip_files=[clip], stem_dirs=[str(stem_dir)])
    t.keep_unlisted()
    out = t.result()
    assert not Path(good).exists() and not Path(part).exists()
    assert not Path(clip).exists() and not stem_dir.exists()
    assert Path(wrong).exists() and Path(stray).exists()      # not in the library: never deleted
    kept = {Path(k["file"]).name: k["reason"] for k in out["kept"]}
    assert "probably the wrong download" in kept["B - Two.mp3"]
    assert kept["C - Three.mp3"] == "not in the library"
    assert out["deleted"] == {"songs": 1, "leftovers": 1, "clips": 1, "clip_stems": 1}
    assert out["freed_bytes"] > 0


def test_cleanup_deletes_flac_clips_and_their_stems(tmp_path):
    t = _tidy(tmp_path)
    clip = _file(t.clips_dir / "0.00-80.00.flac", b"clip")
    stem_dir = t.stems_dir / "abc_htdemucs"
    _file(stem_dir / "vocals.flac", b"v")
    t.sweep({}, clip_files=[clip], stem_dirs=[str(stem_dir)])
    assert not Path(clip).exists() and not stem_dir.exists()
    assert t.result()["deleted"]["clips"] == 1 and t.result()["deleted"]["clip_stems"] == 1


def test_recording_deleted_only_after_every_id_is_cut(tmp_path, monkeypatch):
    t = _tidy(tmp_path)
    rec = Path(_file(t.sets_dir / "S1.mp3", b"the set"))
    tracks = [{"title": "ID - ID", "start": 0.0, "path": None}, {"title": "ID", "start": 300.0, "path": None}]
    monkeypatch.setattr(learn_cleanup, "register_rows",
                        lambda rows: [dict(r, error="ffmpeg failed") if r["action"] == "cut" else r for r in rows])
    t.register(tracks, set_audio=rec)
    t.sweep({}, set_path=rec)
    assert rec.exists() and "ID slot" in t.kept[str(rec)]
    order = []
    monkeypatch.setattr(learn_cleanup, "register_rows",
                        lambda rows: [order.append(("cut", r["title"])) or r for r in rows])
    real_rm = t._rm
    monkeypatch.setattr(t, "_rm", lambda p, kind: (order.append(("rm", kind)), real_rm(p, kind))[1])
    t.register(tracks, set_audio=rec)
    t.sweep({}, set_path=rec)
    assert not rec.exists()
    assert order.index(("rm", "set_recording")) > max(i for i, o in enumerate(order) if o[0] == "cut")


def test_users_own_recording_is_never_deleted(tmp_path, monkeypatch):
    t = _tidy(tmp_path)
    own = Path(_file(tmp_path / "Desktop" / "my set.mp3", b"mine"))
    monkeypatch.setattr(learn_cleanup, "register_rows", lambda rows: rows)
    t.register([{"title": "ID", "start": 0.0, "path": None}], set_audio=own)
    t.sweep({}, set_path=own)
    assert own.exists()


def test_concurrent_learn_of_the_set_skips_cleanup(tmp_path, monkeypatch):
    from app.music_brain import learn_progress as lp
    t = _tidy(tmp_path)
    p = lp.Progress("x", heartbeat_s=0)
    p.set_id("S1")
    p.doc["pid"] = os.getppid()                           # alive, not us
    p._flush(True)
    assert "another learn" in (t.busy() or "")
    p.doc["pid"] = os.getpid()
    p._flush(True)
    assert t.busy() is None


def test_keep_files_deletes_nothing(tmp_path, monkeypatch):
    _stub_study(tmp_path, monkeypatch)
    songs = sl.SETS_DIR / "e2e" / "songs"
    f = Path(_file(songs / "A - One.mp3", b"song"))
    monkeypatch.setattr(sl, "find_or_fetch_song",
                        lambda title, d, download=True, **kw: f if title.startswith("A") else tmp_path / "b.wav")
    rep = sl.learn_set("x", tracklist="0:00 A - One\n0:36 B - Two", store_path=tmp_path / "l.json",
                       ai=False, keep_files=True)
    assert "cleanup" not in rep and f.exists() and (tmp_path / "set.wav").exists()


def test_default_learn_cleans_up_and_repoints_study(tmp_path, monkeypatch):
    import json
    _stub_study(tmp_path, monkeypatch)
    songs = sl.SETS_DIR / "e2e" / "songs"
    songs.mkdir(parents=True, exist_ok=True)
    a = songs / "A - One.wav"
    shutil.copy(tmp_path / "a.wav", a)
    monkeypatch.setattr(sl, "find_or_fetch_song",
                        lambda title, d, download=True, **kw: a if title.startswith("A") else tmp_path / "b.wav")
    rep = sl.learn_set("x", tracklist="0:00 A - One\n0:36 B - Two", store_path=tmp_path / "l.json", ai=False)
    assert rep["cleanup"]["deleted"].get("songs") == 1 and not a.exists()
    lib = sl.CACHE_DIR / "uploads" / f"{content_id(tmp_path / 'a.wav')}.wav"
    assert lib.is_file()
    study = json.loads(Path(rep["study_path"]).read_text())
    assert study["tracks"][0]["path"] == str(lib)          # import-set finds the library copy
    assert not list((sl.SETS_DIR / "e2e" / "clips").glob("*"))
    assert (tmp_path / "set.wav").exists()                 # outside the sets dir: the user's file
