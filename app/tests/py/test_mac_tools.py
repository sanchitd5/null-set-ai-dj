"""The Mac scripts: shell syntax, the dj-core service path resolution start.sh does, build-djcore.sh's pin handling, and the
lyric remix bridge entry against a fake builder (no private atlas, no dart, no model needed)."""
import os
import subprocess
import types
from pathlib import Path

import pytest

from app.music_brain import atlas_bridge as ab

ROOT = Path(__file__).resolve().parents[3]
SHA = "a" * 40


@pytest.mark.parametrize("script", ["start.sh", "build-djcore.sh", "build-lyric-remix.sh", "deploy/djcore-env.sh"])
def test_shell_syntax(script):
    subprocess.run(["bash", "-n", str(ROOT / script)], check=True)


def _pin(tmp_path, sha=SHA):
    p = tmp_path / "pin"
    p.write_text(f"# c\nsha={sha}\nsha256={'b' * 64}\n")
    return p


def _resolve(tmp_path, build_body, **env):
    """Run resolve_djcore in bash; prints rc and DJ_CORE_SVC."""
    build = tmp_path / "fakebuild.sh"
    build.write_text("#!/bin/bash\n" + build_body)
    build.chmod(0o755)
    e = {"PATH": os.environ["PATH"], "DJ_CORE_PIN_FILE": str(_pin(tmp_path)), "DJ_CORE_VERSIONS_DIR": str(tmp_path / "v"),
         "DJC_BUILD": str(build), **env}
    r = subprocess.run(["bash", "-c", f'. "{ROOT}/deploy/djcore-env.sh"; resolve_djcore; echo "rc=$? svc=${{DJ_CORE_SVC:-}}"'],
                       env=e, capture_output=True, text=True)
    return r.stdout.strip().splitlines()[-1], r.stdout + r.stderr


def test_explicit_dj_core_svc_wins(tmp_path):
    last, _ = _resolve(tmp_path, "exit 9", DJ_CORE_SVC="/x/svc")
    assert last == "rc=0 svc=/x/svc"


def test_pinned_server_install_is_left_to_the_bridge(tmp_path):
    (tmp_path / "v").mkdir()
    f = tmp_path / "v" / SHA
    f.write_text("x")
    f.chmod(0o755)
    last, _ = _resolve(tmp_path, "exit 9")
    assert last == "rc=0 svc="


def test_existing_local_build_is_used_without_building(tmp_path):
    last, out = _resolve(tmp_path, f'[ "$1" = --path ] && {{ echo /m/.djcore/bin/{SHA}; exit 0; }}; echo BUILT; exit 0')
    assert last == f"rc=0 svc=/m/.djcore/bin/{SHA}" and "BUILT" not in out


def test_missing_build_is_built_once(tmp_path):
    b = tmp_path / "bin"
    b.write_text("x")
    b.chmod(0o755)
    last, out = _resolve(tmp_path, f'[ "$1" = --path ] && exit 1; echo {b}')
    assert last == f"rc=0 svc={b}" and "building it once" in out


def test_failed_build_is_loud_not_silent(tmp_path):
    last, out = _resolve(tmp_path, '[ "$1" = --path ] && exit 1; echo boom >&2; exit 1')
    assert last == "rc=1 svc=" and "fakebuild.sh  once" in out


def test_skip_on_request(tmp_path):
    last, out = _resolve(tmp_path, "exit 9", DJ_CORE_SKIP="1")
    assert last == "rc=0 svc=" and "DJ_CORE_SKIP" in out


def test_build_djcore_pin_and_path(tmp_path):
    env = {"PATH": os.environ["PATH"], "DJ_CORE_PIN_FILE": str(_pin(tmp_path)), "DJ_CORE_OUT_DIR": str(tmp_path / "out")}
    run = lambda *a: subprocess.run([str(ROOT / "build-djcore.sh"), *a], env=env, capture_output=True, text=True)
    assert run("--sha").stdout.strip() == SHA
    assert run("--path").returncode == 1                       # nothing built yet
    (tmp_path / "out").mkdir()
    b = tmp_path / "out" / SHA
    b.write_text("x")
    b.chmod(0o755)
    r = run("--path")
    assert r.returncode == 0 and r.stdout.strip() == str(b)
    assert run().stdout.strip() == str(b)                      # already built: skipped, path printed
    bad = tmp_path / "bad"
    bad.write_text("sha=zzz\n")
    env["DJ_CORE_PIN_FILE"] = str(bad)
    assert run("--sha").returncode == 1


def test_build_djcore_without_dart_says_so(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "DJ_CORE_PIN_FILE": str(_pin(tmp_path)), "DJ_CORE_OUT_DIR": str(tmp_path / "out"),
           "DJ_CORE_DIR": str(tmp_path / "core")}
    if any((Path(d) / n).exists() for d in ("/usr/bin", "/bin") for n in ("dart", "flutter")):
        pytest.skip("a dart is installed here")
    r = subprocess.run([str(ROOT / "build-djcore.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "no Dart SDK" in r.stderr


# ---------------------------------------------------------------- lyric remix bridge entry

class FakeBuilder:
    """The slice of builder/atlas/lyric_remix.py the bridge entry uses."""

    def __init__(self, tmp_path, plans=()):
        self.state = {"v": 1, "songs": {}, "judged": {}, "calls": 0}
        self.doc = {"plans": list(plans)}
        self.calls = []
        self.tmp = tmp_path
        self.after = None                                    # the document the "build" leaves behind

    def default_out(self):
        return self.tmp / "lyric_remix.json.gz"

    def default_cache_dir(self):
        return self.tmp / "cache"

    def read_doc(self, path):
        return self.doc

    def write_doc(self, path, doc):
        self.doc = doc
        return True

    def load_state(self, cache):
        return self.state

    def save_state(self, cache, state):
        self.state = state
        return state

    def coverage(self, cache, log=print):
        self.calls.append(("coverage", cache))
        log("coverage: {}")

    def build(self, cache, **kw):
        self.calls.append(("build", kw))
        if self.after is not None:
            self.doc = self.after
        return {"plans": 2, "batches": []}


def test_seed_marks_published_plans_judged_and_adds_only(tmp_path):
    fb = FakeBuilder(tmp_path, plans=[{"id": "p1", "a": "x", "b": None, "score": 0.8, "why": "ok"}, {"id": "p2", "a": "y", "b": "z"}, {"bad": 1}])
    fb.state["judged"]["p2"] = {"ok": False}                 # the local verdict stays
    assert ab.seed_published_plans(tmp_path, mod=fb) == 1
    assert fb.state["judged"]["p1"] == {"ok": True, "score": 8, "why": "ok", "a": "x", "b": None}
    assert fb.state["judged"]["p2"] == {"ok": False}
    assert ab.seed_published_plans(tmp_path, mod=fb) == 0


def test_cli_dry_run_counts_only(tmp_path):
    fb = FakeBuilder(tmp_path, plans=[{"id": "p1", "a": "x", "b": None}])
    logs = []
    assert ab.lyric_remix_cli(["--dry-run"], mod=fb, log=logs.append) == 0
    assert [c[0] for c in fb.calls] == ["coverage"] and fb.state["judged"] == {}
    assert any("published plans already" in m and "1" in m for m in logs)


def test_cli_build_passes_backend_queue_and_budget(tmp_path):
    fb = FakeBuilder(tmp_path, plans=[{"id": "p1", "a": "x", "b": None}])
    assert ab.lyric_remix_cli(["--backend", "claudecode", "--queue", "pair", "--max-calls", "7", "--fallback-local"], mod=fb, log=lambda m: None) == 0
    kind, kw = fb.calls[-1]
    assert kind == "build" and kw["backend"] == "claudecode" and kw["queue"] == "pair" and kw["max_calls"] == 7 and kw["fallback_local"]
    assert "p1" in fb.state["judged"]                        # seeded before the build
    fb.calls.clear()
    ab.lyric_remix_cli([], mod=fb, log=lambda m: None)
    kw = fb.calls[-1][1]
    assert kw["queue"] == "all" and "max_calls" not in kw    # both -> the builder's "all"; budget = the builder's default


def test_cli_without_the_builder_explains(monkeypatch):
    monkeypatch.setattr(ab, "AVAILABLE", False)
    logs = []
    assert ab.lyric_remix_cli([], log=logs.append) == 2
    assert any("submodule update" in m for m in logs)


def test_published_plans_of_songs_missing_here_are_kept(tmp_path):
    p_old = {"id": "p1", "a": "x", "b": None}
    p_new = {"id": "p2", "a": "y", "b": None}
    fb = FakeBuilder(tmp_path, plans=[p_old])
    fb.doc.update(v=1, rules="r", songs={"x": {"lines": []}})
    fb.after = {"v": 1, "rules": "r", "songs": {"y": {"lines": []}}, "plans": [p_new]}
    logs = []
    assert ab.lyric_remix_cli([], mod=fb, log=logs.append) == 0
    assert [p["id"] for p in fb.doc["plans"]] == ["p1", "p2"] and set(fb.doc["songs"]) == {"x", "y"}
    assert any("1 published plans" in m for m in logs)


def test_other_rules_are_not_merged(tmp_path):
    fb = FakeBuilder(tmp_path, plans=[{"id": "p1", "a": "x", "b": None}])
    fb.doc.update(v=1, rules="old")
    fb.after = {"v": 1, "rules": "new", "songs": {}, "plans": []}
    ab.lyric_remix_cli([], mod=fb, log=lambda m: None)
    assert fb.doc["plans"] == []
