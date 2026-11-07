"""Cache audio format: FLAC for generated stems and key-locked renders, WAV still read.

research/notes/audio-format-study.md measured FLAC level 5 at 0.42x the size of the 16-bit Demucs WAV
(bit-exact) and 0.46x of the float keylock WAV as 24-bit FLAC (exact length, residual -143 dB). Old
cache entries stay WAV until `python3 -m app.music_brain.audio.audio_convert --apply` rewrites them, so every
reader resolves a stem by trying `.flac` first and falling back to `.wav`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

STEM_EXTS = (".flac", ".wav")                  # reader preference order
FLAC_LEVEL = 5                                 # FLAC's own 0..8 scale
# libsndfile takes the level as 0..1 and maps it onto FLAC's 0..8
_SF_LEVEL = FLAC_LEVEL / 8.0
STEM_SUBTYPE = "PCM_16"                        # Demucs stems (16-bit, bit-exact vs the old WAV)
RENDER_SUBTYPE = "PCM_24"                      # keylock renders (Rubber Band float output)
MEDIA_TYPES = {".flac": "audio/flac", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
               ".opus": "audio/ogg; codecs=opus", ".ogg": "audio/ogg", ".webm": "audio/webm"}


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


NATIVE_EXTS = (".wav", ".flac", ".mp3")      # libsndfile reads these directly; anything else goes through ffmpeg
FFMPEG_TIMEOUT_S = 600


def _tool(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        raise OSError(f"{name} not found")
    return exe


def audio_info(path: Path | str) -> Tuple[int, int, int, float]:
    """(frames, samplerate, channels, duration_s) of a track in any format. Raises OSError when unreadable.
    wav/flac/mp3 via soundfile, the rest (m4a, aac, opus, ...) via ffprobe. Nothing is written to disk."""
    path = Path(path)
    if path.suffix.lower() in NATIVE_EXTS:
        import soundfile as sf

        try:
            i = sf.info(str(path))
        except Exception as exc:
            raise OSError(f"cannot read {path.name}: {exc}") from exc
        return int(i.frames), int(i.samplerate), int(i.channels), float(i.duration)
    try:
        res = subprocess.run([_tool("ffprobe"), "-v", "error", "-select_streams", "a:0", "-show_entries",
                              "stream=sample_rate,channels:format=duration", "-of", "json", str(path)],
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise OSError((res.stderr or "").strip()[-300:] or "ffprobe failed")
        d = json.loads(res.stdout)
        st = d["streams"][0]
        sr, ch, dur = int(st["sample_rate"]), int(st["channels"]), float(d["format"]["duration"])
    except (subprocess.SubprocessError, ValueError, KeyError, IndexError) as exc:
        raise OSError(f"cannot read {path.name}: {exc}") from exc
    return int(round(dur * sr)), sr, ch, dur


def load_audio(path: Path | str, sr: Optional[int] = None, mono: bool = False, dtype: str = "float32",
               start: Optional[float] = None, stop: Optional[float] = None) -> Tuple[np.ndarray, int]:
    """Decode a track fully in memory -> (samples, samplerate). `start`/`stop` are seconds.

    Shape is (n,) when `mono`, else (n, channels). `sr` None keeps the file rate. wav/flac/mp3 use soundfile,
    anything else pipes `ffmpeg -f f32le` into numpy. No temp files. Raises OSError on any failure."""
    path = Path(path)
    done_sr = False
    if path.suffix.lower() in NATIVE_EXTS:
        import soundfile as sf

        try:
            with sf.SoundFile(str(path)) as f:
                rate = f.samplerate
                a = int(max(start or 0.0, 0.0) * rate)
                f.seek(a)
                n = -1 if stop is None else max(int(stop * rate) - a, 0)
                y = f.read(n, dtype="float32", always_2d=True)
        except Exception as exc:
            raise OSError(f"cannot read {path.name}: {exc}") from exc
    else:
        _, rate, ch, _ = audio_info(path)
        cmd = [_tool("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error"]
        if start:
            cmd += ["-ss", f"{start:.6f}"]
        if stop is not None:
            cmd += ["-t", f"{max(stop - (start or 0.0), 0.0):.6f}"]
        out_ch = 1 if mono else ch
        cmd += ["-i", str(path), "-map", "0:a:0", "-vn", "-f", "f32le", "-ac", str(out_ch)]
        if sr:
            cmd += ["-ar", str(int(sr))]
            rate, done_sr = int(sr), True
        try:
            res = subprocess.run(cmd + ["pipe:1"], capture_output=True, timeout=FFMPEG_TIMEOUT_S)
        except subprocess.SubprocessError as exc:
            raise OSError(f"ffmpeg failed on {path.name}: {exc}") from exc
        if res.returncode != 0 or not res.stdout:
            raise OSError(f"ffmpeg failed on {path.name}: {res.stderr.decode(errors='replace').strip()[-300:]}")
        y = np.frombuffer(res.stdout, dtype="<f4")[: len(res.stdout) // 4 // out_ch * out_ch].reshape(-1, out_ch)
    if mono and y.shape[1] > 1:
        y = y.mean(axis=1, keepdims=True)
    if sr and int(sr) != rate and not done_sr:
        import librosa

        y = librosa.resample(y.T, orig_sr=rate, target_sr=int(sr)).T
        rate = int(sr)
    y = np.ascontiguousarray(y[:, 0] if mono else y)
    if dtype != "float32":
        if np.dtype(dtype).kind == "i":
            y = (np.clip(y, -1.0, 1.0) * np.iinfo(dtype).max).astype(dtype)
        else:
            y = y.astype(dtype)
    return y, rate


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
