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
                   "downtempo", "ambient", "future bass", "electro", "idm", "jungle", "dance"),
    "hiphop": ("hip-hop", "hip hop", "rap", "trap", "drill", "grime"),
    "rnb": ("r&b", "rnb", "soul"),
    "latin": ("reggaeton", "latin", "dembow", "cumbia", "bachata", "salsa", "urbano"),
    "afro": ("afrobeat", "afro", "amapiano", "afropop"),
    "rock": ("rock", "punk", "metal", "grunge"),
    "pop": ("pop",),
    "country": ("country", "folk", "americana"),
    "jazz": ("jazz", "funk", "disco"),
}


def genre_families(label) -> set:
    g = str(label or "").lower()
    return {fam for fam, keys in GENRE_FAMILIES.items() if any(k in g for k in keys)}


def family_jump(genre_a, genre_b) -> bool:
    """True when both labels are known and share no family (e.g. melodic house -> metal)."""
    a, b = genre_families(genre_a), genre_families(genre_b)
    return bool(a and b) and not (a & b)


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
