"""audio_convert --jobs: parallel folders, memory cap, failure isolation, interrupt.

Synthetic audio in tmp_path only. Workers are spawned processes, so injected
failures live in module-level worker functions (picklable by name)."""
import fcntl
import json
import os

import pytest

from app.music_brain.audio import audio_convert
from app.tests.py.test_audio_flac import STEMS, _keylock_dir, _stem_dir


def _cache(root, n_stems=6):
    for i in range(n_stems):
        _stem_dir(root, f"s{i:02d}_htdemucs_ft", seed=10 * i)
    _keylock_dir(root, "k00")
    _keylock_dir(root, "k01")
    return root


def _state(root):
    """Every file's name + bytes (json with root made relative): what a run must produce."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            rel = str(p.relative_to(root))
            if p.suffix == ".json":   # manifests hold absolute paths
                out[rel] = json.loads(p.read_text().replace(str(root), "ROOT"))
            else:
                out[rel] = p.read_bytes()
    return out


def _consistent(d):
    """A folder is all-WAV or all-FLAC, and its manifest / meta points at what exists."""
    wavs, flacs = list(d.glob("*.wav")), list(d.glob("*.flac"))
    assert not (wavs and flacs), f"{d.name} half converted"
    assert not list(d.glob("*.convert*")) and not list(d.glob("*.tmp*"))
    if (d / "manifest.json").exists():
        from app.music_brain.audio import audio_io
        m = audio_io.read_manifest(d)
        assert all(os.path.exists(p) for p in m.values())
    return "flac" if flacs else "wav"


def _quiet(s):
    pass


def test_jobs4_matches_jobs1(tmp_path):
    a, b = _cache(tmp_path / "a"), _cache(tmp_path / "b")
    r1 = audio_convert.run(a, apply=True, log=_quiet, jobs=1)
    r4 = audio_convert.run(b, apply=True, log=_quiet, jobs=4)
    # --jobs 4 is capped at cpus - 1 (a 4-core CI runner gets 3), so expect what effective_jobs allows
    assert r1["jobs"] == 1 and r4["jobs"] == audio_convert.effective_jobs(4)
    assert not r1["failed"] and not r4["failed"]
    for k in ("dirs", "files", "converted", "bytes_before", "bytes_after"):
        assert r1[k] == r4[k], k
    s1, s4 = _state(a), _state(b)
    assert s1.keys() == s4.keys() and s1 == s4       # byte-identical FLACs, same manifests
    assert not list(b.rglob("*.wav"))


def failing_worker(kind, d, wavs):
    """Folder s02 fails verify on every stem (in the child), s04 kills its process."""
    if d.name.startswith("s04"):
        os._exit(3)
    if d.name.startswith("s02"):
        audio_convert.verify = lambda w, f, s: "injected"
    return audio_convert.convert_dir(kind, d, wavs)


def test_worker_failures_isolated(tmp_path):
    _cache(tmp_path)
    res = audio_convert.run(tmp_path, apply=True, log=_quiet, jobs=4, _worker=failing_worker)
    stems = tmp_path / "stems"
    failed = {f["file"] for f in res["failed"]}
    assert any("s04" in f and "crashed" in e["error"] for e in res["failed"] for f in [e["file"]])
    assert sum("s02" in f for f in failed) == len(STEMS)
    for d in stems.iterdir():
        kind = _consistent(d)
        assert kind == ("wav" if d.name[:3] in ("s02", "s04") else "flac"), d.name
    for d in (tmp_path / "keylock").iterdir():
        assert _consistent(d) == "flac"
    # a re-run with the real worker finishes the survivors
    again = audio_convert.run(tmp_path, apply=True, log=_quiet, jobs=4)
    assert not again["failed"] and not list(tmp_path.rglob("*.wav"))


def test_interrupt_leaves_consistent_and_rerun_finishes(tmp_path):
    _cache(tmp_path, n_stems=10)
    seen = []

    def log(s):
        seen.append(s)
        if len(seen) == 2:
            audio_convert.request_stop()     # as SIGINT would, from the parent

    res = audio_convert.run(tmp_path, apply=True, log=log, jobs=2)
    assert res["interrupted"] and res["not_started"] > 0 and not res["failed"]
    kinds = [_consistent(d) for d in (tmp_path / "stems").iterdir()]
    kinds += [_consistent(d) for d in (tmp_path / "keylock").iterdir()]
    assert "wav" in kinds and "flac" in kinds
    again = audio_convert.run(tmp_path, apply=True, log=_quiet, jobs=2)
    assert not again["interrupted"] and not again["failed"]
    assert not list(tmp_path.rglob("*.wav"))


def test_effective_jobs_defaults_and_caps():
    assert audio_convert.effective_jobs(None, cpus=16) == 4
    assert audio_convert.effective_jobs(None, cpus=4) == 2
    assert audio_convert.effective_jobs(None, cpus=1) == 1
    assert audio_convert.effective_jobs(64, max_mem_gb=1000, cpus=8) == 7   # hard cap cpus - 1
    assert audio_convert.effective_jobs(64, cpus=64) == int(             # default memory guard
        audio_convert.DEFAULT_MAX_MEM_GB // audio_convert.PEAK_GB_PER_DIR)
    assert audio_convert.effective_jobs(1, cpus=8) == 1
    per = audio_convert.PEAK_GB_PER_DIR
    assert audio_convert.effective_jobs(6, max_mem_gb=2 * per, cpus=16) == 2
    assert audio_convert.effective_jobs(6, max_mem_gb=per / 10, cpus=16) == 1   # never 0


def test_cli_rejects_bad_jobs(capsys):
    for bad in (["--jobs", "0"], ["--jobs", "x"], ["--max-mem-gb", "0"], ["--max-mem-gb", "-1"]):
        with pytest.raises(SystemExit):
            audio_convert.main(bad)


def test_lock_blocks_second_parallel_run(tmp_path):
    _cache(tmp_path, n_stems=1)
    with open(tmp_path / audio_convert.LOCK_NAME, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError):
            audio_convert.run(tmp_path, apply=True, log=_quiet, jobs=4)
    assert list(tmp_path.rglob("*.wav"))
