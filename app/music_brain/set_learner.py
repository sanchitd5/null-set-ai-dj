"""Learn transition and vocal techniques from a recorded DJ set.

    python -m app.music_brain.agent_bridge learn-set <youtube_url | set.mp3> [--tracklist list.txt]

Pipeline (the manual USB002 study, research/notes/set-study-gfF8jzBVWvM.md, as code):

1. Fetch.     Set audio from a URL (yt-dlp) or a local file. Tracklist from
              --tracklist or, failing that, the video description
              ("1:06:30 Artist - Title" lines; "A x B" = layered songs). Songs
              already in data/songs/ are used; the rest are fetched (yt-dlp
              search) into data/cache/sets/<id>/songs/, outside the library.
2. Separate.  The set is cut (ffmpeg) to +-96 s around each tracklist boundary:
              only the blends are studied, not a 70-minute file. 4-stem Demucs
              on every clip and every source song, `jobs` at once (cached by hash).
3. Locate.    For each set window and stem: which source song is playing in
              that stem, where in the song, and at what rate. Onset-envelope
              normalised cross-correlation against the song's own stem, at
              the rate that locks its tempo to the set (pitch-invariant, so
              key-locked stretches match too).
4. Extract.   Per transition (A -> B): the order each stem changes owner
              (bass swap, stem intro, acapella over the other record, hard
              cut, loop). Per vocal passage: where the song's vocal is
              played out of order, repeated, or cut up (the DJ re-sequencing
              lyrics into new lines).
5. Learn.     Observations merge into data/cache/learned_techniques.json;
              techniques.rank() loads them as conditional techniques with
              the tempo gap / key score ranges they were seen at.

Vocal re-sequencing is learned and reported, not performed (live=False).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.music_brain.config import CACHE_DIR, ROOT_DIR

SR = 11025
HOP = 256
FPS = SR / HOP
STEMS = ("drums", "bass", "vocals", "other")
SILENT_DB = -45.0            # same threshold as techniques.SILENT_DB
WIN_S, HOP_S = 8.0, 4.0      # stem-owner windows
VWIN_S, VHOP_S = 3.0, 1.0    # vocal re-cut windows (finer)
CWIN_S, CHOP_HOP_S = 1.0, 0.25  # vocal chop windows (finest)
CHOP_MATCH_R = 0.87          # 1 s envelopes chance-match up to ~0.85 (synthetic dense vocal): stricter
CHOP_SURE_R = 0.93           # two windows this sure make a fragment
CHOP_MAX_DWELL_S = 2.5       # fragment span as seen through 1 s windows; longer = a line, not a chop
CHOP_MIN_JUMPS = 3           # jumps inside one burst to call it chopping
MATCH_R = 0.65               # NCC needed to find a song anywhere in its stem (search)
VOCAL_MATCH_R = 0.72
VDB_STEP_S = 0.25            # resolution of SongData.vocal_db
TRACK_R = 0.35               # NCC at the predicted position (no search): still present
TRACK_SLACK = 3              # frames of slack around the predicted position
JUMP_S = 1.0                 # vocal source-time discontinuity that counts as a cut
SLOT_PAD_S = 120.0           # a song may sound this long before/after its tracklist slot

SETS_DIR = CACHE_DIR / "sets"
SONGS_DIR = ROOT_DIR / "data" / "songs"
LEARNED_PATH = CACHE_DIR / "learned_techniques.json"

# kind -> (what, needs stems, can the live console do it)
KINDS: Dict[str, Tuple[str, bool, bool]] = {
    "bass_swap": ("bass owner flips from A to B in one window while drums keep running", False, True),
    "stem_intro": ("B's drums/top enter before B's bass; A keeps the low end until the swap", True, True),
    "acapella_over": ("one record's vocal rides the other record's drums and bass", True, True),
    "hard_cut": ("every stem of A leaves and B arrives in the same window", False, True),
    "loop_extend": ("A's section is looped (source time stops advancing) to stretch the blend", False, False),
    "vocal_resequence": ("a song's vocal played out of order: lines cut and re-joined into new lyrics", True, False),
    "vocal_loop": ("one vocal line repeated back to back (chant / stutter)", True, False),
    "vocal_chop": ("short vocal fragments (under ~2 s) re-triggered from different points of the song, played as an instrument", True, False),
    "acapella_drop": ("beat out under one sung line (drums + bass gone, vocal alone), then the drop slams back in", True, True),
}


# ------------------------------------------------------------------ tracklist
@dataclass
class TrackEntry:
    start: float                 # seconds into the set
    title: str                   # "Artist - Title" as listed
    path: Optional[str] = None   # local audio once fetched


_TS = re.compile(r"^\s*[\[(]?((?:\d{1,2}:)?\d{1,2}:\d{2})[\])]?\s*(?:[-–—.|)]\s*)?(.+?)\s*$")


def _secs(ts: str) -> float:
    s = 0
    for part in ts.split(":"):
        s = s * 60 + int(part)
    return float(s)


def parse_tracklist(text: str) -> List[TrackEntry]:
    """'1:06:30 Artist - Title' lines -> entries sorted by time. IDs and blanks skipped."""
    out = []
    for line in (text or "").splitlines():
        if line.lstrip().startswith("|"):                # markdown table row: | t | label | ... |
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            line = " ".join(cells[:2])
        m = _TS.match(line)
        if not m:
            continue
        # "A x B x C" = a mashup: each song is its own layer from the same time
        for title in re.split(r"\s+[x×]\s+", m.group(2).strip()):
            title = re.sub(r"\s+x\d+$", "", title.strip())          # "Quiereme x2" = a return, same song
            if not title or title in ("...", "…") or re.fullmatch(r"(?i)id(\s*-\s*id)?\??|end", title):
                continue
            out.append(TrackEntry(_secs(m.group(1)), title))
    out.sort(key=lambda e: e.start)
    return out


# --------------------------------------------------------------------- fetch
def _is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s or ""))


MAX_SONG_S = 15 * 60        # a search hit longer than this is a mix/compilation, not the song


def _ydl(outdir: Path, template: str, max_s: Optional[float] = None) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    opts = {"format": "bestaudio/best", "outtmpl": str(outdir / template), "noplaylist": True,
            "quiet": True, "no_warnings": True, "noprogress": True, "restrictfilenames": True,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}]}
    if max_s:
        opts["match_filter"] = lambda info, *_: (None if (info.get("duration") or 0) <= max_s
                                                 else f"longer than {max_s / 60:.0f} min: not the song")
    return opts


def fetch_set(source: str) -> Tuple[Path, str, str]:
    """-> (audio path, set id, description text). URL via yt-dlp; else a local file."""
    if not _is_url(source):
        p = Path(source).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(p)
        return p, hashlib.sha256(str(p).encode()).hexdigest()[:12], ""
    import yt_dlp

    from app.music_brain import yt_guard

    def run(extra: dict):
        with yt_dlp.YoutubeDL(_ydl(SETS_DIR, "%(id)s.%(ext)s") | extra) as ydl:
            info = ydl.extract_info(source, download=False)
            if not info:
                raise ValueError(f"could not read {source}")
            path = SETS_DIR / f"{info['id']}.mp3"
            if not path.exists():
                ydl.download([source])
            return info, path
    info, path = yt_guard.call(run)
    if not path.exists():
        raise ValueError(f"download finished but {path.name} is missing (ffmpeg installed?)")
    return path, str(info["id"]), info.get("description") or ""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def find_or_fetch_song(title: str, download_dir: Path, library: Path = SONGS_DIR, download: bool = True,
                       exclude_ids: Sequence[str] = ()) -> Optional[Path]:
    """Library (or earlier download) file whose name contains the listed title, else
    a yt-dlp search download into download_dir. Downloads stay out of the library:
    a search on a bare title can land on the wrong song."""
    key = _norm(re.sub(r"\(.*?\)|\[.*?\]", "", title))
    for d in (library, download_dir):
        if d.is_dir() and key:
            for p in d.iterdir():
                if p.suffix.lower() in (".mp3", ".wav", ".flac", ".m4a") and key in _norm(p.stem):
                    return p
    if not download:
        return None
    import yt_dlp

    from app.music_brain import yt_guard
    from app.music_brain.lyrics import split_title

    opts = _ydl(download_dir, "%(title)s.%(ext)s", max_s=MAX_SONG_S)
    artist, track = split_title(title)

    def run(extra: dict) -> Optional[Path]:
        with yt_dlp.YoutubeDL(opts | extra | {"extract_flat": True}) as ydl:
            found = (ydl.extract_info(f"ytsearch8:{title} audio", download=False) or {}).get("entries") or []
        hit = pick_result([e for e in found if e.get("id") not in exclude_ids], artist, track)
        if hit is None:
            return None                       # nothing provably this song: better missing than wrong
        with yt_dlp.YoutubeDL(opts | extra) as ydl:
            info = ydl.extract_info(hit["url"], download=True)
        base = Path(ydl.prepare_filename(info)).with_suffix(".mp3")
        return base if base.exists() else None
    try:
        return yt_guard.call(run)             # bot check: YouTube is paused for a while, song stays missing
    except Exception:                         # one missing song must not stop the study
        return None


def _words(s: str) -> set:
    return {w for w in _norm_words(s) if len(w) > 1 or w.isdigit()}


def _norm_words(s: str) -> List[str]:
    return re.sub(r"[^\w]+", " ", re.sub(r"\(.*?\)|\[.*?\]", "", s or "").lower()).split()


_MASHUP = re.compile(r"(?i)\s[x×]\s|\bmash-?up\b|\bmegamix\b|\bdj set\b|\bboiler room\b|\bset\b|\bedit\b|\bflip\b")


def pick_result(entries: Sequence[dict], artist: str, track: str) -> Optional[dict]:
    """First search result that names this song: every title word in the video title,
    and (with an artist) an artist word in the video title or channel. Ignores
    results longer than MAX_SONG_S (mixes) and covers/karaoke/sped-up edits."""
    tw, aw = _words(track), _words(artist)
    for e in entries:
        vt = e.get("title") or ""
        ch = e.get("channel") or e.get("uploader") or ""
        if (e.get("duration") or 0) > MAX_SONG_S or re.search(r"(?i)\b(cover|karaoke|sped up|slowed|nightcore|reaction)\b", vt):
            continue
        # a mashup / mix / edit that *contains* the song is not the song (often the set itself)
        if _MASHUP.search(vt) and not _MASHUP.search(f"{artist} {track}"):
            continue
        if tw and not tw <= _words(vt):
            continue
        if aw and not aw & (_words(vt) | _words(ch)):
            continue
        if e.get("url") or e.get("id"):
            return e | {"url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}"}
    return None


# ------------------------------------------------------------------ matching
def _load(path: str) -> np.ndarray:
    import librosa

    y, _ = librosa.load(path, sr=SR, mono=True)
    return y


def onset_env(y: np.ndarray) -> np.ndarray:
    import librosa

    env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=HOP).astype(np.float64)
    # smoothed: a song rarely lands on the same hop grid in the set (sub-hop offset)
    w = np.hanning(7)[1:-1]
    return np.convolve(env, w / w.sum(), mode="same")


def rms_db(y: np.ndarray) -> float:
    return float(10 * np.log10(np.mean(np.square(y)) + 1e-12)) if len(y) else -120.0


def ncc(template: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Normalised cross-correlation of template against every offset of x ('valid')."""
    from scipy.signal import correlate

    m = len(template)
    if m < 4 or len(x) < m:
        return np.zeros(0)
    t = template - template.mean()
    tn = np.linalg.norm(t)
    if tn < 1e-9:
        return np.zeros(len(x) - m + 1)
    num = correlate(x, t, mode="valid")
    c1 = np.concatenate([[0.0], np.cumsum(x)])
    c2 = np.concatenate([[0.0], np.cumsum(x * x)])
    s1 = c1[m:] - c1[:-m]
    s2 = c2[m:] - c2[:-m]
    den = np.sqrt(np.maximum(s2 - s1 * s1 / m, 0.0)) * tn
    return np.where(den > 1e-9, num / np.maximum(den, 1e-9), 0.0)


def _rates(set_bpm: float, src_bpm: float) -> List[float]:
    """1.0 plus the rate(s) that lock the song's tempo to the set's (half/double folded)."""
    out = [1.0]
    if set_bpm > 0 and src_bpm > 0:
        for k in (1.0, 2.0, 0.5):
            r = set_bpm / src_bpm * k
            if 0.8 <= r <= 1.25 and all(abs(r - o) > 0.005 for o in out):
                out.append(r)
    return out


@dataclass
class Hit:
    track: int        # tracklist index
    r: float          # NCC
    src_t: float      # position in the song (s) at the window start
    rate: float       # song seconds per set second


def locate(seg_env: np.ndarray, src_env: np.ndarray, rates: Sequence[float]) -> Tuple[float, float, float]:
    """Best (r, src_t, rate) of a set-window envelope inside a song's envelope."""
    best = (-1.0, 0.0, 1.0)
    for rate in rates:
        n = int(len(src_env) / rate)
        if n < len(seg_env):
            continue
        rs = np.interp(np.arange(n) * rate, np.arange(len(src_env)), src_env)
        c = ncc(seg_env, rs)
        if len(c):
            i = int(np.argmax(c))
            if c[i] > best[0]:
                best = (float(c[i]), i * rate / FPS, float(rate))
    return best


@dataclass
class SongData:
    title: str
    start: float
    bpm: float
    key: Optional[str]
    env: Dict[str, np.ndarray]     # stem -> onset envelope
    lyrics: List[dict] = field(default_factory=list)
    vocal_db: Optional[np.ndarray] = None   # song's own vocal stem, dB per VDB_STEP_S (None: not checked)   # [{t, end, text}] aligned to this file (app.music_brain.lyrics)


def _candidates(t: float, songs: List[SongData]) -> List[int]:
    """Tracklist slots that may sound at set time t (the listed song, its neighbours)."""
    out = []
    for i, s in enumerate(songs):
        # layered entries ("A x B") share a start: the slot runs to the next later start
        end = next((x.start for x in songs[i + 1:] if x.start > s.start), float("inf"))
        if s.start - SLOT_PAD_S <= t <= end + SLOT_PAD_S:
            out.append(i)
    return out


def stem_timeline(set_stems: Dict[str, np.ndarray], songs: List[SongData], set_bpm_at: Callable[[float], float],
                  win_s: float = WIN_S, hop_s: float = HOP_S, only: Sequence[str] = STEMS,
                  min_r: float = MATCH_R, track: bool = True, t0: float = 0.0,
                  rates: Optional[Sequence[float]] = None) -> List[dict]:
    """[{t, stem, db, owner: Hit|None, owners: [Hit]}] per window per stem.
    owners = every song heard in that stem (two drum tracks overlap in a
    blend); owner = the strongest. set_stems may be a clip of the set that
    starts at t0 (set seconds); set_bpm_at takes clip-local seconds, rows
    carry set time."""
    rows = []
    dur = min(len(y) for y in set_stems.values()) / SR
    envs = {n: onset_env(set_stems[n]) for n in only if n in set_stems}
    # pass 1: strict search anywhere in each candidate song -> anchors
    t = 0.0
    while t + win_s <= dur:
        a, b = int(t * FPS), int((t + win_s) * FPS)
        bpm = set_bpm_at(t)
        for n, env in envs.items():
            db = rms_db(set_stems[n][int(t * SR): int((t + win_s) * SR)])
            hits = []
            if db > SILENT_DB:
                for i in _candidates(t0 + t, songs):
                    if n not in songs[i].env:
                        continue
                    r, st, rate = locate(env[a:b], songs[i].env[n], rates or _rates(bpm, songs[i].bpm))
                    if r >= min_r:
                        hits.append(Hit(i, round(r, 3), round(st, 2), round(rate, 4)))
            rows.append({"t": round(t, 2), "stem": n, "db": round(db, 1), "owners": hits})
        t += hop_s
    if track:
        _track_pass(rows, envs, songs, win_s)
    for r in rows:
        r["owners"].sort(key=lambda h: -h.r)
        r["owner"] = r["owners"][0] if r["owners"] else None
        r["t"] = round(r["t"] + t0, 2)
    return rows


def _score_at(seg_env: np.ndarray, src_env: np.ndarray, src_t: float, rate: float, slack: int = TRACK_SLACK) -> float:
    """NCC of a set window against the song at one predicted position (+-slack frames)."""
    n = len(seg_env)
    pos = src_t * FPS + np.arange(-slack, slack + 1)[:, None] + np.arange(n)[None, :] * rate
    pos = pos[(pos.min(axis=1) >= 0) & (pos.max(axis=1) <= len(src_env) - 1)]   # offsets inside the song
    if not len(pos):
        return 0.0
    cand = np.interp(pos, np.arange(len(src_env)), src_env)
    t = seg_env - seg_env.mean()
    c = cand - cand.mean(axis=1, keepdims=True)
    den = np.linalg.norm(t) * np.linalg.norm(c, axis=1)
    return float(np.max(np.where(den > 1e-9, c @ t / np.maximum(den, 1e-9), 0.0)))


def _track_pass(rows: List[dict], envs: Dict[str, np.ndarray], songs: List[SongData], win_s: float) -> None:
    """Pass 2: where a song was found confidently nearby, test the other windows at
    its predicted position only. A fixed offset has a far lower chance-match
    floor than a search, so a stem sharing the window with another record
    (the blend itself) still counts as present."""
    times = sorted({r["t"] for r in rows})
    step = {t: i for i, t in enumerate(times)}
    at = {(r["t"], r["stem"]): r for r in rows}
    tried = set()
    changed = True
    while changed:        # grow outward from anchors one window at a time, until nothing new
        changed = False
        for r in rows:
            if r["db"] <= SILENT_DB or r["stem"] not in envs:
                continue
            k = step[r["t"]]
            # neighbours: same stem one window either side, or another stem in this window
            nb = [at.get((times[j], r["stem"])) for j in (k - 1, k + 1) if 0 <= j < len(times)]
            nb += [at.get((r["t"], n)) for n in envs if n != r["stem"]]
            have = {h.track for h in r["owners"]}
            for src in nb:
                for h in (src["owners"] if src else []):
                    if h.track in have or r["stem"] not in songs[h.track].env or (r["t"], r["stem"], h.track) in tried:
                        continue
                    tried.add((r["t"], r["stem"], h.track))
                    pred = h.src_t + (r["t"] - src["t"]) * h.rate
                    a, b = int(r["t"] * FPS), int((r["t"] + win_s) * FPS)
                    s = _score_at(envs[r["stem"]][a:b], songs[h.track].env[r["stem"]], pred, h.rate)
                    if s >= TRACK_R:
                        r["owners"].append(Hit(h.track, round(s, 3), round(pred, 2), h.rate))
                        have.add(h.track)
                        changed = True


# ------------------------------------------------------------------- extract
@dataclass
class Observation:
    kind: str
    set_id: str
    at: float                     # set time (s)
    track_a: str
    track_b: str = ""
    tempo_gap: Optional[float] = None
    key_score: Optional[float] = None
    detail: dict = field(default_factory=dict)


def _owner_grid(rows: List[dict]) -> Dict[float, Dict[str, set]]:
    """{t: {stem: {tracks heard}}}"""
    g: Dict[float, Dict[str, set]] = {}
    for r in rows:
        g.setdefault(r["t"], {})[r["stem"]] = {h.track for h in r.get("owners", [])}
    return g


def transitions(rows: List[dict], songs: List[SongData], set_id: str, hop_s: float = HOP_S) -> List[Observation]:
    """Per consecutive A -> B: the order the stems changed owner, classified."""
    from app.music_brain.techniques import camelot_score

    grid = _owner_grid(rows)
    times = sorted(grid)
    hits = {(r["t"], r["stem"], h.track): h for r in rows for h in r.get("owners", [])}
    out: List[Observation] = []
    for ia in range(len(songs) - 1):
        ib = ia + 1
        A, B = songs[ia], songs[ib]
        gap = round(abs(B.bpm / A.bpm - 1), 4) if A.bpm > 0 and B.bpm > 0 else None   # unknown: no range learned
        ks = camelot_score(A.key, B.key) if A.key and B.key else None
        base = dict(set_id=set_id, track_a=A.title, track_b=B.title, tempo_gap=gap, key_score=ks)
        win = [t for t in times if B.start - SLOT_PAD_S <= t <= B.start + SLOT_PAD_S]
        a_last, b_first = {}, {}
        for n in STEMS:
            ta = [t for t in win if ia in grid[t].get(n, ())]
            tb = [t for t in win if ib in grid[t].get(n, ())]
            if ta:
                a_last[n] = max(ta)
            if tb:
                b_first[n] = min(tb)
        if not b_first:
            continue
        at = min(b_first.values())
        # bass swap: A's bass hands to B's within about one window, drums running
        if "bass" in a_last and "bass" in b_first and -hop_s <= b_first["bass"] - a_last["bass"] <= hop_s * 1.5:
            t = b_first["bass"]
            if grid.get(t, {}).get("drums", set()) & {ia, ib}:
                out.append(Observation("bass_swap", at=t, **base, detail={"swap_at": t}))
        # stem intro: B's drums or top arrive at least two windows before B's bass
        early = [n for n in ("drums", "other") if n in b_first and b_first[n] <= b_first.get("bass", 1e9) - 2 * hop_s]
        if early and "bass" in a_last:
            out.append(Observation("stem_intro", at=at, **base, detail={
                "order": sorted(b_first, key=b_first.get), "lead_s": round(b_first.get("bass", at) - at, 1)}))
        # acapella over: one record's vocal alone over the other's drums + bass
        for t in win:
            g = grid[t]
            v, d, bs = g.get("vocals", set()), g.get("drums", set()), g.get("bass", set())
            for vv, dd in ((ia, ib), (ib, ia)):
                if v == {vv} and d == {dd} and bs == {dd}:
                    out.append(Observation("acapella_over", at=t, **base, detail={"vocal_from": "A" if vv == ia else "B"}))
                    break
            else:
                continue
            break
        # hard cut: A's stems all gone the window B's all arrive, no overlap
        if a_last and max(a_last.values()) < min(b_first.values()) + hop_s \
                and max(b_first.values()) - min(b_first.values()) <= hop_s:
            out.append(Observation("hard_cut", at=at, **base))
        # loop: A's drums keep returning to the same source spot across windows
        src = [hits[(t, "drums", ia)].src_t for t in win if (t, "drums", ia) in hits]
        if len(src) >= 3 and max(src[-3:]) - min(src[-3:]) < hop_s:
            out.append(Observation("loop_extend", at=at, **base, detail={"src_t": src[-1]}))
    return out


MIN_RUN_WINDOWS = 3          # consecutive agreeing windows before a source position counts
SAME_MATERIAL_R = 0.8       # two song positions this alike are one part heard twice (a chorus)
MAX_PASSAGE_S = 45.0         # a re-cut is local: longer vocal stretches are split


def same_material(song: SongData, a: float, b: float, dur: float, stem: str = "vocals") -> bool:
    """Is the song's own vocal at a..a+dur the same as at b..b+dur? A repeated chorus
    makes the set match either copy, which must not read as the DJ jumping."""
    env = song.env.get(stem)
    if env is None:
        return False
    i, j, n = int(a * FPS), int(b * FPS), int(dur * FPS)
    if min(i, j) < 0 or max(i, j) + n > len(env) or n < 4:
        return False
    x, y = env[i:i + n] - env[i:i + n].mean(), env[j:j + n] - env[j:j + n].mean()
    d = np.linalg.norm(x) * np.linalg.norm(y)
    return bool(d > 1e-9 and float(x @ y) / d >= SAME_MATERIAL_R)


def sung_at(song: SongData, t0: float, dur: float) -> bool:
    """Does the song itself carry a vocal over t0..t0+dur? A set-vocal window that
    matches a spot where the song's own vocal is silent is bleed, not the singer."""
    if song.vocal_db is None:
        return True
    a, b = int(t0 / VDB_STEP_S), int((t0 + dur) / VDB_STEP_S) + 1
    seg = song.vocal_db[max(0, a):b]
    return bool(len(seg)) and float(np.mean(seg > SILENT_DB)) >= 0.5


def vocal_db_curve(y: np.ndarray) -> np.ndarray:
    n = int(VDB_STEP_S * SR)
    k = len(y) // n
    return 10 * np.log10(np.mean(np.square(y[:k * n].reshape(k, n)), axis=1) + 1e-12)


def _only_sung(rows: List[dict], songs: List[SongData], win_s: float) -> List[dict]:
    out = []
    for r in rows:
        hs = [h for h in r["owners"] if sung_at(songs[h.track], h.src_t, win_s)]
        out.append(r | {"owners": hs, "owner": hs[0] if hs else None})
    return out


def _drop_tracks(rows: List[dict], bad: set) -> List[dict]:
    out = []
    for r in rows:
        hs = [h for h in r["owners"] if h.track not in bad]
        out.append(r | {"owners": hs, "owner": hs[0] if hs else None})
    return out


def vocal_runs(vrows: List[dict], hop_s: float = VHOP_S, songs: Optional[List[SongData]] = None,
               win_s: float = VWIN_S) -> Dict[int, List[dict]]:
    """Per song: continuous runs of its vocal [{set_t0, set_t1, src_t0, src_t1}]."""
    runs: Dict[int, List[dict]] = {}
    cur: Dict[int, dict] = {}
    for r in sorted((r for r in vrows if r["owner"]), key=lambda r: r["t"]):
        h, t = r["owner"], r["t"]
        c = cur.get(h.track)
        expect = c["src_t1"] + (t - c["set_t1"]) * h.rate if c else None
        near = c and t - c["set_t1"] <= hop_s * 1.5
        if near and abs(h.src_t - expect) > JUMP_S and songs and same_material(songs[h.track], h.src_t, expect, win_s):
            h = Hit(h.track, h.r, round(expect, 2), h.rate)             # the other copy of the same chorus
        if near and abs(h.src_t - expect) <= JUMP_S:
            c["set_t1"], c["src_t1"] = t, h.src_t
        else:
            cur[h.track] = {"set_t0": t, "set_t1": t, "src_t0": h.src_t, "src_t1": h.src_t}
            runs.setdefault(h.track, []).append(cur[h.track])
    # a position held by fewer than MIN_RUN_WINDOWS agreeing windows is a chance match, not a line
    return {k: [r for r in v if r["set_t1"] - r["set_t0"] >= (MIN_RUN_WINDOWS - 1) * hop_s - 1e-6] for k, v in runs.items()}


def vocal_recuts(vrows: List[dict], songs: List[SongData], set_id: str, win_s: float = VWIN_S,
                 hop_s: float = VHOP_S, max_gap_s: float = 16.0) -> List[Observation]:
    """Out-of-order and repeated vocal lines within one passage of a song's vocal."""
    out = []
    for tr, runs in vocal_runs(vrows, hop_s, songs, win_s).items():
        passage: List[dict] = []
        for run in runs + [None]:
            if run is not None and (not passage or (run["set_t0"] - passage[-1]["set_t1"] <= max_gap_s
                                                    and run["set_t1"] - passage[0]["set_t0"] <= MAX_PASSAGE_S)):
                passage.append(run)
                continue
            if len(passage) >= 2:
                lines = [(round(p["src_t0"], 1), round(p["src_t1"] + win_s, 1)) for p in passage]
                repeats = sum(1 for i in range(1, len(lines)) if abs(lines[i][0] - lines[i - 1][0]) <= JUMP_S)
                order = [p["src_t0"] for p in passage]
                resequenced = any(b < a - JUMP_S or b > a + (lines[i][1] - lines[i][0]) + 4 * JUMP_S
                                  for i, (a, b) in enumerate(zip(order, order[1:])))
                detail = {"set_span": [passage[0]["set_t0"], passage[-1]["set_t1"] + win_s], "source_lines": lines}
                if songs[tr].lyrics:                  # the new lyric the DJ built, in the song's own words
                    from app.music_brain.lyrics import words_between
                    detail["words"] = [words_between(songs[tr].lyrics, a, b) for a, b in lines]
                if repeats:
                    out.append(Observation("vocal_loop", set_id=set_id, at=passage[0]["set_t0"],
                                           track_a=songs[tr].title, detail=detail | {"repeats": repeats}))
                if resequenced:
                    out.append(Observation("vocal_resequence", set_id=set_id, at=passage[0]["set_t0"],
                                           track_a=songs[tr].title, detail=detail))
            passage = [run] if run is not None else []
    return out


def vocal_chops(crows: List[dict], songs: List[SongData], set_id: str, hop_s: float = CHOP_HOP_S,
                burst_gap_s: float = 4.0) -> List[Observation]:
    """Bursts of short vocal fragments whose source position keeps jumping."""
    out = []
    by_track: Dict[int, List[Tuple[float, Hit]]] = {}
    for r in sorted((r for r in crows if r["owner"]), key=lambda r: r["t"]):
        by_track.setdefault(r["owner"].track, []).append((r["t"], r["owner"]))
    for tr, hits in by_track.items():
        # fragments: runs of windows where source time advances with set time
        frags: List[dict] = []
        for t, h in hits:
            f = frags[-1] if frags else None
            if f and t - f["t1"] <= hop_s * 1.5 and abs(h.src_t - (f["src1"] + (t - f["t1"]) * h.rate)) <= 0.25:
                f["t1"], f["src1"], f["r"] = t, h.src_t, min(f["r"], h.r)
            else:
                frags.append({"t0": t, "t1": t, "src0": h.src_t, "src1": h.src_t, "r": h.r})
        # a lone window is often a chance match (it straddles two fragments): need two that agree
        # a chop is short: 2 windows are enough when both match very well, else 3
        frags = [f for f in frags if f["t1"] - f["t0"] >= (MIN_RUN_WINDOWS - 1) * hop_s - 1e-6
                 or (f["t1"] > f["t0"] and f["r"] >= CHOP_SURE_R)]
        burst: List[dict] = []
        for f in frags + [None]:
            if f is not None and (f["t1"] - f["t0"] + CWIN_S) <= CHOP_MAX_DWELL_S and \
                    (not burst or f["t0"] - burst[-1]["t1"] <= burst_gap_s):
                burst.append(f)
                continue
            jumps = sum(1 for a, b in zip(burst, burst[1:])
                        if abs(b["src0"] - (a["src1"] + (b["t0"] - a["t1"]))) > JUMP_S
                        and not same_material(songs[tr], b["src0"], a["src1"] + (b["t0"] - a["t1"]), CWIN_S))
            if jumps >= CHOP_MIN_JUMPS:
                from app.music_brain.lyrics import words_between
                lyr = songs[tr].lyrics
                out.append(Observation("vocal_chop", set_id=set_id, at=burst[0]["t0"], track_a=songs[tr].title, detail={
                    "set_span": [burst[0]["t0"], burst[-1]["t1"] + CWIN_S], "jumps": jumps,
                    "fragments": [[round(x["src0"], 2), round(x["src1"] + CWIN_S, 2)] for x in burst],
                    "words": [words_between(lyr, x["src0"], x["src1"] + CWIN_S) for x in burst] if lyr else []}))
            burst = [f] if f is not None and (f["t1"] - f["t0"] + CWIN_S) <= CHOP_MAX_DWELL_S else []
    return out


def acapella_drops(rows: List[dict], songs: List[SongData], set_id: str, hop_s: float = HOP_S,
                   max_hold_s: float = 32.0) -> List[Observation]:
    """Vocal alone (drums and bass silent) for up to max_hold_s, then drums + bass
    back: the DJ making a drop out of a sung line. Records the words held."""
    from app.music_brain.lyrics import is_hook, words_between

    grid: Dict[float, Dict[str, dict]] = {}
    for r in rows:
        grid.setdefault(r["t"], {})[r["stem"]] = r
    times = sorted(grid)
    out, i = [], 0
    while i < len(times):
        g = grid[times[i]]
        solo = lambda g: all(n in g for n in ("vocals", "drums", "bass")) and g["vocals"]["db"] > SILENT_DB \
            and g["drums"]["db"] <= SILENT_DB and g["bass"]["db"] <= SILENT_DB
        if not solo(g):
            i += 1
            continue
        j = i
        while j + 1 < len(times) and times[j + 1] - times[j] <= hop_s * 1.5 and solo(grid[times[j + 1]]):
            j += 1
        k = j + 1
        back = k < len(times) and times[k] - times[j] <= hop_s * 1.5 and \
            grid[times[k]].get("drums", {}).get("db", -120) > SILENT_DB and grid[times[k]].get("bass", {}).get("db", -120) > SILENT_DB
        held = times[j] - times[i] + hop_s
        v = next((grid[times[x]]["vocals"]["owner"] for x in range(i, j + 1) if grid[times[x]]["vocals"].get("owner")), None)
        if back and held <= max_hold_s and v is not None:
            s = songs[v.track]
            src1 = v.src_t + held * v.rate
            words = words_between(s.lyrics, v.src_t, src1) if s.lyrics else ""
            drop_by = grid[times[k]]["drums"].get("owner")
            out.append(Observation("acapella_drop", set_id=set_id, at=times[i], track_a=s.title,
                                   track_b=songs[drop_by.track].title if drop_by and drop_by.track != v.track else "",
                                   detail={"held_s": round(held, 1), "src_span": [round(v.src_t, 1), round(src1, 1)],
                                           "words": words, "hook": bool(words) and any(is_hook(w, s.lyrics) for w in words.split(" / ")),
                                           "drop_at": times[k]}))
        i = k
    return out


# --------------------------------------------------------------------- store
def load_learned(path: Path = LEARNED_PATH) -> Dict[str, dict]:
    """{kind: {kind, what, stems, live, observations[], tempo_gap_max, key_score_min}}; {} if none/corrupt."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def merge(observations: List[Observation], path: Path = LEARNED_PATH) -> Dict[str, dict]:
    """Merge into the store. Re-learning the same set replaces its old observations."""
    store = load_learned(path)
    sets = {o.set_id for o in observations}
    for entry in store.values():
        entry["observations"] = [o for o in entry.get("observations", []) if o.get("set_id") not in sets]
    for o in observations:
        what, stems, live = KINDS[o.kind]
        e = store.setdefault(o.kind, {"kind": o.kind, "what": what, "stems": stems, "live": live, "observations": []})
        e["observations"].append(asdict(o))
    for e in store.values():
        obs = e["observations"]
        e["count"] = len(obs)
        e["ai_rules"] = list(dict.fromkeys(o["detail"]["ai_rule"] for o in obs if o.get("detail", {}).get("ai_rule")))[:8]
        e["tempo_gap_max"] = max([o["tempo_gap"] for o in obs if o.get("tempo_gap") is not None], default=None)
        e["key_score_min"] = min([o["key_score"] for o in obs if o.get("key_score") is not None], default=None)
    store = {k: v for k, v in store.items() if v["observations"] or v.get("user_rules")}
    _save(store, path)
    return store


def _save(store: Dict[str, dict], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
    tmp.replace(path)


def add_user_rule(kind: str, text: str = "", disable: Optional[bool] = None, path: Path = LEARNED_PATH) -> dict:
    """The user's refinement of a learned technique, after hearing it live.

    Same loop as the USB002 study: the set is the evidence, the user's ear is
    the final word ("rap ~9 dB under the riff", "one tonal owner"). Rules are
    kept across re-learning; disable=True keeps the technique out of rank()."""
    if kind not in KINDS:
        raise ValueError(f"unknown technique {kind!r}; one of {', '.join(KINDS)}")
    text = (text or "").strip()
    if not text and disable is None:
        raise ValueError("give a rule text, --disable or --enable")
    store = load_learned(path)
    what, stems, live = KINDS[kind]
    e = store.setdefault(kind, {"kind": kind, "what": what, "stems": stems, "live": live, "observations": [], "count": 0,
                                "tempo_gap_max": None, "key_score_min": None})
    if text:
        from datetime import date

        e.setdefault("user_rules", []).append({"text": text[:500], "date": date.today().isoformat()})
    if disable is not None:
        e["disabled"] = bool(disable)
    _save(store, path)
    return e


# ----------------------------------------------------------------------- run
def _set_tempo_fn(drums: np.ndarray) -> Callable[[float], float]:
    """Set tempo per 30 s block from the set's drum stem."""
    import librosa

    env = onset_env(drums)
    blk = int(30 * FPS)
    tempi = []
    for i in range(0, len(env), blk):
        seg = env[i:i + blk]
        tempi.append(float(np.atleast_1d(librosa.feature.tempo(onset_envelope=seg, sr=SR, hop_length=HOP))[0])
                     if len(seg) > FPS * 4 else 0.0)
    return lambda t: tempi[min(int(t // 30), len(tempi) - 1)] if tempi else 0.0


PROVEN_SONG_SHARE = 0.5      # heard this much of its slot: the file is the song, its name can be trusted


def lyric_query(title: str, path: Optional[str], heard_share: float) -> Optional[str]:
    """'Artist - Title' to look lyrics up by, or None. A bare title ("Delilah") matches
    any artist's song, so the artist must come from the tracklist, or from the download's
    file name once the file is proven to be what the set played."""
    from app.music_brain.lyrics import is_manual, load_plain, split_title

    if split_title(title)[0] or is_manual(title) or load_plain(title):
        return title
    if path and heard_share >= PROVEN_SONG_SHARE:
        q = Path(path).stem.replace("_", " ")
        return q if split_title(q)[0] else None
    return None


def _attach_lyrics(songs: List[SongData], entries: List[TrackEntry], verdict: List[dict],
                   vocal_paths: Dict[str, str], log: Callable[[str], None]) -> None:
    from app.music_brain import lyrics as ly

    cache: Dict[str, List[dict]] = {}
    for i, (s, e, v) in enumerate(zip(songs, entries, verdict)):
        if not s.env or v["likely_wrong_song"] or e.path not in vocal_paths:
            continue
        q = lyric_query(e.title, e.path, v["heard_share"])
        if q is None:
            log(f"{e.title}: no artist known, lyrics not looked up (pin with: agent_bridge lyrics)")
            continue
        if q not in cache:
            y = _load(vocal_paths[e.path])
            cache[q] = ly.for_file(q, y, SR, duration=len(y) / SR, log=log)
        s.lyrics = cache[q]
        v["lyrics"] = {"query": q, "lines": len(s.lyrics), "estimated": bool(s.lyrics and s.lyrics[0].get("estimated"))}


WRONG_SONG_SHARE = 0.3       # heard in under 30 % of its own slot's windows: probably not the song


def verify_songs(rows: List[dict], songs: List[SongData]) -> List[dict]:
    """Per tracklist entry: share of its slot's windows (any stem) where it was heard.
    A search download that never matches the set is flagged, not trusted."""
    heard: Dict[float, set] = {}
    for r in rows:
        heard.setdefault(r["t"], set()).update(h.track for h in r.get("owners", []))
    out = []
    for i, s in enumerate(songs):
        end = next((x.start for x in songs[i + 1:] if x.start > s.start), s.start + 240.0)
        ts = [t for t in heard if s.start <= t < end]
        share = sum(1 for t in ts if i in heard[t]) / len(ts) if ts else 0.0
        out.append({"heard_share": round(share, 3), "likely_wrong_song": bool(s.env) and share < WRONG_SONG_SHARE})
    return out


WHOLE_UNDER_S = 15 * 60      # shorter than this: one clip, the whole file
CLIP_PAD_S = 96.0            # seconds either side of a tracklist boundary sent to Demucs
DEFAULT_JOBS = 2             # Demucs runs at once (each holds a model in memory)


def plan_clips(starts: Sequence[float], duration: float, pad: float = CLIP_PAD_S) -> List[Tuple[float, float]]:
    """Set spans worth separating: +-pad around every boundary, overlaps merged.
    Only the blends are studied, so a 70-minute set becomes a few short clips.
    A short edit (a single mashup) is studied whole."""
    if duration <= WHOLE_UNDER_S:
        return [(0.0, round(duration, 2))]
    spans = sorted((max(0.0, t - pad), min(duration, t + pad)) for t in sorted(set(starts)) if 0 < t < duration)
    out: List[Tuple[float, float]] = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        elif b - a > WIN_S:
            out.append((a, b))
    return [(round(a, 2), round(b, 2)) for a, b in out]


def clip_audio(src: Path, t0: float, t1: float, out_dir: Path) -> Path:
    """ffmpeg cut to 44.1 kHz stereo WAV (deterministic bytes -> Demucs cache hits on re-runs)."""
    import subprocess

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{t0:09.2f}-{t1:09.2f}.wav"
    if out.exists() and out.stat().st_size > 0:
        return out
    tmp = out.with_suffix(".tmp.wav")
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}",
                    "-i", str(src), "-map_metadata", "-1", "-fflags", "+bitexact", "-ac", "2", "-ar", "44100",
                    "-c:a", "pcm_s16le", str(tmp)], check=True)
    tmp.replace(out)
    return out


def _parallel(fn: Callable, items: Sequence, jobs: int, log: Callable[[str], None], label: Callable[[object], str]) -> list:
    """fn over items, at most `jobs` at once, results in item order. One failure is logged, not fatal."""
    from concurrent.futures import ThreadPoolExecutor

    def run(x):
        try:
            return fn(x)
        except Exception as exc:                      # a bad clip/song must not sink the study
            log(f"failed {label(x)}: {exc}")
            return None

    with ThreadPoolExecutor(max_workers=max(1, int(jobs))) as ex:
        return list(ex.map(run, items))


def learn_set(source: str, tracklist: Optional[str] = None, download: bool = True,
              store_path: Path = LEARNED_PATH, log: Callable[[str], None] = lambda m: None,
              jobs: int = DEFAULT_JOBS, ai: bool = True) -> dict:
    """Study one set. tracklist: text or a path to a text file. jobs: Demucs runs at once."""
    import librosa

    from app.music_brain.analyzer import analyze
    from app.music_brain.stem_service import separate

    set_path, set_id, desc = fetch_set(source)
    text = tracklist or ""
    if text and len(text) < 4096 and Path(text).expanduser().is_file():
        text = Path(text).expanduser().read_text(encoding="utf-8")
    entries = parse_tracklist(text or desc)
    if len(entries) < 2:
        raise ValueError("need a tracklist with at least 2 timestamped songs (--tracklist, or the video description)")

    missing = []
    for e in entries:
        p = find_or_fetch_song(e.title, SETS_DIR / set_id / "songs", download=download, exclude_ids=(set_id,))
        e.path = str(p) if p else None
        if not p:
            missing.append(e.title)
        log(f"song {e.title}: {p or 'NOT FOUND'}")

    duration = float(librosa.get_duration(path=str(set_path)))
    clips = plan_clips([e.start for e in entries], duration)
    log(f"clipping {len(clips)} blend windows ({sum(b - a for a, b in clips) / 60:.0f} of {duration / 60:.0f} min)")
    clip_paths = [clip_audio(set_path, a, b, SETS_DIR / set_id / "clips") for a, b in clips]

    vocal_paths: Dict[str, str] = {}

    def song_job(path: str):
        log(f"separating {Path(path).name}")
        stems = separate(path).stems
        env, vdb = {}, None
        for n, q in stems.items():
            if n in STEMS:
                y = _load(q)
                env[n] = onset_env(y)
                if n == "vocals":
                    vdb = vocal_db_curve(y)
                    vocal_paths[path] = q
        a = analyze(path)
        return {"bpm": float(a.bpm), "key": a.key.camelot if a.key else None, "env": env, "vocal_db": vdb}

    def clip_job(path: Path):
        log(f"separating clip {path.name}")
        return {n: _load(q) for n, q in separate(path).stems.items() if n in STEMS}

    uniq = sorted({e.path for e in entries if e.path})           # a song listed 3x separates once
    done = dict(zip(uniq, _parallel(song_job, uniq, jobs, log, lambda x: Path(x).name)))
    songs = [SongData(e.title, e.start, **(done.get(e.path) or {"bpm": 0.0, "key": None, "env": {}})) for e in entries]

    rows: List[dict] = []
    vrows: List[dict] = []
    crows: List[dict] = []
    clip_stems = _parallel(clip_job, clip_paths, jobs, log, lambda x: x.name)
    for (t0, _), stems in zip(clips, clip_stems):
        if not stems or "drums" not in stems:
            continue
        bpm_at = _set_tempo_fn(stems["drums"])
        log(f"locating stems {t0 / 60:.1f} min")
        rows += stem_timeline(stems, songs, bpm_at, t0=t0)
        vrows += stem_timeline(stems, songs, bpm_at, VWIN_S, VHOP_S, only=("vocals",), min_r=VOCAL_MATCH_R,
                               track=False, t0=t0)
        crows += stem_timeline(stems, songs, bpm_at, CWIN_S, CHOP_HOP_S, only=("vocals",), min_r=CHOP_MATCH_R,
                               track=False, t0=t0)
    if not rows:
        raise ValueError("no clip could be separated")

    vrows, crows = _only_sung(vrows, songs, VWIN_S), _only_sung(crows, songs, CWIN_S)
    verdict = verify_songs(rows, songs)
    for i, v in enumerate(verdict):
        if v["likely_wrong_song"]:
            log(f"{songs[i].title}: heard in {v['heard_share']:.0%} of its slot, probably the wrong download: not learned from")
            songs[i] = SongData(songs[i].title, songs[i].start, songs[i].bpm, songs[i].key, {})
    bad = {i for i, v in enumerate(verdict) if v["likely_wrong_song"]}
    rows, vrows, crows = (_drop_tracks(x, bad) for x in (rows, vrows, crows))
    _attach_lyrics(songs, entries, verdict, vocal_paths, log)
    obs = transitions(rows, songs, set_id) + vocal_recuts(vrows, songs, set_id) + acapella_drops(rows, songs, set_id) \
        + vocal_chops(crows, songs, set_id)
    # the balance the DJ ran at each move: what the user's feedback is usually about
    lv = {(r["t"], r["stem"]): r["db"] for r in rows}
    ts = sorted({r["t"] for r in rows})
    for o in obs:
        t = max((x for x in ts if x <= o.at), default=ts[0])
        o.detail["levels_db"] = {n: lv.get((t, n)) for n in STEMS}
    ai_res = {"kept": obs, "rejected": [], "ai": "off"}
    if ai:
        from app.music_brain import set_ai

        ai_res = set_ai.review(obs, log=log)
        log(f"ai: {ai_res['ai']}, kept {len(ai_res['kept'])}, rejected {len(ai_res['rejected'])}")
        obs = ai_res["kept"]
    store = merge(obs, store_path)

    report = {
        "set_id": set_id, "set_path": str(set_path), "missing": missing, "clips": clips,
        "tracks": [asdict(e) | v for e, v in zip(entries, verdict)],
        "identified_share": round(sum(1 for r in rows if r["owner"]) / max(1, sum(1 for r in rows if r["db"] > SILENT_DB)), 3),
        "observations": [asdict(o) for o in obs],
        "ai": ai_res["ai"], "ai_rejected": [asdict(o) for o in ai_res["rejected"]],
        "learned": {k: v["count"] for k, v in store.items()},
    }
    out_dir = SETS_DIR / set_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "study.json").write_text(json.dumps(report | {"timeline": [
        {"t": r["t"], "stem": r["stem"], "db": r["db"], "owners": [asdict(h) for h in r["owners"]]} for r in rows]}, indent=2),
        encoding="utf-8")
    report["study_path"] = str(out_dir / "study.json")
    return report
