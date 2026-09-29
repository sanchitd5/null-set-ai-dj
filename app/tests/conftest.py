import pytest


@pytest.fixture(autouse=True)
def _no_song_lookup(monkeypatch, tmp_path_factory):
    """Suggest tests fake the LLM; the real-song check would hit YouTube. None = unknown -> kept.
    Each test starts with no remembered lookups, and the bot-check breaker reads a
    private state file, so a real YouTube cooldown on this machine can't switch the check off."""
    import app.ui.autopilot_service as svc
    from app.music_brain import yt_guard
    monkeypatch.setattr(svc, "_verify_song", lambda artist, title: None)
    monkeypatch.setattr(yt_guard, "STATE_PATH", tmp_path_factory.mktemp("yt_guard") / "yt_guard.json")
    svc._verify_cache.clear()
    svc._verify_inflight.clear()


@pytest.fixture(autouse=True)
def _private_session_log(tmp_path_factory, monkeypatch):
    """Tests never write into the real per-session event log (data/cache/sessions)."""
    import app.ui.session_log as sl
    monkeypatch.setattr(sl, "SESSIONS_DIR", tmp_path_factory.mktemp("sessions"))


@pytest.fixture(autouse=True)
def _private_learn_progress(tmp_path_factory, monkeypatch):
    """Tests never write into the real data/cache/learn_progress (the console panel reads it)."""
    from app.music_brain import learn_progress
    monkeypatch.setattr(learn_progress, "PROGRESS_DIR", tmp_path_factory.mktemp("learn_progress"))
