"""Find and (reversibly) remove duplicate songs from the cache: the same recording downloaded
several times because YouTube search returned different uploads (official audio, lyric video,
visualizer, re-upload).  Each upload has its own content hash, so each got its own analysis,
Demucs stems, waveform and key-lock sets.

    python3 -m app.ui.dedup_songs                       # dry run (default): report only, deletes nothing
    python3 -m app.ui.dedup_songs --apply [--group ID]  # move duplicates to a quarantine dir (reversible)
    ... --include-review                                # also drop REVIEW pairs: one kept copy per component
    python3 -m app.ui.dedup_songs --restore TS          # put a quarantine run back
    python3 -m app.ui.dedup_songs --purge TS --yes      # delete a quarantine run for good

Rules:
  * Same recording = SAME NAME (artist + title, upload noise stripped, identical remix/live/...
    markers) AND SAME AUDIO (BPM, Camelot key, duration and energy-curve fingerprint from the
    cached analysis).  Name-only and audio-only near matches go to REVIEW and are never merged.
  * Remixes, edits, live, acoustic, slowed, instrumental, ... are different recordings: a marker on
    one side only keeps the pair apart; the same marker on both sides (same remixer) may merge.
  * Apply never deletes: files go to <cache>/_dedup_quarantine/<timestamp>/ with a manifest.json,
    references are remapped through <cache>/track_aliases.json (duplicate id -> canonical id).
  * Stop the app first: the registry and the decks hold ids in memory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app.ui import track_identity

AUDIO_SUFFIXES = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aiff", ".aif", ".opus", ".webm"}
QUARANTINE = "_dedup_quarantine"
ALIASES_FILE = "track_aliases.json"
CATEGORIES = ("songs", "stems", "keylock", "waveforms", "analysis")

# ---------------------------------------------------------------------------------------------
# name normalisation
# ---------------------------------------------------------------------------------------------

# A different recording of the song: never merged with the plain version.
_MARKER = re.compile(
    r"\b(remix|re-?mix|edit|mashup|bootleg|flip|rework(?:ed)?|refix|vip|mix|version|live|acoustic|"
    r"slowed|reverb|sped\s*up|nightcore|instrumental|karaoke|cover|remaster(?:ed)?|extended|dub|8d|"
    r"spatial|a\s*cappella|acapella|stripped|unplugged|demo|reprise|pitched|lo-?fi|tribute|"
    r"unreleased|remake)\b",
    re.IGNORECASE,
)
# "Original Mix" / "Album Version" name the normal recording.
_PLAIN_VERSION = re.compile(r"\b(original|album|studio)\s+(mix|version)\b|\boriginal\b", re.IGNORECASE)
# Upload noise: says nothing about the recording.
_NOISE_WORDS = re.compile(
    r"\b(official|offical|oficial|full|length|audio|video|videos|music|lyric|lyrics|lyrical|letra|"
    r"visuali[sz]er|hd|hq|4k|360|stream|song|topic|explicit|clean|out\s+now|premiere|with|"
    r"loud|ultra|records?|version|the|a|an|and|con|de|la|el)\b|\d{4}|[^\w\s]",
    re.IGNORECASE,
)
_TAIL_NOISE = re.compile(
    r"(?:\s+(?:official|offical|oficial|full|audio|video|lyrics?|lyrical|visuali[sz]er|hd|hq|4k|"
    r"stream|song|music|length|version))+\s*(?:\d{4})?\s*$",
    re.IGNORECASE,
)
_BRACKET = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
_SEP_INSIDE_TITLE = re.compile(r"\s+[|｜]\s+")


def fold(text: str) -> str:
    """Accent/case/punctuation-folded: 'Beyoncé' == 'beyonce', "We’ve" == 'weve'."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    t = t.replace("&", " and ").replace("⧸", "/")
    t = re.sub(r"[\W_]+", " ", t)
    return " ".join(t.split())


@dataclass(frozen=True)
class Ident:
    artist: str          # folded primary artist, "" when the name has none
    title: str           # folded bare title
    markers: frozenset   # folded remix/live/... descriptors; empty for the plain recording

    @property
    def artist_known(self) -> bool:
        return bool(self.artist)


def _marker_text(chunk: str) -> Optional[str]:
    """The descriptor a bracket/segment carries when it names a different recording, else None."""
    if _PLAIN_VERSION.search(chunk) and not re.search(
            r"\b(remix|edit|mashup|bootleg|flip|rework|live|acoustic|slowed|instrumental)\b", chunk, re.I):
        return None
    if not _MARKER.search(chunk):
        return None
    words = [_canon_marker(w) for w in fold(chunk).split()]
    words = [w for w in words if not _NOISE_WORDS.fullmatch(w) or _MARKER.fullmatch(w)]
    return " ".join(words) or fold(chunk)


def _canon_marker(w: str) -> str:
    return {"remastered": "remaster", "reworked": "rework", "remake": "remake"}.get(w, w)


def identity(display: str) -> Ident:
    """(artist, bare title, markers) of a download name.  Reuses track_identity for the artist and
    the credit / noise stripping, then folds and pulls out remix-style markers."""
    display = (display or "").strip()
    artist, title = track_identity.clean_identity(display) if display else ("Unknown", "")
    has_artist = artist != "Unknown"
    # markers come from the RAW pieces (clean_identity keeps remix tags but drops others)
    parts = track_identity._SEPARATORS.split(display, maxsplit=1)
    raw_title = parts[1] if len(parts) == 2 and parts[0] and parts[1] else display
    markers = set()
    for m in _BRACKET.finditer(raw_title):
        mt = _marker_text(m.group(1))
        if mt:
            markers.add(mt)
    body = _BRACKET.sub(" ", raw_title)
    # extra " - " / "|" segments after the title: markers if descriptive, otherwise dropped
    segs = re.split(r"\s+[-–—]\s+|\s+[|｜]\s+", body)
    main = segs[0]
    for extra in segs[1:]:
        mt = _marker_text(extra)
        if mt:
            markers.add(mt)
    # the artist repeated as a title prefix ("BICEP - BICEP | GLUE")
    if len(segs) > 1 and fold(main) == fold(artist):
        main = segs[1]
    main_clean = track_identity.clean_title(main)
    bare = _MARKER.findall(main_clean)
    for w in bare:                                  # "Con Calma Remix", "Pendulum Live X"
        markers.add(_canon_marker(fold(w)))
    t = fold(main_clean)
    t = re.sub(r"\b(?:feat|ft|featuring)\b.*$", "", t).strip()
    for _ in range(3):
        t = _TAIL_NOISE.sub("", t).strip()
    t = " ".join(w for w in t.split() if not (_MARKER.fullmatch(w) and w in markers)) or t
    # drop the trailing credit of "X with Y"/"X ft Y" already done; keep the rest verbatim
    return Ident(fold(artist) if has_artist else "", t, frozenset(markers))


def name_match(a: Ident, b: Ident) -> str:
    """'strong' (same artist + title + markers), 'weak' (same title + markers, an artist unknown), 'no'."""
    if not a.title or a.title != b.title or a.markers != b.markers:
        return "no"
    if a.artist_known and b.artist_known:
        return "strong" if a.artist == b.artist else "no"
    return "weak"


# ---------------------------------------------------------------------------------------------
# catalog of the cache
# ---------------------------------------------------------------------------------------------


@dataclass
class Track:
    id: str
    name: str
    upload: Path
    size: int
    mtime: float
    full_hash: Optional[str] = None
    analysis: Optional[dict] = None
    files: Dict[str, List[Path]] = field(default_factory=lambda: {c: [] for c in CATEGORIES})
    sizes: Dict[str, int] = field(default_factory=lambda: {c: 0 for c in CATEGORIES})
    lyrics: Optional[Path] = None
    ident: Optional[Ident] = None

    @property
    def total(self) -> int:
        return sum(self.sizes.values())

    @property
    def duration(self) -> float:
        return float((self.analysis or {}).get("duration") or 0.0)

    @property
    def bpm(self) -> float:
        return float((self.analysis or {}).get("bpm") or 0.0)

    @property
    def camelot(self) -> str:
        k = (self.analysis or {}).get("key") or {}
        return str(k.get("camelot") or "") if isinstance(k, dict) else ""

    def work_score(self) -> int:
        """How much derived work exists: stems dominate (minutes of Demucs each)."""
        return (8 * bool(self.files["stems"]) + 2 * bool(self.files["keylock"]) + bool(self.lyrics)
                + bool(self.files["waveforms"]) + bool(self.analysis))


def _size(path: Path) -> int:
    try:
        if path.is_file() or path.is_symlink():
            return path.lstat().st_size
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _analysis_version(name: str) -> int:
    m = re.search(r"\.v(\d+)\.json$", name)
    return int(m.group(1)) if m else (0 if name.endswith(".json") and name.count(".") == 1 else -1)


def scan(cache: Path) -> Dict[str, Track]:
    """Every uploaded track with its derived files (read-only)."""
    cache = Path(cache)
    up = cache / "uploads"
    names = _read_json(up / "_names.json") or {}
    tracks: Dict[str, Track] = {}
    if up.is_dir():
        for p in sorted(up.iterdir()):
            if p.is_file() and not p.name.startswith("_") and p.suffix.lower() in AUDIO_SUFFIXES:
                try:
                    st = p.stat()
                except OSError:
                    continue
                name = names.get(p.stem) if isinstance(names.get(p.stem), str) else ""
                tracks[p.stem] = Track(p.stem, name or p.stem, p, st.st_size, st.st_mtime)
    for t in tracks.values():
        t.files["songs"] = [t.upload]
        t.sizes["songs"] = t.size
        t.ident = identity(t.name)

    # analysis: <hash>.v5.json, <hash>.vibe.json, <hash>.energy.json ... (hash starts with the id)
    adir = cache / "analysis"
    best: Dict[str, Tuple[int, Path]] = {}
    if adir.is_dir():
        for p in adir.iterdir():
            h = p.name.split(".")[0]
            t = tracks.get(h[:16])
            if t is None:
                continue
            t.full_hash = t.full_hash or h
            t.files["analysis"].append(p)
            v = _analysis_version(p.name)
            if v >= 0 and (h[:16] not in best or v > best[h[:16]][0]):
                best[h[:16]] = (v, p)
    for tid, (_v, p) in best.items():
        data = _read_json(p)
        if isinstance(data, dict):
            tracks[tid].analysis = data
    # stems: <hash>_<model>[_vocals]
    sdir = cache / "stems"
    if sdir.is_dir():
        for p in sdir.iterdir():
            h = p.name.split("_")[0]
            t = tracks.get(h[:16])
            if t is not None and len(h) >= 16:
                t.full_hash = t.full_hash or h
                t.files["stems"].append(p)
    # waveforms: <id>-v1-4.json, <id>.json
    wdir = cache / "waveforms"
    if wdir.is_dir():
        for p in wdir.iterdir():
            t = tracks.get(re.split(r"[-.]", p.name)[0])
            if t is not None:
                t.files["waveforms"].append(p)
    # key-lock sets: t<hash24>_<bpm> (tempo sets) and <sha20> (groove sets, owner via the key formula)
    kdir = cache / "keylock"
    if kdir.is_dir():
        by24 = {t.full_hash[:24]: t for t in tracks.values() if t.full_hash}
        groove: List[Path] = []
        for p in kdir.iterdir():
            if p.name.startswith("t") and "_" in p.name:
                t = by24.get(p.name[1:].split("_")[0])
                if t is not None:
                    t.files["keylock"].append(p)
            elif p.is_dir() and not p.name.endswith(".tmp"):
                groove.append(p)
        _attribute_groove_sets(groove, tracks)
    lyr_dir = cache / "lyrics"
    if lyr_dir.is_dir():
        try:
            from app.music_brain import lyrics as _lyrics
            for t in tracks.values():
                p = _lyrics._cache_path(t.name, lyr_dir)
                if p.exists():
                    t.lyrics = p
        except Exception:
            pass
    for t in tracks.values():
        for cat in ("stems", "keylock", "waveforms", "analysis"):
            t.sizes[cat] = sum(_size(p) for p in t.files[cat])
    return tracks


def _attribute_groove_sets(dirs: List[Path], tracks: Dict[str, Track]) -> None:
    """keylock/<sha20>: key = sha256(f"{audio_hash}|{ratio}|{a_groove0}|{a_end}")[:20]. Try every
    library hash against the set's meta.json (a_end falls back to a_solo end)."""
    hashes = [t for t in tracks.values() if t.full_hash]
    for d in dirs:
        meta = _read_json(d / "meta.json")
        if not isinstance(meta, dict):
            continue
        try:
            ratio, g0 = float(meta["ratio"]), float(meta["a_groove"][0])
            ends = [float(meta["a_end"])] if "a_end" in meta else []
            ends.append(float(meta["a_solo"][1]))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        for t in hashes:
            if any(hashlib.sha256(f"{t.full_hash}|{ratio:.6f}|{g0:.3f}|{e:.3f}".encode()).hexdigest()[:20] == d.name
                   for e in ends):
                t.files["keylock"].append(d)
                break


# ---------------------------------------------------------------------------------------------
# audio confirmation (from the cached analysis, no decoding)
# ---------------------------------------------------------------------------------------------

DUR_TOL = 2.0            # seconds: same master
DUR_OFFSET_MAX = 30.0    # seconds: a video upload with a longer intro/outro
BPM_TOL = 0.5
CORR_SAME = 0.85
CORR_OFFSET = 0.93
MAX_LAG = 30


def _curve(t: Track):
    import numpy as np

    a = t.analysis or {}
    y, x = a.get("energy_curve") or [], a.get("energy_times") or []
    if len(y) < 30 or len(y) != len(x):
        return None
    grid = np.arange(0, float(x[-1]) + 1e-9, 1.0)
    return np.interp(grid, np.asarray(x, dtype=float), np.asarray(y, dtype=float))


def _best_corr(x, y, max_lag: int = MAX_LAG) -> Tuple[float, int]:
    import numpy as np

    best, best_lag = -1.0, 0
    need = max(30, int(0.7 * min(len(x), len(y))))
    for lag in range(-max_lag, max_lag + 1):
        a, b = x[max(0, lag):], y[max(0, -lag):]
        n = min(len(a), len(b))
        if n < need:
            continue
        a, b = a[:n], b[:n]
        if a.std() < 1e-9 or b.std() < 1e-9:
            continue
        c = float(np.corrcoef(a, b)[0, 1])
        if c > best:
            best, best_lag = c, lag
    return best, best_lag


def bpm_close(a: float, b: float) -> bool:
    if not a or not b:
        return False
    return abs(a - b) <= BPM_TOL or abs(a * 2 - b) <= BPM_TOL * 2 or abs(a - b * 2) <= BPM_TOL * 2


@dataclass
class AudioResult:
    ok: bool
    reason: str
    corr: float = 0.0
    lag: int = 0
    dur_delta: float = 0.0


def audio_compare(a: Track, b: Track) -> AudioResult:
    if not a.analysis or not b.analysis:
        return AudioResult(False, "no cached analysis")
    dd = abs(a.duration - b.duration)
    if not bpm_close(a.bpm, b.bpm):
        return AudioResult(False, f"bpm {a.bpm:.1f} vs {b.bpm:.1f}", dur_delta=dd)
    if not a.camelot or a.camelot != b.camelot:
        return AudioResult(False, f"key {a.camelot or '?'} vs {b.camelot or '?'}", dur_delta=dd)
    if dd > DUR_OFFSET_MAX:
        return AudioResult(False, f"duration differs by {dd:.1f}s", dur_delta=dd)
    ca, cb = _curve(a), _curve(b)
    if ca is None or cb is None:
        return AudioResult(False, "no energy curve", dur_delta=dd)
    corr, lag = _best_corr(ca, cb)
    if dd <= DUR_TOL and corr >= CORR_SAME and abs(lag) <= DUR_TOL:
        return AudioResult(True, f"dur d{dd:.1f}s, corr {corr:.3f}", corr, lag, dd)
    if corr >= CORR_OFFSET:
        return AudioResult(True, f"dur d{dd:.1f}s, corr {corr:.3f} at lag {lag}s (intro/outro offset)", corr, lag, dd)
    return AudioResult(False, f"energy fingerprint differs (corr {corr:.3f} lag {lag}s, dur d{dd:.1f}s)", corr, lag, dd)


# ---------------------------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------------------------

_LYRIC_UPLOAD = re.compile(r"lyric|lyrical|letra|visuali[sz]er|lyrics? booklet|visual backdrop", re.IGNORECASE)
_AUDIO_UPLOAD = re.compile(r"\baudio\b|\bstream\b|\btopic\b", re.IGNORECASE)
_VIDEO_UPLOAD = re.compile(r"\bvideo\b|\bmv\b|\bhd\b|4k", re.IGNORECASE)


def source_rank(name: str) -> Tuple[int, str]:
    """official audio (and plain names, i.e. YT Music songs) 3 > lyric/visualizer 2 > video 1."""
    if _LYRIC_UPLOAD.search(name):
        return 2, "lyric/visualizer upload"
    if _AUDIO_UPLOAD.search(name):
        return 3, "audio upload"
    if _VIDEO_UPLOAD.search(name):
        return 1, "video upload"
    return 3, "plain title"


def pick_canonical(members: List[Track]) -> Tuple[Track, str]:
    def key(t: Track):
        bitrate = t.size / t.duration if t.duration else 0.0
        return (-t.work_score(), -source_rank(t.name)[0], -bitrate, t.mtime, t.id)

    ordered = sorted(members, key=key)
    win = ordered[0]
    why = [f"work {win.work_score()} (stems={'y' if win.files['stems'] else 'n'}, "
           f"keylock={len(win.files['keylock'])}, lyrics={'y' if win.lyrics else 'n'}, "
           f"analysis={'y' if win.analysis else 'n'})", source_rank(win.name)[1]]
    return win, "; ".join(why)


@dataclass
class Group:
    id: str
    canonical: Track
    duplicates: List[Track]
    why: str
    evidence: Dict[str, str]     # duplicate id -> audio evidence

    @property
    def members(self) -> List[Track]:
        return [self.canonical] + self.duplicates


@dataclass
class Review:
    a: Track
    b: Track
    kind: str      # "name match, audio mismatch" | "weak name, audio match" | "audio match, names differ"
    detail: str


def group_id(ids) -> str:
    return "g" + hashlib.sha1(",".join(sorted(ids)).encode()).hexdigest()[:8]


def find_groups(tracks: Dict[str, Track], audio_only_review: bool = True) -> Tuple[List[Group], List[Review]]:
    items = [t for t in tracks.values() if t.ident and t.ident.title]
    parent = {t.id: t.id for t in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    reviews: List[Review] = []
    evidence: Dict[Tuple[str, str], str] = {}
    by_title: Dict[Tuple[str, frozenset], List[Track]] = {}
    for t in items:
        by_title.setdefault((t.ident.title, t.ident.markers), []).append(t)
    for bucket in by_title.values():
        for i, a in enumerate(bucket):
            for b in bucket[i + 1:]:
                m = name_match(a.ident, b.ident)
                if m == "no":
                    continue
                res = audio_compare(a, b)
                if m == "strong" and res.ok:
                    parent[find(a.id)] = find(b.id)
                    evidence[(a.id, b.id)] = res.reason
                elif m == "strong":
                    reviews.append(Review(a, b, "name match, audio mismatch", res.reason))
                elif res.ok:
                    reviews.append(Review(a, b, "weak name (artist unknown), audio match", res.reason))
    if audio_only_review:
        seen = {frozenset((r.a.id, r.b.id)) for r in reviews}
        analysed = [t for t in items if t.analysis]
        for i, a in enumerate(analysed):
            for b in analysed[i + 1:]:
                if (a.ident.title, a.ident.markers) == (b.ident.title, b.ident.markers):
                    continue
                if frozenset((a.id, b.id)) in seen or abs(a.duration - b.duration) > DUR_TOL:
                    continue
                if not bpm_close(a.bpm, b.bpm) or a.camelot != b.camelot:
                    continue
                res = audio_compare(a, b)
                if res.ok and res.corr >= 0.97:
                    reviews.append(Review(a, b, "audio match, names differ", res.reason))
    clusters: Dict[str, List[Track]] = {}
    for t in items:
        clusters.setdefault(find(t.id), []).append(t)
    groups: List[Group] = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        canon, why = pick_canonical(members)
        dups, ev = [], {}
        for d in sorted((m for m in members if m is not canon), key=lambda m: m.id):
            # a chain a~b~c must not merge a with c unless c matches the KEPT copy itself
            res = audio_compare(canon, d)
            if name_match(canon.ident, d.ident) == "strong" and res.ok:
                dups.append(d)
                ev[d.id] = res.reason
            else:
                reviews.append(Review(canon, d, "chain match only (not the kept copy)", res.reason))
        if dups:
            groups.append(Group(group_id(m.id for m in [canon] + dups), canon, dups, why, ev))
    groups.sort(key=lambda g: (g.canonical.ident.title, g.id))
    # a REVIEW pair already inside one group is not news
    gm = {m.id: g.id for g in groups for m in g.members}
    reviews = [r for r in reviews if not (r.a.id in gm and gm.get(r.a.id) == gm.get(r.b.id))]
    return groups, reviews


def review_groups(groups: List[Group], reviews: List[Review], cache: Optional[Path] = None) -> List[Group]:
    """--include-review: every connected component of REVIEW pairs (a~b, b~c) becomes one removal group with a
    single kept copy, chosen by pick_canonical.  Never drops a canonical of a confirmed group or an alias
    target (those win the pick; a component with none left to drop is skipped).  evidence = why each drop was
    in REVIEW."""
    protected = {g.canonical.id for g in groups} | (set(load_aliases(cache).values()) if cache else set())
    gone = {d.id for g in groups for d in g.duplicates}          # already removed by a confirmed group
    parent: Dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    tr: Dict[str, Track] = {}
    why: Dict[str, List[str]] = {}
    for r in reviews:
        if r.a.id in gone or r.b.id in gone:
            continue
        tr[r.a.id], tr[r.b.id] = r.a, r.b
        parent[find(r.a.id)] = find(r.b.id)
        for t, o in ((r.a, r.b), (r.b, r.a)):
            why.setdefault(t.id, []).append(f"{r.kind} vs {o.id}: {r.detail}")
    comps: Dict[str, List[Track]] = {}
    for tid, t in tr.items():
        comps.setdefault(find(tid), []).append(t)
    out: List[Group] = []
    for members in comps.values():
        keep_pool = [m for m in members if m.id in protected] or members
        canon, reason = pick_canonical(keep_pool)
        dups = sorted((m for m in members if m is not canon and m.id not in protected), key=lambda m: m.id)
        if dups:
            out.append(Group(group_id(m.id for m in [canon] + dups), canon, dups, reason,
                             {d.id: "REVIEW: " + "; ".join(why[d.id]) for d in dups}))
    out.sort(key=lambda g: (g.canonical.ident.title, g.id))
    return out


# ---------------------------------------------------------------------------------------------
# aliases and references
# ---------------------------------------------------------------------------------------------


def load_aliases(cache: Path) -> Dict[str, str]:
    d = _read_json(Path(cache) / ALIASES_FILE)
    return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(d, dict) else {}


def resolve_alias(track_id: str, cache: Path, aliases: Optional[Dict[str, str]] = None) -> str:
    """Canonical id for a duplicate id (follows chains); the id itself when it is not an alias."""
    aliases = load_aliases(cache) if aliases is None else aliases
    seen = set()
    while track_id in aliases and track_id not in seen:
        seen.add(track_id)
        track_id = aliases[track_id]
    return track_id


def _write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


_SKIP_SCAN = {"uploads", "analysis", "stems", "keylock", "waveforms", "lyrics", QUARANTINE, "sets", "previews",
              "recordings", "renders", "samples", "sources"}


def find_references(cache: Path, groups: List[Group]) -> Dict[str, Dict[str, int]]:
    """duplicate id -> {relative file: number of mentions of the id or its full hash} in the json/jsonl/log
    state around the library (set logs, sessions, memory, learned techniques, fame)."""
    cache = Path(cache)
    needles: Dict[str, str] = {}
    for g in groups:
        for d in g.duplicates:
            needles[d.id] = d.full_hash or d.id
    out: Dict[str, Dict[str, int]] = {i: {} for i in needles}
    if not needles:
        return out
    for root, dirs, files in os.walk(cache):
        rel = Path(root).relative_to(cache)
        if rel.parts and rel.parts[0] in _SKIP_SCAN:
            dirs[:] = []
            continue
        if not rel.parts:
            dirs[:] = [d for d in dirs if d not in _SKIP_SCAN]
        for f in files:
            if not f.endswith((".json", ".jsonl", ".log", ".txt")) or f == ALIASES_FILE:
                continue
            p = Path(root) / f
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for tid in needles:
                n = text.count(tid)          # the id is a prefix of the full hash: one needle covers both
                if n:
                    out[tid][str(p.relative_to(cache))] = n
    return out


def name_references(cache: Path, groups: List[Group]) -> Dict[str, int]:
    """Name-keyed state (no remap needed: set_memory / learned_techniques key on the song NAME, which the
    canonical copy shares): how many entries mention each duplicate's normalised title."""
    out: Dict[str, int] = {}
    mem = _read_json(Path(cache) / "set_memory.json")
    if isinstance(mem, dict):
        keys = {fold(k) for k in mem}
        for g in groups:
            for t in g.members:
                if t.ident.title and any(t.ident.title in k for k in keys):
                    out["set_memory.json"] = out.get("set_memory.json", 0) + 1
                    break
    return out


# ---------------------------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------------------------


def _gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB"


def build_report(cache: Path, tracks: Dict[str, Track], groups: List[Group], reviews: List[Review],
                 rgroups: Optional[List[Group]] = None) -> str:
    cache = Path(cache)
    rgroups = rgroups or []
    groups = groups + rgroups
    cat_tot = {c: 0 for c in CATEGORIES}
    L: List[str] = []
    dup_n = sum(len(g.duplicates) for g in groups)
    for g in groups:
        for d in g.duplicates:
            for c in CATEGORIES:
                cat_tot[c] += d.sizes[c]
    total = sum(cat_tot.values())
    orphan = _orphan_stems(cache, tracks)
    L += ["# Song dedup report (dry run, nothing deleted)", "",
          f"cache: `{cache}`", f"tracks in library: {len(tracks)}; groups: {len(groups)}; "
          f"duplicates: {dup_n}; REVIEW pairs: {len(reviews)}", "",
          f"reclaimable if applied (moved to quarantine): **{_gb(total)}**", ""]
    L += ["| category | reclaimable |", "|---|---|"] + [f"| {c} | {_gb(cat_tot[c])} |" for c in CATEGORIES] + [""]
    L += ["## Groups", ""]
    refs = find_references(cache, groups)
    for g in groups:
        c = g.canonical
        L += [f"### {g.id}: {c.ident.artist or '?'} / {c.ident.title}"
              + (f" [{', '.join(sorted(c.ident.markers))}]" if c.ident.markers else "")
              + (" (REVIEW component, --include-review)" if g in rgroups else ""), "",
              f"- KEEP `{c.id}` {c.name!r} ({_gb(c.total)}); why: {g.why}"]
        for d in g.duplicates:
            parts = ", ".join(f"{k} {d.sizes[k] / 1e6:.0f} MB" for k in CATEGORIES if d.sizes[k])
            L.append(f"- DUP  `{d.id}` {d.name!r} = {_gb(d.total)} ({parts}); audio: {g.evidence.get(d.id, '')}")
            r = refs.get(d.id) or {}
            if r:
                L.append("  - references: " + "; ".join(f"{f} x{n}" for f, n in sorted(r.items())))
        L.append("")
    L += ["## REVIEW (never auto-merged)" if not rgroups else "## REVIEW pairs left (protected copies)", ""]
    for r in reviews:
        L.append(f"- {r.kind}: `{r.a.id}` {r.a.name!r} vs `{r.b.id}` {r.b.name!r}: {r.detail}")
    if not reviews:
        L.append("- none")
    L += ["", "## References that apply would remap", ""]
    remap = {d: fs for d, fs in refs.items() if fs}
    fame = _read_json(cache / "fame.json") or {}
    L.append(f"- alias map `track_aliases.json`: {dup_n} duplicate id(s) -> canonical id (resolves old ids in "
             "session logs, set logs, urls)")
    L.append(f"- `_names.json`: {dup_n} name entr{'y' if dup_n == 1 else 'ies'} removed (restorable)")
    L.append(f"- `fame.json` (keyed by id): {sum(1 for g in groups for d in g.duplicates if d.id in fame)} "
             "duplicate entr(y/ies) merged into the canonical id")
    for d, fs in sorted(remap.items()):
        L.append(f"- id `{d}` mentioned in: " + ", ".join(f"{f} x{n}" for f, n in sorted(fs.items())) +
                 " (left untouched: alias map resolves them)")
    nr = name_references(cache, groups)
    for f, n in nr.items():
        L.append(f"- `{f}`: keyed by song NAME, {n} group(s) match; no id remap needed")
    L += ["", "## Not touched (info)", "",
          f"- orphan stem sets (no upload with that hash): {orphan[0]} dirs, {_gb(orphan[1])}; out of scope",
          "- lyrics are keyed by title, shared by copies with the same title; never moved",
          "- session logs are never modified or deleted"]
    return "\n".join(L) + "\n"


def _orphan_stems(cache: Path, tracks: Dict[str, Track]) -> Tuple[int, int]:
    sdir = Path(cache) / "stems"
    n = size = 0
    if sdir.is_dir():
        for p in sdir.iterdir():
            if p.name.split("_")[0][:16] not in tracks:
                n += 1
                size += _size(p)
    return n, size


# ---------------------------------------------------------------------------------------------
# apply / restore / purge
# ---------------------------------------------------------------------------------------------


class ApplyRefused(RuntimeError):
    pass


def app_is_running(host: str = "127.0.0.1", port: int = 8000, timeout: float = 0.3) -> bool:
    """The console holds ids in memory (registry, decks, prerender): refuse to move files under it."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _new_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _dup_files(t: Track) -> List[Tuple[str, Path]]:
    return [(cat, p) for cat in CATEGORIES for p in t.files[cat]]


def apply(cache: Path, groups: List[Group], only: Optional[str] = None, stamp: Optional[str] = None,
          is_running: Callable[[], bool] = app_is_running) -> dict:
    """Quarantine the duplicates of each group (atomic per group), remap references. Idempotent: a group
    whose duplicates are already gone is skipped. Returns the manifest."""
    cache = Path(cache)
    if is_running():
        raise ApplyRefused("the app is running: stop uvicorn (start.sh) first, it holds track ids in memory")
    picked = [g for g in groups if only is None or only == g.id or only in {m.id for m in g.members}]
    if only and not picked:
        raise ApplyRefused(f"no such group: {only}")
    stamp = stamp or _new_stamp()
    qdir = cache / QUARANTINE / stamp
    manifest = {"timestamp": stamp, "created": time.time(), "cache": str(cache), "groups": [],
                "restored": False}
    aliases = load_aliases(cache)
    for g in picked:
        entry = {"group": g.id, "canonical": g.canonical.id, "duplicates": [], "moved": [], "missing": [],
                 "edits": [], "aliases": {}}
        moved: List[Tuple[Path, Path]] = []
        try:
            for d in g.duplicates:
                if not d.upload.exists() and not any(p.exists() for _c, p in _dup_files(d)):
                    continue
                entry["duplicates"].append(d.id)
                for cat, src in _dup_files(d):
                    if not os.path.lexists(src):
                        entry["missing"].append(str(src.relative_to(cache)))
                        continue
                    dst = qdir / "files" / src.relative_to(cache)
                    size = _size(src)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(src, dst)
                    moved.append((src, dst))
                    entry["moved"].append({"id": d.id, "kind": cat, "original": str(src.relative_to(cache)),
                                           "quarantined": str(dst.relative_to(cache)), "size": size})
                entry["aliases"][d.id] = g.canonical.id
            if not entry["duplicates"]:
                continue
            _remap_state(cache, g, entry)
            aliases.update(entry["aliases"])
            _write_json_atomic(cache / ALIASES_FILE, aliases)
            manifest["groups"].append(entry)
            _write_json_atomic(qdir / "manifest.json", manifest)
        except Exception:
            for src, dst in reversed(moved):          # atomic per group: put this group's files back
                try:
                    src.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(dst, src)
                except OSError:
                    pass
            _undo_edits(cache, entry["edits"])
            raise
    return manifest


def _remap_state(cache: Path, g: Group, entry: dict) -> None:
    """_names.json: drop the duplicate names.  fame.json: keep the canonical entry (adopt a duplicate's
    when the canonical has none).  Every edit is recorded for --restore."""
    names_p, fame_p = cache / "uploads" / "_names.json", cache / "fame.json"
    names, fame = _read_json(names_p), _read_json(fame_p)
    dups = [d.id for d in g.duplicates if d.id in entry["duplicates"]]
    if isinstance(names, dict):
        for d in dups:
            if d in names:
                entry["edits"].append({"file": "uploads/_names.json", "key": d, "old": names.pop(d), "new": None})
        if any(e["file"] == "uploads/_names.json" for e in entry["edits"]):
            _write_json_atomic(names_p, names)
    if isinstance(fame, dict):
        changed = False
        for d in dups:
            if d in fame:
                old = fame.pop(d)
                entry["edits"].append({"file": "fame.json", "key": d, "old": old, "new": None})
                if g.canonical.id not in fame:
                    fame[g.canonical.id] = old
                    entry["edits"].append({"file": "fame.json", "key": g.canonical.id, "old": None, "new": old})
                changed = True
        if changed:
            _write_json_atomic(fame_p, fame)


def _undo_edits(cache: Path, edits: List[dict]) -> None:
    by_file: Dict[str, List[dict]] = {}
    for e in edits:
        by_file.setdefault(e["file"], []).append(e)
    for rel, es in by_file.items():
        p = cache / rel
        data = _read_json(p)
        if not isinstance(data, dict):
            continue
        for e in reversed(es):
            if e["old"] is None:
                data.pop(e["key"], None)
            else:
                data[e["key"]] = e["old"]
        _write_json_atomic(p, data)


def restore(cache: Path, stamp: str, is_running: Callable[[], bool] = app_is_running) -> dict:
    """Put a quarantine run back: files, name/fame edits, alias entries. Skips (and reports) files whose
    original path is occupied or whose quarantined copy is gone; safe to repeat."""
    cache = Path(cache)
    if is_running():
        raise ApplyRefused("the app is running: stop uvicorn (start.sh) first")
    mpath = cache / QUARANTINE / stamp / "manifest.json"
    manifest = _read_json(mpath)
    if not isinstance(manifest, dict):
        raise ApplyRefused(f"no manifest for {stamp}")
    restored, skipped = 0, []
    aliases = load_aliases(cache)
    for entry in manifest.get("groups", []):
        for m in entry["moved"]:
            src, dst = cache / m["quarantined"], cache / m["original"]
            if not os.path.lexists(src):
                continue
            if os.path.lexists(dst):
                skipped.append(str(dst))
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
            restored += 1
        _undo_edits(cache, entry["edits"])
        for dup in entry["aliases"]:
            aliases.pop(dup, None)
    _write_json_atomic(cache / ALIASES_FILE, aliases)
    manifest["restored"] = True
    manifest["restored_at"] = time.time()
    _write_json_atomic(mpath, manifest)
    return {"restored": restored, "skipped_occupied": skipped}


def purge(cache: Path, stamp: str) -> int:
    """Delete one quarantine run for good (only when the user asks). Returns bytes freed."""
    qdir = Path(cache) / QUARANTINE / stamp
    if not qdir.is_dir():
        raise ApplyRefused(f"no quarantine run {stamp}")
    n = _size(qdir)
    shutil.rmtree(qdir)
    return n


# ---------------------------------------------------------------------------------------------
# prevention: reuse an existing copy instead of downloading another upload of the same song
# ---------------------------------------------------------------------------------------------

_SEARCH_PREFIX = re.compile(r"^\s*ytm?search\d*:", re.IGNORECASE)


def wanted_from_url(url: str) -> Optional[str]:
    """'ytmsearch:Artist - Title' / 'ytsearch5:Artist - Title audio' -> 'Artist - Title'; else None."""
    if not _SEARCH_PREFIX.match(url or ""):
        return None
    q = " ".join(_SEARCH_PREFIX.sub("", url, count=1).split())
    q = re.sub(r"\s+audio$", "", q, flags=re.IGNORECASE)
    return q if " - " in q else None


def find_existing(wanted: str, names: Dict[str, str], exists: Callable[[str], bool] = lambda _i: True,
                  cache: Optional[Path] = None) -> Optional[str]:
    """Id of a library track that is the same recording as `wanted` ("Artist - Title", strong name match,
    same remix markers), preferring the cleanest upload; None when there is none.  Aliases resolve duplicate
    ids to their canonical copy.  Pure lookup: no network."""
    want = identity(wanted)
    if not want.artist_known or not want.title:
        return None
    aliases = load_aliases(cache) if cache is not None else {}
    hits = []
    for tid, name in names.items():
        if name_match(want, identity(name)) == "strong":
            tid = resolve_alias(tid, cache, aliases) if cache is not None else tid
            if exists(tid):
                hits.append((-source_rank(name)[0], tid))
    return sorted(hits)[0][1] if hits else None


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def _default_cache() -> Path:
    from app.music_brain.config import CACHE_DIR
    return CACHE_DIR


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache", type=Path, default=None, help="cache dir (default: the app's data/cache)")
    ap.add_argument("--apply", action="store_true", help="quarantine duplicates (default is a dry run)")
    ap.add_argument("--include-review", action="store_true",
                    help="also treat every REVIEW pair as a duplicate: keep one copy per connected component")
    ap.add_argument("--group", help="apply only this group id (or any member track id)")
    ap.add_argument("--restore", metavar="TS", help="restore a quarantine run")
    ap.add_argument("--purge", metavar="TS", help="delete a quarantine run for good")
    ap.add_argument("--yes", action="store_true", help="confirm --purge")
    ap.add_argument("--report", type=Path, help="also write the dry-run report to this file")
    ap.add_argument("--app-port", type=int, default=8000, help="port whose listener blocks apply/restore")
    args = ap.parse_args(argv)
    cache = (args.cache or _default_cache()).expanduser().resolve()
    running = lambda: app_is_running(port=args.app_port)  # noqa: E731
    try:
        if args.purge:
            if not args.yes:
                print("refusing: --purge deletes the quarantine for good; add --yes")
                return 2
            print(f"purged {args.purge}: {_gb(purge(cache, args.purge))}")
            return 0
        if args.restore:
            print(json.dumps(restore(cache, args.restore, running), indent=2))
            return 0
        tracks = scan(cache)
        groups, reviews = find_groups(tracks)
        rgroups: List[Group] = []
        if args.include_review:
            rgroups = review_groups(groups, reviews, cache)
            moved_ids = {m.id for g in rgroups for m in g.members}
            reviews = [r for r in reviews if not (r.a.id in moved_ids and r.b.id in moved_ids)]
            groups = groups + rgroups
        if args.apply:
            m = apply(cache, groups, only=args.group, is_running=running)
            moved = sum(x["size"] for g in m["groups"] for x in g["moved"])
            print(f"quarantined {sum(len(g['duplicates']) for g in m['groups'])} duplicate(s), {_gb(moved)}: "
                  f"{cache / QUARANTINE / m['timestamp']}\nrestore: --restore {m['timestamp']}")
            return 0
        report = build_report(cache, tracks, groups[:len(groups) - len(rgroups)], reviews, rgroups)
        print(report)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(report, encoding="utf-8")
        return 0
    except ApplyRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
