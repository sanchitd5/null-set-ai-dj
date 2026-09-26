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
    r"[-–]\s*live\s*$|^[^-–]+?\s+live\s+\w",
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


def _is_live(title: str) -> bool:
    return bool(_LIVE_RE.search(title))

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
            return None
        if words:
            hay = " " + _norm(f"{title} {info.get('channel') or ''} {info.get('uploader') or ''}") + " "
            hits = sum(1 for w in words if w in hay)
            if hits / len(words) < _MIN_WORD_MATCH:
                return f"title does not match query ({hits}/{len(words)} words)"
        return None

    return _filter


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
