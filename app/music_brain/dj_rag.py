"""DJ-rules RAG for autopilot's next-track prompt, mined from the ./DJ/ wiki.

Complements dj_knowledge.py (which hands the model a genre playbook + the
"what do I play next" steps): this module serves the cross-cutting rules a
next-track pick needs - Camelot moves, phrase alignment, tempo bridges, EQ
ownership, energy arc - plus per-genre playbook rules and per-recipe "when to
use / when not" guidance for all 28 Transition Cookbook recipes.

Two halves, deliberately split:

* BUILD (authoring time only): `python -m app.music_brain.dj_rag --build`
  parses the markdown notes into short tagged chunks and writes the static
  corpus `dj_rag_corpus.json` next to this file. Plain deterministic text
  extraction plus a handful of hand-distilled CORE rules; no model and no
  network are involved. Re-run it when ./DJ/ changes and commit the JSON.
* RUNTIME: retrieve() only reads that JSON (cached) and ranks chunks with
  tag slots plus a stdlib TF-IDF tie-break. Fully local and offline; if the
  corpus file is missing or broken it returns [] and the caller's prompt
  simply has no DJ RULES block.

The output is advisory prompt text only. Nothing here filters, scores or
gates suggestions; autopilot_service does that on its own rules.
"""
from __future__ import annotations

import json
import math
import re
import sys
from functools import lru_cache
from pathlib import Path

CORPUS_PATH = Path(__file__).with_name("dj_rag_corpus.json")
DJ_DIR = Path(__file__).resolve().parents[2] / "DJ"
MAX_CHUNK_CHARS = 320

# Hand-distilled cross-cutting rules (numbers from CLAUDE.md section 4 and the
# notes named in each chunk). Tagged "core": they win ties inside their slot.
CORE: list[dict] = [
    {"tags": ["camelot", "core"], "source": "02 - Music Theory/Harmonic Mixing & Camelot System.md",
     "text": "CAMELOT SCORE: same key = perfect (1.0); +-1 hour same letter = smooth (0.9); "
             "same hour other letter = relative major/minor (0.85); +2 hours same letter = "
             "energy boost (0.8); 3+ hours apart = harmonic clash, avoid unless bridging with "
             "an Echo Out or a Breakdown Transition."},
    {"tags": ["phrase", "core"], "source": "02 - Music Theory/Phrasing & Structure.md",
     "text": "PHRASING: dance music is built in 8-bar (32-beat) phrases. Snap every transition "
             "entry/exit to the nearest 8-bar boundary so both tracks start a new phrase on the "
             "same Beat 1; never mix mid-phrase."},
    {"tags": ["bpm_gap", "core"], "source": "08 - Open Format/Genre Bridge Playbook.md",
     "text": "BPM GAPS: <=6% tempo delta blends beat-to-beat (32-micro-step pitch ramp). Bigger "
             "gaps (e.g. 128 -> 174) are never stretched: bridge via a half/double-time ratio, an "
             "Echo Out, or a Breakdown Transition, or climb a <=6%-per-step ladder of songs."},
    {"tags": ["genre_bridge", "core"], "source": "08 - Open Format/Open-Format DJing Guide.md",
     "text": "GENRE BRIDGES: cross distant genres through a bridge track that already belongs to "
             "both (a remix, a mash-up, a song whose tempo/energy sits between them). Change genre, "
             "tempo or key, but never all three at once without an auditory bridge."},
    {"tags": ["eq", "core"], "source": "04 - Core Techniques/EQ & Frequency Management.md",
     "text": "EQ OWNERSHIP: sub-bass below 120 Hz never plays from two tracks at once; one deck "
             "owns the low end during a transition and hands it off on a downbeat (Bass Swap), "
             "never stacks it."},
    {"tags": ["energy", "core"], "source": "06 - Energy & Crowd/Energy Management & Dynamics.md",
     "text": "ENERGY ARC: a set reads as a deliberate curve, not a flat line - warm-up builds "
             "slowly, the build pushes, the peak sustains or escalates, the close steps down gently. "
             "Constant maximum energy causes fatigue; contrast is what makes a drop land."},
]

# genre keyword -> playbook, reused from dj_knowledge so both modules agree.
PHASES = ("warmup", "build", "peak", "cooldown")
_PHASE_WORDS = {
    "warmup": {"open", "opening", "warm", "establish", "identity", "atmosphere", "intro"},
    "build": {"build", "builds", "climb", "ramp", "development", "pocket", "first", "contrast"},
    "peak": {"peak", "climax", "main", "banger", "drop", "slam", "valley", "reset"},
    "cooldown": {"finale", "closing", "outro", "sing", "anchor", "cool", "emotional"},
}
_WORD = re.compile(r"[a-z0-9&]+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "it", "for", "with",
         "as", "at", "by", "be", "you", "your", "this", "that", "from", "into", "are", "not"}


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _genre_tag(genre: str) -> str | None:
    from app.music_brain.dj_knowledge import playbook_for
    stem = playbook_for(genre) if genre else None
    return f"genre:{_slug(stem)}" if stem else None


def _phase(set_position: float | None) -> str | None:
    # Same thresholds as autopilot_service._set_arc_phase.
    if set_position is None:
        return None
    if set_position < 0.3:
        return "warmup"
    if set_position < 0.7:
        return "build"
    if set_position < 0.9:
        return "peak"
    return "cooldown"


# --------------------------------------------------------------------------- build

def _flat(text: str) -> str:
    """Markdown/LaTeX noise -> one plain line."""
    from app.music_brain.dj_knowledge import _clean
    text = re.sub(r"\\text\{\s*([^}]*)\}", r" \1", text)
    text = text.replace("\\times", "x").replace("\\infty", "inf").replace("\\pm", "+-")
    text = _clean(text).replace("\\", "")
    return " ".join(text.split())


def _cap(text: str, n: int = MAX_CHUNK_CHARS) -> str:
    if len(text) <= n:
        return text
    cut = text[:n]
    end = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[:end + 1] if end > n // 2 else cut.rsplit(" ", 1)[0]).rstrip(" ;,") + "..."


def _sections(md: str, level: int) -> list[tuple[str, str]]:
    """(heading, body) for every heading of exactly `level`; body runs to the
    next heading of the same or higher level."""
    out, head, body = [], None, []
    for ln in md.splitlines():
        m = re.match(r"^(#+)\s+(.*)", ln)
        if m and len(m.group(1)) <= level:
            if head is not None:
                out.append((head, "\n".join(body)))
            head, body = (m.group(2).strip(), []) if len(m.group(1)) == level else (None, [])
        elif head is not None:
            body.append(ln)
    if head is not None:
        out.append((head, "\n".join(body)))
    return out


def _items(body: str) -> list[str]:
    """Top-level bullets / numbered items, nested lines folded in with '; '."""
    items: list[str] = []
    for ln in body.splitlines():
        if not ln.strip() or ln.strip() == "---" or ln.lstrip().startswith(("```", "|", ">")):
            continue
        top = re.match(r"^(?:[*-]|\d+\.)\s+(.*)", ln)
        if top:
            items.append(top.group(1))
        elif items and re.match(r"^\s+(?:[*-]|\d+\.)\s+", ln):
            items[-1] += "; " + re.sub(r"^\s+(?:[*-]|\d+\.)\s+", "", ln)
        elif items and ln.startswith(" "):
            items[-1] += " " + ln.strip()
    return [i for i in (_flat(x) for x in items) if i]


def _para(body: str) -> str:
    """First prose paragraph of a section (skips bullets, code, tables)."""
    for block in re.split(r"\n\s*\n", body):
        b = block.strip()
        if b and not b.startswith(("*", "-", "```", "|", ">", "#")) and not re.match(r"\d+\.", b):
            return _flat(b)
    return ""


def _note(rel: str) -> str:
    return (DJ_DIR / rel).read_text(encoding="utf-8")


def _heading_label(h: str) -> str:
    return _flat(re.sub(r"^(?:\d+\.|[A-Z]\.|Stage \d+:|Challenge \d+:|Scenario [A-Z]:)\s*", "", h))


def _genres_in(text: str) -> set[str]:
    from app.music_brain.dj_knowledge import _GENRE_PLAYBOOKS
    low = text.lower()
    return {f"genre:{_slug(stem)}" for keys, stem in _GENRE_PLAYBOOKS
            if any(re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", low) for k in keys)}


def _phase_tags(text: str) -> set[str]:
    words = set(_tokens(text))
    return {p for p, ws in _PHASE_WORDS.items() if words & ws}


def build_corpus() -> list[dict]:
    """Parse ./DJ/ into tagged chunks. Authoring-time only (see module doc)."""
    from app.music_brain.dj_knowledge import _GENRE_PLAYBOOKS
    from app.music_brain.knowledge_parser import KnowledgeParser

    chunks: list[dict] = [dict(c) for c in CORE]
    seen: set[str] = {c["text"] for c in chunks}

    def add(tags, source: str, label: str, text: str) -> None:
        text = _cap(f"{label}: {text}" if label else text)
        if len(text) < 40 or text in seen:
            return
        seen.add(text)
        chunks.append({"tags": sorted(set(tags)), "source": source, "text": text})

    # Harmonic mixing: the trajectory table + when key matters / can be ignored.
    src = "02 - Music Theory/Harmonic Mixing & Camelot System.md"
    md = _note(src)
    for row in re.findall(r"^\|\s*\*\*(.+?)\*\*\s*\|(.+)\|\s*$", md, re.M):
        cells = [_flat(c) for c in row[1].split("|")]
        if len(cells) >= 3:
            add(["camelot"], src, f"CAMELOT {_flat(row[0])} ({cells[0]}, e.g. {cells[1]})", cells[2])
    for head, body in _sections(md, 3):
        if head.startswith("When Key Compatibility"):
            bypass = "Disregarded" in head
            for it in _items(body):
                add(["camelot"] + (["camelot_bypass"] if bypass else []), src,
                    "KEY CAN BE IGNORED" if bypass else "KEY IS NON-NEGOTIABLE", it)

    # Phrasing.
    src = "02 - Music Theory/Phrasing & Structure.md"
    md = _note(src)
    for head, body in _sections(md, 2):
        if head[:2] in ("1.", "2.", "3."):
            for it in _items(body):
                add(["phrase"], src, f"PHRASING ({_heading_label(head)})", it)

    # EQ & frequency ownership.
    src = "04 - Core Techniques/EQ & Frequency Management.md"
    md = _note(src)
    for head, body in _sections(md, 3):
        items = _items(body)
        law = [i for i in items if i.startswith(("The Law", "Result", "DJ Usage"))]
        for it in law or items[:2]:
            add(["eq"], src, f"EQ ({_heading_label(head)})", it)

    # Energy: 8 dimensions, set curves, rule-breaking.
    src = "06 - Energy & Crowd/Energy Management & Dynamics.md"
    md = _note(src)
    for head, body in _sections(md, 2):
        if head.startswith("1."):
            for it in _items(body):
                add(["energy"], src, "ENERGY DIMENSION", it)
    for head, body in _sections(md, 3):
        label = f"SET CURVE ({_heading_label(head)})"
        g = _genres_in(head)
        para = _para(body)
        if para:
            add(["energy"] + sorted(g), src, label, para)
        for it in _items(body):
            add(["energy"] + sorted(g | _phase_tags(it.split(":")[0])), src, label, it)

    # Set construction: the 8-stage narrative, each stage bound to an arc phase.
    src = "12 - Set Construction/Set Construction & Architecture.md"
    md = _note(src)
    stage_phase = {1: "warmup", 2: "warmup", 3: "build", 4: "build", 5: "build",
                   6: "peak", 7: "peak", 8: "cooldown"}
    for head, body in _sections(md, 3):
        m = re.match(r"Stage (\d+)", head)
        if m:
            add(["energy", stage_phase.get(int(m.group(1)), "build")], src,
                f"SET STAGE {m.group(1)} ({_heading_label(head)})", " ".join(_items(body)))

    # Reading the room: stay-vs-switch, probing, familiarity.
    src = "06 - Energy & Crowd/Reading the Room & Crowd Psychology.md"
    md = _note(src)
    for head, body in _sections(md, 2):
        if head[:2] in ("2.", "3."):
            for it in _items(body):
                add(["energy", "crowd"], src, f"CROWD ({_heading_label(head)})", it)

    # Genre bridges: one chunk per blueprint (problem + solution steps).
    src = "08 - Open Format/Genre Bridge Playbook.md"
    md = _note(src)
    for head, body in _sections(md, 2):
        if not re.match(r"\d+\.", head) or "Matrix" in head:
            continue
        items = _items(body)
        problem = next((i.split(":", 1)[1].strip() for i in items if i.startswith("The Musical Problem")), "")
        steps = [i for i in items if not i.startswith(("The Musical", "The Solution", "The Musical Physics"))]
        blue = next((re.sub(r":$", "", i.split("(", 1)[-1].split(")")[0]) for i in items
                     if i.startswith("The Solution")), "")
        text = f"{problem} Fix ({blue}): " + " ".join(steps)
        add(["bpm_gap", "genre_bridge"] + sorted(_genres_in(head)), src,
            f"BRIDGE {_heading_label(head)}", text)

    src = "08 - Open Format/Open-Format DJing Guide.md"
    md = _note(src)
    for head, body in _sections(md, 2):
        if head.startswith("1."):
            add(["genre_bridge"], src, "SHARED ANCHOR", " ".join(_items(body)))
    for head, body in _sections(md, 3):
        items = _items(body)
        if items:
            add(["genre_bridge", "bpm_gap"], src, f"BRIDGE MECHANISM ({_heading_label(head)})",
                " ".join(items))

    # Genre playbooks: profile, challenges, transition-in/out.
    for _keys, stem in _GENRE_PLAYBOOKS:
        src = f"09 - Genre Playbooks/{stem}.md"
        path = DJ_DIR / src
        if not path.exists():
            continue
        md = path.read_text(encoding="utf-8")
        g = f"genre:{_slug(stem)}"
        home = f"home:{_slug(stem)}"  # chunk comes from this genre's own playbook
        name = stem.replace(" Playbook", "").upper()
        for head, body in _sections(md, 2):
            if head.startswith("1."):
                for it in _items(body):
                    if it.startswith(("Typical BPM", "Common Transition", "Typical Energy", "Common Structures")):
                        add([g, home, "playbook"], src, name, it)
        for head, body in _sections(md, 3):
            items = _items(body)
            if head.startswith("Challenge"):
                add([g, home, "playbook", "eq" if "bass" in head.lower() else "playbook"], src,
                    f"{name} {_heading_label(head)}", " ".join(items))
            elif head.startswith("How") and "Transition" in head:
                direction = "IN" if "INTO" in head else "OUT"
                for it in items:
                    add([g, home, "playbook", "genre_bridge"] + sorted(_genres_in(it.split(":")[0]) - {g}),
                        src, f"{name} TRANSITION {direction}", it)

    # Transition Cookbook: when to use / when not, per recipe.
    for r in KnowledgeParser().load().get_all():
        use = _items(r.when_to_use)[:2]
        avoid = _items(r.when_not_to_use)[:1]
        if not use:
            continue
        gtags = set()
        for gname in r.genres:
            gtags |= _genres_in(gname)
        flags = [r.difficulty.lower()] if r.difficulty else []
        if r.requires_stems:
            flags.append("needs stems")
        if r.max_bpm_delta is None:
            flags.append("crosses any tempo gap")
        if not r.camelot_compatible_only:
            flags.append("any key")
        tags = {"recipe", f"recipe:{r.slug}"} | gtags
        if r.max_bpm_delta is None:
            tags.add("bpm_gap")
        if not r.camelot_compatible_only:
            tags.add("camelot_bypass")
        text = "Use when " + " ".join(use) + (f" Avoid when {avoid[0]}" if avoid else "")
        add(tags, f"05 - Transition Cookbook/{r.name}.md",
            f"RECIPE {r.name}" + (f" [{', '.join(flags)}]" if flags else ""), text)

    return chunks


def write_corpus(path: Path = CORPUS_PATH) -> int:
    chunks = build_corpus()
    path.write_text(json.dumps({"version": 1, "chunks": chunks}, indent=1, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return len(chunks)


# --------------------------------------------------------------------------- runtime

@lru_cache(maxsize=1)
def _corpus() -> tuple[tuple[dict, ...], dict[str, float]]:
    """Load the static corpus once. Missing/broken file -> empty corpus."""
    try:
        raw = json.loads(CORPUS_PATH.read_text(encoding="utf-8")).get("chunks", [])
    except (OSError, ValueError, AttributeError):
        return (), {}
    chunks = tuple(
        {"tags": frozenset(c["tags"]), "text": c["text"], "words": frozenset(_tokens(c["text"]))}
        for c in raw if isinstance(c, dict) and c.get("text") and isinstance(c.get("tags"), list))
    df: dict[str, int] = {}
    for c in chunks:
        for w in c["words"]:
            df[w] = df.get(w, 0) + 1
    idf = {w: math.log((1 + len(chunks)) / (1 + d)) + 1.0 for w, d in df.items()}
    return chunks, idf


def retrieve(genre: str = "", tempo_gap_pct: float | None = None,
             set_position: float | None = None, n: int = 6) -> list[str]:
    """Up to n rule chunks for the current pick, in slot order.

    Slots, by priority: Camelot + phrasing always; a tempo-bridge rule only
    when the gap is actually > 6 %; a rule from the genre's own playbook (its
    transition-in/out advice when crossing a gap) when the genre maps to one;
    an energy/set-stage rule for the arc phase when the set position is known;
    EQ ownership; a best-fit recipe for the genre; any genre bridge. Remaining
    CORE rules fill free slots. Inside a slot, chunks are ranked by TF-IDF overlap
    with the query words (genre, phase, gap), CORE first on ties, then corpus
    order - fully deterministic.
    """
    chunks, idf = _corpus()
    if not chunks or n <= 0:
        return []
    gtag = _genre_tag(genre)
    big_gap = tempo_gap_pct is not None and tempo_gap_pct > 6
    phase = _phase(set_position)
    query = set(_tokens(genre))
    if big_gap:
        query |= {"tempo", "bpm", "bridge", "gap"}
    if phase:
        query |= _PHASE_WORDS[phase]

    def rank(c: dict, i: int):
        s = sum(idf.get(w, 0.0) for w in query & c["words"])
        return (-s, "core" not in c["tags"], i)

    home = "home:" + gtag[6:] if gtag else None
    # Priority order; slots past n are dropped. "Key can be ignored" rules only
    # make sense when a bridge is in play.
    slots: list = [lambda t: "camelot" in t and ("camelot_bypass" not in t or big_gap),
                   lambda t: "phrase" in t]
    if big_gap:
        slots.append((lambda t: "bpm_gap" in t and gtag in t) if gtag else (lambda t: "bpm_gap" in t))
    if home:
        slots.append(lambda t: home in t and ("genre_bridge" in t) == big_gap)
    if phase:
        slots.append(lambda t: phase in t and "energy" in t)
    slots.append(lambda t: "eq" in t)
    if gtag:
        slots.append((lambda t: gtag in t and "recipe" in t) if not big_gap
                     else (lambda t: gtag in t and "recipe" in t and "bpm_gap" in t))
    if big_gap:
        slots.append(lambda t: "genre_bridge" in t)

    order = sorted(range(len(chunks)), key=lambda i: rank(chunks[i], i))
    used: list[int] = []
    for ok in slots:
        if len(used) >= n:
            break
        pick = next((i for i in order if i not in used and ok(chunks[i]["tags"])), None)
        if pick is not None:
            used.append(pick)
    for i in order:  # fill with remaining CORE rules
        if len(used) >= n:
            break
        if i not in used and "core" in chunks[i]["tags"] \
                and ("energy" not in chunks[i]["tags"] or phase) \
                and ("bpm_gap" not in chunks[i]["tags"] or big_gap) \
                and ("genre_bridge" not in chunks[i]["tags"] or big_gap):
            used.append(i)
    return [chunks[i]["text"] for i in used[:n]]


if __name__ == "__main__":
    if "--build" in sys.argv:
        print(f"wrote {write_corpus()} chunks -> {CORPUS_PATH}")
    else:
        print("usage: python -m app.music_brain.dj_rag --build")
