// SHOW mode: an Anyma-style stage show on one WebGL canvas, driven by what the
// decks are actually playing (brief: research/notes/anyma-visual-brief.md).
//
// Black space, ice-white / cyan light, rare red. Four procedural scenes:
//   head      an android head of particles + contour lines; eyes light with the vocal
//   figure    a giant humanoid made of rings of light; arms rise with the build
//   corridor  square frames rushing at the camera; speed follows energy
//   monolith  a black slab lit by a scan line, LED floor, deep space
// Music sync from the deck's analysis (beat grid, downbeats, 8-bar phrases,
// sections, energy curve, vocal regions) and live stem levels: scenes change
// on phrase boundaries (held >= 16 bars), drops and supermoves are hard cuts
// with a capped flash and 2 bars of fast camera cuts, track transitions are a
// long particle dissolve, learned/artist moves are small accents.
//
// Layout: a pure `core` (music state, scene choice, trigger mapping, director,
// adaptive quality; node-checked by app/tests/anyma_show_check.js) and a thin
// WebGL glue that only runs while SHOW is on and the page is visible. NULL-BOT
// (z 880) stays above the stage (z 870). Nothing here touches the audio graph:
// stem levels are read from the decks' decoded stem buffers.
(function (root) {
  "use strict";

  // ---- core (pure) ---------------------------------------------------------
  const SCENES = ["head", "figure", "corridor", "monolith"];
  const HOLD_BARS = 16;              // a scene holds at least 2 phrases
  const DISSOLVE_BARS = 1;           // phrase change: 1 bar particle dissolve
  const XFADE_BARS = 8;              // track transition: long dissolve
  const BURST_BARS = 2;              // fast cuts after a drop / supermove
  const BURST_EVERY_BEATS = 2;       // one camera cut every 2 beats in a burst
  const FLASH_GAP_S = 1;             // photosensitivity: <= 1 flash per second
  const DROP_DEDUP_S = 4;            // a cue-booked drop and the section edge are one drop
  const MIN_CUT_GAP_S = 0.3;
  const ASSEMBLE_S = 0.7;            // incoming scene assembles from particles after a cut
  const SHOTS = 4;                   // camera presets per scene
  const QUEUE_MAX = 16;

  // Where each music class wants to be, best first. Vocal (not on a drop): face first.
  const PREF = {
    drop: ["figure", "head", "corridor", "monolith"],
    build: ["corridor", "monolith", "figure", "head"],
    breakdown: ["monolith", "head", "corridor", "figure"],
    calm: ["monolith", "corridor", "head", "figure"],
    groove: ["figure", "monolith", "corridor", "head"],
  };
  const VOCAL_PREF = {};
  for (const k of Object.keys(PREF)) VOCAL_PREF[k] = k === "drop" ? PREF[k] : ["head"].concat(PREF[k].filter((s) => s !== "head"));

  const fin = (x) => typeof x === "number" && Number.isFinite(x);
  const clamp01 = (x) => (x > 1 ? 1 : x > 0 ? x : 0);

  // Pure: index of the last value <= x in an ascending array (-1 if none).
  function lastLE(arr, x) {
    if (!arr || !arr.length || !fin(x)) return -1;
    let lo = 0, hi = arr.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] <= x) lo = m + 1; else hi = m; }
    return lo - 1;
  }

  // Pure: section label -> music class. Labels win; without one, energy decides.
  function sectionClass(label, energy, slope) {
    const l = typeof label === "string" ? label.toLowerCase() : "";
    if (l.includes("drop") || l.includes("chorus")) return "drop";
    if (l.includes("build") || l.includes("pre")) return "build";
    if (l.includes("break")) return "breakdown";
    if (l.includes("intro") || l.includes("outro")) return "calm";
    if (l) return "groove";
    const e = fin(energy) ? energy : 0, s = fin(slope) ? slope : 0;
    if (e >= 0.78) return "drop";
    if (s >= 0.12) return "build";
    if (e <= 0.3) return "breakdown";
    return "groove";
  }

  // Pure: per-track constants, computed once per loaded analysis.
  function prepTrack(an) {
    if (!an || typeof an !== "object") return null;
    const c = Array.isArray(an.energy_curve) ? an.energy_curve : [];
    let lo = Infinity, hi = -Infinity;
    for (let i = 0; i < c.length; i++) if (fin(c[i])) { if (c[i] < lo) lo = c[i]; if (c[i] > hi) hi = c[i]; }
    const vox = [];
    for (const r of an.vocal_active_regions || []) {
      const s = Array.isArray(r) ? r[0] : r && r.start, e = Array.isArray(r) ? r[1] : r && r.end;
      if (fin(s) && fin(e) && e > s) vox.push(s, e);
    }
    const secStarts = [], secs = [];
    for (const s of an.sections || []) if (s && fin(s.start)) { secStarts.push(s.start); secs.push(s); }
    return { an, eLo: fin(lo) ? lo : 0, eSpan: hi > lo ? hi - lo : 0, vox, secStarts, secs };
  }

  function energyAt(pt, t) {
    const an = pt.an, c = an.energy_curve, ts = an.energy_times;
    if (!Array.isArray(c) || !c.length || !(pt.eSpan > 0)) return 0.5;
    let v;
    if (Array.isArray(ts) && ts.length === c.length) {
      const i = lastLE(ts, t);
      if (i < 0) v = c[0];
      else if (i >= c.length - 1) v = c[c.length - 1];
      else { const k = (t - ts[i]) / ((ts[i + 1] - ts[i]) || 1); v = c[i] + (c[i + 1] - c[i]) * clamp01(k); }
    } else v = c[Math.max(0, Math.min(c.length - 1, Math.floor(t)))];  // 1 s hop fallback
    return fin(v) ? clamp01((v - pt.eLo) / pt.eSpan) : 0.5;
  }

  function musicStateNew() {
    return { ok: false, pos: 0, bpm: 128, beat: 0.46875, beatIdx: -1, beatPhase: 0, barIdx: -1, barPhase: 0,
      phraseIdx: -1, phrasePhase: 0, section: "", cls: "calm", energy: 0, slope: 0, vocal: false };
  }

  // Pure: the music at song time `pos`, written into `out` (no allocation).
  function musicState(pt, pos, bpm, out) {
    const o = out || musicStateNew();
    if (!pt || !fin(pos)) { o.ok = false; return o; }
    const an = pt.an;
    o.ok = true; o.pos = pos;
    o.bpm = fin(bpm) && bpm > 0 ? bpm : fin(an.bpm) && an.bpm > 0 ? an.bpm : 128;
    o.beat = 60 / o.bpm;
    const bar = 4 * o.beat;
    const bt = an.beat_times, db = an.downbeat_times, pb = an.phrase_boundaries_8bar;
    let i = lastLE(bt, pos);
    if (i >= 0) {
      o.beatIdx = i;
      const nx = i + 1 < bt.length ? bt[i + 1] : bt[i] + o.beat;
      o.beatPhase = clamp01((pos - bt[i]) / ((nx - bt[i]) || o.beat));
    } else { o.beatIdx = Math.floor(pos / o.beat); o.beatPhase = pos / o.beat - o.beatIdx; }
    i = lastLE(db, pos);
    if (i >= 0) {
      o.barIdx = i;
      const nx = i + 1 < db.length ? db[i + 1] : db[i] + bar;
      o.barPhase = clamp01((pos - db[i]) / ((nx - db[i]) || bar));
    } else { o.barIdx = Math.floor(pos / bar); o.barPhase = pos / bar - o.barIdx; }
    i = lastLE(pb, pos);
    if (i >= 0) {
      o.phraseIdx = i;
      const nx = i + 1 < pb.length ? pb[i + 1] : pb[i] + 8 * bar;
      o.phrasePhase = clamp01((pos - pb[i]) / ((nx - pb[i]) || 8 * bar));
    } else { o.phraseIdx = Math.floor(pos / (8 * bar)); o.phrasePhase = pos / (8 * bar) - o.phraseIdx; }
    const si = lastLE(pt.secStarts, pos), sec = si >= 0 ? pt.secs[si] : null;
    o.section = sec && (!fin(sec.end) || pos < sec.end) && typeof sec.label === "string" ? sec.label : "";
    o.energy = energyAt(pt, pos);
    o.slope = o.energy - energyAt(pt, pos - 8 * bar);
    o.cls = sectionClass(o.section, o.energy, o.slope);
    const vi = lastLE(pt.vox.length ? evenView(pt) : null, pos);
    o.vocal = vi >= 0 && pos < pt.vox[vi * 2 + 1];
    return o;
  }
  // region starts (every other value), built once per track
  function evenView(pt) {
    if (!pt._starts) { pt._starts = []; for (let i = 0; i < pt.vox.length; i += 2) pt._starts.push(pt.vox[i]); }
    return pt._starts;
  }

  // Pure: the scene the music asks for. Keeps `cur` if it is one of the class's
  // top two (under a vocal only the face); `force` (drop, supermove) always changes scene.
  function pickScene(cls, vocal, cur, force) {
    const voice = vocal && cls !== "drop";
    const list = (voice ? VOCAL_PREF : PREF)[cls] || (voice ? VOCAL_PREF : PREF).groove;
    if (!force && (list[0] === cur || (!voice && list[1] === cur))) return cur;
    for (let i = 0; i < list.length; i++) if (list[i] !== cur) return list[i];
    return list[0];
  }

  // Pure: console event -> director trigger ({type, at} or null). `at` is on
  // the director's clock; `toNow(audioTime)` converts a cue's audio time.
  function eventTrigger(type, detail, now, toNow) {
    const d = detail || {};
    const conv = (at) => { const v = fin(at) && typeof toNow === "function" ? toNow(at) : now; return fin(v) ? v : now; };
    if (type === "ai-supermove") return { type: "supermove", at: conv(d.at) };
    if (type === "ai-cue") {
      if (d.kind === "drop") return { type: "drop", at: conv(d.at) };
      if (d.kind === "transition") return { type: "transition", at: conv(d.at) };
      if (d.kind === "line" || d.kind === "peak") return { type: "accent", at: conv(d.at) };
      return null;
    }
    if (type === "ai-activity") {
      const k = d.kind;
      if (k === "learned_move" || k === "artist_move" || k === "stem-move") return { type: "accent", at: now };
      return null;
    }
    return null;
  }

  // Director: all timing decisions. Deterministic (own LCG), no allocation per step.
  function createDirector(seed) {
    return { scene: "monolith", shot: 0, shotT: 0, prev: null, prevShot: 0, prevShotT: 0, mix: 1, mixDur: 1,
      assemble: 0, sceneAt: -Infinity, lastCut: -Infinity, lastDrop: -Infinity, lastFlash: -Infinity,
      burstUntil: -Infinity, burstBeat: 0, flash: 0, accent: 0, red: 0, glitch: 0,
      lastBar: null, lastPhrase: null, lastBeat: null, lastCls: "", queue: [], last: "", cuts: 0,
      rng: (seed >>> 0) || 1 };
  }
  function rnd(d) { d.rng = (Math.imul(d.rng, 1664525) + 1013904223) >>> 0; return d.rng / 4294967296; }
  function queueEvent(d, ev) {
    if (!d || !ev || !fin(ev.at)) return;
    if (d.queue.length >= QUEUE_MAX) d.queue.shift();
    d.queue.push(ev);
  }
  function newShot(d) { d.shot = (d.shot + 1 + Math.floor(rnd(d) * (SHOTS - 1))) % SHOTS; d.shotT = 0; }
  function setScene(d, s, now, dissolveS) {
    if (s === d.scene) return;
    if (dissolveS > 0) { d.prev = d.scene; d.prevShot = d.shot; d.prevShotT = d.shotT; d.mix = 0; d.mixDur = dissolveS; }
    else { d.prev = null; d.mix = 1; d.assemble = 1; }
    d.scene = s; d.sceneAt = now; newShot(d);
  }
  function cut(d, now, scene, flash, reduced) {
    if (now - d.lastCut < MIN_CUT_GAP_S) return false;
    d.lastCut = now; d.cuts++;
    if (scene && scene !== d.scene) setScene(d, scene, now, reduced ? 1.2 : 0);
    else if (!reduced) newShot(d);
    if (flash && !reduced && now - d.lastFlash >= FLASH_GAP_S) { d.flash = 1; d.lastFlash = now; }
    if (!reduced) d.glitch = 1;
    return true;
  }
  function bigMoment(d, ms, now, reduced, red) {
    const cls = ms && ms.ok ? ms.cls : "drop";
    const s = pickScene(cls === "breakdown" || cls === "calm" ? "drop" : cls, false, d.scene, true);
    if (!cut(d, now, s, true, reduced)) return false;
    if (red) d.red = 1;
    if (!reduced) { d.burstUntil = now + BURST_BARS * 4 * (ms && ms.ok ? ms.beat : 0.47); d.burstBeat = ms && ms.ok ? ms.beatIdx : 0; }
    return true;
  }

  // Pure step. ms: musicState (ok false = nothing playing). Sets d.last to the
  // notable action of this step: "cut" | "drop" | "supermove" | "dissolve" | "accent" | "".
  function stepDirector(d, ms, now, dt, reduced) {
    d.last = "";
    const k = fin(dt) && dt > 0 ? Math.min(dt, 0.25) : 0;
    d.flash *= Math.exp(-k * 7); d.accent *= Math.exp(-k * 3); d.red *= Math.exp(-k * 0.9); d.glitch *= Math.exp(-k * 10);
    d.assemble = Math.max(0, d.assemble - k / ASSEMBLE_S);
    d.shotT += k; d.prevShotT += k;
    if (d.prev) { d.mix += k / (d.mixDur || 1); if (d.mix >= 1) { d.mix = 1; d.prev = null; } }
    // due events (booked on the audio clock, fired on time)
    for (let i = 0; i < d.queue.length; i++) {
      const e = d.queue[i];
      if (e.at > now + 1e-6) continue;
      d.queue.splice(i--, 1);
      if (now - e.at > 2) continue;              // stale (tab was hidden): drop it
      if (e.type === "supermove") { if (bigMoment(d, ms, now, reduced, true)) { d.lastDrop = now; d.last = "supermove"; } }
      else if (e.type === "drop") {
        if (now - d.lastDrop >= DROP_DEDUP_S && bigMoment(d, ms, now, reduced, false)) { d.lastDrop = now; d.last = "drop"; }
      } else if (e.type === "transition") {
        const bar = 4 * (ms && ms.ok ? ms.beat : 0.47);
        const s = pickScene(ms && ms.ok ? ms.cls : "groove", ms && ms.vocal, d.scene, true);
        setScene(d, s, now, Math.min(16, Math.max(4, XFADE_BARS * bar)));
        d.last = "dissolve";
      } else if (e.type === "accent") { d.accent = 1; if (!reduced) d.glitch = Math.max(d.glitch, 0.5); d.last = d.last || "accent"; }
    }
    if (!ms || !ms.ok) { d.lastBar = d.lastPhrase = d.lastBeat = null; d.lastCls = ""; return d; }
    const seek = d.lastBar !== null && Math.abs(ms.barIdx - d.lastBar) > 2;
    if (d.lastBar === null || seek) {                   // first frame / seek / loop: resync, fire nothing
      d.lastBar = ms.barIdx; d.lastPhrase = ms.phraseIdx; d.lastBeat = ms.beatIdx; d.lastCls = ms.cls;
      if (!fin(d.sceneAt)) { d.scene = pickScene(ms.cls, ms.vocal, d.scene, false); d.sceneAt = now; }
      return d;
    }
    const bar = 4 * ms.beat;
    if (ms.cls === "drop" && d.lastCls !== "drop" && now - d.lastDrop >= DROP_DEDUP_S) {
      if (bigMoment(d, ms, now, reduced, false)) { d.lastDrop = now; d.last = "drop"; }
    } else if (ms.phraseIdx !== d.lastPhrase && now - d.sceneAt >= HOLD_BARS * bar - 0.05 && !d.prev) {
      const s = pickScene(ms.cls, ms.vocal, d.scene, false);
      if (s !== d.scene) { setScene(d, s, now, DISSOLVE_BARS * bar); d.last = d.last || "dissolve"; }
    }
    const nb = ms.beatIdx - d.burstBeat;               // beats since the big moment
    if (ms.beatIdx !== d.lastBeat && now < d.burstUntil && !reduced
        && nb > 0 && nb < BURST_BARS * 4 && nb % BURST_EVERY_BEATS === 0) {
      if (cut(d, now, null, false, reduced)) d.last = d.last || "cut";
    }
    d.lastBar = ms.barIdx; d.lastPhrase = ms.phraseIdx; d.lastBeat = ms.beatIdx; d.lastCls = ms.cls;
    return d;
  }

  // Pure: attack/release follower; returns the new level.
  function follow(level, x, dt, attackS, releaseS) {
    const v = fin(x) ? clamp01(x) : 0, l = fin(level) ? level : 0;
    if (!(dt > 0)) return l;
    const tau = v > l ? attackS : releaseS;
    return l + (v - l) * (1 - Math.exp(-dt / tau));
  }

  // Stem levels: raw RMS per stem -> 0..1 against a slowly decaying running peak.
  const STEM_NAMES = ["drums", "bass", "vocals", "other"];
  function stemsNew() { return { peak: { drums: 0, bass: 0, vocals: 0, other: 0 }, level: { drums: 0, bass: 0, vocals: 0, other: 0 }, live: false }; }
  function stemsStep(st, raw, dt) {
    st.live = !!raw;
    for (const n of STEM_NAMES) {
      const x = raw && fin(raw[n]) && raw[n] > 0 ? raw[n] : 0;
      st.peak[n] = Math.max(x, st.peak[n] * Math.exp(-(fin(dt) ? dt : 0) / 20), 0.02);
      st.level[n] = follow(st.level[n], x / st.peak[n], dt, n === "drums" ? 0.012 : 0.05, n === "drums" ? 0.12 : 0.35);
    }
    return st;
  }

  // Pure: the four visual drives from music + stems.
  //   pulse (drums) weight (bass) eye (vocals) colour (other), all 0..1.
  function drives(ms, st, out) {
    const o = out || { pulse: 0, weight: 0, eye: 0, colour: 0, intensity: 0 };
    if (!ms || !ms.ok) { o.pulse = o.weight = o.eye = o.colour = 0; o.intensity = 0.35; return o; }
    const beatEnv = Math.exp(-ms.beatPhase * 6);
    if (st && st.live) {
      o.pulse = clamp01(st.level.drums * (0.4 + 0.6 * beatEnv));
      o.weight = st.level.bass; o.eye = st.level.vocals; o.colour = st.level.other;
    } else {
      o.pulse = clamp01(beatEnv * (0.3 + 0.7 * ms.energy));
      o.weight = ms.energy; o.eye = ms.vocal ? 0.8 : 0; o.colour = ms.energy * 0.5;
    }
    o.intensity = clamp01(0.45 + 0.55 * ms.energy);
    return o;
  }

  // Adaptive quality: step down fast when our frame work or the frame gap
  // blows the budget, back up slowly; every step down doubles the wait to
  // step up again (no flapping).
  const QUALITY = [{ pts: 1, res: 1 }, { pts: 0.7, res: 0.85 }, { pts: 0.45, res: 0.7 }, { pts: 0.25, res: 0.55 }];
  const WORK_BUDGET_MS = 4, DOWN_FRAMES = 45, UP_FRAMES = 600;
  function qualityNew() { return { level: 0, work: 0, gap: 0, n: 0, over: 0, under: 0, upFrames: UP_FRAMES }; }
  function qualityStep(q, workMs, gapMs, targetMs) {
    if (!q || !fin(workMs) || !fin(gapMs) || workMs < 0 || gapMs <= 0 || gapMs > 250) return false;
    const t = fin(targetMs) && targetMs > 0 ? targetMs : 1000 / 60;
    q.work = q.n ? q.work * 0.9 + workMs * 0.1 : workMs;
    q.gap = q.n ? q.gap * 0.9 + gapMs * 0.1 : gapMs;
    q.n++;
    if (q.work > WORK_BUDGET_MS || q.gap > t * 1.5) {
      q.under = 0;
      if (++q.over >= DOWN_FRAMES && q.level < QUALITY.length - 1) {
        q.level++; q.over = 0; q.n = 0; q.upFrames = Math.min(q.upFrames * 2, 9600); return true;
      }
    } else if (q.work < WORK_BUDGET_MS * 0.4 && q.gap < t * 1.2) {
      q.over = Math.max(0, q.over - 1);
      if (++q.under >= q.upFrames && q.level > 0) { q.level--; q.under = 0; q.n = 0; return true; }
    } else { q.over = Math.max(0, q.over - 1); q.under = 0; }
    return false;
  }

  const core = { SCENES, PREF, HOLD_BARS, BURST_BARS, BURST_EVERY_BEATS, FLASH_GAP_S, DROP_DEDUP_S, QUALITY,
    WORK_BUDGET_MS, DOWN_FRAMES, UP_FRAMES, lastLE, sectionClass, prepTrack, musicState, musicStateNew,
    pickScene, eventTrigger, createDirector, queueEvent, stepDirector, follow, stemsNew, stemsStep, drives,
    qualityNew, qualityStep };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof document === "undefined" || typeof root.addEventListener !== "function") return;
  root.anymaShow = { core };
})(typeof window !== "undefined" ? window : globalThis);
