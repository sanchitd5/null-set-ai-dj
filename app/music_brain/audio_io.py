"""Cache audio format: FLAC for generated stems and key-locked renders, WAV still read.

research/notes/audio-format-study.md measured FLAC level 5 at 0.42x the size of the 16-bit Demucs WAV
(bit-exact) and 0.46x of the float keylock WAV as 24-bit FLAC (exact length, residual -143 dB). Old
cache entries stay WAV until `python3 -m app.music_brain.audio_convert --apply` rewrites them, so every
reader resolves a stem by trying `.flac` first and falling back to `.wav`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

import numpy as np

STEM_EXTS = (".flac", ".wav")                  # reader preference order
FLAC_LEVEL = 5                                 # FLAC's own 0..8 scale
# libsndfile takes the level as 0..1 and maps it onto FLAC's 0..8
_SF_LEVEL = FLAC_LEVEL / 8.0
STEM_SUBTYPE = "PCM_16"                        # Demucs stems (16-bit, bit-exact vs the old WAV)
RENDER_SUBTYPE = "PCM_24"                      # keylock renders (Rubber Band float output)
MEDIA_TYPES = {".flac": "audio/flac", ".wav": "audio/wav"}


def stem_file(folder: Path | str, name: str) -> Optional[Path]:
    """`folder/name.flac`, else `folder/name.wav`, else None."""
    for ext in STEM_EXTS:
        p = Path(folder) / f"{name}{ext}"
        if p.exists():
            return p
    return None


def resolve(path: Path | str) -> Optional[Path]:
    """An existing file for a recorded stem path: the path itself, else its FLAC/WAV sibling (a
    manifest written before a conversion, or a reader holding an old path). None when neither exists."""
    p = Path(path)
    if p.exists():
        return p
    if p.suffix.lower() in STEM_EXTS:
        for ext in STEM_EXTS:
            q = p.with_suffix(ext)
            if q.exists():
                return q
    return None


def media_type(path: Path | str) -> str:
    return MEDIA_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def write_flac(path: Path | str, data: np.ndarray, sr: int, subtype: str = STEM_SUBTYPE) -> Path:
    """Write FLAC atomically (tmp file in the same dir, then os.replace): a reader never sees half a file."""
    import soundfile as sf

    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        with sf.SoundFile(str(tmp), "w", samplerate=int(sr), channels=_channels(data),
                          format="FLAC", subtype=subtype, compression_level=_SF_LEVEL) as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def encode_file(src: Path | str, dest: Path | str, subtype: Optional[str] = None) -> Path:
    """Encode an audio file to FLAC. subtype None: PCM_24 for float/24-bit sources, else PCM_16."""
    import soundfile as sf

    info = sf.info(str(src))
    if subtype is None:
        subtype = RENDER_SUBTYPE if info.subtype in ("FLOAT", "DOUBLE", "PCM_24", "PCM_32") else STEM_SUBTYPE
    dtype = "int16" if subtype == "PCM_16" else "float64"
    data, sr = sf.read(str(src), dtype=dtype, always_2d=True)
    if subtype != "PCM_16":
        data = np.clip(data, -1.0, 1.0)            # 24-bit int cannot hold > 1.0 (study: keylock peaks <= 1.0)
    return write_flac(dest, data, sr, subtype)


def _channels(data: np.ndarray) -> int:
    return 1 if data.ndim == 1 else int(data.shape[1])


# ---- stem cache manifest -------------------------------------------------------------------------
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 2   # v2: {"version", "format", "stems": {name: path}}; v1 was the bare {name: path} (WAV)


def manifest_stems(manifest: object) -> Optional[Dict[str, str]]:
    """{name: path} from a v2 or a v1 manifest, None when it is neither."""
    if isinstance(manifest, dict) and isinstance(manifest.get("version"), int):
        manifest = manifest.get("stems")
    if not isinstance(manifest, dict) or not all(isinstance(v, str) for v in manifest.values()):
        return None
    return manifest


def read_manifest(cache_dir: Path | str) -> Optional[Dict[str, str]]:
    """{name: existing path} of a stem cache dir (v1 or v2 manifest). A path whose file moved to its
    FLAC/WAV sibling (a conversion) resolves to the sibling. None when any stem is missing."""
    try:
        with open(Path(cache_dir) / MANIFEST_NAME, "r", encoding="utf-8") as f:
            stems = manifest_stems(json.load(f))
    except (OSError, ValueError):
        return None
    if not stems:
        return None
    out = {}
    for name, p in stems.items():
        real = resolve(p)
        if real is None:
            return None
        out[name] = str(real)
    return out


def write_manifest(cache_dir: Path | str, stems: Dict[str, str]) -> None:
    """Atomic (tmp + replace): the manifest is the cache-hit marker, a reader never sees half of one."""
    fmt = sorted({Path(p).suffix.lower().lstrip(".") for p in stems.values()})
    body = {"version": MANIFEST_VERSION, "format": fmt[0] if len(fmt) == 1 else "mixed", "stems": stems}
    path = Path(cache_dir) / MANIFEST_NAME
    tmp = path.with_name(f".{MANIFEST_NAME}.tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
