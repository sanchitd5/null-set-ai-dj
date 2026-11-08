"""APK delta updates: deploy/apk_delta.py (patch build / apply / publish) and the /api/app routes that serve them."""
import hashlib
import importlib.util
import json
import os
import random
import subprocess
import zipfile
from pathlib import Path

import pytest

from app.tests.py.testclient_compat import TestClient
from app.ui import server

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("apk_delta", ROOT / "deploy" / "apk_delta.py")
delta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(delta)


def _blob(seed, n):
    return random.Random(seed).randbytes(n)


def make_apk(path, entries):
    """entries: name -> (bytes, deflate?)"""
    with zipfile.ZipFile(path, "w") as z:
        for name, (data, deflate) in entries.items():
            z.writestr(zipfile.ZipInfo(name, (1981, 1, 1, 1, 1, 0)), data,
                       compress_type=zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED)
    return path


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


BIG = _blob(1, 300_000)
OLD = {"classes.dex": (_blob(2, 80_000), True), "lib/libbig.so": (BIG, False), "assets/a.json": (b'{"a":1}' * 500, True)}
NEW = {"classes.dex": (_blob(3, 80_000), True),                       # changed, incompressible: must be sent
       "lib/libbig.so": (BIG[:100_000] + b"INSERTED-BYTES" + BIG[100_000:], False),   # shifted stored lib: found by the matcher
       "assets/a.json": OLD["assets/a.json"]}                         # identical: pure copy


@pytest.fixture
def pair(tmp_path):
    return make_apk(tmp_path / "old.apk", OLD), make_apk(tmp_path / "new.apk", NEW)


def test_roundtrip_is_small_and_exact(pair, tmp_path):
    old, new = pair
    n = delta.make_patch(old, new, tmp_path / "p.patch")
    assert n < 80_000 + 5_000           # the changed dex + overhead; the 300 KB lib and the json are copies
    delta.apply_patch(old, tmp_path / "p.patch", tmp_path / "out.apk")
    assert (tmp_path / "out.apk").read_bytes() == new.read_bytes()
    with zipfile.ZipFile(tmp_path / "out.apk") as z:
        assert z.testzip() is None


def test_identical_apks_give_a_tiny_patch(pair, tmp_path):
    old, _ = pair
    assert delta.make_patch(old, old, tmp_path / "p.patch") < 300


def test_apply_refuses_the_wrong_old_apk(pair, tmp_path):
    old, new = pair
    delta.make_patch(old, new, tmp_path / "p.patch")
    other = make_apk(tmp_path / "other.apk", {"x": (b"y" * 100, True)})
    with pytest.raises(ValueError, match="different old"):
        delta.apply_patch(other, tmp_path / "p.patch", tmp_path / "out.apk")
    assert not (tmp_path / "out.apk").exists() and not (tmp_path / "out.apk.tmp").exists()


def test_apply_refuses_a_corrupt_patch(pair, tmp_path):
    old, new = pair
    delta.make_patch(old, new, tmp_path / "p.patch")
    raw = bytearray((tmp_path / "p.patch").read_bytes())
    raw[-200] ^= 0xFF                                  # flip a literal byte
    (tmp_path / "bad.patch").write_bytes(bytes(raw))
    with pytest.raises(ValueError):
        delta.apply_patch(old, tmp_path / "bad.patch", tmp_path / "out.apk")
    (tmp_path / "cut.patch").write_bytes(bytes(raw[:-500]))
    with pytest.raises(ValueError):
        delta.apply_patch(old, tmp_path / "cut.patch", tmp_path / "out.apk")
    assert not (tmp_path / "out.apk").exists()


def test_publish_archives_prunes_and_lists_patches(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    builds = {}
    for code in (4, 5, 6, 7):
        ents = dict(OLD)
        ents["classes.dex"] = (_blob(100 + code, 80_000), True)
        builds[code] = make_apk(tmp_path / f"b{code}.apk", ents)
    for code in (4, 5, 6, 7):
        (d / "null-set-dj.apk").write_bytes(builds[code].read_bytes())
        meta = {"version_name": f"1.0.{code}", "version_code": code, "sha256": sha(builds[code]),
                "size": builds[code].stat().st_size, "notes": "n", "published_at": "t"}
        (d / "null-set-dj.json").write_text(json.dumps(meta))
        out = delta.publish(str(d), 3)
    assert sorted(os.listdir(d / "history")) == ["5.apk", "6.apk", "7.apk"]       # keep 3: current + 2 older
    assert sorted(os.listdir(d / "patches")) == ["5.patch", "6.patch"]
    assert [p["from_version_code"] for p in out] == [6, 5]
    got = json.loads((d / "null-set-dj.json").read_text())
    assert got["patches"] == out and got["version_code"] == 7
    p6 = next(p for p in out if p["from_version_code"] == 6)
    assert p6["sha256"] == sha(builds[7]) and p6["from_sha256"] == sha(builds[6]) and p6["patch_sha256"] == sha(d / "patches" / "6.patch")
    delta.apply_patch(d / "history" / "6.apk", d / "patches" / "6.patch", tmp_path / "r.apk")
    assert sha(tmp_path / "r.apk") == sha(builds[7])


def test_publish_seeds_history_from_the_bak(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    a, b = make_apk(tmp_path / "a.apk", OLD), make_apk(tmp_path / "b.apk", NEW)
    (d / "null-set-dj.apk.bak").write_bytes(a.read_bytes())
    (d / "null-set-dj.json.bak").write_text(json.dumps({"version_code": 3}))
    (d / "null-set-dj.apk").write_bytes(b.read_bytes())
    (d / "null-set-dj.json").write_text(json.dumps({"version_code": 4, "sha256": sha(b), "size": b.stat().st_size}))
    assert [p["from_version_code"] for p in delta.publish(str(d), 3)] == [3]


def test_publish_skips_a_patch_that_is_not_worth_it(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    a = make_apk(tmp_path / "a.apk", {"x": (_blob(5, 50_000), False)})
    b = make_apk(tmp_path / "b.apk", {"y": (_blob(6, 50_000), False)})
    (d / "null-set-dj.apk").write_bytes(a.read_bytes())
    (d / "null-set-dj.json").write_text(json.dumps({"version_code": 1, "sha256": sha(a), "size": a.stat().st_size}))
    delta.publish(str(d), 3)
    (d / "null-set-dj.apk").write_bytes(b.read_bytes())
    (d / "null-set-dj.json").write_text(json.dumps({"version_code": 2, "sha256": sha(b), "size": b.stat().st_size}))
    assert delta.publish(str(d), 3) == []
    assert os.listdir(d / "patches") == []


# ---- routes -------------------------------------------------------------------------------------------------

META = {"version_name": "1.0.0-x", "version_code": 7, "sha256": "ab" * 32, "size": 100, "notes": "n", "published_at": "t"}
PATCH = {"from_version_code": 6, "from_sha256": "cd" * 32, "sha256": "ab" * 32, "patch_sha256": "ef" * 32,
         "patch_size": 5, "size": 100}


@pytest.fixture
def served(tmp_path, monkeypatch):
    (tmp_path / "patches").mkdir()
    (tmp_path / "null-set-dj.apk").write_bytes(b"x")
    (tmp_path / "patches" / "6.patch").write_bytes(b"PATCH")
    (tmp_path / "null-set-dj.json").write_text(json.dumps({**META, "patches": [{**PATCH, "junk": 1}, {**PATCH, "from_version_code": 5}]}))
    monkeypatch.setattr(server, "ANDROID_APK", tmp_path / "null-set-dj.apk")
    monkeypatch.setattr(server, "ANDROID_META", tmp_path / "null-set-dj.json")
    return tmp_path


def test_json_without_from_has_no_patch(served):
    assert TestClient(server.app).get("/api/app/android.json").json() == META


def test_json_with_from_lists_the_patch(served):
    j = TestClient(server.app).get("/api/app/android.json?from=6").json()
    assert j == {**META, "patch": PATCH}        # junk key dropped


def test_json_no_patch_for_unknown_or_missing_file(served):
    c = TestClient(server.app)
    assert "patch" not in c.get("/api/app/android.json?from=9").json()
    assert "patch" not in c.get("/api/app/android.json?from=5").json()    # listed but the file is gone
    assert c.get("/api/app/android.json?from=abc").status_code == 422


def test_patch_route_serves_the_file_and_needs_auth(served):
    r = TestClient(server.app).get("/api/app/android.patch?from=6")
    assert r.status_code == 200 and r.content == b"PATCH"
    assert TestClient(server.app, auth=False).get("/api/app/android.patch?from=6").status_code == 401
    assert TestClient(server.app).get("/api/app/android.patch?from=5").status_code == 404
    assert TestClient(server.app).get("/api/app/android.patch?from=../x").status_code == 422
    assert TestClient(server.app).get("/api/app/android.patch").status_code == 422


def test_push_script_dry_run_publishes_patches(tmp_path):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"hello")
    run = lambda *extra: subprocess.run(
        ["bash", str(ROOT / "deploy" / "push_apk.sh"), "--dry-run", "--build-name", "1.0.0-x", "--build-number", "7",
         "--out-dir", str(tmp_path), *extra, str(apk), "n"], capture_output=True, text=True)
    out = run("--keep", "4")
    assert out.returncode == 0 and "apk_delta.py\" publish" in out.stdout and "scp -P 2298 " in out.stdout
    assert "bash -s " in out.stdout and " 1 4 " in out.stdout
    assert "apk_delta.py\" publish" in run().stdout and "--keep" in run().stdout
    assert "scp -P 2298 " in run("--no-patches").stdout and ":/root/apk_delta.py" not in run("--no-patches").stdout
    assert run("--keep", "0").returncode != 0
