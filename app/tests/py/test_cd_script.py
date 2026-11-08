"""deploy/cd/nullset-cd.sh against a temp bare "origin" + a prod clone, with PATH shims for systemctl / curl /
journalctl / pip. The GitHub API answer is a local JSON file; nothing touches the network or a real service."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "cd" / "nullset-cd.sh"
pytestmark = pytest.mark.skipif(
    any(shutil.which(t) is None for t in ("git", "bash", "flock", "python3")), reason="needs git, bash, flock, python3")

GIT_ENV = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

SHIM_SYSTEMCTL = """#!/bin/bash
echo "systemctl $*" >> "$FAKE_DIR/calls.log"
if [ "$1" = is-active ]; then
  svc="${@: -1}"
  [ -e "$FAKE_DIR/inactive.$svc" ] && exit 3
fi
exit 0
"""
SHIM_CURL = """#!/bin/bash
url="${@: -1}"
out=""
prev=""
for a in "$@"; do [ "$prev" = -o ] && out="$a"; prev="$a"; done
if [[ "$url" == */health ]]; then
  echo "curl health" >> "$FAKE_DIR/calls.log"
  if [ -e "$NULLSET_CD_APP_DIR/app/BAD" ]; then printf 500; else printf 200; fi
  exit 0
fi
echo "curl api $url" >> "$FAKE_DIR/calls.log"
[ -e "$FAKE_DIR/api_fail" ] && { echo "curl: (22) The requested URL returned error: 403" >&2; exit 22; }
cp "$FAKE_DIR/runs.json" "$out"
"""
SHIM_JOURNALCTL = """#!/bin/bash
[ -e "$FAKE_DIR/journal.txt" ] && cat "$FAKE_DIR/journal.txt"
exit 0
"""
SHIM_PIP = """#!/bin/bash
echo "pip $*" >> "$FAKE_DIR/calls.log"
[ -e "$FAKE_DIR/pip_fail" ] && { echo "ERROR: no matching distribution" >&2; exit 1; }
exit 0
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          env=dict(os.environ, **GIT_ENV), timeout=60).stdout.strip()


class Rig:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.fake = tmp / "fake"
        self.bin = tmp / "bin"
        self.origin = tmp / "origin.git"
        self.work = tmp / "work"
        self.prod = tmp / "prod"
        self.state = tmp / "state"
        self.logfile = tmp / "cd.log"
        for d in (self.fake, self.bin, tmp / "venv" / "bin"):
            d.mkdir(parents=True)
        for name, body in (("systemctl", SHIM_SYSTEMCTL), ("curl", SHIM_CURL), ("journalctl", SHIM_JOURNALCTL)):
            self._exe(self.bin / name, body)
        self._exe(tmp / "venv" / "bin" / "pip", SHIM_PIP)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.work)], check=True,
                       capture_output=True)
        git(self.work, "checkout", "-q", "-b", "main")
        self.commit({"app/ui/server.py": "v1\n", "app/stem_server/server.py": "v1\n", "requirements.txt": "x\n",
                     "start.sh": "#!/bin/bash\n", "docs/a.md": "a\n", "app/tests/py/t.py": "t\n"}, "base")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.prod)], check=True, capture_output=True)
        git(self.prod, "checkout", "-q", "-b", "deploy-parity")
        self.runs = []
        self.green(git(self.prod, "rev-parse", "HEAD"))

    @staticmethod
    def _exe(path: Path, body: str):
        path.write_text(body)
        path.chmod(0o755)

    def commit(self, files: dict, msg="c") -> str:
        for rel, text in files.items():
            p = self.work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", msg)
        git(self.work, "push", "-q", "origin", "HEAD:main")
        return git(self.work, "rev-parse", "HEAD")

    def green(self, sha, conclusion="success", status="completed"):
        self.runs.insert(0, {"head_sha": sha, "head_branch": "main", "status": status, "conclusion": conclusion,
                             "created_at": "2026-11-08T%02d:%02d:00Z" % divmod(len(self.runs), 60)})
        (self.fake / "runs.json").write_text(json.dumps({"workflow_runs": self.runs}))

    def head(self):
        return git(self.prod, "rev-parse", "HEAD")

    def calls(self):
        p = self.fake / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def restarts(self):
        return [c.split()[-1] for c in self.calls() if c.startswith("systemctl restart")]

    def run(self, *args, env_extra=None):
        env = dict(os.environ, **GIT_ENV)
        env.update(
            PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_DIR=str(self.fake),
            NULLSET_CD_APP_DIR=str(self.prod), NULLSET_CD_VENV=str(self.tmp / "venv"),
            NULLSET_CD_STATE_DIR=str(self.state), NULLSET_CD_LOG_FILE=str(self.logfile),
            NULLSET_CD_LOCK_FILE=str(self.tmp / "cd.lock"), NULLSET_CD_DISABLE_FILE=str(self.tmp / "disabled"),
            NULLSET_CD_SETTLE_SECONDS="0", NULLSET_CD_HEALTH_SECONDS="0", NULLSET_CD_HEALTH_POLL="1")
        env.update(env_extra or {})
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=120)

    def log(self):
        return self.logfile.read_text() if self.logfile.exists() else ""


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_not_green_is_a_noop(rig):
    new = rig.commit({"app/ui/server.py": "v2\n"})
    rig.green(new, conclusion="failure")
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.head() != new and rig.restarts() == [] and rig.log() == ""
    rig.green(new, conclusion=None, status="in_progress")     # pending run: also nothing
    assert rig.run().returncode == 0 and rig.restarts() == []


def test_api_error_or_rate_limit_is_a_noop(rig):
    rig.commit({"app/ui/server.py": "v2\n"})
    (rig.fake / "api_fail").write_text("")
    r = rig.run()
    assert r.returncode == 0 and "rate-limited" in r.stdout
    assert rig.restarts() == []


def test_same_sha_exits_quietly(rig):
    r = rig.run()
    assert r.returncode == 0 and r.stdout == "" and rig.log() == "" and rig.restarts() == []
    assert not git(rig.prod, "tag", "-l", "pre-cd-*")


def test_app_change_restarts_web_only(rig):
    new = rig.commit({"app/ui/server.py": "v2\n"})
    rig.green(new)
    r = rig.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert rig.head() == new
    assert rig.restarts() == ["nullset-dj"]
    assert "DEPLOYED" in rig.log() and new[:9] in rig.log()
    assert len(git(rig.prod, "tag", "-l", "pre-cd-*").split()) == 1
    assert not any(c.startswith("pip") for c in rig.calls())


def test_stem_server_change_restarts_stem_only(rig):
    new = rig.commit({"app/stem_server/server.py": "v2\n"})
    rig.green(new)
    assert rig.run().returncode == 0
    assert rig.head() == new and rig.restarts() == ["stem-server"]


def test_shared_stem_dependency_restarts_both(rig):
    new = rig.commit({"app/music_brain/audio/stem_service.py": "v2\n"})
    rig.green(new)
    assert rig.run().returncode == 0
    assert rig.restarts() == ["nullset-dj", "stem-server"]


def test_start_sh_restarts_web(rig):
    new = rig.commit({"start.sh": "#!/bin/bash\n# v2\n"})
    rig.green(new)
    assert rig.run().returncode == 0 and rig.restarts() == ["nullset-dj"]


def test_docs_and_tests_only_restart_nothing(rig):
    new = rig.commit({"docs/a.md": "b\n", "app/tests/py/t.py": "t2\n"})
    rig.green(new)
    r = rig.run()
    assert r.returncode == 0
    assert rig.head() == new and rig.restarts() == []
    assert not any(c == "curl health" for c in rig.calls())      # no smoke when nothing restarted
    assert "DEPLOYED" in rig.log()


def test_requirements_change_runs_pip_then_restarts_both(rig):
    new = rig.commit({"requirements.txt": "x\ny\n"})
    rig.green(new)
    assert rig.run().returncode == 0
    calls = rig.calls()
    assert any(c.startswith("pip install -r") and c.endswith("requirements.txt") for c in calls)
    assert rig.restarts() == ["nullset-dj", "stem-server"]
    assert [i for i, c in enumerate(calls) if c.startswith("pip")][0] < [i for i, c in enumerate(calls) if "restart" in c][0]


def test_smoke_failure_rolls_back_and_remembers_the_bad_sha(rig):
    old = rig.head()
    bad = rig.commit({"app/BAD": "boom\n"})
    rig.green(bad)
    r = rig.run()
    assert r.returncode == 3, r.stdout + r.stderr
    assert rig.head() == old and not (rig.prod / "app" / "BAD").exists()
    assert rig.restarts() == ["nullset-dj", "nullset-dj"]              # restarted for the deploy and again for the rollback
    assert "ROLLED BACK" in r.stdout and "ROLLED BACK" in rig.log()
    assert (rig.state / "bad_sha").read_text().strip() == bad
    # same green commit is not retried
    n = len(rig.calls())
    r2 = rig.run()
    assert r2.returncode == 0 and "waiting for a newer green" in r2.stdout and len(rig.calls()) == n + 1   # only the API call
    # a newer green fix deploys and clears the memory
    (rig.work / "app" / "BAD").unlink()
    git(rig.work, "add", "-A")
    git(rig.work, "commit", "-qm", "unbreak")
    git(rig.work, "push", "-q", "origin", "HEAD:main")
    fix = git(rig.work, "rev-parse", "HEAD")
    rig.green(fix)
    assert rig.run().returncode == 0 and rig.head() == fix
    assert not (rig.state / "bad_sha").exists()


def test_inactive_service_rolls_back(rig):
    old = rig.head()
    rig.green(rig.commit({"app/ui/server.py": "v2\n"}))
    (rig.fake / "inactive.nullset-dj").write_text("")
    r = rig.run()
    assert r.returncode == 2, r.stdout          # the rollback smoke fails too (still inactive): loud, needs a human
    assert rig.head() == old and "ROLLBACK FAILED" in r.stdout and "ROLLBACK-FAILED" in rig.log()


def test_traceback_in_journal_rolls_back(rig):
    old = rig.head()
    rig.green(rig.commit({"app/ui/server.py": "v2\n"}))
    (rig.fake / "journal.txt").write_text("Traceback (most recent call last):\n")
    r = rig.run()
    assert rig.head() == old and "Traceback" in r.stdout


def test_pip_failure_rolls_back(rig):
    old = rig.head()
    rig.green(rig.commit({"requirements.txt": "x\nnope\n"}))
    (rig.fake / "pip_fail").write_text("")
    r = rig.run()
    assert r.returncode == 2, r.stdout          # the shim fails pip in the rollback too: loud, needs a human
    assert rig.head() == old and "pip install failed" in r.stdout
    assert rig.restarts() == ["nullset-dj", "stem-server"]       # only the rollback restarts: the failed deploy never did
    assert (rig.state / "bad_sha").exists()


def test_diverged_prod_is_refused(rig):
    (rig.prod / "local.txt").write_text("hotfix\n")
    git(rig.prod, "add", "-A")
    git(rig.prod, "commit", "-qm", "local-only commit")
    local = rig.head()
    rig.green(rig.commit({"app/ui/server.py": "v2\n"}))
    r = rig.run()
    assert r.returncode == 1 and "DIVERGED" in r.stdout
    assert rig.head() == local and rig.restarts() == []
    assert "REFUSED diverged" in rig.log()


def test_prod_ahead_of_the_newest_green_is_a_noop(rig):
    (rig.prod / "local.txt").write_text("x\n")
    git(rig.prod, "add", "-A")
    git(rig.prod, "commit", "-qm", "ahead")
    r = rig.run()                                                  # green = the old HEAD, an ancestor of ours
    assert r.returncode == 0 and rig.restarts() == []


def test_conflicting_local_edit_is_refused_and_kept(rig):
    old = rig.head()
    (rig.prod / "app" / "ui" / "server.py").write_text("my local edit\n")
    rig.green(rig.commit({"app/ui/server.py": "v2\n"}))
    r = rig.run()
    assert r.returncode == 1 and "REFUSED" in r.stdout
    assert rig.head() == old
    assert (rig.prod / "app" / "ui" / "server.py").read_text() == "my local edit\n"
    assert rig.restarts() == [] and not git(rig.prod, "tag", "-l", "pre-cd-*")


def test_non_conflicting_local_edit_survives_the_deploy(rig):
    (rig.prod / "start.sh").write_text("#!/bin/bash\n# local\n")
    new = rig.commit({"app/ui/server.py": "v2\n"})
    rig.green(new)
    assert rig.run().returncode == 0
    assert rig.head() == new and (rig.prod / "start.sh").read_text().endswith("# local\n")


def test_wrong_branch_is_refused(rig):
    git(rig.prod, "checkout", "-q", "-b", "other")
    rig.green(rig.commit({"app/ui/server.py": "v2\n"}))
    r = rig.run()
    assert r.returncode == 1 and "not deploy-parity" in r.stdout


def test_kill_switch(rig):
    rig.green(rig.commit({"app/ui/server.py": "v2\n"}))
    (rig.tmp / "disabled").write_text("")
    r = rig.run()
    assert r.returncode == 0 and "disabled" in r.stdout
    assert rig.calls() == []                                       # not even the API


def test_lock_held_by_another_run(rig):
    rig.green(rig.commit({"app/ui/server.py": "v2\n"}))
    holder = subprocess.Popen(["flock", str(rig.tmp / "cd.lock"), "sleep", "30"])
    try:
        time.sleep(0.5)
        r = rig.run()
        assert r.returncode == 0 and "holds the lock" in r.stdout
        assert rig.restarts() == [] and rig.calls() == []
    finally:
        holder.kill()
        holder.wait()


def test_dry_run_changes_nothing(rig):
    old = rig.head()
    rig.green(rig.commit({"requirements.txt": "x\ny\n"}))
    r = rig.run("--dry-run")
    assert r.returncode == 0 and "plan:" in r.stdout and "pip=1" in r.stdout
    assert rig.head() == old and rig.restarts() == [] and rig.log() == ""
    assert not git(rig.prod, "tag", "-l", "pre-cd-*")
    assert not any(c.startswith("pip") for c in rig.calls())


def test_once_deploys_a_specific_green_sha_and_rejects_others(rig):
    mid = rig.commit({"app/ui/server.py": "v2\n"})
    rig.green(mid)
    top = rig.commit({"app/ui/server.py": "v3\n"})
    rig.green(top)
    assert rig.run("--once", mid).returncode == 0
    assert rig.head() == mid
    red = rig.commit({"app/ui/server.py": "v4\n"})                 # never went green
    r = rig.run("--once", red)
    assert r.returncode == 1 and "not a green" in r.stdout and rig.head() == mid


def test_old_rollback_tags_are_pruned(rig):
    for i in range(3):
        rig.green(rig.commit({"docs/a.md": f"{i}\n"}))
        assert rig.run(env_extra={"NULLSET_CD_KEEP_TAGS": "2"}).returncode == 0
        time.sleep(1.05)                                           # tag names carry a one-second timestamp
    assert len(git(rig.prod, "tag", "-l", "pre-cd-*").split()) == 2
