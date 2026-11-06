import atexit
import os
import secrets
import shutil
import tempfile
from pathlib import Path

import pytest

# Auth stays ON in tests: a throwaway account store (owner password + one device token) and
# session secret, set before app.ui.server is imported, so the owner's real store and .env
# are never read. testclient_compat.TestClient sends DJ_API_TOKEN as a Bearer header;
# test_auth.py covers logins, cookies and the rest.
_AUTH_DIR = Path(tempfile.mkdtemp(prefix="dj-auth-test-"))
atexit.register(shutil.rmtree, _AUTH_DIR, True)
os.environ["DJ_AUTH_FILE"] = str(_AUTH_DIR / "auth.json")
os.environ["DJ_SESSION_SECRET"] = secrets.token_urlsafe(32)
# Google sign-in off in the shared server (an owner .env setting must not leak in: load_dotenv
# never overrides a variable that is already set). test_auth_google.py builds its own app.
for _k in ("DJ_GOOGLE_CLIENT_ID", "DJ_GOOGLE_CLIENT_SECRET", "DJ_PUBLIC_URL", "DJ_ALLOWED_EMAILS",
           "DJ_GOOGLE_MOBILE_CLIENT_IDS", "DJ_GOOGLE_HD", "DJ_PASSWORD_LOGIN"):
    os.environ[_k] = ""
TEST_USER, TEST_PASSWORD = "owner", "pw-" + secrets.token_urlsafe(16)
from app.ui.services import auth as _auth  # noqa: E402

_store = _auth.AuthStore(_AUTH_DIR / "auth.json")
_store.set_password(TEST_USER, TEST_PASSWORD)
os.environ["DJ_API_TOKEN"] = _store.new_token("pytest")[0]


@pytest.fixture
def api_token():
    """The device token every TestClient sends."""
    return os.environ["DJ_API_TOKEN"]


@pytest.fixture
def owner_login():
    """(username, password) of the test owner account."""
    return TEST_USER, TEST_PASSWORD


@pytest.fixture(autouse=True)
def _never_touch_the_real_cache_dbs(monkeypatch, tmp_path_factory):
    """No test may open app.db / user.db in the real data/cache: a store left on its default
    path would migrate (and rename) the owner's live JSON stores. The default cache dir goes
    to a temp folder, and opening a DB inside the real cache fails the test loudly."""
    import sqlite3
    from app.music_brain import config

    real = Path(config.CACHE_DIR).resolve()
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path_factory.mktemp("cache_default"))
    orig = sqlite3.connect

    def guarded(database, *a, **kw):
        s = str(database)
        if s.startswith("file:"):
            s = s[5:].split("?", 1)[0]
        if s and s != ":memory:":
            p = Path(s).expanduser().resolve()
            if p == real or real in p.parents:
                raise AssertionError(f"test opened a DB in the real cache: {p}")
        return orig(database, *a, **kw)
    # every store (db.connect, set_memory's read-only open, the sandbox) goes through sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", guarded)
    # stores whose default legacy path was computed from the real CACHE_DIR at import time
    from app.music_brain.learning import set_learner
    monkeypatch.setattr(set_learner, "LEARNED_PATH", tmp_path_factory.mktemp("learned") / "learned_techniques.json")


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
    from app.ui.services.set_memory import SetMemory
    monkeypatch.setattr(srv, "LABELS_PATH", tmp_path_factory.mktemp("labels") / "genre_labels.json")
    # the server builds SetMemory(CACHE_DIR / "set_memory.json") from its import-time (real) CACHE_DIR
    monkeypatch.setattr(srv, "_set_memory", SetMemory(tmp_path_factory.mktemp("set_memory") / "set_memory.json"))


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
