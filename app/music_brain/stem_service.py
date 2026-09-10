"""4-stem (vocals/drums/bass/other) and 2-stem (vocals/instrumental) audio
separation via Demucs (htdemucs_ft), with SHA-256-keyed disk caching in
cache/stems/ so a track is never separated twice.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from app.music_brain.config import DEMUCS_MODEL, STEMS_CACHE_DIR

FOUR_STEM_NAMES = ("vocals", "drums", "bass", "other")
TWO_STEM_NAMES = ("vocals", "no_vocals")


@dataclass
class StemResult:
    audio_hash: str
    model: str
    two_stems: Optional[str]
    stems: Dict[str, str]  # stem name -> absolute path
    cache_dir: str
    from_cache: bool

    def to_dict(self) -> dict:
        return {
            "audio_hash": self.audio_hash,
            "model": self.model,
            "two_stems": self.two_stems,
            "stems": self.stems,
            "cache_dir": self.cache_dir,
            "from_cache": self.from_cache,
        }


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_dir_for(audio_hash: str, model: str, two_stems: Optional[str]) -> Path:
    suffix = f"_{two_stems}" if two_stems else ""
    return STEMS_CACHE_DIR / f"{audio_hash}_{model}{suffix}"


def _manifest_path(cache_dir: Path) -> Path:
    return cache_dir / "manifest.json"


def _load_from_cache(cache_dir: Path) -> Optional[Dict[str, str]]:
    manifest_path = _manifest_path(cache_dir)
    if not manifest_path.exists():
        return None
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    if all(Path(p).exists() for p in manifest.values()):
        return manifest
    return None


def separate(
    audio_path: str | Path,
    two_stems: Optional[str] = None,
    model: str = DEMUCS_MODEL,
    device: Optional[str] = None,
    force: bool = False,
) -> StemResult:
    """Separate a track into stems, caching results by content hash.

    Args:
        audio_path: input audio file.
        two_stems: if set (e.g. "vocals"), run 2x-faster 2-stem separation
            (that stem vs. everything else) instead of full 4-stem.
        model: Demucs model name (default htdemucs_ft).
        device: "cuda" or "cpu"; auto-detected via torch if not given.
        force: bypass the cache and re-run separation.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(audio_path)

    audio_hash = file_hash(audio_path)
    cache_dir = _cache_dir_for(audio_hash, model, two_stems)

    if not force:
        cached = _load_from_cache(cache_dir)
        if cached is not None:
            return StemResult(
                audio_hash=audio_hash, model=model, two_stems=two_stems,
                stems=cached, cache_dir=str(cache_dir), from_cache=True,
            )

    if device is None:
        device = _detect_device()

    cache_dir.mkdir(parents=True, exist_ok=True)
    demucs_out_dir = cache_dir / "_demucs_out"
    demucs_out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "demucs",
        "-n", model,
        "-d", device,
        "-o", str(demucs_out_dir),
    ]
    if two_stems:
        cmd += ["--two-stems", two_stems]
    cmd.append(str(audio_path))

    subprocess.run(cmd, check=True)

    track_stem_dir = demucs_out_dir / model / audio_path.stem
    stem_names = (two_stems, [n for n in TWO_STEM_NAMES if n != two_stems][0]) if two_stems else FOUR_STEM_NAMES
    if two_stems:
        stem_names = TWO_STEM_NAMES

    stems: Dict[str, str] = {}
    for name in stem_names:
        src = track_stem_dir / f"{name}.wav"
        if not src.exists():
            continue
        dest = cache_dir / f"{name}.wav"
        shutil.copy2(src, dest)
        stems[name] = str(dest)

    shutil.rmtree(demucs_out_dir, ignore_errors=True)

    with open(_manifest_path(cache_dir), "w", encoding="utf-8") as f:
        json.dump(stems, f, indent=2)

    return StemResult(
        audio_hash=audio_hash, model=model, two_stems=two_stems,
        stems=stems, cache_dir=str(cache_dir), from_cache=False,
    )


def _detect_device() -> str:
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Separate a track into Demucs stems.")
    parser.add_argument("audio_path")
    parser.add_argument("--two-stems", default=None, help="e.g. 'vocals' for a 2-stem split")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    result = separate(args.audio_path, two_stems=args.two_stems, force=args.force)
    print(json.dumps(result.to_dict(), indent=2))
