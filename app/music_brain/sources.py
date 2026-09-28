"""Source recordings a song's vocal was sampled from, and how they were rebuilt.

Fred again.. makes songs out of real recordings (Sabrina (i am a party) is
Sabrina Benaim's poem "Explaining My Depression to My Mother"). A source is
not a song and not a sampler one-shot, so it has its own store:

    data/cache/sources/<video id>/
        audio.mp3        the recording
        info.json        title, uploader, url, duration
        captions.json    timed words + phrases (YouTube captions: manual, else auto)
        links.json       songs built from it, with a note on what it means
        transforms/<label>.json   what learn_transform() found

learn_transform(source, target) answers "how was this made into that": the
target's vocal stem (Demucs) is matched window by window against the source's
own voice, at the rates speech gets stretched to (pitch-invariant onset
envelopes), then joined into fragments. Each fragment carries the caption words
it used, its stretch and its pitch shift, so the result reads as the new lyric
the producer wrote out of someone else's sentences, in their words.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np

from app.music_brain.config import CACHE_DIR

SOURCES_DIR = CACHE_DIR / "sources"
RATES = [round(r, 3) for r in np.arange(0.75, 1.34, 0.05)]    # speech stretched to a song's tempo
WIN_S, HOP_S = 2.0, 0.5
MATCH_R = 0.6
MIN_FRAG_WINDOWS = 3


def _dir(source_id: str) -> Path:
    if not re.fullmatch(r"[\w-]{6,40}", source_id or ""):
        raise ValueError(f"bad source id {source_id!r}")
    return SOURCES_DIR / source_id


def video_id(url_or_id: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", url_or_id or "")
    if m:
        return m.group(1)
    if re.fullmatch(r"[\w-]{11}", url_or_id or ""):
        return url_or_id
    raise ValueError(f"not a YouTube video link or id: {url_or_id!r}")


# ------------------------------------------------------------------ captions
def parse_json3(data: dict) -> dict:
    """YouTube json3 captions -> {"words": [{t, end, w}], "phrases": [{t, end, text}]}."""
    words, phrases = [], []
    for ev in data.get("events") or []:
        segs = [s for s in ev.get("segs") or [] if (s.get("utf8") or "").strip()]
        if not segs:
            continue
        t0 = ev.get("tStartMs", 0) / 1000
        dur = (ev.get("dDurationMs") or 0) / 1000
        text = " ".join(s["utf8"].strip() for s in segs)
        phrases.append({"t": round(t0, 2), "end": round(t0 + dur, 2), "text": re.sub(r"\s+", " ", text)})
        for s in segs:
            words.append({"t": round(t0 + (s.get("tOffsetMs") or 0) / 1000, 2), "w": s["utf8"].strip(), "ev_end": t0 + dur})
    words.sort(key=lambda w: w["t"])
    for i, w in enumerate(words):
        nxt = words[i + 1]["t"] if i + 1 < len(words) else w["ev_end"]
        w.pop("ev_end", None)
        # an event without dDurationMs (or a word stamped at its very end) leaves no gap
        # before nxt: the word still lasts, or words_between() never finds it
        w["end"] = round(min(nxt if nxt > w["t"] else w["t"] + 1.5, w["t"] + 1.5), 2)
    return {"words": words, "phrases": phrases}


def words_between(caps: dict, t0: float, t1: float) -> str:
    return " ".join(w["w"] for w in caps.get("words") or [] if w["t"] < t1 and w["end"] > t0)


def _pick_caption_track(info: dict) -> Optional[tuple]:
    """(kind, lang, url) of the best English json3 track: manual first, then auto (en-orig = as spoken)."""
    for kind, key in (("manual", "subtitles"), ("auto", "automatic_captions")):
        tracks = info.get(key) or {}
        for lang in ("en", "en-US", "en-GB", "en-orig"):
            for f in tracks.get(lang) or []:
                if f.get("ext") == "json3":
                    return kind, lang, f["url"]
        if kind == "auto":
            for lang in ("en-orig", "en"):
                for f in tracks.get(lang) or []:
                    if f.get("ext") == "json3":
                        return kind, lang, f["url"]
    return None


def _extract(url: str, opts: dict, download: bool, log: Callable[[str], None]) -> dict:
    """yt-dlp through app.music_brain.yt_guard (client fallbacks, cookies file if set,
    self-healing backoff on bot checks). YTDLP_COOKIES_FROM_BROWSER=<browser> additionally
    lets yt-dlp read that browser's login; never done by default."""
    import os

    import yt_dlp

    from app.music_brain import yt_guard

    browser = os.environ.get("YTDLP_COOKIES_FROM_BROWSER")

    def run(extra: dict) -> dict:
        o = opts | extra | ({"cookiesfrombrowser": (browser,)} if browser else {})
        with yt_dlp.YoutubeDL(o) as ydl:
            return ydl.extract_info(url, download=download) or {}
    return yt_guard.call(run)


# --------------------------------------------------------------------- store
def add(url_or_id: str, song: Optional[str] = None, note: str = "", log: Callable[[str], None] = lambda m: None) -> dict:
    """Fetch a source recording and its captions into the store (idempotent); optionally
    link it to a song ('Artist - Title') with a note on what it means."""
    import urllib.request

    import yt_dlp

    vid = video_id(url_or_id)
    d = _dir(vid)
    d.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={vid}"
    info_p, caps_p, audio = d / "info.json", d / "captions.json", d / "audio.mp3"
    if not (info_p.exists() and caps_p.exists() and audio.exists()):
        opts = {"format": "bestaudio/best", "outtmpl": str(d / "audio.%(ext)s"), "quiet": True, "no_warnings": True,
                "noprogress": True, "noplaylist": True,
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}]}
        info = _extract(url, opts, download=not audio.exists(), log=log)
        info_p.write_text(json.dumps({k: info.get(k) for k in ("id", "title", "uploader", "duration", "upload_date")}
                                     | {"url": url}, ensure_ascii=False, indent=1), encoding="utf-8")
        track = _pick_caption_track(info)
        caps = {"words": [], "phrases": [], "kind": None}
        if track:
            kind, lang, curl = track
            req = urllib.request.Request(curl, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                caps = parse_json3(json.loads(r.read())) | {"kind": kind, "lang": lang}
            log(f"captions: {kind} {lang}, {len(caps['words'])} words")
        else:
            log("no English captions on this video")
        caps_p.write_text(json.dumps(caps, ensure_ascii=False, indent=1), encoding="utf-8")
    if song:
        links = load_links(vid)
        links = [l for l in links if l["song"] != song] + [{"song": song, "note": note[:300]}]
        (d / "links.json").write_text(json.dumps(links, ensure_ascii=False, indent=1), encoding="utf-8")
    return get(vid)


def load_links(source_id: str) -> List[dict]:
    try:
        return json.loads((_dir(source_id) / "links.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def get(source_id: str) -> dict:
    d = _dir(source_id)
    info = json.loads((d / "info.json").read_text(encoding="utf-8"))
    caps = json.loads((d / "captions.json").read_text(encoding="utf-8"))
    return info | {"audio": str(d / "audio.mp3"), "captions": {"kind": caps.get("kind"), "words": len(caps["words"]),
                                                                "phrases": len(caps["phrases"])},
                   "links": load_links(source_id)}


def captions(source_id: str) -> dict:
    return json.loads((_dir(source_id) / "captions.json").read_text(encoding="utf-8"))


def source_of(song: str) -> Optional[dict]:
    """The source recording linked to a song, {title, url, note, id}, or None."""
    from app.music_brain.lyrics import _clean_title

    key = _clean_title(song).lower()
    if not SOURCES_DIR.is_dir():
        return None
    for d in sorted(SOURCES_DIR.iterdir()):
        for l in load_links(d.name) if d.is_dir() else []:
            if _clean_title(str(l.get("song") or "")).lower() == key:
                try:                       # every hook-drop request reads this: never raise
                    info = json.loads((d / "info.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    info = {}
                info = info if isinstance(info, dict) else {}
                return {"id": d.name, "title": f"{info.get('title')} ({info.get('uploader')})",
                        "url": info.get("url"), "note": l.get("note", "")}
    return None


# ----------------------------------------------------------------- transform
def _semitones(a: np.ndarray, b: np.ndarray, sr: int) -> Optional[float]:
    """Median pitch of b relative to a, in semitones (speech f0; None when unvoiced)."""
    import librosa

    def f0(y):
        try:                       # one fragment pyin cannot read (too short, NaN) must not lose the study
            f, voiced, _ = librosa.pyin(np.nan_to_num(np.asarray(y, np.float32)), fmin=70, fmax=600, sr=sr,
                                        frame_length=1024)
        except Exception:
            return None
        f = f[voiced & np.isfinite(f)]
        return float(np.median(f)) if len(f) >= 5 else None
    fa, fb = f0(a), f0(b)
    return round(12 * float(np.log2(fb / fa)), 1) if fa and fb else None


def learn_transform(source_id: str, target: str, label: Optional[str] = None, start: float = 0.0,
                    end: Optional[float] = None, log: Callable[[str], None] = lambda m: None) -> dict:
    """How the source recording was rebuilt in `target` (a song file, or a set/clip).
    start/end cut a long set to the part that uses the source (ffmpeg, then Demucs)."""
    import librosa

    from app.music_brain import set_learner as sl
    from app.music_brain.stem_service import separate

    d = _dir(source_id)
    caps = captions(source_id)
    target = Path(target)
    if not target.is_file():
        raise FileNotFoundError(target)
    if start or end:
        end = end or float(librosa.get_duration(path=str(target)))
        # clips are named by span only: one folder per target, or a second target cut at
        # the same span would be served the first one's audio
        tkey = hashlib.sha256(str(target.resolve()).encode()).hexdigest()[:12]
        target = sl.clip_audio(target, float(start), float(end), d / "clips" / tkey)
    log("separating source + target vocals")
    src_v = sl._load(separate(d / "audio.mp3").stems["vocals"])
    tgt_v = sl._load(separate(target).stems["vocals"])
    src = sl.SongData("source", 0.0, 0.0, None, {"vocals": sl.onset_env(src_v)}, vocal_db=sl.vocal_db_curve(src_v))
    log("locating the target's vocal in the source")
    rows = sl.stem_timeline({"vocals": tgt_v}, [src], lambda t: 0.0, WIN_S, HOP_S, only=("vocals",),
                            min_r=MATCH_R, track=False, rates=RATES)
    rows = sl._only_sung(rows, [src], WIN_S)
    runs = sl.vocal_runs(rows, HOP_S, [src], WIN_S).get(0, [])
    runs = [r for r in runs if r["set_t1"] - r["set_t0"] >= (MIN_FRAG_WINDOWS - 1) * HOP_S - 1e-6]
    rate_at = {r["t"]: r["owner"].rate for r in rows if r["owner"]}
    frags = []
    for r in runs:
        rate = float(np.median([rate_at[t] for t in rate_at if r["set_t0"] <= t <= r["set_t1"]] or [1.0]))
        t0, t1 = r["set_t0"], r["set_t1"] + WIN_S
        s0, s1 = r["src_t0"], r["src_t1"] + WIN_S * rate
        a, b = tgt_v[int(t0 * sl.SR):int(t1 * sl.SR)], src_v[int(s0 * sl.SR):int(s1 * sl.SR)]
        frags.append({"at": round(t0 + start, 2), "end": round(t1 + start, 2), "src": [round(s0, 2), round(s1, 2)],
                      "words": words_between(caps, s0, s1), "stretch": round(1 / rate, 3),
                      "semitones": _semitones(b, a, sl.SR) if min(len(a), len(b)) > sl.SR else None})
    # summary: what of the source was used, and how
    used = sorted({round(x, 1) for f in frags for x in np.arange(f["src"][0], f["src"][1], 0.1)})
    spoken = sum(max(0.0, w["end"] - w["t"]) for w in caps.get("words") or []) or float(len(src_v) / sl.SR)
    repeats = sum(1 for i, f in enumerate(frags) for g in frags[:i] if abs(f["src"][0] - g["src"][0]) <= 1.0)
    reorders = sum(1 for f, g in zip(frags, frags[1:]) if g["src"][0] < f["src"][1] - 1.0)
    semis = [f["semitones"] for f in frags if f["semitones"] is not None]
    summary = {
        "fragments": len(frags), "source_used_s": round(len(used) * 0.1, 1),
        "source_coverage": round(min(1.0, len(used) * 0.1 / spoken), 3),
        "repeats": repeats, "reorders": reorders,
        "median_stretch": round(float(np.median([f["stretch"] for f in frags])), 3) if frags else None,
        "median_semitones": round(float(np.median(semis)), 1) if semis else None,
        "new_lyric": [f["words"] for f in frags if f["words"]],
        "target_vocal_matched": round(sum(f["end"] - f["at"] for f in frags) /
                                      max(1e-9, float(np.mean(sl.vocal_db_curve(tgt_v) > sl.SILENT_DB)) * len(tgt_v) / sl.SR), 3),
    }
    label = re.sub(r"[^\w-]+", "_", label or Path(target).stem)[:60]
    out = {"source": source_id, "target": str(target), "label": label, "start": start, "summary": summary, "fragments": frags}
    (d / "transforms").mkdir(parents=True, exist_ok=True)
    (d / "transforms" / f"{label}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out
