import pytest


@pytest.fixture(autouse=True)
def _no_song_lookup(monkeypatch):
    """Suggest tests fake the LLM; the real-song check would hit YouTube. None = unknown -> kept."""
    import app.ui.autopilot_service as svc
    monkeypatch.setattr(svc, "_verify_song", lambda artist, title: None)
