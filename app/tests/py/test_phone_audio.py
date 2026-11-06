"""Phone-sized audio (app/ui/services/phone_audio.py, ?q=phone on the audio routes) and Range/206."""
import shutil
import subprocess
import wave

import numpy as np
import pytest

from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import phone_audio

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg not installed")
TID = "abcdef0123456789"
SR = 44100


def _wav(path, secs=6.0, sr=SR):
    n = int(secs * sr)
    x = 0.3 * np.sin(2 * np.pi * 220 * np.arange(n) / sr)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.stack([x, x], 1) * 32767).astype("<i2").tobytes())
    return path


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                          "stream=codec_name,sample_rate,channels", "-of", "default=nw=1", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return dict(line.split("=", 1) for line in out.split())


def _decoded_samples(path):
    """Samples ffmpeg decodes, edit list applied (what Chrome / Safari decodeAudioData return)."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-"],
                         capture_output=True, check=True).stdout
    return len(raw) // 2


@pytest.fixture
def client(tmp_path, monkeypatch):
    src = _wav(tmp_path / "song.wav")
    monkeypatch.setitem(server._tracks, TID, src)
    monkeypatch.setattr(server, "PHONE_AUDIO_DIR", tmp_path / "phone_audio")
    return TestClient(server.app), src, tmp_path / "phone_audio"


@needs_ffmpeg
def test_transcode_is_aac_m4a_sample_aligned_and_cached(tmp_path):
    src = _wav(tmp_path / "a.wav")
    out = phone_audio.phone_variant(src, TID, tmp_path / "cache")
    assert out and out.suffix == ".m4a" and out.stat().st_size < src.stat().st_size / 3
    p = _probe(out)
    assert p["codec_name"] == "aac" and p["sample_rate"] == str(SR) and p["channels"] == "2"
    # the edit list drops the encoder priming and padding: same sample count as the WAV
    assert _decoded_samples(out) == int(6.0 * SR)
    mtime = out.stat().st_mtime_ns
    assert phone_audio.phone_variant(src, TID, tmp_path / "cache") == out and out.stat().st_mtime_ns == mtime
    assert not [p for p in (tmp_path / "cache").iterdir() if p.name.startswith(".")]   # no tmp left


def test_cache_key_follows_source_and_settings(tmp_path, monkeypatch):
    src = _wav(tmp_path / "a.wav")
    k1 = phone_audio.cache_key(src, TID)
    _wav(src, secs=3.0)                                    # re-rendered source: new key
    k2 = phone_audio.cache_key(src, TID)
    monkeypatch.setattr(phone_audio, "SETTINGS", "other")
    assert len({k1, k2, phone_audio.cache_key(src, TID)}) == 3
    assert "/" not in phone_audio.cache_key(src, "../../etc/x") and ".." not in phone_audio.cache_key(src, "../x").split(".")[0]


def test_no_ffmpeg_serves_the_original(client, monkeypatch):
    c, src, _ = client
    monkeypatch.setattr(phone_audio.shutil, "which", lambda name: None)
    r = c.get(f"/api/audio/tracks/{TID}?q=phone")
    assert r.status_code == 200 and r.content == src.read_bytes()


@needs_ffmpeg
def test_route_q_phone_and_bad_q(client):
    c, src, cache = client
    r = c.get(f"/api/audio/tracks/{TID}?q=phone")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mp4"
    assert len(r.content) < src.stat().st_size / 3 and len(list(cache.glob("*.m4a"))) == 1
    assert c.get(f"/api/audio/tracks/{TID}").content == src.read_bytes()           # no q: the WAV, as before
    assert c.get(f"/api/audio/tracks/{TID}?q=tiny").status_code == 400


@needs_ffmpeg
@pytest.mark.parametrize("q", ["", "?q=phone"])
def test_range_answers_206(client, q):
    c, _, _ = client
    whole = c.get(f"/api/audio/tracks/{TID}{q}").content
    r = c.get(f"/api/audio/tracks/{TID}{q}", headers={"Range": "bytes=1000-"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 1000-{len(whole) - 1}/{len(whole)}"
    assert r.content == whole[1000:]
    assert c.get(f"/api/audio/tracks/{TID}{q}").headers.get("accept-ranges") == "bytes"
