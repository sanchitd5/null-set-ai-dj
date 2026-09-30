from pathlib import Path

from app.ui.library_service import (
    SUPPORTED_AUDIO_EXTENSIONS,
    configured_library_dirs,
    file_hash,
    scan_library,
)


def test_configured_dirs_are_semicolon_separated_and_invalid_entries_ignored(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    dirs = configured_library_dirs(
        {"DJ_LIBRARY_DIRS": f" {first} ; {tmp_path / 'missing'};{second};{first} "}
    )

    assert dirs == [first.resolve(), second.resolve()]


def test_scan_filters_extensions_and_returns_content_ids(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    nested = library / "nested"
    nested.mkdir()
    (library / "track.MP3").write_bytes(b"same audio")
    (nested / "track.flac").write_bytes(b"other audio")
    (library / "notes.txt").write_bytes(b"not audio")

    tracks = scan_library({"DJ_LIBRARY_DIRS": str(library)})

    assert [item["extension"] for item in tracks] == [".flac", ".mp3"]
    assert tracks[0]["track_id"] == file_hash(nested / "track.flac")
    assert tracks[1]["track_id"] == file_hash(library / "track.MP3")
    assert all(item["extension"] in SUPPORTED_AUDIO_EXTENSIONS for item in tracks)


def test_scan_is_idempotent_and_deduplicates_identical_content(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    (library / "a.wav").write_bytes(b"identical")
    (library / "copy.ogg").write_bytes(b"identical")

    first = scan_library({"DJ_LIBRARY_DIRS": str(library)})
    second = scan_library({"DJ_LIBRARY_DIRS": str(library)})

    assert first == second
    assert len(first) == 1
    assert first[0]["track_id"] == file_hash(library / "a.wav")


def test_scan_does_not_follow_audio_symlink_outside_allowlisted_root(tmp_path):
    library = tmp_path / "library"
    outside = tmp_path / "outside.mp3"
    library.mkdir()
    outside.write_bytes(b"outside")
    link = library / "linked.mp3"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        # Symlink creation may be disabled on Windows without developer mode.
        return

    assert scan_library({"DJ_LIBRARY_DIRS": str(library)}) == []


def test_empty_or_unset_configuration_returns_no_tracks(monkeypatch, tmp_path):
    monkeypatch.delenv("DJ_LIBRARY_DIRS", raising=False)
    assert scan_library() == []
    monkeypatch.setenv("DJ_LIBRARY_DIRS", f"{tmp_path / 'does-not-exist'};")
    assert scan_library() == []
