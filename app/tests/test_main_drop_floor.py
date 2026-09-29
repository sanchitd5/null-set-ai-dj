from types import SimpleNamespace

from app.music_brain import blend


def _track(duration, bpm, energy_fn):
    bar = 240.0 / bpm
    phrases = [i * 8 * bar for i in range(int(duration // (8 * bar)) + 1)]
    times = [t * 0.5 for t in range(int(duration * 2))]
    return SimpleNamespace(bpm=bpm, duration=duration, phrase_boundaries_8bar=phrases,
                           energy_times=times, energy_curve=[energy_fn(t) for t in times])


def test_short_track_keeps_its_late_main_drop():
    # Anyma - Atoma shape: 2:30, a small early lift, the real drop at ~1:40 until ~2:25
    bpm = 124.0
    bar = 240.0 / bpm
    first, main = 2 * 8 * bar, 6 * 8 * bar                     # phrase lines: ~0:31 lift, ~1:33 drop
    def e(t):
        if main <= t < 145: return 0.95
        if first <= t < first + 8 * bar: return 0.55
        return 0.2
    a = _track(150.0, bpm, e)
    lines = [t for t, _, _ in blend.drop_lines(a.phrase_boundaries_8bar, a.energy_times, a.energy_curve, bar)]
    assert any(abs(t - main) < 1e-6 for t in lines)
    min_exit, lo, hi = blend.min_exit_floor(a, 0.0, 30.0, 90.0, 16)
    assert min_exit is not None and min_exit >= main + 8 * bar - 1e-6   # never before the drop has played
    assert min_exit >= 135                                                # the drop run plays out (to its last phrase line)
    assert lo >= min_exit and hi >= lo


def test_long_track_first_drop_floor_unchanged():
    bpm = 128.0
    bar = 240.0 / bpm
    d = 4 * 8 * bar
    a = _track(300.0, bpm, lambda t: 0.9 if d <= t < 150 else 0.2)    # a mid-song drop, not a finale
    min_exit, lo, _ = blend.min_exit_floor(a, 0.0, 30.0, 200.0, 16)
    assert abs(min_exit - (d + blend.DROP_HOLD_BARS * bar)) < 1e-6


def test_drop_after_a_breakdown_no_louder_than_the_intro():
    # the real Anyma - Atoma analysis: loud synth intro, breakdown 1:00-1:45, the drop, fade out
    bpm, L = 130.0, 8 * 240.0 / 130.0
    means = [0.78, 0.78, 0.75, 0.77, 0.34, 0.45, 0.33, 0.5, 0.75, 0.72, 0.1]
    a = _track(150.0, bpm, lambda t: means[min(len(means) - 1, int(t // L))])
    min_exit, _, _ = blend.min_exit_floor(a, 0.0, 40.0, 100.0, 16)
    assert min_exit is not None and min_exit >= 8 * L       # not before the drop out of the breakdown has played
