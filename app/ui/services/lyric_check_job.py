"""The `lyric_check` job (G25): for every library song that has a vocal stem and no verdict yet, run faster-whisper
on the vocal stem and compare it with the lyrics (app/music_brain/analysis/lyric_check.py). Idle-aware: it is an
idle_only job (never during a live set) and it waits while another process uses the shared GPU. The verdict in the
song folder's meta.json is the resume marker, so a stopped run continues where it left off."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, Optional

from app.music_brain import song_store as ss
from app.music_brain.analysis import lyric_check


def pending(tracks: Dict[str, Path], force: bool = False, hash_fn: Optional[Callable[[Path], str]] = None) -> list:
    """[(track id, path, vocal stem)] of songs with a vocal stem that still need a verdict."""
    from app.music_brain.audio.stem_service import file_hash

    hash_fn = hash_fn or file_hash
    out = []
    for tid, path in sorted(tracks.items()):
        if not force and ss.verdict(tid):
            continue
        try:
            vocal = ss.vocal_stem(tid, audio_hash=hash_fn(Path(path)))
        except OSError:
            continue
        if vocal is not None:
            out.append((tid, Path(path), vocal))
    return out


def run(tracks: Dict[str, Path], names: Dict[str, str], ctx, limit: int = 0, force: bool = False,
        gate: Optional[Callable[[], bool]] = None, sleep: Callable[[float], None] = time.sleep,
        asr_fn=lyric_check.default_asr, lyrics_fn=lyric_check.default_lyrics, hash_fn=None) -> dict:
    from app.music_brain.analysis import lyrics_asr
    from app.music_brain.audio import audio_io
    from app.ui.services import album_art

    gate = gate if gate is not None else lyrics_asr.gpu_busy
    todo = pending(tracks, force, hash_fn)
    if limit:
        todo = todo[:limit]
    out = {"todo": len(todo), "checked": 0, "failed": 0, "by_verdict": {}}
    ctx.log(f"{len(todo)} song(s) to check")
    try:
        for i, (tid, path, vocal) in enumerate(todo, 1):
            if ctx.should_stop():
                break
            while gate() and not ctx.should_stop():
                lyrics_asr.release()                 # hand the card back while someone else works
                ctx.log("GPU busy: waiting")
                sleep(lyrics_asr.GATE_POLL_S)
            if ctx.should_stop():
                break
            ctx.progress(i - 1, len(todo), tid)
            try:
                dur = audio_io.audio_info(path)[3]
            except OSError:
                dur = None
            try:
                res = lyric_check.check_song(tid, names.get(tid) or Path(path).stem, vocal, dur,
                                             video=album_art._video_id(tid), asr_fn=asr_fn, lyrics_fn=lyrics_fn, log=ctx.log)
            except Exception as exc:                 # one bad file must not stop the batch
                out["failed"] += 1
                ctx.log(f"{tid}: failed ({type(exc).__name__}: {str(exc)[:200]})")
                continue
            if res:
                out["checked"] += 1
                out["by_verdict"][res["verdict"]] = out["by_verdict"].get(res["verdict"], 0) + 1
    finally:
        lyrics_asr.release()
    return out
