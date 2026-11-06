"""The phone copy's sha256 sidecar: correct hash, reused, refreshed when the copy changes."""
import hashlib
import os
import time

from app.ui.services import phone_audio


def test_sha256_sidecar(tmp_path):
    f = tmp_path / "x.m4a"
    f.write_bytes(b"abc" * 1000)
    want = hashlib.sha256(f.read_bytes()).hexdigest()
    assert phone_audio.sha256_of(f) == want
    side = tmp_path / "x.m4a.sha256"
    assert side.read_text() == want
    # reused from the sidecar
    side.write_text("0" * 64)
    os.utime(side, None)
    assert phone_audio.sha256_of(f) == "0" * 64
    # a newer copy invalidates it
    time.sleep(0.01)
    f.write_bytes(b"def" * 1000)
    os.utime(side, ns=(1, 1))
    assert phone_audio.sha256_of(f) == hashlib.sha256(f.read_bytes()).hexdigest()
