"""The read-only source library (the main checkout's data/) as the sim sees it.

Used by the offline "library" world (fixtures built without network) and to pick a random
seed track. Nothing here writes into DATA_DIR: pool entries are built from the cached
analysis / vibe / energy / stems and written under app/sim/fixtures/_pool.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.music_brain.audio.audio_io import stem_file
from app.sim.pool import Pool, find_data_dir

_SPLIT = re.compile(r"\s+[-–—]\s+")
MIN_DURATION_S, MAX_DURATION_S = 120.0, 540.0


@dataclass(frozen=True)
class LibTrack:
    tid: str            # sha256[:16], the console's track id
    hash: str           # full sha256 of the audio (the cache key)
    name: str           # display name ("Artist - Title ...")
    audio: Path
    stems: Optional[dict]   # stem name -> flac/wav path (4-stem), or None
    duration: float
    bpm: float
    key: str            # Camelot, "" when unknown


class MainLibrary:
    def __init__(self, data_dir: Optional[Path] = None):
        self.data = Path(data_dir) if data_dir else find_data_dir()
        self.cache = self.data / "cache"
        self._tracks: Optional[list] = None

    # -- discovery ------------------------------------------------------------------
    def _analysis_hashes(self) -> dict:
        out = {}
        for p in (self.cache / "analysis").glob("*.v5.json"):
            out[p.name.split(".")[0][:16]] = p.name.split(".")[0]
        return out

    def _stems_for(self, h: str) -> Optional[dict]:
        for model in ("htdemucs_ft", "htdemucs"):
            d = self.cache / "stems" / f"{h}_{model}"
            paths = {n: stem_file(d, n) for n in ("drums", "bass", "vocals", "other")}   # .flac, else .wav
            if all(p is not None for p in paths.values()):
                return {k: str(v) for k, v in paths.items()}
        return None

    def tracks(self) -> list:
        """Every named upload with a v5 analysis, a vibe record and 4-stem cache. Sorted by
        name then id: the order never depends on directory listing order."""
        if self._tracks is not None:
            return self._tracks
        from app.ui.services.download_service import _is_live, _is_mix, _is_non_music

        try:
            names = json.loads((self.cache / "uploads" / "_names.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            names = {}
        hashes = self._analysis_hashes()
        out = []
        for tid in sorted(names):
            name, h = names[tid], hashes.get(tid)
            if not h or _SPLIT.search(name) is None:
                continue
            if _is_mix(name) or _is_live(name) or _is_non_music(name):
                continue
            audio = next(iter(sorted((self.cache / "uploads").glob(f"{tid}.*"))), None)
            stems = self._stems_for(h)
            if audio is None or stems is None or not (self.cache / "analysis" / f"{h}.vibe.json").exists():
                continue
            try:
                a = json.loads((self.cache / "analysis" / f"{h}.v5.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not (MIN_DURATION_S <= float(a.get("duration") or 0) <= MAX_DURATION_S):
                continue
            key = (a.get("key") or {}).get("camelot") or ""
            out.append(LibTrack(tid, h, name, audio, stems, float(a["duration"]), float(a.get("bpm") or 0), key))
        out.sort(key=lambda t: (t.name.lower(), t.tid))
        self._tracks = out
        return out

    def by_hash(self, h: str) -> Optional[LibTrack]:
        return next((t for t in self.tracks() if t.hash == h), None)

    # -- building one pool entry ------------------------------------------------------
    def build_entry(self, t: LibTrack, pool: Pool, cache_dir: Path, force: bool = False) -> dict:
        """The frozen pool entry of one library track (built once, then read from the pool).

        analysis / vibe / energy come from the source caches (energy is measured when the
        source has none: needs the audio, computed with the real energy module into
        `cache_dir`, the run's private cache). vocals + stem curves are read off the cached
        Demucs stems with the real vocal_presence_map."""
        if not force and pool.has(t.hash):
            return pool.load(t.hash)
        from app.music_brain.analysis import energy as en
        from app.sim.pool import assemble_entry

        a = json.loads((self.cache / "analysis" / f"{t.hash}.v5.json").read_text(encoding="utf-8"))
        v = json.loads((self.cache / "analysis" / f"{t.hash}.vibe.json").read_text(encoding="utf-8"))
        ep = self.cache / "analysis" / f"{t.hash}.energy.json"
        if ep.exists():
            e = json.loads(ep.read_text(encoding="utf-8"))
        else:
            cache_dir = Path(cache_dir)
            (cache_dir / "analysis").mkdir(parents=True, exist_ok=True)
            (cache_dir / "analysis" / f"{t.hash}.vibe.json").write_text(json.dumps(v), encoding="utf-8")
            e = en.measure(t.audio, float(a.get("bpm") or 0))
        entry = assemble_entry(t.hash, t.name, a, v, e, t.stems, "library")
        pool.save(t.hash, entry)
        return entry

    def library_raws(self) -> list:
        """Every measured track's raw energy score, as energy.library_raws() reads it live."""
        from app.music_brain.analysis import energy as en

        out = []
        for p in sorted((self.cache / "analysis").glob("*.energy.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if d.get("version") == en.VERSION:
                    out.append(round(en._raw(d), 4))
            except (OSError, ValueError, KeyError):
                pass
        return out

    def shared_files(self) -> dict:
        """learned_techniques.json and set_memory.json (frozen into fixtures/_shared so before / after runs see the same)."""
        from app.music_brain.learning.set_learner import load_learned

        out = {}
        learned = load_learned(self.cache / "learned_techniques.json")      # the app DB (or a pre-DB JSON)
        if learned:
            out["learned_techniques.json"] = json.dumps(learned, indent=2)
        from app.ui.services import set_memory as sm

        mem = sm.load(self.cache / "set_memory.json")                      # the user DB (or a pre-DB JSON)
        if mem:
            out["set_memory.json"] = json.dumps(mem)
        return out
