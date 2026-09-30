"""Engine(host, ai_backend, config): the brain finds its edges through the injected ports, not through module
globals; the production Host / AIBackend late-bind the real functions, so existing monkeypatches still work."""
import json
from pathlib import Path

from app.ui.services import engine


class FakeAI(engine.AIBackend):
    def __init__(self):
        self.chats = []

    def chat(self, system, user, temperature, timeout, model, max_tokens):
        self.chats.append((system, user))
        return json.dumps({"suggestions": []})


class FakeHost(engine.Host):
    def __init__(self):
        self.events = []
        self.spawned = []
        self.ids = iter(range(1, 100))

    def verify_song(self, artist, title):
        return artist == "Real"

    def log_event(self, kind, **fields):
        self.events.append((kind, fields))

    def spawn(self, pool, fn, *args, **kw):
        self.spawned.append(fn)
        fn(*args, **kw)                       # inline

    def new_id(self, nbytes=6):
        return f"{next(self.ids):0{2 * nbytes}x}"

    def now(self):
        return 1234.5

    def library_raws(self):
        return [0.1, 0.2]


def test_default_engine_is_production_and_installable():
    assert isinstance(engine.current().host, engine.Host) and isinstance(engine.current().ai, engine.AIBackend)
    fake = engine.Engine(FakeHost(), FakeAI())
    with engine.using(fake):
        assert engine.current() is fake
    assert engine.current() is not fake


def test_chat_verify_and_log_go_through_the_injected_ports(monkeypatch):
    from app.ui.services import autopilot_service as svc
    from app.ui.services import session_log

    monkeypatch.undo()                        # conftest stubs svc._verify_song for every test: use the real one here
    host, ai = FakeHost(), FakeAI()
    with engine.using(engine.Engine(host, ai)):
        raw = svc.chat_raw("sys", "usr")
        assert json.loads(raw) == {"suggestions": []} and ai.chats == [("sys", "usr")]
        assert svc._verify_song("Real", "x") is True and svc._verify_song("Fake", "x") is False
        session_log.log("hello", n=1)
    kinds = [k for k, _ in host.events]
    assert "llm" in kinds and ("hello", {"n": 1}) in host.events        # chat_raw logs its call through the host too


def test_suggest_budget_comes_from_the_config():
    e = engine.Engine(config=engine.EngineConfig(suggest_budget_s=1e9))
    assert e.suggest_budget_s(15.0) == 1e9
    assert engine.Engine().suggest_budget_s(15.0) == 15.0


def test_download_and_background_jobs_use_the_host_clock_ids_and_executor(tmp_path):
    from app.ui.services import bg_jobs, download_jobs

    host = FakeHost()
    with engine.using(engine.Engine(host, FakeAI())):
        seen = []
        jid = download_jobs.start_job("u", "l", tmp_path, download_fn=lambda url, d, progress=None: [],
                                      register_fn=lambda paths: [], analyze_fn=lambda t: None)
        job = download_jobs.get_job(jid)
        assert jid == "000000000001" and job["created_at"] == 1234.5 and job["state"] == "done"
        runner = bg_jobs.JobRunner("t", workers=1)
        j = runner.submit("k", lambda: seen.append(1) or "out")
        assert len(j.id) == 16 and int(j.id, 16) > 1 and runner.get(j.id).status == bg_jobs.DONE      # the host's counter, not a random id
    assert seen == [1] and len(host.spawned) == 2


def test_energy_and_hash_read_the_hosts_view_of_the_world(tmp_path):
    from app.music_brain import analyzer, energy

    class H(FakeHost):
        def file_hash(self, path):
            return "h-" + Path(path).name

    with engine.using(engine.Engine(H(), FakeAI())):
        assert energy.library_raws() == [0.1, 0.2]
        assert analyzer._file_hash(tmp_path / "a.wav") == "h-a.wav"
    f = tmp_path / "real.bin"
    f.write_bytes(b"abc")
    assert analyzer._file_hash(f) == analyzer._file_hash_impl(f)        # production: the real sha256
