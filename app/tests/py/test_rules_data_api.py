"""GET /api/rules_data (rules_data_api.py): auth, bytes == generated file, sha256 of data, ETag = version, 304."""
import hashlib
import json

import pytest

from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import rules_data_api

PATH = "/api/rules_data"


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def anon():
    return TestClient(server.app, auth=False)


def test_needs_auth(anon):
    assert anon.get(PATH).status_code in (401, 403)


def test_bytes_equal_file_and_sha_matches(client):
    r = client.get(PATH)
    assert r.status_code == 200
    assert r.content == rules_data_api.PATH.read_bytes()
    doc = json.loads(r.content)
    assert doc["schema"] == 1
    canon = json.dumps(doc["data"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert hashlib.sha256(canon).hexdigest() == doc["sha256"]
    assert doc["version"] == f"1.{doc['sha256'][:12]}"
    assert r.headers["etag"] == f'"{doc["version"]}"'
    assert "no-cache" in r.headers["cache-control"]


def test_304_on_if_none_match(client):
    tag = client.get(PATH).headers["etag"]
    for header in (tag, "W/" + tag, f'"x", {tag}', "*"):
        r = client.get(PATH, headers={"If-None-Match": header})
        assert r.status_code == 304 and r.content == b"" and r.headers["etag"] == tag
    assert client.get(PATH, headers={"If-None-Match": '"stale"'}).status_code == 200


def test_404_when_file_missing(client, monkeypatch, tmp_path):
    monkeypatch.setattr(rules_data_api, "PATH", tmp_path / "none.json")
    assert client.get(PATH).status_code == 404
