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

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
        "windowsfilenames": True,
        "noplaylist": True,
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
    with _yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    after = set(output_dir.glob("*.mp3"))
    new_files = sorted(after - before)
    # Reject DJ mixes / live sets — only individual studio tracks allowed.
    clean = []
    for path in new_files:
        if _is_mix(path.stem):
            path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Downloaded file looks like a DJ mix/set: \"{path.stem}\". "
                "Try a more specific search query."
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
