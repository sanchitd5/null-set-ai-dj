"""CLEANLIB: the span of a song that is real music. Some songs carry an advertisement, a channel promo, credits or
spoken talk in their intro or outro (or a long silent tail). `analyse` finds that head and tail with the same
speech-vs-music rule as not_music.py and stores `music_span` in the song's analysis record (contract:
wt/MUSIC_SPAN_CONTRACT.txt):

    "music_span": {"v": 1, "start": sec, "end": sec, "head": null|"speech"|"silence", "tail": null|"speech"|"silence"}

Deck play starts at `start`; the song is treated as ending at `end`; the transition planner never plans a point
outside [start, end]. Field absent = not judged, use the whole song. Conservative: it needs >= MIN_RUN_S of
speech / silence at the edge, never trims more than MAX_TRIM of the song, and keeps a cappella intros (a window
only counts as speech with pauses and no steady beat). Edges snap to the beat grid when it is known.
"""
from __future__ import annotations

import bisect
import json
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app.ui.services import not_music as nm

VERSION = 1
WIN_S = 6.0
HEAD_S = 60.0
TAIL_S = 96.0
MIN_RUN_S = 12.0
MAX_TRIM = 0.25
SILENT_RMS = 0.004
STEM_SPEECH = 0.08            # instrumental share of the stems below which a window has no music under the voice


def _windows(x, n_win: int) -> list:
    w = int(WIN_S * nm.SR)
    return [x[i * w:(i + 1) * w] for i in range(n_win) if len(x[i * w:(i + 1) * w]) >= w // 2]


def classify(x, stems_x: Optional[Dict[str, object]] = None) -> str:
    """"music" | "speech" | "silence" for one window of samples (stems_x: the same window of each stem)."""
    import numpy as np

    if len(x) < nm.SR * 3 or float(np.sqrt((x ** 2).mean())) < SILENT_RMS:
        return "silence"
    f = nm.feats(x if len(x) >= nm.SR * 5 else np.pad(x, (0, nm.SR * 5 - len(x))))
    if not f or f["pause"] < 0.04 or f["beat"] >= 0.35:          # sung a cappella / steady beat: music
        return "music"
    if stems_x:
        e = {k: float((v ** 2).mean()) for k, v in stems_x.items()}
        ins = e.get("bass", 0) + e.get("drums", 0) + e.get("other", 0)
        if ins / (ins + e.get("vocals", 0) + 1e-12) >= STEM_SPEECH:
            return "music"
    return "speech" if nm.speech_from_feats(f) >= 0.75 else "music"


def _run(labels: List[str]) -> int:
    """How many windows from the start of `labels` are not music (silence / speech). One music-looking window
    between two non-music ones does not end the run (speech is noisy window to window)."""
    n = 0
    for i, lab in enumerate(labels):
        if lab == "music" and not (i + 1 < len(labels) and labels[i + 1] != "music" and n > 0):
            break
        n += 1
    while n and labels[n - 1] == "music":
        n -= 1
    return n


def _snap(t: float, beats: List[float], up: bool) -> float:
    if not beats:
        return t
    i = bisect.bisect_left(beats, t) if up else bisect.bisect_right(beats, t) - 1
    return float(beats[min(max(i, 0), len(beats) - 1)])


def analyse(path: Path, dur: Optional[float] = None, stems: Optional[Dict[str, Path]] = None,
            beats: Optional[List[float]] = None, pcm: Callable = nm._pcm) -> Optional[dict]:
    """The music_span record of one song, or None when the file could not be read."""
    dur = dur or nm._duration(path)
    if not dur or dur < 60:
        return None
    head_len, tail_len = min(HEAD_S, dur * 0.2), min(TAIL_S, dur * 0.25)
    have_stems = bool(stems) and all(k in stems for k in ("vocals", "bass", "drums", "other"))

    def labels(start: float, length: float) -> List[str]:
        x = pcm(path, start, length)
        sx = {k: pcm(stems[k], start, length) for k in ("vocals", "bass", "drums", "other")} if have_stems else None
        n = int(length // WIN_S)
        out = []
        for i in range(n):
            a, b = int(i * WIN_S * nm.SR), int((i + 1) * WIN_S * nm.SR)
            out.append(classify(x[a:b], {k: v[a:b] for k, v in sx.items()} if sx else None))
        return out

    hl, tl = labels(0.0, head_len), labels(dur - tail_len, tail_len)
    start, end, head, tail = 0.0, float(dur), None, None
    hn = _run(hl)
    if hn * WIN_S >= MIN_RUN_S and len(hl) > hn:
        start, head = hn * WIN_S, "speech" if "speech" in hl[:hn] else "silence"
    tn = _run(tl[::-1])
    if tn * WIN_S >= MIN_RUN_S and len(tl) > tn:
        end, tail = dur - tail_len + (len(tl) - tn) * WIN_S, "speech" if "speech" in tl[len(tl) - tn:] else "silence"
    if (start + (dur - end)) > MAX_TRIM * dur:
        start, end, head, tail = 0.0, float(dur), None, None
    if head:
        start = _snap(start, beats or [], True)
    if tail:
        end = _snap(end, beats or [], False)
    return {"v": VERSION, "start": round(start, 2), "end": round(end, 2), "head": head, "tail": tail}


# ------------------------------------------------------------------------------------------------ job

def run(tracks: Dict[str, Path], cache: Path, log: Callable[[str], None] = lambda s: None,
        should_stop: Callable[[], bool] = lambda: False, limit: int = 0, redo: bool = False,
        span_fn: Optional[Callable[[Path, dict], Optional[dict]]] = None) -> dict:
    """Write music_span into the v6 analysis record of every song that has one and no span yet."""
    from app.music_brain.analysis import analyzer

    span_fn = span_fn or (lambda p, rec: analyse(p, rec.get("duration"), nm._stems_of(p.parent.name), rec.get("beat_times")))
    judged = trimmed = head_n = tail_n = 0
    found = []
    for tid, path in list(tracks.items()):
        if should_stop() or (limit and judged >= limit):
            break
        path = Path(path)
        try:
            cp = analyzer._cache_path_for(path)
            if not cp.exists():
                continue
            rec = json.loads(cp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if rec.get("music_span") and not redo:
            continue
        try:
            span = span_fn(path, rec)
        except Exception as exc:                        # noqa: BLE001 - one bad file never stops the job
            log(f"{tid}: span failed: {exc!r}")
            continue
        if span is None:
            continue
        rec["music_span"] = span
        analyzer._write_json(cp, rec)
        analyzer._mem_index.clear()
        judged += 1
        if span["head"] or span["tail"]:
            trimmed += 1
            head_n += bool(span["head"])
            tail_n += bool(span["tail"])
            found.append({"id": tid, **span})
    log(f"music_span: judged {judged}, {trimmed} trimmed (head {head_n}, tail {tail_n})")
    return {"judged": judged, "trimmed": trimmed, "head": head_n, "tail": tail_n, "found": found}
