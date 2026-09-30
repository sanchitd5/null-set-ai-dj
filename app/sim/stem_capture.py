"""Capture ONE transition as the live console plays it (js/stem-capture.js) for a pair of songs.

The console's own scripts run unmodified in node on the virtual clock (app/sim/js/env.js) against
the real FastAPI app. The app runs on a throwaway cache holding copies of only A's and B's upload,
analysis and 4-stem sets, so nothing is written to the real cache and nothing is separated or
downloaded: a song without cached 4 stems is refused before anything starts.

The result (every node, AudioParam automation timeline, connect / disconnect and buffer source
position over the window) is what app/music_brain/render/graph_render.py renders on real audio.

    capture(a_path, b_path, "Bass Swap", a_time=195, b_time=3.3) -> dict
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from app.sim.pool import REPO_ROOT

CAPTURE_JS = Path(__file__).with_name("js") / "stem-capture.js"
STEM_NAMES = ("drums", "bass", "vocals", "other")
MODELS = ("htdemucs_ft", "htdemucs")


def track_id(path: Path) -> str:
    """The app's content id: sha256(bytes)[:16]."""
    return _sha256(path)[:16]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _stem_dir(src_cache: Path, digest: str) -> Optional[Path]:
    for model in MODELS:
        d = src_cache / "stems" / f"{digest}_{model}"
        if all(any((d / f"{n}{ext}").exists() for ext in (".flac", ".wav")) for n in STEM_NAMES):
            return d
    return None


def seed_cache(dst: Path, src_cache: Path, songs: list[Path]) -> dict:
    """Copy each song, its cached analysis and its 4 stems into the throwaway cache `dst`.
    Reads `src_cache`, never writes it. -> {id: {name, stem_dir}}; ValueError without stems."""
    (dst / "uploads").mkdir(parents=True, exist_ok=True)
    (dst / "analysis").mkdir(exist_ok=True)
    out = {}
    for song in songs:
        song = Path(song).resolve()
        if not song.is_file():
            raise FileNotFoundError(f"no such song: {song}")
        digest = _sha256(song)
        sid = digest[:16]
        sd = _stem_dir(src_cache, digest)
        if sd is None:
            raise ValueError(f"{song.name}: no cached 4-stem set (separate it first)")
        shutil.copy2(song, dst / "uploads" / f"{sid}{song.suffix.lower()}")
        for f in (src_cache / "analysis").glob(f"{digest}*.json"):
            shutil.copy2(f, dst / "analysis" / f.name)
        to = dst / "stems" / sd.name
        to.mkdir(parents=True, exist_ok=True)
        stems = {}
        for f in sd.iterdir():
            if f.suffix in (".flac", ".wav"):
                shutil.copy2(f, to / f.name)
                stems[f.stem] = str(to / f.name)
        (to / "manifest.json").write_text(json.dumps(
            {"version": 2, "format": "flac" if to.joinpath("drums.flac").exists() else "wav", "stems": stems}))
        out[sid] = {"name": song.stem, "stem_dir": str(sd), "path": str(song)}
    names = src_cache / "uploads" / "_names.json"
    if names.is_file():
        try:
            known = json.loads(names.read_text(encoding="utf-8"))
            (dst / "uploads" / "_names.json").write_text(json.dumps({k: v for k, v in known.items() if k in out}))
        except (ValueError, AttributeError):
            pass
    return out


@contextlib.contextmanager
def _serve():
    """The real app on a private port (the process's AIDJ_CACHE_DIR is the throwaway cache)."""
    import uvicorn
    from app.ui import server

    cfg = uvicorn.Config(server.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off", access_log=False)
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, name="stem-capture-api", daemon=True)
    th.start()
    t0 = time.monotonic()
    while not srv.started:
        if not th.is_alive() or time.monotonic() - t0 > 60:
            raise RuntimeError("the API server did not start")
        time.sleep(0.05)
    try:
        yield srv.servers[0].sockets[0].getsockname()[1]
    finally:
        srv.should_exit = True
        th.join(timeout=10)


def _worker(cfg_path: Path) -> int:
    """Inside the subprocess: serve the app, run the node capture, leave cfg['out']."""
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node is required: the console is JavaScript")
    with _serve() as port:
        cfg["port"] = port
        run_cfg = cfg_path.with_name("node-cfg.json")
        run_cfg.write_text(json.dumps(cfg))
        p = subprocess.run([node, str(CAPTURE_JS), str(run_cfg)], cwd=REPO_ROOT, capture_output=True,
                           text=True, timeout=cfg.get("timeout_s", 900))
    if p.returncode != 0 or not Path(cfg["out"]).exists():
        sys.stderr.write(p.stderr[-2000:])
        return 1
    return 0


def capture(a_path, b_path, recipe: str, a_time: float, b_time: float, pre: float = 30.0,
            post: float = 30.0, src_cache: Optional[Path] = None, timeout_s: float = 900.0,
            allow_stem_path: bool = False, xf: Optional[float] = None) -> dict:
    """Capture the console playing A -> B with `recipe` forced at A's exit `a_time` and B's
    entry `b_time` (the macro PLAY STEP path: autopilot performNow + forcedBooking gates).

    Preview-only overrides, applied to a throwaway copy of the console scripts the capture
    loads (never app/ui/static, never the live console), listed in the result's `sim_overrides`:
    allow_stem_path lifts the stem blend's loudness floor; xf sets PLAY STEP's crossfade budget
    (default 16 = the booking as it is; below 16 every bar count is halved, like a running set's
    quick / vocal-short window)."""
    if not recipe:
        raise ValueError("recipe is required")
    if xf is not None and not (float(xf) > 0):
        raise ValueError("xf must be a number > 0")
    for v, n in ((a_time, "a_time"), (b_time, "b_time"), (pre, "pre"), (post, "post")):
        if v is None or not (float(v) >= 0):
            raise ValueError(f"{n} must be a number >= 0")
    if src_cache is None:
        from app.music_brain.config import CACHE_DIR
        src_cache = CACHE_DIR
    a_path, b_path = Path(a_path).resolve(), Path(b_path).resolve()
    with tempfile.TemporaryDirectory(prefix="stem-capture-") as d:
        d = Path(d)
        cache = d / "cache"
        songs = seed_cache(cache, Path(src_cache), [a_path, b_path])
        ida, idb = track_id(a_path), track_id(b_path)
        if ida == idb:
            raise ValueError("A and B are the same song")
        out = d / "capture.json"
        cfg = {"a": {"id": ida, "name": songs[ida]["name"]}, "b": {"id": idb, "name": songs[idb]["name"]},
               "recipe": recipe, "aTime": float(a_time), "bTime": float(b_time), "pre": float(pre),
               "post": float(post), "out": str(out), "timeout_s": timeout_s,
               "allowStemPath": bool(allow_stem_path), "xf": None if xf is None else float(xf)}
        cfg_path = d / "cfg.json"
        cfg_path.write_text(json.dumps(cfg))
        env = dict(os.environ, AIDJ_CACHE_DIR=str(cache), PYTHONPATH=str(REPO_ROOT))
        p = subprocess.run([sys.executable, "-m", "app.sim.stem_capture", "--worker", str(cfg_path)],
                           cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=timeout_s + 120)
        if p.returncode != 0 or not out.exists():
            raise RuntimeError(f"stem capture failed (rc {p.returncode}): {p.stderr.strip()[-1500:]}")
        cap = json.loads(out.read_text(encoding="utf-8"))
    cap["songs"] = {"a": {**songs[ida], "id": ida}, "b": {**songs[idb], "id": idb}}
    return cap


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.sim.stem_capture")
    ap.add_argument("--worker", default=None, help=argparse.SUPPRESS)
    ap.add_argument("track_a", nargs="?")
    ap.add_argument("track_b", nargs="?")
    ap.add_argument("--recipe", default="Long Blend")
    ap.add_argument("--a-time", type=float, required=False)
    ap.add_argument("--b-time", type=float, default=0.0)
    ap.add_argument("--out", required=False)
    a = ap.parse_args(argv)
    if a.worker:
        return _worker(Path(a.worker))
    if not (a.track_a and a.track_b and a.a_time is not None and a.out):
        ap.error("track_a track_b --a-time --out are required")
    cap = capture(a.track_a, a.track_b, a.recipe, a.a_time, a.b_time)
    Path(a.out).write_text(json.dumps(cap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
