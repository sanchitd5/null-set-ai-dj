"""Parameters measured from synthetic waveforms: they must differ per track and land where the audio says."""
import numpy as np
import pytest
import soundfile as sf

from app.music_brain import waveform_params as wp

SR = 11025


def _band_noise(seconds, lo, hi, amp, seed=0):
    rng = np.random.RandomState(seed)
    n = int(seconds * SR)
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / SR)
    spec[(f < lo) | (f > hi)] = 0
    y = np.fft.irfft(spec, n)
    return (y / (np.sqrt(np.mean(y ** 2)) + 1e-12) * amp).astype(np.float32)


def _rap(gaps, seconds=32.0, amp=0.1):
    """Voice-band noise with silences at the given (start, end) seconds."""
    y = _band_noise(seconds, 300, 3400, amp)
    for a, b in gaps:
        y[int(a * SR):int(b * SR)] = 0
    return y


def test_gaps_found_where_the_rap_breathes():
    prof = wp.measure(_rap([(8.0, 10.0), (20.0, 21.0)]), SR)
    found = wp.gaps(prof, 0, 32, min_s=0.5)
    assert len(found) == 2
    assert found[0][0] == pytest.approx(8.0, abs=0.5) and found[0][1] == pytest.approx(10.0, abs=0.5)
    assert found[1][0] == pytest.approx(20.0, abs=0.5)
    assert wp.active_share(prof, 0, 8) > 0.95
    assert wp.active_share(prof, 8, 10) < 0.2


def test_hold_bar_follows_this_raps_gaps():
    bar = 2.0
    a = wp.measure(_rap([(21.0, 23.0)]), SR)        # gap in bar 10 and 11 (t 20..24 from a line start of 0)
    b = wp.measure(_rap([(17.0, 19.0)]), SR)        # gap in bar 8 and 9
    bar_a, src_a = wp.pick_hold_bar(a, 0.0, bar, 8, 11)
    bar_b, src_b = wp.pick_hold_bar(b, 0.0, bar, 8, 11)
    assert src_a == src_b == wp.MEASURED
    assert bar_a in (8, 9) and bar_b in (10, 11)     # never a bar with a breath in it
    assert bar_a != bar_b


def test_hold_bar_falls_back_without_a_qualifying_bar():
    assert wp.pick_hold_bar(None, 0.0, 2.0, 8, 11) == (None, wp.FALLBACK)
    silent = wp.measure(np.zeros(SR * 32, np.float32), SR)
    assert wp.pick_hold_bar(silent, 0.0, 2.0, 8, 11) == (None, wp.FALLBACK)


def test_dropout_prefers_the_continuous_stretch():
    bar = 2.0
    prof = wp.measure(_rap([(46.0, 49.0)], seconds=64.0), SR)      # a breath around bars 23-24
    b, src = wp.pick_dropout_bar(prof, 0.0, bar, 20, 29)
    assert src == wp.MEASURED and not (22 <= b <= 24)


def test_rap_offset_differs_between_bass_heavy_and_bright_riff():
    bass_riff = wp.measure(_band_noise(20, 40, 250, 0.2) + _band_noise(20, 300, 3400, 0.02, 1), SR)
    bright_riff = wp.measure(_band_noise(20, 300, 3400, 0.2), SR)
    ob, lb, sb = wp.rap_offsets(wp.mean_db(bass_riff, "db", 0, 20), wp.mean_db(bass_riff, "voice_db", 0, 20))
    oh, lh, sh = wp.rap_offsets(wp.mean_db(bright_riff, "db", 0, 20), wp.mean_db(bright_riff, "voice_db", 0, 20))
    assert sb == sh == wp.MEASURED
    assert ob > oh + 3                                # bass-heavy riff: rap can sit lower overall
    assert lb > lh                                    # and needs a bigger lift to be heard
    lo, hi = wp.RAP_OFFSET_CLAMP_DB
    assert lo <= oh <= ob <= hi
    assert oh == pytest.approx(wp.RAP_UNDER_RIFF_DB - wp.REF_VOICE_LOSS_DB, abs=1.0)   # all-voice-band riff: the 6 dB floor of the reference


def test_rap_offset_reference_riff_reproduces_the_old_constants():
    off, lift, src = wp.rap_offsets(-20.0, -23.0)     # voice band 3 dB under the full level = the reference riff
    assert src == wp.MEASURED
    assert off == pytest.approx(9.0) and 10 ** (lift / 20) == pytest.approx(1.5, abs=0.05)


def test_rap_offset_fallback_marks_itself():
    off, lift, src = wp.rap_offsets(None, None)
    assert (off, src) == (9.0, wp.FALLBACK) and 10 ** (lift / 20) == pytest.approx(1.5, abs=0.05)


def test_sub_corner_follows_where_the_low_end_lives_and_never_breaks_the_kb_floor():
    deep = wp.measure(_band_noise(20, 30, 70, 0.3), SR)             # 808: nothing above 70 Hz
    thick = wp.measure(_band_noise(20, 40, 180, 0.3), SR)           # bass guitar body up to 180 Hz
    cd, sd = wp.sub_corner_hz(deep["low_hz90"])
    ct, st = wp.sub_corner_hz(thick["low_hz90"])
    assert sd == st == wp.MEASURED
    assert cd == 120.0                                # KB floor: the sub band stays single owner
    assert 120.0 < ct <= 200.0
    assert wp.sub_corner_hz(None) == (120.0, wp.FALLBACK)


def test_guest_level_matches_the_host_voice_band():
    quiet_host, _ = wp.guest_level(-20.0, -30.0, 0.9)
    loud_host, _ = wp.guest_level(-20.0, -19.0, 0.9)
    assert quiet_host == pytest.approx(10 ** (-10 / 20), abs=0.01)
    assert loud_host == 1.0                                          # clamped
    assert wp.guest_level(-20.0, -60.0, 0.9)[0] == wp.LEVEL_CLAMP[0]
    assert wp.guest_level(None, -20.0, 0.9) == (0.9, wp.FALLBACK)


def test_profile_is_cached_by_content_and_survives_a_bad_file(tmp_path):
    p = tmp_path / "vocals.wav"
    sf.write(p, _rap([(2.0, 3.0)], seconds=8.0), SR)
    cache = tmp_path / "cache"
    a = wp.profile(p, cache_dir=cache)
    assert a and len(list(cache.iterdir())) == 1
    q = tmp_path / "copy.wav"
    q.write_bytes(p.read_bytes())
    assert wp.profile(q, cache_dir=cache) == a                       # same bytes: same cache entry
    assert len(list(cache.iterdir())) == 1
    assert wp.profile(tmp_path / "missing.wav", cache_dir=cache) is None
    (tmp_path / "junk.wav").write_bytes(b"not audio")
    assert wp.profile(tmp_path / "junk.wav", cache_dir=cache) is None


def test_mean_db_active_only_ignores_the_breaths():
    y = _rap([], seconds=16.0)
    y[int(4 * SR):int(12 * SR)] *= 0.01               # breaths are quiet, not digital silence
    prof = wp.measure(y, SR)
    diluted = wp.mean_db(prof, "voice_db", 0, 16)
    active = wp.mean_db(prof, "voice_db", 0, 16, active_only=True)
    assert active > diluted + 1.0
