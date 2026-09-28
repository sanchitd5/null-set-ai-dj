import shutil
import subprocess
from pathlib import Path

import pytest

from app.music_brain import keylock


def _an(bpm, duration, phrases=None):
    bar = 240.0 / bpm
    return {"bpm": bpm, "duration": duration, "downbeat_times": [i * bar for i in range(int(duration / bar))],
            "phrase_boundaries_8bar": phrases or [i * 8 * bar for i in range(int(duration / (8 * bar)))]}


A, B = _an(122.88, 212.6), _an(140.02, 344.6)
GROOVES = [(15.95, 62.86), (94.16, 140.97)]
BREAKS = [(62.86, 78.60), (140.97, 156.57)]


def test_picks_the_usb002_groove_and_breakdown():
    p = keylock.choose(A, B, GROOVES, BREAKS, [78.0, 167.0])
    assert p["ok"]
    assert p["a_groove"][1] == pytest.approx(62.86, abs=0.6) and p["a_groove"][0] == pytest.approx(62.86 - 16 * 240 / 122.88, abs=0.6)
    assert p["ratio"] == pytest.approx(122.88 / 140.02)
    assert p["b_start"] == pytest.approx(p["b_entry"] - keylock.B_LEAD_BARS * 240 / 140.02)
    assert p["b_start"] >= 0


def test_not_before_moves_to_the_later_groove():
    p = keylock.choose(A, B, GROOVES, BREAKS, [78.0, 167.0], not_before=40.0)
    assert p["ok"] and p["a_groove"][1] == pytest.approx(140.97, abs=0.6)


def test_b_enters_at_its_rap_and_needs_song_after_it():
    p = keylock.choose(A, B, GROOVES, BREAKS, [20.0])
    assert p["ok"] and p["b_start"] == pytest.approx(p["b_entry"]) and p["b_bass_intro"] is False
    late = keylock.choose(A, B, GROOVES, BREAKS, [320.0])     # 24 s of song left after the rap
    assert not late["ok"] and any("rap" in r for r in late["reasons"])


def test_groove_must_run_into_the_breakdown():
    p = keylock.choose(A, B, [(15.95, 40.0)], BREAKS, [78.0])
    assert not p["ok"] and any("groove" in r for r in p["reasons"])




@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_riff_schedule_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("riff_check.js"))],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(not keylock.available(), reason="rubberband not installed")
def test_render_is_key_locked(tmp_path, monkeypatch):
    import numpy as np
    import soundfile as sf

    sr = 44100
    t = np.arange(int(6 * sr)) / sr
    tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    stems = {}
    for n in keylock.STEMS:
        stems[n] = str(tmp_path / f"{n}.wav")
        sf.write(stems[n], np.stack([tone, tone], 1), sr)
    monkeypatch.setattr(keylock, "KEYLOCK_DIR", tmp_path / "kl")
    plan = {"ratio": 0.8, "a_groove": [1.0, 3.0], "a_solo": [3.0, 4.0]}
    keylock.render("k1", stems, plan)
    y, sr2 = sf.read(tmp_path / "kl" / "k1" / "other.wav")
    assert abs(len(y) / sr2 - (4.0 + 1.0 - 0.0) * 0.8) < 0.1                  # window [0, 5] s stretched x0.8
    spec = np.abs(np.fft.rfft(y[:, 0] * np.hanning(len(y))))
    peak_hz = np.argmax(spec) * sr2 / len(y)
    assert abs(peak_hz - 440) < 3                                             # pitch unchanged


def test_balance_puts_rap_under_riff_and_matches_loudness():
    meta = {"a_mix_db": -22.0, "a_riff_db": -26.0}
    b = {"b_mix_db": -16.0, "b_vocals_db": -20.0, "b_bass_db": -19.0}
    g = keylock.balance(meta, b)
    assert g["a_gain_db"] == pytest.approx(6.0)                          # A lifted to B's loudness
    riff = -26.0 + 6.0
    assert 20 * __import__("math").log10(g["b_vocals"]) + -20.0 == pytest.approx(riff - keylock.RAP_UNDER_RIFF_DB)
    assert g["b_bass"] < 1 and g["b_vocals"] < 1
    loud_a = keylock.balance({"a_mix_db": -10.0, "a_riff_db": -12.0}, b)
    assert loud_a["a_gain_db"] == 0.0                                     # never turned down, never boosted past 8 dB


def test_balance_never_lifts_a_past_its_peak_headroom():
    g = keylock.balance({"a_mix_db": -22.0, "a_riff_db": -26.0, "a_peak_db": -5.0},
                        {"b_mix_db": -10.0, "b_vocals_db": -20.0, "b_bass_db": -19.0})
    assert g["a_gain_db"] == pytest.approx(2.0)   # wanted +8, peak -5 dBFS leaves +2


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_auto_sampler_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("auto_sampler_check.js"))],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dsp_worker_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("dsp_worker_check.js"))],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_mascot_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("mascot_check.js"))],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_vibe_ui_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("vibe_ui_check.js"))],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_remix_mode_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("remix_mode_check.js"))],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stem_liveness_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("stem_liveness_check.js"))],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_merge_silence_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("merge_silence_check.js"))],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_visuals_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("visuals_check.js"))],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout


def test_tempo_key_rounds_and_ratio():
    key, t = keylock.tempo_key("a" * 64, 122.88, 140.02)
    assert t == 140.0 and key.startswith("t" + "a" * 24) and "p" in key
    assert keylock.ensure_tempo("a" * 64, {}, 100.0, 130.0)["state"].startswith("error")   # 30 %: refused


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_master_watch_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("master_watch_check.js"))],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout
