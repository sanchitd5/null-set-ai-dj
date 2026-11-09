"""`Content-Encoding: gzip` request bodies (auth.GzipRequestMiddleware), through the real console app."""
import gzip
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import app_crash_api, auth

REPORT = {"kind": "init_error", "message": "Bad state: no engine", "stack": "#0 main (main.dart:55)",
          "version_name": "1.0.0-parity.16", "version_code": 16, "device": "Pixel 8", "at": "2026-11-08T12:00:00Z"}
GZ = {"Content-Type": "application/json", "Content-Encoding": "gzip"}


@pytest.fixture
def log(tmp_path, monkeypatch):
    p = tmp_path / "app" / "crash_reports.jsonl"
    monkeypatch.setattr(app_crash_api, "CRASH_LOG", p)
    return p


def _client(api_token):
    return TestClient(server.app, auth=False, headers={"Authorization": f"Bearer {api_token}"})


def _post(api_token, raw, headers=GZ):
    return _client(api_token).post("/api/app/crash", content=raw, headers=headers)


def test_gzip_body_is_accepted(api_token, log):
    r = _post(api_token, gzip.compress(json.dumps(REPORT).encode()))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert json.loads(log.read_text().splitlines()[0])["message"] == REPORT["message"]


def test_x_gzip_alias_and_case(api_token, log):
    r = _post(api_token, gzip.compress(json.dumps(REPORT).encode()), {**GZ, "Content-Encoding": "X-GZip"})
    assert r.status_code == 200


def test_plain_body_unchanged(api_token, log):
    assert _client(api_token).post("/api/app/crash", json=REPORT).status_code == 200
    assert _post(api_token, json.dumps(REPORT).encode(),
                 {"Content-Type": "application/json", "Content-Encoding": "identity"}).status_code == 200


def test_bomb_is_413_on_the_decompressed_size(api_token, log):
    bomb = gzip.compress(b'{"message":"' + b"a" * (20 * 1024 * 1024) + b'"}')
    assert len(bomb) < 64 * 1024                    # passes the wire cap: only the inflated size is over
    r = _post(api_token, bomb)
    assert r.status_code == 413 and not log.exists()


def test_compressed_size_still_capped(api_token, log):
    import os
    r = _post(api_token, gzip.compress(os.urandom(200 * 1024)))      # incompressible: the wire body is over 64 KB
    assert r.status_code == 413


def test_corrupt_stream_is_400(api_token, log):
    good = gzip.compress(json.dumps(REPORT).encode())
    assert _post(api_token, b"not gzip at all").status_code == 400
    assert _post(api_token, good[:-6]).status_code == 400          # truncated
    assert _post(api_token, good + b"junk").status_code == 400     # trailing data
    assert _post(api_token, b"").status_code == 400


def test_unknown_encoding_is_415(api_token, log):
    for enc in ("br", "deflate", "zstd", "gzip, gzip"):
        r = _post(api_token, b"{}", {**GZ, "Content-Encoding": enc})
        assert r.status_code == 415, enc


def test_gzip_needs_auth_first(log):
    r = TestClient(server.app, auth=False).post("/api/app/crash", content=gzip.compress(b"{}"), headers=GZ)
    assert r.status_code == 401


def test_learn_route_gets_the_inflated_body(api_token, monkeypatch):
    from app.ui.services import learn_ingest_api

    seen = {}

    def fake(body):
        seen.update(body)
        return {"accepted": [], "duplicate": [], "rejected": [], "publish": False}

    monkeypatch.setattr(learn_ingest_api, "_ingest", fake)
    payload = {"device_id": "d" * 16, "items": [{"k": i, "v": "x" * 50} for i in range(200)]}
    raw = json.dumps(payload).encode()
    r = _client(api_token).post("/api/mobile/learn", content=gzip.compress(raw), headers=GZ)
    assert r.status_code == 200 and seen == payload


def test_gzip_inflater_streams_chunks():
    """The ASGI layer alone: a body split over several messages, then the app sees one inflated body."""
    import asyncio

    raw = json.dumps({"a": "b" * 5000}).encode()
    z = gzip.compress(raw)
    parts = [z[i:i + 7] for i in range(0, len(z), 7)]
    msgs = [{"type": "http.request", "body": p, "more_body": i < len(parts) - 1} for i, p in enumerate(parts)]
    got = {}

    async def app(scope, receive, send):
        m = await receive()
        got["body"], got["h"] = m["body"], dict(scope["headers"])

    async def receive():
        return msgs.pop(0)

    mw = auth.GzipRequestMiddleware(app, {}, 10_000)
    scope = {"type": "http", "method": "POST", "path": "/x",
             "headers": [(b"content-encoding", b"gzip"), (b"content-length", str(len(z)).encode())]}
    asyncio.run(mw(scope, receive, None))
    assert got["body"] == raw and b"content-encoding" not in got["h"]
    assert got["h"][b"content-length"] == str(len(raw)).encode()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_gzip_body_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).parents[1].joinpath("js", "gzip_body_check.js"))],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout


def test_index_loads_gzip_body():
    html = (Path(server.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    assert '<script src="/gzip-body.js"></script>' in html
