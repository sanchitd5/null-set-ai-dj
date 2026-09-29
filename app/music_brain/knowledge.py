"""KNOWLEDGE: the macros, the learner's observations and a slim pair atlas, tracked in git
under app/music_brain/knowledge/ (next to seed_combos.json), so a fresh checkout's player starts
with this library's strength. Owner: "macros and atlas knowledge give the AI player its actual
strength". data/ stays gitignored; this folder holds JSON only: no audio, stems, set cuts,
session logs, absolute paths or e-mail addresses (export refuses to write any).

    python3 -m app.music_brain.knowledge export [--cache-dir D] [--out K]   cache -> knowledge/
    python3 -m app.music_brain.knowledge import [--cache-dir D] [--src K]   knowledge/ -> cache

Files: macros/<name>.json, learned_techniques.json, pair_atlas.json.gz (slim: slim_atlas),
names.json (track id -> "Artist - Title"). Output is deterministic (sorted keys, stable order,
gzip without a timestamp), and a file is rewritten only when its bytes change, so git diffs
stay small.

Track ids are sha256(bytes)[:16]: another download of the same song has another id, so import
resolves every id by NAME (dedup_songs.identity) onto the local library. The local cache always
wins: a local macro, atlas pair or studied set is never overwritten; unresolved songs are
skipped and reported. auto_seed runs import when knowledge/ or the local library changed
(stamp in CACHE_DIR/knowledge_seed.json); atlas_api calls it where macros and the atlas load.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
from dataclasses import fields
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
KNOWLEDGE_DIR = HERE / "knowledge"          # tests point this at tmp (conftest)
MACROS = "macros"
LEARNED = "learned_techniques.json"
ATLAS = "pair_atlas.json.gz"
NAMES = "names.json"
STAMP = "knowledge_seed.json"
KEEP, PER_MOVE = 60, 20                     # slim atlas: the server Index's own selection per A
TRACK_FIELDS = ("name", "artist", "bpm", "key", "duration", "level", "stems")
_ABS = re.compile(r"(?:^|[\s\"'(=:,\[])(?:/(?:Users|home|private|tmp|var|opt|Volumes|mnt|root)/|[A-Za-z]:[\\/])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")
_SEEN: Dict[str, str] = {}                  # cache dir -> last seeded stamp (this process)


def _cache(cache_dir: Optional[Path]) -> Path:
    from app.music_brain.config import CACHE_DIR

    return Path(cache_dir or CACHE_DIR)


def _read(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _dump(obj, compact: bool = False) -> bytes:
    if compact:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return (json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def _write_bytes(p: Path, data: bytes, overwrite: bool = True) -> bool:
    """Atomic write; False when the file already holds these bytes (or exists and overwrite=False)."""
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and (not overwrite or p.read_bytes() == data):
        return False
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    if overwrite:
        tmp.replace(p)
        return True
    try:
        os.link(tmp, p)                      # fails if a local file appeared meanwhile: never overwritten
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


def privacy_hits(obj, where: str = "") -> List[str]:
    """Every string in obj that holds an absolute path, the home dir or an e-mail address."""
    home = str(Path.home())
    out: List[str] = []

    def walk(x, at):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(k, at)
                walk(v, f"{at}.{k}")
        elif isinstance(x, (list, tuple)):
            for i, v in enumerate(x):
                walk(v, f"{at}[{i}]")
        elif isinstance(x, str) and (home in x or _ABS.search(x) or _EMAIL.search(x)):
            out.append(f"{at}: {x[:80]}")
    walk(obj, where)
    return out


# ------------------------------------------------------------------------------------ slim atlas
def slim_atlas(atlas: dict, keep: int = KEEP, per_move: int = PER_MOVE) -> dict:
    """The pairs worth publishing: every pair with evidence (combo / studied / seed / played)
    plus, per A, the top `keep` by works and the top `per_move` per judged move (what the
    server's Index serves). Tracks keep what summary / chains / order_picks read.
    Dropped: file-stat sigs, per-bar features, vocal regions, session ids, the cache path."""
    from app.music_brain import pair_atlas as pa

    by_a: Dict[str, List[dict]] = {}
    for p in atlas["pairs"].values():
        by_a.setdefault(p["a"], []).append(p)
    pick: Dict[str, dict] = {}
    for a, ps in by_a.items():
        ps.sort(key=lambda p: (-p["works"], p["b"]))
        chosen = {p["b"]: p for p in ps[:keep]}
        for p in ps:
            if p.get("combo") or p.get("played") or p.get("seed") or p.get("studied"):
                chosen[p["b"]] = p
        for m in pa.MOVES:
            if m in pa.UNJUDGED:
                continue
            ok = [p for p in ps if pa.move_of(p, m).get("ok")]
            ok.sort(key=lambda p: (-(pa.move_of(p, m).get("score") or 0), -p["works"], p["b"]))
            for p in ok[:per_move]:
                chosen[p["b"]] = p
        for p in chosen.values():
            q = {k: v for k, v in p.items() if k not in ("sig", "knowledge")}
            if isinstance(q.get("played"), dict):
                q["played"] = {k: v for k, v in q["played"].items() if k != "sessions"}
            pick[f"{p['a']}>{p['b']}"] = q
    tr = atlas.get("tracks") or {}
    used = {x for p in pick.values() for x in (p["a"], p["b"])}
    tracks = {t: {f: tr[t].get(f) for f in TRACK_FIELDS if f in tr[t]} for t in sorted(used) if t in tr}
    return {"schema": atlas.get("schema"), "rules": atlas.get("rules"), "built_at": atlas.get("built_at"),
            "slim": {"keep": keep, "per_move": per_move}, "tracks": tracks,
            "pairs": dict(sorted(pick.items())),
            "stats": {"tracks": len(tracks), "pairs": len(pick), "source_tracks": len(tr),
                      "source_pairs": len(atlas["pairs"])}}


def load_atlas(src: Optional[Path] = None) -> Optional[dict]:
    try:
        return json.loads(gzip.decompress((Path(src or KNOWLEDGE_DIR) / ATLAS).read_bytes()).decode("utf-8"))
    except (OSError, ValueError, EOFError):
        return None


# ---------------------------------------------------------------------------------------- export
def export(cache_dir: Optional[Path] = None, out: Optional[Path] = None,
           log: Callable[[str], None] = lambda m: None) -> dict:
    """cache -> knowledge/. Every cache macro is written (a tracked macro the cache lacks is
    kept: it may be another machine's, unresolved here); the learned store and the slim atlas
    mirror the cache when it has them. ValueError (nothing written) on a privacy hit."""
    from app.music_brain import macros as mc
    from app.music_brain import studied_combos as sc
    from app.ui import dedup_songs as ds

    cache, out = _cache(cache_dir), Path(out or KNOWLEDGE_DIR)
    names_local, aliases = sc.library_names(cache), ds.load_aliases(cache)
    files: Dict[str, object] = {}
    ids: set = set()
    for p in sorted(mc.macros_dir(cache).glob("*.json")):
        raw = _read(p)
        if not isinstance(raw, dict) or raw.get("schema") != mc.SCHEMA:
            continue
        try:
            m = mc.normalize(raw)
        except ValueError:
            continue
        files[f"{MACROS}/{m['name']}.json"] = m
        ids.update(m["tracks"])
    n_macros = len(files)
    learned = _read(cache / LEARNED)
    n_obs = 0
    if isinstance(learned, dict):
        files[LEARNED] = learned
        n_obs = sum(len(e.get("observations") or []) for e in learned.values() if isinstance(e, dict))
    from app.music_brain import pair_atlas as pa

    atlas = pa.load(cache)
    slim = slim_atlas(atlas) if atlas else None
    if slim:
        ids.update(slim["tracks"])
    names = dict(_read(out / NAMES, {}) or {})
    atlas_names = {t: f.get("name") for t, f in ((atlas or {}).get("tracks") or {}).items()}
    for t in ids:
        n = names_local.get(t) or names_local.get(ds.resolve_alias(t, cache, aliases)) or atlas_names.get(t)
        if n:
            names[t] = n
    files[NAMES] = dict(sorted(names.items()))
    hits = [h for k, v in files.items() for h in privacy_hits(v, k)] + (privacy_hits(slim, ATLAS) if slim else [])
    if hits:
        raise ValueError(f"knowledge export refused, {len(hits)} private strings: " + "; ".join(hits[:5]))
    changed = [k for k, v in files.items() if _write_bytes(out / k, _dump(v))]
    rep = {"out": out.name, "macros": n_macros, "observations": n_obs, "names": len(names), "changed": changed}
    if slim:
        data = gzip.compress(_dump(slim, compact=True), compresslevel=9, mtime=0)
        if _write_bytes(out / ATLAS, data):
            changed.append(ATLAS)
        rep["atlas"] = {"tracks": slim["stats"]["tracks"], "pairs": slim["stats"]["pairs"],
                        "source_pairs": slim["stats"]["source_pairs"], "bytes_gz": len(data)}
    log(f"knowledge: {n_macros} macros, {n_obs} observations, {len(changed)} files changed")
    return rep


def export_safe(cache_dir: Optional[Path] = None, out: Optional[Path] = None,
                log: Callable[[str], None] = lambda m: None) -> dict:
    """export that never raises (learn-set's last step): {"error": str} instead."""
    try:
        return export(cache_dir, out, log)
    except Exception as exc:  # noqa: BLE001 -- the learn result stands whatever happens here
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


# ------------------------------------------------------------------------------------ import/seed
class _Resolver:
    """Tracked id -> local library id: the same id (or its alias) when the library has it,
    else the unique library song of the same name (dedup_songs.identity, strong match)."""

    def __init__(self, cache: Path, tracked_names: Dict[str, str]):
        from app.music_brain import studied_combos as sc
        from app.ui import dedup_songs as ds

        self.ds, self.cache, self.tracked = ds, cache, tracked_names
        self.aliases = ds.load_aliases(cache)
        up = cache / "uploads"
        have = {p.stem for p in up.glob("*") if not p.name.startswith("_")} if up.is_dir() else set()
        self.local = {k: v for k, v in sc.library_names(cache).items() if k in have}
        self.by_name: Dict[str, List[str]] = {}
        self.idents = []
        for tid, n in self.local.items():
            self.by_name.setdefault(ds.fold(n), []).append(tid)
            self.idents.append((ds.identity(n), tid))
        self.memo: Dict[str, Optional[str]] = {}

    def __call__(self, tid: str) -> Optional[str]:
        if tid not in self.memo:
            self.memo[tid] = self._find(tid)
        return self.memo[tid]

    def _find(self, tid: str) -> Optional[str]:
        ds = self.ds
        if tid in self.local or tid in self.aliases:
            return ds.resolve_alias(tid, self.cache, self.aliases)
        name = self.tracked.get(tid)
        if not name:
            return None
        hits = set(self.by_name.get(ds.fold(name), []))
        if not hits:
            me = ds.identity(name)
            hits = {t for i, t in self.idents if ds.name_match(me, i) == "strong"}
        hits = {ds.resolve_alias(t, self.cache, self.aliases) for t in hits}
        return hits.pop() if len(hits) == 1 else None


def seed(cache_dir: Optional[Path] = None, src: Optional[Path] = None,
         log: Callable[[str], None] = lambda m: None) -> dict:
    """knowledge/ -> cache, never overwriting local data:
    macros the cache lacks (every song name-resolved, else skipped), learner observations of
    sets the local store has none of, atlas pairs the local atlas lacks (only when both carry
    the current rules hash)."""
    from app.music_brain import macros as mc
    from app.music_brain import pair_atlas as pa
    from app.music_brain import set_learner as sl
    from app.music_brain.set_import import _atlas_lock

    cache, src = _cache(cache_dir), Path(src or KNOWLEDGE_DIR)
    rep = {"macros": [], "macros_skipped": [], "observations": 0, "pairs": 0, "atlas": None}
    if not src.is_dir():
        rep["atlas"] = "no knowledge folder"
        return rep
    res = _Resolver(cache, _read(src / NAMES, {}) or {})
    mdir = mc.macros_dir(cache)
    for p in sorted((src / MACROS).glob("*.json")):
        m = _read(p)
        if not isinstance(m, dict) or (mdir / p.name).exists():
            continue                                        # the local macro wins
        missing = [res.tracked.get(t, t) for t in m.get("tracks") or [] if res(t) is None]
        if missing:
            rep["macros_skipped"].append({"macro": p.stem, "missing": missing[:10]})
            continue
        try:
            m2 = mc.normalize(mc.resolve_ids(m, lambda t: res(t) or t))
        except (ValueError, KeyError) as exc:
            rep["macros_skipped"].append({"macro": p.stem, "missing": [], "error": str(exc)[:120]})
            continue
        m2["title"], m2["knowledge"] = m.get("title") or m2["title"], True
        if _write_bytes(mdir / p.name, (json.dumps(m2, indent=1) + "\n").encode("utf-8"), overwrite=False):
            rep["macros"].append(p.stem)
    # learner observations: a set the local store knows is the local store's
    tracked = _read(src / LEARNED, {}) or {}
    local_path = cache / LEARNED
    local = sl.load_learned(local_path)
    local_sets = {o.get("set_id") for e in local.values() for o in e.get("observations") or []}
    names = {f.name for f in fields(sl.Observation)}
    new = [sl.Observation(**{k: v for k, v in o.items() if k in names})
           for e in tracked.values() if isinstance(e, dict) for o in e.get("observations") or []
           if isinstance(o, dict) and o.get("set_id") not in local_sets and o.get("kind") in sl.KINDS]
    if new:
        sl.merge(new, path=local_path)
        rep["observations"] = len(new)
    # atlas pairs
    if not any(res(t) for t in res.tracked):
        rep["atlas"] = "no tracked song in this library"
    else:
        rep["atlas"] = _seed_atlas(cache, src, res, pa, _atlas_lock)
        rep["pairs"] = rep["atlas"] if isinstance(rep["atlas"], int) else 0
    log(f"knowledge seed: {len(rep['macros'])} macros, {rep['observations']} observations, {rep['pairs']} pairs")
    return rep


def _seed_atlas(cache: Path, src: Path, res: _Resolver, pa, lock) -> object:
    ka = load_atlas(src)
    if not ka:
        return "no tracked atlas"
    if ka.get("rules") != pa.rules_hash():
        return "tracked atlas is stale (rules changed): rebuild and export"
    with lock(cache):
        path = pa.atlas_path(cache)
        local = pa.load(cache)
        if local is None and path.exists():
            return "local atlas unreadable: left alone"
        if local is not None and local.get("rules") != ka["rules"]:
            return "local atlas has other rules: left alone"
        doc = local or {"schema": pa.SCHEMA, "rules": ka["rules"], "built_at": ka.get("built_at"), "cache_dir": "",
                        "tracks": {}, "pairs": {}, "stats": {}}
        added = 0
        for p in ka["pairs"].values():
            a, b = res(p["a"]), res(p["b"])
            if not a or not b or a == b or f"{a}>{b}" in doc["pairs"]:
                continue
            doc["pairs"][f"{a}>{b}"] = dict(p, a=a, b=b, knowledge=True)
            for tid, kid in ((a, p["a"]), (b, p["b"])):
                if tid not in doc["tracks"] and kid in ka["tracks"]:
                    doc["tracks"][tid] = dict(ka["tracks"][kid], knowledge=True)
            added += 1
        if added:
            doc.setdefault("stats", {})["knowledge_pairs"] = added
            _write_bytes(path, json.dumps(doc, separators=(",", ":")).encode("utf-8"))
        return added


def _stamp(cache: Path, src: Path) -> str:
    h = hashlib.sha256()
    files = sorted(q for q in src.rglob("*") if q.is_file()) if src.is_dir() else []
    for q in files + [cache / "uploads" / "_names.json", cache / "track_aliases.json"]:
        try:
            st = q.stat()
            h.update(f"{q.name}:{st.st_size}:{st.st_mtime_ns};".encode())
        except OSError:
            h.update(b"-;")
    return h.hexdigest()[:16]


def auto_seed(cache_dir: Optional[Path] = None, src: Optional[Path] = None) -> Optional[dict]:
    """seed() when knowledge/ or the local library changed since the last seed, else None
    (a few stats). Never raises: an error is reported and not retried until something changes."""
    cache, src = _cache(cache_dir), Path(src or KNOWLEDGE_DIR)
    try:
        stamp = _stamp(cache, src)
        if _SEEN.get(str(cache)) == stamp:
            return None
        _SEEN[str(cache)] = stamp
        if (_read(cache / STAMP, {}) or {}).get("stamp") == stamp or not src.is_dir() or not cache.is_dir():
            return None
        try:
            rep = seed(cache, src)
        except Exception as exc:  # noqa: BLE001 -- a bad tracked file must not break the API
            rep = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        _write_bytes(cache / STAMP, _dump({"stamp": stamp, "report": rep}))
        return rep
    except OSError:
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="knowledge", description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=("export", "import"))
    ap.add_argument("--cache-dir", default=None, help="cache to read (export) or seed (import); default CACHE_DIR")
    ap.add_argument("--out", default=None, help="export: knowledge folder (default app/music_brain/knowledge)")
    ap.add_argument("--src", default=None, help="import: knowledge folder (default app/music_brain/knowledge)")
    a = ap.parse_args(argv)
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    try:
        if a.cmd == "export":
            out = export(Path(a.cache_dir) if a.cache_dir else None, Path(a.out) if a.out else None, log)
        else:
            out = seed(Path(a.cache_dir) if a.cache_dir else None, Path(a.src) if a.src else None, log)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
