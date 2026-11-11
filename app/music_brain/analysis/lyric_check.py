"""Lyric check (G25): does the vocal stem sing the lyrics of the song it is filed under?

faster-whisper (lyrics_asr) transcribes the vocal stem; that transcript is compared with the fetched lyrics
(lyrics.py, LRCLIB). A wrong file (another song, a cover, a mashup, a spoken video) does not sing these words.
Verdicts, stored in the song folder's meta.json (app/music_brain/song_store.py):

    verified      the transcript matches the lyrics (score >= VERIFIED_MIN)
    mismatch      it does not (the file is probably not this song)
    no_lyrics     words are sung but no lyrics are known to compare with
    instrumental  nothing sung was found in the vocal stem: not wrong, nothing to compare

The score is the share of the transcript's words (and word pairs) found in the lyrics, weighted toward pairs so
common words alone cannot verify a different song. Whisper mishears words, so a transcript word one or two letters
off a lyric word still counts. CJK text is compared as character pairs (no spaces to split on).
Nothing here deletes or moves audio; the verdict only feeds the cache audit flags and /api/library/bad.
"""
from __future__ import annotations

import difflib
import re
import time
import unicodedata
from pathlib import Path
from typing import Callable, List, Optional, Sequence

VERIFIED_MIN = 0.30
UNIGRAM_W = 0.4
BIGRAM_W = 0.6
FUZZY_CUTOFF = 0.8
MIN_FUZZY_LEN = 4
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


def tokens(text: str) -> List[str]:
    """Comparable tokens: lowercase words with accents and punctuation off; CJK runs become character pairs."""
    out: List[str] = []
    s = unicodedata.normalize("NFKC", text or "").lower().replace("'", "").replace("’", "")
    for run in re.findall(r"[぀-ヿ㐀-䶿一-鿿가-힯]+|[^\W_]+", s):
        if _CJK.match(run):
            out.extend(run[i:i + 2] for i in range(max(len(run) - 1, 1)))
        else:
            run = "".join(c for c in unicodedata.normalize("NFKD", run) if not unicodedata.combining(c))
            if len(run) > 1 or run.isdigit():
                out.append(run)
    return out


def _pairs(t: Sequence[str]) -> set:
    return {(a, b) for a, b in zip(t, t[1:])}


def similarity(asr_text: str, lyrics_text: str) -> float:
    """0..1: how much of what was heard is in the lyrics."""
    heard, known = tokens(asr_text), tokens(lyrics_text)
    if not heard or not known:
        return 0.0
    vocab = set(known)
    mapped: List[str] = []
    for w in heard:
        if w not in vocab and len(w) >= MIN_FUZZY_LEN:
            near = difflib.get_close_matches(w, vocab, n=1, cutoff=FUZZY_CUTOFF)
            w = near[0] if near else w
        mapped.append(w)
    uni = sum(w in vocab for w in mapped) / len(mapped)
    hp, kp = _pairs(mapped), _pairs(known)
    bi = (len(hp & kp) / len(hp)) if hp else uni
    return round(UNIGRAM_W * uni + BIGRAM_W * bi, 3)


def judge(asr_text: str, lyrics_text: str, lyrics_instrumental: bool = False) -> dict:
    """{verdict, score} from a transcript and the known lyrics (either may be empty)."""
    if not tokens(asr_text):
        return {"verdict": "instrumental", "score": None}
    if not tokens(lyrics_text):
        return {"verdict": "instrumental" if lyrics_instrumental else "no_lyrics", "score": None}
    score = similarity(asr_text, lyrics_text)
    return {"verdict": "verified" if score >= VERIFIED_MIN else "mismatch", "score": score}


def asr_text_of(doc: dict) -> str:
    """The asr.txt body: a language line, a blank line, one sung line per row."""
    lang = doc.get("language") or "unknown"
    prob = doc.get("language_prob")
    head = f"language: {lang}" + (f" ({prob})" if prob is not None else "")
    return head + "\n\n" + "\n".join(l["text"] for l in doc.get("lines") or []) + "\n"


def _lrc(lines: Sequence[dict]) -> str:
    return "\n".join(f"[{int(l['t'] // 60):02d}:{l['t'] % 60:05.2f}] {l['text']}" for l in lines) + "\n"


def default_lyrics(title: str, duration: Optional[float]) -> dict:
    """{text, lrc, instrumental} for 'Artist - Title' from the lyrics cache / LRCLIB (network on a miss)."""
    from app.music_brain.analysis import lyrics

    lines = lyrics.fetch(title, duration=duration)
    plain = lyrics.load_plain(title)
    text = "\n".join(l["text"] for l in lines) if lines else "\n".join(plain)
    return {"text": text, "lrc": _lrc(lines) if lines else None,
            "instrumental": bool(lyrics._cache_flag(title, "instrumental"))}


def default_asr(vocal_path: str) -> dict:
    from app.music_brain.analysis import lyrics_asr

    return lyrics_asr.for_stem(vocal_path)


def check_song(track_id: str, title: str, vocal_path, duration: Optional[float] = None, video: Optional[str] = None,
               asr_fn: Callable[[str], dict] = default_asr,
               lyrics_fn: Callable[[str, Optional[float]], dict] = default_lyrics,
               log: Callable[[str], None] = lambda m: None) -> Optional[dict]:
    """Check one song: write lyrics.txt / lyrics.lrc / asr.txt and the verdict into its folder.
    None (nothing written) when no ASR backend is installed: a later install must not find a stale answer."""
    from app.music_brain import song_store as ss

    doc = asr_fn(str(vocal_path))
    if doc.get("backend") is None:
        log(f"{track_id}: no ASR backend, not checked")
        return None
    lyr = lyrics_fn(title, duration) or {}
    ss.write_text(ss.asr_path(track_id), asr_text_of(doc))
    if lyr.get("text"):
        ss.write_text(ss.lyrics_path(track_id), lyr["text"].rstrip("\n") + "\n")
    if lyr.get("lrc"):
        ss.write_text(ss.lyrics_path(track_id, synced=True), lyr["lrc"])
    res = judge("\n".join(l["text"] for l in doc.get("lines") or []), lyr.get("text") or "", bool(lyr.get("instrumental")))
    artist, _, name = title.partition(" - ")
    known = {"artist": artist.strip() if name else None, "video": video, "duration": duration}
    ss.update_meta(track_id, title=(name or title).strip(), language=doc.get("language"), verdict=res["verdict"],
                   score=res["score"], checked=time.strftime("%Y-%m-%dT%H:%M:%S"),
                   **{k: v for k, v in known.items() if v is not None})
    log(f"{track_id}: {res['verdict']} score {res['score']}")
    return res
