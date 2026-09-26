from app.ui.set_memory import SetMemory


def test_memory_offers_only_earlier_sets(tmp_path):
    m = SetMemory(tmp_path / "mem.json")
    m.record(["Charli xcx - Guess", "100 gecs - money machine"])        # set 1
    again = SetMemory(tmp_path / "mem.json")                            # survives restart
    assert set(again.earlier_sets(["Charli xcx - Guess"])) == {"100 gecs - money machine"}
    assert again.earlier_sets(["Charli xcx - Guess (official lyric video)", "100 gecs - Money Machine"]) == []
