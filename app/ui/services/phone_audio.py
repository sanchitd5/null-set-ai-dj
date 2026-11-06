"""Phone-sized audio: a track or stem transcoded once to AAC-LC in .m4a and cached on disk.

A phone on a mobile link pulled 35-54 MB WAVs at ~280 KB/s and timed out. `?q=phone` on the audio
routes serves this instead: AAC-LC 256 kbps (about 1.9 MB a minute for stereo).

Why AAC-in-MP4 and not Opus: iOS Safari's decodeAudioData reads MP4 AAC everywhere, but Ogg Opus
only on recent releases. The MP4 muxer writes an edit list that drops the encoder priming
(1024 samples) and the tail padding. Chrome and Safari honour it, so the decoded buffer lines up
sample-for-sample with the WAV. The console relies on that alignment: stems against the mix,
beat grids, phrase lines (app/tests/py/test_phone_audio.py; checked in Chrome by the
phone_audio alignment script).

Cache key = source name + size + mtime + codec settings, so a re-rendered stem or a codec change
never serves a stale file. Writes are atomic (tmp in the same directory, then os.replace) and one
transcode per key runs at a time. Any failure (no ffmpeg, a bad file) returns None: the caller
then serves the original."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

CODEC = "aac"
BITRATE = "256k"
SETTINGS = f"{CODEC}-lc-{BITRATE}-mp4-editlist-v1"   # part of every cache key: change it, re-encode
MEDIA_TYPE = "audio/mp4"
TIMEOUT_S = 600

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def cache_key(src: Path, label: str) -> str:
    st = src.stat()
    raw = f"{label}|{src.name}|{st.st_size}|{st.st_mtime_ns}|{SETTINGS}"
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", label)[:64]       # a file name, never a path
    return f"{safe}.{hashlib.sha256(raw.encode()).hexdigest()[:20]}.m4a"


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def ffmpeg_cmd(src: Path, out: Path) -> list:
    # -map 0:a:0: audio only (no cover art stream); edit list on (the default for mp4, explicit
    # here because alignment depends on it); +faststart puts the index first.
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
            "-map", "0:a:0", "-vn", "-c:a", CODEC, "-b:a", BITRATE,
            "-use_editlist", "1", "-movflags", "+faststart", "-f", "mp4", str(out)]


def sha256_of(path: Path) -> str:
    """sha256 of a cached phone copy, kept in a `<file>.sha256` sidecar so the phone can verify
    the bytes it cached (the copy's hash differs from the source's content id)."""
    side = path.with_name(path.name + ".sha256")
    try:
        st = path.stat()
        if side.exists() and side.stat().st_mtime_ns >= st.st_mtime_ns:
            val = side.read_text().strip()
            if re.fullmatch(r"[0-9a-f]{64}", val):
                return val
    except OSError:
        pass
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    val = h.hexdigest()
    try:
        tmp = side.with_name(side.name + f".tmp{os.getpid()}")
        tmp.write_text(val)
        os.replace(tmp, side)
    except OSError:
        pass
    return val


def phone_variant(src: Path, label: str, cache_dir: Path) -> Optional[Path]:
    """The cached .m4a for `src`, transcoding it first if needed. None = serve the original."""
    if not shutil.which("ffmpeg"):
        log.warning("phone audio: ffmpeg not found, serving the original file")
        return None
    try:
        key = cache_key(src, label)
    except OSError:
        return None
    out = cache_dir / key
    if out.exists():
        return out
    with _lock(key):
        if out.exists():                                  # another request finished it meanwhile
            return out
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_dir / f".{key}.tmp{os.getpid()}.{threading.get_ident()}"
        try:
            res = subprocess.run(ffmpeg_cmd(src, tmp), capture_output=True, text=True, timeout=TIMEOUT_S)
            if res.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
                log.warning("phone audio: ffmpeg failed on %s: %s", src.name, (res.stderr or "").strip()[-300:])
                return None
            os.replace(tmp, out)
            log.info("phone audio: %s -> %s (%.1f MB -> %.1f MB)", src.name, out.name,
                     src.stat().st_size / 1e6, out.stat().st_size / 1e6)
            return out
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("phone audio: transcode of %s failed: %s", src.name, exc)
            return None
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
