"""Every suggestion the server drops is logged as a `suggest_reject` event with its reason.

Session 2026-09-30_102327 sat in HOLD LOOP after 17 ok suggest calls and the log
could not say which filter ate the picks.
"""
import json

import app.ui.autopilot_service as svc
import app.ui.session_log as sl


def _events():
    out = []
    for p in sl.SESSIONS_DIR.glob("*/events.jsonl"):
        out += [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return [e for e in out if e.get("kind") == "suggest_reject"]


def test_note_rejects_logs_one_event_per_pick():
    svc.note_rejects([{"artist": "A", "title": "One"}, {"artist": "B", "title": "Two"}, "junk"],
                     "genre jump (2)")
    ev = _events()
    assert [e["song"] for e in ev] == ["A - One", "B - Two"]
    assert all(e["reason"] == "genre jump (2)" for e in ev)


def test_note_rejects_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(sl, "log", boom)
    svc.note_rejects([{"artist": "A", "title": "One"}], "x")  # no exception


def test_filter_logs_the_reason_it_rejected():
    data = {"current_genre": "house", "current_era": "2020s", "suggestions": [
        {"artist": "Far", "title": "Away", "genre": "death metal", "era": "2020s", "genre_hop": 3}]}
    svc._filter_suggestions(data, [])
    ev = _events()
    assert ev and ev[0]["song"] == "Far - Away"
    assert ev[0]["reason"].startswith("genre jump")
