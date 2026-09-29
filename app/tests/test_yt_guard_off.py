import time

from app.music_brain import yt_guard


def test_guard_off_skips_cooldown_and_sends_cookies(tmp_path, monkeypatch):
    monkeypatch.setattr(yt_guard, "STATE_PATH", tmp_path / "yt_guard.json")
    yt_guard._write({"strikes": 3, "until": time.time() + 600})          # a cooldown is on
    monkeypatch.setenv("YTDLP_COOKIES_FILE", "~/cookies.txt")
    monkeypatch.setenv("YT_GUARD", "off")
    seen = []
    assert yt_guard.call(lambda extra: seen.append(extra) or "ok") == "ok"
    assert len(seen) == 1 and seen[0]["cookiefile"].endswith("cookies.txt")   # cookies on the first request
    assert yt_guard.status()["cooling"] is False and yt_guard.status()["disabled"] is True


def test_guard_on_still_cools(tmp_path, monkeypatch):
    monkeypatch.setattr(yt_guard, "STATE_PATH", tmp_path / "yt_guard.json")
    yt_guard._write({"strikes": 1, "until": time.time() + 600})
    monkeypatch.delenv("YT_GUARD", raising=False)
    try:
        yt_guard.call(lambda extra: "ok")
        raise AssertionError("expected Cooling")
    except yt_guard.Cooling:
        pass
