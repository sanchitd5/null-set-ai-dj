"""Fixed decision vectors for the Punjabi scene profile's "off == today" proof.

`python3 app/tests/py/profile_off_vectors.py` prints the vectors' results as JSON for
whatever `app` package is first on sys.path. The "python" hashes in
app/tests/fixtures/punjabi_off_golden.json were made by running this against the
main checkout BEFORE the profile existed:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<main checkout> python3 app/tests/py/profile_off_vectors.py
test_scene_profile.py recomputes it on this tree (mode off / default) and compares.
"""
from __future__ import annotations

import contextlib
import io
import itertools
import json

KEYS = [("8A", "8A"), ("8A", "3A"), ("8A", "9B")]
BPMS = [(88.0, 88.0), (88.0, 176.0), (88.0, 130.0), (128.0, 124.0)]
GENRES = [(None, None), ("punjabi", "bhangra"), ("house", "house")]
ERAS = [(None, None), ("1980s", "2020s")]

SUGGEST_CASES = [
    {"current_genre": g, "current_era": e, "steering": "stay", "suggestions": [
        {"artist": "A", "title": "One", "genre": "bhangra", "era": "1990s"},
        {"artist": "B", "title": "Two", "genre": "punjabi hip hop", "era": "2020s"},
        {"artist": "C", "title": "Three", "genre": "bollywood", "era": "2010s"},
        {"artist": "D", "title": "Four", "genre": "deep house", "era": "2020s"},
        {"artist": "E", "title": "Five", "genre": "punjabi", "era": "1980s"},
    ]}
    for g, e in [("punjabi", "1980s"), ("bhangra", "2020s"), ("house", "2020s"), ("", "")]
]


def _track(camelot, bpm):
    from app.music_brain.analysis.analyzer import KeyEstimate, StructureSection, TrackAnalysis
    return TrackAnalysis(
        path="fake.mp3", duration=240.0, bpm=bpm,
        phrase_boundaries_8bar=[0.0, 15.0, 30.0, 45.0, 200.0, 215.0],
        key=KeyEstimate(camelot=camelot, key_name="", is_major=camelot.endswith("B"), confidence=0.9),
        sections=[StructureSection("intro", 0.0, 20.0, 0.2), StructureSection("verse", 20.0, 180.0, 0.6),
                  StructureSection("breakdown", 180.0, 210.0, 0.2), StructureSection("outro", 210.0, 240.0, 0.3)],
    )


def compute(match_kw=None, filter_kw=None) -> dict:
    """match_kw / filter_kw: extra kwargs (e.g. profile=None / punjabi_profile="off")."""
    from app.music_brain.knowledge_parser import KnowledgeParser
    from app.music_brain.recipe_matcher import RecipeMatcher
    from app.ui.services import autopilot_service as svc

    m = RecipeMatcher(KnowledgeParser())
    match = []
    for (ka, kb), (ba, bb), (ga, gb), (ea, eb), nc in itertools.product(KEYS, BPMS, GENRES, ERAS, (False, True)):
        cands = m.match(_track(ka, ba), _track(kb, bb), top_n=100, genre_a=ga, genre_b=gb, era_a=ea, era_b=eb,
                        no_cuts=nc, **(match_kw or {}))
        match.append([[c.recipe.name, round(c.score, 6), c.a_time, c.b_time, round(c.camelot_score, 6),
                       round(c.bpm_score, 6)] for c in cands])
    suggest = []
    with contextlib.redirect_stdout(io.StringIO()):
        for case in SUGGEST_CASES:
            data = json.loads(json.dumps(case))
            kept = svc._filter_suggestions(data, [], **(filter_kw or {}))
            suggest.append({"kept": [s["title"] for s in kept],
                            "rejected": {s["title"]: s.get("rejected_reason") for s in data["suggestions"]}})
    return {"match": match, "suggest": suggest}


if __name__ == "__main__":
    print(json.dumps(compute()))
