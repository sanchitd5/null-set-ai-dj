"""Tests for music_brain.stem_service (Phase 2 stem separation + caching).

The cache-logic tests are pure and fast (no Demucs invocation). The
end-to-end separation test actually runs Demucs against a short real audio
clip and is marked `slow` since it downloads model weights on first run and
takes real wall-clock time even on GPU.
"""

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.music_brain.config import ROOT_DIR, STEMS_CACHE_DIR
from app.music_brain.audio.stem_service import (
    _cache_dir_for,
    _load_from_cache,
    _manifest_path,
    file_hash,
    separate,
)

SAMPLE_TRACK = ROOT_DIR / "data" / "songs" / "input.mp3"


def test_file_hash_is_deterministic(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"some audio bytes")
    assert file_hash(p) == file_hash(p)


def test_file_hash_differs_for_different_content(tmp_path):
    p1 = tmp_path / "a.wav"
    p2 = tmp_path / "b.wav"
    p1.write_bytes(b"content one")
    p2.write_bytes(b"content two")
    assert file_hash(p1) != file_hash(p2)


def test_cache_dir_naming_includes_model_and_two_stems():
    d_four = _cache_dir_for("deadbeef", "htdemucs_ft", None)
    d_two = _cache_dir_for("deadbeef", "htdemucs_ft", "vocals")
    assert d_four.name == "deadbeef_htdemucs_ft"
    assert d_two.name == "deadbeef_htdemucs_ft_vocals"
    assert d_four.parent == STEMS_CACHE_DIR


def test_load_from_cache_returns_none_when_manifest_missing(tmp_path):
    assert _load_from_cache(tmp_path) is None


def test_load_from_cache_returns_none_when_referenced_file_deleted(tmp_path):
    stem_file = tmp_path / "vocals.wav"
    stem_file.write_bytes(b"fake wav")
    manifest = {"vocals": str(stem_file)}
    _manifest_path(tmp_path).write_text(json.dumps(manifest), encoding="utf-8")

    stem_file.unlink()  # simulate the cached stem being deleted externally
    assert _load_from_cache(tmp_path) is None


def test_load_from_cache_returns_manifest_when_files_exist(tmp_path):
    stem_file = tmp_path / "vocals.wav"
    stem_file.write_bytes(b"fake wav")
    manifest = {"vocals": str(stem_file)}
    _manifest_path(tmp_path).write_text(json.dumps(manifest), encoding="utf-8")

    loaded = _load_from_cache(tmp_path)
    assert loaded == manifest


def test_separate_raises_on_missing_file():
    with pytest.raises(FileNotFoundError):
        separate("this/file/does/not/exist.mp3")


@pytest.mark.slow
@pytest.mark.skipif(not SAMPLE_TRACK.exists(), reason="sample track not present")
def test_separate_end_to_end_on_short_clip(tmp_path):
    """Runs real Demucs 2-stem separation on a 5s clip trimmed from the
    sample track, then verifies the second call hits the cache."""
    y, sr = sf.read(SAMPLE_TRACK, always_2d=False)
    if y.ndim > 1:
        y = y[:, 0]
    clip = y[: int(sr * 5)]
    clip_path = tmp_path / "clip.wav"
    sf.write(clip_path, clip, sr)

    result_1 = separate(clip_path, two_stems="vocals", force=True)
    assert result_1.from_cache is False
    assert "vocals" in result_1.stems
    assert Path(result_1.stems["vocals"]).exists()

    result_2 = separate(clip_path, two_stems="vocals")
    assert result_2.from_cache is True
    assert result_2.stems == result_1.stems
