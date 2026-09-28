"""One guard around every yt-dlp call, so a YouTube bot check heals itself.

"Sign in to confirm you're not a bot" (and HTTP 429) is YouTube flagging this
IP for request volume. Hammering on makes it stick; going quiet lets it lift.
So:

1. A blocked call is retried once per alternate player client (cheap, often
   enough), with cookies only if YTDLP_COOKIES_FILE points at an exported
   cookies.txt (never read from a browser by default).
2. Still blocked: the circuit opens. For a cooldown (2, 4, 8 ... up to 60 min)
   every call fails fast with Cooling, without touching YouTube. The state is a
   small JSON file, so the app server, download worker and CLI all back off together.
3. After the cooldown the next call probes; success closes the circuit and
   resets the backoff. Nothing has to be restarted.

Callers: `call(fn)` where fn(extra_opts) runs yt-dlp with those options merged in.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional, TypeVar

from app.music_brain.config import CACHE_DIR

STATE_PATH = CACHE_DIR / "yt_guard.json"
BASE_COOLDOWN_S = 120.0
MAX_COOLDOWN_S = 3600.0
CLIENTS = (None, ["android", "ios"], ["tv", "web_safari"], ["mweb"])
_BOT_MARKERS = ("confirm you’re not a bot", "confirm you're not a bot", "sign in to confirm",
                "http error 429", "too many requests")
_lock = threading.Lock()
T = TypeVar("T")


class Cooling(RuntimeError):
    """YouTube is being left alone until `until` (epoch s)."""

    def __init__(self, until: float, reason: str = ""):
        self.until = until
        at = time.strftime("%H:%M", time.localtime(until))
        super().__init__(f"YouTube asked for a bot check; pausing YouTube requests until {at} so it clears "
                         f"(retries automatically){': ' + reason if reason else ''}")


def is_bot_check(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _BOT_MARKERS)


def _read() -> dict:
    try:
        return json.loads(Path(STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(state: dict) -> None:
    p = Path(STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(p)


def status() -> dict:
    s = _read()
    until = float(s.get("until") or 0)
    return {"cooling": until > time.time(), "until": until or None, "strikes": int(s.get("strikes") or 0),
            "last_error": s.get("last_error")}


def check() -> None:
    """Raise Cooling while the circuit is open (callers that want to fail fast)."""
    until = float(_read().get("until") or 0)
    if until > time.time():
        raise Cooling(until)


def _trip(exc: BaseException) -> Cooling:
    with _lock:
        s = _read()
        strikes = int(s.get("strikes") or 0) + 1
        until = time.time() + min(MAX_COOLDOWN_S, BASE_COOLDOWN_S * 2 ** (strikes - 1))
        _write({"strikes": strikes, "until": until, "last_error": str(exc)[:300], "at": time.time()})
    return Cooling(until, "")


def _heal() -> None:
    with _lock:
        if _read().get("strikes"):
            _write({"strikes": 0, "until": 0, "healed_at": time.time()})
            _log("YouTube answering again: pause cleared")


class _Quiet:
    """yt-dlp logger for guarded attempts: its own ERROR lines are dropped (the guard
    reports once per episode instead of four copies per request); other output kept."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        if not any(m in str(msg).lower() for m in _BOT_MARKERS):
            import sys

            print(msg, file=sys.stderr)


def _log(msg: str) -> None:
    import sys

    print(f"[yt_guard] {msg}", file=sys.stderr, flush=True)


def call(fn: Callable[[dict], T]) -> T:
    """Run fn(extra_ydl_opts) under the guard. Non-bot errors pass through unchanged."""
    check()
    cookies = os.environ.get("YTDLP_COOKIES_FILE")
    last: Optional[BaseException] = None
    for clients in CLIENTS:
        extra: dict = {"logger": _Quiet()}
        if clients:
            extra["extractor_args"] = {"youtube": {"player_client": clients}}
        if cookies and clients is not None:           # cookies only once the plain request was refused
            extra["cookiefile"] = os.path.expanduser(cookies)
        try:
            out = fn(extra)
        except Exception as exc:
            if not is_bot_check(exc):
                raise
            last = exc
            continue
        if clients is not None:
            _log(f"bot check passed with {'cookies + ' if 'cookiefile' in extra else ''}{'/'.join(clients)} client")
        _heal()
        return out
    c = _trip(last or RuntimeError("bot check"))
    _log(f"bot check on every client{' (cookies refused too: re-export them)' if cookies else ''}; "
         f"YouTube paused until {time.strftime('%H:%M:%S', time.localtime(c.until))}, then retried automatically")
    raise c


def wait_and_call(fn: Callable[[dict], T], max_wait_s: float = MAX_COOLDOWN_S,
                  on_wait: Callable[[float], None] = lambda until: None,
                  sleep: Callable[[float], None] = time.sleep) -> T:
    """call(), but ride out cooldowns (for background jobs): wait until the circuit
    closes, then retry, up to max_wait_s in total."""
    deadline = time.time() + max_wait_s
    while True:
        try:
            return call(fn)
        except Cooling as c:
            if c.until > deadline:
                raise
            on_wait(c.until)
            sleep(max(1.0, c.until - time.time()))
