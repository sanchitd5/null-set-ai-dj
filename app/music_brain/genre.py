"""Genre-family distance: shared by autopilot's suggestion filter (app/ui/
autopilot_service.py) and RecipeMatcher's transition scoring, so a manual
/api/match call gets the same "no unrelated-genre jump" sense autopilot
already enforces.

Genre labels themselves are never audio-derived (TrackAnalysis stays pure
librosa/DSP); a caller resolves a label from track identity/metadata and
passes it in.
"""

from __future__ import annotations

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


GENRE_JUMP_PENALTY = 0.6  # unrelated-family jump knocks the transition score down hard


def genre_score(genre_a, genre_b) -> float:
    """1.0 when either genre is unknown or they share a family; penalized on a clean jump."""
    return GENRE_JUMP_PENALTY if family_jump(genre_a, genre_b) else 1.0
