"""Convert the existing WAV cache to FLAC in place (stems as 16-bit, keylock renders as 24-bit).

    python3 -m app.music_brain.audio_convert                      # dry run: files, bytes, projected saving
    python3 -m app.music_brain.audio_convert --apply [--only stems|keylock] [--limit N]

Per file: encode to a tmp FLAC, decode it back and compare with the WAV (same rate, channels and frame
count; samples bit-exact at 16-bit, max abs error under 1e-6 at 24-bit), atomically move it into place,
update the manifest / meta, and only then delete the WAV. On any mismatch the WAV stays and the file is
reported. Resumable and idempotent: a converted file has no WAV left, so a re-run skips it. Holds a lock
file so two converters never overlap, and skips keylock sets whose render is in flight (`<key>.tmp`).
Ratios for the dry run are the measured ones in research/notes/audio-format-study.md.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import numpy as np

from app.music_brain import audio_io

RATIO = {"PCM_16": 0.422, "PCM_24": 0.459}       # FLAC bytes / WAV bytes (study, MEASURED)
TOL_24 = 1e-6                                   # 24-bit quantisation step is 2^-23 ~ 1.2e-7
LOCK_NAME = ".audio_convert.lock"


def _default_cache() -> Path:
    from app.music_brain.config import CACHE_DIR
    return Path(CACHE_DIR)


def _subtype_for(wav: Path) -> str:
    import soundfile as sf
    st = sf.info(str(wav)).subtype
    return audio_io.RENDER_SUBTYPE if st in ("FLOAT", "DOUBLE", "PCM_24", "PCM_32") else audio_io.STEM_SUBTYPE


def verify(wav: Path, flac: Path, subtype: str) -> Optional[str]:
    """None when the FLAC decodes to the WAV's audio, else what differs."""
    import soundfile as sf
    a, b = sf.info(str(wav)), sf.info(str(flac))
    if (a.samplerate, a.channels, a.frames) != (b.samplerate, b.channels, b.frames):
        return f"shape {a.samplerate}/{a.channels}/{a.frames} != {b.samplerate}/{b.channels}/{b.frames}"
    if subtype == "PCM_16":
        x, _ = sf.read(str(wav), dtype="int16", always_2d=True)
        y, _ = sf.read(str(flac), dtype="int16", always_2d=True)
        return None if np.array_equal(x, y) else "16-bit samples differ"
    x, _ = sf.read(str(wav), dtype="float64", always_2d=True)
    y, _ = sf.read(str(flac), dtype="float64", always_2d=True)
    err = float(np.max(np.abs(np.clip(x, -1.0, 1.0) - y))) if x.size else 0.0
    return None if err < TOL_24 else f"24-bit max abs error {err:.3g}"


def convert_file(wav: Path, subtype: Optional[str] = None) -> Tuple[Path, int, int]:
    """wav -> sibling .flac, verified. Returns (flac, wav_bytes, flac_bytes); raises on mismatch (WAV kept)."""
    subtype = subtype or _subtype_for(wav)
    flac = wav.with_suffix(".flac")
    tmp = wav.with_name(f".{wav.stem}.convert{os.getpid()}.flac")
    try:
        audio_io.encode_file(wav, tmp, subtype)
        bad = verify(wav, tmp, subtype)
        if bad:
            raise ValueError(bad)
        os.replace(tmp, flac)
    finally:
        tmp.unlink(missing_ok=True)
    return flac, wav.stat().st_size, flac.stat().st_size


# ---- discovery -----------------------------------------------------------------------------------
def stem_dirs(cache: Path) -> Iterator[Path]:
    root = cache / "stems"
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and (d / audio_io.MANIFEST_NAME).exists():   # no manifest = not finished
                yield d


def keylock_dirs(cache: Path) -> Iterator[Path]:
    root = cache / "keylock"
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if (d.is_dir() and not d.name.endswith(".tmp") and (d / "meta.json").exists()
                    and not d.with_suffix(".tmp").exists()):             # render in flight
                yield d


def _stem_wavs(d: Path) -> List[Path]:
    m = audio_io.read_manifest(d) or {}
    return [Path(p) for p in m.values() if Path(p).suffix.lower() == ".wav"]


def _keylock_wavs(d: Path) -> List[Path]:
    return sorted(p for p in d.glob("*.wav") if not p.name.startswith("."))


def plan(cache: Path, only: Optional[str] = None) -> List[Tuple[str, Path, List[Path]]]:
    out = []
    if only in (None, "stems"):
        out += [("stems", d, w) for d in stem_dirs(cache) if (w := _stem_wavs(d))]
    if only in (None, "keylock"):
        out += [("keylock", d, w) for d in keylock_dirs(cache) if (w := _keylock_wavs(d))]
    return out


# ---- run -----------------------------------------------------------------------------------------
def _finish_dir(kind: str, d: Path, done: dict) -> None:
    """Point the manifest / meta at the FLACs before any WAV goes away."""
    if kind == "stems":
        m = audio_io.read_manifest(d)          # WAVs still exist here, so swap in the new FLACs by path
        if not m:
            raise ValueError("manifest unreadable")
        audio_io.write_manifest(d, {n: str(done.get(Path(p), p)) for n, p in m.items()})
    else:
        p = d / "meta.json"
        meta = json.loads(p.read_text())
        wavs_left = [w for w in d.glob("*.wav") if w not in done]
        meta["format"] = "flac" if not wavs_left else "mixed"
        tmp = p.with_name(f".meta.json.tmp{os.getpid()}")
        tmp.write_text(json.dumps(meta))
        os.replace(tmp, p)


def run(cache: Optional[Path] = None, apply: bool = False, only: Optional[str] = None,
        limit: Optional[int] = None, log=None) -> dict:
    cache = Path(cache) if cache else _default_cache()
    log = log or (lambda s: print(s, file=sys.stderr, flush=True))
    todo = plan(cache, only)
    if limit is not None:
        todo = todo[:limit]
    summary = {"apply": apply, "dirs": len(todo), "files": 0, "converted": 0, "skipped": 0,
               "failed": [], "bytes_before": 0, "bytes_after": 0, "seconds": 0.0}
    t0 = time.monotonic()
    if not apply:
        for kind, d, wavs in todo:
            for w in wavs:
                n = w.stat().st_size
                summary["files"] += 1
                summary["bytes_before"] += n
                summary["bytes_after"] += int(n * RATIO[_subtype_for(w)])
        summary["projected"] = True
        summary["saved_bytes"] = summary["bytes_before"] - summary["bytes_after"]
        return summary

    cache.mkdir(parents=True, exist_ok=True)
    with open(cache / LOCK_NAME, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("another audio_convert is running")
        for i, (kind, d, wavs) in enumerate(todo, 1):
            if kind == "keylock" and d.with_suffix(".tmp").exists():
                summary["skipped"] += len(wavs)
                continue
            done = {}
            for w in wavs:
                summary["files"] += 1
                if not w.exists():                     # evicted / converted meanwhile
                    summary["skipped"] += 1
                    continue
                try:
                    sub = audio_io.RENDER_SUBTYPE if kind == "keylock" else None
                    flac, a, b = convert_file(w, sub)
                    done[w] = flac
                    summary["bytes_before"] += a
                    summary["bytes_after"] += b
                except Exception as exc:               # WAV kept; reported
                    summary["failed"].append({"file": str(w), "error": f"{type(exc).__name__}: {exc}"})
            if done:
                try:
                    _finish_dir(kind, d, done)
                except Exception as exc:               # manifest not updated: keep every WAV
                    summary["failed"].append({"file": str(d), "error": f"manifest: {type(exc).__name__}: {exc}"})
                    continue
                for w in done:
                    w.unlink(missing_ok=True)
                    summary["converted"] += 1
            log(f"[{i}/{len(todo)}] {kind} {d.name}: {len(done)}/{len(wavs)} converted")
    summary["projected"] = False
    summary["saved_bytes"] = summary["bytes_before"] - summary["bytes_after"]
    summary["seconds"] = round(time.monotonic() - t0, 2)
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="report files, bytes and projected saving (default)")
    g.add_argument("--apply", action="store_true", help="convert, verify, then delete each WAV")
    ap.add_argument("--only", choices=("stems", "keylock"))
    ap.add_argument("--limit", type=int, default=None, help="at most N cache dirs")
    ap.add_argument("--cache-dir", type=Path, default=None, help="default: data/cache (config.CACHE_DIR)")
    a = ap.parse_args(argv)
    try:
        res = run(a.cache_dir, apply=a.apply, only=a.only, limit=a.limit)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(res, indent=2))
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
