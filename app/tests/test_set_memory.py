from app.ui.set_memory import SetMemory


def test_memory_offers_only_earlier_sets(tmp_path):
    m = SetMemory(tmp_path / "mem.json")
    m.record(["Charli xcx - Guess", "100 gecs - money machine"])        # set 1
    again = SetMemory(tmp_path / "mem.json")                            # survives restart
    assert set(again.earlier_sets(["Charli xcx - Guess"])) == {"100 gecs - money machine"}
    assert again.earlier_sets(["Charli xcx - Guess (official lyric video)", "100 gecs - Money Machine"]) == []


def test_earlier_set_songs_dropped_when_fresh_exists(monkeypatch):
    import app.ui.autopilot_service as ap
    fake = '{"suggestions":[{"artist":"Lane 8","title":"Little By Little"},{"artist":"Four Tet","title":"Baby"}]}'
    monkeypatch.setattr(ap, "chat_raw", lambda *a, **k: fake)
    out = ap.suggest_next_tracks("Marea", "Fred again..", 123, "4A", 240, 0.5, "", [],
                                 earlier_sets=["Lane 8 - Little By Little"])
    assert [s["title"] for s in out] == ["Baby"]
    only_old = '{"suggestions":[{"artist":"Lane 8","title":"Little By Little"}]}'
    monkeypatch.setattr(ap, "chat_raw", lambda *a, **k: only_old)
    out = ap.suggest_next_tracks("Marea", "Fred again..", 123, "4A", 240, 0.5, "", [],
                                 earlier_sets=["Lane 8 - Little By Little"])
    assert [s["title"] for s in out] == ["Little By Little"]      # never empty


def test_lead_to_uses_focused_prompt_and_skips_continuity_filters(monkeypatch):
    import app.ui.autopilot_service as ap
    seen = {}
    def fake(system, user, **kw):
        seen["system"], seen["user"], seen["max_tokens"] = system, user, kw.get("max_tokens")
        return ('{"current_profile":{"energy":6,"mood":"bittersweet","tempo_feel":"driving"},"suggestions":['
                '{"artist":"Nucleya","title":"Bass Rani","expected_key":"9B","occasion_fit":2,'
                '"track_profile":{"energy":9,"mood":"euphoric","tempo_feel":"driving"}}]}')
    monkeypatch.setattr(ap, "chat_raw", fake)
    out = ap.suggest_next_tracks("Marea", "Fred again..", 123, "4A", 285, 0.5, "", [],
                                 lead_to="Diljit Dosanjh - Lover", lead_step=1, lead_steps=3, lead_bpm=123)
    assert seen["system"] == ap._LEAD_SYSTEM and "step 1 of 3" in seen["user"] and seen["max_tokens"] == 1100
    assert [s["title"] for s in out] == ["Bass Rani"]   # key / mood / fit filters don't fight the destination
    assert ap.lead_line("", 1, 3) == "" and "step 3 of 3" in ap.lead_line("Bollywood dance", 5, 3)
