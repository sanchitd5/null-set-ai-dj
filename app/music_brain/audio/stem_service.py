"""4-stem (vocals/drums/bass/other) and 2-stem (vocals/instrumental) audio
separation via Demucs (htdemucs_ft), with SHA-256-keyed disk caching in
cache/stems/ so a track is never separated twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from app.music_brain.audio import audio_io
from app.music_brain.audio.audio_io import MANIFEST_VERSION, read_manifest, write_manifest  # noqa: F401 (re-export)
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
    return read_manifest(cache_dir)


def _demucs_out():
    """Where Demucs' progress output goes: our stderr (never stdout: agent_bridge prints
    JSON there), or nowhere when stderr is not a real file (pytest capture, embedders)."""
    try:
        sys.stderr.fileno()
        return sys.stderr
    except (AttributeError, OSError, ValueError):
        return subprocess.DEVNULL


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

    try:
        subprocess.run(cmd, check=True, stdout=_demucs_out())   # stdout stays JSON-only for agent_bridge
    except subprocess.CalledProcessError:
        if device != "mps":
            raise
        # Some ops are still flaky on MPS: retry the whole run on CPU.
        cmd[cmd.index("-d") + 1] = "cpu"
        shutil.rmtree(demucs_out_dir, ignore_errors=True)
        demucs_out_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(cmd, check=True, stdout=_demucs_out())   # stdout stays JSON-only for agent_bridge

    track_stem_dir = demucs_out_dir / model / audio_path.stem
    stem_names = (two_stems, [n for n in TWO_STEM_NAMES if n != two_stems][0]) if two_stems else FOUR_STEM_NAMES
    if two_stems:
        stem_names = TWO_STEM_NAMES

    stems: Dict[str, str] = {}
    for name in stem_names:
        src = audio_io.stem_file(track_stem_dir, name)
        if src is None:
            continue
        dest = cache_dir / f"{name}.flac"
        if src.suffix.lower() == ".flac":
            shutil.copy2(src, dest)
        else:
            audio_io.encode_file(src, dest)    # Demucs' 16-bit WAV -> 16-bit FLAC, bit-exact
        stems[name] = str(dest)

    shutil.rmtree(demucs_out_dir, ignore_errors=True)

    write_manifest(cache_dir, stems)
    if model == DEMUCS_MODEL and not two_stems:
        _auto_prune(audio_hash, cache_dir.parent)

    return StemResult(
        audio_hash=audio_hash, model=model, two_stems=two_stems,
        stems=stems, cache_dir=str(cache_dir), from_cache=False,
    )


# ---- persistent worker (app/music_brain/audio/stem_worker.py) ---------------------
FAST_MODEL = "htdemucs"   # single model: ~4.5x faster than the htdemucs_ft bag, same 4 stems


class StemWorker:
    """One long-lived `stem_worker` process: model loaded once, next song
    decoded while the current one runs on the GPU, stems written on a thread."""

    def __init__(self, model: str = FAST_MODEL):
        import queue
        import threading

        self.model = model
        self.results: "queue.Queue[dict]" = queue.Queue()
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "app.music_brain.audio.stem_worker", "--model", model],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=str(Path(__file__).resolve().parents[3]),
        )
        first = self._proc.stdout.readline()
        if not first or not json.loads(first).get("ready"):
            raise RuntimeError("stem worker failed to start")
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self._proc.stdout:
            try:
                self.results.put(json.loads(line))
            except ValueError:
                continue
        self.results.put({"id": None, "ok": False, "error": "worker exited"})

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def submit(self, job_id: str, audio_path: Path) -> Path:
        """Queue a 4-stem separation; returns the cache dir it will fill."""
        cache_dir = _cache_dir_for(file_hash(audio_path), self.model, None)
        self._proc.stdin.write(json.dumps({"id": job_id, "path": str(audio_path), "out_dir": str(cache_dir)}) + "\n")
        self._proc.stdin.flush()
        return cache_dir

    @staticmethod
    def finish(result: dict) -> Dict[str, str]:
        """Write the manifest for a finished job (the cache hit marker)."""
        stems = result["stems"]
        cache_dir = Path(next(iter(stems.values()))).parent
        write_manifest(cache_dir, stems)
        ft = f"_{DEMUCS_MODEL}"
        if cache_dir.name.endswith(ft):
            _auto_prune(cache_dir.name[: -len(ft)], cache_dir.parent)
        return stems


def cached_four_stems(audio_path: Path) -> Optional[Dict[str, str]]:
    """4 stems from any model already in the cache (htdemucs_ft first, then htdemucs)."""
    h = file_hash(audio_path)
    for model in (DEMUCS_MODEL, FAST_MODEL):
        st = _load_from_cache(_cache_dir_for(h, model, None))
        if st and all(st.get(n) for n in FOUR_STEM_NAMES):
            return st
    return None


# ---- one stem set per song: htdemucs_ft wins ---------------------------------------
# Owner rule: once a song has a complete htdemucs_ft 4-stem set, its other stem folders
# (<hash>_htdemucs 4-stem, <hash>_htdemucs_vocals 2-stem, any non-ft model) are dead weight:
# every reader prefers ft (cached_four_stems, ft_vocals). They are removed right after an ft
# set is written, and by the maintenance "stems" step for songs that already have one.

def complete_ft(audio_hash: str, stems_dir: Optional[Path] = None) -> Optional[Dict[str, str]]:
    """The song's htdemucs_ft 4-stem set when complete (manifest ok, all 4 files on disk), else None."""
    st = _load_from_cache(Path(stems_dir or STEMS_CACHE_DIR) / f"{audio_hash}_{DEMUCS_MODEL}")
    return st if st and all(st.get(n) for n in FOUR_STEM_NAMES) else None


def ft_vocals(audio_path: str | Path) -> Optional[str]:
    """Vocals of a complete htdemucs_ft 4-stem set (never separates); None when there is none."""
    st = complete_ft(file_hash(Path(audio_path)))
    return st["vocals"] if st else None


def non_ft_dirs(audio_hash: str, stems_dir: Optional[Path] = None) -> List[Path]:
    """Every stem folder of this song made by a model other than htdemucs_ft."""
    root = Path(stems_dir or STEMS_CACHE_DIR)
    if not root.is_dir():
        return []
    n = len(audio_hash) + 1
    return sorted(d for d in root.glob(f"{audio_hash}_*")
                  if d.is_dir() and not d.name[n:].startswith(DEMUCS_MODEL))


def dir_bytes(d: Path) -> int:
    return sum(f.stat().st_size for f in Path(d).rglob("*") if f.is_file())


def prune_non_ft(audio_hash: str, stems_dir: Optional[Path] = None, dry_run: bool = False) -> dict:
    """Remove the song's non-ft stem folders, ONLY when its ft set is complete. Each folder is
    renamed out of stems/ first (atomic: a reader sees the whole folder or none), then deleted.
    -> {"removed": [folder names], "bytes": n, "errors": [...]} or {"skipped": why}."""
    root = Path(stems_dir or STEMS_CACHE_DIR)
    if complete_ft(audio_hash, root) is None:
        return {"removed": [], "bytes": 0, "errors": [], "skipped": "no complete htdemucs_ft set"}
    out = {"removed": [], "bytes": 0, "errors": []}
    trash = root.parent / "stems_trash"
    for d in non_ft_dirs(audio_hash, root):
        size = dir_bytes(d)
        if not dry_run:
            try:
                trash.mkdir(parents=True, exist_ok=True)
                t = trash / f"{d.name}.{os.getpid()}.{time.time_ns()}"
                os.replace(d, t)
            except OSError as exc:
                out["errors"].append(f"{d.name}: {exc}"[:200])
                continue
            shutil.rmtree(t, ignore_errors=True)
        out["removed"].append(d.name)
        out["bytes"] += size
    return out


def _auto_prune(audio_hash: str, stems_dir: Path) -> None:
    """After an ft set was written: drop the non-ft copies. Never fails the separation."""
    try:
        r = prune_non_ft(audio_hash, stems_dir)
        if r["removed"]:
            print(f"[stems] {audio_hash[:12]}: htdemucs_ft set complete, removed {', '.join(r['removed'])}",
                  file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001 -- cleanup is best effort, the ft set stands
        print(f"[stems] {audio_hash[:12]}: non-ft cleanup failed: {exc}", file=sys.stderr, flush=True)


def _detect_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        # Apple Silicon GPU: htdemucs 2-stem on a 4 min song took 21 s on MPS
        # vs 49 s on CPU (M-series, torch 2.14 / demucs 4.1).
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
        return "cpu"
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
