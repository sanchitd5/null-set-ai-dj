"""Guard: the public repo holds no atlas BUILDING code or seed data.

Everything that builds, scores or seeds the pair atlas (and the learned DJ knowledge behind it) lives
in the private sanchitd5/dj-atlas repo, the submodule at app/music_brain/knowledge. This test fails
when a tracked file outside that submodule looks like one of those files again. It reads git's index
only, so it runs the same with or without the submodule checked out.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SUBMODULE = "app/music_brain/knowledge"

# tracked paths (outside the submodule) that must never come back
FORBIDDEN_PATH = [re.compile(p) for p in (
    r"(^|/)pair_atlas[^/]*$",              # pair_atlas.py, pair_atlas_rules.js, pair_atlas.json.gz
    r"(^|/)atlas_store\.py$",
    r"(^|/)studied_combos[^/]*$",
    r"(^|/)(build|split)_atlas[^/]*$",
    r"(^|/)atlas_rules[^/]*$",
    r"(^|/)vetoes(_seed)?\.(py|json)$",
    r"(^|/)seed_combos\.json$",
    r"(^|/)macros\.py$",
    r"(^|/)macros/[^/]+\.json$",           # a macro export
    r"\.json\.gz$",                        # atlas shards
    r"(^|/)learned_techniques\.json$",     # the learned store
    r"(^|/)set-study-[^/]+$", r"(^|/)set_study_analyze\.py$",
)]

# source files that define the build (a rename would dodge the path patterns)
FORBIDDEN_CONTENT = [re.compile(p) for p in (
    r"^RULE_FILES\s*=", r"^def rules_hash\(", r"^COMBO_MIN_WORKS\s*=", r"^def write_atlas\(",
)]

# the adapter and files that only describe the interface
ALLOW = {
    "app/music_brain/atlas_bridge.py",
    "app/tests/py/test_no_atlas_in_public.py",
}


def _tracked():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def _outside_submodule():
    return [p for p in _tracked() if p != SUBMODULE and not p.startswith(SUBMODULE + "/") and p not in ALLOW]


def test_no_atlas_builder_paths_are_tracked():
    bad = [p for p in _outside_submodule() if any(r.search(p) for r in FORBIDDEN_PATH)]
    assert not bad, f"atlas building / seed files belong in the private dj-atlas repo (builder/): {bad}"


def test_no_atlas_builder_code_is_tracked():
    bad = []
    for p in _outside_submodule():
        if not p.endswith((".py", ".js", ".sh")):
            continue
        try:
            text = (ROOT / p).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(r.search(text) for r in map(lambda x: re.compile(x.pattern, re.M), FORBIDDEN_CONTENT)):
            bad.append(p)
    assert not bad, f"atlas build code found outside the private dj-atlas repo: {bad}"


def test_the_submodule_is_the_dj_atlas_repo():
    gm = (ROOT / ".gitmodules").read_text(encoding="utf-8")
    assert SUBMODULE in gm and "dj-atlas" in gm
