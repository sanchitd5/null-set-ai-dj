"""POST /api/match with no_cuts (the autopilot's request): the matcher is asked to
leave Hard Cut / Quick Cut out. Called directly (no TestClient), analysis faked."""
import app.ui.server as srv


def test_match_request_passes_no_cuts_to_the_matcher(monkeypatch):
    seen = []

    class FakeMatcher:
        def match(self, a, b, top_n=3, **kw):
            seen.append(kw.get("no_cuts"))
            return []
    monkeypatch.setattr(srv, "_matcher", FakeMatcher())
    monkeypatch.setattr(srv, "_track_path", lambda tid: f"/nowhere/{tid}.mp3")
    monkeypatch.setattr(srv, "analyze_track", lambda path: type("T", (), {"bpm": 128.0})())
    monkeypatch.setattr(srv, "_cached_vocal_regions", lambda tid: None)
    monkeypatch.setattr(srv, "_pair_vibe", lambda a, b: {})
    for body, want in (({}, False), ({"no_cuts": True}, True)):
        out = srv.post_match(srv.MatchRequest(track_a_id="a", track_b_id="b", top_n=1, **body))
        assert out["candidates"] == []
        assert seen[-1] is want
