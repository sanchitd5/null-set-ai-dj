"""Genre / era labels the model gave songs, kept across server restarts.

The suggestion filters and the library fallback (/api/library/lockable) read these labels.
They used to live only in server memory, so after a restart a Punjabi set's library
fallback found no labelled song and the set sat in HOLD LOOP (session 2026-09-30_102327).

    CACHE_DIR/genre_labels.json
    {"version": 1, "labels": {"<title key>": {"genre": "punjabi pop", "era": "2020s"}}}

Keyed by the normalised clean title (server._genre_key), the same key the readers use:
the model labels songs by title, often before the song is downloaded and has a track id.
The knowledge export carries them per track id (knowledge/genre_labels.json), resolved
through each id's name, so another machine's library gets them by name.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

FILE = "genre_labels.json"
VERSION = 1
MAX_LABELS = 5000              # the model labels ~5 songs a call; a year of sets stays far below
MAX_FIELD = 80


def path(cache_dir: Optional[Path] = None) -> Path:
    from app.music_brain.config import CACHE_DIR

    return Path(cache_dir or CACHE_DIR) / FILE


def title_key(title: str) -> str:
    from app.ui.services.track_identity import clean_title

    return " ".join(clean_title(str(title or "")).lower().split())


def name_key(name: str) -> str:
    """ "Artist - Title (Official Video)" -> the title key."""
    from app.ui.services.track_identity import clean_identity

    return title_key(clean_identity(str(name or ""))[1])


def _clean(v) -> str:
    return str(v or "").strip()[:MAX_FIELD]


def load(p: Optional[Path] = None) -> Tuple[Dict[str, str], Dict[str, str]]:
    """(genres, eras) by title key; empty when the file is missing or unreadable."""
    try:
        d = json.loads(Path(p or path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    labels = d.get("labels") if isinstance(d, dict) else None
    genres, eras = {}, {}
    for k, v in (labels or {}).items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        if _clean(v.get("genre")):
            genres[k] = _clean(v.get("genre"))
        if _clean(v.get("era")):
            eras[k] = _clean(v.get("era"))
    return genres, eras


def save(genres: Dict[str, str], eras: Dict[str, str], p: Optional[Path] = None) -> bool:
    """Atomic write (tmp + replace). The newest MAX_LABELS keys win (dicts keep insertion
    order, the server re-inserts a relabelled key). False on an I/O error: never raises."""
    p = Path(p or path())
    labels: Dict[str, dict] = {}
    for k in list(dict.fromkeys(list(genres) + list(eras)))[-MAX_LABELS:]:
        e = {x: _clean(src.get(k)) for x, src in (("genre", genres), ("era", eras)) if _clean(src.get(k))}
        if e:
            labels[k] = e
    data = json.dumps({"version": VERSION, "labels": dict(sorted(labels.items()))},
                      sort_keys=True, indent=0, ensure_ascii=False)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(p)
        return True
    except OSError:
        return False


def backfill(genres: Dict[str, str], eras: Dict[str, str], tracked: dict, names: Dict[str, str]) -> int:
    """Labels from the knowledge export ({track id: {genre, era}} + its names.json) for songs
    that have none yet, matched by name, so the local label always wins. No model call.
    Returns how many labels were added."""
    added = 0
    for tid, lab in (tracked or {}).items():
        if not isinstance(lab, dict) or not names.get(tid):
            continue
        k = name_key(names[tid])
        if not k:
            continue
        if _clean(lab.get("genre")) and k not in genres:
            genres[k] = _clean(lab.get("genre"))
            added += 1
        if _clean(lab.get("era")) and k not in eras:
            eras[k] = _clean(lab.get("era"))
    return added


def for_export(names: Dict[str, str], genres: Dict[str, str], eras: Dict[str, str]) -> Dict[str, dict]:
    """{track id: {genre, era}} for every named song that has a label (knowledge export)."""
    out = {}
    for tid, n in names.items():
        k = name_key(n)
        e = {x: src[k] for x, src in (("genre", genres), ("era", eras)) if src.get(k)}
        if e:
            out[tid] = e
    return dict(sorted(out.items()))


# ------------------------------------------------------------------ offline labelling
# python -m app.music_brain.analysis.genre_labels label [--missing-only|--all]
#     [--backend local|claudecode] [--overwrite] [--dry-run]
# Labels library songs by name only (nothing else leaves the machine). The backend is
# AI_REVIEW_BACKEND (default local); claudecode is the owner's own `claude` CLI login,
# see app/music_brain/llm/claudecode.py.

LABEL_BATCH = 5                # local model, same as the server's suggest calls
CLAUDECODE_LABEL_BATCH = 40
LABEL_SCHEMA = {"type": "object", "required": ["labels"], "properties": {
    "labels": {"type": "array", "items": {"type": "object", "required": ["title_key", "genre", "era"],
               "properties": {"title_key": {"type": "string"}, "genre": {"type": "string"},
                              "era": {"type": "string"}}}}}}


def _label_system() -> str:
    from app.music_brain.analysis.genre import GENRE_FAMILIES, SCENES
    vocab = sorted({w for words in GENRE_FAMILIES.values() for w in words} | set(SCENES))
    return ("You label songs for a DJ's library by name only. For each song give its genre and "
            "its era. Genre: prefer one of these words, or a short phrase built from them: "
            + ", ".join(vocab) + ". Use another short genre name only when none fits. Era: the "
            "decade the recording came out, written like \"1990s\" or \"2020s\"; \"\" when unsure. "
            "Copy each title_key exactly. Answer JSON: {\"labels\": [{\"title_key\": \"<as given>\", "
            "\"genre\": \"<genre>\", \"era\": \"<decade>\"}]}")


def library_titles(cache_dir: Optional[Path] = None) -> Dict[str, str]:
    """{title key: display name} for every library song: uploads/_names.json, a duplicate
    id resolved to its canonical id (track_aliases.json) so a song counts once."""
    from app.music_brain.config import CACHE_DIR
    from app.ui.services.dedup_songs import load_aliases, resolve_alias
    cache = Path(cache_dir or CACHE_DIR)
    try:
        names = json.loads((cache / "uploads" / "_names.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(names, dict):
        return {}
    aliases = load_aliases(cache)
    out: Dict[str, str] = {}
    for tid in names:
        n = names.get(resolve_alias(tid, cache, aliases)) or names[tid]
        if isinstance(n, str) and (k := name_key(n)):
            out.setdefault(k, n)
    return out


def _era(label) -> str:
    from app.music_brain.analysis.genre import decade_of
    d = decade_of(label)
    return f"{d}s" if d is not None else ""


def label_library(missing_only: bool = True, overwrite: bool = False, backend: Optional[str] = None,
                  dry_run: bool = False, cache_dir: Optional[Path] = None, chat=None,
                  log=lambda m: None) -> dict:
    """Ask the model for {genre, era} of library songs; local labels win unless overwrite.
    dry_run: counts and calls only, no model call. `chat` is for tests (skips the backend)."""
    from app.music_brain.learning import set_ai
    p = path(cache_dir)
    genres, eras = load(p)
    titles = library_titles(cache_dir)
    todo = [(k, n) for k, n in sorted(titles.items())
            if not missing_only or not (genres.get(k) and eras.get(k))]
    from app.music_brain.llm import claudecode as cc
    b = cc.backend(backend)
    batch = CLAUDECODE_LABEL_BATCH if b == "claudecode" else LABEL_BATCH
    system = _label_system()
    users = [json.dumps({"songs": [{"title_key": k, "name": n} for k, n in todo[s:s + batch]]},
                        ensure_ascii=False) for s in range(0, len(todo), batch)]
    out = {"backend": b, "songs": len(titles), "to_label": len(todo), "calls": len(users)}
    if dry_run:   # no model call, no CLI needed
        return out | {"dry_run": True, "prompt_chars": sum(len(system) + len(u) for u in users)}
    tag = ""
    if chat is None:
        chat, tag, _ = set_ai.backend_chat(LABEL_SCHEMA, b)
    added = changed = answered = 0
    for i, user in enumerate(users):
        data = set_ai._ask(system, user, chat, "labels", tag=tag)
        if data is None:
            out["ai"] = "skipped (no model answering)"
            break
        asked = {s["title_key"] for s in json.loads(user)["songs"]}
        for x in data.get("labels") if isinstance(data.get("labels"), list) else []:
            if not isinstance(x, dict) or x.get("title_key") not in asked:
                continue           # a key the model made up or changed
            answered += 1
            k = x["title_key"]
            for store, v in ((genres, _clean(str(x.get("genre") or "")).lower()), (eras, _era(x.get("era")))):
                if not v or store.get(k) == v:
                    continue
                if not store.get(k):
                    added += 1
                elif overwrite:
                    changed += 1
                else:
                    continue       # the local label wins
                store[k] = v
        log(f"labels: {min((i + 1) * batch, len(todo))}/{len(todo)}")
    if added or changed:
        out["saved"] = save(genres, eras, p)
    return out | {"answered": answered, "added": added, "overwritten": changed}


def main(argv=None) -> int:
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="python -m app.music_brain.analysis.genre_labels")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("label", help="Label library songs with genre / era (names only are sent).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--missing-only", action="store_true", help="songs without a genre or era (default)")
    g.add_argument("--all", action="store_true", help="every library song")
    p.add_argument("--backend", choices=("local", "claudecode"), default=None,
                   help="default: AI_REVIEW_BACKEND, else local")
    p.add_argument("--overwrite", action="store_true", help="the model's answer replaces a local label")
    p.add_argument("--dry-run", action="store_true", help="counts and calls only, no model call")
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv()
        res = label_library(missing_only=not args.all, overwrite=args.overwrite, backend=args.backend,
                            dry_run=args.dry_run, log=lambda m: print(m, file=sys.stderr, flush=True))
    except Exception as exc:  # one JSON error line, never a traceback on stdout
        print(json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:400]}"}, indent=2))
        return 1
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
