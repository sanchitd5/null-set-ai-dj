"""Genre-family distance: shared by autopilot's suggestion filter (app/ui/
autopilot_service.py) and RecipeMatcher's transition scoring, so a manual
/api/match call gets the same "no unrelated-genre jump" sense autopilot
already enforces.

Genre labels themselves are never audio-derived (TrackAnalysis stays pure
librosa/DSP); a caller resolves a label from track identity/metadata and
passes it in.
"""

from __future__ import annotations

import re
from typing import Optional

GENRE_FAMILIES = {
    "south_asian": ("punjabi", "bhangra", "desi", "bollywood", "hindi", "urdu", "pakistani",
                    "indian", "filmi", "sufi", "qawwali", "haryanvi", "tamil", "telugu"),
    "electronic": ("house", "techno", "garage", "trance", "edm", "electronic", "electronica",
                   "dubstep", "drum & bass", "drum and bass", "dnb", "bass music", "breakbeat",
                   "downtempo", "ambient", "future bass", "electro", "idm", "jungle", "dance",
                   "breaks", "trip hop", "chillout", "big room", "riddim"),
    "hiphop": ("hip-hop", "hip hop", "rap", "trap", "drill", "grime"),
    "rnb": ("r&b", "rnb", "soul"),
    "latin": ("reggaeton", "latin", "dembow", "cumbia", "bachata", "salsa", "urbano"),
    "afro": ("afrobeat", "afro", "amapiano", "afropop"),
    "rock": ("rock", "punk", "metal", "grunge"),
    "pop": ("pop",),
    "country": ("country", "folk", "americana"),
    "jazz": ("jazz", "funk", "disco"),
}


# "electronic" is too broad to hold a set: it let The Sound of Goodbye (trance) hand over to
# Bonobo - Me and You (downtempo) (owner, session 2026-09-30_205816). A label in that family is
# placed in one or more of these SUB-FAMILIES (clusters of scenes that mix into each other).
# Terms are whole words; the longest term wins and consumes its words, so "melodic techno" is
# melodic, not techno, and "electro" never matches inside "electronic". A label in the family that
# names no cluster ("electronic", "indie electronic", "dance pop") stays the generic "electronic",
# which neighbours every cluster: too vague to refuse anything.
ELECTRONIC_CLUSTERS = {
    # four-to-the-floor house and its garage / disco / afro / latin offshoots
    "house": ("house", "deep house", "tech house", "bass house", "synth house", "disco house",
              "afro house", "latin house", "funky house", "uk garage", "garage", "speed garage"),
    # the melodic / progressive floor: melodic techno + house, progressive, organic
    "melodic": ("melodic", "melodic techno", "melodic house", "progressive house", "progressive",
                "organic house"),
    # trance, split from melodic (owner, 2026-09-30) so the chill <-> melodic bridge does not
    # let trance hand over to downtempo; it keeps every other melodic neighbour
    "trance": ("trance", "psytrance", "uplifting trance"),
    # warehouse techno: straight, hard, minimal, industrial
    "techno": ("techno", "hard techno", "minimal techno", "industrial techno", "acid techno"),
    # bass music: dubstep, riddim, future bass, melodic dubstep
    # (bass house sits in house AND bass: the bridge between the two floors)
    "bass": ("dubstep", "melodic dubstep", "bass music", "bass house", "future bass", "riddim", "brostep"),
    # 170+ BPM: drum and bass, jungle, liquid
    "dnb": ("drum and bass", "drum & bass", "dnb", "jungle", "liquid", "neurofunk"),
    # the low-energy listening end: downtempo, ambient, chillout, trip hop, idm, electronica
    "chill": ("downtempo", "ambient", "chillout", "chill", "trip hop", "idm", "electronica"),
    # festival / commercial dance: big room, edm, electro (house), eurodance, plain "dance"
    "edm": ("edm", "big room", "electro", "electro house", "eurodance", "dance"),
    # breakbeat, breaks, big beat
    "breaks": ("breakbeat", "breaks", "big beat"),
}

# Cluster pairs DJs really mix across (owner decisions, 2026-09-30). chill's only neighbour is
# melodic (organic / melodic house <-> electronica, downtempo); trance <-> chill stays a jump.
# Trance has exactly melodic's neighbours minus chill: melodic, techno, house, bass, edm.
ELECTRONIC_NEIGHBOURS = tuple(frozenset(p) for p in (
    ("house", "melodic"),    # melodic house / progressive sit on the house floor
    ("melodic", "techno"),   # melodic techno <-> techno
    ("chill", "melodic"),    # organic / melodic house <-> electronica, downtempo
    ("bass", "melodic"),     # melodic dubstep / future bass <-> melodic house, techno
    ("bass", "house"),       # dubstep <-> house, bass house bridges them
    ("edm", "melodic"),      # festival main stage: big room / eurodance <-> melodic house, progressive
    ("trance", "melodic"),   # trance keeps melodic's neighbours, chill aside
    ("trance", "techno"),
    ("trance", "house"),
    ("trance", "bass"),
    ("trance", "edm"),
    ("house", "techno"),     # tech house bridges them
    ("house", "edm"),        # electro / big room house
    ("edm", "bass"),         # festival sets drop dubstep / future bass
    ("bass", "dnb"),         # the bass music scene
    ("breaks", "house"),     # UK breaks / garage
    ("breaks", "dnb"),       # breakbeat roots of jungle
)) + tuple(frozenset({"electronic", c}) for c in ELECTRONIC_CLUSTERS)

_TERM_CLUSTERS: dict = {}
for _c, _ts in ELECTRONIC_CLUSTERS.items():
    for _t in _ts:
        _TERM_CLUSTERS.setdefault(_t, set()).add(_c)
_CLUSTER_TERMS = sorted(_TERM_CLUSTERS, key=len, reverse=True)


def _electronic_clusters(g: str) -> set:
    s = " " + " ".join(re.sub(r"[-_/,()]", " ", g).split()) + " "
    found = set()
    for term in _CLUSTER_TERMS:
        if f" {term} " in s:
            found |= _TERM_CLUSTERS[term]
            s = s.replace(f" {term} ", "  ")
    return found or {"electronic"}


def genre_families(label) -> set:
    """Broad families, with "electronic" split into its sub-families (ELECTRONIC_CLUSTERS)."""
    g = str(label or "").lower()
    fams = {fam for fam, keys in GENRE_FAMILIES.items() if any(k in g for k in keys)}
    if "electronic" in fams:
        fams = (fams - {"electronic"}) | _electronic_clusters(g)
    return fams


# Families a set moves between freely (owner, 2026-09-30: hip-hop <-> R&B are neighbours, the
# DJ Timeless set does it): a move between neighbours is not a family jump.
NEIGHBOUR_FAMILIES = (frozenset({"hiphop", "rnb"}),) + ELECTRONIC_NEIGHBOURS


def scene_keys(label) -> list:
    """The console's scene anchor tokens (booking vet `families`): the families plus one "a|b" token
    per electronic neighbour pair, so two labels share a token exactly when their clusters are the
    same or neighbours. hip-hop <-> R&B gets no token: the anchor keeps them apart, as before."""
    fams = genre_families(label)
    pairs = {"|".join(sorted(p)) for p in ELECTRONIC_NEIGHBOURS if p & fams}
    return sorted(fams | pairs)


def _families_touch(a: set, b: set) -> bool:
    """A shared family, or two families listed as neighbours."""
    return bool(a & b) or any(x != y and frozenset({x, y}) in NEIGHBOUR_FAMILIES for x in a for y in b)


def family_jump(genre_a, genre_b) -> bool:
    """True when both labels are known and share no family, neighbours aside (e.g. melodic house -> metal)."""
    a, b = genre_families(genre_a), genre_families(genre_b)
    return bool(a and b) and not _families_touch(a, b)


# Scene-level terms: finer than the families above. "electronic" holds both
# eurodance and UK breakbeat, so family alone lets Aqua "Barbie Girl" ->
# Bicep "Glue" through (user). Two labels are NEAR when they share one of these
# (melodic house / deep house share "house"; eurodance / dance-pop share
# "dance"). Longer terms are listed first so "drum and bass" wins over "bass".
SCENES = (
    "drum and bass", "drum & bass", "future bass", "bass house", "uk garage", "hip hop", "hip-hop",
    "house", "techno", "trance", "garage", "dubstep", "dnb", "jungle", "breakbeat", "breaks",
    "electronica", "idm", "ambient", "downtempo", "disco", "funk", "dance", "pop", "rap", "trap",
    "drill", "grime", "r&b", "rnb", "soul", "reggaeton", "latin", "dembow", "cumbia", "afro",
    "amapiano", "rock", "punk", "metal", "indie", "country", "folk", "jazz", "bollywood",
    "punjabi", "bhangra", "synth", "electro",
    "melodic",   # melodic house <-> melodic techno: a neighbour move the autopilot allows
)


def genre_scenes(label) -> set:
    g = " " + " ".join(str(label or "").lower().replace("-", " ").split()) + " "
    found = set()
    for s in SCENES:
        if s.replace("-", " ") in g and not any(s != f and s in f for f in found):
            found.add(s)
    return found


def genre_near(genre_a, genre_b) -> Optional[bool]:
    """True: same scene (share a scene term). False: both known, no shared
    scene term. None: at least one label unknown or unrecognised."""
    a, b = genre_scenes(genre_a), genre_scenes(genre_b)
    if not a or not b:
        return None
    return bool(a & b)


# Scene continuity for the fallback paths (library tempo-lock, atlas backup, scene anchor).
# Session 2026-09-30_191133: the deadline's library fallback put Four Tet (electronic) after
# Big Boss Vette (hip hop) because the console sent no genre at all. Best first.
SCENE_RANK = {"scene": 0, "family": 1, "unknown": 2, "cross": 3}


def scene_relation(ref, other) -> str:
    """How `other` sits against the reference label `ref` (the playing song's stored label, or
    the set's scene anchor): "scene" (a shared scene term), "family" (a shared genre family
    only), "unknown" (a label unknown or unrecognised), "cross" (both families known, none shared)."""
    if family_jump(ref, other):   # "melodic dubstep" vs "melodic house" share a word, not a scene
        return "cross"
    near = genre_near(ref, other)
    if near is True:
        return "scene"
    a, b = genre_families(ref), genre_families(other)
    if a and b:
        return "family" if _families_touch(a, b) else "cross"
    return "unknown"


# Era: a set holds its decade the way it holds its genre. Aqua "Barbie Girl"
# (1997) -> Bicep "Glue" (2017) shares tempo and even "dance", but breaks the
# vibe (user). Labels come from the model ("1990s", "90s", "late 90s", "1997").
_DECADE_RE = re.compile(r"\b(1[89]\d0|20[0-3]0)s?\b|\b(\d0)'?s\b|\b(1[89]\d\d|20[0-3]\d)\b")


def decade_of(label) -> Optional[int]:
    """1990 for "1990s" / "90s" / "late 90's" / "1997"; None if no decade in it."""
    m = _DECADE_RE.search(str(label or "").lower())
    if not m:
        return None
    if m.group(1):
        return int(m.group(1))
    if m.group(2):
        d = int(m.group(2))
        return (1900 if d >= 30 else 2000) + d
    return int(m.group(3)) // 10 * 10


MAX_ERA_GAP = 1  # decades: 90s -> 2000s is a neighbour, 90s -> 2010s is a jump


def era_gap(era_a, era_b) -> Optional[int]:
    """Decades between two era labels; None if either is unknown."""
    a, b = decade_of(era_a), decade_of(era_b)
    return None if a is None or b is None else abs(a - b) // 10


GENRE_JUMP_PENALTY = 0.6  # unrelated-family jump knocks the transition score down hard


def genre_score(genre_a, genre_b) -> float:
    """1.0 when either genre is unknown or they share a family; penalized on a clean jump."""
    return GENRE_JUMP_PENALTY if family_jump(genre_a, genre_b) else 1.0


# Softer than a genre jump: a 90s house classic into a 2020s house record is a
# stock DJ move, so a big era gap alone only trims the score (0.85 → a 90-point
# pair lands ~77, still playable but no longer tops a same-era option). Stacked
# with a family jump (0.6 x 0.85 ≈ 0.51) it sinks the pair, which is the
# Aqua 1997 -> Bicep 2017 / house -> metal case the user flagged.
ERA_JUMP_PENALTY = 0.85


def era_score(era_a, era_b) -> float:
    """1.0 when either era is unknown or within MAX_ERA_GAP decades; penalized beyond."""
    gap = era_gap(era_a, era_b)
    return ERA_JUMP_PENALTY if gap is not None and gap > MAX_ERA_GAP else 1.0


def vibe_score(genre_a=None, genre_b=None, era_a=None, era_b=None) -> float:
    """Combined genre x era multiplier for transition scoring; 1.0 when all unknown."""
    return genre_score(genre_a, genre_b) * era_score(era_a, era_b)
