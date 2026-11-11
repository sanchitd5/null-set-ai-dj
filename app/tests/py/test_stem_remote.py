"""Console in stem server mode (STEM_SERVER_URL, app/ui/services/stem_remote.py): decks go to the server at
deck priority, the backfill trickles in at backfill priority (2 parked, LLM idle), a deck asking for a parked
backfill song raises it, a server that is down or forgot a job gets it again, the status line is real, the
console never separates locally, and the token can come from the server's env file."""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.ui import server as srv
from app.ui.services import stem_remote


class FakeServer:
    def __init__(self):
        self.jobs, self.calls, self.down = {}, [], False

    def submit(self, tid, path, priority, cache_dir):
        if self.down:
            raise stem_remote.Unreachable("down")
        self.calls.append((tid, priority))
        for j in self.jobs.values():
            if j["id"] == tid and j["status"] in ("queued", "running"):
                if priority == "deck":
                    j["priority"] = "deck"
                return dict(j)
        j = {"job": f"j{len(self.jobs)}", "id": tid, "status": "queued", "priority": priority, "position": 1,
             "progress": 0.0, "model": "htdemucs_ft", "device": None}
        self.jobs[j["job"]] = j
        return dict(j)

    def job(self, jid):
        if self.down:
            raise stem_remote.Unreachable("down")
        j = self.jobs.get(jid)
        return dict(j) if j else None


@pytest.fixture
def remote(monkeypatch, tmp_path):
    fs, done, busy = FakeServer(), set(), {"llm": False}
    monkeypatch.setenv("STEM_SERVER_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(stem_remote, "submit", fs.submit)
    monkeypatch.setattr(stem_remote, "job", fs.job)
    for tid in ("u1", "b1", "b2", "b3"):
        f = tmp_path / f"{tid}.mp3"
        f.write_bytes(b"x")
        monkeypatch.setitem(srv._tracks, tid, f)
    monkeypatch.setattr(srv, "_remote_has_ft", lambda tid: tid in done)
    monkeypatch.setattr(srv, "_cached_stems4", lambda tid: {"vocals": "v"} if tid in done else None)
    monkeypatch.setattr(srv, "_cached_vocal_regions", lambda tid: None)
    monkeypatch.setattr(srv, "_llm_busy", lambda: busy["llm"])
    monkeypatch.setattr(srv, "_stem_queue", [])
    monkeypatch.setattr(srv, "_stem_backlog", [])
    monkeypatch.setattr(srv, "_stem_remote", {})
    monkeypatch.setattr(srv, "_stem_busy", None)
    monkeypatch.setattr(srv, "_stem_remote_err", {"error": None})
    monkeypatch.setattr(srv, "_stem_done", {"separated": 0, "failed": 0})
    monkeypatch.setattr(srv, "_stem_cv", threading.Condition())
    monkeypatch.setattr(srv, "_start_stem_worker", lambda: None)     # the test drives the loop itself
    monkeypatch.setattr(srv, "separate_stems", lambda *a, **k: pytest.fail("console must not separate"))
    return fs, done, busy


def _finish(fs, done, tid, seconds=9.5):
    for j in fs.jobs.values():
        if j["id"] == tid:
            j.update(status="done", progress=1.0, seconds=seconds)
    done.add(tid)


def test_deck_at_deck_priority_backfill_trickles_and_lands(remote):
    fs, done, busy = remote
    for t in ("b1", "b2", "b3"):
        srv._queue_stems_impl(t, urgent=False)
    srv._queue_stems_impl("u1")
    busy["llm"] = True
    srv._remote_stem_step()
    assert fs.calls == [("u1", "deck")]                       # LLM busy: no backfill handed over
    busy["llm"] = False
    srv._remote_stem_step()
    assert fs.calls[1:] == [("b1", "backfill"), ("b2", "backfill")]   # two parked, b3 waits
    assert srv._stem_backlog == ["b3"]
    srv._queue_stems_impl("b2")                               # a deck now wants b2: raised at the server
    srv._remote_stem_step()
    assert fs.calls[-1] == ("b2", "deck") and srv._stem_remote["b2"]["priority"] == "deck"
    _finish(fs, done, "u1")
    _finish(fs, done, "b1")
    srv._remote_stem_step()
    assert "u1" not in srv._stem_remote and srv._stem_done["separated"] == 2
    assert srv._stem_done["last_seconds"] == 9.5
    srv._remote_stem_step()
    assert ("b3", "backfill") in fs.calls                     # a parked slot freed


def test_server_down_then_forgetful(remote):
    fs, done, busy = remote
    fs.down = True
    srv._queue_stems_impl("u1")
    assert srv._remote_stem_step() == 5.0
    assert srv._stem_queue == ["u1"] and "down" in srv._stem_remote_err["error"]
    st = asyncio.run(srv.get_stems_status())
    assert st["line"].startswith("stem server:")
    fs.down = False
    srv._remote_stem_step()
    assert "u1" in srv._stem_remote and srv._stem_remote_err["error"] is None
    fs.jobs.clear()                                           # restarted without its queue file
    srv._remote_stem_step()
    srv._remote_stem_step()
    assert len(fs.calls) == 2 and "u1" in srv._stem_remote    # handed over again


def test_status_line_is_real_progress(remote):
    fs, done, busy = remote
    srv._queue_stems_impl("u1")
    srv._queue_stems_impl("b1", urgent=False)
    srv._remote_stem_step()
    j = next(iter(fs.jobs.values()))
    j.update(status="running", progress=0.42, device="cuda", elapsed=4.2, eta=5.8)
    srv._remote_stem_step()
    st = asyncio.run(srv.get_stems_status())
    assert st["busy"] == "u1"
    assert st["line"] == "htdemucs_ft · cuda · 42% · 4 s, ~6 s left · 1 queued"
    assert st["remote"]["jobs"][0]["progress"] == 0.42


def test_status_line_formats():
    assert stem_remote.status_line(None, 0) == ""
    assert stem_remote.status_line(None, 3) == "3 queued"
    q = {"status": "queued", "position": 2, "model": "htdemucs_ft", "device": None}
    assert stem_remote.status_line(q, 0) == "htdemucs_ft · #2 in queue"


def test_token_from_the_servers_env_file(monkeypatch, tmp_path):
    f = tmp_path / "stem-server.env"
    f.write_text("STEM_SERVER_HOST=127.0.0.1\nSTEM_SERVER_TOKEN='abc123'\n")
    monkeypatch.delenv("STEM_SERVER_TOKEN", raising=False)
    monkeypatch.setenv("STEM_SERVER_TOKEN_FILE", str(f))
    assert stem_remote.token() == "abc123"
    monkeypatch.setenv("STEM_SERVER_TOKEN", "direct")
    assert stem_remote.token() == "direct"
    monkeypatch.delenv("STEM_SERVER_TOKEN")
    monkeypatch.delenv("STEM_SERVER_TOKEN_FILE")
    with pytest.raises(RuntimeError):
        stem_remote.token()


def test_library_tracks_are_linked_uploads_are_not(tmp_path):
    cache = tmp_path / "cache"
    (cache / "songs" / "0123456789abcdef").mkdir(parents=True)
    up = cache / "songs" / "0123456789abcdef" / "audio.mp3"
    up.write_bytes(b"x")
    stem_remote.link("0123456789abcdef", up, cache)
    assert not (cache / "stem_links").exists()
    lib = tmp_path / "Music" / "Song.FLAC"
    lib.parent.mkdir()
    lib.write_bytes(b"y")
    stem_remote.link("fedcba9876543210", lib, cache)
    ln = cache / "stem_links" / "fedcba9876543210.flac"
    assert ln.is_symlink() and ln.resolve() == lib.resolve()
    stem_remote.link("fedcba9876543210", lib, cache)        # idempotent
    assert [p.name for p in (cache / "stem_links").iterdir()] == ["fedcba9876543210.flac"]


def test_host_separate_goes_to_the_server(monkeypatch, tmp_path):
    from app.music_brain.audio import stem_service
    from app.ui.services.engine import Host

    seen = []
    monkeypatch.setenv("STEM_SERVER_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(stem_remote, "separate", lambda p, **kw: seen.append((p, kw)) or "remote")
    monkeypatch.setattr(stem_service, "separate", lambda *a, **k: pytest.fail("local split"))
    assert Host().separate(tmp_path / "a.mp3", two_stems="vocals") == "remote"
    assert seen[0][1] == {"two_stems": "vocals"}
