"""Stem server (app/stem_server): auth, upload, jobs, and sample-exact AAC windows.

The model is mocked (a fake split writes click-track stems); the real-GPU test is marked slow.
Alignment: a click window is encoded to m4a and decoded with ffmpeg twice, once honouring the edit
list (what AVFoundation does) and once as a raw decoder (Android MediaExtractor + MediaCodec loop:
every decoded sample, priming included) trimmed by the X-Stem-* headers; both must put every click
within 1 ms of where it sits in the stem.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.music_brain.audio.audio_io import write_flac, write_manifest
from app.tests.py.ffmpeg_compat import apply_end_trim
from app.stem_server import windows
from app.stem_server.server import Settings, create_app

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

SR = 44100
TOKEN = "t" * 32
CLICKS_S = [1.0, 2.5, 7.25, 11.0, 13.333]


def _click_track(seconds: float, at: list[float], freq: float = 3000.0) -> np.ndarray:
    x = np.zeros((int(seconds * SR), 2), dtype=np.float32)
    n = int(0.004 * SR)
    burst = 0.8 * np.sin(2 * np.pi * freq * np.arange(n) / SR).astype(np.float32)
    for t in at:
        i = round(t * SR)
        x[i:i + n, 0] += burst
        x[i:i + n, 1] += burst
    return x


def _onsets(x: np.ndarray, rel: float = 0.3) -> list[int]:
    """First sample of each burst (|x| over rel * peak), bursts >= 0.2 s apart."""
    a = np.abs(x).max(axis=1)
    hot = np.flatnonzero(a > rel * a.max())
    out = []
    for i in hot:
        if not out or i - out[-1] > 0.2 * SR:
            out.append(int(i))
    return out


def leading_skip(decoded: int, frames: int, delay: int, padding: int) -> int:
    """Python twin of the phone's `leadingSkip` (lib/separation/remote_stems.dart): samples to drop
    from the start of a decode so sample 0 is the window's first frame (negative = prepend zeros)."""
    d = decoded - frames
    if d >= delay:
        return delay                     # priming kept (raw decoder); padding kept or trimmed
    if d == 0 or d == padding:
        return 0                         # priming trimmed by the decoder (edit list / iTunSMPB)
    return d - padding if d < 0 else 0   # over-trimmed start (e.g. a 2112 assumption): pad zeros


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Srv:
    def __init__(self, tmp: Path, separate_fn):
        import uvicorn

        self.cache = tmp / "cache"
        self.settings = Settings({"STEM_SERVER_TOKEN": TOKEN, "AIDJ_CACHE_DIR": str(self.cache),
                                  "DJ_AUTH_FILE": str(tmp / "state" / "auth.json")})
        self.app = create_app(self.settings, separate_fn)
        port = _free_port()
        self.base = f"http://127.0.0.1:{port}"
        self.uv = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="error"))
        threading.Thread(target=self.uv.run, daemon=True).start()
        for _ in range(200):
            if self.uv.started:
                break
            time.sleep(0.02)

    def req(self, method: str, path: str, body: bytes | None = None, token: str | None = TOKEN, js=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if js is not None:
            body, headers["Content-Type"] = json.dumps(js).encode(), "application/json"
        r = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def stop(self):
        self.uv.should_exit = True


def _fake_split(stems_audio: dict[str, np.ndarray], calls: list):
    def run(path, model, device, stems_dir):
        calls.append(path)
        h = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        d = Path(stems_dir) / f"{h}_{model}"
        d.mkdir(parents=True, exist_ok=True)
        out = {n: str(write_flac(d / f"{n}.flac", (x * 32767).astype(np.int16), SR)) for n, x in stems_audio.items()}
        write_manifest(d, out)
        return out, "cpu", None
    return run


@pytest.fixture
def stems_audio():
    seconds = 16.0
    return {
        "drums": _click_track(seconds, CLICKS_S),
        "bass": _click_track(seconds, [3.0], 80.0) * 0.5,
        "other": _click_track(seconds, [5.0], 500.0) * 0.5,
        "vocals": _click_track(seconds, [9.0], 1200.0) * 0.5,
    }


@pytest.fixture
def srv(tmp_path, stems_audio):
    calls: list = []
    s = _Srv(tmp_path, _fake_split(stems_audio, calls))
    s.calls = calls
    yield s
    s.stop()


@pytest.fixture
def track(tmp_path):
    body = b"ID3" + bytes(range(256)) * 64          # any bytes: the mock split never decodes it
    return hashlib.sha256(body).hexdigest()[:16], body


def _upload_and_split(srv, track):
    tid, body = track
    st, _, _ = srv.req("POST", f"/v1/tracks?id={tid}&ext=mp3", body)
    assert st == 200
    st, _, b = srv.req("POST", "/v1/separate", js={"id": tid, "stems": 4})
    assert st == 200, b
    job = json.loads(b)["job"]
    for _ in range(200):
        st, _, b = srv.req("GET", f"/v1/jobs/{job}")
        if json.loads(b)["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert json.loads(b)["status"] == "done", b
    return tid


def test_refuses_to_start_without_token(tmp_path):
    with pytest.raises(RuntimeError):
        Settings({"AIDJ_CACHE_DIR": str(tmp_path)})
    with pytest.raises(RuntimeError):
        Settings({"STEM_SERVER_TOKEN": "short", "AIDJ_CACHE_DIR": str(tmp_path)})


def test_auth_required_except_health(srv, track):
    assert srv.req("GET", "/health", token=None)[0] == 200
    tid, _ = track
    assert srv.req("GET", f"/v1/tracks/{tid}", token=None)[0] == 401
    assert srv.req("GET", f"/v1/tracks/{tid}", token="x" * 32)[0] == 401
    assert srv.req("GET", f"/v1/tracks/{tid}")[0] == 404
    assert srv.req("GET", "/v1/tracks/NOT-AN-ID")[0] == 400
    assert srv.req("GET", "/docs", token=None)[0] == 404           # no console / docs routes


def test_console_device_token_accepted(srv, track):
    """A device token from the console's store (same cache) works here; revoked -> 401."""
    from app.ui.services.auth import AuthStore

    tid, _ = track
    store = AuthStore(srv.settings.auth_store.path)
    tok, row = store.new_token("phone", sub="1234567890")
    assert srv.req("GET", f"/v1/tracks/{tid}", token=tok)[0] == 404          # past auth, unknown id
    assert srv.req("GET", f"/v1/tracks/{tid}")[0] == 404                      # STEM_SERVER_TOKEN still works
    assert store.revoke(row["id"])
    assert srv.req("GET", f"/v1/tracks/{tid}", token=tok)[0] == 401


def test_only_the_server_token_claims_a_deck_slot(srv, track):
    """A phone (device token) asking for "deck" is queued as "phone"; the console's static token may."""
    from app.ui.services.auth import AuthStore

    tid, body = track
    srv.req("POST", f"/v1/tracks?id={tid}&ext=mp3", body)
    tok, _ = AuthStore(srv.settings.auth_store.path).new_token("phone", sub="1234567890")
    st, _, b = srv.req("POST", "/v1/separate", token=tok, js={"id": tid, "stems": 4, "priority": "deck"})
    assert st == 200 and json.loads(b)["priority"] == "phone"
    assert srv.req("POST", "/v1/separate", js={"id": tid, "priority": "urgent"})[0] == 400
    st, _, b = srv.req("GET", "/v1/jobs")
    js = json.loads(b)
    assert st == 200 and {"jobs", "queued", "model", "device", "worker"} <= set(js)


def test_token_needs_32_chars(tmp_path):
    with pytest.raises(RuntimeError):
        Settings({"STEM_SERVER_TOKEN": "x" * 31, "AIDJ_CACHE_DIR": str(tmp_path)})
    assert Settings({"STEM_SERVER_TOKEN": "x" * 32, "AIDJ_CACHE_DIR": str(tmp_path)}).token == "x" * 32


def _xff(srv, ip: str, token: str):
    r = urllib.request.Request(srv.base + "/v1/tracks/0123456789abcdef", method="GET",
                               headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": ip})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def test_failed_auth_locks_the_ip(srv):
    """10 bad tokens from one client IP (X-Forwarded-For from Caddy on loopback) -> 429, even with
    the right token; another IP is unaffected."""
    for _ in range(9):
        assert _xff(srv, "203.0.113.7", "y" * 32) == 401
    assert _xff(srv, "203.0.113.7", "y" * 32) == 401          # 10th failure locks
    assert _xff(srv, "203.0.113.7", TOKEN) == 429
    assert _xff(srv, "198.51.100.9", TOKEN) == 404             # authenticated, unknown track
    assert srv.req("GET", "/health", token=None)[0] == 200     # health never limited


def test_xff_ignored_from_untrusted_peer():
    from app.stem_server.server import LIMITS
    from app.ui.services.auth import client_ip

    xff = {"x-forwarded-for": "1.2.3.4"}
    assert client_ip(LIMITS, {"client": ("127.0.0.1", 5)}, xff) == "1.2.3.4"
    assert client_ip(LIMITS, {"client": ("10.0.0.50", 5)}, xff) == "10.0.0.50"


def test_upload_verifies_hash_and_caps_size(srv, track):
    tid, body = track
    assert srv.req("POST", f"/v1/tracks?id={tid}&ext=mp3", body + b"x")[0] == 422
    assert srv.req("HEAD", f"/v1/tracks/{tid}")[0] == 404
    assert srv.req("POST", f"/v1/tracks?id={tid}&ext=exe", body)[0] == 400
    assert srv.req("POST", f"/v1/tracks?id={tid}&ext=mp3", body)[0] == 200
    assert srv.req("HEAD", f"/v1/tracks/{tid}")[0] == 200
    st, _, b = srv.req("GET", f"/v1/tracks/{tid}")
    assert json.loads(b) == {"id": tid, "separated": False, "model": srv.settings.model}
    assert (srv.cache / "songs" / tid / "audio.mp3").read_bytes() == body     # the console's song folder
    srv.settings.max_upload = 10
    big = b"y" * 100
    assert srv.req("POST", f"/v1/tracks?id={hashlib.sha256(big).hexdigest()[:16]}&ext=mp3", big)[0] == 413


def test_split_once_and_queue_dedup(srv, track):
    tid = _upload_and_split(srv, track)
    st, _, b = srv.req("POST", "/v1/separate", js={"id": tid, "stems": 2})
    assert json.loads(b)["status"] == "done"                 # cached: no second split
    assert len(srv.calls) == 1
    assert json.loads(srv.req("GET", f"/v1/tracks/{tid}")[2])["separated"] is True
    assert srv.req("POST", "/v1/separate", js={"id": tid, "stems": 3})[0] == 400


def test_serves_the_console_backfill_set_until_the_ft_set_lands(srv, track, stems_audio):
    """A song the console's old backfill split (htdemucs) is served at once, but a split request still
    makes the htdemucs_ft set (quality: ft for everything served); then ft is what is served."""
    from app.music_brain.audio.stem_service import DEMUCS_MODEL, FAST_MODEL

    tid, body = track
    srv.req("POST", f"/v1/tracks?id={tid}&ext=mp3", body)
    h = hashlib.sha256(body).hexdigest()
    d = srv.cache / "stems" / f"{h}_{FAST_MODEL}"
    d.mkdir(parents=True)
    write_manifest(d, {n: str(write_flac(d / f"{n}.flac", (x * 32767).astype(np.int16), SR))
                       for n, x in stems_audio.items()})
    assert json.loads(srv.req("GET", f"/v1/tracks/{tid}")[2]) == {"id": tid, "separated": True, "model": FAST_MODEL}
    st, hd, _ = srv.req("GET", f"/v1/tracks/{tid}/stems/drums.m4a?start=0&end=5")
    assert st == 200 and hd["X-Stem-Model"] == FAST_MODEL
    _upload_and_split(srv, track)
    assert len(srv.calls) == 1
    assert json.loads(srv.req("GET", f"/v1/tracks/{tid}")[2])["model"] == DEMUCS_MODEL
    st, hd, _ = srv.req("GET", f"/v1/tracks/{tid}/stems/drums.m4a?start=0&end=5")
    assert st == 200 and hd["X-Stem-Model"] == DEMUCS_MODEL


def test_analysis_route_uses_the_console_analyzer(srv, track, monkeypatch):
    from app.music_brain.analysis import analyzer

    seen = []

    class _A:
        def to_dict(self):
            return {"bpm": 124.0, "key": "8A"}

    monkeypatch.setattr(analyzer, "analyze", lambda p: seen.append(p) or _A())
    tid, body = track
    assert srv.req("GET", f"/v1/tracks/{tid}/analysis")[0] == 404
    assert srv.req("GET", f"/v1/tracks/{tid}/analysis", token=None)[0] == 401
    srv.req("POST", f"/v1/tracks?id={tid}&ext=mp3", body)
    st, _, b = srv.req("GET", f"/v1/tracks/{tid}/analysis")
    assert st == 200 and json.loads(b) == {"bpm": 124.0, "key": "8A"}
    assert seen and seen[0].parent.name == tid and seen[0].name == "audio.mp3"


def test_window_needs_split_first(srv, track):
    tid, body = track
    srv.req("POST", f"/v1/tracks?id={tid}&ext=mp3", body)
    assert srv.req("GET", f"/v1/tracks/{tid}/stems/drums.m4a?start=0&end=5")[0] == 409


def _decode(path: Path, raw) -> np.ndarray:
    args = ["ffmpeg", "-v", "error"]
    if raw == "no-elst":   # ignores the edit list but the decoder still drops the end padding
        args += ["-ignore_editlist", "1"]
    elif raw:              # ignores the edit list and keeps every decoded sample (MediaCodec loop)
        args += ["-flags2", "skip_manual", "-ignore_editlist", "1"]
    args += ["-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"]
    pcm = subprocess.run(args, check=True, capture_output=True).stdout
    x = np.frombuffer(pcm, dtype="<f4").reshape(-1, 2)
    return x[:apply_end_trim(path, len(x))] if raw is False else x   # see ffmpeg_compat.apply_end_trim


@pytest.mark.parametrize("raw", [False, True, "no-elst"], ids=["edit-list-decoder", "raw-decoder", "no-edit-list"])
def test_window_is_sample_exact(srv, track, tmp_path, raw):
    tid = _upload_and_split(srv, track)
    start, end = 2.0, 12.5
    st, h, body = srv.req("GET", f"/v1/tracks/{tid}/stems/drums.m4a?start={start}&end={end}")
    assert st == 200
    assert hashlib.sha256(body).hexdigest() == h["X-Stem-Sha256"]
    s0, n = int(h["X-Stem-Start-Frame"]), int(h["X-Stem-Frames"])
    assert (s0, n) == (round(start * SR), round(end * SR) - round(start * SR))
    assert int(h["X-Stem-Sample-Rate"]) == SR and int(h["X-Stem-Encoder-Delay"]) == windows.ENCODER_DELAY
    m4a = tmp_path / "w.m4a"
    m4a.write_bytes(body)
    x = _decode(m4a, raw)
    if raw:
        assert len(x) >= n + windows.ENCODER_DELAY       # the priming really is there
    skip = leading_skip(len(x), n, int(h["X-Stem-Encoder-Delay"]), int(h["X-Stem-Encoder-Padding"]))
    if not raw:
        assert len(x) == n and skip == 0                 # edit list: exact window, nothing to trim
    x = x[skip:skip + n]
    want = [round(t * SR) - s0 for t in CLICKS_S if start <= t and t + 0.01 < end]
    got = _onsets(x)
    assert len(got) == len(want), (got, want)
    err_ms = [abs(g - w) / SR * 1000 for g, w in zip(got, want)]
    assert max(err_ms) < 1.0, err_ms


def test_no_vocals_is_the_sum_and_windows_are_cached(srv, track, stems_audio):
    tid = _upload_and_split(srv, track)
    st, h, b1 = srv.req("GET", f"/v1/tracks/{tid}/stems/no_vocals.m4a?start=0&end=10")
    assert st == 200
    st, h2, b2 = srv.req("GET", f"/v1/tracks/{tid}/stems/no_vocals.m4a?start=0&end=10")
    assert b1 == b2 and h["X-Stem-Sha256"] == h2["X-Stem-Sha256"]
    assert srv.req("GET", f"/v1/tracks/{tid}/stems/piano.m4a?start=0&end=10")[0] == 404
    assert srv.req("GET", f"/v1/tracks/{tid}/stems/drums.m4a?start=5&end=1")[0] == 400
    assert srv.req("GET", f"/v1/tracks/{tid}/stems/drums.m4a?start=99&end=120")[0] == 416
    p = srv.cache / "w.m4a"
    p.write_bytes(b1)
    on = _onsets(_decode(p, False), rel=0.2)
    # drums clicks at 1.0 / 2.5 / 7.25, bass at 3.0, other at 5.0; vocals (9.0) left out
    assert [round(i / SR, 2) for i in on] == [1.0, 2.5, 3.0, 5.0, 7.25]


def test_whole_stem_is_one_cached_file_with_range_resume(srv, track, tmp_path):
    tid, body = track
    srv.req("POST", f"/v1/tracks?id={tid}&ext=mp3", body)
    assert srv.req("GET", f"/v1/tracks/{tid}/stems/drums.m4a")[0] == 409          # not split yet
    tid = _upload_and_split(srv, track)
    assert srv.req("GET", f"/v1/tracks/{tid}/stems/drums.m4a?start=3")[0] == 400   # both or neither
    assert srv.req("GET", f"/v1/tracks/{tid}/stems/piano.m4a")[0] == 404
    st, h, whole = srv.req("GET", f"/v1/tracks/{tid}/stems/drums.m4a")
    assert st == 200 and h["Accept-Ranges"] == "bytes" and int(h["Content-Length"]) == len(whole)
    assert hashlib.sha256(whole).hexdigest() == h["X-Stem-Sha256"]
    total = int(h["X-Stem-Total-Frames"])
    assert int(h["X-Stem-Start-Frame"]) == 0 and int(h["X-Stem-Frames"]) == total == 16 * SR
    p = tmp_path / "whole.m4a"
    p.write_bytes(whole)
    x = _decode(p, False)
    assert len(x) == total                                                         # exact length, edit list honoured
    got = _onsets(x)
    assert max(abs(g - round(t * SR)) for g, t in zip(got, CLICKS_S)) < SR / 1000  # within 1 ms
    # encoded once: the other stems were encoded with it, a second request is a file read
    files = sorted(f.name for f in (srv.cache / "stem_windows").rglob("*_whole.m4a"))
    assert files == ["bass_whole.m4a", "drums_whole.m4a", "other_whole.m4a", "vocals_whole.m4a"]
    assert srv.req("GET", f"/v1/tracks/{tid}/stems/drums.m4a")[2] == whole
    # Range resume: the second half joins the first byte for byte
    cut = len(whole) // 3
    req = urllib.request.Request(srv.base + f"/v1/tracks/{tid}/stems/drums.m4a",
                                 headers={"Authorization": f"Bearer {TOKEN}", "Range": f"bytes={cut}-"})
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.status == 206 and r.headers["Content-Range"] == f"bytes {cut}-{len(whole) - 1}/{len(whole)}"
        assert whole[:cut] + r.read() == whole
    req = urllib.request.Request(srv.base + f"/v1/tracks/{tid}/stems/drums.m4a",
                                 headers={"Authorization": f"Bearer {TOKEN}", "Range": f"bytes={len(whole) + 5}-"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=30)
    assert e.value.code == 416


def test_corrupt_whole_file_is_re_encoded_not_served(srv, track):
    tid = _upload_and_split(srv, track)
    st, h, good = srv.req("GET", f"/v1/tracks/{tid}/stems/vocals.m4a")
    f = next((srv.cache / "stem_windows").rglob("vocals_whole.m4a"))
    bad = bytearray(f.read_bytes())
    bad[len(bad) // 2] ^= 0xFF
    f.write_bytes(bytes(bad))
    st, h2, again = srv.req("GET", f"/v1/tracks/{tid}/stems/vocals.m4a")
    assert st == 200 and hashlib.sha256(again).hexdigest() == h2["X-Stem-Sha256"]


def test_corrupt_window_and_stem_are_never_served(srv, track):
    tid = _upload_and_split(srv, track)
    path = f"/v1/tracks/{tid}/stems/vocals.m4a?start=0&end=10"
    st, h, good = srv.req("GET", path)
    win = next((srv.cache / "stem_windows").rglob("vocals_*.m4a"))
    bad = bytearray(win.read_bytes())
    bad[len(bad) // 2] ^= 0xFF
    win.write_bytes(bytes(bad))
    st, h2, again = srv.req("GET", path)                    # mismatch -> re-encoded, never the flipped file
    assert st == 200 and hashlib.sha256(again).hexdigest() == h2["X-Stem-Sha256"]
    # a flipped stem: refuse, drop the set, re-split
    stem = next((srv.cache / "stems").rglob("drums.flac"))
    data = bytearray(stem.read_bytes())
    data[-100] ^= 0xFF
    stem.write_bytes(bytes(data))
    st, _, b = srv.req("GET", f"/v1/tracks/{tid}/stems/drums.m4a?start=0&end=10")
    assert st == 409 and b"re-splitting" in b
    for _ in range(200):
        if len(srv.calls) == 2 and json.loads(srv.req("GET", f"/v1/tracks/{tid}")[2])["separated"]:
            break
        time.sleep(0.02)
    assert len(srv.calls) == 2
    assert srv.req("GET", f"/v1/tracks/{tid}/stems/drums.m4a?start=0&end=10")[0] == 200


def test_cuda_oom_falls_back_to_cpu(monkeypatch, tmp_path):
    from app.music_brain.audio import stem_service
    from app.stem_server import server
    from app.stem_server.demucs_runner import OOM_EXIT

    seen = []

    def fake(path, model, device, runner, force=False):
        seen.append(device)
        if device == "cuda":
            raise subprocess.CalledProcessError(OOM_EXIT, ["demucs"])
        return stem_service.StemResult("h", model, None, {"vocals": "v"}, "d", False)

    monkeypatch.setattr(stem_service, "separate", lambda p, model, device, runner, force=False: fake(p, model, device, runner, force))
    stems, dev, note = server.separate_once(tmp_path / "x.mp3", "htdemucs_ft", "cuda", tmp_path)
    assert seen == ["cuda", "cpu"] and dev == "cpu" and "out of memory" in note


def test_weights_checked_by_sha256(tmp_path):
    from app.stem_server.demucs_runner import verify_weights

    good = b"weights"
    h = hashlib.sha256(good).hexdigest()
    (tmp_path / f"abcd1234-{h[:8]}.th").write_bytes(good)
    (tmp_path / f"abcd5678-{h[:8]}.th").write_bytes(b"flipped")
    assert verify_weights(tmp_path) == [f"abcd5678-{h[:8]}.th"]
    assert (tmp_path / f"abcd1234-{h[:8]}.th").exists() and not (tmp_path / f"abcd5678-{h[:8]}.th").exists()


@pytest.mark.slow
def test_real_split_on_this_host(tmp_path):
    """Real htdemucs_ft through the runner (GPU when present): 20 s of noise -> a window decodes."""
    import os

    from app.music_brain.audio import stem_service
    from app.stem_server.server import separate_once

    os.environ.setdefault("STEM_SERVER_VRAM_GB", "6")
    src = tmp_path / "noise.wav"
    sf.write(src, (np.random.default_rng(0).standard_normal((20 * SR, 2)) * 0.1).astype(np.float32), SR)
    stems, dev, _ = separate_once(src, stem_service.DEMUCS_MODEL, None, tmp_path)
    assert set(stems) == {"vocals", "drums", "bass", "other"}
    meta = windows.encode_m4a(windows.read_window([stems["drums"]], 0, 5 * SR), tmp_path / "w.m4a")
    assert meta["frames"] == 5 * SR and len(_decode(tmp_path / "w.m4a", False)) == 5 * SR
