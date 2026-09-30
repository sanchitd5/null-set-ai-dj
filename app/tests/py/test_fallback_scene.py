"""Session 2026-09-30_191133 (owner, live): "Pretty girl walk to Four Tet - Two Thousand and
Seventeen; bad choice". The deadline's library fallback got genre="" for a FOLLOW SET song and
filtered nothing: Big Boss Vette (hip hop) -> Four Tet (Fred V Bootleg) (electronic). Labels
below are the persisted genre_labels.json ones for these songs."""
import json
from pathlib import Path

LABELS = {"pretty girls walk": "hip hop", "big dawgs": "hip hop", "naughty girl": "r&b",
          "two thousand and seventeen (fred v bootleg)": "electronic", "flight fm": "house",
          "cheques": "punjabi"}


def test_scene_relation():
    from app.music_brain.analysis.genre import scene_relation
    assert scene_relation("hip hop", "hip hop") == "scene"
    assert scene_relation("hip hop", "trap") == "family"
    assert scene_relation("hip hop", "electronic") == "cross"
    assert scene_relation("hip hop", "") == "unknown"
    assert scene_relation("", "house") == "unknown"


def _library(monkeypatch, srv, songs):
    from app.music_brain.analysis.analyzer import KeyEstimate
    tracks = {tid: Path(f"/x/{name}.mp3") for tid, name in songs.items()}
    monkeypatch.setattr(srv, "_tracks", tracks)
    monkeypatch.setattr(srv, "_track_names", dict(songs))
    monkeypatch.setattr(srv, "_suggested_genres", {srv._genre_key(k): v for k, v in LABELS.items()})
    monkeypatch.setattr(srv, "_suggested_eras", {})
    fake = type("T", (), {"bpm": 97.0, "duration": 200.0, "key": KeyEstimate("8A", "", False, 0.9)})()
    monkeypatch.setattr(srv, "analyze_track", lambda p: fake)


SONGS = {"pgw": "Big Boss Vette - Pretty Girls Walk", "ft": "Four Tet - Two Thousand and Seventeen (Fred V Bootleg)",
         "bd": "Hanumankind & Kalmi - Big Dawgs", "un": "Nobody - Unlabelled Song"}


def test_library_fallback_uses_the_playing_songs_stored_label(monkeypatch):
    import app.ui.server as srv
    _library(monkeypatch, srv, SONGS)
    res = srv.get_library_lockable(bpm=97.0, exclude="pgw", genre="", a_id="pgw")
    assert res["ref_genre"] == "hip hop"
    assert [t["name"] for t in res["tracks"]] == [SONGS["bd"]]            # Four Tet is not in-scene
    # outside the scene: unknown genre before the cross-family jump, each flagged
    assert [(t["track_id"], t["scene_rel"]) for t in res["outside"]] == [("un", "unknown"), ("ft", "cross")]


def test_library_fallback_anchor_wins_over_the_playing_label(monkeypatch):
    import app.ui.server as srv
    _library(monkeypatch, srv, SONGS)
    # Four Tet playing (a mistake), the set's anchor is hip hop: the pick back is Big Dawgs
    res = srv.get_library_lockable(bpm=97.0, exclude="ft,pgw", a_id="ft", anchor="hip hop")
    assert res["ref_genre"] == "hip hop" and [t["track_id"] for t in res["tracks"]] == ["bd"]


def test_library_fallback_old_query_unchanged(monkeypatch):
    import app.ui.server as srv
    _library(monkeypatch, srv, SONGS)
    res = srv.get_library_lockable(bpm=97.0, genre="electronic")
    assert [t["track_id"] for t in res["tracks"]] == ["ft"]


def test_vet_scene_anchor_refuses_the_mistaken_genre():
    from app.ui.services import booking_vet as bv
    cands = [{"name": "Joy Orbison - flight fm", "genre": "house"},
             {"name": "Hanumankind & Kalmi - Big Dawgs", "genre": "hip hop"},
             {"name": "Nobody - Unlabelled", "genre": None}]
    kw = dict(a_genre="electronic", history=[], vetoes=[])
    free = bv.vet("Four Tet - Two Thousand and Seventeen", cands, **kw)
    assert [r["ok"] for r in free] == [True, True, True]
    rec = bv.vet("Four Tet - Two Thousand and Seventeen", cands, anchor_genre="hip hop", **kw)
    assert [(r["ok"], r["gate"]) for r in rec] == [(False, "scene_anchor"), (True, None), (True, None)]
    assert [r["scene_rel"] for r in rec] == ["cross", "scene", "unknown"]
    assert rec[1]["families"] == ["hiphop"] and rec[2]["families"] == []


def test_atlas_backup_rows_carry_scene(monkeypatch):
    import app.ui.server as srv
    from app.ui.services import atlas_api
    monkeypatch.setattr(srv, "_track_vibe", lambda tid: {"genre": {"pgw": "hip hop", "ft": "electronic", "bd": "hip hop"}.get(tid), "era": None})
    monkeypatch.setattr(srv, "_vibe_by_name", lambda n: {"genre": None, "era": None})
    rows = [{"b": "ft", "b_name": SONGS["ft"]}, {"b": "bd", "b_name": SONGS["bd"]}, {"b": "un", "b_name": SONGS["un"]}]
    assert atlas_api._mark_scene("pgw", "", rows) == "hip hop"
    assert [r["scene_rel"] for r in rows] == ["cross", "scene", "unknown"]
    atlas_api._mark_scene("ft", "hip hop", rows)                       # anchor over A's label
    assert [r["scene_rel"] for r in rows] == ["cross", "scene", "unknown"]


def test_suggest_current_genre_from_the_stored_label(monkeypatch):
    """19:52:33: the model called the playing Four Tet "deep house" over its stored label."""
    from app.ui.services import autopilot_service as svc
    seen = []
    reply = {"current_genre": "deep house", "suggestions": [
        {"artist": "Bicep", "title": "Glue", "genre": "breakbeat", "occasion_fit": 8, "search_query": "q"}]}
    monkeypatch.setattr(svc, "chat_raw", lambda s, u, **k: seen.append(u) or json.dumps(reply))
    monkeypatch.setattr(svc, "_verify_picks", lambda picks: (picks, []))
    meta = {}
    svc.suggest_next_tracks("Two Thousand and Seventeen", "Four Tet", 124.0, "8A", 200.0, 0.6, "", [],
                            meta=meta, genre="electronic")
    assert "stored genre label: electronic" in seen[0]
    assert meta["current_genre"] == "electronic" and meta["model_genre"] == "deep house"


def test_suggest_scene_anchor_recovers(monkeypatch):
    from app.ui.services import autopilot_service as svc
    seen = []
    reply = {"current_genre": "deep house", "suggestions": [
        {"artist": "Joy Orbison", "title": "flight fm", "genre": "house", "occasion_fit": 8, "search_query": "q1"},
        {"artist": "Hanumankind", "title": "Big Dawgs", "genre": "hip hop", "occasion_fit": 8, "search_query": "q2"}]}
    monkeypatch.setattr(svc, "chat_raw", lambda s, u, **k: seen.append(u) or json.dumps(reply))
    monkeypatch.setattr(svc, "_verify_picks", lambda picks: (picks, []))
    meta = {}
    out = svc.suggest_next_tracks("Two Thousand and Seventeen", "Four Tet", 124.0, "8A", 200.0, 0.6, "", [],
                                  meta=meta, genre="electronic", scene_anchor="hip hop")
    assert "SCENE RECOVERY" in seen[0] and "back in hip hop" in seen[0]
    assert [s["title"] for s in out] == ["Big Dawgs"]
    assert meta["current_genre"] == "electronic" and meta["scene_anchor"] == "hip hop"


def test_fallback_fix_js_check():
    """app/tests/js/fallback_fix_check.js: prepared wait, pending pair reject, gate reason, deck events,
    scene anchor tracking + recovery ranking, atlas scene ranking, the session's replay rows."""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    check = Path(__file__).parents[1].joinpath("js", "fallback_fix_check.js")
    res = subprocess.run([node, str(check)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout
