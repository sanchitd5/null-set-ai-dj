"""AFRIX: excluded-songs matcher + quarantine."""
import json

from app.music_brain.excluded import excluded_reason as ex


def test_artist_blocklist_matches_credits():
    assert ex(display_name="Burna Boy - Last Last [Audio]")["strong"]
    assert ex(display_name="Shakira, Burna Boy - Dai Dai")["cue"] == "artist"
    assert ex(display_name="Some Act ft. Burna Boy - Song")["strong"]
    assert ex(artist="BurnaBoy", title="x")["strong"]
    assert ex(display_name="Burnaby Boyd - Song") is None


def test_genre_strong_weak_and_clean():
    assert ex(display_name="a - b", genres=["Afropop"])["strong"]
    assert ex(display_name="a - b", genres=["amapiano"])["strong"]
    w = ex(display_name="a - b", genres=["afro house"])
    assert w and not w["strong"]
    assert ex(display_name="a - b", genres=["house", "pop"]) is None


def test_language_is_weak_only():
    w = ex(display_name="a - b", lang_id="yo")
    assert w and not w["strong"]
    assert ex(display_name="a - b", lang_id="en") is None


def test_quarantine_and_restore(tmp_path, monkeypatch):
    from app.ui.services import excluded_songs as es
    from app.music_brain import song_store

    monkeypatch.setattr(es, "_cache", lambda: tmp_path)
    monkeypatch.setattr(song_store, "_cache", lambda: tmp_path)
    (tmp_path / "songs" / "abc123").mkdir(parents=True)
    (tmp_path / "songs" / "abc123" / "audio.m4a").write_bytes(b"x")
    assert es.quarantine("abc123", "Burna Boy - X", "excluded: artist burna boy", "artist")
    assert not (tmp_path / "songs" / "abc123").exists()
    assert es.removed_list()[0]["track_id"] == "abc123"
    assert es.restore("abc123")
    assert (tmp_path / "songs" / "abc123" / "audio.m4a").exists() and not es.removed_list()
    assert es.gate_name("Burna Boy - Y") and not es.gate_name("Nobody - Y")
