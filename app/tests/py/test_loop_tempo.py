import numpy as np

from app.music_brain.analysis.tempo import _beat_regression_bpm, loop_tempo

SR = 22050


def _track(bpm: float, seconds: float = 90.0, seed: int = 0) -> np.ndarray:
    """Kick on every beat, hat on off-beats, a 2-bar accent pattern and noise,
    so the 8-bar lag is a real repeat rather than one click train."""
    rng = np.random.default_rng(seed)
    y = 0.01 * rng.standard_normal(int(seconds * SR))
    beat = 60.0 / bpm
    kick = np.exp(-np.arange(int(0.08 * SR)) / (0.015 * SR)) * np.sin(2 * np.pi * 60 * np.arange(int(0.08 * SR)) / SR)
    hat = 0.3 * rng.standard_normal(int(0.02 * SR)) * np.exp(-np.arange(int(0.02 * SR)) / (0.004 * SR))
    n = int(seconds / beat)
    for k in range(n):
        i = int(k * beat * SR)
        amp = 1.0 if k % 8 in (0, 3, 6) else 0.6
        y[i:i + len(kick)] += amp * kick[: len(y) - i]
        j = int((k + 0.5) * beat * SR)
        if j < len(y):
            y[j:j + len(hat)] += hat[: len(y) - j]
    return y.astype(np.float32)


def test_binned_tempo_is_corrected_to_the_loop_tempo():
    y = _track(125.0)
    beats = np.arange(0, 90, 60 / 125.0)
    assert abs(loop_tempo(y, SR, 123.05, beats) - 125.0) < 0.15   # 123.05 = tempogram bin


def test_right_tempo_stays():
    y = _track(128.0, seed=1)
    beats = np.arange(0, 90, 60 / 128.0)
    assert abs(loop_tempo(y, SR, 128.0, beats) - 128.0) < 0.15


def test_regression_ignores_other_metrical_level():
    beats = np.arange(0, 90, 60 / 125.0)
    assert _beat_regression_bpm(123.05, beats) > 124.9
    assert _beat_regression_bpm(62.5, beats) == 62.5            # 2x away: not the same level
    assert _beat_regression_bpm(120.0, beats[:10]) == 120.0     # too few beats


def test_short_clip_keeps_bpm():
    assert loop_tempo(_track(125.0, seconds=10), SR, 123.05, np.arange(0, 10, 0.48)) == 123.05
