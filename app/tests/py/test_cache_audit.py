"""HF9: the song cache audit flags (never deletes) wrong files; a second download of the same video is reused;
a clip / album never enters the library through a download job."""
import json
from pathlib import Path

import pytest

from app.ui.services import cache_audit, download_jobs


def _mk(tmp_path, name, mtime):
    p = tmp_path / name
    p.write_bytes(b"x")
    import os
    os.utime(p, (mtime, mtime))
    return p


def test_scan_flags_duplicate_video_length_and_shape_and_deletes_nothing(tmp_path):
    a, b, c, d = (_mk(tmp_path, f"{n}.flac", t) for n, t in (("a", 100), ("b", 200), ("c", 300), ("d", 400)))
    info = {"a": {"dur": 200.0, "vid": "AAAAAAAAAAA"}, "b": {"dur": 200.0, "vid": "AAAAAAAAAAA"},
            "c": {"dur": 4500.0, "vid": None}, "d": {"dur": 57.0, "vid": "DDDDDDDDDDD"}}
    tracks = {"a": a, "b": b, "c": c, "d": d}
    names = {"a": "Song A", "b": "Song A", "c": "Artist - Dive (Full Album)", "d": "How to pronounce X"}
    flags = cache_audit.scan(tracks, names, {}, probe_fn=lambda p: info[p.stem],
                             shape_fn=lambda n: "Full Album" in n or n.startswith("How to"))
    assert set(flags) == {"b", "c", "d"}
    assert flags["b"]["classes"] == ["duplicate_source"] and flags["b"]["of"] == "a"      # the oldest copy is kept
    assert flags["c"]["classes"] == ["bad_length", "not_a_track"]
    assert flags["d"]["classes"] == ["bad_length", "not_a_track"]
    assert all(p.exists() for p in tracks.values())


def test_known_source_overrides_tag_and_missing_file_is_skipped(tmp_path):
    a = _mk(tmp_path, "a.flac", 1)
    b = _mk(tmp_path, "b.flac", 2)
    flags = cache_audit.scan({"a": a, "b": b, "gone": tmp_path / "gone.flac"}, {}, {"a": "V" * 11, "b": "V" * 11},
                             probe_fn=lambda p: {"dur": 200.0, "vid": None}, shape_fn=lambda n: False)
    assert set(flags) == {"b"}


def test_merge_keeps_status_and_bad_ids(tmp_path):
    cache_audit.merge(tmp_path, {"x": {"classes": ["bad_length"], "detail": "10s", "video": "V", "of": None}}, 5.0)
    assert cache_audit.mark(tmp_path, "x", status="refetched", new_id="n")
    res = cache_audit.merge(tmp_path, {"x": {"classes": ["bad_length"], "detail": "10s", "video": "V", "of": None},
                                       "y": {"classes": ["not_a_track"], "detail": "", "video": None, "of": None}}, 9.0)
    assert res == {"flagged": 2, "by_class": {"bad_length": 1, "not_a_track": 1}}
    d = cache_audit.load(tmp_path)
    assert d["x"]["status"] == "refetched" and d["x"]["new_id"] == "n" and d["x"]["at"] == 5.0
    assert cache_audit.bad_ids(tmp_path) == ["x", "y"]
    cache_audit.merge(tmp_path, {}, 10.0)                       # no longer suspect: drops out
    assert cache_audit.bad_ids(tmp_path) == []


def test_video_id_of():
    assert cache_audit.video_id_of("https://www.youtube.com/watch?v=iH3uetMaaUc") == "iH3uetMaaUc"
    assert cache_audit.video_id_of("https://youtu.be/iH3uetMaaUc?t=3") == "iH3uetMaaUc"
    assert cache_audit.video_id_of("ytmsearch:Artist - Title") is None


def test_track_for_video_skips_unusable(tmp_path, monkeypatch):
    from app.ui.services import album_art

    monkeypatch.setattr(album_art, "art_dir", lambda: tmp_path)
    (tmp_path / "sources.json").write_text(json.dumps({"t1": "V" * 11, "t2": "V" * 11, "t3": "W" * 11}))
    assert album_art.track_for_video("V" * 11) == "t1"
    assert album_art.track_for_video("V" * 11, lambda t: t != "t1") == "t2"
    assert album_art.track_for_video("Z" * 11) is None


def test_length_gate_quarantines_and_fails_the_job_before_registration(tmp_path, monkeypatch):
    from app.ui.services import download_service

    clip = tmp_path / "clip.m4a"
    clip.write_bytes(b"x")
    moved = []
    monkeypatch.setattr(download_service, "_duration_secs", lambda p: 57.0)
    monkeypatch.setattr(download_service, "_quarantine", lambda path, pick, why, info: moved.append((path, why)))
    with pytest.raises(RuntimeError, match="length check failed"):
        download_jobs._length_gate([clip])
    assert moved and "0.9 min" in moved[0][1]
    monkeypatch.setattr(download_service, "_duration_secs", lambda p: 200.0)
    download_jobs._length_gate([clip])                           # in range: passes
    monkeypatch.setattr(download_service, "_duration_secs", lambda p: None)
    download_jobs._length_gate([clip])                           # unreadable: the later analysis decides


def test_post_check_without_a_pick_catches_a_truncated_file(tmp_path, monkeypatch):
    from app.ui.services import download_service as ds

    f = tmp_path / "t.m4a"
    f.write_bytes(b"x")
    monkeypatch.setattr(ds, "_duration_secs", lambda p: 40.0)
    seen = []
    monkeypatch.setattr(ds, "_quarantine", lambda *a: seen.append(a))
    with pytest.raises(RuntimeError, match="does not match"):
        ds._post_check(None, {"title": "T", "duration": 200}, f)
    assert seen
    monkeypatch.setattr(ds, "_duration_secs", lambda p: 198.0)
    ds._post_check(None, {"title": "T", "duration": 200}, f)    # within slack: fine


def test_second_request_for_the_same_video_reuses_the_first_copy(tmp_path, monkeypatch):
    """Cause of the duplicate files: a watch URL never went through the library lookup, so the same video was
    downloaded again and (content-hash id) became a second track."""
    from app.ui import server
    from app.ui.services import album_art

    f = tmp_path / "t1.flac"
    f.write_bytes(b"x")
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(album_art, "art_dir", lambda: tmp_path)
    monkeypatch.setitem(server._tracks, "t1", f)
    monkeypatch.setitem(server._track_names, "t1", "Some Song")
    (tmp_path / "sources.json").write_text(json.dumps({"t1": "iH3uetMaaUc"}))
    got = server._reuse_existing("https://www.youtube.com/watch?v=iH3uetMaaUc")
    assert got and got[0]["track_id"] == "t1"
    assert server._reuse_existing("https://www.youtube.com/watch?v=OTHEROTHER1") is None
    cache_audit.merge(tmp_path, {"t1": {"classes": ["bad_length"], "detail": "", "video": "iH3uetMaaUc", "of": None}}, 1.0)
    assert server._reuse_existing("https://www.youtube.com/watch?v=iH3uetMaaUc") is None    # flagged: fetch afresh


def test_admin_page_lists_flags_with_a_refetch_button_only_for_open_items_with_a_source(tmp_path, monkeypatch):
    from app.music_brain import config
    from app.ui import server
    from app.ui.services import admin

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setitem(server._track_names, "t1", "Name <b>")
    cache_audit.merge(tmp_path, {
        "t1": {"classes": ["bad_length"], "detail": "57s", "video": "V" * 11, "of": None},
        "t2": {"classes": ["not_a_track"], "detail": "mix", "video": None, "of": None}}, 1.0)
    body = admin._cache_audit_body()
    assert body.count("Re-fetch</button>") == 1 and "/api/admin/cache-audit/t1/refetch" in body
    assert "Name &lt;b&gt;" in body and "Name <b>" not in body                      # escaped
    assert ("Cache audit", "/admin/cache-audit") in admin.NAV
