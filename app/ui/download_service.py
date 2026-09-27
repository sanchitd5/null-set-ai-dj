"""
URL download service: YouTube / YouTube Music -> FLAC (yt-dlp).

Autopilot suggestions use "ytmsearch:Artist - Title": YouTube Music's *songs*
search (official audio, no videos/live uploads), falling back to a regular
YouTube search; both go through the same song filters (no mixes, sets, live
recordings, interviews, covers; 90 s - 9 min; title must match the query).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote_plus

try:
    import yt_dlp as _yt_dlp
except ImportError:
    _yt_dlp = None

_YT_MUSIC_RE = re.compile(r"https?://music\.youtube\.com/")
_YT_RE = re.compile(r"https?://(www\.)?(youtube\.com|youtu\.be)/")
_YTSEARCH_RE = re.compile(r"ytsearch\d*:")
# Autopilot suggestions: "ytmsearch:Artist - Title" -> YouTube Music songs search.
_YTMSEARCH_RE = re.compile(r"ytmsearch:")

# Live recordings: "(Live)", "[Live at ...]", "Song - Live", "Live at/in/from ...",
# "Artist Live Song, City" (no dash before "Live", so "Oasis - Live Forever" passes).
_LIVE_RE = re.compile(
    r"[\(\[]\s*live\b|\blive\s+(at|in|from|@|on|session|version|recording|performance)\b|"
    r"[-–]\s*live\s*$|^[^-–]+?\s+live\s+\w|\blive\s+(19|20)\d{2}\b|\blive\s+\d{1,2}[./-]\d{1,2}",
    re.IGNORECASE,
)


_AUDIO_GLOBS = ("*.flac", "*.mp3", "*.m4a", "*.opus", "*.ogg", "*.wav")


def _audio_files(directory: Path) -> set[Path]:
    return {p for g in _AUDIO_GLOBS for p in directory.glob(g)}


def _duration_secs(path: Path) -> float | None:
    try:
        import soundfile as sf

        return sf.info(str(path)).duration
    except Exception:
        return None  # unreadable by soundfile (e.g. m4a): rely on the pre-download filter


# Event / venue recordings whose title never says "live": "Skrillex at the
# Question Mark sound camp at Burning Man 2015".
_EVENT_RE = re.compile(
    r"\b(sound\s*camp|burning\s+man|coachella|tomorrowland|edc|lollapalooza|glastonbury|"
    r"creamfields|awakenings|printworks|warehouse\s+project|red\s+rocks|ultra\s+(music\s+)?festival|"
    r"festival|main\s*stage|boiler\s+room|cercle|mixmag|dj\s*mag|resident\s+advisor|hör\s+berlin|"
    r"lab\s+ldn|radio\s*1|bbc\s+radio)\b|\s@\s",
    re.IGNORECASE,
)


def _is_live(title: str) -> bool:
    return bool(_LIVE_RE.search(title) or _EVENT_RE.search(title))


# Audio check: a clip cut out of a live recording / stream starts AND ends at
# full level (no intro build, no fade). Measured on the set's files: the
# Burning Man clip sat at -0.4 dB / +1.6 dB (first/last 2 s vs the median
# level); every one of 39 studio tracks had at least one end >= 13 dB quieter.
EXCERPT_HEAD_DB = -3.0
EXCERPT_TAIL_DB = -6.0


def _looks_like_excerpt(path: Path) -> bool:
    try:
        import numpy as np
        import soundfile as sf

        y, sr = sf.read(str(path), always_2d=True, dtype="float32")
    except Exception:
        return False
    mono = y.mean(axis=1)
    hop = max(1, int(sr * 0.05))
    n = len(mono) // hop
    if n < 200:
        return False
    rms = np.sqrt(np.mean(mono[: n * hop].reshape(n, hop) ** 2, axis=1)) + 1e-9
    db = 20 * np.log10(rms)
    med = float(np.median(db))
    k = int(2 / 0.05)
    head = float(np.mean(db[:k])) - med
    tail = float(np.mean(db[-k:])) - med
    return head > EXCERPT_HEAD_DB and tail > EXCERPT_TAIL_DB

# Keywords that identify DJ mixes / live sets — reject these, only individual tracks allowed.
_MIX_KEYWORDS = re.compile(
    r"\b(mix|dj.?set|live.?set|liveset|radio.?show|podcast|essential.?mix|"
    r"boiler.?room|fabric.?live|renaissance|compilation|megamix|mashup.?mix)\b",
    re.IGNORECASE,
)


def _is_mix(title: str) -> bool:
    """Return True if the title looks like a DJ mix or set rather than a single track."""
    return bool(_MIX_KEYWORDS.search(title))


# Spoken / non-music videos (interviews, reactions, tutorials) — reject.
_NON_MUSIC_KEYWORDS = re.compile(
    r"\b(interview|talks?\s+about|in\s+conversation|conversation\s+with|podcast|"
    r"reacts?|reaction|review|tutorial|how\s+to|lesson|masterclass|documentary|"
    r"behind\s+the\s+scenes|making\s+of|explains?|trailer|q\s*&\s*a|vlog|"
    r"cover|karaoke|nightcore|slowed|sped\s+up|8d|spatial\s+audio|loop\s+version|hour\s+version|"
    # tribute / imitation uploads that reuse the real title and artist names
    r"8[\s-]?bit|16[\s-]?bit|chiptune|emulation|tribute|in\s+the\s+style\s+of|originally\s+performed|"
    r"made\s+famous|music\s+box|lullaby|piano\s+version|string\s+quartet|orchestral\s+version|"
    r"ringtone|backing\s+track|type\s+beat)\b",
    re.IGNORECASE,
)

MIN_TRACK_SECS = 90
MAX_TRACK_SECS = 9 * 60  # >9 min is almost certainly a mix / set / compilation
_SEARCH_POOL = 8  # search results to consider for ytsearch queries
_MIN_WORD_MATCH = 0.7  # fraction of query words that must appear in title+channel


def _is_non_music(title: str) -> bool:
    return bool(_NON_MUSIC_KEYWORDS.search(title))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower().replace("-", " "))


def _query_words(url: str) -> list[str]:
    """Extract the positive search words from a ytsearch URL (drops -exclusions and 'audio')."""
    q = _YTSEARCH_RE.sub("", url, count=1)
    q = re.sub(r'-"[^"]*"', " ", q)  # -"DJ set"
    q = re.sub(r"(^|\s)-\S+", " ", q)  # -mix
    words = [_norm(w).strip() for w in q.split()]
    return [w for w in words if w and w != "audio"]


# Alternate versions: only accepted when the requested title asks for one.
_VERSION_RE = re.compile(
    r"\b(remix|re-?edit|edit|rework|bootleg|vip|flip|extended|mashup|refix|dub|sped|slowed|acoustic|instrumental)\b",
    re.IGNORECASE,
)


# Sequel markers: "Victory Lap Five" is not "Victory Lap" (user, 2026-09-27).
_SEQUEL_WORDS = {"two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                 "ii", "iii", "iv", "v", "2", "3", "4", "5", "6", "7", "8", "9", "10"}


def _sequel(video_title: str, wanted_title: str) -> bool:
    """True when the video title continues the wanted title with a sequel
    marker the request doesn't have ("Victory Lap Five", "Song Pt. 2")."""
    want = _norm(re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", wanted_title)).split()
    if not want:
        return False
    vt = _norm(re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", video_title)).split()
    for i in range(len(vt) - len(want) + 1):
        if vt[i:i + len(want)] == want:
            rest = vt[i + len(want): i + len(want) + 2]
            if rest and (rest[0] in _SEQUEL_WORDS or (rest[0] in ("pt", "part") and len(rest) > 1)):
                return rest[0] not in want
    return False


def _search_match_filter(words: list[str], song: Optional[tuple[list[str], list[str], str]] = None):
    """yt-dlp match_filter: accept only a real song whose title matches the query.

    song = (artist_words, title_words, raw_title) for "Artist - Title" queries:
    stricter than `words` — the song title's words must be in the VIDEO title
    (not the channel), the artist must appear in title or channel, and remixes /
    edits are rejected unless the requested title is one ("Four Tet - Baby" must
    not resolve to "Dream Baby Dream (Four Tet Remix)").
    """

    def _filter(info: dict, *, incomplete: bool = False):
        # yt-dlp also calls this on the search-results container itself (and on
        # half-extracted entries): never reject those, only real videos.
        if info.get("_type") == "playlist" or not info.get("title"):
            return None
        title = info["title"]
        duration = info.get("duration")
        if duration is not None:
            if duration >= MAX_TRACK_SECS:
                return f"too long ({duration}s), likely a mix/set"
            if duration < MIN_TRACK_SECS:
                return f"too short ({duration}s), likely a clip"
        if _is_mix(title):
            return "title looks like a mix/set"
        if _is_non_music(title):
            return "title looks like an interview/non-music video"
        if _is_live(title):
            return "title looks like a live recording"
        if song:
            artist_w, title_w, raw_title = song
            vt = " " + _norm(title) + " "
            full = " " + _norm(f"{title} {info.get('channel') or ''} {info.get('uploader') or ''}") + " "
            t_hits = sum(1 for w in title_w if f" {w} " in vt)
            if title_w and t_hits / len(title_w) < 0.8:
                return f"song title does not match ({t_hits}/{len(title_w)} words)"
            # Prefer YouTube's artist metadata (YT Music results carry it): a tribute
            # upload puts the real artist in its TITLE, but not in its artist field.
            meta_artist = info.get("artists") or info.get("artist") or info.get("creator")
            if isinstance(meta_artist, list):
                meta_artist = " ".join(str(x) for x in meta_artist)
            artist_hay = " " + _norm(str(meta_artist)) + " " if meta_artist else full
            a_hits = sum(1 for w in artist_w if f" {w} " in artist_hay)
            if artist_w and a_hits / len(artist_w) < 0.5:
                return f"artist does not match ({a_hits}/{len(artist_w)} words)"
            if _VERSION_RE.search(title) and not _VERSION_RE.search(raw_title):
                return "alternate version (remix/edit) not requested"
            if _sequel(title, raw_title):
                return "a sequel (Two / Five / Pt. 2) of the requested song, not the song"
            return None
        if words:
            hay = " " + _norm(f"{title} {info.get('channel') or ''} {info.get('uploader') or ''}") + " "
            hits = sum(1 for w in words if w in hay)
            if hits / len(words) < _MIN_WORD_MATCH:
                return f"title does not match query ({hits}/{len(words)} words)"
        return None

    return _filter


SEARCH_LIMIT = 12


def search_songs(query: str, limit: int = 8) -> list[dict]:
    """YouTube search for songs (no download): [{id, url, title, channel,
    duration}]. Same filters as downloads: no mixes / sets / live recordings /
    interviews / covers, 90 s - 9 min. Used by LEAD TO to pick a destination."""
    if _yt_dlp is None:
        raise RuntimeError("yt-dlp not installed")
    q = " ".join(str(query or "").split())[:120]
    if len(q) < 2:
        return []
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
            "skip_download": True, "playlistend": SEARCH_LIMIT}
    with _yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{SEARCH_LIMIT}:{q}", download=False)
    out = []
    for e in (info or {}).get("entries") or []:
        vid, title = e.get("id"), e.get("title") or ""
        dur = e.get("duration")
        if not vid or not title:
            continue
        if dur is not None and not (MIN_TRACK_SECS <= dur < MAX_TRACK_SECS):
            continue
        if _is_mix(title) or _is_non_music(title) or _is_live(title):
            continue
        out.append({"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": title,
                    "channel": e.get("channel") or e.get("uploader") or "",
                    "duration": dur})
        if len(out) >= limit:
            break
    return out


# Dated uploads are concert / radio recordings: "... - April 29, 2023 - Morrison, CO".
_DATED_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(st|nd|rd|th)?,?\s+(19|20)\d{2}\b"
    r"|\b\d{1,2}(st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(19|20)\d{2}\b",
    re.IGNORECASE,
)
VERIFY_POOL = 8


def verify_song(artist: str, title: str) -> Optional[bool]:
    """Does "artist - title" exist as a real song? One flat YouTube search (~1.5 s,
    no download). True = found, False = no upload matches, None = search failed
    (caller must not drop the pick on None).

    Stricter than the download matcher: EVERY artist word must appear (so
    "AP Dhillon" does not pass on "Arjan Dhillon"), and dated concert uploads
    don't count. A local model invents plausible titles; catching them here costs
    ~1.5 s instead of a failed download round."""
    artist, title = " ".join(str(artist or "").split()), " ".join(str(title or "").split())
    if _yt_dlp is None or not artist or not title or len(artist) + len(title) > 200:
        return None
    artist_w, title_w = _words_of(artist), _words_of(re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title))
    if not artist_w or not title_w:
        return None
    base = _search_match_filter(_words_of(f"{artist} {title}"), (artist_w, title_w, title))
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
            "skip_download": True, "playlistend": VERIFY_POOL}
    try:
        with _yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{VERIFY_POOL}:{artist} - {title} audio", download=False)
    except Exception:
        return None
    for e in (info or {}).get("entries") or []:
        if not e or base(e) is not None or _DATED_RE.search(e.get("title") or ""):
            continue
        hay = " " + _norm(f"{e.get('title') or ''} {e.get('channel') or ''} {e.get('uploader') or ''}") + " "
        if all(f" {w} " in hay for w in artist_w):
            return True
    return False


def song_views(name: str) -> Optional[int]:
    """YouTube views of the best-known upload of `name` (a track display name).
    Highest view count among the top results whose title shares most of the
    name's words; None when the search fails or nothing matches."""
    q = " ".join(re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", str(name or "")).split())[:120]
    words = [w for w in _words_of(q) if w not in ("official", "audio", "video", "lyric", "lyrics", "music")]
    if _yt_dlp is None or len(words) < 1:
        return None
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
            "skip_download": True, "playlistend": 6}
    try:
        with _yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch6:{q}", download=False)
    except Exception:
        return None
    best = None
    for e in (info or {}).get("entries") or []:
        if not e or _is_mix(e.get("title") or "") or _DATED_RE.search(e.get("title") or ""):
            continue
        hay = " " + _norm(f"{e.get('title') or ''} {e.get('channel') or ''}") + " "
        if sum(1 for w in words if f" {w} " in hay) / len(words) < 0.6:
            continue
        v = e.get("view_count")
        if isinstance(v, int) and (best is None or v > best):
            best = v
    return best if best is not None else 0


def detect_source(url: str) -> str:
    """Return 'youtube_music', 'youtube', or 'unknown'."""
    if _YT_MUSIC_RE.match(url):
        return "youtube_music"
    if _YT_RE.match(url):
        return "youtube"
    if _YTSEARCH_RE.match(url):
        return "youtube"  # yt-dlp handles ytsearch: natively
    if _YTMSEARCH_RE.match(url):
        return "youtube_music"
    return "unknown"


Progress = Callable[[str, Optional[float]], None]  # (stage, percent 0-100 or None)


def _noop_progress(stage: str, percent: Optional[float] = None) -> None:
    pass


def download_to_dir(url: str, output_dir: Path, progress: Optional[Progress] = None) -> list[Path]:
    """Download audio to output_dir. Returns list of new audio file paths (FLAC).

    progress(stage, percent) is called from the download thread: percent is a
    real byte ratio while downloading, None (indeterminate) for search / convert steps.
    """
    progress = progress or _noop_progress
    output_dir.mkdir(parents=True, exist_ok=True)
    if _YTMSEARCH_RE.match(url):
        query = " ".join(_YTMSEARCH_RE.sub("", url, count=1).split())
        if not query or len(query) > 200:
            raise RuntimeError("invalid search query")
        if _is_mix(query) or _is_non_music(query) or _is_live(query):
            raise RuntimeError(f"not a studio song: \"{query}\"")
        progress("searching YouTube Music", None)
        artist, _, title = query.partition(" - ")
        if not title:
            artist, title = "", query
        song = (_words_of(artist), _words_of(title), title)
        try:
            return _ytdlp(
                f"https://music.youtube.com/search?q={quote_plus(query)}#songs",
                output_dir, progress, words=_words_of(query), song=song,
            )
        except RuntimeError as exc:
            if "No matching studio track" not in str(exc):
                raise
            progress("searching YouTube", None)  # song not on YT Music: regular search
            return _ytdlp(f"ytsearch{_SEARCH_POOL}:{query} audio", output_dir, progress,
                          words=_words_of(query), song=song)
    progress("searching YouTube" if _YTSEARCH_RE.match(url) else "fetching", None)
    return _ytdlp(url, output_dir, progress)


def _words_of(query: str) -> list[str]:
    return [w for w in _norm(query).split() if w and w != "audio"]


def _ytdlp(url: str, output_dir: Path, progress: Optional[Progress] = None,
           words: Optional[list[str]] = None,
           song: Optional[tuple[list[str], list[str], str]] = None) -> list[Path]:
    progress = progress or _noop_progress

    def _dl_hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            progress("downloading", (100.0 * done / total) if total else None)
        elif d.get("status") == "finished":
            progress("converting", None)

    if _yt_dlp is None:
        raise RuntimeError("yt-dlp not installed — run: pip install yt-dlp")

    before = _audio_files(output_dir)

    # Search queries: widen to a pool of results and take the FIRST one that is a real
    # song matching the query (not an interview, mix, clip). Direct URLs: only guard
    # length / mix / non-music.
    is_music_search = url.startswith("https://music.youtube.com/search")
    is_search = bool(_YTSEARCH_RE.match(url)) or is_music_search
    if words is None:
        words = _query_words(url) if _YTSEARCH_RE.match(url) else []
    if _YTSEARCH_RE.match(url):
        url = _YTSEARCH_RE.sub(f"ytsearch{_SEARCH_POOL}:", url, count=1)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
        "windowsfilenames": True,
        # a search results page IS a playlist; only single direct links get noplaylist
        "noplaylist": not is_search,
        "playlistend": _SEARCH_POOL,
        "match_filter": _search_match_filter(words, song),
        "progress_hooks": [_dl_hook],
        "max_downloads": 1,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            # FLAC, not MP3: YouTube's best stream is already lossy (~128 kbps
            # Opus/AAC); re-encoding it to MP3 stacked a second lossy pass.
            # FLAC keeps exactly what YouTube delivered.
            {"key": "FFmpegExtractAudio", "preferredcodec": "flac"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
        # 16-bit: the lossy source has no more resolution than that; ffmpeg's
        # default s32 FLAC doubled file size for nothing.
        "postprocessor_args": {"extractaudio": ["-sample_fmt", "s16"]},
    }
    for attempt in range(2):  # YouTube intermittently answers 403 on the first stream fetch
        try:
            with _yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            break
        except _yt_dlp.utils.MaxDownloadsReached:
            break  # got our one matching track
        except _yt_dlp.utils.DownloadError as exc:
            msg = str(exc)
            if attempt == 0 and ("403" in msg or "timed out" in msg.lower()):
                continue
            raise

    after = _audio_files(output_dir)
    new_files = sorted(after - before)
    if not new_files:
        raise RuntimeError(
            "No matching studio track found (all results were mixes, interviews, "
            "clips or title mismatches)."
        )
    return _reject_non_tracks(new_files, check_live=is_search)


def _reject_non_tracks(new_files: list[Path], check_live: bool = True) -> list[Path]:
    """Only individual studio tracks: no mixes / sets / interviews / live
    recordings / >9 min files. (Live check is skipped for a URL the user pasted.)"""
    clean = []
    for path in new_files:
        if _is_mix(path.stem) or _is_non_music(path.stem) or (check_live and _is_live(path.stem)):
            path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Downloaded file looks like a mix, set or live recording: \"{path.stem}\"."
            )
        if check_live and _looks_like_excerpt(path):
            path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Sounds like a live/stream excerpt (starts and ends at full level): \"{path.stem}\"."
            )
        # Length guard: >9 min is almost certainly a mix / set / compilation.
        secs = _duration_secs(path)
        if secs is not None and secs >= MAX_TRACK_SECS:
            path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Track too long ({secs / 60:.1f} min, likely a mix/set): \"{path.stem}\". "
                "Only individual tracks under 9 minutes are allowed."
            )
        clean.append(path)
    return clean
