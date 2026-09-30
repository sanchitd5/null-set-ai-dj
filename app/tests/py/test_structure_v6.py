"""v6 analysis structure (analysis/structure.py): phrase-grid sections, drops and
the main drop on a synthetic track with a known shape, v5 compatibility, and the
readers (drop-line gate, exit floor, entry drops) that consume the new data."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.music_brain.analysis import analyzer, structure
from app.music_brain.render import blend, drop_line

CHECK = Path(__file__).parents[1] / "js" / "structure_v6_check.js"
BPM = 120.0           # bar = 2 s, 8-bar phrase = 16 s
BAR = 240.0 / BPM
# intro 16 bars, build 8, drop 16, breakdown 16, drop 16, outro 16 (bars -> mix energy)
SHAPE = [(16, 0.3), (8, 0.5), (16, 0.85), (16, 0.2), (16, 1.0), (16, 0.3)]
DUR = sum(b for b, _ in SHAPE) * BAR   # 176 s


def _v5_record():
    times, curve, t = [], [], 0.0
    for bars, e in SHAPE:
        end = t + bars * BAR
        while t < end - 1e-9:
            times.append(t)
            curve.append(e)
            t += 1.0
    return {
        "path": "synthetic.mp3", "duration": DUR, "bpm": BPM,
        "beat_times": [i * BAR / 4 for i in range(int(DUR / BAR * 4))],
        "downbeat_times": [i * BAR for i in range(int(DUR / BAR))],
        "phrase_boundaries_8bar": [i * 8 * BAR for i in range(int(DUR / (8 * BAR)))],
        "phrase_boundaries_16bar": [i * 16 * BAR for i in range(int(DUR / (16 * BAR)))],
        "key": None, "energy_curve": curve, "energy_times": times,
        # v5 flicker: 1 s slivers
        "sections": [{"label": "drop" if i % 2 else "verse", "start": float(i), "end": float(i + 1),
                      "energy": 0.5} for i in range(int(DUR))],
        "vocal_active_regions": [],
    }


def _labels(secs):
    return [(s["label"], s["start"], s["end"]) for s in secs]


def test_mix_rule_sections_drops_main():
    r = structure.refine(_v5_record())
    assert _labels(r["sections"]) == [
        ("intro", 0.0, 32.0), ("build", 32.0, 48.0), ("drop", 48.0, 80.0),
        ("breakdown", 80.0, 112.0), ("drop", 112.0, 144.0), ("outro", 144.0, DUR)]
    assert all(s["end"] - s["start"] >= 8 * BAR - 1e-6 for s in r["sections"])
    assert [(d["start"], d["end"]) for d in r["drops"]] == [(48.0, 80.0), (112.0, 144.0)]
    assert all(d["source"] == "mix" for d in r["drops"])
    assert r["main_drop"]["start"] == 112.0
    assert r["structure"] == {"version": structure.STRUCTURE_VERSION, "stems": False}


def test_stem_rule_picks_the_groove_not_a_loud_synth_intro(monkeypatch):
    rec = _v5_record()
    # a loud synth intro: the mix curve is as loud as the drop, but drums+bass are quiet
    rec["energy_curve"] = [0.9 if t < 32 else e for t, e in zip(rec["energy_times"], rec["energy_curve"])]
    groove = [-30, -30, -20, -10, -10, -35, -35, -9, -9, -30, -30]   # dB per phrase
    monkeypatch.setattr(structure, "stem_phrase_db", lambda stems, edges: list(groove[:len(edges)]))
    r = structure.refine(rec, {"drums": "d.flac", "bass": "b.flac"})
    assert [d["start"] for d in r["drops"]] == [48.0, 112.0]
    assert all(d["source"] == "stems" for d in r["drops"])
    assert r["main_drop"]["start"] == 112.0
    assert r["structure"]["stems"] is True


def test_no_drop_on_a_flat_song():
    rec = _v5_record()
    rec["energy_curve"] = [0.5] * len(rec["energy_curve"])
    r = structure.refine(rec)
    assert r["drops"] == [] and r["main_drop"] is None
    assert len(r["sections"]) >= 1


def test_v5_record_still_reads():
    rec = _v5_record()
    ta = analyzer._from_dict(dict(rec))
    assert ta.drops == [] and ta.main_drop is None
    # no drops -> the v5 energy rule, unchanged
    assert blend.track_drop_lines(rec, BAR) == blend.drop_lines(
        rec["phrase_boundaries_8bar"], rec["energy_times"], rec["energy_curve"], BAR)
    assert blend.main_drop_time(rec) is None


def test_v6_record_feeds_readers():
    rec = structure.refine(_v5_record())
    ta = analyzer._from_dict(json.loads(json.dumps(rec)))
    assert [t for t, _, _ in blend.track_drop_lines(ta, BAR)] == [48.0, 112.0]
    assert blend.main_drop_time(ta) == 112.0
    # the drop-line gate sees the v6 drops
    spans = drop_line.drop_spans(rec)
    assert (48.0, 48.0 + drop_line.DROP_WINDOW_BARS * BAR) in spans
    assert (112.0, 112.0 + drop_line.DROP_WINDOW_BARS * BAR) in spans
    # the exit floor waits for the main drop plus DROP_HOLD_BARS
    min_exit, _, _ = blend.min_exit_floor(ta, 0.0, 0.0, DUR, 16)
    assert min_exit == pytest.approx(112.0 + blend.DROP_HOLD_BARS * BAR)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_twins_read_v6_drops(tmp_path):
    v6 = structure.refine(_v5_record())
    spans = sorted([round(a, 3), round(b, 3)] for a, b in drop_line.drop_spans(v6))
    fx = tmp_path / "fx.json"
    fx.write_text(json.dumps({"v6": v6, "v5": _v5_record(), "bar": BAR, "spans": spans,
                              "lines": [round(t, 3) for t, _, _ in blend.track_drop_lines(v6, BAR)]}))
    r = subprocess.run([shutil.which("node"), str(CHECK), str(fx)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_reanalyse_command_is_idempotent(tmp_path, monkeypatch):
    from app.music_brain import config
    from app.music_brain.analysis import reanalyse

    monkeypatch.setattr(config, "ANALYSIS_CACHE_DIR", tmp_path)
    monkeypatch.setattr(analyzer, "STEMS_CACHE_DIR", tmp_path / "stems")
    (tmp_path / "abc.v5.json").write_text(json.dumps(_v5_record()))
    (tmp_path / "bad.v5.json").write_text("{not json")
    dry = reanalyse.run(True, jobs=1, dry=True)
    assert dry["done"] == 1 and dry["failed"] == 1 and not (tmp_path / "abc.v6.json").exists()
    first = reanalyse.run(True, jobs=1)
    assert first["done"] == 1 and json.loads((tmp_path / "abc.v6.json").read_text())["main_drop"]["start"] == 112.0
    (tmp_path / "bad.v5.json").unlink()
    assert reanalyse.run(True, jobs=1)["done"] == 0      # resumable: done songs are skipped


def test_upgrade_v5_to_v6_on_disk(tmp_path, monkeypatch):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"not really audio")
    monkeypatch.setattr(analyzer, "ANALYSIS_CACHE_DIR", tmp_path)
    monkeypatch.setattr(analyzer, "STEMS_CACHE_DIR", tmp_path / "stems")
    digest = analyzer._file_hash(audio)
    v5 = tmp_path / f"{digest}.v5.json"
    v5.write_text(json.dumps(_v5_record()))
    assert analyzer.upgrade_to_current(audio) is True
    v6 = json.loads((tmp_path / f"{digest}.v6.json").read_text())
    assert v6["main_drop"]["start"] == 112.0
    assert v5.exists()                               # the v5 record is left for older readers
    assert analyzer.upgrade_to_current(audio) is True  # idempotent
