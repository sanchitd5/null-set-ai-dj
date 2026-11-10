"""Album art for library tracks: fetched once per track on the server, cached, served to web and phone.

Source chain, first confident hit wins:
  1. art embedded in the file (ffprobe/ffmpeg attached_pic; no new dependency)
  2. iTunes Search  3. Deezer  4. MusicBrainz + Cover Art Archive  5. the YouTube thumbnail (only when the
  track's download URL was a YouTube one), cropped square.  None: the client keeps its gradient.

Privacy: third parties see only the artist and title (and, for YouTube, the video id of a download the
server already made). No user, account or track id ever leaves the server.

Matching: artist and title are normalised (accents, case, punctuation, feat./Official Video noise gone) and
compared by similarity; remix info is kept. A wanted remix takes an exact remix hit first, else the original
cover only when the artist matches; a candidate that is a *different* remix, or a remix of a wanted original,
is rejected. Low confidence means no art, never a wrong cover.

Cache: <cache>/art/<track_id>.jpg (<= 600 px square), <track_id>.160.jpg, and <track_id>.json
(source, score, fetched_at; or a negative result with retry_after so a miss is not retried every scan).
"""
from __future__ import annotations

import difflib
import io
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app.ui.services.track_identity import clean_identity

SIZES = (600, 160)
MIN_SCORE = 0.80
MIN_TITLE_SIM = 0.85
MIN_ARTIST_SIM = 0.75
NEG_RETRY_S = 7 * 86400          # nothing matched: look again in a week
ERR_RETRY_S = 3600               # sources unreachable: look again in an hour
MAX_IMAGE_BYTES = 8 * 1024 * 1024
HTTP_TIMEOUT_S = 10.0
TRACK_GAP_S = 0.5                # the backfill worker idles this long between tracks
MIN_INTERVAL_S = {"itunes": 3.0, "deezer": 0.3, "musicbrainz": 1.1, "caa": 0.6, "youtube": 0.5}
USER_AGENT = "NullSetDJ/1.0 ( %s )" % os.environ.get("DJ_ART_CONTACT", "album-art lookup, artist+title only")
IMAGE_HOSTS = ("mzstatic.com", "dzcdn.net", "deezer.com", "coverartarchive.org", "archive.org", "ytimg.com")

_REMIX_WORDS = r"remix|rmx|vip|bootleg|flip|rework|edit|mix|version"
_PLAIN_TAGS = re.compile(r"^(?:original|extended|radio|club|album|single|clean|explicit|instrumental|"
                         r"remaster(?:ed)?(?:\s+\d{4})?|\d{4}\s+remaster(?:ed)?|mono|stereo)"
                         r"(?:\s+(?:mix|edit|version|remaster(?:ed)?))?$", re.I)
_TAG_RE = re.compile(r"[\(\[]([^)\]]+)[\)\]]")
_DASH_TAIL_RE = re.compile(r"\s+[-–]\s+([^-–]+)$")
_YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([0-9A-Za-z_-]{11})(?![0-9A-Za-z_-])")
_ID_RE = re.compile(r"[0-9A-Za-z_-]{1,64}")


# ---------------------------------------------------------------- paths / state
def art_dir() -> Path:
    from app.music_brain import config

    return Path(config.CACHE_DIR) / "art"


def enabled() -> bool:
    return os.environ.get("DJ_ART_FETCH", "1") != "0"


def art_path(track_id: str, size: int = 600) -> Path:
    return art_dir() / (f"{track_id}.jpg" if size == 600 else f"{track_id}.{size}.jpg")


def _meta_path(track_id: str) -> Path:
    return art_dir() / f"{track_id}.json"


def read_meta(track_id: str) -> Optional[dict]:
    try:
        return json.loads(_meta_path(track_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def has_art(track_id: str) -> bool:
    return _ID_RE.fullmatch(track_id or "") is not None and art_path(track_id).is_file()


# {track_id: mtime ms of the 600 px cover}: one scandir of art/ on first use, then kept in step by _store,
# so /api/tracks does zero syscalls per track (a stat per track on mergerfs was the slow part).
_mt_lock = threading.Lock()
_mt: Optional[dict] = None
_mt_dir: Optional[Path] = None


def invalidate_art_index() -> None:
    global _mt
    with _mt_lock:
        _mt = None


def _art_mtimes() -> dict:
    global _mt, _mt_dir
    d = art_dir()
    with _mt_lock:
        if _mt is None or _mt_dir != d:
            m = {}
            try:
                with os.scandir(d) as it:
                    for e in it:
                        n = e.name
                        if n.endswith(".jpg") and n.count(".") == 1 and e.is_file():
                            m[n[:-4]] = e.stat().st_mtime_ns // 1_000_000
            except OSError:
                pass
            _mt, _mt_dir = m, d
        return _mt


def art_url(track_id: str) -> Optional[str]:
    """Where a client fetches the cover, or None (keep the gradient). ?v= changes when the file does."""
    if _ID_RE.fullmatch(track_id or "") is None:
        return None
    v = _art_mtimes().get(track_id)
    if v is None:
        return None
    return f"/api/tracks/{track_id}/art?size=600&v={v:x}"


def with_art(row: dict, key: str = "track_id") -> dict:
    row["art_url"] = art_url(str(row.get(key) or ""))
    return row


_src_lock = threading.Lock()


def remember_source(track_id: str, url: str) -> None:
    """Keep the YouTube video id of a download (the only source detail stored) for the thumbnail step."""
    m = _YT_ID_RE.search(url or "")
    if not m or not _ID_RE.fullmatch(track_id or ""):
        return
    with _src_lock:
        p = art_dir() / "sources.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[track_id] = m.group(1)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass


def _video_id(track_id: str) -> Optional[str]:
    try:
        return json.loads((art_dir() / "sources.json").read_text(encoding="utf-8")).get(track_id)
    except (OSError, ValueError):
        return None


def track_for_video(video_id: str, usable: Callable[[str], bool] = lambda _t: True) -> Optional[str]:
    """The library track already downloaded from this YouTube video (oldest id first for a stable answer), or None.
    `usable` filters out ids that are gone or flagged as wrong."""
    try:
        data = json.loads((art_dir() / "sources.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for tid in sorted(t for t, v in data.items() if v == video_id):
        if usable(tid):
            return tid
    return None


# ---------------------------------------------------------------- identity / matching
@dataclass(frozen=True)
class Ident:
    artist: str          # primary artist, "" when unknown
    title: str           # title without the remix tag
    remix: str           # "Other Remix" or ""

    def query(self) -> str:
        return f"{self.artist}|{self.title}|{self.remix}".lower()


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def norm(s: str) -> str:
    s = _fold(s).lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def split_remix(title: str) -> Tuple[str, str]:
    """("Glue", "Other Remix") from "Glue (Other Remix)" or "Glue - Other Remix"; plain tags (Radio Edit,
    Original Mix, Remastered 2011) are dropped, not treated as remix identity."""
    remix, base = "", title
    for m in list(_TAG_RE.finditer(title)):
        tag = m.group(1).strip()
        if _PLAIN_TAGS.match(tag):
            base = base.replace(m.group(0), " ")
        elif re.search(rf"\b(?:{_REMIX_WORDS})\b", tag, re.I) and not remix:
            remix = tag
            base = base.replace(m.group(0), " ")
    m = _DASH_TAIL_RE.search(base)
    if m:
        tag = m.group(1).strip()
        if _PLAIN_TAGS.match(tag):
            base = base[:m.start()]
        elif re.search(rf"\b(?:{_REMIX_WORDS})\b", tag, re.I) and not remix:
            remix, base = tag, base[:m.start()]
    return re.sub(r"\s{2,}", " ", base).strip(" -–"), remix


def ident_from_name(name: str) -> Ident:
    artist, title = clean_identity(name or "")
    base, remix = split_remix(title)
    return Ident("" if artist == "Unknown" else artist, base or title, remix)


def _remix_key(remix: str) -> str:
    return norm(re.sub(rf"\b(?:{_REMIX_WORDS})\b", " ", remix, flags=re.I))


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _artist_sim(wanted: str, cand: str) -> float:
    w, c = norm(wanted), norm(cand)
    if not w or not c:
        return 0.0
    if w == c:
        return 1.0
    if re.search(rf"(?:^|\s){re.escape(w)}(?:\s|$)", c):          # "fred again" in "fred again the blessed madonna"
        return 0.9
    return _ratio(w, c)


@dataclass
class Cand:
    artist: str
    title: str
    image: str
    score: float = 0.0


def score_candidate(want: Ident, c: Cand) -> float:
    """0..1.1; 0 = rejected. Artist and title similarity, remix rule on top."""
    if not want.artist:
        return 0.0                         # no artist to check against: a wrong cover is worse than none
    base, remix = split_remix(c.title)
    t_sim = _ratio(norm(want.title), norm(base))
    a_sim = _artist_sim(want.artist, c.artist)
    if t_sim < MIN_TITLE_SIM or a_sim < MIN_ARTIST_SIM:
        return 0.0
    s = 0.55 * t_sim + 0.45 * a_sim
    if want.remix and remix:
        if _remix_key(want.remix) != _remix_key(remix):
            return 0.0                     # a different remix
        s += 0.1                           # exact remix
    elif want.remix:
        if a_sim < 0.85:
            return 0.0
        s -= 0.05                          # the original's cover, artist confirmed
    elif remix:
        return 0.0                         # we want the original, this is someone's remix
    return s if s >= MIN_SCORE else 0.0


# ---------------------------------------------------------------- http (patched in tests: no real network there)
_last_hit: Dict[str, float] = {}
_throttle_lock = threading.Lock()
_sleep: Callable[[float], None] = time.sleep
_now: Callable[[], float] = time.monotonic


def _throttle(source: str) -> None:
    with _throttle_lock:
        wait = _last_hit.get(source, -1e9) + MIN_INTERVAL_S.get(source, 1.0) - _now()
        if wait > 0:
            _sleep(wait)
        _last_hit[source] = _now()


def _http_get(url: str, headers: Optional[dict] = None, timeout: float = HTTP_TIMEOUT_S) -> bytes:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise OSError("https only")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:     # noqa: S310 -- scheme checked above
        data = r.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise OSError("response too large")
    return data


def _get_json(source: str, url: str) -> dict:
    _throttle(source)
    return json.loads(_http_get(url, {"Accept": "application/json"}).decode("utf-8", "replace"))


def _get_image(source: str, url: str) -> bytes:
    host = urllib.parse.urlsplit(url).hostname or ""
    if not any(host == h or host.endswith("." + h) for h in IMAGE_HOSTS):
        raise OSError(f"image host not allowed: {host}")
    _throttle(source)
    return _http_get(url)


# ---------------------------------------------------------------- sources
def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _term(w: Ident) -> str:
    return " ".join(x for x in (w.artist, w.title, w.remix) if x)


def search_itunes(w: Ident) -> List[Cand]:
    d = _get_json("itunes", f"https://itunes.apple.com/search?term={_q(_term(w))}&entity=song&limit=5")
    out = []
    for r in d.get("results") or []:
        art = r.get("artworkUrl100") or ""
        if art:
            out.append(Cand(r.get("artistName") or "", r.get("trackName") or "",
                            re.sub(r"/\d+x\d+(bb)?\.(jpg|png)$", r"/600x600bb.\2", art)))
    return out


def search_deezer(w: Ident) -> List[Cand]:
    q = f'artist:"{w.artist}" track:"{" ".join(x for x in (w.title, w.remix) if x)}"'
    d = _get_json("deezer", f"https://api.deezer.com/search?q={_q(q)}&limit=5")
    if not d.get("data"):                  # the fielded query is flaky on Deezer's side: plain terms, same scoring
        d = _get_json("deezer", f"https://api.deezer.com/search?q={_q(_term(w))}&limit=5")
    out = []
    for r in d.get("data") or []:
        alb = r.get("album") or {}
        img = alb.get("cover_xl") or alb.get("cover_big") or ""
        if img:
            out.append(Cand((r.get("artist") or {}).get("name") or "", r.get("title") or "", img))
    return out


def search_musicbrainz(w: Ident) -> List[Cand]:
    q = f'artist:"{w.artist}" AND recording:"{w.title}"'
    d = _get_json("musicbrainz", f"https://musicbrainz.org/ws/2/recording?query={_q(q)}&fmt=json&limit=5")
    out = []
    for r in d.get("recordings") or []:
        credit = " ".join((c.get("name") or "") + (c.get("joinphrase") or "") for c in r.get("artist-credit") or [])
        for rel in (r.get("releases") or [])[:2]:
            if rel.get("id"):
                out.append(Cand(credit, r.get("title") or "",
                                f"https://coverartarchive.org/release/{rel['id']}/front-500"))
    return out


SOURCES: List[Tuple[str, Callable[[Ident], List[Cand]]]] = [
    ("itunes", search_itunes), ("deezer", search_deezer), ("musicbrainz", search_musicbrainz)]
_IMAGE_BUCKET = {"musicbrainz": "caa"}      # which rate limit the image download falls under


def embedded_art(path: Path) -> Optional[bytes]:
    """The attached picture of an mp3/m4a/flac, copied out untouched. None when there is none."""
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not (ffprobe and ffmpeg):
        return None
    try:
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v", "-show_entries", "stream=index:stream_disposition=attached_pic",
             "-of", "json", str(path)], capture_output=True, timeout=30, check=True)
        streams = json.loads(probe.stdout or b"{}").get("streams") or []
        pic = next((s["index"] for s in streams if (s.get("disposition") or {}).get("attached_pic")), None)
        if pic is None:
            return None
        out = subprocess.run(
            [ffmpeg, "-nostdin", "-loglevel", "error", "-i", str(path), "-map", f"0:{pic}", "-an", "-vcodec", "copy",
             "-frames:v", "1", "-f", "image2pipe", "-"], capture_output=True, timeout=60, check=True)
        return out.stdout or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def youtube_thumb(video_id: str) -> Optional[bytes]:
    for name in ("maxresdefault", "hqdefault"):
        try:
            return _get_image("youtube", f"https://i.ytimg.com/vi/{video_id}/{name}.jpg")
        except OSError:
            continue
    return None


# ---------------------------------------------------------------- image processing
def _pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def make_variants(raw: bytes) -> Tuple[bytes, bytes]:
    """(600 px, 160 px) square JPEGs from any image bytes; center-cropped, never upscaled past 600."""
    Image = _pil()
    if Image is not None:
        img = Image.open(io.BytesIO(raw))
        img.load()
        img = img.convert("RGB")
        side = min(img.size)
        l, t = (img.width - side) // 2, (img.height - side) // 2
        img = img.crop((l, t, l + side, t + side))
        if side > SIZES[0]:
            img = img.resize((SIZES[0], SIZES[0]), Image.LANCZOS)
        big, small = io.BytesIO(), io.BytesIO()
        img.save(big, "JPEG", quality=88, optimize=True)
        img.resize((SIZES[1], SIZES[1]), Image.LANCZOS).save(small, "JPEG", quality=82, optimize=True)
        return big.getvalue(), small.getvalue()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise OSError("neither Pillow nor ffmpeg available")

    def run(data: bytes, vf: str) -> bytes:
        r = subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-i", "pipe:0", "-vf", vf, "-frames:v", "1",
                            "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "pipe:1"],
                           input=data, capture_output=True, timeout=60)
        if r.returncode or not r.stdout:
            raise OSError("ffmpeg could not scale the cover")
        return r.stdout

    big = run(raw, f"crop='min(iw,ih)':'min(iw,ih)',scale='min({SIZES[0]},iw)':'min({SIZES[0]},iw)'")
    return big, run(big, f"scale={SIZES[1]}:{SIZES[1]}")


def _atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _store(track_id: str, raw: bytes, meta: dict) -> None:
    big, small = make_variants(raw)
    d = art_dir()
    d.mkdir(parents=True, exist_ok=True)
    meta = {**meta, "fetched_at": time.time()}
    _atomic(art_path(track_id, 160), small)
    _atomic(_meta_path(track_id), json.dumps(meta).encode())
    _atomic(art_path(track_id, 600), big)         # last: art_url appears only when everything is in place
    try:
        mt = art_path(track_id).stat().st_mtime_ns // 1_000_000
    except OSError:
        mt = None
    with _mt_lock:
        if _mt is not None and mt is not None:
            _mt[track_id] = mt


def _store_negative(track_id: str, want: Ident, retry_s: float) -> None:
    now = time.time()
    try:
        art_dir().mkdir(parents=True, exist_ok=True)
        _atomic(_meta_path(track_id), json.dumps(
            {"source": None, "query": want.query(), "fetched_at": now, "retry_after": now + retry_s}).encode())
    except OSError:
        pass


# ---------------------------------------------------------------- the chain
def needs_fetch(track_id: str, name: str) -> bool:
    if has_art(track_id):
        return False
    meta = read_meta(track_id)
    if meta and meta.get("source") is None and meta.get("query") == ident_from_name(name).query():
        return time.time() >= float(meta.get("retry_after") or 0)
    return True


def find_art(track_id: str, path: Optional[Path], name: str) -> Optional[dict]:
    """Run the chain; store and return the meta of the first confident hit, else record a miss and return None.
    Network errors in a source skip that source; if nothing matched and some source failed, retry sooner."""
    want = ident_from_name(name)
    failed = False

    if path is not None:
        raw = embedded_art(path)
        if raw:
            try:
                meta = {"source": "embedded", "score": 1.0}
                _store(track_id, raw, meta)
                return meta
            except (OSError, ValueError):
                pass

    if want.artist:
        for src, search in SOURCES:
            try:
                cands = search(want)
            except (OSError, ValueError):
                failed = True
                continue
            ranked = sorted(((score_candidate(want, c), c) for c in cands), key=lambda x: -x[0])
            for score, c in [x for x in ranked if x[0] > 0][:3]:
                try:
                    raw = _get_image(_IMAGE_BUCKET.get(src, src), c.image)
                    meta = {"source": src, "score": round(score, 3), "match": f"{c.artist} - {c.title}"}
                    _store(track_id, raw, meta)
                    return meta
                except (OSError, ValueError):
                    failed = True

    vid = _video_id(track_id)
    if vid:
        raw = youtube_thumb(vid)
        if raw:
            try:
                meta = {"source": "youtube", "score": 0.5}
                _store(track_id, raw, meta)
                return meta
            except (OSError, ValueError):
                failed = True
        else:
            failed = True

    _store_negative(track_id, want, ERR_RETRY_S if failed else NEG_RETRY_S)
    return None


# ---------------------------------------------------------------- background worker + backfill
_resolver: Optional[Callable[[str], Optional[Tuple[Optional[Path], str]]]] = None
_q_ids: "queue.Queue[str]" = queue.Queue()
_queued: set = set()
_worker_lock = threading.Lock()
_worker: Optional[threading.Thread] = None


def set_resolver(fn: Callable[[str], Optional[Tuple[Optional[Path], str]]]) -> None:
    """fn(track_id) -> (path, display name), or None for a track that should not get art (mix, live)."""
    global _resolver
    _resolver = fn


def enqueue(track_id: str) -> None:
    global _worker
    if not enabled() or _resolver is None or not _ID_RE.fullmatch(track_id or ""):
        return
    with _worker_lock:
        if track_id in _queued:
            return
        _queued.add(track_id)
        _q_ids.put(track_id)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_worker, name="album-art", daemon=True)
            _worker.start()


def _run_worker() -> None:
    while True:
        try:
            tid = _q_ids.get(timeout=30)
        except queue.Empty:
            return
        try:
            got = _resolver(tid) if _resolver else None
            if got and needs_fetch(tid, got[1]):
                find_art(tid, got[0], got[1])
        except Exception as exc:  # noqa: BLE001 -- one bad track never stops the backfill
            print(f"[art] {tid}: {exc}", flush=True)
        finally:
            with _worker_lock:
                _queued.discard(tid)
        time.sleep(TRACK_GAP_S)


def start_backfill(track_ids: Callable[[], List[str]]) -> Optional[threading.Thread]:
    """Queue every library track that has no art and no fresh miss. Returns the scan thread."""
    if not enabled() or _resolver is None:
        return None

    def scan() -> None:
        n = 0
        for tid in list(track_ids()):
            try:
                got = _resolver(tid) if _resolver else None
                if got and needs_fetch(tid, got[1]):
                    enqueue(tid)
                    n += 1
            except Exception:  # noqa: BLE001
                continue
        print(f"[art] library backfill: {n} tracks to look up", flush=True)

    t = threading.Thread(target=scan, name="album-art-scan", daemon=True)
    t.start()
    return t
