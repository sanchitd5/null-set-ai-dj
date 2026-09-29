"""Mashup planner: tempo lock, key gate, vocal-phrase and instrumental-window choice.

Vocal activity is injected (no Demucs), so these run fast.
"""

import pytest

from app.music_brain import mashup
from app.music_brain.analyzer import KeyEstimate, StructureSection, TrackAnalysis


def _track(path, bpm, camelot, duration=240.0, sections=None):
    bar = 240.0 / bpm
    phrases = [i * 8 * bar for i in range(int(duration // (8 * bar)) + 1)]
    return TrackAnalysis(
        path=path,
        duration=duration,
        bpm=bpm,
        phrase_boundaries_8bar=phrases,
        key=KeyEstimate(camelot=camelot, key_name="", is_major=camelot.endswith("B"), confidence=0.8),
        sections=sections or [StructureSection("intro", 0, 20, 0.3), StructureSection("verse", 20, 220, 0.6),
                              StructureSection("outro", 220, duration, 0.3)],
    )


@pytest.fixture
def regions(monkeypatch):
    table = {}
    monkeypatch.setattr(mashup, "vocal_presence_map", lambda p: table[str(p)])
    return table


def test_tempo_lock_same_half_double_and_reject():
    assert mashup.tempo_lock(124, 124) == (1.0, 1.0)
    rate, mult = mashup.tempo_lock(174, 87)
    assert mult == 2.0 and rate == pytest.approx(1.0)
    assert mashup.tempo_lock(128, 124)[0] == pytest.approx(128 / 124)
    assert mashup.tempo_lock(128, 110) is None  # 16% off: vocal would be ~2.6 semitones sharp


def test_key_clash_rejected_without_separating(regions):
    host, guest = _track("h", 124, "8A"), _track("g", 124, "2B")
    calls = []
    plan = mashup.plan_mashup(host, guest, lambda: calls.append("h"), lambda: calls.append("g"))
    assert plan["ok"] is False and "keys clash" in plan["reasons"][0]
    assert calls == []  # Demucs never invoked for an incompatible pair


def test_plan_picks_vocal_phrase_and_instrumental_host_window(regions):
    host, guest = _track("h", 120, "8A"), _track("g", 120, "9A")
    bar = 2.0  # 120 BPM
    regions["host.wav"] = [(20.0, 64.0)]          # host vocal early on
    regions["guest.wav"] = [(80.0, 100.0)]        # guest sings 80-100 s
    plan = mashup.plan_mashup(host, guest, lambda: "host.wav", lambda: "guest.wav", bars=8)
    assert plan["ok"] is True
    assert plan["guest_start"] == pytest.approx(80.0)         # the phrase that is all vocal
    assert plan["guest_duration"] == pytest.approx(8 * bar)
    assert all(e >= 64.0 for e in plan["host_entries"])        # never over the host's own vocal
    assert all(e + 16 <= 220 for e in plan["host_entries"])    # not in the outro
    assert plan["guest_vocal_coverage"] == pytest.approx(1.0)


def test_no_vocal_phrase_in_guest(regions):
    host, guest = _track("h", 120, "8A"), _track("g", 120, "8A")
    regions["host.wav"] = []
    regions["guest.wav"] = [(81.0, 83.0)]  # one short ad-lib only
    plan = mashup.plan_mashup(host, guest, lambda: "host.wav", lambda: "guest.wav")
    assert plan["ok"] is False and "no 8-bar vocal phrase" in plan["reasons"][0]


def test_bars_validated():
    t = _track("h", 120, "8A")
    with pytest.raises(ValueError):
        mashup.plan_mashup(t, t, lambda: "", lambda: "", bars=12)


def test_host_mutable_allows_a_sung_host_phrase(regions):
    """Live stems on the host: a phrase where the host sings qualifies, flagged
    mute_host_vocals; without stems the same pair has no instrumental phrase."""
    host, guest = _track("h", 124, "8A"), _track("g", 124, "8A")
    regions["host.wav"] = [(0.0, 240.0)]       # host sings the whole song
    regions["guest.wav"] = [(0.0, 240.0)]
    plain = mashup.plan_mashup(host, guest, lambda: "host.wav", lambda: "guest.wav", bars=8)
    assert plain["ok"] is False
    muted = mashup.plan_mashup(host, guest, lambda: "host.wav", lambda: "guest.wav", bars=8, host_mutable=True)
    assert muted["ok"] is True and muted["mute_host_vocals"] is True


def test_full_mashup_32_bars(regions):
    host, guest = _track("h", 120, "8A"), _track("g", 120, "8A")
    regions["host.wav"] = []
    regions["guest.wav"] = [(0.0, 240.0)]
    plan = mashup.plan_mashup(host, guest, lambda: "host.wav", lambda: "guest.wav", bars=32)
    assert plan["ok"] and plan["guest_duration"] == pytest.approx(64.0) and plan["mute_host_vocals"] is False


# ---- parameters measured from the audio (waveform_params) -----------------------------------------------

def _band_wav(path, lo, hi, amp, seconds=100, seed=0, sr=11025):
    import numpy as np
    import soundfile as sf

    rng = np.random.RandomState(seed)
    n = seconds * sr
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / sr)
    spec[(f < lo) | (f > hi)] = 0
    y = np.fft.irfft(spec, n)
    sf.write(path, (y / np.sqrt(np.mean(y ** 2)) * amp).astype("float32"), sr)
    return str(path)


def _mix_wav(path, bed_amp, low_hi, seed):
    """A host mix: a voice-band bed at bed_amp over a bass line that reaches low_hi Hz."""
    import numpy as np
    import soundfile as sf

    _band_wav(path.with_name("bed.wav"), 300, 3400, bed_amp, seed=seed)
    _band_wav(path.with_name("bass.wav"), 40, low_hi, 0.2, seed=seed + 1)
    a, sr = sf.read(path.with_name("bed.wav"))
    b, _ = sf.read(path.with_name("bass.wav"))
    sf.write(path, (a + b).astype("float32"), sr)
    return str(path)


def test_guest_gain_and_highpass_follow_the_host_audio(regions, tmp_path):
    secs = [StructureSection("intro", 0, 10, 0.3), StructureSection("verse", 10, 90, 0.6), StructureSection("outro", 90, 100, 0.3)]
    guest_voc = _band_wav(tmp_path / "guest_voc.wav", 300, 3400, 0.05, seed=5)
    plans = {}
    for name, bed, low_hi in (("loud", 0.15, 90), ("quiet", 0.01, 190)):
        d = tmp_path / name
        d.mkdir()
        host_path = _mix_wav(d / "host.wav", bed, low_hi, seed=1)
        host = _track(host_path, 120, "8A", duration=100, sections=secs)
        guest = _track("g", 120, "8A", duration=100, sections=secs)
        regions[str(d / "hv.wav")] = []
        regions[guest_voc] = [(0.0, 100.0)]
        plans[name] = mashup.plan_mashup(host, guest, lambda p=str(d / "hv.wav"): p, lambda: guest_voc, bars=8)
    loud, quiet = plans["loud"], plans["quiet"]
    assert loud["ok"] and quiet["ok"]
    assert loud["param_sources"] == quiet["param_sources"] == {"guest_level": "measured", "hp_hz": "measured"}
    assert len(loud["host_levels"]) == len(loud["host_entries"])
    assert loud["level"] > quiet["level"] + 0.3                 # a quiet host bed asks for a much quieter guest vocal
    assert loud["hp_hz"] == 120.0                                # deep sub: the KB crossover
    assert 130.0 < quiet["hp_hz"] <= 200.0                       # a bass that reaches 190 Hz: the vocal leaves more low end


def test_unmeasurable_audio_keeps_the_old_constants(regions):
    host, guest = _track("h", 120, "8A"), _track("g", 120, "8A")
    regions["host.wav"] = []
    regions["guest.wav"] = [(0.0, 240.0)]
    plan = mashup.plan_mashup(host, guest, lambda: "host.wav", lambda: "guest.wav", bars=8)
    assert plan["level"] == mashup.FALLBACK_LEVEL and plan["hp_hz"] == 120.0
    assert plan["param_sources"] == {"guest_level": "fallback", "hp_hz": "fallback"}
