"""Safe discovery of audio files from user-configured local libraries.

The service intentionally has no FastAPI or global registry dependency.  A
caller can scan the configured folders whenever it needs a fresh view; track
IDs are derived from file content, so repeated scans (and identical files in
different folders) are idempotent.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable, Mapping


# Keep this list deliberately explicit.  In particular, a file's MIME type or
# user supplied filename must not be enough to make it eligible for scanning.
SUPPORTED_AUDIO_EXTENSIONS = frozenset(
    {
        ".aac",
        ".flac",
        ".m4a",
        ".mp3",
        ".ogg",
        ".opus",
        ".wav",
        ".webm",
        ".wma",
    }
)

CONTENT_ID_LENGTH = 16
_HASH_CHUNK_SIZE = 1024 * 1024


def configured_library_dirs(environ: Mapping[str, str] | None = None) -> list[Path]:
    """Return existing, unique directories from ``DJ_LIBRARY_DIRS``.

    The variable is a semicolon-separated list to match Windows path
    conventions.  Invalid, missing, and non-directory entries are ignored.
    Paths are resolved once here and all files are checked against these roots
    again during scanning, preventing symlink traversal outside the allowlist.
    """

    env = os.environ if environ is None else environ
    configured = env.get("DJ_LIBRARY_DIRS", "")
    result: list[Path] = []
    seen: set[Path] = set()
    for raw in configured.split(";"):
        value = raw.strip()
        if not value:
            continue
        try:
            root = Path(value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        if not root.is_dir() or root in seen:
            continue
        seen.add(root)
        result.append(root)
    return result


def file_hash(path: str | os.PathLike[str]) -> str:
    """Return the stable, short content ID used by the UI track API."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()[:CONTENT_ID_LENGTH]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _iter_audio_files(root: Path) -> Iterable[Path]:
    # rglob itself does not guarantee that a symlink cannot point outside the
    # root, hence the resolved-path check below.
    try:
        candidates = root.rglob("*")
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if _is_within(resolved, root):
                yield resolved
    except (OSError, RuntimeError):
        return


def scan_library(environ: Mapping[str, str] | None = None) -> list[dict[str, object]]:
    """Scan configured folders and return deterministic, content-addressed tracks.

    Results are sorted by canonical path.  If two paths contain identical
    bytes, only the first path is returned because both represent the same
    content-addressed track.  Files that disappear or become unreadable while
    scanning are skipped safely.
    """

    tracks: dict[str, dict[str, object]] = {}
    for root in configured_library_dirs(environ):
        for path in _iter_audio_files(root):
            try:
                track_id = file_hash(path)
                stat = path.stat()
            except OSError:
                continue
            tracks.setdefault(
                track_id,
                {
                    "track_id": track_id,
                    "path": str(path),
                    "filename": path.name,
                    "size_bytes": stat.st_size,
                    "extension": path.suffix.lower(),
                },
            )
    return sorted(tracks.values(), key=lambda track: str(track["path"]))


# A descriptive alias for callers that prefer an explicit operation name.
list_library_tracks = scan_library


__all__ = [
    "CONTENT_ID_LENGTH",
    "SUPPORTED_AUDIO_EXTENSIONS",
    "configured_library_dirs",
    "file_hash",
    "list_library_tracks",
    "scan_library",
]
