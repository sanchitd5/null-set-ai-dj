import pytest


@pytest.fixture(autouse=True)
def _no_song_lookup(monkeypatch):
    """Suggest tests fake the LLM; the real-song check would hit YouTube. None = unknown -> kept."""
    import app.ui.autopilot_service as svc
    monkeypatch.setattr(svc, "_verify_song", lambda artist, title: None)


@pytest.fixture(autouse=True)
def _private_session_log(tmp_path_factory, monkeypatch):
    """Tests never write into the real per-session event log (data/cache/sessions)."""
    import app.ui.session_log as sl
    monkeypatch.setattr(sl, "SESSIONS_DIR", tmp_path_factory.mktemp("sessions"))
