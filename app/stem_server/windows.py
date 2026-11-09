"""AAC (m4a) windows of cached stems, plus sha256 sidecars that guard every cached file.

Timing contract (the phone mixes stems sample-aligned against the original track):
- A window covers frames [start_frame, start_frame + frames) of the stem at 44.1 kHz.
- ffmpeg's AAC encoder primes the stream with ENCODER_DELAY (1024) samples and pads the last
  AAC frame; the m4a carries an edit list (elst media_time = 1024, duration = frames), so a
  decoder that honours it (ffmpeg, AVFoundation) yields exactly `frames` samples starting at
  start_frame. A raw decoder (Android MediaExtractor + MediaCodec loop) yields the priming too.
- Every response therefore also states frames / encoder_delay / encoder_padding, and the client
  trims by them (see the mobile `leadingSkip`), so either decoder lands on the same sample.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from app.music_brain.generated import dj_config as DJ

SAMPLE_RATE = 44100
ENCODER_DELAY = 1024        # ffmpeg native aac: initial_padding (verified by app/tests/py/test_stem_server.py)
AAC_FRAME = 1024
BITRATE = "256k"
MAX_WINDOW_S = float(DJ.STEM_WINDOW_MAX_S)        # one request never encodes more than 10 min


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# ---- stem integrity sidecar (host RAM is non-ECC: a flipped bit must never be served) -------------
SIDECAR = ".stem_server_sha256.json"


def write_sidecar(cache_dir: Path, stems: Dict[str, str]) -> Dict[str, str]:
    """Record sha256 of each stem file. A set made elsewhere (the console) is decoded in full first,
    so a FLAC whose frames fail their CRC is not trusted on first use."""
    import soundfile as sf

    sums = {}
    for name, p in stems.items():
        with sf.SoundFile(p) as f:          # full decode: libsndfile rejects a corrupt FLAC frame
            while f.read(1 << 18, dtype="int16").size:
                pass
        sums[name] = sha256_file(p)
    atomic_write_text(Path(cache_dir) / SIDECAR, json.dumps(sums, indent=2))
    return sums


def verify_stems(cache_dir: Path, stems: Dict[str, str], names: List[str]) -> Optional[str]:
    """None when every named stem matches its recorded sha256 (sidecar written on first use);
    else the reason it does not."""
    side = Path(cache_dir) / SIDECAR
    try:
        sums = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        try:
            sums = write_sidecar(cache_dir, stems)
        except Exception as exc:  # noqa: BLE001 -- unreadable stem = corrupt
            return f"stem set unreadable: {exc}"
    for n in names:
        if n not in stems or n not in sums:
            return f"stem {n} missing"
        if sha256_file(stems[n]) != sums[n]:
            return f"stem {n} sha256 mismatch"
    return None


# ---- window read + encode --------------------------------------------------------------------------
def total_frames(path: str) -> int:
    import soundfile as sf

    return int(sf.info(path).frames)


def read_window(paths: List[str], s0: int, s1: int) -> np.ndarray:
    """Sum of the given stems over frames [s0, s1) as float32 (n, 2). One path = that stem;
    drums+bass+other = no_vocals (htdemucs_ft is always split 4-way, see server._two_from_four)."""
    import soundfile as sf

    out = None
    for p in paths:
        with sf.SoundFile(p) as f:
            if f.samplerate != SAMPLE_RATE:
                raise ValueError(f"stem sample rate {f.samplerate}, expected {SAMPLE_RATE}")
            f.seek(s0)
            x = f.read(s1 - s0, dtype="float32", always_2d=True)
        if x.shape[1] == 1:
            x = np.repeat(x, 2, axis=1)
        out = x if out is None else out + x
    return np.clip(out, -1.0, 1.0)


def encode_m4a(pcm: np.ndarray, dest: Path) -> dict:
    """AAC-LC 256 kbps 44.1 kHz stereo m4a, written atomically, with a meta sidecar holding the
    sha256 of the m4a and the priming / padding the client trims by."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = int(pcm.shape[0])
    fd, tmp = tempfile.mkstemp(prefix=f".{dest.stem}.", suffix=".m4a", dir=dest.parent)
    os.close(fd)
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y",
             "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "2", "-i", "pipe:0",
             "-c:a", "aac", "-b:a", BITRATE, "-ar", str(SAMPLE_RATE), "-ac", "2",
             "-movflags", "+faststart", tmp],
            input=np.ascontiguousarray(pcm, dtype="<f4").tobytes(), check=True, capture_output=True)
        packets = int(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-count_packets",
             "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", tmp],
            check=True, capture_output=True, text=True).stdout.strip().rstrip(","))
        meta = {"frames": n, "sample_rate": SAMPLE_RATE, "encoder_delay": ENCODER_DELAY,
                "encoder_padding": max(0, packets * AAC_FRAME - ENCODER_DELAY - n),
                "sha256": sha256_file(tmp)}
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return meta


def encode_whole_m4a(stem_path: str, dest: Path, block_s: float = 30.0) -> dict:
    """Whole stem -> m4a (same codec, bitrate, priming contract and meta as [encode_m4a]), streamed to
    ffmpeg in blocks so a long song never sits in memory as one float array."""
    import soundfile as sf

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dest.stem}.", suffix=".m4a", dir=dest.parent)
    os.close(fd)
    try:
        proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-v", "error", "-y",
             "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "2", "-i", "pipe:0",
             "-c:a", "aac", "-b:a", BITRATE, "-ar", str(SAMPLE_RATE), "-ac", "2",
             "-movflags", "+faststart", tmp],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        n = 0
        try:
            with sf.SoundFile(stem_path) as f:
                if f.samplerate != SAMPLE_RATE:
                    raise ValueError(f"stem sample rate {f.samplerate}, expected {SAMPLE_RATE}")
                step = int(block_s * SAMPLE_RATE)
                while True:
                    x = f.read(step, dtype="float32", always_2d=True)
                    if not x.shape[0]:
                        break
                    if x.shape[1] == 1:
                        x = np.repeat(x, 2, axis=1)
                    n += int(x.shape[0])
                    proc.stdin.write(np.ascontiguousarray(np.clip(x, -1.0, 1.0), dtype="<f4").tobytes())
        finally:
            proc.stdin.close()
            err = proc.stderr.read()
            if proc.wait() != 0:
                raise RuntimeError(f"ffmpeg failed: {err.decode(errors='replace')[:200]}")
        packets = int(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-count_packets",
             "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", tmp],
            check=True, capture_output=True, text=True).stdout.strip().rstrip(","))
        meta = {"frames": n, "sample_rate": SAMPLE_RATE, "encoder_delay": ENCODER_DELAY,
                "encoder_padding": max(0, packets * AAC_FRAME - ENCODER_DELAY - n),
                "sha256": sha256_file(tmp)}
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return meta


def cached_window(dest: Path) -> Optional[dict]:
    """Meta of a cached window when the m4a still matches its sha256; a mismatch deletes both."""
    meta_p = dest.with_suffix(".json")
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        if sha256_file(dest) == meta.get("sha256"):
            return meta
    except (OSError, ValueError):
        pass
    dest.unlink(missing_ok=True)
    meta_p.unlink(missing_ok=True)
    return None


def store_window_meta(dest: Path, meta: dict) -> None:
    atomic_write_text(dest.with_suffix(".json"), json.dumps(meta))
