"""AFRIX: the excluded-songs matcher. The rule is data in dj-config EXCLUDED_RULES (generated into
generated/dj_config.py); this module only evaluates it for the server's library sweep and download gate.
dj-core `excludedReason` is the reference (the pick gate); the goldens keep both in step.

excluded_reason(display_name=, artist=, title=, genres=, lang_id=, country=) -> None | {"reason", "strong", "cue"}
  strong cues (artist, genre, country) exclude; weak cues (lang_id, title word) are review-only.
Pick / download config only: never put the blocklist names in an LLM prompt or KB text.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional

from app.music_brain.generated import dj_config as _cfg


def norm(text: object) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _rules() -> dict:
    return _cfg.EXCLUDED_RULES


def excluded_reason(display_name: str = "", artist: str = "", title: str = "", genres: Iterable[str] = (),
                    lang_id: Optional[str] = None, country: Optional[str] = None) -> Optional[dict]:
    r = _rules()
    hay = f" {norm(artist)} {norm(title)} {norm(display_name)} "
    a_compact = norm(artist).replace(" ", "")
    for name in r["artists"]:
        n = norm(name)
        if n and (f" {n} " in hay or (a_compact and a_compact == n.replace(" ", ""))):
            return {"reason": f"excluded: artist {n}", "strong": True, "cue": "artist"}
    gset = {norm(g) for g in r["genres"]}
    for g in genres or ():
        if norm(g) in gset:
            return {"reason": f"african: genre {norm(g)}", "strong": True, "cue": "genre"}
    wset = {norm(g) for g in r.get("weak_genres", [])}
    if country and str(country).upper() in r["countries"]:
        return {"reason": f"african: country {str(country).upper()}", "strong": True, "cue": "country"}
    for g in genres or ():
        if norm(g) in wset:
            return {"reason": f"african: weak genre {norm(g)}", "strong": False, "cue": "genre"}
    if lang_id and str(lang_id).lower() in r["lang_ids"]:
        return {"reason": f"african: language {lang_id}", "strong": False, "cue": "lang_id"}
    t = f" {norm(title) or norm(display_name)} "
    for g in sorted(gset | wset):
        if f" {g} " in t:
            return {"reason": f"african: title cue {g}", "strong": False, "cue": "title_cue"}
    return None
