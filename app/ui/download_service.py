"""
URL download service: YouTube, YouTube Music, Spotify → MP3.

YouTube / YouTube Music: yt-dlp (direct stream extraction).
Spotify track/album/playlist: spotdl (matches on YouTube Music, downloads MP3).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

try:
    import yt_dlp as _yt_dlp
except ImportError:
    _yt_dlp = None

_SPOTIFY_RE = re.compile(
    r"https?://open\.spotify\.com/(track|album|playlist)/[A-Za-z0-9]+"
)
_YT_MUSIC_RE = re.compile(r"https?://music\.youtube\.com/")
_YT_RE = re.compile(r"https?://(www\.)?(youtube\.com|youtu\.be)/")
_YTSEARCH_RE = re.compile(r"ytsearch\d*:")

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
    r"cover|karaoke|nightcore|slowed|sped\s+up|8d|spatial\s+audio|loop\s+version|hour\s+version)\b",
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


def _search_match_filter(words: list[str]):
    """yt-dlp match_filter: accept only a real song whose title matches the query."""

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
        if words:
            hay = " " + _norm(f"{title} {info.get('channel') or ''} {info.get('uploader') or ''}") + " "
            hits = sum(1 for w in words if w in hay)
            if hits / len(words) < _MIN_WORD_MATCH:
                return f"title does not match query ({hits}/{len(words)} words)"
        return None

    return _filter


def detect_source(url: str) -> str:
    """Return 'spotify', 'youtube_music', 'youtube', 'ytsearch', or 'unknown'."""
    if _SPOTIFY_RE.match(url):
        return "spotify"
    if _YT_MUSIC_RE.match(url):
        return "youtube_music"
    if _YT_RE.match(url):
        return "youtube"
    if _YTSEARCH_RE.match(url):
        return "youtube"  # yt-dlp handles ytsearch: natively
    return "unknown"


def download_to_dir(url: str, output_dir: Path) -> list[Path]:
    """Download audio to output_dir. Returns list of new .mp3 paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    source = detect_source(url)
    if source == "spotify":
        return _spotdl(url, output_dir)
    return _ytdlp(url, output_dir)


def _ytdlp(url: str, output_dir: Path) -> list[Path]:
    if _yt_dlp is None:
        raise RuntimeError("yt-dlp not installed — run: pip install yt-dlp")

    before = set(output_dir.glob("*.mp3"))

    # Search queries: widen to a pool of results and take the FIRST one that is a real
    # song matching the query (not an interview, mix, clip). Direct URLs: only guard
    # length / mix / non-music.
    is_search = bool(_YTSEARCH_RE.match(url))
    words = _query_words(url) if is_search else []
    if is_search:
        url = _YTSEARCH_RE.sub(f"ytsearch{_SEARCH_POOL}:", url, count=1)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
        "windowsfilenames": True,
        "noplaylist": True,
        "match_filter": _search_match_filter(words),
        "max_downloads": 1,
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            },
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail"},
        ],
        "postprocessor_args": {
            "FFmpegExtractAudio": ["-id3v2_version", "3"],
        },
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

    after = set(output_dir.glob("*.mp3"))
    new_files = sorted(after - before)
    if not new_files:
        raise RuntimeError(
            "No matching studio track found (all results were mixes, interviews, "
            "clips or title mismatches)."
        )
    # Reject DJ mixes / live sets — only individual studio tracks allowed.
    clean = []
    for path in new_files:
        if _is_mix(path.stem) or _is_non_music(path.stem):
            path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Downloaded file looks like a DJ mix/set: \"{path.stem}\". "
                "Try a more specific search query."
            )
        # Secondary duration guard using file size heuristic (320 kbps MP3).
        # 9 min × 60 s × 320 000 bit/s / 8 = ~21.6 MB. Anything bigger → reject.
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > 22:
            path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Track too long (>{size_mb:.0f} MB — likely a mix/set): \"{path.stem}\". "
                "Only individual tracks under 9 minutes are allowed."
            )
        clean.append(path)
    return clean


def _spotdl(url: str, output_dir: Path) -> list[Path]:
    before = set(output_dir.glob("*.mp3"))

    result = subprocess.run(
        [
            sys.executable, "-m", "spotdl",
            "download", url,
            "--output", str(output_dir),
            "--format", "mp3",
            "--bitrate", "320k",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"spotdl failed:\n{result.stderr.strip() or result.stdout.strip()}"
        )

    after = set(output_dir.glob("*.mp3"))
    return sorted(after - before)
