"""Cleanup after a set study (set_learner.learn_set, after each part of a split learn).

Order matters, because a cleanup once deleted songs that were not in the library:

  1. register: every located, good song goes into the library through the existing
     import path (set_import.plan_tracks -> apply, i.e. POST /api/tracks or
     server._register_downloaded). Songs already there are reused, not duplicated. On
     the final pass the set recording is still on disk, so every ID slot is cut and
     registered too.
  2. repoint: the caller points the study at the library copy and saves it.
  3. sweep: only then delete clips, the clips' stems, song files whose sha256[:16] is
     now in CACHE_DIR/uploads, yt-dlp leftovers and (final pass, every ID cut
     registered) the set recording.

A file is deleted only when the library provably holds the same bytes (song) or it is
pure working data (clips, clip stems, partial downloads). Everything else is kept and
listed with the reason. Nothing runs while another live learn of the same set exists.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

# yt-dlp temp names: "X.f251.webm.part", "X.webm", "X.mp3.part", "X.ytdl"
_LEFTOVER = re.compile(r"(\.f\d+)?(\.(webm|m4a|opus|mp4|mp3))?(\.part|\.ytdl)?$", re.I)
_AUDIO = (".mp3", ".wav", ".flac", ".m4a", ".opus", ".ogg", ".webm")


def register_rows(rows: List[dict]) -> List[dict]:
    """The library's import path (set_import.apply: POST /api/tracks or
    server._register_downloaded). A seam: tests register into a temporary library."""
    from app.music_brain import set_import
    return set_import.apply(rows)


def _inside(p: Path, root: Path) -> bool:
    try:
        return Path(p).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def _size(p: Path) -> int:
    if p.is_dir():
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return p.stat().st_size


class Tidy:
    """One learn's cleanup. Never raises: a failure keeps files, it does not sink the learn."""

    def __init__(self, set_id: str, cache_dir: Path, sets_dir: Path, stems_dir: Path,
                 log: Callable[[str], None] = lambda m: None):
        self.set_id, self.log = set_id, log
        self.cache_dir, self.sets_dir, self.stems_dir = Path(cache_dir), Path(sets_dir), Path(stems_dir)
        self.songs_dir = self.sets_dir / set_id / "songs"
        self.clips_dir = self.sets_dir / set_id / "clips"
        self.freed = 0
        self.deleted: Dict[str, int] = {}
        self.kept: Dict[str, str] = {}
        self.skipped: Optional[str] = None
        self.cuts_ok = False

    # ------------------------------------------------------------------ guards
    def busy(self) -> Optional[str]:
        """Another live learn of this set still needs its files: say why, else None."""
        from app.music_brain import learn_progress as lp
        d = lp.read_one(self.set_id)
        if d and d.get("state") == "running" and d.get("pid") not in (None, os.getpid()):
            return f"another learn of {self.set_id} is running (pid {d['pid']})"
        return None

    def library_copy(self, path: Path) -> Optional[Path]:
        """The library file holding exactly these bytes (CACHE_DIR/uploads/<sha256[:16]>.*)."""
        from app.music_brain.set_import import content_id
        try:
            cid = content_id(Path(path))
        except OSError:
            return None
        return next((p for p in (self.cache_dir / "uploads").glob(f"{cid}.*")
                     if p.is_file() and not p.name.startswith("_")), None)

    # ------------------------------------------------------------------ 1. register
    def register(self, tracks: List[dict], set_audio: Optional[Path] = None) -> Dict[str, str]:
        """Import every good song (and, with set_audio, cut every ID slot) into the library.
        -> {old path: library path} for song files of this set that the library now holds."""
        from app.music_brain import set_import
        self.cuts_ok = False
        try:
            rows = register_rows(set_import.plan_tracks(self.cache_dir, self.set_id, tracks, set_audio=set_audio))
        except Exception as exc:  # noqa: BLE001 -- no registration: nothing is deleted
            self.log(f"cleanup: register failed ({exc}): keeping every file")
            self.skipped = f"register failed: {str(exc)[:200]}"
            return {}
        cuts = [r for r in rows if r["action"] == "cut"]
        self.cuts_ok = set_audio is not None and not any(r.get("error") for r in cuts)
        moved: Dict[str, str] = {}
        for r in rows:
            p = r.get("path")
            if not p or not _inside(Path(p), self.songs_dir) or not Path(p).is_file():
                continue
            lib = self.library_copy(Path(p))
            if lib:
                moved[str(p)] = str(lib)
            else:
                self.kept[str(p)] = r.get("error") or (r.get("why") if r["action"] == "skip" else None) or "not in the library"
        for r in cuts:
            if r.get("error"):
                self.kept[f"ID cut: {r['title']}"] = f"cut failed: {r['error']}"
        return moved

    # ------------------------------------------------------------------ 3. sweep
    def _rm(self, p: Path, kind: str) -> None:
        try:
            if not p.exists():
                return
            n = _size(p)
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError as exc:
            self.kept[str(p)] = f"delete failed: {exc}"
            return
        self.freed += n
        self.deleted[kind] = self.deleted.get(kind, 0) + 1
        self.kept.pop(str(p), None)

    def sweep(self, moved: Dict[str, str], clip_files: Iterable[str] = (), stem_dirs: Iterable[str] = (),
              set_path: Optional[Path] = None) -> None:
        """Delete what step 1 made safe. Every path is checked again here: a song only when
        the library copy exists now, a clip only inside this set's clips/, a stem folder only
        inside the stem cache, the recording only inside the sets dir after every ID cut."""
        if self.skipped:
            return
        for old in moved:
            p = Path(old)
            if _inside(p, self.songs_dir) and p.is_file() and self.library_copy(p):
                self._rm(p, "songs")
        for c in clip_files:
            if _inside(Path(c), self.clips_dir):
                self._rm(Path(c), "clips")
        for d in stem_dirs:
            d = Path(d)
            if _inside(d, self.stems_dir) and d.resolve() != self.stems_dir.resolve():
                self._rm(d, "clip_stems")
        self._leftovers(self.songs_dir, "leftovers")
        if set_path is None:
            return
        self._leftovers(self.sets_dir, "leftovers", prefix=self.set_id)
        sp = Path(set_path)
        if not _inside(sp, self.sets_dir):
            self.kept[str(sp)] = "set recording is the user's own file (not downloaded by the learner)"
        elif not self.cuts_ok:
            self.kept[str(sp)] = "set recording kept: an ID slot could not be cut and registered"
        else:
            self._rm(sp, "set_recording")

    def _leftovers(self, d: Path, kind: str, prefix: str = "") -> None:
        """yt-dlp partials next to a finished .mp3 of the same name."""
        if not d.is_dir():
            return
        for p in d.iterdir():
            if not p.is_file() or p.suffix.lower() not in (".part", ".ytdl", ".webm") or not p.name.startswith(prefix):
                continue
            base = _LEFTOVER.sub("", p.name)
            if base and (d / f"{base}.mp3").is_file() and f"{base}.mp3" != p.name:
                self._rm(p, kind)

    def keep_unlisted(self) -> None:
        """Audio in this set's songs/ that no step touched: listed, never deleted."""
        if self.songs_dir.is_dir():
            for p in self.songs_dir.iterdir():
                if p.suffix.lower() in _AUDIO and str(p) not in self.kept:
                    self.kept[str(p)] = "not in the library"

    def result(self) -> dict:
        out = {"freed_bytes": self.freed, "deleted": dict(self.deleted),
               "kept": [{"file": f, "reason": r} for f, r in self.kept.items()]}
        if self.skipped:
            out["skipped"] = self.skipped
        return out
