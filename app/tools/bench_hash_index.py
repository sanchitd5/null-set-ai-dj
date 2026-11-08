"""Time analyzer.analyze() for N local tracks: cold (no hash index = the old behaviour: every file is hashed),
then after a simulated restart (a NEW process that loads the persisted index). Analysis records are pre-seeded so
only the hash + JSON read is measured, which is what a restarted server does on first touch.

    python -m app.tools.bench_hash_index [--n 50] [--mb 20] [--dir DIR]   # DIR defaults to a fresh temp dir
Page cache is warm in both runs (no sudo purge), so the real cold-disk gap is larger than shown.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def child(songs: Path) -> None:
    from app.music_brain.analysis import analyzer

    files = sorted(songs.glob("*.bin"))
    t0 = time.perf_counter()
    for f in files:
        analyzer.analyze(f)
    print(json.dumps({"n": len(files), "seconds": round(time.perf_counter() - t0, 3)}))


def run(cache: Path, songs: Path) -> dict:
    env = dict(os.environ, AIDJ_CACHE_DIR=str(cache))
    out = subprocess.run([sys.executable, "-m", "app.tools.bench_hash_index", "--child", str(songs)],
                         env=env, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--mb", type=int, default=20)
    ap.add_argument("--dir")
    ap.add_argument("--child")
    a = ap.parse_args()
    if a.child:
        return child(Path(a.child))
    root = Path(a.dir or tempfile.mkdtemp(prefix="hashbench_"))
    songs, cache = root / "songs", root / "cache"
    shutil.rmtree(cache, ignore_errors=True)
    songs.mkdir(parents=True, exist_ok=True)
    (cache / "analysis").mkdir(parents=True)
    import hashlib
    from app.music_brain.analysis.analyzer import ANALYSIS_VERSION

    rec = {"path": "", "duration": 200.0, "bpm": 124.0, "beat_times": [i * 0.48 for i in range(400)],
           "downbeat_times": [], "phrase_boundaries_8bar": [], "phrase_boundaries_16bar": [], "key": None,
           "energy_curve": [0.5] * 1500, "energy_times": [i * 0.13 for i in range(1500)], "sections": [],
           "vocal_active_regions": [], "drops": [], "main_drop": None, "structure": None}
    for i in range(a.n):
        f = songs / f"t{i:03d}.bin"
        if not f.exists():
            f.write_bytes(os.urandom(a.mb << 20))
        h = hashlib.sha256(f.read_bytes()).hexdigest()
        (cache / "analysis" / f"{h}.v{ANALYSIS_VERSION}.json").write_text(json.dumps(rec))
    cold = run(cache, songs)                      # no hash_index.json: hashes every file (old behaviour)
    assert (cache / "hash_index.json").exists()
    restart = run(cache, songs)                   # new process, index persisted
    print(json.dumps({"tracks": a.n, "mb_each": a.mb, "cold_s": cold["seconds"], "after_restart_s": restart["seconds"],
                      "speedup": round(cold["seconds"] / max(restart["seconds"], 1e-6), 1), "dir": str(root)}))


if __name__ == "__main__":
    main()
