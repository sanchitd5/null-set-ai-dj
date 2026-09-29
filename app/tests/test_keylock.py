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


def _riff_folder(tmp_path, name, other):
    """A rendered-riff folder: 4 stems, `other` is the riff. 22.05 kHz, 12 s, groove 0-8 s, solo 8-12 s."""
    import soundfile as sf

    folder = tmp_path / name
    folder.mkdir()
    for n in keylock.STEMS:
        sf.write(folder / f"{n}.wav", other if n == "other" else 0.01 * other, 22050)
    return folder


def _band(lo, hi, amp, seed):
    import numpy as np

    rng = np.random.RandomState(seed)
    n = 22050 * 12
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / 22050)
    spec[(f < lo) | (f > hi)] = 0
    y = np.fft.irfft(spec, n)
    return (y / np.sqrt(np.mean(y ** 2)) * amp).astype(np.float32)


def test_balance_derives_the_rap_offset_from_each_riffs_spectrum(tmp_path):
    plan = {"ratio": 1.0, "a_groove": [0.0, 8.0], "a_solo": [8.0, 12.0]}
    meta = {"window_start": 0.0, "ratio": 1.0}
    bass_riff = _riff_folder(tmp_path, "bass", _band(40, 250, 0.2, 1) + _band(300, 3400, 0.02, 2))
    bright_riff = _riff_folder(tmp_path, "bright", _band(300, 3400, 0.2, 3))
    lv_bass = keylock.stretched_levels(bass_riff, meta, plan)
    lv_bright = keylock.stretched_levels(bright_riff, meta, plan)
    assert lv_bass["a_riff_voice_db"] < lv_bass["a_riff_db"] - 8          # measured: the riff is mostly low end
    assert abs(lv_bright["a_riff_voice_db"] - lv_bright["a_riff_db"]) < 1.5
    b = {"b_mix_db": -16.0, "b_vocals_db": -20.0, "b_bass_db": -19.0}
    gb = keylock.balance({**lv_bass, "a_mix_db": -22.0}, b)
    gh = keylock.balance({**lv_bright, "a_mix_db": -22.0}, b)
    assert gb["param_source"] == gh["param_source"] == "measured"
    assert gb["rap_under_riff_db"] > gh["rap_under_riff_db"] + 3          # the rap sits differently per riff
    assert gb["rap_lift"] > gh["rap_lift"] > 1.2


def test_balance_without_a_voice_band_measurement_keeps_the_old_constants():
    g = keylock.balance({"a_mix_db": -22.0, "a_riff_db": -26.0}, {"b_mix_db": -16.0, "b_vocals_db": -20.0, "b_bass_db": -19.0})
    assert g["param_source"] == "fallback" and g["rap_under_riff_db"] == 9.0
    assert g["rap_lift"] == pytest.approx(1.5, abs=0.05)


def test_rap_moves_are_placed_on_each_raps_own_lines():
    from app.music_brain import waveform_params as wp
    import numpy as np

    def rap(gaps, seed=0):
        rng = np.random.RandomState(seed)
        n = 11025 * 80
        spec = np.fft.rfft(rng.standard_normal(n))
        f = np.fft.rfftfreq(n, 1 / 11025)
        spec[(f < 300) | (f > 3400)] = 0
        y = np.fft.irfft(spec, n).astype(np.float32)
        y *= 0.1 / np.sqrt(np.mean(y ** 2))
        for a, b in gaps:
            y[int(a * 11025):int(b * 11025)] = 0
        return wp.measure(y, 11025)

    tl = keylock.timeline(32)
    bar = 2.0
    a, srcs_a = keylock.measured_lines(tl, rap([(16.0, 20.0), (48.0, 50.0)]), 0.0, bar)   # breathes in bars 8-9 and 24
    b, srcs_b = keylock.measured_lines(tl, rap([(20.0, 24.0), (52.0, 56.0)]), 0.0, bar)   # breathes in bars 10-11 and 26-27
    assert srcs_a == srcs_b == {"hold_on_bar": "measured", "dropout_bar": "measured"}
    rel = lambda t: [h - 24 for h in t["holds"]]                                          # noqa: E731  (bars from the mashup line)
    assert rel(a)[0] in (10, 11) and rel(a)[1] in (25, 26, 27)         # never a bar with a breath in it
    assert rel(b)[0] in (8, 9) and rel(b)[1] in (24, 25)
    assert a["holds"] != b["holds"]
    assert a["dropout"] - 24 >= 25                                     # A's rap breathes at bar 24: the 2-bar drop out is elsewhere
    assert 24 <= b["dropout"] - 24 <= 30 and (b["dropout"] - 24) not in (25, 26, 27)
    fixed, fsrc = keylock.measured_lines(tl, None, 0.0, bar)
    assert fixed["holds"] == [24 + 11, 24 + 16 + 11] and fixed["dropout"] == tl["blend"] - 2
    assert set(fsrc.values()) == {"fallback"}


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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_marquee_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("marquee_check.js"))],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout


def test_tempo_render_gate_matches_client_8pct_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(keylock, "render_tempo", lambda key, stems, ratio: None)
    monkeypatch.setattr(keylock, "KEYLOCK_DIR", tmp_path)
    assert keylock.ensure_tempo("b" * 64, {}, 124.0, 110.0)["state"].startswith("error")   # 11 %: refused
    assert keylock.ensure_tempo("b" * 64, {}, 100.0, 115.0)["state"].startswith("error")   # 15 %: refused
    assert not keylock.ensure_tempo("b" * 64, {}, 100.0, 108.0)["state"].startswith("error")  # 8 %: ok
    assert keylock.MAX_TEMPO_STRETCH < 0.09
