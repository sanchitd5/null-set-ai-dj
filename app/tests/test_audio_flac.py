"""FLAC cache: writers, readers with WAV fallback, the v2 manifest and the in-place converter.

Synthetic audio only (no Demucs, no network)."""

import json

import numpy as np
import pytest
import soundfile as sf

from app.music_brain import audio_convert, audio_io, keylock_cache, stem_service

SR = 44100
STEMS = ("vocals", "drums", "bass", "other")


def _noise(n=SR, seed=0, dtype="int16"):
    rng = np.random.RandomState(seed)
    x = rng.randn(n, 2) * 0.2
    return (x * 32767).astype("int16") if dtype == "int16" else x.astype("float32")


def _stem_dir(root, name="abc_htdemucs_ft", seed=0, manifest=True):
    d = root / "stems" / name
    d.mkdir(parents=True)
    paths = {}
    for i, n in enumerate(STEMS):
        p = d / f"{n}.wav"
        sf.write(p, _noise(SR + 17, seed + i), SR, subtype="PCM_16")
        paths[n] = str(p)
    if manifest:
        (d / "manifest.json").write_text(json.dumps(paths))                   # v1: bare {name: path}
    return d, paths


def _keylock_dir(root, key="tabc_120p0"):
    d = root / "keylock" / key
    d.mkdir(parents=True)
    for i, n in enumerate(STEMS):
        x = _noise(SR // 2, 10 + i, "float32")
        x[0, 0] = 0.999
        sf.write(d / f"{n}.wav", x, SR, subtype="FLOAT")
    (d / "meta.json").write_text(json.dumps({"ratio": 1.0}))
    return d


# ---- audio_io ---------------------------------------------------------------

def test_flac_round_trip_bit_exact_16(tmp_path):
    x = _noise(SR + 3)
    p = audio_io.write_flac(tmp_path / "a.flac", x, SR)
    y, sr = sf.read(p, dtype="int16", always_2d=True)
    assert sr == SR and y.shape == x.shape and np.array_equal(x, y)           # exact length, sample 0 start
    assert not list(tmp_path.glob("*.tmp*"))


def test_flac_24_within_tolerance(tmp_path):
    x = _noise(SR, 3, "float32")
    wav = tmp_path / "k.wav"
    sf.write(wav, x, SR, subtype="FLOAT")
    audio_io.encode_file(wav, tmp_path / "k.flac")
    assert sf.info(str(tmp_path / "k.flac")).subtype == "PCM_24"
    y, _ = sf.read(tmp_path / "k.flac", dtype="float64", always_2d=True)
    assert y.shape == x.shape and np.max(np.abs(np.clip(x, -1, 1) - y)) < 1e-6


def test_readers_prefer_flac_then_wav(tmp_path):
    sf.write(tmp_path / "bass.wav", _noise(100), SR)
    assert audio_io.stem_file(tmp_path, "bass") == tmp_path / "bass.wav"
    assert audio_io.resolve(tmp_path / "bass.flac") == tmp_path / "bass.wav"
    audio_io.write_flac(tmp_path / "bass.flac", _noise(100), SR)
    assert audio_io.stem_file(tmp_path, "bass") == tmp_path / "bass.flac"
    assert audio_io.resolve(tmp_path / "bass.wav") == tmp_path / "bass.wav"   # an existing path wins
    assert audio_io.stem_file(tmp_path, "drums") is None
    assert audio_io.media_type("x.flac") == "audio/flac" and audio_io.media_type("x.wav") == "audio/wav"


def test_old_v1_manifest_still_loads_and_follows_conversion(tmp_path):
    d, paths = _stem_dir(tmp_path)
    assert stem_service._load_from_cache(d) == paths                          # v1 WAV manifest
    for n in STEMS:                                                           # converted behind its back
        audio_io.encode_file(d / f"{n}.wav", d / f"{n}.flac")
        (d / f"{n}.wav").unlink()
    got = stem_service._load_from_cache(d)
    assert got == {n: str(d / f"{n}.flac") for n in STEMS}


def test_v2_manifest_written(tmp_path):
    d, paths = _stem_dir(tmp_path)
    audio_io.write_manifest(d, paths)
    body = json.loads((d / "manifest.json").read_text())
    assert body["version"] == audio_io.MANIFEST_VERSION and body["format"] == "wav"
    assert stem_service._load_from_cache(d) == paths


def test_keylock_size_counts_flac(tmp_path):
    d = _keylock_dir(tmp_path)
    before = keylock_cache._size(d)
    for n in STEMS:
        audio_io.encode_file(d / f"{n}.wav", d / f"{n}.flac")
        (d / f"{n}.wav").unlink()
    after = keylock_cache._size(d)
    assert 0 < after < before


# ---- converter --------------------------------------------------------------

def _snapshot(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size) for p in root.rglob("*") if p.is_file())


def test_dry_run_changes_nothing(tmp_path):
    _stem_dir(tmp_path)
    _keylock_dir(tmp_path)
    snap = _snapshot(tmp_path)
    res = audio_convert.run(tmp_path, log=lambda s: None)
    assert res["projected"] and res["files"] == 8 and res["saved_bytes"] > 0
    assert _snapshot(tmp_path) == snap


def test_apply_converts_verifies_and_is_idempotent(tmp_path):
    d, paths = _stem_dir(tmp_path)
    k = _keylock_dir(tmp_path)
    orig = {n: sf.read(p, dtype="int16")[0] for n, p in paths.items()}
    res = audio_convert.run(tmp_path, apply=True, log=lambda s: None)
    assert res["converted"] == 8 and not res["failed"] and res["bytes_after"] < res["bytes_before"]
    assert not list(tmp_path.rglob("*.wav"))
    m = json.loads((d / "manifest.json").read_text())
    assert m["format"] == "flac" and m["stems"] == {n: str(d / f"{n}.flac") for n in STEMS}
    for n in STEMS:
        assert np.array_equal(sf.read(d / f"{n}.flac", dtype="int16")[0], orig[n])
        assert sf.info(str(k / f"{n}.flac")).subtype == "PCM_24"
    assert json.loads((k / "meta.json").read_text())["format"] == "flac"
    again = audio_convert.run(tmp_path, apply=True, log=lambda s: None)
    assert again["files"] == 0 and again["converted"] == 0 and not again["failed"]


def test_mismatch_keeps_wav(tmp_path, monkeypatch):
    d, paths = _stem_dir(tmp_path)
    real = audio_convert.verify
    monkeypatch.setattr(audio_convert, "verify",
                        lambda w, f, s: "injected" if w.name == "bass.wav" else real(w, f, s))
    res = audio_convert.run(tmp_path, apply=True, log=lambda s: None)
    assert len(res["failed"]) == 1 and "bass.wav" in res["failed"][0]["file"]
    assert (d / "bass.wav").exists() and not (d / "bass.flac").exists()
    assert not (d / "drums.wav").exists()
    got = stem_service._load_from_cache(d)                                    # mixed dir still fully readable
    assert got["bass"].endswith("bass.wav") and got["drums"].endswith("drums.flac")


def test_limit_and_only(tmp_path):
    _stem_dir(tmp_path, "a_htdemucs_ft", 0)
    _stem_dir(tmp_path, "b_htdemucs_ft", 20)
    _keylock_dir(tmp_path)
    res = audio_convert.run(tmp_path, apply=True, only="stems", limit=1, log=lambda s: None)
    assert res["dirs"] == 1 and res["converted"] == 4
    assert len(list(tmp_path.rglob("*.wav"))) == 8                            # other stem dir + keylock untouched


def test_skips_unfinished_and_inflight(tmp_path):
    _stem_dir(tmp_path, manifest=False)                                       # separation not finished
    k = _keylock_dir(tmp_path, "tabc_120p0.tmp")                              # render in flight
    res = audio_convert.run(tmp_path, apply=True, log=lambda s: None)
    assert res["files"] == 0 and (k / "bass.wav").exists()


def test_lock_blocks_second_run(tmp_path):
    import fcntl

    _stem_dir(tmp_path)
    with open(tmp_path / audio_convert.LOCK_NAME, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError):
            audio_convert.run(tmp_path, apply=True, log=lambda s: None)
