"""The virtual set is deterministic: replaying one fixture twice gives byte-identical outputs (zero
network), and a reworded prompt still finds the reply recorded for its subject."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _replay(name, out):
    subprocess.run([sys.executable, "-m", "app.sim.virtual_set", "--replay", name, "--out", str(out)],
                   cwd=ROOT, check=True, capture_output=True, timeout=900)
    return out


@pytest.mark.slow
def test_replay_is_deterministic(tmp_path):
    if not (ROOT / "app/sim/fixtures/lib-s1-quick/run.json").exists():
        pytest.skip("fixture not recorded")
    a, b = _replay("lib-s1-quick", tmp_path / "a"), _replay("lib-s1-quick", tmp_path / "b")
    assert json.loads((a / "report.json").read_text())["score"] == json.loads((b / "report.json").read_text())["score"]
    files = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    for rel in files:                    # events.jsonl, net.jsonl, console.jsonl, songs/*/steps.jsonl ... all of it
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), f"{rel} differs between two replays"


def _world(tmp_path):
    from app.sim.world import World

    w = World("library", "t", 1, tmp_path, fixtures_dir=tmp_path)
    w.fx["llm"] = [
        {"kind": "suggest", "sig": "s1", "stable": "suggest|Song A", "cur": "Song A", "reply": "A1"},
        {"kind": "suggest", "sig": "s2", "stable": "suggest|Song A", "cur": "Song A", "reply": "A2"},
        {"kind": "plan", "sig": "p1", "stable": "plan|124.0|8A|126.0|9A", "cur": "", "reply": "P1"},
    ]
    return w


def test_exact_prompt_wins(tmp_path):
    w = _world(tmp_path)
    assert w._replayed("suggest", "s2", "suggest|Song A", "llm")["reply"] == "A2"
    assert w.drift == [] and w.misses == []


def test_reworded_prompt_gets_the_reply_recorded_for_its_subject(tmp_path):
    w = _world(tmp_path)
    # a rule change rewrote the prompt: the signature is new, the subject (song playing / tempo pair) is not
    assert w._replayed("suggest", "new1", "suggest|Song A", "llm", "Song A")["reply"] == "A1"
    assert w._replayed("suggest", "new2", "suggest|Song A", "llm", "Song A")["reply"] == "A2"
    assert w._replayed("plan", "newp", "plan|124.0|8A|126.0|9A", "llm")["reply"] == "P1"
    assert len(w.drift) == 3 and w.misses == []


def test_no_recorded_reply_is_a_miss_and_never_borrows_another_subject(tmp_path):
    w = _world(tmp_path)
    assert w._replayed("suggest", "x", "suggest|Other Song", "llm", "Other Song") is None
    assert w._replayed("plan", "y", "plan|90.0|1A|91.0|2A", "llm") is None
    assert [m["key"] for m in w.misses] == ["suggest|Other Song", "plan|90.0|1A|91.0|2A"]


def test_call_ordinal_counts_per_subject(tmp_path):
    w = _world(tmp_path)
    sys_p, user = "S", 'NOW PLAYING: "Song A" by X | 120 BPM'
    a = w._llm_call_info(sys_p, user)
    w._llm_call_info(sys_p, user)
    other = w._llm_call_info(sys_p, 'NOW PLAYING: "Song B" by Y | 120 BPM')
    assert (a["ord"], other["ord"]) == (0, 0)             # another subject's calls do not shift the stub's noise
    assert w._llm_call_info(sys_p, user)["ord"] == 2
