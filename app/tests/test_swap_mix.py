"""swap_mix: lead window on the drop line, one-tempo plan, sub ownership and loudness of a synthetic render."""
import numpy as np
import pytest
import soundfile as sf

from app.music_brain.render import swap_mix as sm

BPM = 120.0
BAR = 2.0


def _analysis(drop_at=64.0, dur=200.0):
    phrases = [i * 8 * BAR for i in range(int(dur // (8 * BAR)))]
    times = list(np.arange(0, dur, 1.0))
    curve = [1.0 if drop_at <= t < drop_at + 32 else 0.3 for t in times]
    return {"bpm": BPM, "duration": dur, "phrase_boundaries_8bar": phrases,
            "energy_times": times, "energy_curve": curve}


def test_window_starts_on_drop_line():
    w = sm.pick_window(_analysis())
    assert w == {"start": 64.0, "end": 64.0 + 16 * BAR, "how": "drop line"}


def test_window_needs_room_for_handover():
    w = sm.pick_window(_analysis(drop_at=176.0, dur=200.0))
    assert w["how"] == "strongest groove phrase" and w["end"] + 2 * BAR <= 200.0
    assert sm.pick_window({"bpm": 120, "duration": 20, "phrase_boundaries_8bar": [0.0]}) is None


def test_weak_drop_line_loses_to_stem_groove():
    lv = [0.1] * 100
    lv[64:80] = [1.0] * 16          # bars 64-80 = 128-160 s: the stems' strongest groove
    stem = {"anchor": 0.0, "bar": BAR, "bars": {"drums": lv, "bass": lv, "other": lv}}
    w = sm.pick_window(_analysis(drop_at=64.0), stem)
    assert w["start"] == 128.0 and w["how"] == "strongest groove phrase"
    lv[32:48] = [1.0] * 16          # now the drop line at 64 s carries the groove too
    assert sm.pick_window(_analysis(drop_at=64.0), stem)["start"] == 64.0


def test_target_and_plan():
    assert sm.target_bpm([124.98, 128.0, 127.44, 125.0]) == 126.0
    p = sm.plan([{"bpm": 120.0}, {"bpm": 123.0}], 120.0)
    assert p[0]["pre"] == 0 and p[1]["lead"] == pytest.approx(32.0) and p[1]["pre"] == pytest.approx(28.0)
    assert p[1]["stretch_pct"] == pytest.approx(-2.44, abs=0.01)
    with pytest.raises(ValueError):
        sm.plan([{"bpm": 100.0, "name": "x"}], 120.0)


def _stems(tmp, name, f0, sr=8000, dur=80.0):
    d = tmp / name
    d.mkdir()
    t = np.arange(int(dur * sr)) / sr
    parts = {"bass": 0.3 * np.sin(2 * np.pi * 50 * t), "drums": 0.2 * np.sin(2 * np.pi * 60 * t),
             "other": 0.2 * np.sin(2 * np.pi * f0 * t), "vocals": 0.05 * np.sin(2 * np.pi * 2 * f0 * t)}
    for n, x in parts.items():
        sf.write(d / f"{n}.flac", np.stack([x, x], 1).astype(np.float32), sr)
    return d


def test_render_one_sub_owner_no_gaps(tmp_path):
    songs = [{"name": f"s{i}", "bpm": BPM, "stems": str(_stems(tmp_path, f"s{i}", 400 + 100 * i)),
              "window": {"start": 16.0, "end": 48.0}} for i in range(3)]
    rep = sm.render(songs, BPM, tmp_path / "mix.wav", tmp_path / "tmp")
    assert rep["peak_dbfs"] == pytest.approx(sm.PEAK_DBFS, abs=0.05)
    assert rep["duration"] == pytest.approx(3 * 32 + 4, abs=0.5)
    assert not rep["silent_seconds"]
    assert rep["sub_clash_blocks"] == 0
