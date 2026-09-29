"""Synthetic audio for the virtual set: real WAV files with the measured energy of real songs.

The headless console decodes audio through its own code path (fetch -> decodeAudioData -> deck
buffers -> stem-moves.js stemEnergyBars / audibleRms). To keep that path real, the sim world
serves real files: a mix and four stems per song, made from the pool's per-second stem RMS
curves. Each stem is white noise scaled to its measured audible-band RMS plus a 60 Hz tone that
carries the rest of its energy (the sub the drums / bass stems really have). The stem-moves
measurements (full-band RMS, RMS above 150 Hz) then read back the numbers the real stems gave.

The files are 4 kHz mono 16-bit (the console reads the sample rate from the buffer). Each holds
a small "SIMT" chunk {hash, stem, ...} so that whatever decodes it knows which song and stem it
is: that is how the audible-effect log is tied back to the real energy curves.

Deterministic: the noise is seeded from the song's hash and the stem name.
"""
from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path
from typing import Optional

import numpy as np

SR = 4000
STEMS = ("drums", "bass", "vocals", "other")
LOW_HZ = 60.0


def write_wav(path: Path, x: np.ndarray, sr: int = SR, tag: Optional[dict] = None) -> None:
    """16-bit mono PCM, with an optional SIMT chunk between fmt and data."""
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    chunks = [b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)]
    if tag is not None:
        body = json.dumps(tag, separators=(",", ":"), sort_keys=True).encode("utf-8")
        pad = b"\0" if len(body) % 2 else b""
        chunks.append(b"SIMT" + struct.pack("<I", len(body)) + body + pad)
    chunks.append(b"data" + struct.pack("<I", len(pcm)) + pcm)
    payload = b"WAVE" + b"".join(chunks)
    Path(path).write_bytes(b"RIFF" + struct.pack("<I", len(payload)) + payload)


def read_tag(path: Path) -> Optional[dict]:
    """The SIMT tag of a synthetic file (reads only the head), or None for any other file."""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return None
    i = head.find(b"SIMT")
    if i < 0 or head[:4] != b"RIFF":
        return None
    n = struct.unpack("<I", head[i + 4:i + 8])[0]
    try:
        return json.loads(head[i + 8:i + 8 + n].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _seed(hash_: str, stem: str) -> int:
    return zlib.crc32(f"{hash_}:{stem}".encode("utf-8"))


def _curve_at(vals, n: int, sr: int) -> np.ndarray:
    """Per-second values -> per-sample, linear between second centres."""
    v = np.asarray(vals if len(vals) else [0.0], dtype=np.float64)
    t = (np.arange(n) / sr)
    return np.interp(t, np.arange(len(v)) + 0.5, v)


def stem_signal(hash_: str, stem: str, rms, aud, n: int, sr: int = SR) -> np.ndarray:
    rng = np.random.RandomState(_seed(hash_, stem))
    noise = rng.standard_normal(n)
    a = _curve_at(aud, n, sr)
    r = _curve_at(rms, n, sr)
    low = np.sqrt(np.maximum(0.0, r * r - a * a))
    tone = np.sin(2 * np.pi * LOW_HZ * np.arange(n) / sr)
    return noise * a + tone * low * np.sqrt(2.0)


def synth_track(entry: dict, out_dir: Path, name: Optional[str] = None) -> dict:
    """Write the mix (and, when the entry has stem curves, the four stems). -> {mix, stems}"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    h = entry["hash"]
    dur = float(entry["analysis"].get("duration") or 0.0)
    n = int(dur * SR)
    curves = entry.get("stems")
    stems_paths: dict = {}
    if curves:
        parts = {}
        for s in STEMS:
            parts[s] = stem_signal(h, s, curves["rms"][s], curves["aud"][s], n)
            p = out_dir / f"{s}.wav"
            write_wav(p, parts[s], SR, {"hash": h, "stem": s})
            stems_paths[s] = str(p)
        mix = sum(parts.values())
    else:
        e = entry["analysis"].get("energy_curve") or []
        et = entry["analysis"].get("energy_times") or []
        env = np.interp(np.arange(n) / SR, et, e) if len(e) else np.full(n, 0.3)
        mix = np.random.RandomState(_seed(h, "mix")).standard_normal(n) * env * 0.12
    mp = out_dir / f"{name or 'mix'}.wav"
    write_wav(mp, mix, SR, {"hash": h, "stem": "mix"})
    return {"mix": str(mp), "stems": stems_paths}
