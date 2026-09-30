"""Vectors for "the Punjabi profile off leaves learned moves as they were".

app/tests/fixtures/learned_off_golden.json was made by running compute(GLOBAL_STORE) against
main at 1a2a54c, before scene-tagged sets existed (tempo gaps and key scores learned from every
set). test_learned_scene.py recomputes it on this tree for GLOBAL_STORE and for SCENE_STORE
(the same store plus observations from a Punjabi-tagged set) and compares: with no profile,
a tagged set's sightings must change nothing.
"""
import copy

from app.music_brain import techniques as tq

TAGGED = "aLWCv6MGyho"          # scene_profile.SET_SCENES: punjabi


def _obs(kind, set_id, gap, key, at=60.0, **detail):
    return {"kind": kind, "set_id": set_id, "at": at, "track_a": "A", "track_b": "B",
            "tempo_gap": gap, "key_score": key, "detail": dict(detail)}


def _entry(kind, obs, stems, live=True):
    gaps = [o["tempo_gap"] for o in obs if o["tempo_gap"] is not None]
    keys = [o["key_score"] for o in obs if o["key_score"] is not None]
    return {"kind": kind, "what": kind, "stems": stems, "live": live, "observations": obs, "count": len(obs),
            "ai_rules": [], "tempo_gap_max": max(gaps, default=None), "key_score_min": min(keys, default=None)}


GLOBAL_STORE = {
    "stem_intro": _entry("stem_intro", [_obs("stem_intro", "s1", 0.02, 0.9), _obs("stem_intro", "s2", 0.05, 0.0),
                                        _obs("stem_intro", "s2", 0.0, 1.0, at=200.0)], True),
    "bass_swap": _entry("bass_swap", [_obs("bass_swap", "s1", 0.01, 1.0)], False),
    "acapella_over": _entry("acapella_over", [_obs("acapella_over", "s1", 0.0, 0.9, vocal_from="B")], True),
}

# + a Punjabi-tagged set: 4 key-clash stem intros near in tempo, one at a 32 % gap, one bass swap on a clash
SCENE_STORE = copy.deepcopy(GLOBAL_STORE)
SCENE_STORE["stem_intro"]["observations"] += [
    _obs("stem_intro", TAGGED, 0.0196, 0.0, at=932.0), _obs("stem_intro", TAGGED, 0.0472, 0.0, at=1296.0),
    _obs("stem_intro", TAGGED, 0.0257, 0.0, at=712.0), _obs("stem_intro", TAGGED, 0.3245, 0.0, at=3115.0),
    _obs("stem_intro", TAGGED, 0.178, 0.9, at=1432.0)]
SCENE_STORE["bass_swap"]["observations"] += [_obs("bass_swap", TAGGED, 0.03, 0.0)]
SCENE_STORE["loop_extend"] = _entry("loop_extend", [_obs("loop_extend", TAGGED, 0.0441, 0.0)], False, live=False)
for _e in SCENE_STORE.values():       # stored fields as a merge on main would have written them
    _e.update({k: v for k, v in _entry(_e["kind"], _e["observations"], _e["stems"], _e["live"]).items()
               if k in ("count", "tempo_gap_max", "key_score_min")})

BPMS = [(124.0, 124.0), (124.0, 126.5), (124.0, 130.0), (124.0, 164.0), (88.0, 176.0), (100.0, 150.0)]
KEYS = [("8A", "8A"), ("8A", "9A"), ("8A", "2A"), (None, None)]
STEMS = [(True, True), (False, False)]


def compute(store):
    """[{pair, rank: [{name, fits, reasons}], pick}] over the grid, profile off (today's call shape)."""
    out = []
    for bpm_a, bpm_b in BPMS:
        for key_a, key_b in KEYS:
            for sa, sb in STEMS:
                f = tq.PairFeatures(bpm_a, bpm_b, key_a, key_b, stems_a=sa, stems_b=sb)
                ranked = tq.rank(f, learned=copy.deepcopy(store))
                ks = tq.camelot_score(key_a, key_b) if key_a and key_b else None
                pick = tq.learned_pick(ranked, copy.deepcopy(store), key_score=ks)
                out.append({"pair": [bpm_a, bpm_b, key_a, key_b, sa, sb],
                            "rank": [{"name": r["name"], "fits": r["fits"], "reasons": r["reasons"]}
                                     for r in ranked if r["name"].startswith("learned:")],
                            "pick": pick})
    return out
