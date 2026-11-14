"""Prod canary for the stem caches (STEMGUARD): a few random song folders with a complete stem set must
have stems as long as the song's own audio, and no derived cache dir (stem_windows/<key>/) may hold files of
two songs. Cheap (header reads + sidecar JSON), run from the stem server (GET /v1/canary) or the CLI
(`python -m app.stem_server canary [N]`, exit 1 on failure)."""
from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import List, Optional

from app.music_brain import song_store
from app.music_brain.audio import audio_io
from app.music_brain.audio.stem_service import FOUR_STEM_NAMES
from app.stem_server import windows

ID_RE = re.compile(r"^[0-9a-f]{16}$")
SLACK_S = 1.5          # decoded song length vs stem length: mp3 duration estimates are a little loose
SLACK_REL = 0.01


def _ready_set(songs: Path, track_id: str, models: List[str]) -> Optional[tuple[dict, str]]:
    for m in models:
        st = audio_io.read_manifest(songs / track_id / "stems" / m)
        if st and all(st.get(n) for n in FOUR_STEM_NAMES):
            return st, m
    return None


def shared_cache_problems(windows_dir: Path) -> List[str]:
    """Derived cache dirs whose files name more than one track, or a track that is not the dir's key owner."""
    out: List[str] = []
    try:
        dirs = [d for d in windows_dir.iterdir() if d.is_dir()]
    except OSError:
        return out
    for d in dirs:
        ids = set()
        for mp in d.glob("*.json"):
            try:
                tid = json.loads(mp.read_text(encoding="utf-8")).get("track_id")
            except (OSError, ValueError):
                continue
            if tid:                                  # a sidecar from before the identity binding has none
                ids.add(tid)
        if len(ids) > 1:
            out.append(f"{d.name}: files of {len(ids)} songs share this cache dir")
        elif ids and not d.name.startswith(next(iter(ids))):
            out.append(f"{d.name}: holds files of {next(iter(ids))}, not its own song")
    return out


def run(cache: Path, models: List[str], sample: int = 5, rng: Optional[random.Random] = None) -> dict:
    songs = Path(cache) / "songs"
    rng = rng or random.Random()
    ready = []
    try:
        ids = sorted(e.name for e in songs.iterdir() if ID_RE.match(e.name) and e.is_dir())
    except OSError:
        ids = []
    for tid in ids:
        r = _ready_set(songs, tid, models)
        if r:
            ready.append((tid, *r))
    checked, problems = [], []
    for tid, stems, model in rng.sample(ready, min(sample, len(ready))):
        audio = song_store._folder_audio(str(songs / tid))
        try:
            frames = {n: windows.total_frames(stems[n]) for n in FOUR_STEM_NAMES}
            audio_s = audio_io.audio_info(audio)[3] if audio else None
        except Exception as exc:  # noqa: BLE001 -- unreadable = a finding, not a crash
            problems.append(f"{tid}: unreadable ({exc})")
            continue
        checked.append(tid)
        if len(set(frames.values())) != 1:
            problems.append(f"{tid}: stems differ in length {frames}")
        if audio_s is None:
            problems.append(f"{tid}: has stems but no audio file")
            continue
        stem_s = max(frames.values()) / windows.SAMPLE_RATE
        if abs(stem_s - audio_s) > max(SLACK_S, SLACK_REL * audio_s):
            problems.append(f"{tid}: stems {stem_s:.1f} s vs song audio {audio_s:.1f} s ({model})")
    problems += shared_cache_problems(Path(cache) / "stem_windows")
    return {"ok": not problems, "sets_ready": len(ready), "checked": checked, "problems": problems}
