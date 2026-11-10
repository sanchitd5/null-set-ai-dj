"""Booking vet: the vibe rules every candidate the console books must pass, whatever path found it.

Owner, live set 2026-09-30_154332: FOLLOW SET performed the studied pair TH;EN - Bodyrock ->
Masters At Work - Work (Skytech Remix) twice (16:09 and 16:37) while the song picks rejected Work as
"played in an earlier set". A studied pair is evidence, not an override of the vibe rules.

vet() is pure (node-free, file-free: the server hands it the labels, the earlier-set memory and the
vetoes). POST /api/autopilot/vet (server.py) wraps it; autopilot.js evaluateCandidate calls it for
every candidate and refuses what it refuses (the step log gets the gate and reason).

stored=True (a macro step, a FOLLOW SET song, a studied combo: a move replayed from memory) runs:
  repeat       the song already played this set (by clean title, not just track id: a re-download
               or an autopilot restart must not replay it)
  earlier_set  the song was heard in an earlier set (set_memory), the rule the picks already follow
  scene        a genre jump as the suggestion filter scores it (_filter_suggestions): both labels known and
               no shared family (genre.family_jump = hop 2 > MAX_GENRE_HOP), except the Punjabi profile's own
               pairs while it is active (scene_profile.in_scene_pair). "electronic" is split into sub-families
               (genre.ELECTRONIC_CLUSTERS): house <-> melodic techno are neighbours, kept; trance -> downtempo
               is a jump, refused
Every candidate (stored or not):
  veto         the owner vetoed the pair, or the song in this scene (atlas/vetoes.py)
  set_scene    set_genre given (the set's opening scene, pinned for the whole set): B's label is a
               family jump from it, maps to no family, or is missing (genre.family_jump; Punjabi profile pairs
               excepted) -> refused, by every
               path (owner 2026-10-11: "should not do a genre shift for the whole set", a Punjabi /
               Bollywood set drifted to EDM / R&B through atlas combos, FOLLOW SET and library fallback)
  scene_anchor while the console recovers from an off-scene mistake (anchor_genre given: the set's
               scene before a fallback / BAD PAIR brought A in from outside it), a candidate that
               continues A's mistaken genre (shares A's family, none of the anchor's) is refused:
               the song after the mistake goes back to the set's scene (owner, session 2026-09-30_191133)
Each result carries B's scene anchor tokens (genre.scene_keys, as `families`) and scene_rel against the anchor (else A's label), which the
console ranks and feeds its scene anchor with.
The energy step is not here: evaluateCandidate already runs energyStepOk on every candidate.
An unknown genre passes, as in the suggestion filter (the studied pair is the evidence). Era is not a
stored-move rule (owner's list: repeat + scene; the owner-liked Adapter - Catchaman -> PACS & Ruiz (BR) -
No Control is 2000s -> 2020s, owner_liked_pairs.json).
"""
from __future__ import annotations

from typing import Iterable, List, Optional

from app.music_brain.analysis import scene_profile as sp
from app.music_brain.analysis.genre import family_jump, genre_families, scene_keys, scene_relation
try:
    from app.music_brain.atlas import vetoes as vt
except ImportError:       # private atlas builder absent (see atlas_bridge): no owner vetoes to apply
    vt = None
from app.music_brain.pick_accept import accept_candidate
from app.ui.services.set_memory import _key as memory_key


def _k(name: str) -> str:
    return memory_key(str(name or ""))


def vet_one(a_name: str, b_name: str, *, a_genre: Optional[str] = None, a_era: Optional[str] = None,
            b_genre: Optional[str] = None, b_era: Optional[str] = None, history: Iterable[str] = (),
            earlier: Iterable[str] = (), vetoes: Iterable[dict] = (), stored: bool = False,
            punjabi_profile: str = "off", anchor_genre: Optional[str] = None,
            set_genre: Optional[str] = None) -> Optional[dict]:
    """None when B may follow A, else {gate, why}."""
    why = vt.blocked(vetoes, a_name, b_name, a_genre) if vt else None
    if why:
        return {"gate": "veto", "why": why}
    # a label that maps to no family ("indie": Beedi -> We Are The People) or no label at all ("CC" after
    # Oh Ho Ho Ho, session 2026-10-11_200026) is off-scene too: a pinned set books only songs known to fit
    # G23: the scene gate of the one acceptance function (no song state is given, so only the scene gate can refuse)
    ok, why = accept_candidate({"ids": [b_name], "genre": b_genre}, {"scene_genre": set_genre, "punjabi_profile": punjabi_profile})
    if not ok:
        return {"gate": "set_scene", "why": why}
    if anchor_genre and family_jump(anchor_genre, b_genre) and not family_jump(a_genre, b_genre) \
            and genre_families(a_genre):
        return {"gate": "scene_anchor", "why": f"continues the off-scene {a_genre} instead of going back to "
                                               f"the set's {anchor_genre} ({b_genre})"}
    if not stored:
        return None
    bk = _k(b_name)
    if bk and bk in {_k(h) for h in history or []}:
        return {"gate": "repeat", "why": "already played this set"}
    if bk and bk in {_k(h) for h in earlier or []}:
        return {"gate": "earlier_set", "why": "played in an earlier set"}
    active = sp.selection_active(punjabi_profile, a_genre)
    in_profile = active and sp.in_scene_pair(a_genre, b_genre)
    if not in_profile and family_jump(a_genre, b_genre):
        return {"gate": "scene", "why": f"genre jump ({a_genre} -> {b_genre})"}
    return None


def vet(a_name: str, cands: List[dict], **kw) -> List[dict]:
    """cands: [{track_id, name, genre, era, stored}] -> [{track_id, name, ok, gate, why}] in order."""
    out = []
    for c in cands or []:
        r = vet_one(a_name, c.get("name") or "", b_genre=c.get("genre"), b_era=c.get("era"),
                    **dict(kw, stored=bool(c.get("stored", kw.get("stored", False)))))
        # scene_clash: a CLEAR mismatch (both labels known, no shared family): the console's mashup
        # transition refuses only that (render/mashup.py scene_gate, the same narrow rule)
        clash = bool(family_jump(kw.get("a_genre"), c.get("genre")))
        ref = kw.get("anchor_genre") or kw.get("a_genre")
        out.append({"track_id": c.get("track_id"), "name": c.get("name"), "ok": r is None,
                    "gate": r["gate"] if r else None, "why": r["why"] if r else None, "scene_clash": clash,
                    "families": scene_keys(c.get("genre")),
                    "scene_rel": scene_relation(ref, c.get("genre")) if ref else "unknown"})
    return out
