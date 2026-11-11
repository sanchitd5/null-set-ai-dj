"""G26: song folders are the only structure. Writers land in songs/<id>/, readers go through song_store (guard)."""
import hashlib
import os
import re
from pathlib import Path

import pytest

from app.music_brain import song_store as ss

APP = Path(__file__).resolve().parents[2]

# Files that may name the flat directories: the resolver, the migration, config constants, the sim fixture writer and the
# read-only fallbacks to the old layout (stem server, sim library, swap_mix; TODO 2026-12-15: remove with the fallback).
ALLOWED = {
    "music_brain/song_store.py", "ui/services/song_migrate.py", "music_brain/config.py",
    "sim/stem_capture.py", "stem_server/server.py", "sim/library.py", "music_brain/render/swap_mix.py",
}
FLAT = [
    re.compile(r"\bSTEMS_CACHE_DIR\b"),
    re.compile(r"\bUPLOAD_DIR\b"),
    re.compile(r"/\s*[\"'](uploads|stems)[\"']"),          # cache / "uploads", cache / "stems"
    re.compile(r"\}/(uploads|stems)/(?!\{)"),               # f"{cache_dir}/stems/..." (not URL routes "/stems/{name}")
    re.compile(r"\"uploads/_names\.json\"|'uploads/_names\.json'"),
]


def test_no_new_code_touches_the_flat_song_dirs():
    bad = []
    for p in sorted(APP.rglob("*.py")):
        rel = p.relative_to(APP).as_posix()
        if rel.startswith(("tests/", "music_brain/knowledge/")) or rel in ALLOWED:   # knowledge/ = dj-atlas submodule
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if any(r.search(code) for r in FLAT):
                bad.append(f"{rel}:{n}: {line.strip()[:100]}")
    assert not bad, "go through app/music_brain/song_store.py instead:\n" + "\n".join(bad)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    import app.music_brain.config as cfg
    monkeypatch.setattr(cfg, "CACHE_DIR", tmp_path)
    return tmp_path


def test_adopt_audio_renames_from_incoming_into_the_song_folder(cache):
    inc = ss.incoming_dir()
    inc.mkdir(parents=True)
    f = inc / "dl.mp3"
    f.write_bytes(b"abc" * 100)
    ino = f.stat().st_ino
    tid = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
    dest = ss.adopt_audio(f, tid)
    assert dest == ss.song_dir(tid) / "audio.mp3" and dest.stat().st_ino == ino and not f.exists()
    assert ss.audio_path(tid) == dest
    assert not (cache / "uploads").exists()


def test_adopt_audio_keeps_an_existing_song(cache):
    inc = ss.incoming_dir()
    inc.mkdir(parents=True)
    first = ss.store_bytes(b"one", "t1", ".mp3")
    f = inc / "again.flac"
    f.write_bytes(b"other")
    assert ss.adopt_audio(f, "t1") == first and not f.exists() and first.read_bytes() == b"one"


def test_library_audio_lists_folders_and_unmoved_flat_files_not_scratch(cache):
    ss.store_bytes(b"new", "a1", ".flac")
    (cache / "uploads").mkdir()
    (cache / "uploads" / "b2.mp3").write_bytes(b"old")
    (cache / "uploads" / "a1.mp3").write_bytes(b"dup")             # the folder wins
    (cache / "uploads" / "_names.json").write_text("{}")
    ss.incoming_dir().mkdir(parents=True, exist_ok=True)
    (ss.incoming_dir() / "x.mp3").write_bytes(b"x")
    got = ss.library_audio()
    assert set(got) == {"a1", "b2"} and got["a1"].parent == ss.song_dir("a1")


def test_names_file_moves_to_songs_once(cache):
    (cache / "uploads").mkdir()
    (cache / "uploads" / "_names.json").write_text('{"a": "A"}')
    assert ss.names_file() == cache / "uploads" / "_names.json"          # reading copies nothing
    p = ss.names_file(for_write=True)
    assert p == cache / "songs" / "_names.json" and p.read_text() == '{"a": "A"}'


def test_upload_route_lands_in_the_song_folder(cache, monkeypatch):
    from app.tests.py.testclient_compat import TestClient
    from app.ui import server

    monkeypatch.setattr(server, "_queue_stems", lambda t: None)
    monkeypatch.setattr(server.album_art, "enqueue", lambda t: None)
    c = TestClient(server.app)
    body = b"RIFF" + b"x" * 200
    r = c.post("/api/tracks", files={"file": ("Some Name.mp3", body, "audio/mpeg")})
    tid = hashlib.sha256(body).hexdigest()[:16]
    assert r.json()["track_id"] == tid
    assert (cache / "songs" / tid / "audio.mp3").read_bytes() == body
    assert not (cache / "uploads").exists()
    assert server._tracks[tid] == cache / "songs" / tid / "audio.mp3"
    assert server._song_stem(server._tracks[tid]) == tid


def test_register_downloaded_moves_into_the_folder(cache, monkeypatch):
    from app.ui import server

    monkeypatch.setattr(server, "_queue_stems", lambda t: None)
    monkeypatch.setattr(server.album_art, "enqueue", lambda t: None)
    ss.incoming_dir().mkdir(parents=True)
    f = ss.incoming_dir() / "Artist - Title.m4a"
    f.write_bytes(b"m4a-bytes" * 20)
    res = server._register_downloaded([f])
    tid = res[0]["track_id"]
    assert res[0]["filename"] == f"{tid}.m4a"
    assert (cache / "songs" / tid / "audio.m4a").is_file() and not f.exists()


def test_stem_sets_are_written_to_and_read_from_the_song_folder(cache):
    from app.music_brain.audio import stem_service as sv

    h = "ab" * 32
    d = sv._cache_dir_for(h, "htdemucs_ft", None)
    assert d == cache / "songs" / h[:16] / "stems" / "htdemucs_ft"
    d.mkdir(parents=True)
    paths = {}
    for n in sv.FOUR_STEM_NAMES:
        (d / f"{n}.flac").write_bytes(b"s")
        paths[n] = str(d / f"{n}.flac")
    sv.write_manifest(d, paths)
    assert sv.complete_ft(h) == paths and sv.ft_vocals is not None
    assert sv.load_set(h, "htdemucs_ft") == paths
    assert sv.load_set(h, "htdemucs") is None


def test_stem_sets_in_the_old_flat_cache_still_read(cache):
    from app.music_brain.audio import stem_service as sv

    h = "cd" * 32
    d = cache / "stems" / f"{h}_htdemucs_ft"
    d.mkdir(parents=True)
    paths = {}
    for n in sv.FOUR_STEM_NAMES:
        (d / f"{n}.flac").write_bytes(b"s")
        paths[n] = str(d / f"{n}.flac")
    sv.write_manifest(d, paths)
    assert sv.complete_ft(h) == paths


def test_vocal_stems_listing_covers_song_folders(cache):
    from app.music_brain.analysis import lyrics_asr as asr
    from app.music_brain.audio import stem_service as sv

    h = "ef" * 32
    d = sv._cache_dir_for(h, "htdemucs_ft", None)
    d.mkdir(parents=True)
    (d / "vocals.flac").write_bytes(b"v")
    sv.write_manifest(d, {"vocals": str(d / "vocals.flac")})
    assert asr.vocal_stems() == [(h[:16], str(d / "vocals.flac"))]
    assert asr.current_vocal(h) == str(d / "vocals.flac")
