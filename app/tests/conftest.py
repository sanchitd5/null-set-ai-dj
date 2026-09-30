from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_song_lookup(monkeypatch, tmp_path_factory):
    """Suggest tests fake the LLM; the real-song check would hit YouTube. None = unknown -> kept.
    Each test starts with no remembered lookups, and the bot-check breaker reads a
    private state file, so a real YouTube cooldown on this machine can't switch the check off."""
    import app.ui.services.autopilot_service as svc
    from app.music_brain import yt_guard
    monkeypatch.setattr(svc, "_verify_song", lambda artist, title: None)
    monkeypatch.setattr(yt_guard, "STATE_PATH", tmp_path_factory.mktemp("yt_guard") / "yt_guard.json")
    svc._verify_cache.clear()
    svc._verify_inflight.clear()


@pytest.fixture(autouse=True)
def _private_session_log(tmp_path_factory, monkeypatch):
    """Tests never write into the real per-session event log (data/cache/sessions) nor the real
    set history (data/cache/user.db): a private cache's sessions/ (history keeps its DB beside)."""
    import app.ui.services.session_log as sl
    monkeypatch.setattr(sl, "SESSIONS_DIR", tmp_path_factory.mktemp("cache") / "sessions")


@pytest.fixture(autouse=True)
def _private_genre_labels(tmp_path_factory, monkeypatch):
    """Tests never write the real data/cache/genre_labels.json."""
    import app.ui.server as srv
    monkeypatch.setattr(srv, "LABELS_PATH", tmp_path_factory.mktemp("labels") / "genre_labels.json")


@pytest.fixture(autouse=True)
def _private_vetoes(tmp_path_factory, monkeypatch):
    """Tests never write the real data/cache/vetoes.json (the owner's private vetoes)."""
    from app.music_brain.atlas import vetoes
    p = tmp_path_factory.mktemp("vetoes") / "vetoes.json"
    monkeypatch.setattr(vetoes, "path", lambda cache_dir=None: p)


@pytest.fixture(autouse=True)
def _no_tracked_knowledge(tmp_path_factory, monkeypatch):
    """Tests never seed from, or export into, the tracked app/music_brain/knowledge/."""
    from app.music_brain.matching import knowledge
    monkeypatch.setattr(knowledge, "KNOWLEDGE_DIR", tmp_path_factory.mktemp("knowledge"))
    knowledge._SEEN.clear()


@pytest.fixture(autouse=True)
def _private_learn_progress(tmp_path_factory, monkeypatch):
    """Tests never write into the real data/cache/learn_progress (the console panel reads it)."""
    from app.music_brain.learning import learn_progress
    monkeypatch.setattr(learn_progress, "PROGRESS_DIR", tmp_path_factory.mktemp("learn_progress"))


@pytest.fixture(autouse=True)
def _private_library(tmp_path_factory, monkeypatch):
    """A learn's cleanup never registers into, or deletes from, the real library: it gets a
    temporary CACHE_DIR whose uploads/ is filled by content hash, like POST /api/tracks."""
    import shutil
    from app.music_brain.learning import learn_cleanup, set_learner
    from app.music_brain.learning.set_import import content_id
    cache = tmp_path_factory.mktemp("cache")
    monkeypatch.setattr(set_learner, "CACHE_DIR", cache)

    def fake_register(rows):
        (cache / "uploads").mkdir(exist_ok=True)
        for r in rows:
            if r["action"] == "import":
                shutil.copyfile(r["path"], cache / "uploads" / f"{r['id']}{Path(r['path']).suffix}")
            elif r["action"] == "cut":
                r["error"] = "cut not stubbed"
        return rows
    monkeypatch.setattr(learn_cleanup, "register_rows", fake_register)
