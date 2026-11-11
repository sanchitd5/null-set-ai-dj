"""djcore_bridge against a fake child that speaks the wire protocol (runs everywhere, needs no dj-core build)."""
import os
import stat
import sys
import textwrap
import time

import pytest

from app.music_brain import djcore_bridge as db

FAKE = textwrap.dedent('''\
    #!{py}
    import json, os, sys, time
    mode = os.environ.get("FAKE_MODE", "ok")
    proto = 99 if mode == "badproto" else 1
    print(json.dumps({{"hello": "dj-core-svc", "protocol": proto, "sha": "fake", "fns": []}}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        if req["fn"] == "die":
            os._exit(3)
        if req["fn"] == "hang":
            time.sleep(60)
        if req["fn"] == "stale":
            print(json.dumps({{"id": req["id"] - 1, "ok": True, "result": "old"}}), flush=True)
        if req["fn"] == "bad":
            print(json.dumps({{"id": req["id"], "ok": False, "error": "nope"}}), flush=True)
            continue
        print(json.dumps({{"id": req["id"], "ok": True, "result": req["args"]}}), flush=True)
''')


@pytest.fixture
def fake(tmp_path, monkeypatch):
    p = tmp_path / "fake-svc"
    p.write_text(FAKE.format(py=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(db, "RESTART_BACKOFF_S", 0.0)
    b = db.DjCoreBridge(str(p))
    yield b
    b.close()


def test_echo_and_info(fake):
    assert fake.call("echo", {"a": 1}) == {"a": 1}
    assert fake.info()["sha"] == "fake"


def test_rejected_call_is_dj_core_error(fake):
    with pytest.raises(db.DjCoreError, match="nope"):
        fake.call("bad")


def test_stale_answer_is_skipped(fake):
    assert fake.call("stale", "x") == "x"


def test_missing_binary_raises_loudly(tmp_path, capsys):
    b = db.DjCoreBridge(str(tmp_path / "nope"))
    with pytest.raises(db.DjCoreUnavailable, match="not found"):
        b.call("ping")


def test_protocol_mismatch_refused(fake, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "badproto")
    with pytest.raises(db.DjCoreUnavailable, match="protocol"):
        fake.call("ping")


def test_crash_then_restart(fake, capsys):
    assert fake.call("echo", 1) == 1
    with pytest.raises(db.DjCoreUnavailable):
        fake.call("die")
    assert "ERROR" in capsys.readouterr().out          # loud
    assert fake.call("echo", 2) == 2                   # restarted


def test_timeout_kills_and_restarts(fake):
    t = time.time()
    with pytest.raises(db.DjCoreUnavailable, match="no answer"):
        fake.call("hang", timeout=0.3)
    assert time.time() - t < 5
    assert fake.call("echo", 3) == 3


def test_threads_share_one_child(fake):
    import threading
    out, errs = [], []

    def run(i):
        try:
            out.append(fake.call("echo", i) == i)
        except Exception as e:                         # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and all(out) and len(out) == 20


def test_pin_selects_versioned_build_and_refuses_wrong_checksum(tmp_path, monkeypatch):
    import hashlib
    from app.music_brain import djcore_bridge as b
    exe = tmp_path / "versions" / "abc123"
    exe.parent.mkdir()
    exe.write_bytes(b"#!/bin/sh\n")
    exe.chmod(0o755)
    good = hashlib.sha256(exe.read_bytes()).hexdigest()
    pin = tmp_path / "dj-core-svc.version"
    pin.write_text(f"# comment\nsha=abc123\nsha256={good}\n")
    monkeypatch.delenv("DJ_CORE_SVC", raising=False)
    monkeypatch.setattr(b, "PIN_FILE", str(pin))
    monkeypatch.setattr(b, "VERSIONS_DIR", str(tmp_path / "versions"))
    assert b.binary_path() == str(exe)
    b.check_pinned(str(exe))
    pin.write_text("sha=abc123\nsha256=" + "0" * 64 + "\n")
    with pytest.raises(b.DjCoreUnavailable):
        b.check_pinned(str(exe))
    pin.unlink()
    assert b.binary_path() == b.DEFAULT_BIN


def test_repo_pin_is_well_formed():
    from app.music_brain import djcore_bridge as b
    pin = b.read_pin()
    assert len(pin.get("sha", "")) == 40 and len(pin.get("sha256", "")) == 64
