"""The world outside the decision code: model, YouTube, downloads, stems, caches.

The virtual set runs the REAL server code (app.ui.server, autopilot_service, download
routes, analysis, matcher ...) through a test client. Only the edges that touch the network
or a GPU are replaced, and every replaced edge goes through one of three backends:

  live     the real thing: the LLM server, YouTube search / download, Demucs, librosa.
           With `record=True` every answer is written to a fixture (app/sim/fixtures/<run>/).
  replay   answers come from a fixture, zero network / LLM / Demucs. Deterministic.
  library  offline: songs come from the local source library (DATA_DIR), the "model" is the
           seeded StubLLM. Used to build the first fixtures without a model server, and by
           pytest.

The edges (each patched once, all in install()):
  autopilot_service._chat_call          the LLM HTTP call (suggest, look-ahead, plan)
  download_service.search_songs / verify_song / song_views / download_to_dir   YouTube
  server._queue_stems                   Demucs (live: separates synchronously, capped)
  analyzer._file_hash (+ vibe copy)     replay songs are placeholder files, not audio
  energy.library_raws                   the library's energy distribution, frozen per run
  session_log.log                       events land in the run's events.jsonl, virtual time

A replay that diverges from the recording (a rule change picked another song) is answered
from the pool / StubLLM where it can be and counts a `miss`; the report shows the count so a
run that left its recording is never mistaken for one that followed it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from app.sim.pool import (FIXTURES_DIR, MARKER, SHARED_DIR, Pool, marker_bytes, marker_hash, round_floats)
from app.sim.stubllm import LLM, StubLLM, split_name

FIXTURE_VERSION = 1
_WORD = re.compile(r"[a-z0-9]+")


class WorldError(RuntimeError):
    """A real dependency the run needs is unreachable (fail clearly, never silently stub)."""


@dataclass
class Caps:
    max_downloads: int = 14          # YouTube downloads per run (each is a real fetch in live mode)
    min_interval_s: float = 3.0      # live: pause between two YouTube calls (on top of yt_guard)


def _words(s: str) -> list:
    return _WORD.findall((s or "").lower())


class World:
    def __init__(self, mode: str, name: str, seed: int, run_cache: Path, pool: Optional[Pool] = None,
                 record: bool = False, llm: Optional[LLM] = None, library=None, caps: Optional[Caps] = None,
                 fixtures_dir: Optional[Path] = None):
        if mode not in ("live", "replay", "library"):
            raise ValueError(f"bad world mode {mode!r}")
        self.mode, self.name, self.seed = mode, name, seed
        self.run_cache = Path(run_cache)
        self.pool = pool or Pool()
        self.record = record
        self.llm = llm                       # library / replay-fallback model
        self.library = library               # MainLibrary (library world, live seed track)
        self.caps = caps or Caps()
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else FIXTURES_DIR
        self.dir = self.fixtures_dir / name
        self.ctx: dict = {}                  # set by the driver: {song_index, phase, current, ...}
        self.vclock: Callable[[], float] = lambda: 0.0
        self.events: list = []
        self.misses: list = []               # replay answers that had to leave the recording
        self.downloads = 0
        self.fx = self._blank_fixture()
        if mode == "replay":
            self.fx = json.loads((self.dir / "run.json").read_text(encoding="utf-8"))
        self._llm_n: dict = {}
        self._tracks: dict = {}              # track id -> pool entry (hash, name) for tracks in this run
        self._patched: list = []
        self._last_net = 0.0
        self.last_llm_key = ""
        self.fatal: Optional[WorldError] = None     # set when a needed dependency is missing: the run stops
        self._live_curves: dict = {}
        self.seed_track: Optional[dict] = None
        self.steps: list = []                # song_log steps captured with virtual time
        self.touched: dict = {}              # hash -> name of every pool entry this run used (record)

    def _blank_fixture(self) -> dict:
        return {"version": FIXTURE_VERSION, "name": self.name, "source": self.mode, "seed": self.seed,
                "llm": [], "search": {}, "verify": {}, "views": {}, "downloads": {}, "library_raws": [],
                "seed_track": None, "pool": []}

    # ---- patching -----------------------------------------------------------------
    def _patch(self, obj, attr, value) -> None:
        self._patched.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def uninstall(self) -> None:
        while self._patched:
            obj, attr, old = self._patched.pop()
            setattr(obj, attr, old)

    def install(self) -> None:
        """Patch every edge. Call once, after AIDJ_CACHE_DIR points at run_cache."""
        from app.music_brain import analyzer, energy, vibe
        from app.ui import autopilot_service as svc
        from app.ui import download_service as dl
        from app.ui import server, session_log

        orig_hash = analyzer._file_hash

        def file_hash(path):
            h = marker_hash(Path(path))
            return h if h else orig_hash(path)

        self._patch(analyzer, "_file_hash", file_hash)
        self._patch(vibe, "_file_hash", file_hash)
        self._patch(session_log, "log", self._emit)
        self._patch(svc, "_chat_call", self._chat_call)
        self._patch(svc, "SUGGEST_BUDGET_S", 1e9)     # retries are a rule, not a race against a wall clock
        self._patch(dl, "search_songs", self._search_songs)
        self._patch(dl, "verify_song", self._verify_song)
        self._patch(dl, "song_views", self._song_views)
        self._patch(dl, "download_to_dir", self._download_to_dir)
        self._patch(server, "_queue_stems", self._queue_stems)
        raws = self._library_raws()
        self._patch(energy, "library_raws", lambda: raws)
        if self.mode != "live":
            self._install_frozen_stems(server)
        from app.ui import song_log

        self._patch(song_log, "step", self._song_step)
        self._patch(song_log, "on_session_event", lambda *a, **k: None)
        # a fresh run never sees a song lookup cached by an earlier one
        svc._verify_cache.clear()
        svc._verify_inflight.clear()
        # session-scoped server state starts empty
        server._suggest_inflight.clear()
        server._set_memory = None
        server._pair_cache.clear()
        self._orig = {"file_hash": orig_hash}
        self._seed_shared_files()

    def _seed_shared_files(self) -> None:
        """The frozen learned_techniques.json (fixtures/_shared) goes into the run's private cache:
        before / after a rule change the run sees the same learned store."""
        for fn in ("learned_techniques.json",):
            src = SHARED_DIR / fn
            if src.exists():
                (self.run_cache / fn).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    def _song_step(self, kind, track_id, **fields) -> None:
        self.steps.append({"t": round(1_790_000_000.0 + self.vclock(), 3), "track_id": track_id, "kind": kind,
                           "phase": fields.get("phase"), "decision": fields.get("decision"), "why": fields.get("why"),
                           "inputs": fields.get("inputs"), "result": fields.get("result")})

    def _install_frozen_stems(self, server) -> None:
        """Replay / library: the stems are pool curves, not audio. server._pair_features (grooves,
        breakdowns, rap detection for the technique library) reads stem audio through librosa.load,
        techniques.stem_map and techniques.vocal_style: those three are answered from the pool's
        frozen results (the same functions ran on the real stems when the entry was built), the
        rest of _pair_features runs as is."""
        import librosa
        import numpy as np

        from app.music_brain import techniques as tq

        class Tagged(np.ndarray):
            pass

        orig_load = librosa.load
        pat = re.compile(r"^sim://stems/([0-9a-f]+)/(\w+)$")

        def load(path, sr=22050, mono=True, offset=0.0, duration=None, **kw):
            m = pat.match(str(path))
            if not m:
                return orig_load(path, sr=sr, mono=mono, offset=offset, duration=duration, **kw)
            a = np.zeros(1, dtype=np.float32).view(Tagged)
            a.tag = (m.group(1), m.group(2), round(float(offset or 0.0), 3))
            return a, sr

        def entry(h):
            e = self.pool.load(h)
            if e is None:
                raise WorldError(f"no fixture data for audio {h[:12]}")
            return e

        orig_map, orig_style = tq.stem_map, tq.vocal_style

        def stem_map(audio, sr, phrases):
            if hasattr(audio.get("drums"), "tag"):
                return entry(audio["drums"].tag[0]).get("smap") or []
            return orig_map(audio, sr, phrases)            # real audio (building a pool entry)

        def vocal_style(y, sr):
            if hasattr(y, "tag"):
                return entry(y.tag[0]).get("vocal_style", {}).get(repr(y.tag[2])) or {"rap": False, "rap_score": 0.0}
            return orig_style(y, sr)

        self._patch(librosa, "load", load)
        self._patch(tq, "stem_map", stem_map)
        self._patch(tq, "vocal_style", vocal_style)

        def hook_drops(tid):
            e = self._tracks.get(tid)
            return list((e or {}).get("hook_drops") or [])

        self._patch(server, "_safe_hook_drops", hook_drops)

    def live_curves(self, tid: str):
        """live mode: stem curves of a song separated in this run (computed once, kept)."""
        from app.sim.pool import stem_curves
        from app.ui import server

        if tid in self._live_curves:
            return self._live_curves[tid]
        paths = server._stem_cache.get(tid)
        try:
            self._live_curves[tid] = stem_curves(paths) if paths else None
        except Exception:
            self._live_curves[tid] = None
        return self._live_curves[tid]

    def pick_seed(self, rng) -> str:
        """The seeded random seed track: run.json's when replaying, else drawn from the library."""
        if self.mode == "replay":
            st = self.fx["seed_track"]
            return self.register_seed(st["name"], audio_hash=st["hash"])
        if self.library is None:
            raise WorldError("no source library to draw a seed track from (set DATA_DIR)")
        lib = self.library.tracks()
        if not lib:
            raise WorldError(f"the source library {self.library.data} has no usable tracks")
        t = lib[rng.randrange(len(lib))]
        self.seed_track = {"hash": t.hash, "name": t.name}
        if self.mode == "library":
            self._entry_for(t.hash)
            return self.register_seed(t.name, audio_hash=t.hash)
        self._copy_seed_caches(t)
        return self.register_seed(t.name, audio_path=t.audio)

    def _copy_seed_caches(self, t) -> None:
        """live: the seed's own analysis / vibe / energy / stems are reused from the source
        library (same code produced them; saves a Demucs run). Read-only on DATA_DIR."""
        a = self.run_cache / "analysis"
        a.mkdir(parents=True, exist_ok=True)
        for suffix in ("v5.json", "vibe.json", "energy.json"):
            src = self.library.cache / "analysis" / f"{t.hash}.{suffix}"
            if src.exists():
                shutil.copy2(src, a / src.name)
        for model in ("htdemucs_ft", "htdemucs"):
            src = self.library.cache / "stems" / f"{t.hash}_{model}"
            dst = self.run_cache / "stems" / src.name
            if src.is_dir() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.symlink_to(src, target_is_directory=True)

    def export_live(self, pool: Pool) -> list:
        """live + record: freeze every song this run analysed into the pool (the fixture's data)."""
        from app.music_brain.analyzer import _file_hash
        from app.sim.pool import assemble_entry
        from app.ui import server

        done = []
        for tid, path in sorted(server._tracks.items()):
            h = self._orig["file_hash"](path)
            if pool.has(h):
                self.touched[h] = server._track_names.get(tid, "")
                continue
            ad = self.run_cache / "analysis"
            try:
                a = json.loads((ad / f"{h}.v5.json").read_text(encoding="utf-8"))
                v = json.loads((ad / f"{h}.vibe.json").read_text(encoding="utf-8"))
                e = json.loads((ad / f"{h}.energy.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            stems = server._stem_cache.get(tid)
            try:
                drops = server._safe_hook_drops(tid)
            except Exception:
                drops = []
            entry = assemble_entry(h, server._track_names.get(tid, path.stem), a, v, e, stems, "live", hook_drops=drops)
            pool.save(h, entry)
            self.touched[h] = entry["name"]
            self.fx["downloads"].setdefault("_hash_of", {})[tid] = h
            done.append(h)
        return done

    # ---- events ---------------------------------------------------------------------
    def _emit(self, kind: str, **fields) -> None:
        t = round(self.vclock(), 3)
        ev = {"t": 1_790_000_000.0 + t, "at": time.strftime("%H:%M:%S", time.gmtime(t)), "kind": str(kind)[:40]}
        for k, v in fields.items():
            ev[k] = v if isinstance(v, (int, float, bool, type(None), str)) else json.loads(json.dumps(v, default=str))
        self.events.append(ev)

    # ---- LLM ------------------------------------------------------------------------
    def _llm_key(self, kind: str) -> tuple:
        """(fixture key, call number). The key names the situation (song index, phase, the song
        playing / the pair, k-th call there) so a reply is only ever replayed into it."""
        c = self.ctx
        base = f"{c.get('song_index', 0)}|{kind}|{c.get('current', '')}"
        n = self._llm_n.get(base, 0)
        self._llm_n[base] = n + 1
        return f"{base}|{n}", n

    def _chat_call(self, system, user, temperature, timeout, model, max_tokens) -> str:
        kind = self.ctx.get("phase") or "suggest"
        key, n_call = self._llm_key(kind)
        self.last_llm_key = key
        ctx = dict(self.ctx, n_call=n_call)
        if self.mode == "replay":
            for e in self.fx["llm"]:
                if e["key"] == key and not e.get("_used"):
                    e["_used"] = True
                    return e["reply"]
            self.misses.append({"what": "llm", "key": key})
            if self.llm is None:
                self.fatal = WorldError(f"replay miss: no recorded model reply for {key} and no fallback model")
                raise self.fatal
            return self.llm.reply(kind, system, user, ctx)
        if self.mode == "library":
            if self.llm is None:
                self.fatal = WorldError("library world needs a StubLLM")
                raise self.fatal
            reply = self.llm.reply(kind, system, user, ctx)
        else:
            t0 = time.monotonic()
            try:
                reply = self._orig_chat(system, user, temperature, timeout, model, max_tokens)
            except Exception as exc:
                self.fatal = WorldError(f"the LLM is unreachable ({type(exc).__name__}: {exc}); "
                                        "start the model server or use --replay")
                raise self.fatal from exc
            self.fx.setdefault("llm_elapsed", {})[key] = round(time.monotonic() - t0, 2)
        if self.record:
            self.fx["llm"].append({"key": key, "kind": kind, "reply": reply})
        return reply

    # ---- YouTube edges ----------------------------------------------------------------
    def _throttle(self) -> None:
        """live: never hammer YouTube; yt_guard's own cooldown still applies underneath."""
        wait = self.caps.min_interval_s - (time.monotonic() - self._last_net)
        if wait > 0:
            time.sleep(wait)
        self._last_net = time.monotonic()

    def _lib_match(self, text: str):
        """Best pool / library track for a free-text query ("Artist - Title"), or None."""
        want = set(_words(text))
        if not want:
            return None
        best, best_s = None, 0.0
        for h, nm in sorted(self._catalog_names().items()):
            have = set(_words(nm))
            if not have:
                continue
            s = len(want & have) / max(1, len(want | have))
            if s > best_s:
                best, best_s = (h, nm), s
        return best if best_s >= 0.5 else None

    def _catalog_names(self) -> dict:
        if self.mode == "library" and self.library is not None:
            return {t.hash: t.name for t in self.library.tracks()}
        return dict(self.pool.names())

    def _search_songs(self, query: str, limit: int = 8):
        if self.mode == "replay":
            hit = self.fx["search"].get(query)
            if hit is not None:
                return hit
            self.misses.append({"what": "search", "key": query})
            m = self._lib_match(query)
            return [{"title": m[1], "url": f"sim://{m[0]}", "duration": 0}] if m else []
        if self.mode == "library":
            m = self._lib_match(query)
            res = [{"title": m[1], "url": f"sim://{m[0]}", "duration": 0}] if m else []
        else:
            self._throttle()
            res = self._orig_search(query, limit)
        if self.record:
            self.fx["search"][query] = res
        return res

    def _verify_song(self, artist: str, title: str):
        key = f"{artist}|{title}"
        if self.mode == "replay":
            if key in self.fx["verify"]:
                return self.fx["verify"][key]
            self.misses.append({"what": "verify", "key": key})
            return None if self._lib_match(f"{artist} {title}") is None else True
        if self.mode == "library":
            res = self._lib_match(f"{artist} {title}") is not None
        else:
            self._throttle()
            res = self._orig_verify(artist, title)
        if self.record:
            self.fx["verify"][key] = res
        return res

    def _song_views(self, name: str):
        if self.mode == "replay":
            return self.fx["views"].get(name)
        if self.mode == "library":
            res = None
        else:
            self._throttle()
            res = self._orig_views(name)
        if self.record:
            self.fx["views"][name] = res
        return res

    def _download_to_dir(self, url: str, output_dir: Path, progress=None):
        """Same contract as download_service.download_to_dir: files written into output_dir."""
        output_dir = Path(output_dir)
        if self.downloads >= self.caps.max_downloads:
            raise WorldError(f"download cap reached ({self.caps.max_downloads} per run)")
        if self.mode == "live":
            self._throttle()
            paths = self._orig_download(url, output_dir, progress)
            self.downloads += len(paths)
            if self.record:
                from app.music_brain.analyzer import _file_hash

                self.fx["downloads"][url] = [{"name": p.stem, "hash": self._orig["file_hash"](p), "suffix": p.suffix}
                                             for p in paths]
            return paths
        # replay / library: a placeholder file per recorded download
        if self.mode == "replay" and url in self.fx["downloads"]:
            found = self.fx["downloads"][url]
        else:
            if self.mode == "replay":
                self.misses.append({"what": "download", "key": url})
            if url.startswith("sim://"):
                h = url[len("sim://"):]
                nm = self._catalog_names().get(h)
                m = (h, nm) if nm else None
            else:
                m = self._lib_match(re.sub(r"^\w*search\d*:", "", url))
            if not m:
                raise WorldError(f"nothing downloadable for {url!r}")
            found = [{"name": m[1], "hash": m[0], "suffix": ".mp3"}]
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for f in found:
            p = output_dir / f"{f['name']}{f.get('suffix', '.mp3')}"
            p.write_bytes(marker_bytes(f["hash"]))
            paths.append(p)
            self.downloads += 1
        if self.record and self.mode == "library":
            self.fx["downloads"][url] = found
        return paths

    # ---- stems / registration -------------------------------------------------------------
    def _queue_stems(self, track_id: str, urgent: bool = True) -> bool:
        """server._queue_stems: a song was registered. Replay / library: install its frozen
        analysis, vibe, energy, vocal regions and stem state. Live: separate now (capped)."""
        from app.ui import server

        path = server._tracks.get(track_id)
        if path is None:
            return False
        h = marker_hash(path)
        if h is None:                      # a real file (live)
            return self._register_live(track_id, path)
        entry = self._entry_for(h)
        self._install_entry(track_id, path, entry)
        return True

    def _entry_for(self, h: str) -> dict:
        entry = self.pool.load(h)
        if entry is None and self.mode == "library" and self.library is not None:
            t = self.library.by_hash(h)
            if t is not None:
                self.library.build_entry(t, self.pool, self.run_cache)
                entry = self.pool.load(h)        # from disk: the same rounded numbers a replay reads
        if entry is None:
            raise WorldError(f"no fixture data for audio {h[:12]} (pool {self.pool.root})")
        return entry

    def _install_entry(self, track_id: str, path: Path, entry: dict) -> None:
        from app.ui import server

        h = entry["hash"]
        a = self.run_cache / "analysis"
        a.mkdir(parents=True, exist_ok=True)
        for suffix, key in (("v5.json", "analysis"), ("vibe.json", "vibe"), ("energy.json", "energy")):
            (a / f"{h}.{suffix}").write_text(json.dumps(entry[key]), encoding="utf-8")
        server._vocal_regions[track_id] = [list(r) for r in entry.get("vocals") or []]
        if entry.get("stems"):
            server._stem_cache[track_id] = {n: f"sim://stems/{h}/{n}" for n in ("drums", "bass", "vocals", "other")}
        self._tracks[track_id] = entry
        self.touched[h] = entry.get("name", "")

    def _register_live(self, track_id: str, path: Path) -> bool:
        """live: real Demucs (synchronous, the run waits for it) then read the curves back."""
        from app.music_brain import stem_service
        from app.ui import server

        try:
            res = stem_service.separate(path)
        except Exception as exc:
            raise WorldError(f"stem separation failed for {path.name}: {exc}") from exc
        server._stem_cache[track_id] = dict(res.stems)
        try:
            server._cached_vocal_regions(track_id)
        except Exception:
            pass
        return True

    def register_seed(self, name: str, audio_hash: Optional[str] = None, audio_path: Optional[Path] = None) -> str:
        """Put the seed track in the run's library and return its track id."""
        from app.ui import server

        up = server.UPLOAD_DIR
        up.mkdir(parents=True, exist_ok=True)
        if audio_path is not None:                               # live: the real file, copied
            data = Path(audio_path).read_bytes()
            import hashlib

            tid = hashlib.sha256(data).hexdigest()[:16]
            dest = up / f"{tid}{Path(audio_path).suffix}"
            if not dest.exists():
                dest.write_bytes(data)
        else:                                                     # replay / library: a placeholder
            import hashlib

            data = marker_bytes(audio_hash)
            tid = hashlib.sha256(data).hexdigest()[:16]
            dest = up / f"{tid}.mp3"
            dest.write_bytes(data)
        server._tracks[tid] = dest
        server._remember_track_name(tid, name)
        self._queue_stems(tid)
        return tid

    # ---- energy ------------------------------------------------------------------------
    def _library_raws(self) -> list:
        if self.mode == "replay":
            return list(self.fx.get("library_raws") or [])
        raws = self.library.library_raws() if self.library is not None else []
        self.fx["library_raws"] = raws
        return raws

    # ---- live originals -----------------------------------------------------------------
    def bind_live(self) -> None:
        """live mode: remember the real functions before install() replaces them."""
        from app.ui import autopilot_service as svc
        from app.ui import download_service as dl

        self._orig_chat = svc._chat_call
        self._orig_search, self._orig_verify = dl.search_songs, dl.verify_song
        self._orig_views, self._orig_download = dl.song_views, dl.download_to_dir

    # ---- recording -----------------------------------------------------------------------
    def save_fixture(self, seed_track: Optional[dict], run_meta: dict) -> Path:
        """Write app/sim/fixtures/<name>/run.json (+ the shared frozen files once)."""
        if not self.record:
            raise WorldError("save_fixture on a world that is not recording")
        for e in self.fx["llm"]:
            e.pop("_used", None)
        self.fx["seed_track"] = seed_track
        self.fx["pool"] = sorted(self.touched)
        self.fx.update(run_meta)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "run.json").write_text(
            json.dumps(round_floats(self.fx), indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        if self.library is not None:
            SHARED_DIR.mkdir(parents=True, exist_ok=True)
            for fn, text in self.library.shared_files().items():
                if not (SHARED_DIR / fn).exists():
                    (SHARED_DIR / fn).write_text(text, encoding="utf-8")
        return self.dir / "run.json"
