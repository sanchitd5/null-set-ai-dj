"""A pick ("Artist - Title") must resolve to the right recording: the yt-dlp match filter, the real-song check
and the post-download check, on real-world-shaped candidate lists (synthetic names). YouTube is never called."""
import json

import pytest

from app.ui.services import download_service as ds

SONG = (["vela", "moon"], ["glass", "harbor"], "Glass Harbor", "Vela Moon")


def _first_clear(entries, *, song=SONG, official_only=False):
    """What yt-dlp does: the first entry the match filter lets through."""
    f = ds._search_match_filter([], song, official_only)
    for e in entries:
        if f(e, incomplete=True) is None and f(e) is None:
            return e
    return None


NAMESAKE_FIRST = [
    {"title": "Orin Pike - Glass Harbor (Official Audio)", "channel": "Orin Pike", "duration": 201},       # namesake
    {"title": "Vela Moon talks about Glass Harbor | interview", "channel": "Late Show", "duration": 600},   # talk
    {"title": "Glass Harbor (sample)", "channel": "Vela Moon", "duration": 120},                            # sample
    {"title": "Vela Moon - Glass Harbor", "channel": "Vela Moon - Topic", "artists": ["Vela Moon"], "duration": 41},   # snippet
    {"title": "Vela Moon - Glass Harbor (Orin Pike Remix)", "channel": "Vela Moon", "duration": 280},      # remix
    {"title": "Vela Moon - Glass Harbor (Lyrics)", "channel": "lyric hub", "duration": 214},                # fan lyric upload
    {"title": "Glass Harbor", "channel": "Vela Moon - Topic", "artists": ["Vela Moon"], "duration": 214},  # the song
]


def test_only_the_real_recording_clears_a_list_full_of_lookalikes():
    got = _first_clear(NAMESAKE_FIRST, official_only=True)          # pass 1: the lyric copy waits for pass 2
    assert _first_clear(NAMESAKE_FIRST[:-1]) is NAMESAKE_FIRST[-2]    # pass 2 with no Topic upload: the credited lyric copy
    assert got is NAMESAKE_FIRST[-1]


def test_first_pass_takes_the_official_upload_over_a_fan_copy():
    fan = {"title": "Vela Moon - Glass Harbor", "channel": "some uploads", "duration": 213}
    topic = NAMESAKE_FIRST[-1]
    assert _first_clear([fan, topic], official_only=True) is topic         # pass 1: HIGH tier only
    assert _first_clear([fan], official_only=True) is None
    assert _first_clear([fan]) is fan                                       # pass 2: a credited fan copy is better than nothing


def test_flat_rows_are_not_judged_for_identity():
    # a Topic video's flat row has no artist metadata yet: it must reach the full-entry check
    f = ds._search_match_filter([], SONG, True)
    assert f({"title": "Glass Harbor", "channel": "Vela Moon - Topic", "duration": 214}, incomplete=True) is None


def test_old_three_tuple_still_works():
    f = ds._search_match_filter([], (["vela", "moon"], ["glass", "harbor"], "Glass Harbor"))
    assert f({"title": "Vela Moon - Glass Harbor", "channel": "x", "duration": 200}) is None


def test_title_only_query_keeps_the_word_check():
    f = ds._search_match_filter([], ([], ["glass", "harbor"], "Glass Harbor", ""))
    assert f({"title": "Glass Harbor", "channel": "x", "duration": 200}) is None
    assert "title does not match" in f({"title": "Other Song", "channel": "x", "duration": 200})


def test_trace_records_each_judged_candidate():
    trace = []
    f = ds._search_match_filter([], SONG, False, trace=trace)
    f(NAMESAKE_FIRST[0])
    f(NAMESAKE_FIRST[-1])
    assert [r["score"] > 0 for r in trace] == [False, True]
    assert "another artist" in trace[0]["why"]


# ---- verify_song ------------------------------------------------------------------------------------
def _verify(monkeypatch, entries, artist="Vela Moon", title="Glass Harbor"):
    monkeypatch.setattr(ds, "_yt_dlp", object())
    monkeypatch.setattr(ds, "_guarded_extract", lambda opts, url: {"entries": entries})
    return ds.verify_song(artist, title)


def test_verify_rejects_a_namesake_only_result_list(monkeypatch):
    rows = [{"title": "Orin Pike - Glass Harbor", "channel": "Orin Pike", "duration": 200},
            {"title": "Glass Harbor - Orin Pike", "channel": "Orin Pike - Topic", "duration": 200}]
    assert _verify(monkeypatch, rows) is False


def test_verify_accepts_the_real_one_among_others(monkeypatch):
    rows = [{"title": "Orin Pike - Glass Harbor", "channel": "Orin Pike", "duration": 200},
            {"title": "Glass Harbor (Official Audio)", "channel": "Vela Moon - Topic", "duration": 200}]
    assert _verify(monkeypatch, rows) is True


def test_verify_partial_artist_name_is_not_the_artist(monkeypatch):
    rows = [{"title": "Arjan Dhale - Paper Boats", "channel": "Arjan Dhale", "duration": 200}]
    assert _verify(monkeypatch, rows, artist="Ari Dhale", title="Paper Boats") is False


def test_verify_ignores_a_dated_concert_upload(monkeypatch):
    rows = [{"title": "Vela Moon - Glass Harbor - April 29, 2023 - Denver", "channel": "Vela Moon", "duration": 300}]
    assert _verify(monkeypatch, rows) is False


# ---- post-download check ----------------------------------------------------------------------------
@pytest.fixture
def cache(tmp_path, monkeypatch):
    import app.music_brain.config as cfg
    monkeypatch.setattr(cfg, "CACHE_DIR", tmp_path / "cache")
    return tmp_path / "cache"


def _file(tmp_path, name="a.m4a"):
    p = tmp_path / name
    p.write_bytes(b"x")
    return p


GOOD = {"title": "Glass Harbor", "channel": "Vela Moon - Topic", "artists": ["Vela Moon"], "duration": 214.0}


def test_post_check_keeps_a_file_that_is_the_pick(tmp_path, cache, monkeypatch):
    monkeypatch.setattr(ds, "_duration_secs", lambda p: 214.4)
    p = _file(tmp_path)
    ds._post_check(SONG, GOOD, p)
    assert p.exists() and not cache.exists()


def test_post_check_quarantines_a_truncated_preview(tmp_path, cache, monkeypatch):
    monkeypatch.setattr(ds, "_duration_secs", lambda p: 30.0)               # a 30 s preview was delivered
    p = _file(tmp_path)
    with pytest.raises(RuntimeError, match="quarantine"):
        ds._post_check(SONG, GOOD, p)
    assert not p.exists()
    kept = list((cache / ds.PICK_QUARANTINE).glob("*.m4a"))
    note = list((cache / ds.PICK_QUARANTINE).glob("*.json"))
    assert len(kept) == 1 and len(note) == 1
    assert "too short" in json.loads(note[0].read_text())["why"]


def test_post_check_quarantines_a_file_whose_length_contradicts_the_listing(tmp_path, cache, monkeypatch):
    monkeypatch.setattr(ds, "_duration_secs", lambda p: 120.0)
    p = _file(tmp_path)
    with pytest.raises(RuntimeError):
        ds._post_check(SONG, GOOD, p)
    assert not p.exists()


def test_post_check_quarantines_another_artist_in_the_delivered_metadata(tmp_path, cache, monkeypatch):
    monkeypatch.setattr(ds, "_duration_secs", lambda p: 200.0)
    other = {"title": "Glass Harbor", "channel": "Orin Pike - Topic", "artists": ["Orin Pike"], "duration": 200.0}
    p = _file(tmp_path)
    with pytest.raises(RuntimeError, match="someone else"):
        ds._post_check(SONG, other, p)
    assert not p.exists()


def test_post_check_never_loses_a_download_when_the_check_itself_breaks(tmp_path, cache, monkeypatch):
    monkeypatch.setattr(ds, "_duration_secs", lambda p: (_ for _ in ()).throw(OSError("no ffprobe")))
    p = _file(tmp_path)
    ds._post_check(SONG, GOOD, p)
    assert p.exists()
