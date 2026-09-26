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
# Autopilot suggestions: "spotsearch:Artist - Title" -> spotdl looks the song up on
# Spotify (real released tracks only) and fetches duration-matched audio.
_SPOTSEARCH_RE = re.compile(r"spotsearch:")

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
        if _is_live(title):
            return "title looks like a live recording"
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
    if _SPOTSEARCH_RE.match(url):
        return "spotify_search"
    return "unknown"


def download_to_dir(url: str, output_dir: Path) -> list[Path]:
    """Download audio to output_dir. Returns list of new audio file paths (FLAC)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    source = detect_source(url)
    if source == "spotify":
        return _spotdl(url, output_dir)
    if source == "spotify_search":
        return _spotdl_search(_SPOTSEARCH_RE.sub("", url, count=1), output_dir)
    return _ytdlp(url, output_dir)


SPOTDL_TIMEOUT_SECS = 240


def _spotdl_search(query: str, output_dir: Path) -> list[Path]:
    """Find a released song on Spotify by "Artist - Title" and download it.

    No Spotify match means the song most likely does not exist (LLM made it up),
    so this raises instead of falling back to a loose YouTube search, which is
    how live recordings and sets used to slip in.
    """
    query = " ".join(query.split())
    if not query or query.startswith("-") or len(query) > 200:
        raise RuntimeError("invalid Spotify search query")
    if _is_mix(query) or _is_non_music(query) or _is_live(query):
        raise RuntimeError(f"not a studio song: \"{query}\"")

    before = _audio_files(output_dir)
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "spotdl", "download", query,
                "--output", str(output_dir / "{artists} - {title}.{output-ext}"),
                "--format", "flac",
            ],
            capture_output=True,
            text=True,
            timeout=SPOTDL_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Spotify download timed out for \"{query}\"")
    new_files = sorted(_audio_files(output_dir) - before)
    if not new_files:
        detail = (result.stdout or result.stderr or "").strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"no Spotify match for \"{query}\" ({detail[0][:120]})")
    # spotdl takes Spotify's top hit even when it is unrelated (a made-up song
    # came back as a random chart track), so the result must actually be the
    # requested song: most query words present in "{artists} - {title}".
    words = [w for w in _norm(query).split() if len(w) > 1]
    for path in new_files:
        hay = " " + _norm(path.stem) + " "
        hits = sum(1 for w in words if f" {w} " in hay)
        if words and hits / len(words) < _MIN_WORD_MATCH:
            for p in new_files:
                p.unlink(missing_ok=True)
            raise RuntimeError(
                f"no Spotify match for \"{query}\" (closest was \"{path.stem}\")"
            )
    return _reject_non_tracks(new_files)


def _ytdlp(url: str, output_dir: Path) -> list[Path]:
    if _yt_dlp is None:
        raise RuntimeError("yt-dlp not installed — run: pip install yt-dlp")

    before = _audio_files(output_dir)

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


def _spotdl(url: str, output_dir: Path) -> list[Path]:
    before = _audio_files(output_dir)

    result = subprocess.run(
        [
            sys.executable, "-m", "spotdl",
            "download", url,
            "--output", str(output_dir),
            "--format", "flac",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"spotdl failed:\n{result.stderr.strip() or result.stdout.strip()}"
        )

    after = _audio_files(output_dir)
    return sorted(after - before)
