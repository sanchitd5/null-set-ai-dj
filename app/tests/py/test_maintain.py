"""The one maintenance command (app/music_brain/maintain.py): step order, dry-run writes nothing,
idempotent re-runs, the long-file / missing-backend skips, the running-app guard and the report
shape. Separation, backends and the atlas build are stubbed: no Demucs, no network."""
import json

import pytest

from app.music_brain import maintain as mt
from app.music_brain.audio import stem_service as ss
from app.music_brain.audio.audio_io import write_manifest


def _cache(tmp_path, tracks):
    """tracks: {id: (name, duration_s)} -> a cache dir Library.tracks() sees as uploads."""
    up, an = tmp_path / "uploads", tmp_path / "analysis"
    up.mkdir()
    an.mkdir()
    for tid, (_, dur) in tracks.items():
        (up / f"{tid}.mp3").write_bytes(b"x")
        (an / f"{tid}.v5.json").write_text(json.dumps({"duration": dur}))
    (up / "_names.json").write_text(json.dumps({tid: n for tid, (n, _) in tracks.items()}))
    return tmp_path


def _tree(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size) for p in root.rglob("*"))


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(mt, "app_running", lambda port=None: False)
    monkeypatch.setattr(mt, "backend_state", lambda no_network: {"backend": "local", "ok": True, "note": None})
    monkeypatch.setattr(mt, "separate_one", lambda path: pytest.fail("separation must be stubbed"))


def test_steps_run_in_canonical_order_and_unknown_is_refused():
    assert mt.parse_steps("export,stems,atlas") == ["stems", "atlas", "export"]
    assert mt.parse_steps(None) == list(mt.STEPS)
    with pytest.raises(ValueError, match="nope"):
        mt.parse_steps("stems,nope")


def test_dry_run_writes_nothing_and_counts(tmp_path):
    cache = _cache(tmp_path, {"a1": ("Artist - Short", 200.0), "b2": ("Artist - Album", 3600.0)})
    before = _tree(cache)
    rep = mt.run(list(mt.STEPS), cache=cache, dry_run=True)
    assert _tree(cache) == before
    assert rep["dry_run"] and "saved" not in rep
    st = rep["results"]["stems"]
    assert st["missing"] == 1 and st["upgrade"] == 0 and st["skipped_long"] == ["Artist - Album"]
    assert st["separated"] == 0 and st["cleanup"]["folders"] == []
    assert rep["before"]["tracks"] == 2 and rep["before"]["tracks_without_stems"] == 2
    assert rep["results"]["atlas"]["rules_changed"] is True          # no atlas yet
    assert rep["results"]["labels"]["dry_run"] is True
    assert rep["results"]["review"]["unreviewed_sets"] == 0
    assert set(rep) >= {"steps", "app_running", "backend", "before", "after", "results", "seconds"}


def _stem_set(stems_dir, h, variant):
    d = stems_dir / f"{h}_{variant}"
    d.mkdir(parents=True)
    paths = {n: str(d / f"{n}.flac") for n in ss.FOUR_STEM_NAMES}
    for p in paths.values():
        open(p, "wb").write(b"x" * 100)
    write_manifest(d, paths)
    return d


def test_stems_new_and_upgrade_skip_long_count_failures_rerun_idempotent(tmp_path, monkeypatch):
    cache = _cache(tmp_path, {"a1": ("A - One", 200.0), "b2": ("B - Two", 180.0),
                              "c3": ("C - Set", 5400.0), "d4": ("D - Fast", 190.0)})
    fast = _stem_set(cache / "stems", "d4", "htdemucs")                # fast stems only -> upgrade
    calls = []

    def sep(path):                                                      # what stem_service.separate does
        tid = path.rsplit("/", 1)[1].split(".")[0]
        calls.append(tid)
        if tid == "b2":
            raise RuntimeError("demucs died")
        _stem_set(cache / "stems", tid, "htdemucs_ft")
        ss.prune_non_ft(tid, cache / "stems")

    monkeypatch.setattr(mt, "separate_one", sep)
    rep = mt.run(["stems"], cache=cache, save=False)
    r = rep["results"]["stems"]
    assert (r["missing"], r["upgrade"], r["separated"]) == (2, 1, 2)
    assert [f["name"] for f in r["failed"]] == ["B - Two"] and r["skipped_long"] == ["C - Set"]
    assert sorted(calls) == ["a1", "b2", "d4"] and not fast.exists()
    assert rep["after"]["tracks_without_stems"] == 2
    calls.clear()
    r2 = mt.run(["stems"], cache=cache, save=False)["results"]["stems"]
    assert (r2["missing"], r2["upgrade"]) == (1, 0) and calls == ["b2"]  # only the failed one again


def test_running_app_refuses_stems_and_flac_but_not_locked_steps(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(mt, "app_running", lambda port=None: True)
    for s in mt.STEPS:
        monkeypatch.setitem(mt.STEP_FNS, s, lambda ctx, s=s: seen.append(s) or {})
    rep = mt.run(list(mt.STEPS), cache=tmp_path, save=False)
    assert seen == ["labels", "review", "atlas", "export"]
    assert "app is running" in rep["results"]["stems"]["skipped"]
    assert "app is running" in rep["results"]["flac"]["skipped"]


def test_missing_backend_skips_labels_and_review_with_a_note(tmp_path, monkeypatch):
    note = "no local model answering"
    monkeypatch.setattr(mt, "backend_state", lambda no_network: {"backend": "local", "ok": False, "note": note})
    (tmp_path / "learned_techniques.json").write_text(json.dumps(
        {"k": {"observations": [{"set_id": "s1", "detail": {}}]}}))
    rep = mt.run(["labels", "review"], cache=tmp_path, save=False)
    assert rep["results"]["labels"]["skipped"] == note
    assert rep["results"]["review"]["skipped"] == note and rep["results"]["review"]["unreviewed_sets"] == 1


def test_no_network_skips_the_claudecode_backend(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setenv("AI_REVIEW_BACKEND", "claudecode")
    st = mt.backend_state(no_network=True)
    assert st["ok"] is False and "--no-network" in st["note"]


def test_review_picks_only_sets_no_model_reviewed(tmp_path):
    p = tmp_path / "learned_techniques.json"
    p.write_text(json.dumps({"k": {"observations": [
        {"set_id": "done", "detail": {"ai_rule": "hold the hook"}},
        {"set_id": "done", "detail": {}},
        {"set_id": "new", "detail": {}}]}}))
    assert mt.unreviewed_sets(p) == ["new"]


def test_time_budget_skips_remaining_steps(tmp_path, monkeypatch):
    for s in mt.STEPS:
        monkeypatch.setitem(mt.STEP_FNS, s, lambda ctx: pytest.fail("no step may start"))
    rep = mt.run(["atlas", "export"], cache=tmp_path, max_minutes=1e-9, save=False)
    assert all(r["skipped"] == "time budget reached" for r in rep["results"].values())


def test_failed_step_is_reported_next_step_runs_and_report_is_saved(tmp_path, monkeypatch):
    def boom(ctx):
        raise RuntimeError("atlas lock busy")

    monkeypatch.setitem(mt.STEP_FNS, "atlas", boom)
    monkeypatch.setitem(mt.STEP_FNS, "export", lambda ctx: {"privacy_check": "passed"})
    rep = mt.run(["atlas", "export"], cache=tmp_path)
    assert "atlas lock busy" in rep["results"]["atlas"]["error"]
    assert rep["results"]["export"]["privacy_check"] == "passed"
    saved = json.loads(open(rep["saved"]).read())
    assert saved["results"]["export"]["privacy_check"] == "passed"
    assert rep["saved"].startswith(str(tmp_path / "maintain"))


def test_cli_bad_step_prints_one_json_error(capsys):
    assert mt.main(["--steps", "bogus", "--dry-run"]) == 1
    assert "bogus" in json.loads(capsys.readouterr().out)["error"]
