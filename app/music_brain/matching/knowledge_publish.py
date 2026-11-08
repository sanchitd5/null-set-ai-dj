"""KNOWLEDGE PUBLISH: the server contributes what it learned back to the dj-atlas repo.

app/music_brain/knowledge/ is its own git repo (sanchitd5/dj-atlas) mounted here as a submodule.
`export` (knowledge.py) writes the cache into that checkout in the tracked file formats; this module
commits the result and pushes it, merging with whatever the owner (or another machine) pushed
meanwhile, row by row, owner first:

    python3 -m app.music_brain.matching.knowledge publish [--no-push] [--dry-run]
    python3 -m app.music_brain.matching.knowledge update      git submodule update --remote, then sync
    python3 -m app.music_brain.matching.knowledge version     {sha, rules_hash, files, bytes}

Flow of one publish (all under a file lock; at most DJ_ATLAS_ATTEMPTS tries when the push races):
  1. fetch the remote branch (R). B = merge-base(HEAD, R): the last state this checkout shared with
     the remote. A dirty checkout is stashed first (dropped again when a fresh export reproduces it).
  2. the checkout moves to R and `sync` pulls R into the cache: rows the server did not touch take
     the owner's edits, rows it did touch become recorded CONFLICTS (knowledge_conflicts).
  3. `export` rewrites the checkout from the cache: S, deterministic and byte-stable.
  4. three-way merge B / S / R per ROW (macro file, label, learned technique, pair, track): a row the
     remote changed since B wins (owner-wins); a row only the server changed is published; a row with
     an open sync conflict stays as the remote has it until `take` resolves it. A remote on other
     atlas rules than the server wins the whole atlas folder, and the other way round when only the
     server moved. Played evidence is carried over when the remote's row lacks it.
  5. privacy scan of every changed file (knowledge.privacy_hits plus the repo's home-path rule),
     VERSION bump, commit with counts per store, push when DJ_ATLAS_PUSH=1. Nothing changed after
     the merge: no commit, no VERSION bump, no push.

Env (no keys in code): DJ_ATLAS_PUSH=1 pushes; DJ_ATLAS_REMOTE_URL sets the `origin` URL;
DJ_ATLAS_SSH_KEY is the path of the deploy key (used through GIT_SSH_COMMAND); DJ_ATLAS_BRANCH
(main); DJ_ATLAS_AUTOPUBLISH=1 arms the session-end hook and the daily thread; DJ_ATLAS_PUBLISH_HOURS
(24); DJ_ATLAS_AUTHOR_NAME / DJ_ATLAS_AUTHOR_EMAIL for the commits.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import shlex
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from app.music_brain.matching import knowledge as kn

REMOTE = "origin"
VERSION_FILE = "VERSION"
GIT_TIMEOUT_S = 300
MISSING = object()
# the repo's no-personal-paths test (app/tests/py/test_no_personal_paths.py), applied to the knowledge files
_HOME = re.compile(r"(?:/Users|/home)/([A-Za-z0-9._-]+)/")
_PLACEHOLDER_HOMES = {"me", "x", "someone"}
_lock = threading.Lock()


class PublishError(ValueError):
    """Publish stopped; nothing was pushed."""


# ------------------------------------------------------------------------------------------ env
def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def push_enabled() -> bool:
    return _truthy("DJ_ATLAS_PUSH")


def autopublish_enabled() -> bool:
    return _truthy("DJ_ATLAS_AUTOPUBLISH")


def _branch() -> str:
    b = os.environ.get("DJ_ATLAS_BRANCH", "main").strip() or "main"
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", b) or b.startswith("-") or ".." in b:
        raise PublishError(f"DJ_ATLAS_BRANCH {b!r} is not a plain branch name")
    return b


# ------------------------------------------------------------------------------------------ git
def _env() -> dict:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    key = os.environ.get("DJ_ATLAS_SSH_KEY", "").strip()
    if key:
        env["GIT_SSH_COMMAND"] = (f"ssh -i {shlex.quote(key)} -o IdentitiesOnly=yes "
                                  "-o StrictHostKeyChecking=accept-new -o BatchMode=yes")
    return env


def _identity() -> List[str]:
    return ["-c", f"user.name={os.environ.get('DJ_ATLAS_AUTHOR_NAME', 'dj-atlas-bot')}",
            "-c", f"user.email={os.environ.get('DJ_ATLAS_AUTHOR_EMAIL', 'dj-atlas-bot@users.noreply.invalid')}",
            "-c", "commit.gpgsign=false"]


def _run(repo: Path, *args: str, check: bool = True, binary: bool = False, stdin: Optional[bytes] = None):
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false", *_identity(), "-C", str(repo), *args],
                           capture_output=True, input=stdin, timeout=GIT_TIMEOUT_S, env=_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise PublishError(f"git {args[0]} failed: {exc}") from exc
    if check and r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip()
        raise PublishError(f"git {' '.join(args[:2])} failed: {err[:300]}")
    return r if binary else (r.returncode, r.stdout.decode("utf-8", "replace").strip())


def _git(repo: Path, *args: str) -> str:
    return _run(repo, *args)[1]


def _ok(repo: Path, *args: str) -> bool:
    return _run(repo, *args, check=False)[0] == 0


def _is_repo_root(out: Path) -> bool:
    try:
        top = _git(out, "rev-parse", "--show-toplevel")
    except PublishError:
        return False
    return Path(top).resolve() == out.resolve()


def _tree_files(repo: Path, rev: Optional[str]) -> Dict[str, bytes]:
    """{path: bytes} of every file in the tree of `rev`; {} for no revision."""
    if not rev:
        return {}
    names = [n for n in _git(repo, "ls-tree", "-r", "-z", "--name-only", rev).split("\0") if n and not _skip(n)]
    if not names:
        return {}
    r = _run(repo, "cat-file", "--batch", binary=True, stdin="".join(f"{rev}:{n}\n" for n in names).encode())
    out: Dict[str, bytes] = {}
    buf, pos = r.stdout, 0
    for n in names:
        end = buf.index(b"\n", pos)
        header = buf[pos:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise PublishError(f"cannot read {n} at {rev[:8]}")
        size = int(header[2])
        out[n] = buf[end + 1:end + 1 + size]
        pos = end + 1 + size + 1
    return out


def _skip(rel: str) -> bool:
    return rel == VERSION_FILE or rel.endswith(".tmp") or rel == ".git" or rel.startswith(".git/")


def _work_files(out: Path) -> Dict[str, bytes]:
    files: Dict[str, bytes] = {}
    for p in sorted(out.rglob("*")):
        rel = p.relative_to(out).as_posix()
        if p.is_file() and not _skip(rel):
            files[rel] = p.read_bytes()
    return files


# ------------------------------------------------------------------------------------- row model
def _kind(path: str) -> str:
    if path.startswith(kn.MACROS + "/") and path.endswith(".json"):
        return "macro"
    if path.startswith(f"{kn.ATLAS_DIR}/pairs/") and path.endswith(".json.gz"):
        return "pair"
    return {kn.NAMES: "names", kn.LABELS: "label", kn.LEARNED: "learned",
            f"{kn.ATLAS_DIR}/{kn.ATLAS_META}": "meta"}.get(path, "other")


def _is_atlas(path: str) -> bool:
    return path == kn.ATLAS or path.startswith(kn.ATLAS_DIR + "/")


def _decode(path: str, data: Optional[bytes]):
    if data is None:
        return MISSING
    try:
        raw = gzip.decompress(data) if path.endswith(".gz") else data
        return json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, EOFError):
        return MISSING


def _encode(path: str, obj) -> bytes:
    if path.endswith(".gz"):
        return gzip.compress(kn._dump(obj, compact=True), compresslevel=9, mtime=0)
    return kn._dump(obj)


def _rules(files: Dict[str, bytes]) -> Optional[str]:
    meta = _decode(f"{kn.ATLAS_DIR}/{kn.ATLAS_META}", files.get(f"{kn.ATLAS_DIR}/{kn.ATLAS_META}"))
    return meta.get("rules") if isinstance(meta, dict) else None


def _rhash(row) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


class _Ctx:
    """What the merge needs besides the three trees. `conflicts` are sync's open conflicts ("store:item");
    `kept_prev` the rows an earlier publish left as the remote had them ({row id: hash of that row}),
    which stay the remote's while the conflict is open and the remote's row has not changed again;
    `names` are the remote's track names (label rows are keyed by name there)."""

    def __init__(self, conflicts: Set[str], names: dict, kept_prev: Optional[Dict[str, str]] = None):
        from app.music_brain.analysis import genre_labels as gl
        self.conflicts, self.names, self._key = conflicts, names or {}, gl.name_key
        self.kept_prev = dict(kept_prev or {})
        self.kept: Dict[str, str] = {}          # row id -> hash of the remote's row, left as the remote has it

    def rid(self, kind: str, key: str) -> Optional[str]:
        if kind in ("macro", "pair", "learned"):
            return f"{kind}:{key}"
        if kind == "label":
            n = self.names.get(key)
            return f"label:{self._key(n)}" if n else None
        return None

    def _conflicted(self, rid: str, row) -> bool:
        if rid.startswith("learned:"):
            obs = row.get("observations") if isinstance(row, dict) else None
            return any(isinstance(o, dict) and f"learned:{o.get('set_id')}" in self.conflicts for o in obs or [])
        return rid in self.conflicts

    def still_theirs(self, kind: str, key: str, row_r) -> bool:
        """An earlier publish kept the remote's row and that row is unchanged. A macro or learned technique is
        released by `take --local` (sync drops the conflict); sync records no conflict for a pair or a label
        (it just keeps the local row), so those stay the remote's until the owner edits the row again."""
        rid = self.rid(kind, key)
        if not rid or self.kept_prev.get(rid) != _rhash(row_r):
            return False
        return rid.startswith(("pair:", "label:")) or self._conflicted(rid, row_r)

    def keep(self, kind: str, key: str, row_r) -> None:
        rid = self.rid(kind, key)
        if rid:
            self.kept[rid] = _rhash(row_r)

    def touches(self, path: str, kind: str) -> bool:
        """True when an earlier publish kept a row of this file (the whole-file shortcuts must not run)."""
        pre = {"macro": f"macro:{path[len(kn.MACROS) + 1:-5]}", "label": "label:", "learned": "learned:"}.get(kind)
        if kind == "pair":
            pre = "pair:" + path[len(kn.ATLAS_DIR) + 7:-len(".json.gz")] + ">"
        if not pre or not self.kept_prev:
            return False
        return any(k == pre or k.startswith(pre) for k in self.kept_prev)


def _resolve(kind: str, key: str, vb, vs, vr, ctx: _Ctx):
    """One row: the value to publish (MISSING: the row is gone). The remote's row wins when it changed
    since the base; the server's wins only when the remote did not touch the row."""
    if vs == vr:
        return vs
    if vr == vb:
        if ctx.still_theirs(kind, key, vr):             # an unresolved conflict from an earlier publish
            ctx.keep(kind, key, vr)
            return vr
        return vs                                       # only the server changed it
    if vs == vb:
        return vr                                       # only the remote did
    ctx.keep(kind, key, vr)                             # both changed: the owner's row, plus our evidence
    if kind == "pair" and isinstance(vr, dict) and isinstance(vs, dict) and "played" not in vr and "played" in vs:
        return dict(vr, played=vs["played"])
    return vr


def _merge_rows(kind: str, ob, os_, or_, ctx: _Ctx):
    out: dict = {}
    for k in sorted(set(ob) | set(os_) | set(or_)):
        v = _resolve(kind, k, ob.get(k, MISSING), os_.get(k, MISSING), or_.get(k, MISSING), ctx)
        if v is not MISSING:
            out[k] = v
    return out


def _merge_file(path: str, b: Optional[bytes], s: Optional[bytes], r: Optional[bytes], ctx: _Ctx) -> Optional[bytes]:
    kind = _kind(path)
    if s == r:
        return s
    if not ctx.touches(path, kind):
        if r == b:
            return s                                    # only the server changed it
        if s == b:
            return r                                    # only the remote did
    if kind == "macro":                                 # one row per file
        name = path[len(kn.MACROS) + 1:-5]
        ob, os_, or_ = (_decode(path, x) for x in (b, s, r))
        v = _resolve("macro", name, ob, os_, or_, ctx)
        return None if v is MISSING else (r if v == or_ else s if v == os_ else _encode(path, v))
    if kind in ("names", "label", "learned", "pair"):
        ob, os_, or_ = (_decode(path, x) for x in (b, s, r))
        ob = {} if ob is MISSING else ob
        if not all(isinstance(x, dict) for x in (ob, os_, or_)):
            return r
        merged = _merge_rows(kind, ob, os_, or_, ctx)
        if not merged and kind == "pair":
            return None
        if merged == or_:
            return r
        if merged == os_:
            return s
        return _encode(path, merged)
    return r


def _merge_meta(path: str, b, s, r, files: Dict[str, bytes], ctx: _Ctx) -> Optional[bytes]:
    """meta.json after the pair files are merged: tracks per row, the rest per key; shards and stats derived."""
    ob, os_, or_ = (_decode(path, x) for x in (b, s, r))
    if not isinstance(os_, dict) and not isinstance(or_, dict):
        return r if r is not None else s
    if not isinstance(os_, dict):
        return r
    if not isinstance(or_, dict):
        return s
    ob = ob if isinstance(ob, dict) else {}
    meta: dict = {}
    for k in sorted(set(ob) | set(os_) | set(or_)):
        if k in ("shards", "stats"):
            continue
        vb, vs, vr = ob.get(k, MISSING), os_.get(k, MISSING), or_.get(k, MISSING)
        if k == "tracks" and all(isinstance(x, dict) for x in (vs, vr)):
            v = _merge_rows("track", vb if isinstance(vb, dict) else {}, vs, vr, ctx)
        else:
            v = vs if vs == vr or vr == vb else (vr if vs == vb else vr)
        if v is not MISSING:
            meta[k] = v
    pairs = {p[len(kn.ATLAS_DIR) + 7:-len(".json.gz")]: p for p in files if _kind(p) == "pair"}
    meta["shards"] = sorted(pairs)
    stats = dict(os_.get("stats") or {})
    stats.update({k: v for k, v in (or_.get("stats") or {}).items() if (ob.get("stats") or {}).get(k) != v})
    n_pairs = 0
    for p in pairs.values():
        d = _decode(p, files[p])
        n_pairs += len(d) if isinstance(d, dict) else 0
    stats["pairs"], stats["tracks"] = n_pairs, len(meta.get("tracks") or {})
    meta["stats"] = stats
    if meta == or_:
        return r
    if meta == os_:
        return s
    return _encode(path, meta)


def merge_trees(base: Dict[str, bytes], mine: Dict[str, bytes], theirs: Dict[str, bytes],
                ctx: _Ctx) -> Tuple[Dict[str, bytes], dict]:
    """Three-way merge of whole knowledge trees (B, S, R) by row; see the module doc. Returns
    (merged files, notes)."""
    notes: dict = {"atlas": "merged"}
    meta_path = f"{kn.ATLAS_DIR}/{kn.ATLAS_META}"
    rb, rs, rr = _rules(base), _rules(mine), _rules(theirs)
    wholesale: Optional[Dict[str, bytes]] = None
    if rs != rr and rr is not None:
        if rr != rb:
            wholesale, notes["atlas"] = theirs, "remote atlas is on other rules: kept"
        elif rs != rb:
            wholesale, notes["atlas"] = mine, "server atlas is on new rules: replaces the published one"
    out: Dict[str, bytes] = {}
    for path in sorted(set(base) | set(mine) | set(theirs)):
        if path == meta_path:
            continue
        if wholesale is not None and _is_atlas(path):
            data = wholesale.get(path)
        else:
            data = _merge_file(path, base.get(path), mine.get(path), theirs.get(path), ctx)
        if data is not None:
            out[path] = data
    if wholesale is not None:
        data = wholesale.get(meta_path)
    else:
        data = _merge_meta(meta_path, base.get(meta_path), mine.get(meta_path), theirs.get(meta_path), out, ctx)
    if data is not None:
        out[meta_path] = data
    notes["kept_remote_rows"] = sorted(ctx.kept)
    return out, notes


# ------------------------------------------------------------------------------- counts, privacy
def _rows(files: Dict[str, bytes]) -> Dict[str, Dict[str, object]]:
    """store -> {row key: row} for the files given."""
    stores: Dict[str, Dict[str, object]] = {"macros": {}, "pairs": {}, "learned": {}, "labels": {}, "names": {}}
    for path, data in files.items():
        kind = _kind(path)
        if kind == "macro":
            m = _decode(path, data)
            if isinstance(m, dict):
                stores["macros"][path] = {k: v for k, v in m.items() if k != "created"}
            continue
        store = {"pair": "pairs", "learned": "learned", "label": "labels", "names": "names"}.get(kind)
        d = _decode(path, data) if store else MISSING
        if isinstance(d, dict):
            stores[store].update({f"{path}#{k}": v for k, v in d.items()})
    return stores


def count_changes(before: Dict[str, bytes], after: Dict[str, bytes]) -> dict:
    """Rows added / changed / removed per store between two trees, plus pairs with new played evidence."""
    changed_paths = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    a, b = _rows({p: before[p] for p in changed_paths if p in before}), _rows({p: after[p] for p in changed_paths if p in after})
    out = {}
    for store in a:
        keys = set(a[store]) | set(b[store])
        out[store] = sum(1 for k in keys if a[store].get(k, MISSING) != b[store].get(k, MISSING))
    out["played"] = sum(1 for k in set(a["pairs"]) | set(b["pairs"])
                        if (a["pairs"].get(k) or {}).get("played") != (b["pairs"].get(k) or {}).get("played")
                        and (b["pairs"].get(k) or {}).get("played") is not None)
    return out


def privacy_scan(files: Dict[str, bytes], only: Optional[Set[str]] = None) -> List[str]:
    """Absolute paths, home dirs and e-mail addresses (knowledge.privacy_hits) and real /Users/<name>/ or
    /home/<name>/ paths (the repo's test_no_personal_paths rule) in the given knowledge files."""
    hits: List[str] = []
    for path in sorted(files if only is None else only & set(files)):
        obj = _decode(path, files[path])
        if obj is MISSING:
            text = files[path].decode("utf-8", "replace") if not path.endswith(".gz") else ""
            obj = text
        hits += kn.privacy_hits(obj, path)
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
        hits += [f"{path}: {m.group(0)}" for m in _HOME.finditer(text) if m.group(1) not in _PLACEHOLDER_HOMES]
    return hits


# ----------------------------------------------------------------------------------- the publish
def _file_lock(gitdir: Path):
    class _L:
        def __enter__(self_):
            self_.f = open(gitdir / "dj-atlas-publish.lock", "a+")
            try:
                import fcntl
                fcntl.flock(self_.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:
                pass
            except OSError:
                self_.f.close()
                raise PublishError("another publish is running")
            return self_

        def __exit__(self_, *a):
            self_.f.close()
    return _L()


def _ensure_remote(out: Path, url: Optional[str]) -> None:
    url = (url or os.environ.get("DJ_ATLAS_REMOTE_URL", "")).strip()
    have = _run(out, "remote", "get-url", REMOTE, check=False)
    if url:
        if url.startswith("-"):
            raise PublishError("DJ_ATLAS_REMOTE_URL must not start with '-'")
        if have[0] != 0:
            _git(out, "remote", "add", REMOTE, url)
        elif have[1] != url:
            _git(out, "remote", "set-url", REMOTE, url)
    elif have[0] != 0:
        raise PublishError(f"no remote: set DJ_ATLAS_REMOTE_URL (or `git remote add {REMOTE} ...` in the checkout)")


def _fetch(out: Path, branch: str) -> Optional[str]:
    """Fetch the remote branch; its commit sha, None when the remote has no such branch yet."""
    rc, listing = _run(out, "ls-remote", "--heads", REMOTE, f"refs/heads/{branch}", check=False)
    if rc != 0:
        raise PublishError(f"cannot reach the {REMOTE} remote: {listing[:200]}")
    if not listing:
        return None
    _git(out, "fetch", "-q", REMOTE, f"+refs/heads/{branch}:refs/remotes/{REMOTE}/{branch}")
    return _git(out, "rev-parse", f"refs/remotes/{REMOTE}/{branch}")


STATE = "knowledge_publish.json"          # in the cache dir: rows an earlier publish left as the remote had them


def _json_or(data: Optional[bytes], default):
    try:
        return json.loads(data.decode("utf-8")) if data is not None else default
    except ValueError:
        return default


def _load_state(cache: Path) -> dict:
    s = kn._read(cache / STATE, {})
    return s if isinstance(s, dict) else {}


def _save_state(cache: Path, state: dict) -> None:
    try:
        kn._write_bytes(cache / STATE, kn._dump(state))
    except OSError:
        pass


def _conflict_items(cache: Path) -> Set[str]:
    try:
        from app.music_brain.matching import knowledge_sync
        return {c["item"] for c in knowledge_sync.conflicts(cache)}
    except Exception:  # noqa: BLE001 -- a broken provenance table must not stop the merge; rows still 3-way
        return set()


def _message(counts: dict, rules: Optional[str], base: Optional[str]) -> str:
    parts = [f"{counts['macros']} macros", f"{counts['pairs']} pairs", f"{counts['learned']} learned techniques",
             f"{counts['labels']} labels", f"{counts['names']} names"]
    shown = [p for p in parts if not p.startswith("0 ")]
    subject = "knowledge: server publish (" + (", ".join(shown) or "metadata only") + ")"
    body = ["Rows added, changed or removed per store:"] + [f"  {p}" for p in parts]
    body.append(f"  {counts['played']} pairs with new played evidence")
    body.append(f"Atlas rules {rules or 'none'}; on top of {base[:12] if base else 'an empty remote'}.")
    return subject + "\n\n" + "\n".join(body)


def _version_bytes(rules: Optional[str], base: Optional[str]) -> bytes:
    return kn._dump({"rules_hash": rules, "sha": base or ""})


def _attempt(cache: Path, out: Path, branch: str, push: bool, dry: bool, log) -> dict:
    remote_sha = _fetch(out, branch)
    head = _run(out, "rev-parse", "--verify", "-q", "HEAD", check=False)[1] or None
    base_sha = None                     # the last state this checkout shared with the remote (None: nothing shared)
    if head and remote_sha:
        rc, mb = _run(out, "merge-base", head, remote_sha, check=False)
        base_sha = mb if rc == 0 and mb else None
    rep: dict = {"remote": remote_sha, "base": base_sha, "stashed": False}
    theirs = _tree_files(out, remote_sha)
    base = theirs if base_sha == remote_sha else _tree_files(out, base_sha)
    work, tmp = out, None
    if dry:                                 # a dry run exports into a scratch copy of the remote's tree
        import tempfile
        tmp = tempfile.TemporaryDirectory(prefix="dj-atlas-dry-")
        work = Path(tmp.name)
        for rel, data in theirs.items():
            kn._write_bytes(work / rel, data)
    try:
        dirty = _work_files(out) if head and not dry and _git(out, "status", "--porcelain") else None
        if dirty:
            _git(out, "stash", "push", "-q", "--include-untracked", "-m", "dj-atlas publish: pre-publish worktree")
        if remote_sha and not dry:
            _git(out, "checkout", "-q", "-f", "-B", branch, remote_sha)
        elif not remote_sha and not head and not dry:
            _git(out, "checkout", "-q", "-B", branch)
        if not dry and remote_sha:                   # an empty remote has nothing to pull (and would read as "all rows removed")
            try:
                from app.music_brain.matching import knowledge_sync
                sync = knowledge_sync.sync(cache, out)
                rep["sync"] = {k: len(sync[k]) for k in ("added", "updated", "removed", "kept", "conflicts")}
            except Exception as exc:  # noqa: BLE001 -- the merge still protects upstream rows by the 3-way rule
                rep["sync"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        exp = kn.export(cache, work, log)
        rep["export"] = {k: exp[k] for k in ("macros", "observations", "names") if k in exp}
        if dirty:
            if dirty == _work_files(out):
                _git(out, "stash", "drop", "-q")       # it was only an earlier export: reproduced
            else:
                rep["stashed"] = True
                log("knowledge publish: the checkout had changes an export does not reproduce; kept in `git stash`")
        mine = _work_files(work)
        return _finish(cache, out, branch, push, dry, rep, base, mine, theirs, remote_sha)
    finally:
        if tmp is not None:
            tmp.cleanup()


def _finish(cache: Path, out: Path, branch: str, push: bool, dry: bool, rep: dict, base, mine, theirs,
            remote_sha: Optional[str]) -> dict:
    ctx = _Ctx(_conflict_items(cache), _json_or(theirs.get(kn.NAMES), {}), _load_state(cache).get("kept"))
    merged, notes = merge_trees(base, mine, theirs, ctx)
    if not dry:
        _save_state(cache, {"kept": ctx.kept})
    rep.update(notes)
    changed = {p for p in set(merged) | set(theirs) if merged.get(p) != theirs.get(p)}
    counts = count_changes(theirs, merged)
    rep["counts"], rep["files_changed"] = counts, len(changed)
    rep["changed"] = sorted(changed)[:20]
    if not changed:
        rep["status"] = "noop"
        _apply(out, merged, theirs, mine)
        return rep
    hits = privacy_scan(merged, changed)
    if hits:
        raise PublishError(f"publish refused, {len(hits)} private strings: " + "; ".join(hits[:5]))
    if dry:
        rep["status"] = "dry-run"
        return rep
    rules = _rules(merged)
    _apply(out, merged, theirs, mine)
    (out / VERSION_FILE).write_bytes(_version_bytes(rules, remote_sha))
    _git(out, "add", "-A")
    _git(out, "commit", "-q", "-m", _message(counts, rules, remote_sha))
    rep["commit"] = _git(out, "rev-parse", "HEAD")
    rep["status"] = "committed"
    if push:
        rc, err = _run(out, "push", "-q", REMOTE, f"HEAD:refs/heads/{branch}", check=False)
        if rc != 0:
            rep["status"] = "rejected"
            rep["error"] = err[:200]
        else:
            rep["status"] = "pushed"
    return rep


def _apply(out: Path, merged: Dict[str, bytes], theirs: Dict[str, bytes], mine: Dict[str, bytes]) -> None:
    """Make the worktree hold `merged`: files written when their bytes differ, files gone removed."""
    for rel, data in merged.items():
        p = out / rel
        if mine.get(rel) != data or not p.exists():
            kn._write_bytes(p, data)
    for rel in set(mine) | set(theirs):
        if rel not in merged and (out / rel).exists():
            (out / rel).unlink()
    for d in sorted((p for p in out.rglob("*") if p.is_dir() and ".git" not in p.relative_to(out).parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass


def publish(cache_dir: Optional[Path] = None, out: Optional[Path] = None, push: Optional[bool] = None,
            dry_run: bool = False, remote_url: Optional[str] = None,
            log: Callable[[str], None] = lambda m: None) -> dict:
    """Export the cache into the knowledge checkout, merge with the remote (owner wins), commit with
    counts per store and push (push=None: DJ_ATLAS_PUSH). PublishError when it cannot proceed."""
    cache, out = kn._cache(cache_dir), Path(out or kn.KNOWLEDGE_DIR)
    push = push_enabled() if push is None else push
    if not out.is_dir() or not _is_repo_root(out):
        raise PublishError(f"{out} is not the root of its own git checkout (the dj-atlas submodule)")
    branch = _branch()
    _ensure_remote(out, remote_url)
    gitdir = Path(_git(out, "rev-parse", "--absolute-git-dir"))
    attempts = max(1, int(os.environ.get("DJ_ATLAS_ATTEMPTS", "3") or 3))
    if not _lock.acquire(blocking=False):
        raise PublishError("another publish is running")
    try:
        with _file_lock(gitdir):
            rep: dict = {}
            for n in range(1, attempts + 1):
                try:
                    rep = _attempt(cache, out, branch, push, dry_run, log)
                except PublishError:
                    raise
                except ValueError as exc:               # knowledge.export refused: private strings
                    raise PublishError(str(exc)) from exc
                rep["attempts"] = n
                if rep["status"] != "rejected":
                    break
                log("knowledge publish: push rejected (the remote moved), merging again")
            if rep["status"] == "rejected":
                raise PublishError(f"push rejected {attempts} times: {rep.get('error')}")
    finally:
        _lock.release()
    log(f"knowledge publish: {rep['status']}, {rep['counts']}")
    return rep


def publish_safe(*a, **kw) -> dict:
    """publish that never raises ({"error": str} instead): the background callers."""
    try:
        return publish(*a, **kw)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


# ------------------------------------------------------------------------------------- update
def update(cache_dir: Optional[Path] = None, out: Optional[Path] = None,
           log: Callable[[str], None] = lambda m: None) -> dict:
    """`git submodule update --remote` for the knowledge checkout, then `sync`. Safe on a dirty tree:
    local changes are stashed and put back (a failed pop leaves the stash and says so)."""
    cache, out = kn._cache(cache_dir), Path(out or kn.KNOWLEDGE_DIR)
    if not out.is_dir() or not _is_repo_root(out):
        raise PublishError(f"{out} is not the root of its own git checkout (the dj-atlas submodule)")
    rep: dict = {"stashed": False, "restored": None}
    dirty = bool(_git(out, "status", "--porcelain"))
    if dirty:
        _git(out, "stash", "push", "-q", "--include-untracked", "-m", "dj-atlas update: worktree")
        rep["stashed"] = True
    try:
        sup = _git(out, "rev-parse", "--show-superproject-working-tree")
        before = _run(out, "rev-parse", "--verify", "-q", "HEAD", check=False)[1]
        if sup:
            rel = out.resolve().relative_to(Path(sup).resolve()).as_posix()
            _git(Path(sup), "submodule", "update", "--init", "--remote", "--", rel)
        else:
            _ensure_remote(out, None)
            branch = _branch()
            if _fetch(out, branch):
                _git(out, "merge", "-q", "--ff-only", f"refs/remotes/{REMOTE}/{branch}")
        rep["sha"] = _run(out, "rev-parse", "--verify", "-q", "HEAD", check=False)[1]
        rep["moved"] = rep["sha"] != before
    finally:
        if rep["stashed"]:
            rc, err = _run(out, "stash", "pop", "-q", check=False)
            rep["restored"] = rc == 0
            if rc != 0:
                rep["stash_kept"] = err[:200]
                log("knowledge update: your local changes did not re-apply cleanly; they stay in `git stash`")
    from app.music_brain.matching import knowledge_sync
    sync = knowledge_sync.sync(cache, out, log=log)
    rep["sync"] = {k: len(sync[k]) for k in ("added", "updated", "removed", "kept", "conflicts")}
    return rep


# ------------------------------------------------------------------------------ version, snapshot
def _snapshot_files(src: Path) -> Dict[str, bytes]:
    """The knowledge files a client needs, relative paths: the same set `knowledge_sync` reads."""
    from app.music_brain.matching import knowledge_sync
    return {rel: (src / rel).read_bytes() for rel in knowledge_sync._files(src)}


def _head(src: Path) -> Tuple[str, bool]:
    """(HEAD sha or the VERSION file's, worktree differs from HEAD)."""
    try:
        sha = _git(src, "rev-parse", "--verify", "-q", "HEAD")
        dirty = bool(_git(src, "status", "--porcelain", "--", "."))
        return sha, dirty
    except PublishError:
        pass
    v = kn._read(src / VERSION_FILE, {}) or {}
    return str(v.get("sha") or ""), False


def _version(src: Path, files: Dict[str, bytes]) -> dict:
    sha, dirty = _head(src) if src.is_dir() else ("", False)
    return {"sha": sha, "rules_hash": _rules(files), "files": len(files),
            "bytes": sum(len(v) for v in files.values()), "dirty": dirty}


def version(src: Optional[Path] = None) -> dict:
    """{sha, rules_hash, files, bytes, dirty}: what the knowledge checkout holds. `sha` is the
    checkout's commit; `dirty` says files differ from it (the ETag then carries a content hash)."""
    src = Path(src or kn.KNOWLEDGE_DIR)
    return _version(src, _snapshot_files(src) if src.is_dir() else {})


def _etag(v: dict, files: Dict[str, bytes]) -> str:
    import hashlib
    tag = v["sha"] or "nogit"
    if v["dirty"] or not v["sha"]:
        h = hashlib.sha256()
        for rel, data in sorted(files.items()):
            h.update(rel.encode() + b"\0" + data)
        tag += "+" + h.hexdigest()[:12]
    return f'"{tag}"'


def etag(src: Optional[Path] = None) -> str:
    """Strong validator for the snapshot: the commit sha; with local changes, sha + a content hash."""
    src = Path(src or kn.KNOWLEDGE_DIR)
    files = _snapshot_files(src) if src.is_dir() else {}
    return _etag(_version(src, files), files)


_snap_cache: Dict[str, bytes] = {}


def snapshot_zip(src: Optional[Path] = None) -> Tuple[str, bytes]:
    """(etag, zip) of the knowledge in the layout the phone's bundled assets/atlas uses: meta.json,
    names.json, genre_labels.json, learned_techniques.json, macros.json ({"macros": [...]}), pairs/<a>.json.gz,
    plus version.json. Deterministic bytes; the one built snapshot is kept for its etag."""
    src = Path(src or kn.KNOWLEDGE_DIR)
    files = _snapshot_files(src) if src.is_dir() else {}
    v = _version(src, files)
    tag = _etag(v, files)
    key = f"{src.resolve()}|{tag}"
    if key in _snap_cache:
        return tag, _snap_cache[key]
    macros = []
    for rel in sorted(files):
        if _kind(rel) == "macro":
            m = _decode(rel, files[rel])
            if isinstance(m, dict):
                macros.append(m)
    members: Dict[str, bytes] = {}
    for rel, data in files.items():
        kind = _kind(rel)
        if kind == "pair":
            members["pairs/" + rel.rsplit("/", 1)[1]] = data
        elif kind == "meta":
            members["meta.json"] = data
        elif kind in ("names", "label", "learned"):
            members[rel] = data
    members["macros.json"] = kn._dump({"macros": macros}, compact=True)
    members["version.json"] = kn._dump({"sha": v["sha"], "rules_hash": v["rules_hash"], "files": v["files"]})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in sorted(members):
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_STORED if name.endswith(".gz") else zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, members[name])
    _snap_cache.clear()
    _snap_cache[key] = buf.getvalue()
    return tag, _snap_cache[key]


# --------------------------------------------------------------------------- background schedule
_state = {"running": False, "last": None, "pending": False}
_state_lock = threading.Lock()
_stop = threading.Event()
_timer: Optional[threading.Thread] = None


def _run_background(log) -> None:
    with _state_lock:
        if _state["running"]:
            _state["pending"] = True
            return
        _state["running"] = True
    try:
        while True:
            _state["last"] = {"at": time.time(), **publish_safe(log=log)}
            if _state["last"].get("error"):
                log(f"knowledge publish failed: {_state['last']['error']}")
            with _state_lock:
                if not _state["pending"]:
                    return
                _state["pending"] = False
    finally:
        with _state_lock:
            _state["running"] = False


def on_set_end(delay_s: float = 5.0, log: Callable[[str], None] = lambda m: None) -> bool:
    """Session-end hook (the set lease was released): publish in the background when
    DJ_ATLAS_AUTOPUBLISH is on. Calls during a run coalesce into one more run. Returns True when scheduled."""
    if not autopublish_enabled():
        return False
    t = threading.Timer(delay_s, _run_background, args=(log,))
    t.daemon = True
    t.start()
    return True


def start_timer(log: Callable[[str], None] = lambda m: None, hours: Optional[float] = None,
                period_s: Optional[float] = None) -> bool:
    """The daily thread (server startup): publishes every DJ_ATLAS_PUBLISH_HOURS (24) while
    DJ_ATLAS_AUTOPUBLISH is on. One thread per process."""
    global _timer
    if not autopublish_enabled() or (_timer is not None and _timer.is_alive()):
        return False
    if hours is None:
        try:
            hours = float(os.environ.get("DJ_ATLAS_PUBLISH_HOURS", "24"))
        except ValueError:
            hours = 24.0
    period = period_s if period_s else max(0.25, hours) * 3600
    _stop.clear()

    def loop():
        while not _stop.wait(period):
            _run_background(log)

    _timer = threading.Thread(target=loop, name="dj-atlas-publish", daemon=True)
    _timer.start()
    return True


def stop_timer() -> None:
    _stop.set()
