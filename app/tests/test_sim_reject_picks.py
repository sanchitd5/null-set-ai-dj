"""SIM_REJECT_PICKS=1 (app/sim/world.py invent_picks): every suggested song becomes one nobody can download."""
import json

from app.sim.world import invent_picks


def test_invent_picks_renames_every_suggestion():
    reply = json.dumps({"current_genre": "punjabi pop", "suggestions": [
        {"artist": "Diljit Dosanjh", "title": "Lover"}, {"artist": "Shubh", "title": "Cheques"}]})
    out = json.loads(invent_picks(reply))
    assert [(s["artist"], s["title"]) for s in out["suggestions"]] == [("Nobody", "Invented Song 0"), ("Nobody", "Invented Song 1")]
    assert out["current_genre"] == "punjabi pop"


def test_invent_picks_leaves_non_json_alone():
    assert invent_picks("not json") == "not json"
