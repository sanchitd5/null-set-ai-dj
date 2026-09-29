"""The world outside the decision code: model, YouTube, downloads, stems, caches.

The virtual set runs the REAL server code (app.ui.server, autopilot_service, download
routes, analysis, matcher ...) through a test client. The edges that touch the network or a
GPU are the ports of `app.ui.engine.Engine` (host + AI backend): the World fills them
(simhost.py builds SimHost / SimAI over this class) and installs the Engine. Nothing is
monkeypatched. Every edge goes through one of three backends:

  live     the real thing: the LLM server, YouTube search / download, Demucs, librosa.
           With `record=True` every answer is written to a fixture (app/sim/fixtures/<run>/).
  replay   answers come from a fixture, zero network / LLM / Demucs. Deterministic.
  library  offline: songs come from the local source library (DATA_DIR), the "model" is the
           seeded StubLLM. Used to build the first fixtures without a model server, and by
           pytest.

Replay is keyed by subject, not by prompt text: a suggestion by the song playing, a plan by
its tempo / key pair, an ear call by its loop. A rule change that rewords a prompt still gets
the reply the model gave for that situation (counted as `drift`); only a call with no recorded
reply is a `miss` (answered by the StubLLM, seeded by subject). The report shows both counts so
a run that left its recording is never mistaken for one that followed it.
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

from app.sim.pool import FIXTURES_DIR, SHARED_DIR, Pool, round_floats
from app.sim.stubllm import LLM, StubLLM, split_name
from app.sim.synth import read_tag

FIXTURE_VERSION = 1
SHARED_FILES = ("learned_techniques.json", "set_memory.json")   # frozen stores every run of a panel starts from
_WORD = re.compile(r"[a-z0-9]+")


def reply_quality(kind: str, reply: str) -> str:
    """ok | empty | invalid: what a model reply is worth, for the scorer. A suggestion with no picks, a plan
    that is "{}" or a reply that is not a JSON object is the model failing the task (the sim then shows the
    rules coping, which is real but is not a fair test of them)."""
    text = (reply or "").strip()
    if not text:
        return "empty"
    if kind not in ("suggest", "lookahead", "plan", "ear", "audition"):
        return "ok"
    from app.ui.autopilot_service import _extract_json

    try:
        d = _extract_json(text)
    except Exception:
        return "invalid"
    if not isinstance(d, dict):
        return "invalid"
    if kind in ("suggest", "lookahead"):
        return "ok" if d.get("suggestions") else "empty"
    if kind in ("ear", "audition"):
        return "ok"
    return "ok" if d else "empty"


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
        self._vt = 0.0
        self.events: list = []
        self.misses: list = []               # replay answers that had to leave the recording
        self.downloads = 0
        self.fx = self._blank_fixture()
        if mode == "replay":
            self.fx = json.loads((self.dir / "run.json").read_text(encoding="utf-8"))
        self._llm_n: dict = {}               # subject (kind + song / tempo pair) -> calls so far
        self._tracks: dict = {}              # track id -> pool entry (hash, name) for tracks in this run
        self.llm_calls: list = []            # every model call of the run: kind, quality, latency
        self._req_llm_s = 0.0
        self.drift: list = []                # replay answers recovered from the recording for the same subject
        self._last_net = 0.0
        self.last_llm_key = ""
        self._synth: dict = {}               # audio hash -> {mix, stems} synthesised files of this run
        from collections import Counter

        self.prompt_flags: Counter = Counter()   # what the prompts the model saw contained (set memory, energy notes)
        self.fatal: Optional[WorldError] = None     # set when a needed dependency is missing: the run stops
        self.seed_track: Optional[dict] = None
        self.steps: list = []                # song_log steps captured with virtual time
        self.touched: dict = {}              # hash -> name of every pool entry this run used (record)
        self._stems_state: dict = {}         # track id -> "pending" | "done": the modelled separation queue (replay / library)
        self._stems_hashes: set = set()      # audio hashes already separated this run (cache hits)
        self._sep_q: list = []               # serial separation worker: {tid, hash, enq, start, done}
        self._sep_free_at = 0.0
        self.jobs: list = []                 # finished separations: {kind, hash, start, done}
        self._tempo: dict = {}               # tempo set key -> {key, enq, start, done}
        self._tempo_order: list = []         # serial key-lock worker, first asked first served

    def _blank_fixture(self) -> dict:
        return {"version": FIXTURE_VERSION, "name": self.name, "source": self.mode, "seed": self.seed,
                "llm": [], "search": {}, "verify": {}, "views": {}, "downloads": {}, "library_raws": [],
                "seed_track": None, "pool": []}

    # ---- the engine ------------------------------------------------------------------
    def install(self) -> None:
        """Build the Engine (SimHost + SimAI) and install it. Call once, after AIDJ_CACHE_DIR points
        at run_cache. Nothing in the brain is patched: it finds its edges through the engine."""
        from app.sim.simhost import SimAI, SimHost
        from app.ui import autopilot_service as svc
        from app.ui import engine, server

        self._raws = self._library_raws()
        # retries are a rule, not a race against a wall clock
        self.engine = engine.Engine(SimHost(self), SimAI(self), engine.EngineConfig(suggest_budget_s=1e9, verify_timeout_s=120.0))
        engine.use(self.engine)
        # a fresh run never sees a song lookup cached by an earlier one
        svc._verify_cache.clear()
        svc._verify_inflight.clear()
        # session-scoped server state starts empty
        server._suggest_inflight.clear()
        server._set_memory = None
        server._pair_cache.clear()
        server._stem_cache.clear()
        server._stem_queue.clear()
        server._stem_backlog.clear()
        from app.ui import prerender

        prerender.reset()                       # a fresh pre-render scheduler for the fresh engine
        self._seed_shared_files()

    def uninstall(self) -> None:
        from app.ui import engine

        engine.use(None)

    def library_raws_frozen(self) -> list:
        return self._raws

    def _seed_shared_files(self) -> None:
        """The frozen learned_techniques.json (fixtures/_shared) goes into the run's private cache:
        before / after a rule change the run sees the same learned store."""
        for fn in SHARED_FILES:
            src = SHARED_DIR / fn
            if src.exists():                                           # frozen: every run of a panel starts from the same store
                (self.run_cache / fn).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            elif self.mode == "live" and self.library is not None:     # the first recording freezes the user's real store
                text = self.library.shared_files().get(fn)
                if text is not None:
                    (self.run_cache / fn).write_text(text, encoding="utf-8")

    def song_step(self, kind, track_id, **fields) -> None:
        self.steps.append({"t": round(1_790_000_000.0 + self.vclock(), 3), "track_id": track_id, "kind": kind,
                           "phase": fields.get("phase"), "decision": fields.get("decision"), "why": fields.get("why"),
                           "inputs": fields.get("inputs"), "result": fields.get("result")})

    # ---- synthetic audio (replay / library) ------------------------------------------------
    def stem_tag(self, path):
        """The tag of a synthetic stem file of this run, else None (then the real audio is read)."""
        if not str(path).startswith(str(self.run_cache / "synth")):
            return None
        tag = read_tag(Path(path))
        return tag if tag and tag.get("stem") in ("drums", "bass", "vocals", "other") else None

    def synth_for_path(self, path):
        tag = read_tag(Path(path))
        return self._synth.get(tag["hash"]) if tag and tag.get("hash") else None

    def track_entry(self, track_id: str):
        return self._tracks.get(track_id)

    def synth_vocals_stem(self, track_id: str) -> str:
        e = self._tracks.get(track_id)
        files = self._synth.get(e["hash"]) if e else None
        if not files or not files["stems"]:
            raise WorldError(f"no synthetic vocal stem for {track_id}")
        return files["stems"]["vocals"]

    def tag_keylock_render(self, p, key, name):
        """A key-locked render is real rubberband output; tag it so the audio graph knows the stem."""
        tagged = Path(p).with_name(f"{Path(p).stem}.sim.wav")
        if not tagged.exists():
            import numpy as np
            import soundfile as sf

            from app.sim.synth import write_wav

            x, sr = sf.read(str(p), always_2d=True, dtype="float32")
            write_wav(tagged, x[:, 0].astype(np.float64), sr, {"stem": name, "keylock": key})
        return tagged

    def pick_seed(self, rng) -> str:
        """The seeded random seed track, as the seed URL the console's START box takes
        ("ytmsearch:Artist - Title": the same download route a user's seed goes through).
        Replay: the fixture's seed; otherwise drawn from the source library."""
        if self.mode == "replay":
            return self._seed_url(self.fx["seed_track"]["name"])
        if self.library is None:
            raise WorldError("no source library to draw a seed track from (set DATA_DIR)")
        lib = self.library.tracks()
        if not lib:
            raise WorldError(f"the source library {self.library.data} has no usable tracks")
        t = lib[rng.randrange(len(lib))]
        self.seed_track = {"hash": t.hash, "name": t.name}
        return self._seed_url(t.name)

    @staticmethod
    def _seed_url(name: str) -> str:
        return f"ytmsearch:{name}"

    # ---- events ---------------------------------------------------------------------
    def set_time(self, t: float) -> None:
        """The console's virtual clock (seconds), stamped on each request it makes."""
        self._vt = t
        self._advance()

    def vclock(self) -> float:
        return self._vt

    def emit(self, kind: str, **fields) -> None:
        t = round(self.vclock(), 3)
        ev = {"t": 1_790_000_000.0 + t, "at": time.strftime("%H:%M:%S", time.gmtime(t)), "kind": str(kind)[:40]}
        for k, v in fields.items():
            if k == "elapsed":                  # wall-clock seconds of a real computation: not part of the virtual run
                continue
            ev[k] = v if isinstance(v, (int, float, bool, type(None), str)) else json.loads(json.dumps(v, default=str))
        self.events.append(ev)

    # ---- LLM ------------------------------------------------------------------------
    _NOW = re.compile(r'NOW PLAYING: "(.*?)" by ')
    _PAIR = re.compile(r"CURRENT song: ([\d.]+) BPM, key (\w+)\. NEXT song: ([\d.]+) BPM, key (\w+)\.")

    def _llm_call_info(self, system: str, user: str) -> dict:
        """What a model call is, read off the call itself (the console drives it, the sim does not
        announce it): its kind (suggest / lookahead by the gate's priority, plan, ear), the
        signature of the exact prompt, a STABLE key (what the call is about, not how the prompt is
        worded: the song playing for a suggestion, the tempo / key pair for a plan), the song playing,
        and which call about that subject this is."""
        import hashlib

        from app.ui.llm_gate import gate

        prio = None
        try:
            snap = gate.snapshot()
            prio = (snap.get("in_flight") or {}).get("priority") if isinstance(snap.get("in_flight"), dict) else snap.get("in_flight")
        except Exception:
            pass
        m = self._NOW.search(user)
        cur = m.group(1) if m else ""
        if "NOW PLAYING" in user:
            kind = "lookahead" if str(prio).lower().startswith("look") else "suggest"
            stable = f"{kind}|{cur}"
        elif user.startswith("CURRENT song:"):
            kind = "plan"
            pm = self._PAIR.match(user)
            stable = "plan|" + "|".join(pm.groups()) if pm else "plan"
        else:
            kind = str(prio or "other").lower()
            stable = kind
        if kind in ("suggest", "lookahead"):
            m2 = re.search(r"are exempt\): (.*)", user)
            if m2 and m2.group(1).strip().lower() != "none":
                self.prompt_flags["earlier_sets"] += 1                     # set memory offered earlier sets' songs
            for phrase, key in (("at peak energy for a while", "dip"), ("near its end - one track that calls back", "callback"),
                                ("recurring hook", "reprise")):
                if phrase in user:
                    self.prompt_flags["energy_note"] += 1
                    self.prompt_flags[f"energy_note_{key}"] += 1
        sig = hashlib.sha1(f"{system}\n{user}".encode("utf-8")).hexdigest()[:16]
        n = self._llm_n.get(stable, 0)               # the n-th call ABOUT this subject: unaffected by other calls
        self._llm_n[stable] = n + 1
        return {"kind": kind, "sig": sig, "stable": stable, "cur": cur, "ord": n}

    def _replayed(self, kind: str, sig: str, stable: str, what: str, cur: str = ""):
        """The recorded entry for this call, or None. Exact prompt first; a drifted prompt (a rule
        changed what the model is asked) gets the next unused entry recorded for the same subject
        (`drift`, harmless: it is the reply the model gave for this very situation); only a call
        with no recorded reply at all is a `miss` (answered by the fallback model)."""
        for e in self.fx["llm"]:
            if e.get("sig") == sig and not e.get("_used"):
                e["_used"] = True
                return e
        for e in self.fx["llm"]:
            if e["kind"] == kind and (e.get("stable") or f"{e['kind']}|{e.get('cur', '')}") == stable and not e.get("_used"):
                e["_used"] = True
                self.drift.append({"what": what, "key": stable})
                return e
        self.misses.append({"what": what, "key": stable, "cur": cur})
        return None

    def _model_call(self, info: dict, live, offline) -> str:
        """One model call, whichever model answers it.
        live     `live()` is the app's own client (AIBackend, the real server); its wall-clock latency and reply
                 are what a recording keeps.
        replay   the recorded reply, and the recorded latency (the console's virtual clock waits as long as the
                 real model did); a call with no recording is answered by `offline()` (the seeded stub) and is a miss.
        library  `offline()`.
        Every call is noted for the scorer (empty / invalid replies) and its latency is added to the request in
        flight (the console sees it as the round trip)."""
        kind, latency = info["kind"], None
        if self.mode == "replay":
            e = self._replayed(kind, info["sig"], info["stable"], kind, info["cur"])
            if e is not None:
                reply, latency = e["reply"], e.get("latency_s")
            else:
                if offline is None:
                    self.fatal = WorldError(f"replay miss: no recorded model reply for {info['stable']} and no fallback model")
                    raise self.fatal
                reply = offline()
        elif self.mode == "library":
            if offline is None:
                self.fatal = WorldError("library world needs a StubLLM")
                raise self.fatal
            reply = offline()
        else:
            t0 = time.monotonic()
            try:
                reply = live()
            except Exception as exc:
                if kind in ("ear", "audition"):          # optional model: the rules answer, as in the live app
                    self.fx["ear_errors"] = self.fx.get("ear_errors", 0) + 1
                    raise
                self.fatal = WorldError(f"the LLM is unreachable ({type(exc).__name__}: {exc}); "
                                        "start the model server or use --replay")
                raise self.fatal from exc
            latency = round(time.monotonic() - t0, 2)
        quality = reply_quality(kind, reply)
        self.llm_calls.append({"kind": kind, "cur": info["cur"], "quality": quality, "latency_s": latency})
        if latency is not None:
            self._req_llm_s += latency
        if self.record and self.mode != "replay":
            self.fx["llm"].append({"kind": kind, "sig": info["sig"], "stable": info["stable"], "cur": info["cur"],
                                   "ord": info["ord"], "reply": reply, "latency_s": latency, "quality": quality})
        return reply

    def chat(self, system, user, temperature, timeout, model, max_tokens, real) -> str:
        """The model behind AIBackend.chat (suggest, look-ahead, plan, set-learning review)."""
        info = self._llm_call_info(system, user)
        self.last_llm_key = f"{info['kind']}|{info['sig']}"
        ctx = {"phase": info["kind"], "n_call": info["ord"], "n_picks": 5, "current": info["cur"],
               "song_index": len(self.ctx.get("history", [])),
               **{k: v for k, v in self.ctx.items() if k in ("history", "avoid", "queue", "current_meta")}}
        offline = (lambda: self.llm.reply(info["kind"], system, user, ctx)) if self.llm is not None else None
        return self._model_call(info, lambda: real(system, user, temperature, timeout, model, max_tokens), offline)

    def ear(self, cfg: dict, wav: bytes, metrics: dict, real) -> str:
        """The audio model behind AIBackend.ear (the live ear's Omni call). Without a recording "{}" is
        the offline answer: live_ear.validate turns it into its rule decision, the fallback the live app
        takes when the model is busy."""
        import hashlib

        from app.ui import live_ear

        sig = hashlib.sha1((live_ear._user_text(metrics) + "|" + hashlib.sha1(wav).hexdigest()[:12]).encode("utf-8")).hexdigest()[:16]
        info = {"kind": "ear", "sig": sig, "stable": f"ear|{metrics.get('deck')}|{bool(metrics.get('precheck'))}", "cur": "", "ord": 0}
        return self._model_call(info, lambda: real(cfg, wav, metrics), lambda: "{}")

    def audition(self, system: str, wav: bytes, text: str, real) -> str:
        """The audio model behind AIBackend.audition (the silent ear rating a rendered merge)."""
        import hashlib

        sig = hashlib.sha1((text + "|" + hashlib.sha1(wav).hexdigest()[:12]).encode("utf-8")).hexdigest()[:16]
        info = {"kind": "audition", "sig": sig, "stable": f"audition|{text[:80]}", "cur": "", "ord": 0}
        return self._model_call(info, lambda: real(system, wav, text), lambda: "{}")

    # ---- per-request model latency ---------------------------------------------------------
    def begin_request(self) -> None:
        self._req_llm_s = 0.0

    def end_request(self) -> Optional[float]:
        """Seconds the model spent on the request just served (None when it made no call): the console's
        transport waits that long on its virtual clock instead of a typical figure."""
        s, self._req_llm_s = self._req_llm_s, 0.0
        return s or None

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
        if self.mode in ("library", "live") and self.library is not None:
            return {t.hash: t.name for t in self.library.tracks()}
        return dict(self.pool.names())

    def search_songs(self, query: str, limit: int, real):
        if self.mode == "replay":
            hit = self.fx["search"].get(query)
            if isinstance(hit, dict) and "error" in hit:
                raise RuntimeError(hit["error"])
            if hit is not None:
                return hit
            self.misses.append({"what": "search", "key": query})
            m = self._lib_match(query)
            return [{"title": m[1], "url": f"sim://{m[0]}", "duration": 0}] if m else []
        if self.mode == "library":
            m = self._lib_match(query)
            res = [{"title": m[1], "url": f"sim://{m[0]}", "duration": 0}] if m else []
        elif self._lib_match(query) is not None:            # live: a song the library has costs no network
            m = self._lib_match(query)
            res = [{"title": m[1], "url": f"sim://{m[0]}", "duration": 0}]
        else:
            self._throttle()
            res = self._recorded_call(self.fx["search"], query, lambda: real(query, limit))
        if self.record:
            self.fx["search"][query] = res
        return res

    def verify_song(self, artist: str, title: str, real):
        key = f"{artist}|{title}"
        if self.mode == "replay":
            if key in self.fx["verify"]:
                v = self.fx["verify"][key]
                if isinstance(v, dict) and "error" in v:
                    raise RuntimeError(v["error"])
                return v
            # A lookup the recording never finished (the live app's VERIFY_TIMEOUT_S is wall-clock: slow or queued
            # lookups are cancelled and the pick kept as "unknown") is unknown again, exactly as it was live.
            self.drift.append({"what": "verify", "key": key})
            return None if self._lib_match(f"{artist} {title}") is None else True
        if self.mode == "library":
            res = self._lib_match(f"{artist} {title}") is not None
        elif self._lib_match(f"{artist} {title}") is not None:      # in the library: real by definition
            res = True
        else:
            self._throttle()
            res = self._recorded_call(self.fx["verify"], key, lambda: real(artist, title))
        if self.record:
            self.fx["verify"][key] = res
        return res

    def song_views(self, name: str, real):
        if self.mode == "replay":
            v = self.fx["views"].get(name)
            if isinstance(v, dict) and "error" in v:
                raise RuntimeError(v["error"])
            return v
        if self.mode == "library":
            res = None
        else:
            self._throttle()
            res = self._recorded_call(self.fx["views"], name, lambda: real(name))
        if self.record:
            self.fx["views"][name] = res
        return res

    def _recorded_call(self, store: dict, key: str, call):
        """A live edge call. When recording, a failure is kept as {"error": text} (a download that finds no studio
        track is part of the set: replay must fail the same way) and re-raised."""
        try:
            return call()
        except Exception as exc:
            if self.record:
                store[key] = {"error": f"{type(exc).__name__}: {exc}"[:300] if not str(exc) else str(exc)[:300]}
            raise

    def lrclib_search(self, artist: str, track: str, real):
        """Lyrics candidates (LRCLIB). live: the real service, recorded (top candidates only); replay: the
        recording, else none; library: none."""
        key = f"{artist}|{track}"
        if self.mode == "replay":
            hit = self.fx.get("lyrics", {}).get(key)
            if hit is None:
                self.misses.append({"what": "lyrics", "key": key})
            return hit or []
        if self.mode == "library":
            return []
        self._throttle()
        try:
            res = real(artist, track)
        except Exception:
            if self.record:
                self.fx.setdefault("lyrics", {})[key] = []      # lyrics.fetch treats a failure as "no lyrics"
            raise
        if self.record:
            self.fx.setdefault("lyrics", {})[key] = res[:6]
        return res

    def download_to_dir(self, url: str, output_dir: Path, progress, real):
        """Same contract as download_service.download_to_dir: files written into output_dir."""
        output_dir = Path(output_dir)
        # the console is driven by the song's GRAPH data (analysis, energy, stem envelopes), never by decoded
        # audio: what it loads is synthesised from the pool entry (synth.py). A song the library has is served
        # from its cached analysis / stems; one it has not is downloaded for real and only analysed (no Demucs).
        m = None
        if self.mode == "live":
            m = self._library_hit(url)
            if m is None:
                return self._download_and_analyse(url, output_dir, progress, real)
        if self.mode == "replay" and url in self.fx["downloads"]:
            found = self.fx["downloads"][url]
            if isinstance(found, dict) and "error" in found:       # the download failed when it was recorded
                raise RuntimeError(found["error"])
        else:
            if self.mode == "replay":
                self.misses.append({"what": "download", "key": url})
            if m is None:
                m = self._library_hit(url)
            if not m:
                raise WorldError(f"nothing downloadable for {url!r}")
            found = [{"name": m[1], "hash": m[0], "suffix": ".wav"}]
        from app.sim.synth import synth_track

        output_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for f in found:
            entry = self.entry_for(f["hash"])
            files = self._synth.get(f["hash"])
            if files is None:
                files = self._synth[f["hash"]] = synth_track(entry, self.run_cache / "synth" / f["hash"], name="mix")
            p = output_dir / f"{f['name']}.wav"
            shutil.copyfile(files["mix"], p)
            paths.append(p)
        if self.record and self.mode in ("library", "live"):
            self.fx["downloads"][url] = found
        for f in found:
            self.touched[f["hash"]] = f["name"]
        return paths

    def _library_hit(self, url: str):
        """(hash, name) of the library song a download url stands for, else None."""
        if url.startswith("sim://"):
            h = url[len("sim://"):]
            nm = self._catalog_names().get(h)
            return (h, nm) if nm else None
        return self._lib_match(re.sub(r"^\w*search\d*:", "", url))

    def _download_and_analyse(self, url: str, output_dir: Path, progress, real):
        """live, a song the library does not have: the real YouTube download, then only what the console's graph
        needs (analysis, vibe, energy: librosa, seconds). No Demucs: the entry has no stem lanes (`stems` None), the
        console sees a track whose separation has not finished. The audio it is handed is synthesised from the entry."""
        from app.music_brain import analyzer, energy as en, vibe
        from app.music_brain.analyzer import _file_hash
        from app.sim.pool import assemble_entry
        from app.sim.synth import synth_track

        self._throttle()

        def fetch():
            if self.downloads >= self.caps.max_downloads:          # the cap protects YouTube (library songs cost none)
                raise RuntimeError(f"download cap reached ({self.caps.max_downloads} YouTube downloads per run)")
            return real(url, output_dir, progress)

        paths = self._recorded_call(self.fx["downloads"], url, fetch)
        out, found = [], []
        for p in paths:
            h = _file_hash(p)
            entry = self.pool.load(h)
            if entry is None:
                try:
                    a = analyzer.analyze(p)
                    ad = self.run_cache / "analysis"
                    vibe.analyze_vibe(p)
                    en.measure(p, float(a.bpm or 0))
                    entry = assemble_entry(h, p.stem, json.loads((ad / f"{h}.v5.json").read_text(encoding="utf-8")),
                                           json.loads((ad / f"{h}.vibe.json").read_text(encoding="utf-8")),
                                           json.loads((ad / f"{h}.energy.json").read_text(encoding="utf-8")), None, "live")
                except Exception as exc:
                    raise RuntimeError(f"analysis failed for {p.name}: {exc}") from exc
                self.pool.save(h, entry)
                entry = self.pool.load(h)                    # from disk: the same rounded numbers a replay reads
            files = self._synth.get(h) or self._synth.setdefault(h, synth_track(entry, self.run_cache / "synth" / h, name="mix"))
            q = Path(output_dir) / f"{p.stem}.wav"
            shutil.copyfile(files["mix"], q)
            if q != p:
                p.unlink(missing_ok=True)
            out.append(q)
            found.append({"name": p.stem, "hash": h, "suffix": ".wav"})
            self.downloads += 1
            self.touched[h] = p.stem
        if self.record:
            self.fx["downloads"][url] = found
        return out

    # ---- stems / registration -------------------------------------------------------------
    def queue_stems(self, track_id: str) -> bool:
        """server._queue_stems: a song was registered. Replay / library: install its frozen
        analysis, vibe, energy and vocal regions now and its STEMS after the modelled separation
        (SEP_S on one serial worker, virtual time); a song separated earlier in the run is a cache
        hit. Live: separate now (capped)."""
        from app.ui import server

        path = server._tracks.get(track_id)
        if path is None:
            return False
        tag = read_tag(Path(path))
        if not tag or not tag.get("hash"):
            raise WorldError(f"{Path(path).name}: real audio reached the console (the sim serves synthesised audio only)")
        entry = self.entry_for(tag["hash"])
        self._install_entry(track_id, path, entry)
        self._enqueue_stems(track_id, entry)
        return True

    # ---- modelled job latency (virtual time) --------------------------------------------------
    # One separation at a time and one key-locked render at a time, as the pre-render scheduler runs them.
    # SEP_S: median gap between consecutive finished separations of the real app (361 gaps under 200 s in
    # data/cache/stems, median 26 s, p10 7 s, p90 81 s). TEMPO_S: the keylock.py docstring's "~30 s to render
    # a 3-4 min song" (not measurable from the logs: UNVERIFIED). A repeat of a song already separated this
    # run is a cache hit (0 s), as is a tempo set already rendered.
    SEP_S = 26.0
    TEMPO_S = 30.0

    def _enqueue_stems(self, track_id: str, entry: dict) -> None:
        h = entry["hash"]
        st = self._stems_state.get(track_id)
        if st in ("pending", "done"):
            return
        if h in self._stems_hashes:                       # cache hit: the same audio was separated earlier in the run
            self._finish_stems(track_id, entry)
            return
        self._stems_state[track_id] = "pending"
        self._sep_q.append({"tid": track_id, "hash": h, "enq": self.vclock(), "start": None, "done": None})
        self._advance()

    def _finish_stems(self, track_id: str, entry: dict) -> None:
        from app.ui import server

        files = self._synth.get(entry["hash"])
        if files and files["stems"]:
            server._stem_cache[track_id] = dict(files["stems"])
        self._stems_state[track_id] = "done"
        self._stems_hashes.add(entry["hash"])

    def _schedule(self) -> None:
        free = self._sep_free_at
        for j in self._sep_q:
            j["start"] = max(free, j["enq"])
            j["done"] = j["start"] + self.SEP_S
            free = j["done"]

    def _advance(self) -> None:
        """Land every separation whose modelled time has passed (called on each request's virtual time)."""
        self._schedule()
        now = self._vt
        while self._sep_q and self._sep_q[0]["done"] <= now:
            j = self._sep_q.pop(0)
            self._sep_free_at = j["done"]
            self.jobs.append({"kind": "stems", "hash": j["hash"], "start": j["start"], "done": j["done"]})
            self._finish_stems(j["tid"], self._tracks[j["tid"]])
            self._schedule()

    def drop_stems(self, track_id: str) -> bool:
        """A separation queued and not yet started is cancelled (a running one finishes)."""
        self._advance()
        for j in list(self._sep_q):
            if j["tid"] == track_id and j["start"] > self._vt:
                self._sep_q.remove(j)
                self._stems_state.pop(track_id, None)
                self._schedule()
                return True
        return False

    def stems_running(self) -> bool:
        self._advance()
        return bool(self._sep_q and self._sep_q[0]["start"] <= self._vt)

    def tempo_gate(self, key: str) -> bool:
        """server.get_track_stems / prerender: a tempo set the real Rubber Band already wrote may be served only
        once its modelled render (TEMPO_S, serial, from the first time anyone asked) has passed."""
        j = self._tempo.get(key)
        if j is None:
            j = self._tempo[key] = {"key": key, "enq": self._vt}
            self._tempo_order.append(j)
            free = 0.0
            for x in self._tempo_order:
                x["start"] = max(free, x["enq"])
                x["done"] = x["start"] + self.TEMPO_S
                free = x["done"]
        return j["done"] <= self._vt

    def tempo_running(self) -> int:
        return sum(1 for j in self._tempo_order if j["start"] <= self._vt < j["done"])

    def prerender_report(self, played_hashes: set) -> dict:
        """Render seconds spent on songs that never played, and the most heavy jobs (separations + key-locked
        renders) running at once, from the modelled intervals. Informational metrics (scorer)."""
        self._advance()
        iv, wasted = [], 0.0
        for j in self.jobs:
            iv.append((j["start"], j["done"]))
            if j["hash"] not in played_hashes:
                wasted += j["done"] - j["start"]
        for j in self._tempo_order:
            if j["done"] > self._vt:
                continue                                    # never finished inside the run: not counted as spent
            iv.append((j["start"], j["done"]))
            prefix = j["key"][1:25]
            if not any(h.startswith(prefix) for h in played_hashes):
                wasted += j["done"] - j["start"]
        ev = sorted([(a, 1) for a, _ in iv] + [(b, -1) for _, b in iv], key=lambda x: (x[0], x[1]))
        cur = peak = 0
        for _, d in ev:
            cur += d
            peak = max(peak, cur)
        return {"wasted_render_seconds": round(wasted, 1), "max_concurrent_heavy_jobs": peak,
                "stems_jobs": len(self.jobs), "tempo_jobs": sum(1 for j in self._tempo_order if j["done"] <= self._vt)}

    def entry_for(self, h: str) -> dict:
        entry = self.pool.load(h)
        if entry is None and self.mode in ("library", "live") and self.library is not None:
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
        self._tracks[track_id] = entry                # the stems themselves land in _finish_stems (modelled separation time)
        self.touched[h] = entry.get("name", "")

    # ---- energy ------------------------------------------------------------------------
    def _library_raws(self) -> list:
        if self.mode == "replay":
            return list(self.fx.get("library_raws") or [])
        raws = self.library.library_raws() if self.library is not None else []
        self.fx["library_raws"] = raws
        return raws

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
