"""One folder per song: the single resolver for every song file (G25).

    <cache>/songs/<track_id>/
        audio.<ext>                       the song file
        stems/<model>/<stem>.<m4a|flac|wav>   vocals, drums, bass, other (no_vocals for the 2-stem set)
        lyrics.txt  lyrics.lrc            fetched lyrics (lrc only when synced)
        asr.txt                           faster-whisper transcript of the vocal stem (language line, then text)
        meta.json                         artist, title, source video, duration, language, verdict, timestamps

Readers never build these paths themselves: they ask this module. Every lookup tries the song folder first and then
the old flat layout (uploads/<id>.<ext>, stems/<audio sha256>_<model>/), so nothing breaks while files are still
being moved (app/ui/services/song_migrate.py moves them by rename only, never deletes, never symlinks).
G26: every writer lands its files in the folder (downloads go to songs/_incoming/ first, then are renamed in once the
content id is known) and every reader asks this module; the flat fallback below is kept for one release.
TODO(2026-12-15): remove the flat-layout fallbacks (uploads/, stems/, uploads/_names.json) once the prod migration is done.
All directories are resolved at call time so tests (and AIDJ_CACHE_DIR) can redirect them.
"""
from __future__ import annotations

import json
import os
import re
import time
import shutil
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aiff", ".aif", ".opus")
STEM_EXTS = (".m4a", ".flac", ".wav")          # preferred first: the phone-sized copy is the cheapest to serve
VERDICTS = ("verified", "mismatch", "no_lyrics", "instrumental")
_SAFE_ID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


def _cache() -> Path:
    from app.music_brain.config import CACHE_DIR
    return Path(CACHE_DIR)


def songs_dir(cache: Optional[Path] = None) -> Path:
    return (Path(cache) if cache is not None else _cache()) / "songs"


def song_dir(track_id: str) -> Path:
    """<cache>/songs/<track_id>. A track id that is not a plain token (path separators, dots) is refused."""
    if not _SAFE_ID.match(track_id or ""):
        raise ValueError(f"bad track id: {track_id!r}")
    return songs_dir() / track_id


def audio_path(track_id: str, legacy: Optional[Path] = None) -> Optional[Path]:
    """The song file: audio.<ext> in its folder, else the old flat path (`legacy`, or uploads/<id>.<ext>)."""
    d = song_dir(track_id)
    for ext in AUDIO_EXTS:
        p = d / f"audio{ext}"
        if p.is_file():
            return p
    if legacy is not None and Path(legacy).is_file():
        return Path(legacy)
    up = _cache() / "uploads"
    for ext in AUDIO_EXTS:
        p = up / f"{track_id}{ext}"
        if p.is_file():
            return p
    return None


def incoming_dir() -> Path:
    """Scratch for downloads whose content id is not known yet; same disk as the song folders, so adopt_audio renames."""
    return songs_dir() / "_incoming"


def names_file(cache: Optional[Path] = None, for_write: bool = False) -> Path:
    """Display names {track id: name}: songs/_names.json. A flat uploads/_names.json that is newer (written by older
    code) wins for reading; for_write first copies it over (a few KB). Reading changes nothing on disk."""
    new = songs_dir(cache) / "_names.json"
    old = (Path(cache) if cache is not None else _cache()) / "uploads" / "_names.json"
    try:
        stale = old.is_file() and (not new.exists() or old.stat().st_mtime_ns > new.stat().st_mtime_ns)
    except OSError:
        stale = False
    if not stale:
        return new
    if not for_write:
        return old
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(old, new)
    except OSError:
        return old
    return new


def _folder_audio(d: str) -> Optional[Path]:
    try:
        with os.scandir(d) as it:
            names = {e.name for e in it if e.is_file()}
    except OSError:
        return None
    for ext in AUDIO_EXTS:
        if f"audio{ext}" in names:
            return Path(d) / f"audio{ext}"
    return None


def library_audio(cache: Optional[Path] = None) -> Dict[str, Path]:
    """{track id: song file} of the whole library: every songs/<id>/audio.<ext>, plus flat uploads/<id>.<ext> files of
    songs not moved yet (the folder wins). Names starting with "_" (scratch, names, migration) are not songs."""
    out: Dict[str, Path] = {}
    try:
        with os.scandir(songs_dir(cache)) as it:
            for e in it:
                if e.name.startswith("_") or not _SAFE_ID.match(e.name) or not e.is_dir():
                    continue
                p = _folder_audio(e.path)
                if p is not None:
                    out[e.name] = p
    except OSError:
        pass
    try:
        with os.scandir((Path(cache) if cache is not None else _cache()) / "uploads") as it:
            for e in it:
                p = Path(e.path)
                if e.name.startswith("_") or p.suffix.lower() not in AUDIO_EXTS or p.stem in out:
                    continue
                if e.is_file():
                    out[p.stem] = p
    except OSError:
        pass
    return out


def adopt_audio(src: Path, track_id: str, move: bool = True) -> Path:
    """Put a finished download/upload into its song folder as audio.<ext>. The file is renamed (src sits in
    songs/_incoming/, same disk); a cross-disk rename (mergerfs) falls back to a single-file move, never a bulk copy.
    move=False copies (the caller keeps its file). A song already in the library is left alone."""
    have = audio_path(track_id)
    if have is not None:
        if move:
            Path(src).unlink(missing_ok=True)
        return have
    src = Path(src)
    dest = song_dir(track_id) / f"audio{src.suffix.lower()}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not move:
        shutil.copy2(src, dest)
    else:
        try:
            os.rename(src, dest)
        except OSError:
            shutil.move(str(src), str(dest))
    return dest


def store_bytes(data: bytes, track_id: str, suffix: str) -> Path:
    """Write uploaded bytes into the song's folder (tmp name in _incoming, then adopt)."""
    have = audio_path(track_id)
    if have is not None:
        return have
    inc = incoming_dir()
    inc.mkdir(parents=True, exist_ok=True)
    tmp = inc / f"{track_id}.{os.getpid()}.{time.time_ns()}{suffix.lower() or '.mp3'}"
    try:
        tmp.write_bytes(data)
        return adopt_audio(tmp, track_id)
    finally:
        tmp.unlink(missing_ok=True)


def stems_dir(track_id: str, model: str) -> Path:
    return song_dir(track_id) / "stems" / model


def legacy_stems_dir(audio_hash: str, model: str) -> Path:
    return _cache() / "stems" / f"{audio_hash}_{model}"


def stem_files(track_id: str, model: str, audio_hash: Optional[str] = None) -> Dict[str, Path]:
    """{stem name: file} of a song for one separation model. The folder wins; a name missing there falls back to the
    old stem cache (needs the song's audio sha256 for its directory name, and its manifest.json)."""
    out: Dict[str, Path] = {}
    d = stems_dir(track_id, model)
    if d.is_dir():
        for ext in reversed(STEM_EXTS):                  # later (preferred) extension overwrites
            for f in d.glob(f"*{ext}"):
                if f.is_file():
                    out[f.stem] = f
    if audio_hash:
        from app.music_brain.audio.audio_io import read_manifest
        for name, p in (read_manifest(legacy_stems_dir(audio_hash, model)) or {}).items():
            out.setdefault(name, Path(p))
    return out


def vocal_stem(track_id: str, models=("htdemucs_ft", "htdemucs"), audio_hash: Optional[str] = None) -> Optional[Path]:
    """The song's vocal stem, best model first, or None."""
    for m in models:
        v = stem_files(track_id, m, audio_hash).get("vocals")
        if v is not None:
            return v
    return None


def lyrics_path(track_id: str, synced: bool = False) -> Path:
    return song_dir(track_id) / ("lyrics.lrc" if synced else "lyrics.txt")


def asr_path(track_id: str) -> Path:
    return song_dir(track_id) / "asr.txt"


def meta_path(track_id: str) -> Path:
    return song_dir(track_id) / "meta.json"


def read_meta(track_id: str) -> dict:
    try:
        d = json.loads(meta_path(track_id).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_text(path: Path, text: str) -> None:
    """Atomic text write (tmp in the same folder, then replace); parents are created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def update_meta(track_id: str, **fields) -> dict:
    """Merge fields into meta.json (None values drop the key) and stamp `updated`. Returns the new document."""
    meta = read_meta(track_id)
    for k, v in fields.items():
        if v is None:
            meta.pop(k, None)
        else:
            meta[k] = v
    meta["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    write_text(meta_path(track_id), json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def verdict(track_id: str) -> Optional[dict]:
    """{verdict, score} from meta.json, or None when the song was never checked."""
    m = read_meta(track_id)
    v = m.get("verdict")
    return {"verdict": v, "score": m.get("score")} if v in VERDICTS else None


# ---- stem sets (G26): htdemucs_ft etc. live in songs/<id>/stems/<variant>/ ------------------------------------------
# <variant> is the model name, plus "_<stem>" for a two-stem set. The song's folder id is the first 16 hex of its audio
# sha256 (the track id). `root` is the old flat stem cache directory: when given (tests, set tidy-up) the flat layout
# <root>/<audio sha256>_<variant> is used as it was.

def stems_root_for(cache) -> Optional[Path]:
    """stems_dir argument for a cache directory: None (song folders, then the flat cache) for the live cache, the flat
    <cache>/stems for any other directory (tests, copies)."""
    c = Path(cache)
    return None if c.resolve() == _cache().resolve() else c / "stems"


def stem_cache_dir(audio_hash: str, variant: str, root: Optional[Path] = None) -> Path:
    if root is not None:
        return Path(root) / f"{audio_hash}_{variant}"
    return stems_dir(audio_hash[:16], variant)


def stem_set(audio_hash: str, variant: str, root: Optional[Path] = None) -> Optional[Dict[str, str]]:
    """{stem: file} of a complete set (manifest.json present, every file there), the song folder first, then the
    old flat cache. None when there is none."""
    from app.music_brain.audio.audio_io import read_manifest
    got = read_manifest(stem_cache_dir(audio_hash, variant, root))
    if got is not None or root is not None:
        return got
    return read_manifest(legacy_stems_dir(audio_hash, variant))


def stem_variant_dirs(audio_hash: str, root: Optional[Path] = None) -> List[Path]:
    """Every stem folder (all variants) of one song, song folder and flat cache."""
    out: List[Path] = []
    if root is None:
        d = song_dir(audio_hash[:16]) / "stems"
        if d.is_dir():
            out += sorted(x for x in d.iterdir() if x.is_dir() and not x.name.startswith("."))
    base = Path(root) if root is not None else _cache() / "stems"
    if base.is_dir():
        out += sorted(x for x in base.glob(f"{audio_hash}_*") if x.is_dir())
    return out


def variant_of(d: Path, audio_hash: str) -> str:
    """The variant name of a stem folder returned by stem_variant_dirs."""
    return d.name[len(audio_hash) + 1:] if d.name.startswith(audio_hash + "_") else d.name


def iter_stem_variants(root: Optional[Path] = None, cache: Optional[Path] = None) -> Iterator[Tuple[str, Path]]:
    """(song key, stem folder) for every stem folder of the library: key is the 16-hex song id for a song folder, the
    full sha256 for a flat one."""
    if root is None:
        try:
            for sd in sorted(songs_dir(cache).iterdir()):
                st = sd / "stems"
                if sd.name.startswith("_") or not st.is_dir():
                    continue
                for v in sorted(st.iterdir()):
                    if v.is_dir() and not v.name.startswith("."):
                        yield sd.name, v
        except OSError:
            pass
    base = Path(root) if root is not None else (Path(cache) if cache is not None else _cache()) / "stems"
    if base.is_dir():
        for v in sorted(base.iterdir()):
            h, _, rest = v.name.partition("_")
            if v.is_dir() and rest and not v.name.startswith("."):
                yield h, v
