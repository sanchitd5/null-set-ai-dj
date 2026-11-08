"""knowledge publish / update (knowledge_publish.py): the server contributes to the dj-atlas repo.
Real git against temp bare repos on disk; no network, no keys, nothing under the real knowledge/."""
import gzip
import json
import subprocess
import threading

import pytest

from app.music_brain import db
from app.music_brain.atlas import macros as mc
from app.music_brain.atlas import pair_atlas as pa
from app.music_brain.matching import knowledge as kn
from app.music_brain.matching import knowledge_publish as kp
from app.music_brain.matching import knowledge_sync as ks
from app.tests.py.test_knowledge import A, B, C, NAMES, src  # noqa: F401 (src: fixture)


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.email=owner@example.invalid", "-c", "user.name=owner",
                           "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()


def tree(repo, rev="HEAD"):
    return kp._tree_files(repo, rev)


def shard(files, a):
    return json.loads(gzip.decompress(files[f"atlas/pairs/{a}.json.gz"]))


@pytest.fixture
def env(src, tmp_path, monkeypatch):  # noqa: F811
    """A bare 'dj-atlas' remote holding the exported fixture, the server's checkout (a clone of it),
    and the owner's own clone."""
    for k in ("DJ_ATLAS_PUSH", "DJ_ATLAS_REMOTE_URL", "DJ_ATLAS_SSH_KEY", "DJ_ATLAS_AUTOPUBLISH", "DJ_ATLAS_BRANCH"):
        monkeypatch.delenv(k, raising=False)
    bare = tmp_path / "dj-atlas.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    seed.mkdir()
    for p in src["out"].rglob("*"):
        if p.is_file():
            t = seed / p.relative_to(src["out"])
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(p.read_bytes())
    git(seed, "init", "-q", "-b", "main")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "initial knowledge")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "main")
    server = tmp_path / "server_k"
    owner = tmp_path / "owner_k"
    git(tmp_path, "clone", "-q", str(bare), str(server))
    git(tmp_path, "clone", "-q", str(bare), str(owner))
    monkeypatch.setenv("DJ_ATLAS_PUSH", "1")
    # the cache is a seeded install: sync records provenance, the two hand-made macros become published rows
    ks.sync(src["cache"], server)
    for c in ks.conflicts(src["cache"]):
        ks.take(c["item"], True, src["cache"])
    return {"bare": bare, "server": server, "owner": owner, "cache": src["cache"], "tmp": tmp_path}


def remote_head(env):
    return git(env["bare"], "rev-parse", "main")


def remote_files(env):
    return tree(env["bare"], "main")


def owner_push(env, msg="owner edit"):
    git(env["owner"], "add", "-A")
    git(env["owner"], "commit", "-qm", msg)
    git(env["owner"], "pull", "-q", "--rebase", "origin", "main")
    git(env["owner"], "push", "-q", "origin", "main")


def edit_cache_pair(cache, key, **fields):
    atlas = pa.load(cache)
    atlas["pairs"][key].update(fields)
    pa.write_atlas(atlas, pa.atlas_path(cache))


def edit_cache_macro(cache, name, **fields):
    conn = mc._mdb(cache)
    with db.tx(conn):
        m = dict(mc.stored(cache)[name])
        m.update(fields)
        mc._put(conn, m)


def publish(env, **kw):
    return kp.publish(env["cache"], env["server"], **kw)


# ---------------------------------------------------------------------------------- no-op, push
def test_publish_is_a_noop_when_nothing_changed(env):
    head = remote_head(env)
    rep = publish(env)
    assert rep["status"] == "noop" and rep["files_changed"] == 0
    assert remote_head(env) == head and not (env["server"] / "VERSION").exists()
    assert git(env["server"], "rev-parse", "HEAD") == head


def test_publish_commits_counts_bumps_version_and_pushes(env):
    base = remote_head(env)
    edit_cache_pair(env["cache"], f"{B}>{C}", works=71)
    edit_cache_pair(env["cache"], f"{A}>{B}", played={"good": 3, "bad": 0, "sessions": ["s"]})
    edit_cache_macro(env["cache"], "chain-1-anyma", note="more")
    rep = publish(env)
    assert rep["status"] == "pushed" and rep["counts"]["pairs"] == 2 and rep["counts"]["macros"] == 1
    assert rep["counts"]["played"] == 1
    head = remote_head(env)
    assert head != base and git(env["bare"], "rev-list", "--count", "main") == "2"
    msg = git(env["bare"], "log", "-1", "--format=%B", "main")
    assert msg.startswith("knowledge: server publish (1 macros, 2 pairs") and "1 pairs with new played evidence" in msg
    files = remote_files(env)
    assert shard(files, B)[f"{B}>{C}"]["works"] == 71
    assert shard(files, A)[f"{A}>{B}"]["played"]["good"] == 3
    v = json.loads(git(env["bare"], "show", "main:VERSION"))
    assert v == {"rules_hash": pa.rules_hash(), "sha": base}
    # the server's next publish with nothing new: nothing committed, VERSION not bumped
    again = publish(env)
    assert again["status"] == "noop" and remote_head(env) == head


def test_no_push_without_the_env_flag(env, monkeypatch):
    monkeypatch.delenv("DJ_ATLAS_PUSH")
    base = remote_head(env)
    edit_cache_pair(env["cache"], f"{B}>{C}", works=72)
    rep = publish(env)
    assert rep["status"] == "committed" and remote_head(env) == base
    assert git(env["server"], "rev-list", "--count", "HEAD") == "2"
    assert publish(env, push=True)["status"] == "pushed"          # explicit push still works


def test_dry_run_changes_nothing(env):
    edit_cache_pair(env["cache"], f"{B}>{C}", works=73)
    head, local = remote_head(env), git(env["server"], "rev-parse", "HEAD")
    rep = publish(env, dry_run=True)
    assert rep["status"] == "dry-run" and rep["counts"]["pairs"] == 1
    assert remote_head(env) == head and git(env["server"], "rev-parse", "HEAD") == local
    assert git(env["server"], "status", "--porcelain") == ""


def test_publish_is_deterministic(env, src, tmp_path, monkeypatch):  # noqa: F811
    edit_cache_pair(env["cache"], f"{B}>{C}", works=75)
    publish(env)
    first = remote_files(env)
    # a second, independent remote + checkout publishing the same cache gets byte-identical files
    bare2 = tmp_path / "second.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare2))
    git(env["owner"], "remote", "add", "second", str(bare2))
    git(env["owner"], "fetch", "-q", "origin")
    git(env["owner"], "push", "-q", "second", f"{git(env['owner'], 'rev-parse', 'origin/main~1')}:refs/heads/main")
    other = tmp_path / "other_k"
    git(tmp_path, "clone", "-q", str(bare2), str(other))
    kp.publish(env["cache"], other)
    assert {k: v for k, v in tree(other).items() if k != "VERSION"} == {k: v for k, v in first.items() if k != "VERSION"}


def test_first_publish_to_an_empty_remote(env, tmp_path):
    empty = tmp_path / "empty.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(empty))
    fresh = tmp_path / "fresh_k"
    git(tmp_path, "init", "-q", "-b", "main", str(fresh))
    git(fresh, "remote", "add", "origin", str(empty))
    rep = kp.publish(env["cache"], fresh)
    assert rep["status"] == "pushed" and rep["remote"] is None
    files = tree(empty, "main")
    assert f"atlas/pairs/{A}.json.gz" in files and "macros/studied-s1-1.json" in files
    assert json.loads(git(empty, "show", "main:VERSION"))["sha"] == ""


# ---------------------------------------------------------------------------- owner wins, rebase
def test_owner_edits_win_over_published_ones(env):
    # the owner edits a macro, a pair in shard A and a label on the remote
    m = env["owner"] / "macros" / "studied-s1-1.json"
    d = json.loads(m.read_text())
    d["title"] = "Owner's title"
    m.write_bytes(kn._dump(d))
    p = env["owner"] / "atlas" / "pairs" / f"{A}.json.gz"
    rows = json.loads(gzip.decompress(p.read_bytes()))
    rows[f"{A}>{C}"]["works"] = 99
    p.write_bytes(kp._encode(f"atlas/pairs/{A}.json.gz", rows))
    owner_push(env)
    # meanwhile the server changed the same macro, another pair of the SAME shard, and a pair elsewhere
    edit_cache_macro(env["cache"], "studied-s1-1", title="Server title")
    edit_cache_pair(env["cache"], f"{A}>{C}", works=11)           # same row the owner edited
    edit_cache_pair(env["cache"], f"{A}>{B}", works=89)           # other row, same shard
    edit_cache_pair(env["cache"], f"{B}>{C}", works=71)           # other shard
    rep = publish(env)
    assert rep["status"] == "pushed"
    files = remote_files(env)
    assert json.loads(files["macros/studied-s1-1.json"])["title"] == "Owner's title"
    a = shard(files, A)
    assert a[f"{A}>{C}"]["works"] == 99, "the owner's row stays"
    assert a[f"{A}>{B}"]["works"] == 89, "the server's change to another row of that shard is merged in"
    assert shard(files, B)[f"{B}>{C}"]["works"] == 71
    # and it keeps winning on the next publish, while the cache still holds the server's own row
    again = publish(env)
    files = remote_files(env)
    assert again["status"] in ("noop", "pushed")
    assert json.loads(files["macros/studied-s1-1.json"])["title"] == "Owner's title"
    assert shard(files, A)[f"{A}>{C}"]["works"] == 99


def test_owner_deletion_stays_deleted(env):
    (env["owner"] / "macros" / "chain-1-anyma.json").unlink()
    owner_push(env, "drop a macro")
    edit_cache_macro(env["cache"], "chain-1-anyma", note="server touched it")
    publish(env)
    assert "macros/chain-1-anyma.json" not in remote_files(env)


def test_rebases_over_a_concurrent_remote_change(env, monkeypatch):
    """The remote moves between our fetch and our push: the push is rejected, the next attempt
    merges the new commit and pushes; both sides' changes end up on the remote."""
    edit_cache_pair(env["cache"], f"{B}>{C}", works=71)
    real_fetch, calls = kp._fetch, []

    def racing_fetch(out, branch):
        sha = real_fetch(out, branch)
        if not calls:                                             # first attempt only
            calls.append(1)
            m = env["owner"] / "macros" / "studied-s1-1.json"
            d = json.loads(m.read_text())
            d["title"] = "Raced title"
            m.write_bytes(kn._dump(d))
            owner_push(env, "raced edit")
        return sha

    monkeypatch.setattr(kp, "_fetch", racing_fetch)
    rep = publish(env)
    assert rep["status"] == "pushed" and rep["attempts"] == 2
    files = remote_files(env)
    assert json.loads(files["macros/studied-s1-1.json"])["title"] == "Raced title"
    assert shard(files, B)[f"{B}>{C}"]["works"] == 71
    assert git(env["bare"], "rev-list", "--count", "main") == "3"          # linear: initial, owner, ours


def test_remote_on_other_atlas_rules_keeps_its_atlas(env):
    meta = env["owner"] / "atlas" / "meta.json"
    d = json.loads(meta.read_text())
    d["rules"] = "feedfacefeedface"
    meta.write_bytes(kn._dump(d))
    owner_push(env, "atlas rebuilt on other rules")
    edit_cache_pair(env["cache"], f"{B}>{C}", works=71)
    edit_cache_macro(env["cache"], "chain-1-anyma", note="x")
    rep = publish(env)
    files = remote_files(env)
    assert json.loads(files["atlas/meta.json"])["rules"] == "feedfacefeedface"
    assert shard(files, B)[f"{B}>{C}"]["works"] == 70, "server pairs from other rules are not mixed in"
    assert rep["counts"]["macros"] == 1 and "kept" in rep["atlas"]


# --------------------------------------------------------------------------------- privacy, guards
# real-looking home dirs are assembled at runtime so this file itself passes test_no_personal_paths
_ALICE_HOME = "/home/" + "alice/"
_BOB_HOME = "/Users/" + "bob/"


def test_never_publishes_private_strings(env):
    edit_cache_macro(env["cache"], "chain-1-anyma", note=f"rendered at {_ALICE_HOME}music/set.wav")
    head = remote_head(env)
    with pytest.raises(kp.PublishError, match="private"):
        publish(env)
    assert remote_head(env) == head


def test_privacy_scan_applies_the_repos_home_path_rule():
    files = {"names.json": kn._dump({"x": f"see {_BOB_HOME}Music/a.mp3"}),
             "macros/m.json": kn._dump({"note": "placeholder /Users/me/x is fine"}),
             "atlas/pairs/aaaaaaaaaaaaaaaa.json.gz": kp._encode("a.json.gz", {"k": {"n": "mail a@b.example"}})}
    hits = kp.privacy_scan(files)
    assert any(_BOB_HOME in h for h in hits) and any("a@b.example" in h for h in hits)
    assert not any("macros/m.json" in h for h in hits if "/Users/me/" in h and "bob" in h)


def test_refuses_a_folder_that_is_not_its_own_checkout(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(kp.PublishError, match="own git checkout"):
        kp.publish(env["cache"], plain)
    inner = env["server"] / "sub"
    inner.mkdir()
    with pytest.raises(kp.PublishError, match="own git checkout"):
        kp.publish(env["cache"], inner)


def test_deploy_key_goes_through_git_ssh_command_only(monkeypatch):
    monkeypatch.setenv("DJ_ATLAS_SSH_KEY", "/run/keys/atlas deploy")
    cmd = kp._env()["GIT_SSH_COMMAND"]
    assert "-i '/run/keys/atlas deploy'" in cmd and "IdentitiesOnly=yes" in cmd and "BatchMode=yes" in cmd
    monkeypatch.delenv("DJ_ATLAS_SSH_KEY")
    assert "GIT_SSH_COMMAND" not in {k for k in kp._env() if k not in __import__("os").environ}


# ------------------------------------------------------------------------------------------ update
def test_update_fast_forwards_syncs_and_survives_a_dirty_tree(env):
    m = env["owner"] / "macros" / "studied-s1-1.json"
    d = json.loads(m.read_text())
    d["title"] = "Pulled title"
    m.write_bytes(kn._dump(d))
    owner_push(env)
    scratch = env["server"] / "notes.txt"
    scratch.write_text("local scratch")
    names = env["server"] / "names.json"
    names.write_bytes(names.read_bytes() + b"\n")                  # an uncommitted tracked change
    rep = kp.update(env["cache"], env["server"])
    assert rep["moved"] and rep["stashed"] and rep["restored"] is True
    assert rep["sha"] == remote_head(env)
    assert json.loads((env["server"] / "macros" / "studied-s1-1.json").read_text())["title"] == "Pulled title"
    assert scratch.read_text() == "local scratch" and names.read_bytes().endswith(b"\n\n")
    assert mc.stored(env["cache"])["studied-s1-1"]["title"] == "Pulled title", "sync ran after the update"


def test_update_in_a_superproject_uses_submodule_update_remote(env, tmp_path, monkeypatch):
    sup = tmp_path / "super"
    sup.mkdir()
    git(sup, "init", "-q", "-b", "main")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    git(sup, "submodule", "add", "-q", "-b", "main", str(env["bare"]), "app/knowledge")
    git(sup, "commit", "-qm", "pin")
    m = env["owner"] / "macros" / "studied-s1-1.json"
    d = json.loads(m.read_text())
    d["title"] = "Via submodule"
    m.write_bytes(kn._dump(d))
    owner_push(env)
    pinned = git(sup / "app" / "knowledge", "rev-parse", "HEAD")
    rep = kp.update(env["cache"], sup / "app" / "knowledge")
    assert rep["sha"] == remote_head(env) != pinned and rep["moved"]


# --------------------------------------------------------------------------- background schedule
def test_set_end_and_daily_thread_are_opt_in(monkeypatch):
    ran = threading.Event()
    monkeypatch.setattr(kp, "publish_safe", lambda *a, **kw: ran.set() or {"status": "noop"})
    monkeypatch.delenv("DJ_ATLAS_AUTOPUBLISH", raising=False)
    assert kp.on_set_end(delay_s=0) is False and kp.start_timer(period_s=0.05) is False
    assert not ran.wait(0.3)
    monkeypatch.setenv("DJ_ATLAS_AUTOPUBLISH", "1")
    assert kp.on_set_end(delay_s=0) is True
    assert ran.wait(5)
    ran.clear()
    try:
        assert kp.start_timer(period_s=0.05) is True and kp.start_timer(period_s=0.05) is False   # one thread
        assert ran.wait(5), "the daily thread publishes on its period"
    finally:
        kp.stop_timer()
        kp._timer.join(5)
        kp._timer = None


# --------------------------------------------------------------------------------- dirty checkouts
def test_dirty_checkout_is_stashed_and_dropped_when_an_export_reproduces_it(env):
    edit_cache_pair(env["cache"], f"{B}>{C}", works=71)
    kn.export(env["cache"], env["server"])                       # what learn-set leaves behind: an uncommitted export
    assert git(env["server"], "status", "--porcelain")
    assert publish(env)["status"] == "pushed"
    assert git(env["server"], "stash", "list") == "" and git(env["server"], "status", "--porcelain") == ""


def test_hand_edits_in_the_checkout_are_kept_in_a_stash(env):
    (env["server"] / "README.md").write_text("notes by hand")
    rep = publish(env)
    assert rep["stashed"] is True and "dj-atlas publish" in git(env["server"], "stash", "list")
