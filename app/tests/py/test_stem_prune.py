"""Owner rule "if ft exists it should automatically remove non ft" (stem_service.prune_non_ft):
a song's non-ft stem folders go only once its htdemucs_ft 4-stem set is complete, the write paths
(separate, StemWorker.finish) prune by themselves, and the vocal readers use the ft vocals so a
pruned 2-stem folder never forces a new separation. No Demucs: stem files are written by hand."""
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.music_brain import maintain as mt
from app.music_brain.audio import stem_service as ss
from app.music_brain.audio.audio_io import write_manifest

H = "ab" * 32


def _set(stems_dir: Path, h: str, variant: str, names=ss.FOUR_STEM_NAMES) -> Path:
    d = stems_dir / f"{h}_{variant}"
    d.mkdir(parents=True)
    paths = {}
    for n in names:
        (d / f"{n}.flac").write_bytes(b"x" * 1000)
        paths[n] = str(d / f"{n}.flac")
    write_manifest(d, paths)
    return d


@pytest.fixture
def stems(tmp_path, monkeypatch):
    d = tmp_path / "stems"
    d.mkdir()
    monkeypatch.setattr(ss, "STEMS_CACHE_DIR", d)
    return d


def test_prune_only_after_a_complete_ft_set(stems):
    fast = _set(stems, H, "htdemucs")
    voc = _set(stems, H, "htdemucs_vocals", ss.TWO_STEM_NAMES)
    ft = _set(stems, H, "htdemucs_ft", ("vocals", "drums", "bass"))          # 3 of 4: incomplete
    r = ss.prune_non_ft(H, stems)
    assert r["skipped"] and r["removed"] == [] and fast.exists() and voc.exists()
    (ft / "other.flac").write_bytes(b"x")
    write_manifest(ft, {n: str(ft / f"{n}.flac") for n in ss.FOUR_STEM_NAMES})
    (ft / "bass.flac").unlink()                                               # manifest ok, file gone
    assert ss.prune_non_ft(H, stems)["skipped"] and fast.exists()
    (ft / "bass.flac").write_bytes(b"x")
    ft_voc = _set(stems, H, "htdemucs_ft_vocals", ss.TWO_STEM_NAMES)
    other = _set(stems, "cd" * 32, "htdemucs")                                # another song
    size = ss.dir_bytes(fast) + ss.dir_bytes(voc)
    r = ss.prune_non_ft(H, stems)
    assert sorted(r["removed"]) == [fast.name, voc.name] and r["bytes"] == size > 6000
    assert not fast.exists() and not voc.exists() and ft.exists() and ft_voc.exists() and other.exists()
    assert not any((stems.parent / "stems_trash").iterdir())                  # renamed out, then deleted
    assert ss.prune_non_ft(H, stems)["removed"] == []                         # idempotent


def test_dry_run_prune_reports_without_deleting(stems):
    fast = _set(stems, H, "htdemucs")
    _set(stems, H, "htdemucs_ft")
    r = ss.prune_non_ft(H, stems, dry_run=True)
    assert r["removed"] == [fast.name] and r["bytes"] == ss.dir_bytes(fast) and fast.exists()


def test_stem_worker_finish_prunes_only_for_an_ft_set(stems):
    fast = _set(stems, H, "htdemucs")
    ft_dir = stems / f"{H}_htdemucs_ft"
    ft_dir.mkdir()
    paths = {n: str(ft_dir / f"{n}.flac") for n in ss.FOUR_STEM_NAMES}
    for p in paths.values():
        Path(p).write_bytes(b"x")
    ss.StemWorker.finish({"stems": paths})
    assert not fast.exists() and ss.complete_ft(H, stems)
    h2 = "ef" * 32
    other = _set(stems, h2, "htdemucs_vocals", ss.TWO_STEM_NAMES)
    d2 = stems / f"{h2}_htdemucs"
    d2.mkdir()
    p2 = {n: str(d2 / f"{n}.flac") for n in ss.FOUR_STEM_NAMES}
    for p in p2.values():
        Path(p).write_bytes(b"x")
    ss.StemWorker.finish({"stems": p2})                                      # a fast set: nothing pruned
    assert other.exists()


def test_separate_ft_write_prunes_but_a_failed_run_keeps_everything(stems, tmp_path, monkeypatch):
    song = tmp_path / "song.wav"
    sf.write(song, np.zeros((4410, 2), dtype="float32"), 44100)
    h = ss.file_hash(song)
    fast = _set(stems, h, "htdemucs")
    voc = _set(stems, h, "htdemucs_vocals", ss.TWO_STEM_NAMES)
    monkeypatch.setattr(ss, "_detect_device", lambda: "cpu")

    def fail(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(ss.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        ss.separate(song)
    assert fast.exists() and voc.exists()

    def demucs(cmd, **kw):
        out = Path(cmd[cmd.index("-o") + 1]) / cmd[cmd.index("-n") + 1] / song.stem
        out.mkdir(parents=True)
        for n in ss.FOUR_STEM_NAMES:
            sf.write(out / f"{n}.wav", np.zeros((4410, 2), dtype="int16"), 44100, subtype="PCM_16")

    monkeypatch.setattr(ss.subprocess, "run", demucs)
    res = ss.separate(song)
    assert res.model == "htdemucs_ft" and set(res.stems) == set(ss.FOUR_STEM_NAMES)
    assert not fast.exists() and not voc.exists()


def test_vocals_readers_use_ft_vocals_and_fall_back_to_a_fast_run(stems, tmp_path, monkeypatch):
    import app.ui.server as srv

    song = tmp_path / "s.mp3"
    song.write_bytes(b"audio")
    h = ss.file_hash(song)
    calls = []
    monkeypatch.setattr(srv, "_track_path", lambda tid: str(song))
    monkeypatch.setattr(srv, "separate_stems", lambda p, **kw: calls.append(kw) or ss.StemResult(
        h, kw["model"], kw["two_stems"], {"vocals": "/fast/vocals.flac"}, "", False))
    assert srv._vocals_stem_impl("t") == "/fast/vocals.flac"                  # no ft set: fast 2-stem run
    assert calls == [{"two_stems": "vocals", "model": "htdemucs"}]
    assert srv._vocals_cached("t") is False
    ft = _set(stems, h, "htdemucs_ft")
    assert srv._vocals_stem_impl("t") == str(ft / "vocals.flac") and len(calls) == 1
    assert srv._vocals_cached("t") is True


def test_server_stem_cache_drops_a_pruned_folder(stems, tmp_path, monkeypatch):
    import app.ui.server as srv

    song = tmp_path / "s.mp3"
    song.write_bytes(b"audio")
    h = ss.file_hash(song)
    fast = _set(stems, h, "htdemucs")
    monkeypatch.setattr(srv, "_track_path", lambda tid: str(song))
    monkeypatch.setattr(srv, "_stem_cache", {})
    assert srv._cached_stems4("t")["vocals"].startswith(str(fast))
    ft = _set(stems, h, "htdemucs_ft")
    ss.prune_non_ft(h, stems)
    assert srv._cached_stems4("t")["vocals"].startswith(str(ft))


def test_maintain_cleanup_lists_folders_and_gb_then_is_idempotent(stems):
    _set(stems, H, "htdemucs_ft")
    fast = _set(stems, H, "htdemucs")
    voc = _set(stems, H, "htdemucs_vocals", ss.TWO_STEM_NAMES)
    lone = _set(stems, "cd" * 32, "htdemucs")                                 # no ft: never touched
    dry = mt.cleanup_non_ft(stems, dry_run=True)
    assert dry["songs"] == 1 and sorted(dry["folders"]) == [fast.name, voc.name]
    assert dry["bytes"] == ss.dir_bytes(fast) + ss.dir_bytes(voc) and dry["gb"] == 0.0 and fast.exists() and voc.exists()
    real = mt.cleanup_non_ft(stems, dry_run=False)
    assert sorted(real["folders"]) == sorted(dry["folders"]) and not fast.exists() and lone.exists()
    again = mt.cleanup_non_ft(stems, dry_run=False)
    assert again["songs"] == 0 and again["folders"] == []
