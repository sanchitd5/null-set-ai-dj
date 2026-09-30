"""The brain's service entry: `Engine(host, ai_backend, config)`, plain constructor injection.

Everything the decision code needs from the world outside it goes through two ports:

  Host        the world: YouTube search / verify / download, stem separation, audio reads, the
              session and song logs, the library's energy distribution, background job
              execution, ids and the clock. `Host` below IS the production host: every method
              calls the real implementation (late bound, so a test may still monkeypatch the
              module function it wraps).
  AIBackend   the model: `chat` (suggest, look-ahead, plan) and `ear` (the live ear's audio
              call). `AIBackend` below is the production one (the local OpenAI-compatible
              server). A replay, a stub or a recorder is another AIBackend.

`Engine` also holds the `EngineConfig` and is the service entry the server and the virtual set
share (`Engine.suggest`). The composition root builds one Engine and installs it with `use()`;
nothing in the brain reaches for a module global to find its edges, and the sim installs no
monkeypatches: it builds a `SimHost(Host)` / `SimAI(AIBackend)` and an Engine from them.

This module imports no other app module at import time (production methods import lazily), so
any module may import it.
"""
from __future__ import annotations

import contextlib
import secrets
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class EngineConfig:
    """Knobs the composition root may set. None keeps the module's own default."""
    suggest_budget_s: Optional[float] = None      # wall-clock budget of one suggest call (corrective retries)
    verify_timeout_s: Optional[float] = None      # wall-clock wait for one call's YouTube lookups (slower = unknown, kept)


class AIBackend:
    """Production model backend: the local OpenAI-compatible text server and the Omni ear."""

    def chat(self, system, user, temperature, timeout, model, max_tokens) -> str:
        from app.ui.services import autopilot_service as svc

        return svc._chat_call(system, user, temperature, timeout, model, max_tokens)

    def ear(self, cfg: dict, wav: bytes, metrics: dict) -> str:
        from app.ui.services import live_ear

        return live_ear._ask_omni(cfg, wav, metrics)

    def audition(self, system: str, wav: bytes, text: str) -> str:
        """The silent ear rating one rendered merge clip (merge.ear_rate)."""
        from app.music_brain import merge

        return merge._ask_omni(system, wav, text)


class Host:
    """Production host: the real edges."""

    threaded = True     # background workers (pre-render) run on their own threads; the sim steps them itself

    # ---- YouTube ----------------------------------------------------------------
    def search_songs(self, query, limit=8):
        from app.ui.services import download_service

        return download_service.search_songs(query, limit)

    def verify_song(self, artist, title):
        from app.ui.services import download_service

        return download_service.verify_song(artist, title)

    def song_views(self, name):
        from app.ui.services import download_service

        return download_service.song_views(name)

    def download_to_dir(self, url, output_dir, progress=None):
        from app.ui.services import download_service

        return download_service.download_to_dir(url, output_dir, progress=progress)

    def lrclib_search(self, artist, track):
        from app.music_brain import lyrics

        return lyrics._lrclib_search(artist, track)

    # ---- stems and audio ------------------------------------------------------------
    def queue_stems(self, track_id, urgent=True):
        from app.ui import server

        return server._queue_stems_impl(track_id, urgent)

    def drop_stems(self, track_id):
        """Cancel a separation that is queued and not started (a pool candidate that left the list)."""
        from app.ui import server

        return server._dequeue_stems_impl(track_id)

    def stems_running(self):
        from app.ui import server

        return server._stem_busy is not None

    def tempo_running(self):
        """Key-locked tempo renders in flight (Rubber Band, one thread each)."""
        from app.music_brain import keylock

        with keylock._lock:
            return sum(1 for s in keylock._jobs.values() if s == "running")

    def tempo_gate(self, key):
        """A finished tempo set may be served now (production: always; the sim holds it back until the
        render would have finished on the virtual clock)."""
        return True

    def separate(self, audio_path, **kw):
        from app.music_brain import stem_service

        return stem_service.separate(audio_path, **kw)

    def vocals_stem(self, track_id):
        from app.ui import server

        return server._vocals_stem_impl(track_id)

    def hook_drops(self, track_id):
        from app.ui import server

        return server._safe_hook_drops_impl(track_id)

    def keylock_stem_path(self, key, name):
        from app.music_brain import keylock

        return keylock.stem_path(key, name)

    def load_audio(self, path, **kw):
        import librosa

        return librosa.load(path, **kw)

    def stem_map(self, audio, sr, phrases):
        from app.music_brain import techniques

        return techniques.stem_map(audio, sr, phrases)

    def vocal_style(self, y, sr):
        from app.music_brain import techniques

        return techniques.vocal_style(y, sr)

    def file_hash(self, path):
        from app.music_brain import analyzer

        return analyzer._file_hash_impl(path)

    def library_raws(self):
        from app.music_brain import energy

        return energy._library_raws_impl()

    # ---- logs -------------------------------------------------------------------------
    def log_event(self, kind, **fields):
        from app.ui.services import session_log

        session_log._write(kind, **fields)

    def song_step(self, kind, track_id, **fields):
        from app.ui.services import song_log

        song_log.step(kind, track_id, **fields)

    def session_event(self, kind, fields):
        from app.ui.services import song_log

        song_log.on_session_event(kind, fields)

    # ---- jobs, ids, time -----------------------------------------------------------------
    def spawn(self, pool, fn, *args, **kw):
        """Run `fn` on the worker `pool` (a concurrent.futures executor); the sim runs it inline."""
        return pool.submit(fn, *args, **kw)

    def new_id(self, nbytes: int = 6) -> str:
        return secrets.token_hex(nbytes)

    def now(self) -> float:
        return time.time()


class Engine:
    def __init__(self, host: Optional[Host] = None, ai_backend: Optional[AIBackend] = None,
                 config: Optional[EngineConfig] = None):
        self.host = host or Host()
        self.ai = ai_backend or AIBackend()
        self.config = config or EngineConfig()

    # ---- the service entry ---------------------------------------------------------------
    def suggest(self, *args, **kw):
        """Next-song suggestion (model call, repeat / artist / tempo filters, verification):
        `autopilot_service.suggest_next_tracks`, run against this engine's ports. The server's
        /api/autopilot/suggest and the virtual set both come through here."""
        from app.ui.services import autopilot_service as svc

        with using(self):
            return svc.suggest_next_tracks(*args, **kw)

    def verify_timeout_s(self, default: float) -> float:
        b = self.config.verify_timeout_s
        return default if b is None else b

    def suggest_budget_s(self, default: float) -> float:
        b = self.config.suggest_budget_s
        return default if b is None else b


_current: Optional[Engine] = None


def current() -> Engine:
    """The installed engine (a production one until the composition root installs another)."""
    global _current
    if _current is None:
        _current = Engine()
    return _current


def use(engine: Optional[Engine]) -> None:
    """Install `engine` (None: back to a fresh production engine)."""
    global _current
    _current = engine


def load_audio(path, **kw):
    """Read audio through the installed host (production: librosa.load)."""
    return current().host.load_audio(path, **kw)


@contextlib.contextmanager
def using(engine: Engine):
    global _current
    prev, _current = _current, engine
    try:
        yield engine
    finally:
        _current = prev
