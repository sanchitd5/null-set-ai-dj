"""G25: per-song folders (resolver, old layout still resolves), lyric check verdicts, migration."""
import json
import os
from pathlib import Path

import pytest

from app.music_brain import song_store as ss
from app.music_brain.analysis import lyric_check as lc
from app.ui.services import cache_audit, lyric_check_job, song_migrate

LYRICS = """the river runs beneath the bridge tonight
we dance until the morning light
hold me close and never let go
the river runs beneath the bridge tonight"""


@pytest.fixture
def cache(tmp_path, monkeypatch):
    import app.music_brain.config as cfg
    monkeypatch.setattr(cfg, "CACHE_DIR", tmp_path)
    return tmp_path


def _stem_set(cache, audio_hash, model, names=("vocals", "drums")):
    d = cache / "stems" / f"{audio_hash}_{model}"
    d.mkdir(parents=True)
    paths = {}
    for n in names:
        p = d / f"{n}.flac"
        p.write_bytes(n.encode() * 50)
        paths[n] = str(p)
    (d / "manifest.json").write_text(json.dumps({"version": 2, "format": "flac", "stems": paths}))
    return d


# ---- resolver
def test_audio_resolves_old_then_new(cache):
    (cache / "uploads").mkdir()
    old = cache / "uploads" / "abc123.mp3"
    old.write_bytes(b"x")
    assert ss.audio_path("abc123") == old
    new = ss.song_dir("abc123") / "audio.flac"
    new.parent.mkdir(parents=True)
    new.write_bytes(b"y")
    assert ss.audio_path("abc123", legacy=old) == new
    assert ss.audio_path("nope") is None


def test_stems_resolve_old_layout_and_folder_wins(cache):
    _stem_set(cache, "H" * 8, "htdemucs_ft")
    got = ss.stem_files("t1", "htdemucs_ft", audio_hash="H" * 8)
    assert set(got) == {"vocals", "drums"} and got["vocals"].parent.name == "H" * 8 + "_htdemucs_ft"
    d = ss.stems_dir("t1", "htdemucs_ft")
    d.mkdir(parents=True)
    (d / "vocals.m4a").write_bytes(b"m")
    (d / "vocals.flac").write_bytes(b"f")
    got = ss.stem_files("t1", "htdemucs_ft", audio_hash="H" * 8)
    assert got["vocals"].suffix == ".m4a" and got["drums"].parent.name.endswith("htdemucs_ft") and got["drums"].suffix == ".flac"
    assert ss.vocal_stem("t1", audio_hash="H" * 8) == d / "vocals.m4a"


def test_bad_track_id_refused(cache):
    for bad in ("../x", "a/b", "", "a.b"):
        with pytest.raises(ValueError):
            ss.song_dir(bad)


def test_meta_roundtrip_and_verdict(cache):
    assert ss.verdict("t1") is None
    ss.update_meta("t1", verdict="verified", score=0.7, title="x")
    ss.update_meta("t1", score=0.8, title=None)
    m = ss.read_meta("t1")
    assert m["verdict"] == "verified" and m["score"] == 0.8 and "title" not in m and "updated" in m
    assert ss.verdict("t1") == {"verdict": "verified", "score": 0.8}


# ---- lyric compare
def test_judge_match_with_misheard_words():
    heard = "the river runs beneath the bridge tonite\nwe dance until the morning lite\nhold me close and never let go"
    r = lc.judge(heard, LYRICS)
    assert r["verdict"] == "verified" and r["score"] > 0.7


def test_judge_mismatch_other_song_common_words():
    heard = "you and me and the love that we know\nI want you to hold me and never go away\nthe night is the time"
    assert lc.judge(heard, LYRICS)["verdict"] == "mismatch"


def test_judge_instrumental_and_no_lyrics():
    assert lc.judge("", LYRICS) == {"verdict": "instrumental", "score": None}
    assert lc.judge("", "", True)["verdict"] == "instrumental"
    assert lc.judge("some words are sung here", "")["verdict"] == "no_lyrics"
    assert lc.judge("some words are sung here", "", lyrics_instrumental=True)["verdict"] == "instrumental"


def test_judge_other_language_is_mismatch_and_cjk_matches():
    assert lc.judge("el rio corre bajo el puente esta noche", LYRICS)["verdict"] == "mismatch"
    assert lc.judge("夜空に星が輝く今夜", "夜空に星が輝く今夜も")["verdict"] == "verified"
    assert lc.judge("夜空に星が輝く今夜", LYRICS)["verdict"] == "mismatch"


def _asr(lines, backend="cuda", lang="en"):
    return lambda p: {"backend": backend, "language": lang, "language_prob": 0.9,
                      "lines": [{"t": i * 3.0, "end": i * 3.0 + 2, "text": t} for i, t in enumerate(lines)]}


def test_check_song_writes_files_and_meta(cache):
    lyr = lambda title, dur: {"text": LYRICS, "lrc": "[00:01.00] a\n", "instrumental": False}
    res = lc.check_song("t1", "Some Artist - Some Title", "v.flac", 200.0, video="V" * 11,
                        asr_fn=_asr(LYRICS.splitlines()), lyrics_fn=lyr)
    assert res["verdict"] == "verified"
    assert ss.asr_path("t1").read_text().startswith("language: en (0.9)")
    assert ss.lyrics_path("t1").read_text().startswith("the river")
    assert ss.lyrics_path("t1", synced=True).exists()
    m = ss.read_meta("t1")
    assert m["artist"] == "Some Artist" and m["title"] == "Some Title" and m["video"] == "V" * 11 and m["duration"] == 200.0


def test_check_song_without_backend_writes_nothing(cache):
    assert lc.check_song("t1", "A - B", "v.flac", asr_fn=_asr([], backend=None), lyrics_fn=lambda *a: {}) is None
    assert not ss.song_dir("t1").exists()


# ---- job + cache audit
class _Ctx:
    def __init__(self):
        self.msgs = []

    def should_stop(self):
        return False

    def progress(self, *a):
        pass

    def log(self, m):
        self.msgs.append(m)


def test_job_checks_missing_only_and_mismatch_feeds_audit(cache, monkeypatch):
    for tid in ("a", "b"):
        (cache / "uploads").mkdir(exist_ok=True)
        (cache / "uploads" / f"{tid}.flac").write_bytes(tid.encode())
        _stem_set(cache, f"hash{tid}", "htdemucs_ft")
    tracks = {t: cache / "uploads" / f"{t}.flac" for t in "ab"}
    ss.update_meta("a", verdict="verified", score=0.9)               # already judged: skipped
    monkeypatch.setattr("app.music_brain.audio.audio_io.audio_info", lambda p: (0, 44100, 2, 200.0))
    monkeypatch.setattr("app.ui.services.album_art._video_id", lambda t: None)
    out = lyric_check_job.run(tracks, {"b": "X - Y"}, _Ctx(), gate=lambda: False, hash_fn=lambda p: f"hash{p.stem}",
                              asr_fn=_asr(["totally different words about something else entirely"]),
                              lyrics_fn=lambda t, d: {"text": LYRICS, "lrc": None, "instrumental": False})
    assert out["todo"] == 1 and out["by_verdict"] == {"mismatch": 1}
    flags = cache_audit.apply_verdicts(cache, {"b": ss.verdict("b"), "a": ss.verdict("a")}, 1.0)
    assert flags["by_class"] == {"lyric_mismatch": 1} and cache_audit.bad_ids(cache) == ["b"]
    cache_audit.apply_verdicts(cache, {"b": {"verdict": "verified"}}, 2.0)
    assert cache_audit.bad_ids(cache) == []


def test_scan_flags_lyric_mismatch(tmp_path):
    p = tmp_path / "a.flac"
    p.write_bytes(b"x")
    f = cache_audit.scan({"a": p}, {}, {}, probe_fn=lambda q: {"dur": 200.0, "vid": None}, shape_fn=lambda n: False,
                         verdicts={"a": {"verdict": "mismatch", "score": 0.1}})
    assert f["a"]["classes"] == ["lyric_mismatch"]


# ---- migration
def _lib(cache):
    (cache / "uploads").mkdir()
    a = cache / "uploads" / "t1.flac"
    a.write_bytes(b"audio-bytes" * 100)
    _stem_set(cache, "HASH1", "htdemucs_ft")
    return {"t1": a}


def test_migrate_dry_run_changes_nothing(cache):
    tracks = _lib(cache)
    before = sorted(str(p) for p in cache.rglob("*"))
    res = song_migrate.run(tracks, dry_run=True, hash_fn=lambda p: "HASH1")
    assert res["planned"] == 3 and res["moved"] == 0 and sorted(str(p) for p in cache.rglob("*")) == before


def test_migrate_moves_links_and_is_idempotent(cache):
    tracks = _lib(cache)
    old = tracks["t1"].read_bytes()
    logs = []
    res = song_migrate.run(tracks, dry_run=False, log=logs.append, hash_fn=lambda p: "HASH1")
    assert res["moved"] == 3 and res["failed"] == 0 and len(logs) == 3
    new = ss.song_dir("t1") / "audio.flac"
    assert new.read_bytes() == old and tracks["t1"].is_symlink() and tracks["t1"].read_bytes() == old   # old path resolves
    assert ss.audio_path("t1") == new
    got = ss.stem_files("t1", "htdemucs_ft", audio_hash="HASH1")
    assert set(got) == {"vocals", "drums"} and got["vocals"].parent == ss.stems_dir("t1", "htdemucs_ft")
    man = json.loads(Path(res["manifest"]).read_text())
    assert len(man["moves"]) == 3 and all(m["sha256"] for m in man["moves"])
    again = song_migrate.run(tracks, dry_run=False, hash_fn=lambda p: "HASH1")           # nothing left to do
    assert again["planned"] == 0 and again["moved"] == 0
    from app.music_brain.audio.audio_io import read_manifest
    assert set(read_manifest(cache / "stems" / "HASH1_htdemucs_ft")) == {"vocals", "drums"}   # old stem cache still reads


def test_migrate_recopies_a_truncated_target_and_resumes(cache):
    tracks = _lib(cache)
    dst = ss.song_dir("t1") / "audio.flac"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"trunc")                                                              # a crashed earlier run
    res = song_migrate.run(tracks, dry_run=False, hash_fn=lambda p: "HASH1", limit=1)
    assert res["moved"] == 1 and dst.read_bytes() == b"audio-bytes" * 100
    res = song_migrate.run(tracks, dry_run=False, hash_fn=lambda p: "HASH1")
    assert res["planned"] == 2 and res["moved"] == 2


# ---- HTTP
def test_library_verdicts_and_files_routes(cache, monkeypatch):
    from app.tests.py.testclient_compat import TestClient
    from app.ui import server

    monkeypatch.setitem(server._tracks, "t9", cache / "t9.flac")
    ss.update_meta("t9", verdict="verified", score=0.8)
    ss.write_text(ss.lyrics_path("t9"), "words\n")
    c = TestClient(server.app)
    assert c.get("/api/library/lyric-verdicts").json()["verdicts"]["t9"] == {"verdict": "verified", "score": 0.8}
    j = c.get("/api/library/song/t9/files").json()
    assert j["lyrics"] == "words\n" and j["asr"] == "" and j["meta"]["verdict"] == "verified"
    assert c.get("/api/library/song/nope/files").status_code == 404
