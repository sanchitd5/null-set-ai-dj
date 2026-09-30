"""Real-song check of AI suggestions (app/ui/services/autopilot_service.py `_verify_picks`):
a few YouTube lookups at a time for the whole process, none while the bot-check
breaker has YouTube paused, and each song asked about once. YouTube is never
called here: `_verify_song` (the yt-dlp search) is replaced by a fake."""
import itertools
import json
import threading
import time

import app.ui.services.autopilot_service as svc
from app.music_brain import yt_guard


class FakeLookups:
    """Stands in for download_service.verify_song: records every lookup and the
    most lookups ever running at the same time."""

    def __init__(self, delay=0.0, verdict=True):
        self.delay, self.verdict = delay, verdict
        self.lock = threading.Lock()
        self.calls, self.running, self.peak = [], 0, 0

    def __call__(self, artist, title):
        with self.lock:
            self.calls.append(title)
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            time.sleep(self.delay)
            return self.verdict(title) if callable(self.verdict) else self.verdict
        finally:
            with self.lock:
                self.running -= 1


def _picks(*titles, artist="Beach House"):
    return [{"artist": artist, "title": t} for t in titles]


def _titles(picks):
    return [p["title"] for p in picks]


def _drain():
    """Let lookups that outlived a budget finish (they land in the cache)."""
    deadline = time.time() + 5
    while svc._verify_inflight and time.time() < deadline:
        time.sleep(0.02)
    assert not svc._verify_inflight


def test_lookups_capped_process_wide_under_parallel_suggest_calls(monkeypatch):
    fake = FakeLookups(delay=0.15)
    monkeypatch.setattr(svc, "_verify_song", fake)
    monkeypatch.setattr(svc, "VERIFY_TIMEOUT_S", 0.4)
    replies = itertools.count()

    def model(*a, **k):
        n = next(replies)
        return json.dumps({"current_genre": "dream pop", "suggestions": [
            {"artist": "Beach House", "title": f"Song {n}-{i}", "genre": "dream pop",
             "genre_hop": 0, "expected_bpm": 95} for i in range(4)]})
    monkeypatch.setattr(svc, "chat_raw", model)
    outs = []

    def suggest():
        outs.append(svc.suggest_next_tracks("Apocalypse", "Cigarettes After Sex", 96.0, "8A", 290.0, 0.3, "", []))
    threads = [threading.Thread(target=suggest) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    _drain()
    time.sleep(0.2)                                   # nothing queued starts after its budget ran out
    assert len(outs) == 8 and all(outs)               # unverified picks are kept: every call answers
    assert fake.peak == svc.VERIFY_MAX_INFLIGHT == 3  # the pool was saturated and never exceeded
    assert len(fake.calls) < 8 * 4                    # picks past the budget were never looked up
    assert fake.running == 0


def test_no_lookups_while_youtube_is_paused(monkeypatch):
    fake = FakeLookups()
    monkeypatch.setattr(svc, "_verify_song", fake)
    yt_guard._write({"strikes": 1, "until": time.time() + 600})
    assert yt_guard.status()["cooling"]
    t0 = time.time()
    real, invented = svc._verify_picks(_picks("Space Song", "Myth"))
    assert fake.calls == [] and svc._verify_inflight == set()
    assert _titles(real) == ["Space Song", "Myth"] and invented == []
    assert time.time() - t0 < 0.5                     # fails fast, no budget spent waiting


def test_queued_picks_skip_youtube_once_the_breaker_opens(monkeypatch):
    calls = []

    def bot_check(artist, title):                     # every lookup trips the breaker
        calls.append(title)
        with yt_guard._lock:                          # as yt_guard._trip writes it
            yt_guard._write({"strikes": 1, "until": time.time() + 600})
        time.sleep(0.05)
        return None
    monkeypatch.setattr(svc, "_verify_song", bot_check)
    real, invented = svc._verify_picks(_picks(*[f"Song {i}" for i in range(8)]))
    assert len(real) == 8 and invented == []
    assert len(calls) <= svc.VERIFY_MAX_INFLIGHT      # only lookups already running when it opened
    _drain()


def test_a_song_is_looked_up_once_hit_or_miss(monkeypatch):
    fake = FakeLookups(verdict=lambda title: title == "Space Song")
    monkeypatch.setattr(svc, "_verify_song", fake)
    real, invented = svc._verify_picks(_picks("Space Song", "Morrison"))
    assert _titles(real) == ["Space Song"] and _titles(invented) == ["Morrison"]
    # the next suggest call: same songs, other spelling of case / spaces
    real, invented = svc._verify_picks(_picks("Space  Song", "morrison", artist="beach house"))
    assert _titles(real) == ["Space  Song"] and _titles(invented) == ["morrison"]
    assert sorted(fake.calls) == ["Morrison", "Space Song"]


def test_unknown_answers_are_not_remembered(monkeypatch):
    fake = FakeLookups(verdict=None)                  # search failed: unknown, ask again next time
    monkeypatch.setattr(svc, "_verify_song", fake)
    svc._verify_picks(_picks("Space Song"))
    svc._verify_picks(_picks("Space Song"))
    assert fake.calls == ["Space Song", "Space Song"]


def test_a_late_answer_serves_the_next_call_and_is_not_asked_twice(monkeypatch):
    fake = FakeLookups(delay=0.3, verdict=False)
    monkeypatch.setattr(svc, "_verify_song", fake)
    monkeypatch.setattr(svc, "VERIFY_TIMEOUT_S", 0.05)
    real, invented = svc._verify_picks(_picks("Morrison"))
    assert _titles(real) == ["Morrison"] and invented == []      # too slow: unknown, kept
    real, invented = svc._verify_picks(_picks("Morrison"))       # still being looked up: no second request
    assert _titles(real) == ["Morrison"] and invented == []
    _drain()
    real, invented = svc._verify_picks(_picks("Morrison"))       # the late answer was remembered
    assert real == [] and _titles(invented) == ["Morrison"]
    assert fake.calls == ["Morrison"]


def test_remembered_answers_are_capped_and_expire(monkeypatch):
    fake = FakeLookups()
    monkeypatch.setattr(svc, "_verify_song", fake)
    monkeypatch.setattr(svc, "VERIFY_CACHE_MAX", 2)
    for title in ("One", "Two", "Three"):
        svc._verify_picks(_picks(title))
    assert len(svc._verify_cache) == 2
    svc._verify_picks(_picks("Three"))                # newest: still remembered
    svc._verify_picks(_picks("One"))                  # oldest: evicted, asked again
    assert fake.calls == ["One", "Two", "Three", "One"]
    monkeypatch.setattr(svc, "VERIFY_CACHE_TTL_S", 0.0)
    svc._verify_picks(_picks("One"))                  # expired
    assert fake.calls[-1] == "One" and len(fake.calls) == 5
