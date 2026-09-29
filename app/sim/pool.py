"""Per-track fixture data (the "pool") and the paths the virtual set works in.

A pool entry is everything the real pipeline computes for one downloaded song, frozen:
the analysis JSON, the vibe and energy measurements, the vocal regions and a per-second
RMS curve of each stem (full band and above 150 Hz, the numbers stem-moves.js judges a
transition on). Entries are keyed by the sha256 of the audio, live in
app/sim/fixtures/_pool/<sha256>.json and are shared by every fixture run. Audio and
stems themselves never enter git: they stay in the source data dir.

Where things live (all overridable by env):
    DATA_DIR       read-only source library (defaults to the main checkout's data/)
    SIM_DATA_DIR   scratch space for run caches (default ~/.cache/aidj-sim, never in git)
    fixtures       app/sim/fixtures (in git)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

SIM_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SIM_ROOT.parent.parent
FIXTURES_DIR = Path(os.environ.get("SIM_FIXTURES_DIR") or SIM_ROOT / "fixtures")
POOL_DIR = FIXTURES_DIR / "_pool"
SHARED_DIR = FIXTURES_DIR / "_shared"
MARKER = b"SIMAUDIO:"
STEM_NAMES = ("drums", "bass", "vocals", "other")
HOP_S = 1.0
AUDIBLE_HZ = 150.0


def find_data_dir() -> Path:
    """The library the sim reads: $DATA_DIR, else the nearest ancestor data/ with a cache."""
    env = os.environ.get("DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    for p in [REPO_ROOT, *REPO_ROOT.parents]:
        d = p / "data" / "cache" / "analysis"
        if d.is_dir() and next(d.glob("*.v5.json"), None) is not None:     # config.py creates empty cache dirs
            return p / "data"
    return REPO_ROOT / "data"


def sim_data_dir() -> Path:
    p = Path(os.environ.get("SIM_DATA_DIR") or Path.home() / ".cache" / "aidj-sim").expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _np_default(o):
    if hasattr(o, "item"):
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def _dump(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False, default=_np_default)


def round_floats(x, nd: int = 4):
    if hasattr(x, "item") and not isinstance(x, (list, dict)):
        x = x.item()
    if isinstance(x, float):
        return round(x, nd)
    if isinstance(x, list):
        return [round_floats(v, nd) for v in x]
    if isinstance(x, dict):
        return {k: round_floats(v, nd) for k, v in x.items()}
    return x


class Pool:
    """The shared per-track store. `root` defaults to the committed fixtures."""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else POOL_DIR
        self._names: Optional[dict] = None

    def path(self, h: str) -> Path:
        return self.root / f"{h}.json"

    def has(self, h: str) -> bool:
        return self.path(h).exists()

    def load(self, h: str) -> Optional[dict]:
        p = self.path(h)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def save(self, h: str, entry: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path(h).write_text(_dump(round_floats(entry)) + "\n", encoding="utf-8")
        self._names = None

    def hashes(self) -> list:
        return sorted(p.stem for p in self.root.glob("*.json")) if self.root.is_dir() else []

    def names(self) -> dict:
        """hash -> display name of every pooled track (cheap: name field only, cached)."""
        if self._names is None:
            self._names = {}
            for h in self.hashes():
                try:
                    self._names[h] = json.loads(self.path(h).read_text(encoding="utf-8")).get("name", "")
                except (OSError, ValueError):
                    pass
        return self._names


def marker_bytes(h: str) -> bytes:
    """Content of the placeholder file that stands in for a song's audio in replay."""
    return MARKER + h.encode("ascii")


def marker_hash(path: Path) -> Optional[str]:
    """The audio hash a placeholder file stands for, or None for a real file."""
    try:
        if path.stat().st_size > 200:
            return None
        head = path.read_bytes()
    except OSError:
        return None
    return head[len(MARKER):].decode("ascii") if head.startswith(MARKER) else None


# ---- assembling an entry from a real analysed + separated song ---------------------------
def assemble_entry(h: str, name: str, analysis: dict, vibe: dict, energy: dict, stem_paths: Optional[dict],
                   source: str, hook_drops: Optional[list] = None, vocal_regions: Optional[list] = None) -> dict:
    """Freeze one song's pipeline results. Everything is computed with the real functions:
    vocal_presence_map (vocal regions), techniques.stem_map (per phrase stem energy, what
    server._pair_features feeds full_groove_runs / breakdowns), techniques.vocal_style
    (rap / sung, per 20 s chunk of every sung region) and the stem curves."""
    entry = {"hash": h, "name": name, "source": source, "analysis": analysis, "vibe": vibe, "energy": energy,
             "vocals": [], "stems": None, "smap": [], "vocal_style": {}, "hook_drops": hook_drops or []}
    if not stem_paths:
        return entry
    import librosa

    from app.music_brain import techniques as tq
    from app.music_brain.analyzer import vocal_presence_map

    regions = vocal_regions
    if regions is None:
        regions = [[float(s), float(e)] for s, e in vocal_presence_map(Path(stem_paths["vocals"]))]
    entry["vocals"] = regions
    entry["stems"] = stem_curves(stem_paths)
    audio = {n: librosa.load(stem_paths[n], sr=11025, mono=True)[0] for n in tq.STEMS}
    entry["smap"] = tq.stem_map(audio, 11025, analysis.get("phrase_boundaries_8bar") or [])
    styles = {}
    for s, e in regions:
        if e - s < 12:
            continue
        y, sr = librosa.load(stem_paths["vocals"], sr=16000, mono=True, offset=s, duration=min(e, s + 20) - s)
        styles[repr(round(float(s), 3))] = tq.vocal_style(y, sr)
    entry["vocal_style"] = styles
    return entry


# ---- stem curves ---------------------------------------------------------------------
def stem_curves(stem_paths: dict, hop: float = HOP_S) -> Optional[dict]:
    """{hop, rms: {stem: [..]}, aud: {stem: [..]}} from four decoded stem files, or None.

    rms  = RMS of channel 0 per hop seconds (what stemEnergyBars reads off the decoded buffer)
    aud  = the same above AUDIBLE_HZ (4th order Butterworth high-pass: stem-moves audibleRms)
    """
    import numpy as np
    import soundfile as sf
    from scipy import signal

    out = {"hop": hop, "rms": {}, "aud": {}}
    for name in STEM_NAMES:
        p = stem_paths.get(name)
        if not p or not Path(p).exists():
            return None
        info = sf.info(str(p))
        sr = info.samplerate
        y = sf.read(str(p), always_2d=True, dtype="float32")[0][:, 0]
        n = int(sr * hop)
        m = len(y) // n
        if m == 0:
            return None
        sos = signal.butter(4, AUDIBLE_HZ, btype="highpass", fs=sr, output="sos")
        hp = signal.sosfilt(sos, y)
        seg = y[: m * n].reshape(m, n).astype(np.float64)
        segh = hp[: m * n].reshape(m, n).astype(np.float64)
        out["rms"][name] = [float(v) for v in np.sqrt((seg ** 2).mean(axis=1))]
        out["aud"][name] = [float(v) for v in np.sqrt((segh ** 2).mean(axis=1))]
    return out
