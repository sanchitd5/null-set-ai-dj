"""KNOWLEDGE SYNC: pulled updates to the tracked knowledge/ folder reach an install that
already seeded it, while the owner's own changes still win.

Owner: "use latest commit sha on any x file as check if anything changed in atlas knowledge".

* VERSION STAMP per knowledge file: the newest git commit that touched it (one `git log
  --name-only` over the folder, walked newest first). A file git does not vouch for (no git,
  no .git, untracked, or edited since its commit) is stamped with its sha256 instead. The
  stamps of the last sync live in app.db (knowledge_files); an unchanged stamp skips the file.
* PROVENANCE per seeded row (knowledge_rows): origin "knowledge" or "local", the file and stamp
  it came from, seeded_hash (hash of the row as written) and pub_hash (the published version
  last seen). A row whose content no longer equals seeded_hash was changed by the owner or the
  app: it is kept and the published change is recorded as a CONFLICT (knowledge_conflicts),
  resolved with `take`.
* Stores: macros (one per file), learned observations (a set is one unit; user rules or a
  disabled flag on its kinds block replacement), atlas pairs (only when the published rules
  hash equals the local code's; the owner's PLAYED evidence is never overwritten, a published
  one is kept apart as played_published), genre / era labels (by title key).

    python3 -m app.music_brain.matching.knowledge sync [--dry-run]
    python3 -m app.music_brain.matching.knowledge conflicts
    python3 -m app.music_brain.matching.knowledge take <store:item> --published|--local
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from app.music_brain.matching import knowledge as kn

SYNC_STEPS = (
    """CREATE TABLE knowledge_files (path TEXT PRIMARY KEY, kind TEXT NOT NULL, stamp TEXT NOT NULL);
    CREATE TABLE knowledge_rows (store TEXT NOT NULL, item TEXT NOT NULL, origin TEXT NOT NULL,
        source_path TEXT, source_sha TEXT, seeded_hash TEXT, pub_hash TEXT, PRIMARY KEY (store, item));
    CREATE TABLE knowledge_conflicts (store TEXT NOT NULL, item TEXT NOT NULL, path TEXT,
        local TEXT, published TEXT, data TEXT, at REAL NOT NULL, PRIMARY KEY (store, item))""",
)
LIBRARY = "<library>"                  # knowledge_files row: the local library's own stamp
CONFLICT_STORES = ("macro", "learned")  # a pre-existing local row that differs is a conflict
GIT_TIMEOUT_S = 10


# ---------------------------------------------------------------------------------- stamps
def _files(src: Path) -> List[str]:
    """The knowledge files a sync reads, relative posix paths."""
    out = [r for r in (kn.NAMES, kn.LABELS, kn.LEARNED, kn.ATLAS, f"{kn.ATLAS_DIR}/{kn.ATLAS_META}")
           if (src / r).is_file()]
    for sub, pat in ((kn.MACROS, "*.json"), (f"{kn.ATLAS_DIR}/pairs", "*.json.gz")):
        if (src / sub).is_dir():
            out += [f"{sub}/{p.name}" for p in (src / sub).glob(pat) if p.is_file()]
    return sorted(out)


def _git(src: Path, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false", "-C", str(src), *args],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _git_shas(src: Path) -> Dict[str, str]:
    """path -> newest commit sha, for files whose working copy is that commit's; {} without git."""
    log = _git(src, "log", "--format=%x00%H", "--name-only", "--relative", "--", ".")
    diff = _git(src, "diff", "HEAD", "--name-only", "--relative", "-z", "--", ".") if log else None
    extra = _git(src, "ls-files", "-o", "--exclude-standard", "-z", "--", ".") if diff is not None else None
    if log is None or diff is None or extra is None:
        return {}
    dirty = set(filter(None, (diff + extra).split("\0")))
    shas: Dict[str, str] = {}
    sha = None
    for line in log.split("\n"):
        if line.startswith("\0"):
            sha = line[1:].strip()
        elif line.strip() and sha and line.strip() not in shas:
            shas[line.strip()] = sha
    return {p: s for p, s in shas.items() if p not in dirty}


def stamps(src: Path) -> Dict[str, Tuple[str, str]]:
    """{relative path: (kind "git" | "hash", commit sha | sha256)} for every knowledge file."""
    shas = _git_shas(src)
    out: Dict[str, Tuple[str, str]] = {}
    for rel in _files(src):
        if rel in shas:
            out[rel] = ("git", shas[rel])
            continue
        try:
            out[rel] = ("hash", hashlib.sha256((src / rel).read_bytes()).hexdigest())
        except OSError:
            continue
    return out


def _library_stamp(cache: Path) -> str:
    """Changes when a song arrives or is renamed: a macro or pair skipped as unresolved may resolve now."""
    h = hashlib.sha256()
    up = cache / "uploads"
    for q in sorted(up.iterdir()) if up.is_dir() else []:
        h.update(q.name.encode() + b";")
    for q in (up / "_names.json", cache / "track_aliases.json"):
        try:
            st = q.stat()
            h.update(f"{st.st_size}:{st.st_mtime_ns};".encode())
        except OSError:
            h.update(b"-;")
    return h.hexdigest()[:16]


# ------------------------------------------------------------------------- row content
def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
                          .encode("utf-8")).hexdigest()[:16]


def _core(store: str, row):
    """The part of a row that is compared: a macro's created and a pair's played evidence are
    the local install's own, never a difference."""
    if isinstance(row, dict) and store == "macro":
        return {k: v for k, v in row.items() if k != "created"}
    if isinstance(row, dict) and store == "pair":
        return {k: v for k, v in row.items() if k not in ("played", "played_published")}
    return row


def _summary(store: str, row) -> Optional[str]:
    if row is None:
        return None
    if store == "macro":
        return f"{row.get('title') or row.get('name')} ({len(row.get('steps') or [])} steps)"
    if store == "learned":
        kinds = sorted({o.get("kind") for o in row})
        return f"{len(row)} observations ({', '.join(kinds)})"
    if store == "pair":
        return f"works {row.get('works')}, {row.get('recipe') or row.get('best')}"
    return f"{row.get('genre')} / {row.get('era')}"


def _conn(cache: Path):
    from app.music_brain import db
    conn = db.connect(db.db_path(cache))
    db.ensure(conn, "knowledge", SYNC_STEPS)
    return conn


def _obs_list(obs: List[dict]) -> List[dict]:
    return sorted(obs, key=lambda o: json.dumps(o, sort_keys=True, default=str))


def _observations(sl, rows: List[dict]) -> List[dict]:
    names = {f.name for f in fields(sl.Observation)}
    out = []
    for o in rows:
        if isinstance(o, dict) and o.get("kind") in sl.KINDS and o.get("set_id"):
            try:
                out.append(asdict(sl.Observation(**{k: v for k, v in o.items() if k in names})))
            except TypeError:
                continue
    return out


# ------------------------------------------------------------------------------- sync
class _Sync:
    def __init__(self, cache: Path, src: Path, dry: bool):
        self.cache, self.src, self.dry = cache, src, dry
        self.conn = _conn(cache)
        self.prov = {(r[0], r[1]): dict(origin=r[2], path=r[3], sha=r[4], seeded=r[5], pub=r[6])
                     for r in self.conn.execute("SELECT store, item, origin, source_path, source_sha, "
                                                "seeded_hash, pub_hash FROM knowledge_rows")}
        self.prov_out: Dict[Tuple[str, str], Optional[dict]] = {}
        self.conf_out: Dict[Tuple[str, str], Optional[tuple]] = {}
        self.hold: Set[str] = set()        # files not synced (stale atlas): stamp not recorded
        self.now: Dict[str, Tuple[str, str]] = {}
        self.rep: dict = {"dry_run": dry, "files_changed": [], "updated": [], "added": [], "removed": [],
                          "kept": [], "conflicts": [], "macros": [], "macros_skipped": [],
                          "observations": 0, "pairs": 0, "atlas": None}

    def _set(self, key, rec: Optional[dict]) -> None:
        self.prov[key] = rec
        self.prov_out[key] = rec

    def _conflict(self, key, path, cur, pub) -> None:
        store = key[0]
        self.conf_out[key] = (path, _summary(store, cur), _summary(store, pub),
                              None if pub is None else json.dumps(pub, sort_keys=True, default=str))
        self.rep["conflicts"].append(f"{store}:{key[1]}")

    def decide(self, store: str, item: str, path: str, P, cur, blocked: bool = False) -> bool:
        """Record what happens to one published row; True when the caller must write P."""
        key, name = (store, item), f"{store}:{item}"
        sha = self.now.get(path, ("", ""))[1]
        ph = _h(_core(store, P))
        ch = None if cur is None else _h(_core(store, cur))
        rec = lambda origin, seeded: dict(origin=origin, path=path, sha=sha, seeded=seeded, pub=ph)  # noqa: E731
        pv = self.prov.get(key)
        if pv is None:
            if cur is None:
                self._set(key, rec("knowledge", ph))
                self.rep["added"].append(name)
                return True
            if ch == ph:                       # seeded before provenance existed, or identical
                self._set(key, rec("knowledge", ph))
                return False
            self._set(key, rec("local", None))
            if store in CONFLICT_STORES:
                self._conflict(key, path, cur, P)
            self.rep["kept"].append(name)
            return False
        if ch == ph and not blocked:
            if pv["origin"] != "knowledge" or pv["pub"] != ph:
                self._set(key, rec("knowledge", ph))
                self.conf_out[key] = None
            return False
        mine = pv["origin"] == "knowledge" and not blocked and (
            (cur is None and store == "pair") or (cur is not None and ch == pv["seeded"]))
        if pv["pub"] == ph:                    # published unchanged
            if mine and cur is None:           # an atlas rebuild dropped it: back it comes
                self._set(key, rec("knowledge", ph))
                self.rep["added"].append(name)
                return True
            if pv["origin"] == "knowledge" and not mine:
                self._set(key, dict(pv, origin="local"))
            return False
        if mine:
            self._set(key, rec("knowledge", ph))
            self.conf_out[key] = None
            self.rep["updated"].append(name)
            return True
        self._set(key, dict(rec("local", pv["seeded"])))
        self._conflict(key, path, cur, P)
        self.rep["kept"].append(name)
        return False

    def gone(self, store: str, item: str, cur) -> bool:
        """The published row was removed upstream; True when the caller must delete it locally."""
        key, name = (store, item), f"{store}:{item}"
        pv = self.prov.get(key)
        if pv is None or (pv["origin"] == "local" and pv["pub"] is None):
            return False
        if pv["origin"] == "knowledge" and (cur is None or _h(_core(store, cur)) == pv["seeded"]):
            self._set(key, None)
            self.conf_out[key] = None
            self.rep["removed"].append(name)
            return cur is not None
        self._set(key, dict(pv, origin="local", pub=None))
        self._conflict(key, pv["path"], cur, None)
        self.rep["kept"].append(name)
        return False

    def stale(self, store: str, paths: Set[str], seen: Set[str]) -> List[str]:
        return sorted(k[1] for k, v in self.prov.items()
                      if v and k[0] == store and v["path"] in paths and k[1] not in seen)

    # ------------------------------------------------------------------ stores
    def macros(self, res, todo: Set[str]) -> None:
        from app.music_brain import db
        from app.music_brain.atlas import macros as mc
        paths = {p for p in todo if p.startswith(kn.MACROS + "/")}
        if not paths:
            return
        local = mc.stored(self.cache)
        writes, seen = [], set()
        for rel in sorted(p for p in paths if p in self.now):
            name = Path(rel).name[:-len(".json")]
            m = kn._read(self.src / rel)
            if not isinstance(m, dict):
                continue
            seen.add(name)
            missing = [res.tracked.get(t, t) for t in m.get("tracks") or [] if res(t) is None]
            if missing:
                self.rep["macros_skipped"].append({"macro": name, "missing": missing[:10]})
                continue
            try:
                m2 = mc.normalize(mc.resolve_ids(m, lambda t: res(t) or t))
            except (ValueError, KeyError) as exc:
                self.rep["macros_skipped"].append({"macro": name, "missing": [], "error": str(exc)[:120]})
                continue
            m2["title"], m2["knowledge"] = m.get("title") or m2["title"], True
            P, cur = dict(m2, name=name), local.get(name)
            if self.decide("macro", name, rel, P, cur):
                if isinstance(cur, dict) and cur.get("created"):
                    P["created"] = cur["created"]
                writes.append(P)
                self.rep["macros"].append(name)
        drop = [n for n in self.stale("macro", paths, seen) if self.gone("macro", n, local.get(n))]
        if self.dry or not (writes or drop):
            return
        conn = mc._mdb(self.cache)
        with db.tx(conn):
            for P in writes:
                mc._put(conn, P)
            for n in drop:
                conn.execute("DELETE FROM macros WHERE name = ?", (n,))

    def learned(self, todo: Set[str]) -> None:
        from app.music_brain.learning import set_learner as sl
        rel = kn.LEARNED
        if rel not in todo:
            return
        tracked = (kn._read(self.src / rel, {}) or {}) if rel in self.now else {}
        pub: Dict[str, List[dict]] = {}
        for e in tracked.values() if isinstance(tracked, dict) else []:
            if isinstance(e, dict):
                for o in _observations(sl, e.get("observations") or []):
                    pub.setdefault(str(o["set_id"]), []).append(o)
        path = self.cache / kn.LEARNED
        local = sl.load_learned(path)
        have = sl._stored_by_set(local)
        ruled = {k for k, e in local.items() if isinstance(e, dict) and (e.get("user_rules") or e.get("disabled"))}
        writes: Dict[str, List[dict]] = {}
        for sid in sorted(pub):
            P = _obs_list(pub[sid])
            cur = _obs_list(have[sid]) if have.get(sid) else None
            blocked = bool(ruled & {o["kind"] for o in P + (cur or [])})
            if self.decide("learned", sid, rel, P, cur, blocked):
                writes[sid] = P
        for sid in self.stale("learned", {rel}, set(pub)):
            cur = _obs_list(have[sid]) if have.get(sid) else None
            if self.gone("learned", sid, cur):
                writes[sid] = []
        self.rep["observations"] = sum(len(v) for v in writes.values())
        if writes and not self.dry:
            sl.merge([sl.Observation(**o) for v in writes.values() for o in v], path, set_ids=sorted(writes))

    def labels(self, todo: Set[str]) -> None:
        from app.music_brain.analysis import genre_labels as gl
        rel = kn.LABELS
        if rel not in todo:
            return
        names = kn._read(self.src / kn.NAMES, {}) or {}
        tracked = (kn._read(self.src / rel, {}) or {}) if rel in self.now else {}
        pub: Dict[str, dict] = {}
        for tid, lab in tracked.items() if isinstance(tracked, dict) else []:
            k = gl.name_key(names[tid]) if isinstance(lab, dict) and names.get(tid) else ""
            g, e = (gl._clean(lab.get("genre")), gl._clean(lab.get("era"))) if k else ("", "")
            if g or e:
                pub[k] = {"genre": g or None, "era": e or None}
        lp = gl.path(self.cache)
        genres, eras = gl.load(lp)

        def cur(k):
            return {"genre": genres.get(k) or None, "era": eras.get(k) or None} \
                if genres.get(k) or eras.get(k) else None

        def put(k, lab):
            for d, f in ((genres, "genre"), (eras, "era")):
                if lab and lab.get(f):
                    d[k] = lab[f]
                else:
                    d.pop(k, None)

        dirty = False
        for k in sorted(pub):
            if self.decide("label", k, rel, pub[k], cur(k)):
                put(k, pub[k])
                dirty = True
        for k in self.stale("label", {rel}, set(pub)):
            if self.gone("label", k, cur(k)):
                put(k, None)
                dirty = True
        if dirty and not self.dry:
            gl.save(genres, eras, lp)

    def atlas(self, res, todo: Set[str]) -> None:
        from app.music_brain.atlas import pair_atlas as pa
        from app.music_brain.learning.set_import import _atlas_lock
        paths = {p for p in todo if p == kn.ATLAS or p.startswith(kn.ATLAS_DIR + "/")}
        if not paths:
            return
        if not any(p in self.now for p in paths):          # every atlas file deleted upstream
            ka = {"rules": pa.rules_hash(), "pairs": {}}
        else:
            ka = kn.load_atlas(self.src)
            if not ka:
                self.rep["atlas"] = "no tracked atlas"
                return
        if ka.get("rules") != pa.rules_hash():
            self.rep["atlas"] = "tracked atlas is stale (rules changed): rebuild and export"
            self.hold |= paths
            return
        segmented = isinstance(kn._read(self.src / kn.ATLAS_DIR / kn.ATLAS_META), dict)
        full = kn.ATLAS in paths or f"{kn.ATLAS_DIR}/{kn.ATLAS_META}" in paths
        with _atlas_lock(self.cache):
            path = pa.atlas_path(self.cache)
            local = pa.load(self.cache)
            if local is None and (pa._has_atlas(pa._db(path)) or (path / pa.META).exists()
                                  or pa._legacy(path).exists()):
                self.rep["atlas"] = "local atlas unreadable: left alone"
                self.hold |= paths
                return
            if local is not None and local.get("rules") != ka["rules"]:
                self.rep["atlas"] = "local atlas has other rules: left alone"
                self.hold |= paths
                return
            doc = local or {"schema": pa.SCHEMA, "rules": ka["rules"], "built_at": ka.get("built_at"),
                            "cache_dir": "", "tracks": {}, "pairs": {}, "stats": {}}
            n, seen, dirty = 0, set(), False
            for p in ka.get("pairs", {}).values():
                rel = f"{kn.ATLAS_DIR}/pairs/{p['a']}.json.gz" if segmented else kn.ATLAS
                if not (full or rel in paths):
                    continue
                a, b = res(p["a"]), res(p["b"])
                if not a or not b or a == b:
                    continue
                key = f"{a}>{b}"
                seen.add(key)
                P = dict(p, a=a, b=b, knowledge=True)
                pl = P.pop("played", None)
                if pl:
                    P["played_published"] = pl
                cur = doc["pairs"].get(key)
                if not self.decide("pair", key, rel, P, cur):
                    continue
                if isinstance(cur, dict) and cur.get("played") is not None:
                    P["played"] = cur["played"]
                doc["pairs"][key] = P
                for tid, kid in ((a, p["a"]), (b, p["b"])):
                    if tid not in doc["tracks"] and kid in (ka.get("tracks") or {}):
                        doc["tracks"][tid] = dict(ka["tracks"][kid], knowledge=True)
                n, dirty = n + 1, True
            scope = {v["path"] for k, v in self.prov.items() if v and k[0] == "pair"} if full else paths
            for key in self.stale("pair", scope, seen):
                if self.gone("pair", key, doc["pairs"].get(key)):
                    doc["pairs"].pop(key, None)
                    dirty = True
            if n:
                doc.setdefault("stats", {})["knowledge_pairs"] = n
            if dirty and not self.dry:
                pa.write_atlas(doc, path)
        self.rep["pairs"] = self.rep["atlas"] = n

    def flush(self, old: Dict[str, Tuple[str, str]], lib: str) -> None:
        if self.dry:
            return
        from app.music_brain import db
        with db.tx(self.conn):
            for (store, item), r in self.prov_out.items():
                if r is None:
                    self.conn.execute("DELETE FROM knowledge_rows WHERE store = ? AND item = ?", (store, item))
                else:
                    self.conn.execute("INSERT OR REPLACE INTO knowledge_rows VALUES (?, ?, ?, ?, ?, ?, ?)",
                                      (store, item, r["origin"], r["path"], r["sha"], r["seeded"], r["pub"]))
            for (store, item), c in self.conf_out.items():
                if c is None:
                    self.conn.execute("DELETE FROM knowledge_conflicts WHERE store = ? AND item = ?", (store, item))
                else:
                    self.conn.execute("INSERT OR REPLACE INTO knowledge_conflicts VALUES (?, ?, ?, ?, ?, ?, ?)",
                                      (store, item, *c, time.time()))
            for p in set(old) - set(self.now) - self.hold - {LIBRARY}:
                self.conn.execute("DELETE FROM knowledge_files WHERE path = ?", (p,))
            for p, (kind, st) in self.now.items():
                if p not in self.hold:
                    self.conn.execute("INSERT OR REPLACE INTO knowledge_files VALUES (?, ?, ?)", (p, kind, st))
            self.conn.execute("INSERT OR REPLACE INTO knowledge_files VALUES (?, 'hash', ?)", (LIBRARY, lib))


def sync(cache_dir: Optional[Path] = None, src: Optional[Path] = None, dry_run: bool = False,
         log: Callable[[str], None] = lambda m: None) -> dict:
    """knowledge/ -> cache by version stamp: files whose stamp changed since the last sync are
    applied (unchanged-from-seed rows updated, new rows added, rows removed upstream dropped when
    still unchanged, locally changed rows kept as conflicts); unchanged files are skipped."""
    cache, src = kn._cache(cache_dir), Path(src or kn.KNOWLEDGE_DIR)
    s = _Sync(cache, src, dry_run)
    rep = s.rep
    if not src.is_dir():
        rep["atlas"] = "no knowledge folder"
        return rep
    t0 = time.perf_counter()
    s.now = stamps(src)
    rep["stamps"] = {"git": sum(1 for k, _ in s.now.values() if k == "git"),
                     "hash": sum(1 for k, _ in s.now.values() if k == "hash"),
                     "ms": round((time.perf_counter() - t0) * 1000, 1)}
    old = {p: (k, st) for p, k, st in s.conn.execute("SELECT path, kind, stamp FROM knowledge_files")}
    lib = _library_stamp(cache)
    full = old.get(LIBRARY, ("", ""))[1] != lib or old.get(kn.NAMES) != s.now.get(kn.NAMES)
    todo = {p for p, st in s.now.items() if full or old.get(p) != st} | {p for p in old if p != LIBRARY and p not in s.now}
    rep["files_changed"] = sorted(todo)
    if todo:
        res = kn._Resolver(cache, kn._read(src / kn.NAMES, {}) or {})
        s.macros(res, todo)
        s.learned(todo)
        s.labels(todo)
        s.atlas(res, todo)
    if todo or old.get(LIBRARY, ("", ""))[1] != lib:
        s.flush(old, lib)
    log(f"knowledge sync: {len(todo)} files changed, {len(rep['added'])} added, {len(rep['updated'])} "
        f"updated, {len(rep['removed'])} removed, {len(rep['kept'])} kept local, "
        f"{len(rep['conflicts'])} conflicts")
    return rep


# ------------------------------------------------------------------------ conflicts
def conflicts(cache_dir: Optional[Path] = None) -> List[dict]:
    conn = _conn(kn._cache(cache_dir))
    return [dict(item=f"{r[0]}:{r[1]}", path=r[2], local=r[3], published=r[4], at=r[5])
            for r in conn.execute("SELECT store, item, path, local, published, at FROM knowledge_conflicts "
                                  "ORDER BY store, item")]


def take(item: str, published: bool, cache_dir: Optional[Path] = None) -> dict:
    """Resolve one conflict: --local keeps the owner's row (origin local); --published writes
    the published row (or deletes the row when it was removed upstream), origin knowledge."""
    from app.music_brain import db
    cache = kn._cache(cache_dir)
    conn = _conn(cache)
    rows = conn.execute("SELECT store, item, path, data FROM knowledge_conflicts").fetchall()
    hit = [r for r in rows if f"{r[0]}:{r[1]}" == item] or [r for r in rows if r[1] == item]
    if len(hit) != 1:
        raise ValueError(f"{'ambiguous' if hit else 'no'} conflict {item!r}: use store:item from `conflicts`")
    store, key, path, data = hit[0]
    P = None if data is None else json.loads(data)
    pv = conn.execute("SELECT source_sha FROM knowledge_rows WHERE store = ? AND item = ?", (store, key)).fetchone()
    if published:
        _apply(cache, store, key, P)
    with db.tx(conn):
        conn.execute("DELETE FROM knowledge_conflicts WHERE store = ? AND item = ?", (store, key))
        if published and P is None:
            conn.execute("DELETE FROM knowledge_rows WHERE store = ? AND item = ?", (store, key))
        else:
            ph = None if P is None else _h(_core(store, P))
            conn.execute("INSERT OR REPLACE INTO knowledge_rows VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (store, key, "knowledge" if published else "local", path,
                          pv[0] if pv else None, ph if published else None, ph))
    return {"item": f"{store}:{key}", "took": "published" if published else "local"}


def _apply(cache: Path, store: str, key: str, P) -> None:
    """Write one published row over the local one (None: delete it)."""
    from app.music_brain import db
    if store == "macro":
        from app.music_brain.atlas import macros as mc
        conn = mc._mdb(cache)
        with db.tx(conn):
            if P is None:
                conn.execute("DELETE FROM macros WHERE name = ?", (key,))
                return
            old = mc._get(conn, key)
            if isinstance(old, dict) and old.get("created"):
                P["created"] = old["created"]
            mc._put(conn, P)
    elif store == "learned":
        from app.music_brain.learning import set_learner as sl
        sl.merge([sl.Observation(**o) for o in P or []], cache / kn.LEARNED, set_ids=[key])
    elif store == "label":
        from app.music_brain.analysis import genre_labels as gl
        lp = gl.path(cache)
        genres, eras = gl.load(lp)
        for d, f in ((genres, "genre"), (eras, "era")):
            if P and P.get(f):
                d[key] = P[f]
            else:
                d.pop(key, None)
        gl.save(genres, eras, lp)
    elif store == "pair":
        from app.music_brain.atlas import pair_atlas as pa
        from app.music_brain.learning.set_import import _atlas_lock
        with _atlas_lock(cache):
            doc = pa.load(cache)
            if doc is None:
                raise ValueError("no local atlas")
            cur = doc["pairs"].get(key)
            if P is None:
                doc["pairs"].pop(key, None)
            else:
                if isinstance(cur, dict) and cur.get("played") is not None:
                    P["played"] = cur["played"]
                doc["pairs"][key] = P
            pa.write_atlas(doc, pa.atlas_path(cache))
