"""Learned in-song moves: the node check for the pure planners / runtime (learned-moves.js) and the
server half (techniques.learned_moves, GET /api/learned/moves) that parses the learned store."""

import shutil
import subprocess
from pathlib import Path

import pytest

from app.music_brain.learning import set_learner as sl
from app.music_brain.matching import techniques as tq

NODE = shutil.which("node")
CHECK = Path(__file__).parents[1].joinpath("js", "learned_moves_check.js")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_learned_moves_node_check():
    res = subprocess.run([NODE, str(CHECK)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr or res.stdout


def _obs(kind, **detail):
    return sl.Observation(kind, set_id="s", at=10.0, track_a="A", detail=detail)


def test_learned_moves_read_the_sightings(tmp_path):
    path = tmp_path / "learned.json"
    sl.merge([
        _obs("vocal_loop", set_span=[10, 24], source_lines=[[1.0, 6.0], [1.0, 6.0], [1.0, 7.0]], repeats=2),
        _obs("vocal_loop", set_span=[50, 60], source_lines=[[1.0, 4.0], [1.0, 4.0]], repeats=1),
        _obs("vocal_chop", set_span=[5, 17], jumps=4, fragments=[[1.0, 2.2], [3.0, 4.4], [9.0, 10.3]]),
        _obs("vocal_resequence", set_span=[9, 30], source_lines=[[1.0, 5.0], [9.0, 14.0]]),
    ], path=path)
    moves = tq.learned_moves(sl.load_learned(path))
    assert set(moves) == set(tq.MOVE_KINDS)
    loop = moves["vocal_loop"]
    assert loop["enabled"] and loop["seen"] == 2
    assert loop["params"]["repeats"] == 1.5 and loop["params"]["repeats_max"] == 2
    assert loop["params"]["line_s"] == 5.0            # median of 5, 5, 6, 3, 3
    chop = moves["vocal_chop"]["params"]
    assert chop["frags"] == 3 and chop["jumps"] == 4 and abs(chop["frag_s"] - 1.3) < 1e-9
    assert moves["vocal_resequence"]["params"]["lines"] == 2
    # never sighted -> not enabled, no parameters to invent
    ext = moves["loop_extend"]
    assert not ext["enabled"] and ext["seen"] == 0 and ext["params"]["tempo_gap_max"] is None


def test_learned_moves_honour_disable_and_user_rules(tmp_path):
    path = tmp_path / "learned.json"
    sl.merge([_obs("vocal_chop", set_span=[5, 9], jumps=3, fragments=[[1, 2]]),
              _obs("vocal_loop", set_span=[5, 9], source_lines=[[1, 3], [1, 3]], repeats=1)], path=path)
    sl.add_user_rule("vocal_chop", "never chop the vocal over a rap", path=path)
    sl.add_user_rule("vocal_loop", disable=True, path=path)
    moves = tq.learned_moves(sl.load_learned(path))
    assert moves["vocal_chop"]["enabled"] and moves["vocal_chop"]["rules"] == ["never chop the vocal over a rap"]
    assert moves["vocal_loop"]["disabled"] and not moves["vocal_loop"]["enabled"]


def test_learned_moves_survive_an_empty_or_odd_store():
    assert all(not m["enabled"] for m in tq.learned_moves({}).values())
    odd = {"vocal_loop": {"observations": [None, {"detail": None}, {"detail": {"set_span": "x"}}], "user_rules": [None, "x"]}}
    moves = tq.learned_moves(odd)                       # malformed rows are skipped, nothing raises
    assert moves["vocal_loop"]["seen"] == 2         # the None row is dropped


def test_learned_moves_endpoint(monkeypatch):
    from app.tests.py.testclient_compat import TestClient
    from app.ui import server

    monkeypatch.setattr(tq, "learned_moves", lambda store=None: {"vocal_loop": {"kind": "vocal_loop", "enabled": True}})
    res = TestClient(server.app).get("/api/learned/moves")
    assert res.status_code == 200 and res.json() == {"moves": {"vocal_loop": {"kind": "vocal_loop", "enabled": True}}}
