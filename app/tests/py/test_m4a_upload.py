"""m4a (phone upload) must be readable: transcoded to FLAC at console upload, decoded on demand elsewhere."""

import shutil
import subprocess

import librosa
import pytest

from app.music_brain.audio import audio_io
from app.tests.py.testclient_compat import TestClient

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")


@pytest.fixture()
def m4a(tmp_path):
    out = tmp_path / "tone.m4a"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "aac", str(out)], check=True)
    return out


def test_readable_decodes_m4a_and_passes_native(m4a, tmp_path):
    flac = audio_io.readable(m4a, tmp_path / "dec")
    assert flac.suffix == ".flac" and flac.parent == tmp_path / "dec"
    y, _ = librosa.load(str(flac), sr=22050, mono=True)
    assert 1.8 < len(y) / 22050 < 2.3
    assert audio_io.readable(flac, tmp_path / "dec") == flac
    assert audio_io.readable(m4a, tmp_path / "dec") == flac       # cached


def test_readable_falls_back_on_garbage(tmp_path):
    bad = tmp_path / "bad.m4a"
    bad.write_bytes(b"not audio")
    assert audio_io.readable(bad, tmp_path / "dec") == bad


def test_upload_transcodes_m4a(m4a, tmp_path, monkeypatch):
    from app.ui import server

    monkeypatch.setattr(server, "UPLOAD_DIR", tmp_path / "up")
    (tmp_path / "up").mkdir()
    monkeypatch.setattr(server, "_queue_stems", lambda *a, **k: None)
    with open(m4a, "rb") as f:
        res = TestClient(server.app).post("/api/tracks", files={"file": ("tone.m4a", f, "audio/mp4")})
    assert res.status_code == 200
    tid = res.json()["track_id"]
    stored = server._tracks[tid]
    assert stored.suffix == ".flac" and stored.exists()
    assert not (tmp_path / "up" / f"{tid}.m4a").exists()
    y, _ = librosa.load(str(stored), sr=22050, mono=True)
    assert len(y) > 22050
