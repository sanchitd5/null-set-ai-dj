"""Grounded pick pool: the model CHOOSES a candidate id instead of free-texting a song.

Evidence (PICKID, prod): 48 % of suggest_reject events were "not a real song (lookup failed)" - the LLM
invents plausible titles. Here the prompt lists real candidates and a pick must carry one's id (or copy
one's "Artist - Title" exactly); anything else is rejected BEFORE any lookup.

Candidates, best first:
  1. atlas neighbours of the playing song (library songs the pair atlas already scored against it),
  2. verified catalogue songs: YouTube searches (artist's songs, the scene's songs) whose every hit
     passes pick_identity at HIGH, so the lookup is already done. Cached per seed.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable, Iterable

MIN_POOL = 4            # fewer candidates than this: grounding is not enforced (free text still verified)
MAX_POOL = 16
CATALOGUE_TTL_S = 6 * 3600
CATALOGUE_CACHE_MAX = 200

_lock = threading.Lock()
_catalogue: dict[str, tuple[float, list[dict]]] = {}      # seed key -> (at, [{artist, title}])


def key_of(artist: str, title: str) -> str:
    from app.ui.services import pick_identity as pi
    return pi.norm(artist) + "|" + _bare(title)


def _bare(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", re.sub(r"[(\[][^)\]]*[)\]]", " ", str(title).lower())).split())


def catalogue(seed: str, queries: Iterable[str], search: Callable[[str, int], list]) -> list[dict]:
    """Verified catalogue songs for one seed, cached. Every entry passed pick_identity at HIGH."""
    with _lock:
        hit = _catalogue.get(seed)
        if hit and time.time() - hit[0] < CATALOGUE_TTL_S:
            return list(hit[1])
    from app.ui.services import pick_identity as pi
    from app.ui.services.track_identity import clean_identity
    out, seen, failed = [], set(), 0
    for q in queries:
        try:
            rows = search(q, 10) or []
        except Exception:                       # cooling / offline: no catalogue, never an error
            failed += 1
            continue
        for e in rows:
            artist, title = clean_identity(str(e.get("title") or ""))
            if not artist or artist == "Unknown" or not title:
                continue
            v = pi.judge(artist, title, e.get("title") or "", channel=e.get("channel") or "",
                         duration=e.get("duration"))
            k = key_of(artist, title)
            if v.ok and v.tier == "high" and k not in seen:
                seen.add(k)
                row = {"artist": artist, "title": title, "source": "catalogue"}
                g = str(e.get("genre") or "").strip()      # the platform's own label, when the search returns one
                if g:
                    row["genre"] = g
                out.append(row)
    if not failed:                               # a failed search is retried next call, not cached empty
        with _lock:
            _catalogue.pop(seed, None)
            _catalogue[seed] = (time.time(), out)
            while len(_catalogue) > CATALOGUE_CACHE_MAX:
                _catalogue.pop(next(iter(_catalogue)))
    return out


def atlas_neighbours(track_id: str, names: dict, cache_dir) -> list[dict]:
    """Library songs the pair atlas scored against `track_id`, best pair first."""
    try:
        from app.music_brain.atlas import atlas_store
        pairs = atlas_store.pairs_for(track_id, cache_dir)
    except Exception:
        return []
    scored = []
    for k, p in pairs.items():
        b = k.split(">", 1)[-1]
        name = names.get(b)
        if not name or " - " not in name:
            continue
        sc = p.get("score") if isinstance(p, dict) else None
        if sc is None and isinstance(p, dict):
            sc = (p.get("merge") or {}).get("score")
        scored.append((float(sc) if isinstance(sc, (int, float)) else 0.0, b, name))
    scored.sort(reverse=True)
    from app.ui.services.track_identity import clean_identity
    out = []
    for _, b, name in scored:
        artist, title = clean_identity(name)
        if artist and artist != "Unknown" and title:
            out.append({"artist": artist, "title": title, "source": "library", "track_id": b})
    return out


def _by_language(rows: list[dict], lang_check) -> list[dict]:
    """Rows in a language the set is not in: dropped (one-language set) or moved to the back (mixed set).
    Unknown / unanalysed rows stay (the script gate and the post-download check decide)."""
    keep, back = [], []
    for r in rows:
        try:
            verdict, _ = lang_check(r)
        except Exception:               # a gate bug must not empty the pool
            verdict = "ok"
        if verdict == "block":
            continue
        (back if verdict == "penalty" else keep).append(r)
    return keep + back


def build_pool(*, atlas: list[dict], cat: list[dict], exclude: Iterable[str], seed_title: str, lang_check=None,
               limit: int = MAX_POOL) -> list[dict]:
    """Merge, drop the playing / played / rejected songs, number them C1..Cn. Library entries first
    (about half the slots), the rest catalogue, so the model sees both. lang_check(row) -> (verdict, reason):
    a song in a language the set is not in is dropped ("block") or sent to the back ("penalty")."""
    skip = {_bare(str(x).split(" - ", 1)[-1]) for x in exclude}
    skip.add(_bare(seed_title))
    if lang_check is not None:
        atlas, cat = _by_language(atlas, lang_check), _by_language(cat, lang_check)
    seen, pool = set(), []

    def take(rows: list[dict], cap: int) -> None:
        n = 0
        for r in rows:
            b, k = _bare(r["title"]), key_of(r["artist"], r["title"])
            if b in skip or k in seen or n >= cap:
                continue
            seen.add(k)
            pool.append(dict(r))
            n += 1

    take(atlas, limit // 2)
    take(cat, limit - len(pool))
    take(atlas, limit - len(pool))
    for i, r in enumerate(pool, 1):
        r["cid"] = f"C{i}"
    return pool


def prompt_block(pool: list[dict]) -> str:
    lines = "\n".join(f'{c["cid"]} | {c["artist"]} - {c["title"]}' for c in pool)
    return ("\n\nCANDIDATES (real, released songs; pick ONLY from this list):\n" + lines +
            '\nEvery suggestion MUST carry "cid" (one id above) and copy that candidate\'s artist and title '
            "exactly. A song that is not in the list is rejected unheard. Keep all the rules above "
            "(tempo, key, energy, scene) when choosing.\n")


_CID_RE = re.compile(r"^\s*(?:[\[(]\s*(C\d{1,3})\s*[\])]|(C\d{1,3})\s*[-:|\u2013\u2014.]|(C\d{1,3})\s*$)\s*", re.I)


def _strip_cid(text: str) -> tuple[str, str]:
    """(embedded cid or "", text without it) for "C12 - Name", "C12: Name", "[C12] Name", "C12 | Name"."""
    m = _CID_RE.match(text or "")
    if not m:
        return "", text or ""
    return (m.group(1) or m.group(2) or m.group(3)).lower(), (text or "")[m.end():].strip()


def _embedded_cid(p: dict) -> str:
    """Cid hidden in artist or title (the model writes "C12 - Artist" into the artist field); strips it in place."""
    found = ""
    for f in ("artist", "title"):
        cid, rest = _strip_cid(str(p.get(f) or ""))
        if cid:
            found = found or cid
            p[f] = rest
    return found


def ground(picks: list[dict], pool: list[dict]) -> tuple[list[dict], list[dict]]:
    """(grounded, free_text). A pick is grounded by its cid, else by an exact artist+title match."""
    by_id = {c["cid"].lower(): c for c in pool}
    by_key = {key_of(c["artist"], c["title"]): c for c in pool}
    ok, free, used = [], [], set()
    for p in picks:
        emb = _embedded_cid(p)
        c = by_id.get(str(p.get("cid") or "").strip().lower()) or by_id.get(emb) or by_key.get(
            key_of(p.get("artist", ""), p.get("title", "")))
        if not c or c["cid"] in used:
            free.append(p)
            continue
        used.add(c["cid"])
        p["artist"], p["title"] = c["artist"], c["title"]
        if c.get("track_id"):
            p["track_id"] = c["track_id"]
        p["pool_source"] = c["source"]
        if c.get("genre"):                      # a label from data (catalogue / platform), never the model's own
            p["data_genre"] = c["genre"]
        p.pop("cid", None)
        ok.append(p)
    return ok, free
