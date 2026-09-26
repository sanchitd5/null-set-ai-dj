"""Compact DJ-knowledge briefs for LLM prompts, pulled from the ./DJ/ wiki.

The song-selection prompt used to rely on the model's own (thin) DJ knowledge.
This module extracts the decision rules the wiki already documents and hands
the model a short, grounded brief instead:

  * "DJ - What Do I Play Next"  -> Steps 3-5 (energy move, continuity vs
    contrast, connecting thread)
  * "Track Selection Framework" -> the Golden Rule / Rule of Two Anchors
  * the Genre Playbook matching the current genre -> genre profile + how DJs
    transition into / out of it

Headings are matched by text, bullets are kept and markdown noise (wiki links,
bold, LaTeX arrows) is stripped. Every section is capped so the whole brief
stays small enough for a local model (~1.5k chars). Results are cached.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

DJ_DIR = Path(__file__).resolve().parents[2] / "DJ"
MAX_SECTION_CHARS = 420
MAX_BRIEF_CHARS = 2000

# genre keyword -> playbook file stem (first match wins, most specific first)
_GENRE_PLAYBOOKS = [
    (("drum and bass", "drum & bass", "dnb", "jungle", "liquid"), "Drum & Bass (DnB) Playbook"),
    (("dubstep", "riddim"), "Dubstep Playbook"),
    (("future bass", "melodic bass"), "Future Bass & Melodic Bass Playbook"),
    (("trap", "bass music"), "Trap & Bass Music Playbook"),
    (("hip-hop", "hip hop", "rap", "r&b", "rnb"), "Hip-Hop & R&B Playbook"),
    (("melodic", "downtempo", "ambient", "electronica", "idm"), "Melodic Electronic Playbook"),
    (("progressive", "edm", "big room", "electro house"), "EDM & Progressive House Playbook"),
    (("tech house", "house", "garage", "ukg", "2-step", "disco"), "House & Tech House Playbook"),
    (("techno", "breaks"), "Melodic Electronic Playbook"),
    (("pop",), "Pop Playbook"),
]


def _clean(text: str) -> str:
    text = re.sub(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]", r"\1", text)      # [[Link|alias]] -> Link
    text = text.replace("$\\to$", "->").replace("$\\leftrightarrow$", "<->")
    text = re.sub(r"\$([^$]*)\$", r"\1", text)                        # $x$ -> x
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text).replace("*", "-")
    text = re.sub(r"^> ?(\[![A-Z]+\] ?)?", "", text, flags=re.M)      # callouts
    text = re.sub(r"[ \t]+", " ", text)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln and not ln.startswith(("```", "|", "---", "flowchart", "graph ")))


def _section(md: str, heading_contains: str) -> str:
    """Body of the first heading containing `heading_contains` (until the next
    heading of the same or higher level)."""
    lines = md.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"^(#+)\s+(.*)", ln)
        if m and heading_contains.lower() in m.group(2).lower():
            level = len(m.group(1))
            body = []
            for nxt in lines[i + 1:]:
                m2 = re.match(r"^(#+)\s", nxt)
                if m2 and len(m2.group(1)) <= level:
                    break
                body.append(nxt)
            return _clean("\n".join(body))
    return ""


def _cap(text: str, n: int = MAX_SECTION_CHARS) -> str:
    if len(text) <= n:
        return text
    cut = text[:n]
    return cut[: cut.rfind("\n")] if "\n" in cut else cut


@lru_cache(maxsize=None)
def _note(stem: str) -> str:
    for p in DJ_DIR.rglob(f"{stem}.md"):
        return p.read_text(encoding="utf-8")
    return ""


def playbook_for(genre: str) -> Optional[str]:
    g = (genre or "").lower()
    for keys, stem in _GENRE_PLAYBOOKS:
        if any(k in g for k in keys):
            return stem
    return None


def _parts(genre: str) -> Iterable[str]:
    stem = playbook_for(genre)
    if stem:
        pb = _note(stem)
        into = _section(pb, "Transition INTO")
        out = _section(pb, "Transition OUT")
        if into or out:
            yield f"GENRE PLAYBOOK ({stem}):\n" + _cap(f"Into: {into}\nOut of: {out}", 420)
    nxt = _note("DJ - What Do I Play Next")
    if nxt:
        yield "ENERGY MOVE (What Do I Play Next, step 3):\n" + _cap(_section(nxt, "Step 3"), 300)
        yield "CONTINUITY OR CONTRAST (step 4):\n" + _cap(_section(nxt, "Step 4"), 380)
        yield "CONNECTING THREAD (step 5, need at least one):\n" + _cap(_section(nxt, "Step 5"), 420)
    tsf = _note("Track Selection Framework")
    if tsf:
        rule = [ln for ln in _section(tsf, "Golden Rule").splitlines() if "Two Anchors" in ln]
        if rule:
            yield "RULE OF TWO ANCHORS (Track Selection Framework):\n" + _cap(rule[0], 300)


@lru_cache(maxsize=64)
def selection_brief(genre: str = "") -> str:
    """Grounded track-selection rules from ./DJ/, <= MAX_BRIEF_CHARS."""
    out, used = [], 0
    for part in _parts(genre):
        if used + len(part) > MAX_BRIEF_CHARS:
            break
        out.append(part)
        used += len(part) + 2
    return "\n\n".join(out)
