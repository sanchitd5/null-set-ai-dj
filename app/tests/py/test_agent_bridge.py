"""Tests for music_brain.agent_bridge (Phase 4: Python API + CLI).

Acceptance criterion 5 (spec section 5): running
`python -m music_brain.agent_bridge match <song1> <song2>` must output
structured JSON suitable for another program to consume.
"""

import json
import subprocess
import sys

import pytest

from app.music_brain.agent_bridge import analyze, list_recipes, main, match, preview
from app.music_brain.config import ROOT_DIR
from app.music_brain.knowledge_parser import KnowledgeParser

SAMPLE_A = ROOT_DIR / "data" / "songs" / "input.mp3"
SAMPLE_B = ROOT_DIR / "data" / "songs" / "input2.mp3"

pytestmark = pytest.mark.skipif(
    not (SAMPLE_A.exists() and SAMPLE_B.exists()), reason="sample tracks not present"
)


# --- Python API ---------------------------------------------------------------

def test_analyze_returns_serializable_dict():
    payload = analyze(str(SAMPLE_A))
    json.dumps(payload)  # raises if not serializable
    assert "bpm" in payload and payload["bpm"] > 0
    assert "key" in payload


def test_match_returns_top_n_candidates():
    payload = match(str(SAMPLE_A), str(SAMPLE_B), top_n=2)
    assert len(payload["candidates"]) == 2
    assert payload["track_a"] == str(SAMPLE_A)
    json.dumps(payload)


def test_preview_with_explicit_recipe(tmp_path):
    out_path = tmp_path / "preview.mp3"
    payload = preview(str(SAMPLE_A), str(SAMPLE_B), recipe_name="Bass Swap", out=str(out_path))
    assert payload["recipe"] == "Bass Swap"
    assert out_path.exists()


def test_preview_with_unknown_recipe_raises():
    with pytest.raises(ValueError):
        preview(str(SAMPLE_A), str(SAMPLE_B), recipe_name="Not A Real Recipe")


def test_list_recipes_returns_all():
    payload = list_recipes()
    assert len(payload["recipes"]) == len(KnowledgeParser())


# --- CLI (in-process, via main()) --------------------------------------------

def test_cli_main_analyze(capsys):
    exit_code = main(["analyze", str(SAMPLE_A)])
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["bpm"] > 0


def test_cli_main_match_outputs_valid_json(capsys):
    exit_code = main(["match", str(SAMPLE_A), str(SAMPLE_B), "--top-n", "3"])
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert len(output["candidates"]) == 3


def test_cli_main_missing_file_returns_error_json(capsys):
    exit_code = main(["analyze", "does/not/exist.mp3"])
    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert "error" in output


def test_cli_main_list_recipes(capsys):
    exit_code = main(["list-recipes"])
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert len(output["recipes"]) == len(KnowledgeParser())


# --- True subprocess invocation (exactly as an external agent would call it) --

def test_subprocess_match_produces_parseable_json():
    proc = subprocess.run(
        [sys.executable, "-m", "app.music_brain.agent_bridge", "match", str(SAMPLE_A), str(SAMPLE_B)],
        cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert len(payload["candidates"]) == 3
