"""deploy/atlas/split_atlas.sh on a tiny fixture repo: the dry run proves history + layout and touches
nothing; the real mode (to a local bare repo, never GitHub) pushes the split and swaps in the submodule."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "atlas" / "split_atlas.sh"
pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="needs git + bash")


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True, timeout=60).stdout.strip()


def run(*args, **kw):
    env = dict(os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="protocol.file.allow", GIT_CONFIG_VALUE_0="always")
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=120, env=env, **kw)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "main_repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    k = r / "app" / "knowledge"
    (k / "macros").mkdir(parents=True)
    (r / "app" / "code.py").write_text("print(1)\n")
    (k / "names.json").write_text('{"a": "one"}\n')
    git(r, "add", "-A")
    git(r, "commit", "-qm", "knowledge: first export")
    (r / "app" / "code.py").write_text("print(2)\n")                   # unrelated commit: must not come along
    git(r, "commit", "-qam", "code only")
    (k / "macros" / "m1.json").write_text('{"name": "m1"}\n')
    git(r, "add", "-A")
    git(r, "commit", "-qm", "knowledge: a macro")
    (k / "names.json").write_text('{"a": "one", "b": "two"}\n')
    (r / "app" / "code.py").write_text("print(3)\n")                   # one commit touching both
    git(r, "add", "-A")
    git(r, "commit", "-qm", "knowledge: names, plus code")
    return r


def snapshot(r):
    return (git(r, "rev-parse", "HEAD"), git(r, "status", "--porcelain"), git(r, "for-each-ref"))


def test_dry_run_proves_history_and_layout_and_touches_nothing(repo, tmp_path):
    before = snapshot(repo)
    work = tmp_path / "scratch"
    p = run("--dry-run", "--repo", str(repo), "--path", "app/knowledge", "--work", str(work))
    assert p.returncode == 0, p.stderr + p.stdout
    assert "verified: 3 commits, 2 files" in p.stdout and "dry run OK" in p.stdout
    assert "layout: macros" in p.stdout and "layout: names.json" in p.stdout and "layout: app" not in p.stdout
    assert snapshot(repo) == before and not (repo / ".gitmodules").exists(), "the repo itself is untouched"
    split = work / "atlas"
    subjects = git(split, "log", "--format=%s", "main").splitlines()
    assert subjects == ["knowledge: names, plus code", "knowledge: a macro", "knowledge: first export"]
    assert git(split, "rev-parse", "main^{tree}") == git(repo, "rev-parse", "HEAD:app/knowledge")
    sim = work / "sim"                                                   # the swapped clone
    assert git(sim, "ls-tree", "HEAD", "app/knowledge").startswith("160000 commit " + git(split, "rev-parse", "main"))
    assert (sim / "app" / "knowledge" / "macros" / "m1.json").is_file() and (sim / "app" / "code.py").is_file()


def test_dry_run_removes_its_scratch_dir_by_default(repo):
    before = set(Path(os.environ.get("TMPDIR", "/tmp")).glob("tmp.*"))
    assert run("--dry-run", "--repo", str(repo), "--path", "app/knowledge").returncode == 0
    assert set(Path(os.environ.get("TMPDIR", "/tmp")).glob("tmp.*")) <= before


def test_refuses_a_bad_path_and_a_folder_that_is_already_a_submodule(repo, tmp_path):
    p = run("--dry-run", "--repo", str(repo), "--path", "app/nope")
    assert p.returncode != 0 and "not a folder" in p.stderr
    p = run("--dry-run", "--repo", str(repo), "--path", "../x")
    assert p.returncode != 0 and "relative folder" in p.stderr


def test_real_mode_pushes_the_split_and_swaps_in_the_submodule(repo, tmp_path):
    bare = tmp_path / "dj-atlas.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    p = run("--repo", str(repo), "--path", "app/knowledge", "--remote-url", str(bare))
    assert p.returncode == 0, p.stderr + p.stdout
    assert git(bare, "rev-list", "--count", "main") == "3"
    assert git(repo, "ls-tree", "HEAD", "app/knowledge").startswith("160000 commit " + git(bare, "rev-parse", "main"))
    assert "path = app/knowledge" in (repo / ".gitmodules").read_text()
    assert (repo / "app" / "knowledge" / "names.json").read_text() == '{"a": "one", "b": "two"}\n'
    assert git(repo, "status", "--porcelain") == ""
    again = run("--dry-run", "--repo", str(repo), "--path", "app/knowledge")
    assert again.returncode != 0 and "already a submodule" in again.stderr


def test_real_mode_refuses_a_dirty_tree(repo, tmp_path):
    (repo / "app" / "code.py").write_text("dirty\n")
    bare = tmp_path / "x.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    p = run("--repo", str(repo), "--path", "app/knowledge", "--remote-url", str(bare))
    assert p.returncode != 0 and "not clean" in p.stderr
    assert git(bare, "for-each-ref") == "", "nothing was pushed"
