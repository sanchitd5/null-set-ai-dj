"""Time-synced lyrics for a song: what is sung when, and which line is the hook.

Source: LRCLIB's search API, cached under data/cache/lyrics/. A hit is only
used when it is provably this song: artist and title match the query, and its
duration is within DURATION_TOL_S of the local file. A wrong lyric is worse
than none (the learner would quote words the DJ never played). Manual
lyrics (set_manual) always win and are never overwritten.
An LRC is timed against the original release, so the
lines are re-aligned to the local file's own vocal stem before use (edits
and remixes start at a different point).

Used by the set learner (which words a DJ re-cut into a new line, which line
was left alone when the beat dropped out) and by the techniques that go
acapella on a song's hook before the drop.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from app.music_brain.config import CACHE_DIR

LYRICS_DIR = CACHE_DIR / "lyrics"
LRCLIB = "https://lrclib.net/api/search"
DURATION_TOL_S = 10.0          # an LRC for a different edit/length of the song is rejected
MAX_OFFSET_S = 20.0            # search range when aligning an LRC to the local file
MIN_ALIGN_GAIN_DB = 3.0        # sung lines must sit this much louder than the gaps to trust the offset

_LRC = re.compile(r"^\s*((?:\[\d+:\d+(?:\.\d+)?\]\s*)+)(.*)$")
_LRC_TAG = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]")
_CREDIT = re.compile(r"^\s*(作词|作曲|编曲|制作人|词|曲|written by|composed by|lyrics by|produced by)\s*[:：]", re.I)


def parse_lrc(text: str) -> List[dict]:
    """'[01:02.34] words' -> [{t, end, text}] (end = next line's start); blank and credit lines dropped.
    A compressed line '[00:10.00][00:30.00] chorus' is the same words at each time."""
    rows = []
    for line in (text or "").splitlines():
        m = _LRC.match(line)
        if m:
            for mm, ss in _LRC_TAG.findall(m.group(1)):
                rows.append((int(mm) * 60 + float(ss), m.group(2).strip()))
    rows.sort()
    out = []
    for i, (t, words) in enumerate(rows):
        if not words or _CREDIT.match(words):
            continue
        end = rows[i + 1][0] if i + 1 < len(rows) else t + 5.0
        out.append({"t": round(t, 2), "end": round(min(end, t + 12.0), 2), "text": words})
    # CJK lines in a mostly non-CJK lyric are provider metadata/translations, not sung words
    cjk = [bool(_CJK.search(l["text"])) for l in out]
    if out and sum(cjk) < len(out) / 2:
        out = [l for l, c in zip(out, cjk) if not c]
    return out


_JUNK = re.compile(r"(?i)\b(official\s+(music\s+)?(audio|video|visuali[sz]er)|lyric\s+video|lyrics?|visuali[sz]er|"
                   r"audio|video|explicit|clean|hd|hq|4k|remaster(ed)?|id\s*\d+|track\s+no\s+vocals)\b")
_FEAT = re.compile(r"(?i)\s*[\(\[]?\b(feat\.?|ft\.?|featuring|with)\s.*$")


def _clean_title(title: str) -> str:
    t = re.sub(r"[\(\[](?:[^\)\]]*\b(?:official|lyric|audio|video|visuali[sz]er)\b[^\)\]]*)[\)\]]", "", title.replace("_", " "), flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" -")


def split_title(title: str) -> tuple:
    """'Artist - Title (Official Audio) ft. X' -> ('Artist', 'Title'); no dash -> ('', title).
    Download-name junk and featured artists are dropped from the title part."""
    q = _clean_title(title)
    a, t = "", q
    for sep in (" - ", " – ", " — "):
        if sep in q:
            a, t = (x.strip() for x in q.split(sep, 1))
            break
    t = _FEAT.sub("", _JUNK.sub(" ", t))
    return a, re.sub(r"\s+", " ", t).strip(" -")


def _fold(s: str) -> str:
    """lowercase, accents off (Quiereme == Quiéreme), punctuation and '&' to spaces."""
    import unicodedata

    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^\w]+", " ", s.replace("'", "").replace("’", "")).strip()


def _tokens(s: str) -> set:
    return {w for w in _fold(s).split() if len(w) > 1 or w.isdigit()}


def _get(params: dict) -> List[dict]:
    import urllib.parse
    import urllib.request

    req = urllib.request.Request(f"{LRCLIB}?{urllib.parse.urlencode(params)}",
                                 headers={"User-Agent": "null-set-ai-dj (set learner)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read()) or []


def _lrclib_search(artist: str, track: str) -> List[dict]:
    """Field search and free-text search, merged: the field search misses multi-artist
    and differently-worded entries the free-text search finds."""
    tries = [{"q": f"{artist} {track}".strip()}, {"track_name": track}]
    if artist:
        tries.insert(0, {"track_name": track, "artist_name": artist})
    seen, out = set(), []
    for p in tries:
        for c in _get(p):
            if c.get("id") not in seen:
                seen.add(c.get("id"))
                out.append(c)
    return out


_VERSION = re.compile(r"\b(remix|rmx|vip|mashup|bootleg|flip|live|acoustic|instrumental|karaoke|cover|"
                      r"sped|slowed|nightcore|extended|radio edit|x)\b")


def pick(candidates: Sequence[dict], artist: str, track: str, duration: Optional[float]) -> Optional[dict]:
    """The candidate that is provably this song, else None.
    Every title word must appear in the hit (its title with any bracketed part, or its
    artist field: "Cmon LATIN MAFIA Fred edit" vs "Cmon (LATIN MAFIA & Fred edit)");
    with an artist, one artist word must be in the hit's artist. Duration: synced
    lyrics are re-aligned to the file afterwards, so a mismatch only ranks lower; untimed
    text must be within DURATION_TOL_S (nothing else checks it). Synced beats untimed.
    Returns {"instrumental": True} when the matching entries all say so."""
    tw, aw = _tokens(track) - {"remix", "edit", "mix", "version", "original"}, _tokens(artist)
    asked = set(_VERSION.findall(_fold(track)))
    ok, instr = [], 0
    for c in candidates:
        text = _tokens(c.get("trackName") or "") | _tokens(c.get("artistName") or "")
        if not tw or not tw <= text:
            continue
        if set(_VERSION.findall(_fold(c.get("trackName") or ""))) - asked:
            continue                   # a remix / mashup / live take has other words or timing
        if aw and not aw & _tokens(c.get("artistName") or "") and not aw & _tokens(c.get("trackName") or ""):
            continue
        if c.get("instrumental") or not (c.get("syncedLyrics") or c.get("plainLyrics")):
            instr += 1
            continue
        d = float(c.get("duration") or 0)
        off = abs(d - duration) if duration and d else 0.0
        if not c.get("syncedLyrics") and off > DURATION_TOL_S:
            continue
        ok.append((not c.get("syncedLyrics"), off > DURATION_TOL_S, off, c))
    if not ok:
        return {"instrumental": True} if instr else None
    return min(ok, key=lambda x: x[:3])[3]


def _cache_path(title: str, cache_dir: Optional[Path] = None) -> Path:
    # resolved at call time (not a default argument): tests redirect LYRICS_DIR
    return Path(cache_dir if cache_dir is not None else LYRICS_DIR) / f"{hashlib.sha256(_clean_title(title).lower().encode()).hexdigest()[:16]}.json"


CACHE_V = 3


def fetch(title: str, cache_dir: Optional[Path] = None, search: Optional[callable] = None,
          duration: Optional[float] = None) -> List[dict]:
    """Synced lines for 'Artist - Title' (or just a title), or [] when no hit is provably
    this song (untimed text of a hit is kept for load_plain / time_plain). Answers and
    misses are cached; a manual entry always wins.
    search(artist, track) -> LRCLIB-shaped candidates (tests inject it)."""
    q = _clean_title(title)
    if not q:
        return []
    path = _cache_path(title, cache_dir)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("manual") or cached.get("v") == CACHE_V:
            return cached["lines"]
    except (OSError, ValueError, KeyError):
        pass
    artist, track = split_title(title)
    try:
        from app.ui.services import engine

        cands = (search or engine.current().host.lrclib_search)(artist, track)
    except Exception:              # network / provider failure: not cached, retried next time
        return []
    hit = pick(cands, artist, track, duration) or {}
    lines = parse_lrc(hit.get("syncedLyrics") or "")
    data = {"v": CACHE_V, "query": q, "lines": lines,
            "plain": [] if lines else plain_lines(hit.get("plainLyrics") or ""),
            "instrumental": bool(hit.get("instrumental")),
            "source": ({"artist": hit.get("artistName"), "track": hit.get("trackName"),
                        "duration": hit.get("duration"), "id": hit.get("id")} if hit.get("id") else None)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return lines


def lrclib_get(track_id: int) -> dict:
    import urllib.request

    req = urllib.request.Request(f"https://lrclib.net/api/get/{int(track_id)}",
                                 headers={"User-Agent": "null-set-ai-dj (set learner)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def set_from_lrclib(title: str, track_id: int, cache_dir: Optional[Path] = None, get: Optional[callable] = None) -> List[dict]:
    """Pin a song's lyrics to one LRCLIB entry (https://lrclib.net/tracks/<id>) the user chose."""
    d = (get or lrclib_get)(track_id)
    if d.get("syncedLyrics"):
        lines = set_manual(title, d["syncedLyrics"], cache_dir)
    elif d.get("plainLyrics"):           # words without times: timed later against the vocal stem
        lines = []
        set_manual(title, None, cache_dir)
    else:
        raise ValueError(f"LRCLIB {track_id} has no lyrics")
    path = _cache_path(title, cache_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["plain"] = [] if lines else plain_lines(d["plainLyrics"])
    data["source"] = {"artist": d.get("artistName"), "track": d.get("trackName"), "duration": d.get("duration"), "id": track_id}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return lines


def plain_lines(text: str) -> List[str]:
    out = [l.strip() for l in (text or "").splitlines()]
    out = [l for l in out if l and not _CREDIT.match(l)]
    cjk = [bool(_CJK.search(l)) for l in out]
    return [l for l, c in zip(out, cjk) if not c] if out and sum(cjk) < len(out) / 2 else out


def load_plain(title: str, cache_dir: Optional[Path] = None) -> List[str]:
    try:
        return list(json.loads(_cache_path(title, cache_dir).read_text(encoding="utf-8")).get("plain") or [])
    except (OSError, ValueError):
        return []


PHRASE_GAP_S = 0.35            # silence in the vocal stem that separates two sung phrases


def time_plain(plain: Sequence[str], vocal: np.ndarray, sr: int, step: float = 0.05) -> List[dict]:
    """Estimated times for untimed lyric lines: the vocal stem's sung phrases, in order,
    shared out to the lines by word count. Rough (a line may land a phrase early or
    late) but in order and on real singing; every line is marked estimated."""
    if not plain or len(vocal) < sr:
        return []
    hop = int(sr * step)
    n = len(vocal) // hop
    db = 10 * np.log10(np.mean(np.square(vocal[:n * hop].reshape(n, hop)), axis=1) + 1e-12)
    sung = db > max(-45.0, float(np.percentile(db, 90)) - 25.0)
    phrases, i = [], 0
    gap = int(PHRASE_GAP_S / step)
    while i < n:
        if not sung[i]:
            i += 1
            continue
        j = i
        while j < n and (sung[j] or sung[j:j + gap].any()):
            j += 1
        if (j - i) * step >= 0.3:
            phrases.append((i * step, j * step))
        i = j
    total = sum(b - a for a, b in phrases)
    if not phrases or total <= 0:
        return []
    words = [max(1, len(l.split())) for l in plain]
    per_word = total / sum(words)
    # walk sung time: each line takes its share of singing, starting where the last ended
    out, k, used = [], 0, 0.0
    for text, w in zip(plain, words):
        need = w * per_word
        a, b = phrases[k]
        start = a + used
        while need > 1e-6 and k < len(phrases):
            a, b = phrases[k]
            take = min(need, b - a - used)
            need -= take
            used += take
            if b - a - used <= 1e-6:
                end, k, used = b, k + 1, 0.0
            else:
                end = a + used
        out.append({"t": round(start, 2), "end": round(end, 2), "text": text, "estimated": True})
        if k >= len(phrases):
            break
    return out


def for_file(title: str, vocal: Optional[np.ndarray], sr: int, duration: Optional[float] = None,
             log=lambda m: None) -> List[dict]:
    """Lines to use for this local file: verified online LRC aligned to its vocal, a
    user pin as given, or a pinned plain lyric timed against the vocal. [] otherwise."""
    raw = fetch(title, duration=duration)
    if not raw:
        plain = load_plain(title)
        return time_plain(plain, vocal, sr) if plain and vocal is not None else []
    if vocal is None:
        return raw
    al = align(raw, vocal, sr)
    if al["trusted"]:
        return al["lines"]
    if is_manual(title):
        return raw                     # pinned by the user: their word beats the aligner's
    log(f"lyrics for {title} don't sit on this file's vocal ({al['gain_db']} dB): not used")
    return []


def sample_of(title: str, cache_dir: Optional[Path] = None) -> Optional[dict]:
    """The recording this song's vocal was sampled from (app.music_brain.matching.sources), or None."""
    from app.music_brain.matching.sources import source_of

    return source_of(title)


def is_manual(title: str, cache_dir: Optional[Path] = None) -> bool:
    try:
        return bool(json.loads(_cache_path(title, cache_dir).read_text(encoding="utf-8")).get("manual"))
    except (OSError, ValueError):
        return False


def set_manual(title: str, lrc: Optional[str], cache_dir: Optional[Path] = None) -> List[dict]:
    """Pin a song's lyrics by hand (lrc text), or pin 'no lyrics' (lrc=None) for a song
    whose online lyrics are wrong. Never overwritten by fetch()."""
    lines = parse_lrc(lrc) if lrc else []
    if lrc and not lines:
        raise ValueError("no timestamped '[mm:ss.xx] words' lines in the LRC")
    path = _cache_path(title, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"v": CACHE_V, "manual": True, "query": _clean_title(title), "lines": lines},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    return lines


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", s.lower()).strip()


def hooks(lines: Sequence[dict], min_count: int = 2) -> List[dict]:
    """Repeated lines, most repeated first: [{text, count, times[]}]. The top one
    is the chorus / hook, the line a crowd sings back."""
    c = Counter(_norm(l["text"]) for l in lines if _norm(l["text"]))
    out = []
    for key, n in c.most_common():
        if n < min_count:
            break
        first = next(l for l in lines if _norm(l["text"]) == key)
        out.append({"text": first["text"], "count": n, "times": [l["t"] for l in lines if _norm(l["text"]) == key]})
    return out


def is_hook(text: str, lines: Sequence[dict]) -> bool:
    top = hooks(lines)[:3]
    return bool(text) and any(_norm(text) == _norm(h["text"]) for h in top)


def words_between(lines: Sequence[dict], t0: float, t1: float) -> str:
    """Lyric text sung between song seconds t0 and t1 (lines overlapping the span)."""
    return " / ".join(l["text"] for l in lines if l["t"] < t1 and l["end"] > t0)


def line_at(lines: Sequence[dict], t: float) -> Optional[dict]:
    return next((l for l in lines if l["t"] <= t < l["end"]), None)


def align(lines: List[dict], vocal: np.ndarray, sr: int, step: float = 0.1) -> dict:
    """Shift the LRC onto the local file: the offset (s) that puts the sung
    lines where the vocal stem is loudest relative to the gaps.
    -> {offset, gain_db, trusted, lines (shifted; [] when untrusted)}."""
    if not lines or len(vocal) < sr:
        return {"offset": 0.0, "gain_db": 0.0, "trusted": False, "lines": []}
    hop = int(sr * step)
    n = len(vocal) // hop
    db = 10 * np.log10(np.mean(np.square(vocal[:n * hop].reshape(n, hop)), axis=1) + 1e-12)
    mask0 = np.zeros(n + int(2 * MAX_OFFSET_S / step) + 2, bool)
    base = int(MAX_OFFSET_S / step)
    for l in lines:
        mask0[base + int(l["t"] / step): base + int(l["end"] / step)] = True
    best = (-1e9, 0.0)
    for k in range(-base, base + 1):          # local frame j <- LRC frame j - k: lines shifted by k*step
        m = mask0[base - k: base - k + n]
        if len(m) < n or not m.any() or m.all():
            continue
        gain = float(np.median(db[m]) - np.median(db[~m]))
        if gain > best[0]:
            best = (gain, k * step)
    gain, off = best
    trusted = gain >= MIN_ALIGN_GAIN_DB
    # untrusted: the words don't sit on this file's vocal -> wrong song or wrong edit: use none
    shifted = [l | {"t": round(l["t"] + off, 2), "end": round(l["end"] + off, 2)} for l in lines] if trusted else []
    return {"offset": round(off, 2), "gain_db": round(gain, 1), "trusted": trusted, "lines": shifted}
