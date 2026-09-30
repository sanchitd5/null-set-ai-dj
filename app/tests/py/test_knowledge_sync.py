"""knowledge sync: per-file version stamps (git sha, sha256 fallback) and per-row provenance,
so a pulled update reaches an install that seeded the old version while the owner's own
changes win. A tmp git repo holds the tracked knowledge folder; no network."""
import json
import shutil
import subprocess

import pytest

from app.music_brain.atlas import macros as mc
from app.music_brain.atlas import pair_atlas as pa
from app.music_brain.learning import set_learner as sl
from app.music_brain.matching import knowledge as kn
from app.music_brain.matching import knowledge_sync as ks
from app.tests.py.test_knowledge import A, B, NAMES, _cache, src  # noqa: F401 (src: fixture)


def _git(repo, *args):
    return subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-C", str(repo),
                           *args], capture_output=True, text=True, check=True, timeout=30).stdout.strip()


@pytest.fixture
def repo(src, tmp_path):  # noqa: F811
    """A git repo whose knowledge/ is the exported fixture, committed once; plus an away cache."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    k = root / "knowledge"
    shutil.copytree(src["out"], k)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    return {"k": k, "root": root, "cache": _cache(tmp_path / "away", dict(NAMES))}


def _commit(r, msg="two"):
    _git(r["root"], "add", "-A")
    _git(r["root"], "commit", "-qm", msg)


def _edit_json(p, fn):
    d = json.loads(p.read_text())
    fn(d)
    p.write_text(json.dumps(d))


def _own(cache, m):
    """The owner edits a macro in place (as the console's save would)."""
    from app.music_brain import db
    conn = mc._mdb(cache)
    with db.tx(conn):
        mc._put(conn, m)


def _macro_title(r, title):
    _edit_json(r["k"] / "macros" / "studied-s1-1.json", lambda d: d.update(title=title))


def test_stamps_are_commit_shas_and_hash_when_dirty_or_no_git(repo, src):  # noqa: F811
    head = _git(repo["root"], "rev-parse", "HEAD")
    st = ks.stamps(repo["k"])
    assert st and all(v == ("git", head) for v in st.values())
    _macro_title(repo, "edited, not committed")
    st = ks.stamps(repo["k"])
    assert st["macros/studied-s1-1.json"][0] == "hash" and st[kn.NAMES] == ("git", head)
    assert {k for k, _ in ks.stamps(src["out"]).values()} == {"hash"}, "no .git: content hash"


def test_unchanged_stamps_do_no_work(repo):
    first = ks.sync(repo["cache"], repo["k"])
    assert "macro:studied-s1-1" in first["added"] and first["pairs"] == 3
    again = ks.sync(repo["cache"], repo["k"])
    assert again["files_changed"] == [] and again["added"] == again["updated"] == []


def test_changed_file_updates_an_unmodified_seeded_row(repo):
    ks.sync(repo["cache"], repo["k"])
    created = mc.stored(repo["cache"])["studied-s1-1"]["created"]
    _macro_title(repo, "Anyma @ Atomium (re-reviewed)")
    _commit(repo)
    rep = ks.sync(repo["cache"], repo["k"])
    assert rep["files_changed"] == ["macros/studied-s1-1.json"]
    assert rep["updated"] == ["macro:studied-s1-1"] and rep["conflicts"] == []
    m = mc.stored(repo["cache"])["studied-s1-1"]
    assert m["title"] == "Anyma @ Atomium (re-reviewed)" and m["created"] == created


def test_locally_changed_row_is_kept_as_a_conflict_then_taken(repo):
    ks.sync(repo["cache"], repo["k"])
    mine = dict(mc.stored(repo["cache"])["studied-s1-1"], title="my own title")
    _own(repo["cache"], mine)
    _macro_title(repo, "published v2")
    _commit(repo)
    rep = ks.sync(repo["cache"], repo["k"])
    assert rep["kept"] == ["macro:studied-s1-1"] and rep["conflicts"] == ["macro:studied-s1-1"]
    assert mc.stored(repo["cache"])["studied-s1-1"]["title"] == "my own title"
    [c] = ks.conflicts(repo["cache"])
    assert c["item"] == "macro:studied-s1-1" and "my own title" in c["local"] and "published v2" in c["published"]
    ks.take("studied-s1-1", True, repo["cache"])
    assert mc.stored(repo["cache"])["studied-s1-1"]["title"] == "published v2"
    assert ks.conflicts(repo["cache"]) == []
    _macro_title(repo, "published v3")                 # taken: origin knowledge again, updates flow
    _commit(repo, "three")
    assert ks.sync(repo["cache"], repo["k"])["updated"] == ["macro:studied-s1-1"]


def test_take_local_keeps_the_owner_row(repo):
    ks.sync(repo["cache"], repo["k"])
    _own(repo["cache"], dict(mc.stored(repo["cache"])["studied-s1-1"], title="mine"))
    _macro_title(repo, "published v2")
    _commit(repo)
    ks.sync(repo["cache"], repo["k"])
    assert ks.take("macro:studied-s1-1", False, repo["cache"])["took"] == "local"
    assert ks.conflicts(repo["cache"]) == [] and mc.stored(repo["cache"])["studied-s1-1"]["title"] == "mine"
    with pytest.raises(ValueError):
        ks.take("macro:studied-s1-1", True, repo["cache"])


def test_hash_fallback_without_git(src, tmp_path):  # noqa: F811
    k, cache = src["out"], _cache(tmp_path / "away", dict(NAMES))
    ks.sync(cache, k)
    _edit_json(k / "macros" / "studied-s1-1.json", lambda d: d.update(title="hashed update"))
    rep = ks.sync(cache, k)
    assert rep["stamps"]["git"] == 0 and rep["files_changed"] == ["macros/studied-s1-1.json"]
    assert mc.stored(cache)["studied-s1-1"]["title"] == "hashed update"


def test_atlas_rules_mismatch_publishes_no_pairs_and_retries(repo, monkeypatch):
    real = pa.rules_hash
    monkeypatch.setattr(pa, "rules_hash", lambda: "other rules")
    rep = ks.sync(repo["cache"], repo["k"])
    assert rep["pairs"] == 0 and "stale" in str(rep["atlas"]) and pa.load(repo["cache"]) is None
    monkeypatch.setattr(pa, "rules_hash", real)
    rep = ks.sync(repo["cache"], repo["k"])
    assert rep["pairs"] == 3, "atlas stamps were not recorded while stale"


def test_published_pair_update_keeps_the_owner_played_evidence(repo):
    ks.sync(repo["cache"], repo["k"])
    doc = pa.load(repo["cache"])
    doc["pairs"][f"{A}>{B}"]["played"] = {"good": 7, "bad": 0}
    pa.write_atlas(doc, pa.atlas_path(repo["cache"]))
    shard = repo["k"] / "atlas" / "pairs" / f"{A}.json.gz"
    import gzip
    d = json.loads(gzip.decompress(shard.read_bytes()))
    d[f"{A}>{B}"]["works"] = 93
    shard.write_bytes(gzip.compress(json.dumps(d).encode(), mtime=0))
    _commit(repo)
    rep = ks.sync(repo["cache"], repo["k"])
    assert rep["updated"] == [f"pair:{A}>{B}"]
    p = pa.load(repo["cache"])["pairs"][f"{A}>{B}"]
    assert p["works"] == 93 and p["played"] == {"good": 7, "bad": 0} and "played_published" in p


def _learned(r, fn):
    _edit_json(r["k"] / kn.LEARNED, fn)
    _commit(r)


def _two_s1(d):
    o = d["bass_swap"]["observations"][0]
    d["bass_swap"]["observations"] = [dict(o, at=20.0), dict(o, at=40.0)]


def test_learned_set_is_replaced_as_a_unit(repo):
    ks.sync(repo["cache"], repo["k"])
    _learned(repo, _two_s1)
    rep = ks.sync(repo["cache"], repo["k"])
    assert rep["updated"] == ["learned:S1"] and rep["observations"] == 2
    obs = sl.load_learned(repo["cache"] / kn.LEARNED)["bass_swap"]["observations"]
    assert sorted(o["at"] for o in obs) == [20.0, 40.0]


def test_user_rules_block_a_learned_replacement(repo):
    ks.sync(repo["cache"], repo["k"])
    sl.add_user_rule("bass_swap", "only on drops", path=repo["cache"] / kn.LEARNED)
    _learned(repo, _two_s1)
    rep = ks.sync(repo["cache"], repo["k"])
    assert rep["conflicts"] == ["learned:S1"] and rep["updated"] == []
    obs = sl.load_learned(repo["cache"] / kn.LEARNED)["bass_swap"]["observations"]
    assert [o["at"] for o in obs] == [10.0]


def test_deleted_upstream_removes_only_unchanged_rows(repo):
    ks.sync(repo["cache"], repo["k"])
    _own(repo["cache"], dict(mc.stored(repo["cache"])["studied-s1-1"], title="mine"))
    _git(repo["root"], "rm", "-q", "knowledge/macros/chain-1-anyma.json", "knowledge/macros/studied-s1-1.json")
    _commit(repo)
    rep = ks.sync(repo["cache"], repo["k"])
    assert rep["removed"] == ["macro:chain-1-anyma"] and rep["kept"] == ["macro:studied-s1-1"]
    assert set(mc.stored(repo["cache"])) == {"studied-s1-1"}


def test_labels_follow_the_published_file(repo):
    _edit_json(repo["k"] / kn.NAMES, lambda d: None)
    (repo["k"] / kn.LABELS).write_text(json.dumps({A: {"genre": "techno", "era": "2020s"}}))
    _commit(repo)
    from app.music_brain.analysis import genre_labels as gl
    lp = gl.path(repo["cache"])
    ks.sync(repo["cache"], repo["k"])
    key = gl.name_key(NAMES[A])
    assert gl.load(lp)[0][key] == "techno"
    (repo["k"] / kn.LABELS).write_text(json.dumps({A: {"genre": "melodic techno", "era": "2020s"}}))
    _commit(repo, "three")
    assert ks.sync(repo["cache"], repo["k"])["updated"] == [f"label:{key}"]
    assert gl.load(lp)[0][key] == "melodic techno"


def test_dry_run_writes_nothing_and_user_db_is_untouched(repo):
    rep = ks.sync(repo["cache"], repo["k"], dry_run=True)
    assert rep["added"] and mc.stored(repo["cache"]) == {}
    assert ks.sync(repo["cache"], repo["k"])["added"] == rep["added"]
    assert not (repo["cache"] / "user.db").exists()


def test_cli_sync_and_conflicts(repo, capsys):
    assert kn.main(["sync", "--cache-dir", str(repo["cache"]), "--src", str(repo["k"])]) == 0
    assert json.loads(capsys.readouterr().out)["pairs"] == 3
    assert kn.main(["conflicts", "--cache-dir", str(repo["cache"])]) == 0
    assert json.loads(capsys.readouterr().out) == {"conflicts": []}
    assert kn.main(["take", "x", "--cache-dir", str(repo["cache"])]) == 1
