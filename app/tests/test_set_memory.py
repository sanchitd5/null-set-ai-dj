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
