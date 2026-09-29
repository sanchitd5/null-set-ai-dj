"""Silent virtual DJ set: the real pipeline, no audio device, a virtual clock.

    python3 -m app.sim.virtual_set --replay demo-long-1 --out app/sim/out/run1
    python3 -m app.sim.virtual_set --record my-run --seed 7 --tracks 10 --mode long   # real LLM + YouTube
    python3 -m app.sim.virtual_set --record lib-1 --library --seed 1 --tracks 10      # offline, StubLLM

What runs for real (the same code the live console talks to):
  Python   the FastAPI app (app.ui.server) through a test client: /api/autopilot/suggest
           (autopilot_service: prompt, filters, artist spacing, energy / key / era / genre
           checks), /api/download (download_service), analysis, /api/match (recipe_matcher,
           vibe, energy), /api/autopilot/plan, /api/blend/plan, /api/layer/plan,
           /api/learned/pick (techniques), /api/library/lockable
  JS       autopilot.js core (energyStepOk, decideRecipe, playWindowFor, exit*, learnedRecipe),
           tempo-rule.js, dj-mind.js core (camelotScore, layerDecision), stem-moves.js core
           (fitStemBlend, stemBridgePlan, gates), through node (bridge.js)
What is glue (mirrors autopilot.js prepareTransition / evaluateCandidate ordering; keep in sync,
the README lists it): the round loop, the candidate gate order, the virtual clock.
What is NOT simulated: audio, riff over rap, song merge / mashup transitions (need the silent
ear), PEAK moves, the bridge ladder, the live ear, the live drums / sampler.

Time is virtual: latencies are the recorded ones (or fixed defaults), never wall clock.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.sim.world import WorldError

LATENCY_S = {"suggest": 9.0, "lookahead": 9.0, "plan": 8.0, "download": 25.0, "cached": 0.5, "match": 1.5, "blend": 1.0}
MAX_ROUNDS = 3
MAX_STALLS = 3
MODES = ("long", "quick", "hybrid")


@dataclass
class Trk:
    id: str
    name: str
    a: dict                      # /api/tracks/<id>/analysis
    level: Optional[int] = None  # measured energy 1-10 (energy.level)
    raw: Optional[float] = None
    stems: bool = False
    vocals: list = field(default_factory=list)
    curves: Optional[dict] = None
    famous: bool = False

    @property
    def bpm(self) -> float:
        return float(self.a.get("bpm") or 0)

    @property
    def key(self) -> str:
        return ((self.a.get("key") or {}).get("camelot")) or ""

    @property
    def duration(self) -> float:
        return float(self.a.get("duration") or 0)


class Sim:
    def __init__(self, world, js, client, seed: int, n_tracks: int, mode: str, occasion: str = ""):
        self.w, self.js, self.c = world, js, client
        self.seed, self.n_tracks, self.mode, self.occasion = seed, n_tracks, mode, occasion
        self.rng = random.Random(f"sim:{seed}")
        self.t = 0.0                       # virtual seconds since the set started
        self.tracks: dict = {}
        self.history: list = []            # display names, play order
        self.played_ids: list = []
        self.ready: list = []              # pre-downloaded candidates {id, name, ...}
        self.pair_rejects: dict = {}
        self.empty_streak = 0
        self.cur: Optional[Trk] = None
        self.entry_pos = 0.0
        self.t_entry = 0.0
        self.trans_total = 0.0
        self.current_energy = None         # the LLM's profile energy of the playing song
        self.since_layer = 99
        self.energies: list = []           # djMind.noteEnergy history (energy_note hints)
        self.callback_done = False
        self.reprise_asked = None
        self.energy_noted_for = None
        self.scheduled_name = None
        self.scheduled_track: Optional[Trk] = None
        self.recent_moves: list = []
        self.songs: list = []
        self.transitions: list = []
        self.preps: list = []
        self.counters = {"llm_suggest": 0, "candidates": 0, "downloads": 0, "download_failures": 0, "picks_empty": 0,
                         "stalls": 0, "reject_tempo": 0, "reject_vibe": 0, "reject_energy": 0, "reject_planfit": 0,
                         "reject_short": 0, "reject_repeat": 0, "reject_pair": 0, "reject_match": 0,
                         "plans_asked": 0, "plans_none": 0, "blend_dropped": 0, "layers": 0, "http_errors": 0}
        self.rejects: list = []
        self.consts = js.call("tempoRule.constants")

    # ---- helpers -----------------------------------------------------------------
    def log(self, kind: str, **kw) -> None:
        self.w._emit(kind, **kw)

    def pos(self) -> float:
        """A's position now: song seconds from the start, at native tempo (the tempo is home by now)."""
        return self.entry_pos + (self.t - self.t_entry)

    def _post(self, url: str, body: dict):
        r = self.c.post(url, json=body)
        if r.status_code >= 400:
            if self.w.fatal:                       # an unreachable model / replay hole: stop, never carry on with less
                raise self.w.fatal
            self.counters["http_errors"] += 1
            raise RuntimeError(f"{url} -> {r.status_code}: {str(r.json().get('detail') if r.headers.get('content-type', '').startswith('application/json') else r.text)[:200]}")
        return r.json()

    def _get(self, url: str, **params):
        r = self.c.get(url, params=params)
        if r.status_code >= 400:
            self.counters["http_errors"] += 1
            raise RuntimeError(f"{url} -> {r.status_code}")
        return r.json()

    def _lat(self, kind: str, key: Optional[str] = None) -> float:
        el = (self.w.fx.get("llm_elapsed") or {}).get(key or "")
        return float(el) if el is not None else LATENCY_S[kind]

    # ---- tracks -----------------------------------------------------------------------
    def load_track(self, tid: str) -> Trk:
        if tid in self.tracks:
            return self.tracks[tid]
        from app.music_brain import energy as en
        from app.ui import server

        a = self._get(f"/api/tracks/{tid}/analysis")
        path = server._tracks[tid]
        t = Trk(tid, server._track_names.get(tid) or path.stem, a)
        try:
            lv = en.level(path, t.bpm)
            t.level, t.raw = int(lv["level"]), float(lv["raw"])
        except Exception:
            pass
        entry = self.w._tracks.get(tid)
        t.vocals = [tuple(r) for r in (server._vocal_regions.get(tid) or a.get("vocal_active_regions") or [])]
        t.stems = server._stem_cache.get(tid) is not None
        t.curves = (entry or {}).get("stems") if entry else self.w.live_curves(tid)
        try:
            fame = self._get(f"/api/tracks/{tid}/fame")
            t.famous = bool(fame.get("famous"))
        except Exception:
            t.famous = False
        self.tracks[tid] = t
        return t

    def pick_seed(self) -> str:
        return self.w.pick_seed(self.rng)

    # ---- suggestions ---------------------------------------------------------------------
    def get_suggestions(self, base: Trk, avoid: list, lookahead: bool = False) -> list:
        set_pos = min(len(self.history) / 10, 1.0)
        note = hook = None
        if not lookahead:          # dj-mind.js nextEnergyNote: "dip" / "callback" / "reprise" hints
            r = self.js.call("sim.nextEnergyNote", self.energies, set_pos, self.callback_done, self.history, self.reprise_asked)
            note, hook, self.callback_done, self.reprise_asked = r["note"], r["hook"], r["callbackDone"], r["repriseAsked"]
        phase = "lookahead" if lookahead else "suggest"
        self.w.ctx = {"song_index": len(self.history), "phase": phase, "current": base.name, "n_picks": 5,
                      "history": list(self.history), "avoid": list(avoid[-6:]), "queue": self._queue_names(),
                      "current_meta": {"name": base.name, "bpm": base.bpm, "key": base.key, "level": base.level}}
        body = {"set_id": f"sim-{self.seed}", "track_id": base.id, "occasion": self.occasion or None,
                "history": self.history[-30:], "avoid": avoid[-6:], "queue": self._queue_names(),
                "set_position": set_pos, "set_mode": self.mode, "relaxed": False, "energy_note": note,
                "energy_hook": hook, "lookahead": lookahead, "variety_run": 0, "variety_genre": "",
                "tempo_target": None, "tempo_note": None, "elapsed_seconds": round(self.t, 1)}
        self.counters["llm_suggest"] += 1
        data = self._post("/api/autopilot/suggest", body)
        self.t += self._lat(phase, self.w.last_llm_key)
        if not lookahead:
            prof = data.get("current_profile") or {}
            try:
                e = float(prof.get("energy"))
                self.current_energy = e * 10 if e <= 1 else e
                if self.energy_noted_for != base.id:          # djMind.noteEnergy, once per song
                    self.energy_noted_for = base.id
                    self.energies.append(self.current_energy)
            except (TypeError, ValueError):
                pass
        return data.get("suggestions") or []

    def _queue_names(self) -> list:
        return [n for n in [getattr(self, "scheduled_name", None)] + [c["name"] for c in self.ready] if n][:3]

    # ---- downloads -------------------------------------------------------------------------
    def download_suggestion(self, s: dict) -> Optional[dict]:
        label = f"{s.get('artist')} — {s.get('title')}"
        cached = self.find_cached(s.get("artist", ""), s.get("title", ""))
        if cached:
            tid, name = cached
            self.t += LATENCY_S["cached"]
        else:
            self.counters["downloads"] += 1
            try:
                res = self._post("/api/download", {"url": s.get("search_query") or f"{s.get('artist')} {s.get('title')}"})
            except RuntimeError as exc:
                self.counters["download_failures"] += 1
                self.log("track", event="download_failed", song=label, error=str(exc)[:120])
                return None
            tracks = res.get("tracks") or []
            if not tracks:
                self.counters["download_failures"] += 1
                return None
            tid, name = tracks[0]["track_id"], tracks[0].get("display_name") or label
            self.t += LATENCY_S["download"]
        try:
            t = self.load_track(tid)
        except Exception as exc:
            self.counters["download_failures"] += 1
            self.log("track", event="load_failed", song=name, error=str(exc)[:120])
            return None
        return {"id": tid, "name": name, "bpm": t.bpm, "duration": t.duration, "suggestion": s, "keep": False}

    def find_cached(self, artist: str, title: str):
        a, ti = artist.lower(), title.lower()
        for tr in self._get("/api/tracks")["tracks"]:
            if tr.get("not_a_song"):
                continue
            dn = (tr.get("display_name") or "").lower()
            if a in dn and ti in dn:
                return tr["track_id"], tr.get("display_name")
        return None

    # ---- candidate evaluation (autopilot.js evaluateCandidate) ------------------------------------------
    def reject(self, cand: dict, why: str, keep: bool, key: str) -> bool:
        self.counters[key] = self.counters.get(key, 0) + 1
        cand["keep"] = keep
        self.rejects.append({"song_index": len(self.history), "to": cand["name"], "why": why, "gate": key})
        return False

    def try_candidate(self, cand: dict, force_jump: bool, allow_jump: bool) -> Optional[dict]:
        self.counters["candidates"] += 1
        try:
            return self.evaluate(cand, force_jump, allow_jump)
        except Exception as exc:
            self.counters["http_errors"] += 1
            self.rejects.append({"song_index": len(self.history), "to": cand["name"], "why": f"error: {str(exc)[:160]}", "gate": "error"})
            return None

    def evaluate(self, cand: dict, force_jump: bool, allow_jump: bool) -> Optional[dict]:
        cur = self.cur
        nxt = self.load_track(cand["id"])
        a_eff = cur.bpm
        # tempo gate: beat-match by pitch / key-locked stems, or (last round) a tempo jump
        if nxt.bpm and not self.js.call("autopilot.tempoLockableAt", a_eff, nxt.bpm, self.consts["PITCH_RANGE_PCT"] / 100):
            if not allow_jump:
                return self.reject(cand, f"{nxt.bpm:.0f} BPM cannot be beat-matched to {a_eff:.0f}", True, "reject_tempo") or None
        if nxt.id == cur.id or nxt.name in self.history:
            return self.reject(cand, "already played", False, "reject_repeat") or None
        min_secs = 180 if self.mode == "long" else 90
        if nxt.duration and nxt.duration < min_secs:
            return self.reject(cand, f"{nxt.duration:.0f}s is too short for a {self.mode.upper()} set", False, "reject_short") or None
        k = f"{cur.id}>{nxt.id}"
        known = self.pair_rejects.get(k)
        if known and (not force_jump or known["forced"]):          # autopilotCore.pairRejected
            return self.reject(cand, f"already checked: {known['why']}", True, "reject_pair") or None
        m = self._post("/api/match", {"track_a_id": cur.id, "track_b_id": nxt.id, "top_n": 1, "no_cuts": True})
        self.t += LATENCY_S["match"]
        cands = m.get("candidates") or []
        if not cands:
            return self.reject(cand, "no match candidates", False, "reject_match") or None
        candidate, vibe = dict(cands[0]), m.get("vibe") or {}
        if vibe.get("ok") is False:
            why = "; ".join(vibe.get("reasons") or []) or f"distance {vibe.get('distance')}"
            self.pair_rejects[k] = {"why": why, "forced": True}
            return self.reject(cand, f"vibe: {why}", True, "reject_vibe") or None
        ea, eb = vibe.get("energy_a"), vibe.get("energy_b")
        if isinstance(ea, (int, float)) and isinstance(eb, (int, float)):
            raw_a, raw_b = vibe.get("energy_raw_a"), vibe.get("energy_raw_b")
            verdict = self.js.call("autopilot.energyStepOk", ea, eb, {
                "relaxed": False, "songs": len(self.history), "force": force_jump,
                "rawDelta": (raw_b - raw_a) if isinstance(raw_a, (int, float)) and isinstance(raw_b, (int, float)) else None})
            if not verdict["ok"]:
                self.pair_rejects[k] = {"why": verdict["why"], "forced": force_jump}
                return self.reject(cand, verdict["why"], True, "reject_energy") or None
        # plan-before-pick: only a candidate whose transition is already smooth
        stems_both = cur.stems and nxt.stems
        tempo_stems = self._tempo_stems_bpm(a_eff, nxt, stems_both)
        fit = self.js.call("tempoRule.planFit", {"aEff": a_eff, "bBpm": nxt.bpm, "stemsBoth": stems_both, "tempoStemsBpm": tempo_stems})
        candidate["plannedFit"] = fit
        if not fit["smooth"] and not allow_jump:
            self.pair_rejects[k] = {"why": fit["why"], "forced": force_jump}
            return self.reject(cand, fit["why"], True, "reject_planfit") or None
        plan = self.request_mind_plan(cur, nxt, candidate)
        if plan and plan.get("candidate"):
            candidate = {**candidate, **plan["candidate"], "vibe": vibe}
        blend, min_exit = self.request_blend(cur, nxt, candidate)
        layer = self.request_layer(cur, nxt, candidate, plan) if blend else None
        if layer and not (layer["start"] >= self.pos() + 16):
            layer = None
        return self.schedule(cur, nxt, cand, candidate, vibe, blend, min_exit, layer, plan, force_jump)

    def _tempo_stems_bpm(self, a_eff: float, nxt: Trk, stems_both: bool):
        """The key-locked tempo stems scheduleTransition renders for a 2-8 % gap (their tempo), or None."""
        if not (stems_both and nxt.bpm and a_eff):
            return None
        m = min((1, 2, 0.5), key=lambda x: abs(a_eff / (nxt.bpm * x) - 1))
        gap = abs(a_eff / (nxt.bpm * m) - 1)
        return a_eff / m if 0.02 < gap <= self.consts["KEYLOCK_RANGE_PCT"] / 100 else None

    # ---- the AI plan / blend / layer requests ------------------------------------------------------------
    def play_window(self, score: float, cur: Trk) -> dict:
        famous = bool(cur.famous and cur.duration)
        return self.js.call("autopilot.playWindowFor", {
            "steering": "stay", "famous": famous, "rem": (cur.duration - self.entry_pos) if famous else 0,
            "mode": self.mode, "score": score, "energy": self.current_energy})

    def exit_window(self, score: float, cur: Trk) -> dict:
        w = self.play_window(score, cur)
        b = self.js.call("autopilot.exitBounds", {"w": w, "entryPos": self.entry_pos, "trackDur": cur.duration or None})
        return {"lo": b["lo"], "hi": b["hi"], "w": w, "trackEnd": b["trackEnd"]}

    def request_mind_plan(self, cur: Trk, nxt: Trk, candidate: dict) -> Optional[dict]:
        win = self.exit_window(candidate.get("score") or 50, cur)
        if not (win["hi"] > win["lo"]):
            return None
        self.counters["plans_asked"] += 1
        self.w.ctx = {"song_index": len(self.history), "phase": "plan", "current": f"{cur.name}>{nxt.name}",
                      "history": list(self.history)}
        body = {"track_a_id": cur.id, "track_b_id": nxt.id, "now": round(self.pos(), 2), "entry": self.entry_pos,
                "window_lo": win["lo"], "window_hi": win["hi"], "set_mode": self.mode,
                "set_position": min(len(self.history) / 10, 1.0), "recent_moves": self.recent_moves[-4:],
                "remix_used": [], "mashup_possible": False, "subdrop_last_track": False, "peak_moves": True,
                "big_moment_ok": False}
        try:
            plan = self._post("/api/autopilot/plan", body)
        except RuntimeError:
            self.counters["plans_none"] += 1
            return None
        self.t += LATENCY_S["plan"]
        if not plan.get("parsed"):
            self.counters["plans_none"] += 1
        return plan

    def request_blend(self, cur: Trk, nxt: Trk, candidate: dict):
        win = self.exit_window(candidate.get("score") or 50, cur)
        lo = max(win["lo"], self.pos() + 20)
        if not (win["hi"] > lo):
            return None, None
        body = {"a_id": cur.id, "b_id": nxt.id, "window_lo": lo, "window_hi": win["hi"], "a_bpm_effective": cur.bpm,
                "bars": 8 if self.mode == "quick" else 16, "a_entry": self.entry_pos}
        try:
            plan = self._post("/api/blend/plan", body)
        except RuntimeError:
            return None, None
        self.t += LATENCY_S["blend"]
        if not plan.get("ok"):
            return None, plan.get("min_exit")
        return plan, plan.get("min_exit")

    def request_layer(self, cur: Trk, nxt: Trk, candidate: dict, plan: Optional[dict]):
        ai = bool(plan and plan.get("layer"))
        base = {"steering": False, "peak": False, "energy": self.current_energy, "aiProposed": ai,
                "aiWhy": plan.get("layer_reason") if plan else None, "sinceLayer": self.since_layer}
        pre = self.js.call("djMind.layerDecision", {"ok": True, "keyScore": 1, "vocalClash": 0, "groove": True, **base})
        if not pre.get("layer"):
            return None
        win = self.exit_window(candidate.get("score") or 50, cur)
        lo = max(win["lo"], self.pos() + 20)
        if not (win["hi"] > lo):
            return None
        bars = self.js.call("djMind.layerBars", self.mode)
        body = {"a_id": cur.id, "b_id": nxt.id, "window_lo": lo, "window_hi": win["hi"], "a_bpm_effective": cur.bpm,
                "a_entry": self.entry_pos, "max_hold_bars": bars["maxHold"], "unwind_bars": bars["unwind"], "third_ids": []}
        try:
            lay = self._post("/api/layer/plan", body)
        except RuntimeError:
            return None
        dec = self.js.call("djMind.layerDecision", {"ok": lay.get("ok"), "why": "; ".join(lay.get("reasons") or []),
                                                    "keyScore": lay.get("key_score"), "vocalClash": lay.get("vocal_clash"),
                                                    "groove": lay.get("groove"), **base})
        if not dec.get("layer"):
            return None
        return {**lay, "source": dec.get("source"), "why": dec.get("why")}

    # ---- scheduleTransition ---------------------------------------------------------------------------------
    def schedule(self, cur: Trk, nxt: Trk, cand: dict, candidate: dict, vibe: dict, blend, min_exit, layer, plan, force_jump) -> dict:
        js = self.js
        a_eff = cur.bpm
        stems_both = cur.stems and nxt.stems
        key_score = js.call("djMind.camelotScore", cur.key, nxt.key) if cur.key and nxt.key else None
        tempo_stems = self._tempo_stems_bpm(a_eff, nxt, stems_both)
        dec = js.call("autopilot.decideRecipe", {
            "recipe": candidate.get("recipe") or "Blend", "blend": blend, "layer": bool(layer), "aStems": cur.stems,
            "bStems": nxt.stems, "aEff": a_eff, "bBpm": nxt.bpm, "tempoStemsBpm": tempo_stems, "keyScore": key_score,
            "mashupFits": False})
        b_time = float(candidate.get("b_time") or 0)
        if blend and not dec["dropLayer"]:
            b_time = float(blend["entry"])
        if dec["dropLayer"]:
            layer = None
        blend_final = dec["blend"]
        if blend and blend_final is None:
            self.counters["blend_dropped"] += 1
        recipe = dec["recipe"]
        if layer:
            b_time = float(layer["entry"])
            recipe = f"LAYER {layer['hold_bars']}+{layer['unwind_bars']} bars"
        score = candidate.get("score") or 50
        w = self.play_window(score, cur)
        bounds = js.call("autopilot.exitBounds", {"w": w, "entryPos": self.entry_pos, "trackDur": cur.duration or None})
        exit_at = js.call("autopilot.exitPick", {
            "lo": bounds["lo"], "hi": bounds["hi"], "trackEnd": bounds["trackEnd"], "layerStart": layer["start"] if layer else None,
            "hasBlend": bool(blend_final), "blendExit": blend_final.get("exit") if blend_final else None,
            "candidateATime": candidate.get("a_time"), "minExit": min_exit})
        phrase_s = 32 * 60 / (cur.bpm or 128)
        timing = js.call("autopilot.exitTiming", {"exitAt": exit_at, "nowPos": self.pos(), "phraseS": phrase_s, "w": w,
                                                  "oneSong": dec["oneSong"], "vocalShort": dec["vocalShort"], "peak": False})
        eff = timing["effectiveATime"]
        moved = 0
        if not layer:
            hp = js.call("autopilot.exitHighPush", {"t": eff, "phraseS": phrase_s, "bpm": cur.bpm or 128, "trackEnd": bounds["trackEnd"],
                                                    "energyTimes": cur.a.get("energy_times"), "energyCurve": cur.a.get("energy_curve")})
            eff, moved = hp["t"], hp["moved"]
        booked = recipe
        learned = None
        if not layer:
            try:
                pick = self._get("/api/learned/pick", a=cur.id, b=nxt.id, keylock="true").get("pick")
            except RuntimeError:
                pick = None
            ch = js.call("autopilot.learnedRecipe", pick, {
                "layer": bool(layer), "peak": None, "riff": False, "recipe": recipe, "blend": blend_final, "oneSong": dec["oneSong"],
                "stemsBoth": stems_both, "vocalRule": dec["vocalRule"], "keyScore": key_score, "mashupFits": False})
            if ch:
                learned = {"pick": pick, "why": ch["why"]}
                recipe = booked = ch["recipe"]
        lock = dec["lockS"]
        return {"cand": cand, "candidate": candidate, "vibe": vibe, "from": cur, "to": nxt, "recipe": recipe, "planned_by_matcher": candidate.get("recipe"),
                "dec": dec, "blend": blend_final, "blend_in": bool(blend), "layer": layer, "b_time": b_time, "fire_at": eff, "xf": timing["xfDuration"],
                "exit_moved": moved, "learned": learned, "key_score": key_score, "lock": lock, "window": w["label"],
                "plan": plan, "tempo_stems": tempo_stems, "force_jump": force_jump, "score": score}

    # ---- prepareTransition ------------------------------------------------------------------------------------
    def prepare_transition(self) -> Optional[dict]:
        cur = self.cur
        prep = {"song_index": len(self.history), "rounds": 0, "picked_from": None, "empty": 0}
        self.preps.append(prep)
        # 1) songs already pre-downloaded in an earlier round
        pool = list(self.ready)
        self.ready.clear()
        for i, c in enumerate(pool):
            c["keep"] = False
            b = self.try_candidate(c, False, False)
            if b:
                for rest in pool[i + 1:]:
                    self.add_ready(rest)
                prep["picked_from"] = "pool"
                return b
            if c["keep"]:
                self.add_ready(c)
        # 2) fresh AI suggestions
        rejected: list = []
        for rnd in range(1, MAX_ROUNDS + 1):
            prep["rounds"] = rnd
            force = rnd == MAX_ROUNDS
            allow_jump = force                                      # stems on both decks: no planned tempo jumps
            if rnd == MAX_ROUNDS:
                b = self.try_library_lockable(force, allow_jump)
                if b:
                    prep["picked_from"] = "library"
                    return b
                if self.ready:
                    waiting = list(self.ready)
                    self.ready.clear()
                    for i, c in enumerate(waiting):
                        c["keep"] = False
                        b = self.try_candidate(c, True, True)
                        if b:
                            for rest in waiting[i + 1:]:
                                self.add_ready(rest)
                            prep["picked_from"] = "waiting"
                            return b
                        if c["keep"]:
                            self.add_ready(c)
            try:
                sugs = self.get_suggestions(cur, rejected)
            except WorldError:
                raise
            except Exception as exc:
                self.rejects.append({"song_index": len(self.history), "to": None, "why": f"suggest error: {str(exc)[:160]}", "gate": "suggest_error"})
                continue
            if not sugs:
                self.empty_streak += 1
                self.counters["picks_empty"] += 1
                prep["empty"] += 1
                if self.js.call("autopilot.useLibraryFallback", self.empty_streak):
                    b = self.try_library_lockable(force, allow_jump)
                    if b:
                        self.empty_streak = 0
                        prep["picked_from"] = "library"
                        return b
                continue
            self.empty_streak = 0
            cands = []
            for s in sugs:
                c = self.download_suggestion(s)
                if c is None:
                    rejected.append(f"{s.get('artist')} - {s.get('title')}")
                else:
                    cands.append(c)
            for i, c in enumerate(cands):
                b = self.try_candidate(c, force, allow_jump)
                if b:
                    for rest in cands[i + 1:]:
                        self.add_ready(rest)
                    prep["picked_from"] = f"round{rnd}"
                    return b
                if c["keep"]:
                    self.add_ready(c)
                rejected.append(f"{c['suggestion'].get('artist')} - {c['suggestion'].get('title')}")
        self.counters["stalls"] += 1
        self.t += self.js.call("autopilot.emptyRetryMs", self.empty_streak) / 1000.0
        return None

    def add_ready(self, c: dict) -> None:
        if c["id"] in {r["id"] for r in self.ready} or c["name"] in self.history:
            return
        self.ready.append(c)
        del self.ready[:-4]

    def try_library_lockable(self, force: bool, allow_jump: bool) -> Optional[dict]:
        cur = self.cur
        exclude = ",".join([*self.played_ids, cur.id])
        try:
            lib = self._get("/api/library/lockable", bpm=f"{cur.bpm:.2f}", key=cur.key, exclude=exclude,
                            max_gap=self.consts["PITCH_RANGE_PCT"] / 100, genre="", era="").get("tracks") or []
        except RuntimeError:
            return None
        for t in lib:
            if t["name"] in self.history:
                continue
            c = {"id": t["track_id"], "name": t["name"], "bpm": t["bpm"], "duration": t["duration"], "keep": False, "fromLibrary": True,
                 "suggestion": {"artist": "", "title": t["name"]}}
            b = self.try_candidate(c, force, allow_jump)
            if b:
                return b
        return None

    def top_up_pool(self) -> None:
        """Songs for AFTER the booked next one (a look-ahead suggest + downloads)."""
        if len(self.ready) >= 2:
            return
        nxt = self.scheduled_track
        avoid = [n for n in [nxt.name] + [c["name"] for c in self.ready] if n]
        try:
            sugs = self.get_suggestions(nxt, avoid, lookahead=True)
        except Exception:
            return
        for s in sugs:
            c = self.download_suggestion(s)
            if c:
                self.add_ready(c)

    # ---- executing a booking --------------------------------------------------------------------------------------
    def execute(self, b: dict) -> None:
        from app.sim import moves

        cur, nxt = b["from"], b["to"]
        dec, lock = b["dec"], b["lock"]
        fire_at = min(b["fire_at"], max(cur.duration - 2.0, 0.0)) if cur.duration else b["fire_at"]
        overrun = bool(cur.duration and b["fire_at"] > cur.duration - 2.0)
        fire_t = self.t_entry + (fire_at - self.entry_pos)
        beat = bool(lock["beat"])
        tempo_pct = abs(lock["lock"]["pct"]) if beat else 0.0
        if beat:
            bl = self.js.call("tempoRule.beatLock", {"aEff": cur.bpm, "bBpm": nxt.bpm, "tempoStemsBpm": b["tempo_stems"]})
            tempo_pct = abs(bl["pct"])
        if b["layer"]:
            seconds = (b["layer"]["hold_bars"] + b["layer"]["unwind_bars"]) * 240.0 / max(1.0, cur.bpm)
            move = {"path": "layer", "executed": b["recipe"], "refused": "", "bars": b["layer"]["hold_bars"] + b["layer"]["unwind_bars"],
                    "seconds": round(seconds, 2), "dead_air_s": 0.0, "min_db": 0.0, "intro": None, "intro_rms": None,
                    "silent_intro": False, "unlocked_overlap_s": 0.0, "degraded": False, "kind": "layer"}
            self.counters["layers"] += 1
            self.since_layer = 0
        else:
            move = moves.simulate_move(self.js, {
                "recipe": b["recipe"], "xf_duration": b["xf"], "beat": beat, "a_eff": cur.bpm, "a_bpm": cur.bpm, "b_bpm": nxt.bpm,
                "key_score": b["key_score"], "a_pos": fire_at, "b_pos": b["b_time"], "a_left_s": max(0.0, (cur.duration or fire_at + 60) - fire_at),
                "a_vocals": [list(r) for r in cur.vocals], "b_vocals": [list(r) for r in nxt.vocals],
                "a_curve": cur.curves, "b_curve": nxt.curves, "stems_both": cur.stems and nxt.stems,
                "a_energy": (cur.a.get("energy_times") or [], cur.a.get("energy_curve") or []),
                "b_energy": (nxt.a.get("energy_times") or [], nxt.a.get("energy_curve") or [])})
            self.since_layer += 1
        self.recent_moves = (self.recent_moves + [move["executed"]])[-8:]
        idx = len(self.history)
        self.transitions.append({
            "i": idx, "from": cur.name, "to": nxt.name, "from_id": cur.id, "to_id": nxt.id,
            "from_bpm": round(cur.bpm, 2), "to_bpm": round(nxt.bpm, 2), "from_key": cur.key, "to_key": nxt.key,
            "key_score": b["key_score"], "key_kb": self._kb_key_score(cur.key, nxt.key),
            "beat_locked": beat, "tempo_pct": round(tempo_pct, 3),
            "tempo_jump": not beat, "recipe_matcher": b["planned_by_matcher"], "recipe_planned": b["recipe"],
            "recipe_executed": move["executed"], "path": move["path"], "kind": move["kind"], "refused": move["refused"],
            "degraded": move["degraded"], "dead_air_s": move["dead_air_s"], "min_db": move["min_db"], "intro": move["intro"],
            "intro_rms": move["intro_rms"], "silent_intro": move["silent_intro"], "unlocked_overlap_s": move["unlocked_overlap_s"],
            "seconds": move["seconds"], "bars": move["bars"], "fire_at": round(fire_at, 2), "fire_t": round(fire_t, 2),
            "overrun": overrun, "window": b["window"], "learned": b["learned"]["why"] if b["learned"] else None,
            "blend_dropped": b["blend_in"] and b["blend"] is None,
            "key_rewrite": dec["keyRewrite"], "layer": bool(b["layer"]), "one_song": dec["oneSong"], "vocal_rule": dec["vocalRule"],
            "plan_parsed": bool(b["plan"] and b["plan"].get("parsed")), "plan_asked": b["plan"] is not None,
            "energy_a": (b["vibe"] or {}).get("energy_a"), "energy_b": (b["vibe"] or {}).get("energy_b"),
            "match_score": b["score"], "forced": b["force_jump"], "exit_moved_phrases": b["exit_moved"]})
        self.log("track", event="transition_start", **{"from": cur.name, "to": nxt.name, "recipe": move["executed"],
                                                       "planned": b["recipe"] if move["executed"] != b["recipe"] else None,
                                                       "seconds": move["seconds"]})
        # B is on air from the fire line; the transition ends `seconds` later
        self.songs[-1]["seconds"] = round(fire_t - self.t_entry, 2)
        self.cur = nxt
        self.entry_pos = float(b["b_time"] if not b["layer"] else b["layer"].get("b_swap", b["b_time"]))
        self.t_entry = fire_t
        self.trans_total = float(move["seconds"])
        self.t = fire_t + self.trans_total + 0.5
        self.history.append(nxt.name)
        self.played_ids.append(nxt.id)
        self.current_energy = None
        self.scheduled_name = None
        self.songs.append(self._song_row(nxt))
        self.log("track", event="transition_end", now_playing=nxt.name, set_songs=len(self.history))

    @staticmethod
    def _kb_key_score(a: str, b: str):
        """The KB Camelot score (recipe_matcher.camelot_distance_score) the scorer judges keys by."""
        from app.music_brain.recipe_matcher import camelot_distance_score

        try:
            return camelot_distance_score(a, b)[0] if a and b else None
        except ValueError:
            return None

    def _song_row(self, t: Trk) -> dict:
        return {"i": len(self.history) - 1, "id": t.id, "name": t.name, "bpm": round(t.bpm, 2), "key": t.key, "level": t.level,
                "duration": round(t.duration, 1), "entry_pos": round(self.entry_pos, 2), "t_entry": round(self.t_entry, 2),
                "seconds": None, "stems": t.stems}

    # ---- the set -------------------------------------------------------------------------------------------------------
    def run(self) -> dict:
        seed_id = self.pick_seed()
        self.cur = self.load_track(seed_id)
        self.history.append(self.cur.name)
        self.played_ids.append(seed_id)
        self.t = 6.0
        self.entry_pos = 0.0
        self.songs.append(self._song_row(self.cur))
        self.log("track", event="deck_load", song=self.cur.name)
        stalls = 0
        while len(self.history) < self.n_tracks and stalls < MAX_STALLS:
            b = self.prepare_transition()
            if b is None:
                stalls += 1
                continue
            stalls = 0
            self.scheduled_name, self.scheduled_track = b["to"].name, b["to"]
            self.top_up_pool()
            self.execute(b)
        return self.run_data(stalled=stalls >= MAX_STALLS)

    def run_data(self, stalled: bool) -> dict:
        return {"meta": {"seed": self.seed, "mode": self.mode, "tracks_requested": self.n_tracks, "tracks_played": len(self.history),
                         "world": self.w.mode, "name": self.w.name, "stalled": stalled, "replay_misses": len(self.w.misses),
                         "misses": self.w.misses[:20], "occasion": self.occasion},
                "songs": self.songs, "transitions": self.transitions, "preps": self.preps, "rejects": self.rejects,
                "counters": self.counters}


# ---- command line -------------------------------------------------------------------------------------------
def _slug(s: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "song"


def write_outputs(out: Path, world, run: dict, report: dict) -> None:
    """events.jsonl (session log shape), songs/NN-slug/steps.jsonl (song log shape), run.json, report.json."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in world.events),
                                      encoding="utf-8")
    ids = {s["id"]: s for s in run["songs"]}
    by_song: dict = {}
    for st in world.steps:
        by_song.setdefault(st["track_id"], []).append(st)
    for s in run["songs"]:
        d = out / "songs" / f"{s['i'] + 1:02d}-{_slug(s['name'])}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps({k: s[k] for k in ("name", "bpm", "key", "level", "duration", "seconds")}, indent=1,
                                                sort_keys=True), encoding="utf-8")
        rows = []
        for st in by_song.get(s["id"], []):
            rows.append({"t": st["t"], "at_song": None, "deck": None, "phase": st["phase"] or "planning", "kind": st["kind"],
                         "decision": st["decision"], "why": st["why"], "inputs": st["inputs"], "result": st["result"]})
        (d / "steps.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows),
                                       encoding="utf-8")
    (out / "run.json").write_text(json.dumps(run, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (out / "report.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def run_set(a: argparse.Namespace) -> dict:
    """One set. The private cache dir must be in AIDJ_CACHE_DIR before app.* is imported (main() does it)."""
    from app.music_brain import config

    run_cache = Path(os.environ["AIDJ_CACHE_DIR"]).resolve()
    if config.CACHE_DIR != run_cache:
        raise RuntimeError(f"app.music_brain.config was imported before AIDJ_CACHE_DIR was set "
                           f"({config.CACHE_DIR} != {run_cache}): run the sim as `python -m app.sim.virtual_set`")
    from app.sim import scorer
    from app.sim.jsbridge import JsBridge
    from app.sim.library import MainLibrary
    from app.sim.pool import Pool
    from app.sim.stubllm import StubLLM
    from app.sim.world import Caps, World

    pool = Pool()
    library = None
    seed, tracks, mode, occasion = a.seed, a.tracks, a.mode, a.occasion or ""
    if a.replay:
        rj = json.loads(((Path(a.fixtures) if a.fixtures else pool.root.parent) / a.replay / "run.json").read_text(encoding="utf-8"))
        seed = a.seed if a.seed is not None else rj["seed"]
        tracks = a.tracks if a.tracks else rj.get("tracks", 10)
        mode = a.mode or rj.get("mode", "long")
        occasion = a.occasion if a.occasion is not None else rj.get("occasion", "")
        world_mode, name = "replay", a.replay
    elif a.library:
        world_mode, name = "library", a.record or "library"
    else:
        world_mode, name = "live", a.record or "live"
    seed = 1 if seed is None else seed
    tracks = tracks or 10
    mode = mode or "long"
    if mode not in MODES:
        raise SystemExit(f"--mode must be one of {MODES}")
    if world_mode != "replay":
        library = MainLibrary()
    llm = None
    if world_mode == "library" or world_mode == "replay":
        # the stub answers the library world, and a replay that leaves its recording
        catalog = _catalog_for(world_mode, library, pool)
        llm = StubLLM(seed, catalog)
    world = World(world_mode, name, seed, run_cache, pool=pool, record=bool(a.record), llm=llm, library=library,
                  caps=Caps(max_downloads=a.max_downloads), fixtures_dir=Path(a.fixtures) if a.fixtures else None)
    if world_mode == "live":
        world.bind_live()
    world.install()
    from app.tests.testclient_compat import TestClient
    from app.ui.server import app as fastapi_app

    try:
        with JsBridge() as js:
            sim = Sim(world, js, TestClient(fastapi_app), seed, tracks, mode, occasion)
            world.vclock = lambda: sim.t
            run = sim.run()
    finally:
        world.uninstall()
    run["meta"]["fixture_source"] = world.fx.get("source")
    report = scorer.score_run(run)
    if a.record:
        if world_mode == "live":
            world.export_live(pool)
        st = world.seed_track or {"hash": None, "name": run["songs"][0]["name"]}
        world.save_fixture(st, {"tracks": tracks, "mode": mode, "occasion": occasion})
    if a.out:
        write_outputs(Path(a.out), world, run, report)
    return report


def _catalog_for(world_mode: str, library, pool) -> list:
    """The songs the stub may suggest: every pool track (replay) or every usable library song (library)."""
    from app.music_brain import energy as en

    cat = []
    if world_mode == "library":
        for t in library.tracks():
            lvl = None
            ep = library.cache / "analysis" / f"{t.hash}.energy.json"
            if ep.exists():
                d = json.loads(ep.read_text(encoding="utf-8"))
                lvl = en.level_from_raw(en._raw(d), library.library_raws())
            cat.append({"name": t.name, "bpm": t.bpm, "key": t.key, "level": lvl, "id": t.hash})
        return cat
    for h in pool.hashes():
        e = pool.load(h)
        raws = None
        lvl = en.level_from_raw(en._raw(e["energy"]), raws) if e.get("energy") else None
        cat.append({"name": e["name"], "bpm": float(e["analysis"].get("bpm") or 0),
                    "key": (e["analysis"].get("key") or {}).get("camelot") or "", "level": lvl, "id": h})
    return cat


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m app.sim.virtual_set", description=__doc__.split("\n\n")[0])
    p.add_argument("--seed", type=int, default=None, help="RNG seed (seed track, stub picks); replay: the fixture's")
    p.add_argument("--tracks", type=int, default=0, help="songs in the set (default 10)")
    p.add_argument("--mode", choices=MODES, default=None, help="set mode (default long; replay: the fixture's)")
    p.add_argument("--occasion", default=None)
    p.add_argument("--out", default=None, help="output dir: events.jsonl, report.json, songs/*/steps.jsonl, run.json")
    p.add_argument("--replay", metavar="NAME", help="replay app/sim/fixtures/NAME: zero network / LLM / Demucs")
    p.add_argument("--record", metavar="NAME", help="record this run as app/sim/fixtures/NAME (live: real LLM + YouTube)")
    p.add_argument("--library", action="store_true", help="offline world: songs from DATA_DIR, StubLLM (builds fixtures without a model)")
    p.add_argument("--max-downloads", type=int, default=14, help="cap on YouTube downloads in a live run")
    p.add_argument("--fixtures", default=None, help="fixtures dir (default app/sim/fixtures)")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.replay and (a.record or a.library):
        print("--replay cannot be combined with --record / --library", file=sys.stderr)
        return 2
    from app.sim.pool import sim_data_dir

    runs = sim_data_dir() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    cache = Path(tempfile.mkdtemp(prefix="run-", dir=runs))
    os.environ["AIDJ_CACHE_DIR"] = str(cache)
    try:
        report = run_set(a)
    except Exception as exc:
        from app.sim.world import WorldError

        if isinstance(exc, WorldError):
            print(f"virtual set stopped: {exc}", file=sys.stderr)
            return 3
        raise
    finally:
        import shutil

        shutil.rmtree(cache, ignore_errors=True)
    print(json.dumps({"score": report["score"], "metrics": report["metrics"], "worst": report["worst"][:5]}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
