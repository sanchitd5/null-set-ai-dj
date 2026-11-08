"""GET /api/tracks/{id}/analysis: a stored record is served without a heavy slot; only a miss (full analysis) takes one."""
import asyncio

from app.ui import server
from app.ui.services import load_shed


class _Rec:
    def to_dict(self):
        return {"bpm": 120.0}


def test_hit_skips_heavy_slot_miss_takes_one(monkeypatch):
    monkeypatch.setattr(server, "_track_path", lambda tid: f"/tmp/{tid}.flac")
    monkeypatch.setattr(server.album_art, "art_url", lambda tid: None)
    from app.music_brain.analysis import analyzer

    seen = []
    monkeypatch.setattr(analyzer, "cached_analysis", lambda p: _Rec() if "hit" in str(p) else None)
    monkeypatch.setattr(server, "analyze_track", lambda p: (seen.append(load_shed.stats["active"]), _Rec())[1])

    before = load_shed.stats["peak"]
    load_shed.stats["peak"] = 0
    try:
        hit = asyncio.run(server.get_analysis("hit0000000000000"))
        assert hit == {"bpm": 120.0, "art_url": None}
        assert load_shed.stats["peak"] == 0, "a cache hit must not take a heavy slot"
        miss = asyncio.run(server.get_analysis("miss000000000000"))
        assert miss["bpm"] == 120.0
        assert seen == [1], "a miss runs the full analysis inside one heavy slot"
    finally:
        load_shed.stats["peak"] = max(before, load_shed.stats["peak"])
