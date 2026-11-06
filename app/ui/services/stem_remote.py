"""The console's client of the stem server (app/stem_server): ONE GPU worker for the decks, the library
backfill and the phone, so the console never loads a Demucs model of its own.

On when STEM_SERVER_URL is set (CT116: http://127.0.0.1:47830). The token is STEM_SERVER_TOKEN, or the
STEM_SERVER_TOKEN= line of the env file named by STEM_SERVER_TOKEN_FILE (the stem server's own
/etc/null-set/stem-server.env), so the secret is not copied into a second file. Unset: the console keeps
its own local worker (a laptop with no stem server), unchanged.

Both processes share the cache (data/cache): a split lands in stems/<sha256>_htdemucs_ft and the console
reads it from there. Library tracks outside uploads/ are linked into stem_links/<id><ext> for the server.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

TIMEOUT_S = 5.0


class Unreachable(RuntimeError):
    """The stem server did not answer (down, restarting)."""


def url() -> str:
    return os.environ.get("STEM_SERVER_URL", "").strip().rstrip("/")


def enabled() -> bool:
    return bool(url())


def token() -> str:
    t = os.environ.get("STEM_SERVER_TOKEN", "").strip()
    if t:
        return t
    f = os.environ.get("STEM_SERVER_TOKEN_FILE", "").strip()
    if f:
        try:
            for line in Path(f).read_text(encoding="utf-8").splitlines():
                k, sep, v = line.strip().partition("=")
                if sep and k.strip() == "STEM_SERVER_TOKEN":
                    return v.strip().strip("'\"")
        except OSError as exc:
            raise RuntimeError(f"STEM_SERVER_TOKEN_FILE unreadable: {exc.strerror}") from None
    raise RuntimeError("STEM_SERVER_URL is set but neither STEM_SERVER_TOKEN nor STEM_SERVER_TOKEN_FILE gives a token")


def _req(method: str, path: str, body: Optional[dict] = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url() + path, data=data, method=method,
                               headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=TIMEOUT_S) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            js = json.loads(e.read() or b"{}")
        except ValueError:
            js = {}
        return e.code, js if isinstance(js, dict) else {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Unreachable(f"stem server unreachable ({getattr(exc, 'reason', exc)})") from None


def link(track_id: str, path: Path, cache_dir: Path) -> None:
    """Make a track the server can find by id: uploads/<id>.* already is; anything else (a library
    file) gets stem_links/<id><ext> -> the file. The server checks sha256(bytes)[:16] == id itself."""
    path = Path(path).resolve()
    if path.parent == (Path(cache_dir) / "uploads").resolve():
        return
    d = Path(cache_dir) / "stem_links"
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{track_id}{path.suffix.lower()}"
    if dest.is_symlink() and os.readlink(dest) == str(path):
        return
    tmp = d / f".{track_id}.{os.getpid()}.lnk"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(path)
    os.replace(tmp, dest)


def submit(track_id: str, path: Path, priority: str, cache_dir: Path) -> dict:
    """Queue a 4-stem split at `priority` (deck | booked | phone | backfill). -> the server's job dict."""
    link(track_id, path, cache_dir)
    st, js = _req("POST", "/v1/separate", {"id": track_id, "stems": 4, "priority": priority})
    if st != 200:
        raise RuntimeError(f"stem server refused {track_id}: {st} {js.get('error', '')}".strip())
    return js


def job(job_id: str) -> Optional[dict]:
    """The job, or None when the server does not know it (restarted before it saved it)."""
    st, js = _req("GET", f"/v1/jobs/{job_id}")
    if st == 404:
        return None
    if st != 200:
        raise Unreachable(f"stem server answered {st} for job {job_id}")
    return js


def queue() -> dict:
    st, js = _req("GET", "/v1/jobs")
    if st != 200:
        raise Unreachable(f"stem server answered {st} for /v1/jobs")
    return js


def separate(audio_path, two_stems: Optional[str] = None, model: Optional[str] = None,
             priority: str = "deck", timeout_s: float = 1800.0, **_kw):
    """stem_service.separate() through the stem server: blocks until the song's htdemucs_ft set exists.
    Every caller gets the ft 4-stem set (a 2-stem request gets its vocals from it)."""
    from app.music_brain.audio import stem_service
    from app.music_brain.config import CACHE_DIR

    path = Path(audio_path)
    h = stem_service.file_hash(path)
    st = stem_service.complete_ft(h)
    cached = st is not None
    if st is None:
        js = submit(h[:16], path, priority, CACHE_DIR)
        t_end = time.monotonic() + timeout_s
        while js.get("status") not in ("done", "error"):
            if time.monotonic() > t_end:
                raise TimeoutError(f"stem server: {h[:16]} not split after {timeout_s:.0f} s")
            time.sleep(1.0)
            js = job(js["job"]) or submit(h[:16], path, priority, CACHE_DIR)
        if js["status"] == "error":
            raise RuntimeError(f"stem server: {js.get('error')}")
        st = stem_service.complete_ft(h)
        if st is None:
            raise RuntimeError("stem server finished but the htdemucs_ft set is not complete")
    if two_stems:
        st = {two_stems: st[two_stems]} if two_stems in st else st
    return stem_service.StemResult(audio_hash=h, model=stem_service.DEMUCS_MODEL, two_stems=two_stems,
                                   stems=st, cache_dir=str(Path(next(iter(st.values()))).parent),
                                   from_cache=cached)


def status_line(js: Optional[dict], queued: int, error: Optional[str] = None) -> str:
    """One line for the console's STEMS badge: model, device, real progress, elapsed, ETA, queue."""
    if error:
        return f"stem server: {error}"
    if not js:
        return f"{queued} queued" if queued else ""
    parts = [js.get("model") or "", js.get("device") or ""]
    if js.get("status") == "running":
        parts.append(f"{round(100 * float(js.get('progress') or 0))}%")
        el = js.get("elapsed")
        eta = js.get("eta")
        parts.append(f"{el:.0f} s" + (f", ~{eta:.0f} s left" if eta is not None else "") if el is not None else "")
    elif js.get("position"):
        parts.append(f"#{js['position']} in queue")
    if queued:
        parts.append(f"{queued} queued")
    return " · ".join(p for p in parts if p)
