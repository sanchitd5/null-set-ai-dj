"""GET /api/config/live (app/ui/services/config_live.py): generated defaults + overrides file, clamped."""
import json

import pytest

from app.music_brain.generated import dj_config as DJ
from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import config_live


@pytest.fixture
def overrides(tmp_path, monkeypatch):
    p = tmp_path / "config_overrides.json"
    monkeypatch.setattr(config_live, "OVERRIDES_PATH", p)
    return p


def _get():
    r = TestClient(server.app).get("/api/config/live")
    assert r.status_code == 200
    return r.json()


def test_defaults_carry_value_min_max_for_every_live_entry(overrides):
    body = _get()
    assert body["config_hash"] == DJ.CONFIG_HASH
    assert set(body["values"]) == set(DJ.LIVE)
    for name, e in body["values"].items():
        assert e["min"] == DJ.LIVE[name]["min"] and e["max"] == DJ.LIVE[name]["max"]
        assert e["value"] == getattr(DJ, name)


def test_override_applies_and_clamps(overrides):
    overrides.write_text(json.dumps({"KEY_SAFE_MIN": 0.7, "BYPASS_KEY_SCORE": 5, "ENERGY_MIN_RAW": -3}))
    v = _get()["values"]
    assert v["KEY_SAFE_MIN"]["value"] == 0.7
    assert v["BYPASS_KEY_SCORE"]["value"] == DJ.LIVE["BYPASS_KEY_SCORE"]["max"]
    assert v["ENERGY_MIN_RAW"]["value"] == DJ.LIVE["ENERGY_MIN_RAW"]["min"]


def test_unknown_name_non_number_and_non_live_are_ignored(overrides):
    overrides.write_text(json.dumps({"NOPE": 1, "KEY_SAFE_MIN": "high", "KEYLOCK_RANGE_PCT": 1, "BYPASS_KEY_SCORE": True}))
    body = _get()
    assert "NOPE" not in body["values"] and "KEYLOCK_RANGE_PCT" not in body["values"]
    assert body["values"]["KEY_SAFE_MIN"]["value"] == DJ.KEY_SAFE_MIN
    assert body["values"]["BYPASS_KEY_SCORE"]["value"] == DJ.BYPASS_KEY_SCORE


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '"x"', ""])
def test_bad_overrides_file_is_ignored(overrides, text):
    overrides.write_text(text)
    assert _get()["values"]["KEY_SAFE_MIN"]["value"] == DJ.KEY_SAFE_MIN


def test_requires_the_same_auth_as_other_routes(overrides):
    anon = TestClient(server.app, auth=False)
    r = anon.get("/api/config/live")
    assert r.status_code == 401 and r.json() == {"detail": "unauthorized"}


def test_config_live_js_is_a_script_with_clamped_values(overrides):
    overrides.write_text(json.dumps({"KEY_SAFE_MIN": 9}))
    r = TestClient(server.app).get("/config-live.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-store"
    head, _, rest = r.text.partition(";\n")
    assert head.startswith("window.DJ_CONFIG_SERVER = ")
    payload = json.loads(head.removeprefix("window.DJ_CONFIG_SERVER = "))
    assert payload["values"]["KEY_SAFE_MIN"]["value"] == DJ.LIVE["KEY_SAFE_MIN"]["max"]
    assert "applyLive" in rest


def test_config_live_js_error_gives_empty_object(overrides, monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(config_live, "live_config", boom)
    r = TestClient(server.app).get("/config-live.js")
    assert r.status_code == 200 and r.text.startswith("window.DJ_CONFIG_SERVER = {};\n")


def test_config_live_js_requires_auth(overrides):
    assert TestClient(server.app, auth=False).get("/config-live.js").status_code == 401
