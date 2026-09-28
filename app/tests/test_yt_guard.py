"""yt_guard: bot checks back off and heal on their own."""
import time

import pytest

from app.music_brain import yt_guard as g

BOT = RuntimeError("ERROR: [youtube] aqu4ezLQEUA: Sign in to confirm you’re not a bot. Use --cookies-from-browser")


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "STATE_PATH", tmp_path / "yt_guard.json")
    monkeypatch.delenv("YTDLP_COOKIES_FILE", raising=False)


def test_alternate_client_rescues_without_tripping():
    seen = []

    def fn(extra):
        seen.append(extra)
        if "extractor_args" not in extra:
            raise BOT
        return "ok"
    assert g.call(fn) == "ok" and seen[1]["extractor_args"]["youtube"]["player_client"] == ["android", "ios"]
    assert not g.status()["cooling"]


def test_blocked_everywhere_trips_then_fails_fast_then_heals(monkeypatch):
    calls = []

    def blocked(extra):
        calls.append(1)
        raise BOT
    with pytest.raises(g.Cooling) as e:
        g.call(blocked)
    assert len(calls) == len(g.CLIENTS) and g.status()["cooling"] and g.status()["strikes"] == 1
    assert e.value.until - time.time() == pytest.approx(g.BASE_COOLDOWN_S, abs=5)
    n = len(calls)
    with pytest.raises(g.Cooling):
        g.call(blocked)                                   # cooling: YouTube not touched at all
    assert len(calls) == n
    now = time.time()
    monkeypatch.setattr(g.time, "time", lambda: now + g.BASE_COOLDOWN_S + 1)
    with pytest.raises(g.Cooling) as e2:
        g.call(blocked)                                   # probe, still blocked: backoff doubles
    assert g.status()["strikes"] == 2 and e2.value.until - (now + g.BASE_COOLDOWN_S + 1) == pytest.approx(2 * g.BASE_COOLDOWN_S, abs=5)
    monkeypatch.setattr(g.time, "time", lambda: now + 10 * g.BASE_COOLDOWN_S)
    assert g.call(lambda extra: "back") == "back"         # probe succeeds: healed
    assert g.status() == {"cooling": False, "until": None, "strikes": 0, "last_error": None}


def test_backoff_is_capped():
    g._write({"strikes": 30, "until": 0})
    with pytest.raises(g.Cooling) as e:
        g.call(lambda extra: (_ for _ in ()).throw(BOT))
    assert e.value.until - time.time() <= g.MAX_COOLDOWN_S + 1


def test_other_errors_pass_through_untouched():
    with pytest.raises(ValueError):
        g.call(lambda extra: (_ for _ in ()).throw(ValueError("Video unavailable")))
    assert not g.status()["cooling"]


def test_cookies_only_after_a_refusal(monkeypatch):
    monkeypatch.setenv("YTDLP_COOKIES_FILE", "~/c.txt")
    seen = []

    def fn(extra):
        seen.append(extra)
        if len(seen) == 1:
            raise BOT
        return 1
    g.call(fn)
    assert "cookiefile" not in seen[0] and seen[1]["cookiefile"].endswith("c.txt")


def test_background_jobs_wait_out_the_cooldown():
    g._write({"strikes": 1, "until": time.time() + 30})
    waited, slept = [], []
    assert g.wait_and_call(lambda extra: "done", on_wait=waited.append,
                           sleep=lambda s: (slept.append(s), g._write({"strikes": 1, "until": 0}))) == "done"
    assert waited and 25 <= slept[0] <= 31
    g._write({"strikes": 1, "until": time.time() + 7200})
    with pytest.raises(g.Cooling):
        g.wait_and_call(lambda extra: "x", max_wait_s=60, sleep=lambda s: None)   # too long to wait: report


def test_download_service_optional_lookups_go_quiet_while_cooling(monkeypatch):
    from app.ui import download_service as ds
    if ds._yt_dlp is None:
        pytest.skip("yt-dlp not installed")
    g._write({"strikes": 1, "until": time.time() + 600})
    monkeypatch.setattr(ds._yt_dlp, "YoutubeDL", lambda *a, **k: (_ for _ in ()).throw(AssertionError("YouTube touched")))
    assert ds.verify_song("Fred again..", "Delilah") is None
    assert ds.song_views("Fred again.. - Delilah") is None
    with pytest.raises(g.Cooling):
        ds.search_songs("Fred again.. Delilah")


def test_bot_errors_are_reported_once_not_per_attempt(capsys):
    def blocked(extra):
        extra["logger"].error(str(BOT))                   # what yt-dlp itself would print
        raise BOT
    with pytest.raises(g.Cooling):
        g.call(blocked)
    err = capsys.readouterr().err
    assert "Sign in to confirm" not in err and err.count("[yt_guard]") == 1 and "paused until" in err
