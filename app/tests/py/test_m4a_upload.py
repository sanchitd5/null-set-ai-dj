"""Audio is stored as delivered (m4a stays m4a); the server decodes it in memory through audio_io."""

import shutil
import subprocess

import pytest

from app.music_brain.audio import audio_io
from app.tests.py.testclient_compat import TestClient

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg missing")


@pytest.fixture()
def m4a(tmp_path):
    out = tmp_path / "tone.m4a"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "aac", str(out)], check=True)
    return out


@pytest.fixture()
def beat_m4a(tmp_path):
    """20 s of 120 BPM kicks (a sine tone has no beats to analyse)."""
    import numpy as np
    import soundfile as sf

    sr, t = 44100, np.arange(44100 * 20) / 44100
    y = np.zeros_like(t)
    for k in range(40):
        i = int(k * 0.5 * sr)
        n = min(4000, len(y) - i)
        y[i:i + n] += np.sin(2 * np.pi * 70 * t[:n]) * np.exp(-t[:n] * 30)
    wav = tmp_path / "beat.wav"
    sf.write(str(wav), y.astype("float32") * 0.8, sr)
    out = tmp_path / "beat.m4a"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(wav), "-c:a", "aac", str(out)], check=True)
    return out


def test_audio_info_and_load_m4a(m4a):
    frames, sr, ch, dur = audio_io.audio_info(m4a)
    assert sr == 44100 and ch == 1 and 1.8 < dur < 2.3 and abs(frames - dur * sr) < 2
    y, rate = audio_io.load_audio(m4a, sr=22050, mono=True)
    assert rate == 22050 and y.ndim == 1 and 1.8 < len(y) / 22050 < 2.3 and abs(y).max() > 0.1
    w, rate = audio_io.load_audio(m4a, mono=True, start=0.5, stop=1.0)
    assert rate == 44100 and abs(len(w) - 22050) < 2200


def test_load_audio_native_matches_shape(tmp_path):
    import numpy as np
    import soundfile as sf

    p = tmp_path / "t.wav"
    sf.write(str(p), np.random.default_rng(0).uniform(-.5, .5, (44100, 2)).astype("float32"), 44100)
    y, sr = audio_io.load_audio(p)
    assert y.shape == (44100, 2) and sr == 44100
    y, sr = audio_io.load_audio(p, sr=22050, mono=True, start=0.25, stop=0.75)
    assert sr == 22050 and y.ndim == 1 and abs(len(y) - 11025) < 3
    assert audio_io.audio_info(p)[:3] == (44100, 44100, 2)


def test_garbage_gives_clear_oserror(tmp_path):
    bad = tmp_path / "bad.m4a"
    bad.write_bytes(b"not audio")
    with pytest.raises(OSError, match="bad.m4a"):
        audio_io.audio_info(bad)
    with pytest.raises(OSError, match="bad.m4a"):
        audio_io.load_audio(bad)
    badw = tmp_path / "bad.wav"
    badw.write_bytes(b"not audio")
    with pytest.raises(OSError, match="bad.wav"):
        audio_io.load_audio(badw)


def test_upload_keeps_m4a_and_serves_it(beat_m4a, tmp_path, monkeypatch):
    from app.ui import server

    monkeypatch.setattr(server, "UPLOAD_DIR", tmp_path / "up")
    (tmp_path / "up").mkdir()
    monkeypatch.setattr(server, "_queue_stems", lambda *a, **k: None)
    client = TestClient(server.app)
    with open(beat_m4a, "rb") as f:
        res = client.post("/api/tracks", files={"file": ("beat.m4a", f, "audio/mp4")})
    assert res.status_code == 200
    tid = res.json()["track_id"]
    stored = server._tracks[tid]
    assert stored.suffix == ".m4a" and stored.read_bytes() == beat_m4a.read_bytes()
    assert not list((tmp_path / "up").glob("*.flac"))
    assert audio_io.audio_info(stored)[3] > 19
    from app.music_brain.analysis.analyzer import analyze

    assert analyze(stored, use_cache=False).duration > 19
    got = client.get(f"/api/audio/tracks/{tid}")
    assert got.status_code == 200 and got.headers["content-type"].startswith("audio/mp4")
