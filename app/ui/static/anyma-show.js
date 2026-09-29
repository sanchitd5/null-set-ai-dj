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
// Anyma drops (a long dip or build, then a hard jump on a phrase line, found
// once per track) cut to the figure, which DANCES on the beat for 16-32 bars.
// SHOW AUTO (#ap-show-auto): while the AI drives, the AI sizes the stage
// itself (full on big moments, window in calm phrases), logged as
// `show: full|window: <why>`. It never calls the browser Fullscreen API.
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
  const DANCE_MIN_BARS = 16, DANCE_MAX_BARS = 32;   // an Anyma drop's dance: 16 bars, up to 32

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
      dance: 0, danceFrom: -Infinity, danceUntil: -Infinity,
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
    const danceWant = now < d.danceUntil ? 1 : 0;
    d.dance += (danceWant - d.dance) * (1 - Math.exp(-k * (danceWant ? 8 : 1.5)));
    if (d.dance < 1e-3) d.dance = 0;
    // due events (booked on the audio clock, fired on time)
    for (let i = 0; i < d.queue.length; i++) {
      const e = d.queue[i];
      if (e.at > now + 1e-6) continue;
      d.queue.splice(i--, 1);
      if (now - e.at > 2) continue;              // stale (tab was hidden): drop it
      if (e.type === "anyma") {
        // Anyma drop: hard cut to the figure, which dances 16-32 bars (no camera burst, so the dance reads)
        const bar = 4 * (ms && ms.ok ? ms.beat : 0.47);
        if (!cut(d, now, "figure", true, reduced)) setScene(d, "figure", now, 0);
        d.lastDrop = now; d.danceFrom = now; d.danceUntil = now + DANCE_MAX_BARS * bar; d.burstUntil = -Infinity;
        d.last = "anyma";
      } else if (e.type === "supermove") { if (bigMoment(d, ms, now, reduced, true)) { d.lastDrop = now; d.last = "supermove"; } }
      else if (e.type === "drop") {
        if (now - d.lastDrop >= DROP_DEDUP_S && bigMoment(d, ms, now, reduced, false)) { d.lastDrop = now; d.last = "drop"; }
      } else if (e.type === "transition") {
        const bar = 4 * (ms && ms.ok ? ms.beat : 0.47);
        const s = pickScene(ms && ms.ok ? ms.cls : "groove", ms && ms.vocal, d.scene, true);
        setScene(d, s, now, Math.min(16, Math.max(4, XFADE_BARS * bar)));
        d.danceUntil = Math.min(d.danceUntil, now);             // a new track ends the dance
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
    if (now < d.danceUntil && ms.phraseIdx !== d.lastPhrase && now - d.danceFrom >= DANCE_MIN_BARS * bar - 0.05
        && (ms.cls === "calm" || ms.cls === "breakdown")) d.danceUntil = now;        // calm phrase after 16 bars: stop
    if (now < d.danceUntil) { /* the figure keeps the floor: no scene change while it dances */ }
    else if (ms.cls === "drop" && d.lastCls !== "drop" && now - d.lastDrop >= DROP_DEDUP_S) {
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

  // Pure: camera per scene. Each shot is [distance, yaw, pitch] around a target;
  // the camera drifts slowly inside a shot (bounded sway, a slow push in).
  // Writes eye / target into `out` (6 numbers), no allocation.
  const CAM = {
    head: { ty: 0.08, tz: 0, sway: 0.5, sh: [[3.4, 0, 0.02], [2.3, 0.55, 0.08], [4.8, -0.4, -0.06], [1.7, 0.12, 0.03]] },
    figure: { ty: 0.4, tz: 0, sway: 0.5, sh: [[6.5, 0, -0.28], [4.2, 0.65, -0.4], [9, -0.3, -0.05], [3.2, -0.85, -0.55]] },
    corridor: { ty: 0, tz: -10, sway: 0.12, sh: [[13, 0, 0], [13, 0.07, 0.05], [12.5, -0.09, -0.04], [14, 0.03, -0.08]] },
    monolith: { ty: 1.8, tz: 0, sway: 0.5, sh: [[8.5, 0, 0.04], [5.2, 0.6, 0.12], [12.5, -0.45, 0.28], [3.8, 0.22, -0.2]] },
  };
  function cameraPose(scene, shot, shotT, reduced, out) {
    const c = CAM[scene] || CAM.monolith, s = c.sh[((shot | 0) % c.sh.length + c.sh.length) % c.sh.length];
    const t = fin(shotT) && shotT > 0 ? shotT : 0, rm = reduced ? 0.4 : 1, sign = shot & 1 ? -1 : 1;
    const yaw = s[1] + sign * c.sway * rm * Math.sin(t * 0.045);
    const pitch = s[2] + 0.02 * rm * Math.sin(t * 0.21);
    const dist = s[0] * (1 - Math.min(0.12, t * 0.004 * rm));
    out[3] = 0; out[4] = c.ty; out[5] = c.tz;
    out[0] = dist * Math.sin(yaw) * Math.cos(pitch);
    out[1] = c.ty + dist * Math.sin(pitch);
    out[2] = c.tz + dist * Math.cos(yaw) * Math.cos(pitch);
    return out;
  }

  // Pure: the four scenes as vertex data, built once. A vertex is 5 floats:
  // x, y, z, kind, rand. Kinds: 0 body, 1 eye, 2/3 left/right arm (arm-local,
  // the shader swings it from the shoulder), 4 corridor (the shader moves it
  // along z), 5 LED floor, 6 monolith face (scan line), 7 head contour.
  // Points are shuffled so any prefix is an even sample (adaptive quality
  // draws a prefix). Line segments keep one rand, so a dissolve moves them whole.
  const REC = 5;
  function buildScenes(seed) {
    let s = (seed >>> 0) || 1;
    const R = () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; };
    const TAU = Math.PI * 2;
    const mk = () => ({ p: [], l: [] });
    const P = (g, x, y, z, k) => { g.p.push(x, y, z, k, R()); };
    const L = (g, a, b, c, x, y, z, k) => { const r = R(); g.l.push(a, b, c, k, r, x, y, z, k, r); };
    const ring = (g, cx, cy, cz, rx, rz, n, k, pts) => {
      for (let i = 0; i < n; i++) {
        const a0 = i / n * TAU, a1 = (i + 1) / n * TAU;
        L(g, cx + Math.cos(a0) * rx, cy, cz + Math.sin(a0) * rz, cx + Math.cos(a1) * rx, cy, cz + Math.sin(a1) * rz, k);
      }
      for (let i = 0; i < pts; i++) { const a = R() * TAU; P(g, cx + Math.cos(a) * rx, cy + (R() - 0.5) * 0.02, cz + Math.sin(a) * rz, k); }
    };

    // head: an android face on a stretched sphere (jaw taper, nose, sockets, brow, lips)
    const head = mk(), o = [0, 0, 0], o2 = [0, 0, 0];
    const hs = (u, v, out) => {
      const th = u * TAU, ph = v * Math.PI, rr = Math.sin(ph);
      let x = Math.sin(th) * rr * 0.72, y = Math.cos(ph), z = Math.cos(th) * rr * 0.86;
      if (y < -0.1) { const t = Math.min(1, (-0.1 - y) / 0.9); x *= 1 - 0.35 * t; z *= 1 - 0.15 * t; }
      if (z > 0) {
        const ex = Math.abs(x) - 0.27, ey = y - 0.12;
        z += 0.16 * Math.exp(-(x * x / 0.004 + (y + 0.12) * (y + 0.12) / 0.05))
          - 0.09 * Math.exp(-(ex * ex + ey * ey) / 0.01)
          + 0.05 * Math.exp(-(x * x / 0.3 + (y - 0.3) * (y - 0.3) / 0.004))
          + 0.03 * Math.exp(-(x * x / 0.02 + (y + 0.5) * (y + 0.5) / 0.003));
      }
      out[0] = x; out[1] = y; out[2] = z; return out;
    };
    for (let i = 0; i < 9000; i++) { hs(R(), Math.acos(1 - 2 * R()) / Math.PI, o); P(head, o[0], o[1], o[2], 0); }
    for (const sx of [-1, 1]) for (let i = 0; i < 260; i++) {
      const a = R() * TAU, r = 0.055 * Math.sqrt(R());
      P(head, sx * 0.27 + Math.cos(a) * r, 0.12 + Math.sin(a) * r * 0.6, 0.72 + 0.01 * R(), 1);
    }
    for (let i = 0; i < 1400; i++) { const a = R() * TAU; P(head, Math.cos(a) * 0.27, -0.85 - R() * 0.8, Math.sin(a) * 0.3, 0); }
    for (let j = 0; j < 6; j++) ring(head, 0, -0.9 - j * 0.14, 0, 0.27, 0.3, 48, 7, 0);
    for (let j = 0; j < 22; j++) {
      const v = 0.06 + j * 0.04;
      for (let i = 0; i < 72; i++) { hs(i / 72, v, o); hs((i + 1) / 72, v, o2); L(head, o[0], o[1], o[2], o2[0], o2[1], o2[2], 7); }
    }
    for (let m = 0; m < 18; m++) for (let i = 0; i < 40; i++) {
      hs(m / 18, 0.03 + i * 0.0235, o); hs(m / 18, 0.03 + (i + 1) * 0.0235, o2); L(head, o[0], o[1], o[2], o2[0], o2[1], o2[2], 7);
    }

    // figure: a giant humanoid drawn in rings of light
    const fig = mk();
    const KP = [[0, 0.3], [0.45, 0.24], [1.0, 0.34], [1.25, 0.4], [1.34, 0.2]];
    const prof = (y) => { for (let i = 1; i < KP.length; i++) if (y <= KP[i][0]) { const [y0, r0] = KP[i - 1], [y1, r1] = KP[i]; return r0 + (r1 - r0) * (y - y0) / (y1 - y0); } return 0.2; };
    for (let y = 0; y <= 1.34; y += 0.06) ring(fig, 0, y, 0, prof(y), prof(y) * 0.6, 40, 0, 60);
    ring(fig, 0, 1.42, 0, 0.1, 0.1, 24, 0, 20); ring(fig, 0, 1.5, 0, 0.1, 0.1, 24, 0, 20);
    for (let y = 1.56; y <= 1.95; y += 0.04) { const r = 0.19 * Math.sqrt(Math.max(0, 1 - ((y - 1.75) / 0.21) ** 2)); ring(fig, 0, y, 0, r, r * 1.1, 32, 0, 40); }
    for (const sx of [-1, 1]) for (let y = -1.9; y <= -0.02; y += 0.08) { const r = 0.07 + 0.07 * (y + 1.9) / 1.88; ring(fig, sx * 0.16, y, 0, r, r, 28, 0, 40); }
    for (const k of [2, 3]) for (let t = 0.05; t <= 1.3; t += 0.075) { const r = 0.085 - 0.035 * t / 1.3; ring(fig, 0, -t, 0, r, r, 24, k, 36); }
    // eyes on the figure's face (kind 1: lit by the vocal while it dances)
    for (const sx of [-1, 1]) for (let i = 0; i < 70; i++) {
      const a = R() * TAU, r = 0.03 * Math.sqrt(R());
      P(fig, sx * 0.075 + Math.cos(a) * r, 1.78 + Math.sin(a) * r * 0.6, 0.2 + 0.005 * R(), 1);
    }

    // corridor: square frames rushing at the camera, dust on the walls
    const cor = mk(), CW = 2.4, CH = 1.6;
    for (let i = 0; i < 16; i++) {
      const z = -i * 1.5;
      L(cor, -CW, -CH, z, CW, -CH, z, 4); L(cor, CW, -CH, z, CW, CH, z, 4);
      L(cor, CW, CH, z, -CW, CH, z, 4); L(cor, -CW, CH, z, -CW, -CH, z, 4);
      for (let j = 0; j < 90; j++) { const e = R() * 4 | 0, f = R() * 2 - 1; P(cor, e < 2 ? f * CW : (e === 2 ? CW : -CW), e < 2 ? (e ? CH : -CH) : f * CH, z, 4); }
    }
    for (let i = 0; i < 5000; i++) {
      const f = R() * 2 - 1, z = -R() * 24;
      if (R() < 0.5) P(cor, R() < 0.5 ? -CW : CW, f * CH, z, 4); else P(cor, f * CW, R() < 0.5 ? -CH : CH, z, 4);
    }

    // monolith: a black slab lit by a scan line, standing on an LED floor
    const mon = mk(), MX = 0.6, MY = 4.2, MZ = 0.15;
    for (const [a, b, c, x, y, z] of [
      [-MX, 0, -MZ, MX, 0, -MZ], [-MX, MY, -MZ, MX, MY, -MZ], [-MX, 0, MZ, MX, 0, MZ], [-MX, MY, MZ, MX, MY, MZ],
      [-MX, 0, -MZ, -MX, MY, -MZ], [MX, 0, -MZ, MX, MY, -MZ], [-MX, 0, MZ, -MX, MY, MZ], [MX, 0, MZ, MX, MY, MZ],
      [-MX, 0, -MZ, -MX, 0, MZ], [MX, 0, -MZ, MX, 0, MZ], [-MX, MY, -MZ, -MX, MY, MZ], [MX, MY, -MZ, MX, MY, MZ]]) L(mon, a, b, c, x, y, z, 0);
    for (let i = 0; i < 34; i++) for (let j = 0; j < 118; j++) P(mon, -MX + (i + 0.5 + (R() - 0.5) * 0.3) * 2 * MX / 34, (j + 0.5) * MY / 118, MZ + 0.01, 6);
    for (let i = 0; i < 72; i++) for (let j = 0; j < 72; j++) P(mon, -12 + (i + R() * 0.2) * 24 / 72, 0, -12 + (j + R() * 0.2) * 24 / 72, 5);

    const pack = (g) => {
      const p = new Float32Array(g.p), n = p.length / REC;
      for (let i = n - 1; i > 0; i--) {                      // Fisher-Yates over whole records
        const j = Math.floor(R() * (i + 1));
        for (let c = 0; c < REC; c++) { const t = p[i * REC + c]; p[i * REC + c] = p[j * REC + c]; p[j * REC + c] = t; }
      }
      return { points: p, pointCount: n, lines: new Float32Array(g.l), lineCount: g.l.length / REC };
    };
    return { head: pack(head), figure: pack(fig), corridor: pack(cor), monolith: pack(mon) };
  }

  // Pure: which deck is on air (the louder one, with hysteresis so a fader
  // wobble does not flip it). gains: numbers 0..1 or NaN. Returns "a" | "b" | null.
  function onAirDeck(cur, ga, gb) {
    const a = fin(ga) && ga > 0 ? ga : 0, b = fin(gb) && gb > 0 ? gb : 0;
    if (a < 0.02 && b < 0.02) return null;
    if (cur !== "a" && cur !== "b") return a >= b ? "a" : "b";
    const c = cur === "a" ? a : b, other = cur === "a" ? b : a;
    if (c < 0.02 || other > c * 1.25 + 0.05) return cur === "a" ? "b" : "a";
    return cur;
  }

  // ---- Anyma drop: a long tension (breakdown dip or build) then a hard jump ----
  // Owner: "go FULL on Anyma-style drops, e.g. Anyma & Rebuke 'Syren', where a
  // giant humanoid dances on the drop". Found once per track from the analysis:
  // on an 8-bar phrase line, the 16 bars before hold a real dip (energy low for
  // 8+ bars), the 2 bars before are still low, and the 4 bars after jump high.
  // A flat track, a gradual rise or a build that is already loud do not fire.
  // Genre / artist hints (melodic techno, Afterlife names) relax the thresholds.
  const ANYMA_NAMES = ["anyma", "afterlife", "tale of us", "argy", "cassian", "rebuke", "adam sellouk", "melodic techno"];
  const DROP_T = { plain: { post: 0.72, jump: 0.38, dip: 0.5, low: 8 }, hint: { post: 0.6, jump: 0.28, dip: 0.38, low: 6 } };
  // Pure: text (title, artist, genre, tags) -> the matched hint name or "".
  function anymaHint(text) {
    if (typeof text !== "string" || !text) return "";
    const t = text.normalize ? text.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase() : text.toLowerCase();
    for (const n of ANYMA_NAMES) if (t.includes(n)) return n;
    return "";
  }
  // Pure: [{at, post, tail, dip, low, jump, hint, section}] for one prepped track.
  function anymaDrops(pt, hint) {
    if (!pt || !pt.an || !(pt.eSpan > 0)) return [];
    // a near-flat curve (noise only) normalises to 0..1 too: no drop in it
    if (pt.eSpan < 0.2 * Math.max(Math.abs(pt.eLo), Math.abs(pt.eLo + pt.eSpan))) return [];
    const an = pt.an, pb = an.phrase_boundaries_8bar;
    if (!Array.isArray(pb) || !pb.length) return [];
    const bar = 240 / (fin(an.bpm) && an.bpm > 0 ? an.bpm : 128), th = hint ? DROP_T.hint : DROP_T.plain;
    const out = [];
    for (const p of pb) {
      if (!fin(p) || p < 16 * bar - 1e-6) continue;
      let post = 0; for (let k = 0; k < 4; k++) post += energyAt(pt, p + (k + 0.5) * bar); post /= 4;
      const tail = (energyAt(pt, p - 0.5 * bar) + energyAt(pt, p - 1.5 * bar)) / 2;
      let lo = 1, low = 0;
      for (let k = 0; k < 16; k++) { const e = energyAt(pt, p - (k + 0.5) * bar); if (e < lo) lo = e; if (e < post - 0.3) low++; }
      const jump = post - tail, dip = post - lo;
      if (post < th.post || jump < th.jump || dip < th.dip || low < th.low) continue;
      if (out.length && p - out[out.length - 1].at < 16 * bar) continue;       // one per 16 bars
      const si = lastLE(pt.secStarts, p), sec = si >= 0 ? pt.secs[si] : null;
      const r2 = (x) => Math.round(x * 100) / 100;
      out.push({ at: p, post: r2(post), tail: r2(tail), dip: r2(dip), low, jump: r2(jump), hint: hint || "",
        section: sec && typeof sec.label === "string" ? sec.label : "" });
    }
    return out;
  }
  // Pure: the drop crossed moving prev -> pos (null on a seek / backwards / none).
  function dropCrossed(drops, prev, pos) {
    if (!Array.isArray(drops) || !fin(prev) || !fin(pos) || pos < prev || pos - prev > 1.5) return null;
    for (const d of drops) if (d.at > prev && d.at <= pos) return d;
    return null;
  }
  // Pure: text for the step log.
  function dropEvidence(d) {
    if (!d) return "";
    return `jump ${d.jump} (tail ${d.tail} -> ${d.post}), dip ${d.dip} over ${d.low}/16 bars low`
      + `${d.section ? `, section ${d.section}` : ""}${d.hint ? `, hint ${d.hint}` : ", energy shape only"}`;
  }

  // ---- DANCE: the figure's beat-locked moves (all 0..1 or -1..1) ----
  //   sway  body side to side, an extreme on every beat (alternating)
  //   hit   arms punch on the kick (every beat), decaying
  //   nod   head nod on the backbeat (beats 2 and 4)
  //   pose  a bigger pose on each bar downbeat; poseIdx picks which (4 poses)
  //   amp   energy-scaled amplitude; weight (bass) sinks the body; eye (vocal)
  function danceNew() { return { sway: 0, hit: 0, nod: 0, pose: 0, poseIdx: 0, amp: 0, weight: 0, eye: 0 }; }
  function dancePose(ms, dv, reduced, out) {
    const o = out || danceNew();
    if (!ms || !ms.ok) { o.sway = o.hit = o.nod = o.pose = o.amp = o.weight = o.eye = 0; return o; }
    const bp = clamp01(ms.beatPhase), barP = clamp01(ms.barPhase);
    const inBar = Math.min(3, Math.floor(barP * 4 + 1e-6));
    o.sway = Math.cos(Math.PI * ((ms.beatIdx | 0) + bp));
    o.hit = Math.exp(-bp * 8);
    o.nod = inBar & 1 ? Math.exp(-bp * 6) : 0;
    o.pose = Math.exp(-barP * 10);
    o.poseIdx = (((ms.barIdx | 0) % 4) + 4) % 4;
    o.amp = (0.35 + 0.65 * clamp01(ms.energy)) * (reduced ? 0.3 : 1);
    o.weight = dv ? clamp01(dv.weight) : 0;
    o.eye = dv ? clamp01(dv.eye) : 0;
    return o;
  }
  // arm angles per pose [left, right] (radians added to the base arm swing)
  const POSES = [[0.9, 0.9], [1.6, -0.2], [-0.2, 1.6], [2.2, 2.2]];

  // ---- SHOW AUTO: the AI picks stage (full) or window itself ----
  // Pure director. Only acts while SHOW is on, SHOW AUTO is checked and the
  // autopilot drives. FULL on big moments (set start, supermove, peak move,
  // merge -> hold, drop, Anyma drop), WINDOW in calm / low breakdown phrases.
  // Switches land on 8-bar phrase lines (a moment in the line's first bar
  // counts as on the line, a later one waits for the next line), a mode holds
  // 16+ bars, one switch per 32 bars (a supermove may break that). User input
  // or Esc: window now, auto paused 2 minutes. AI stops driving: window now.
  const AUTO = { MIN_BARS: 16, GAP_BARS: 32, START_BARS: 32, FULL_BARS: 16, PAUSE_S: 120, LATCH_BARS: 8 };
  function autoNew() {
    return { mode: "pip", at: -Infinity, lastSwitch: -Infinity, holdUntil: -Infinity, pausedUntil: -Infinity,
      driving: false, started: false, pending: null, pendingAt: -Infinity, lastPhrase: null, why: "" };
  }
  function autoSwitch(a, mode, why, now, evidence) {
    a.mode = mode; a.at = now; a.lastSwitch = now; a.why = why;
    return { mode, why, evidence: evidence || "" };
  }
  // inp: {on, driving, mode (actual "pip"|"full"), ms, moment {kind, evidence}|null,
  //       user (deck / mixer input), esc, manual (the user picked the stage size: pause, keep it)}
  function autoStep(a, inp, now) {
    const i = inp || {};
    if (!i.on) { a.lastPhrase = null; a.pending = null; if (i.mode === "pip" || i.mode === "full") a.mode = i.mode; return null; }
    if (i.user || i.esc) {                       // a.mode is still the mode before this input
      a.pausedUntil = now + AUTO.PAUSE_S; a.pending = null;
      const was = a.mode;
      a.mode = "pip";
      return was === "full" ? autoSwitch(a, "pip", i.esc ? "esc, auto paused 2 min" : "user input, auto paused 2 min", now) : null;
    }
    if (i.mode === "pip" || i.mode === "full") { if (i.mode !== a.mode) a.at = now; a.mode = i.mode; }
    if (i.manual) { a.pausedUntil = now + AUTO.PAUSE_S; a.pending = null; return null; }
    if (!i.driving) {
      const was = a.driving;
      a.driving = false; a.started = false; a.pending = null; a.lastPhrase = null;
      return was && a.mode === "full" ? autoSwitch(a, "pip", "AI stopped driving", now) : null;
    }
    if (!a.driving) { a.driving = true; a.started = false; }
    if (now < a.pausedUntil) { a.lastPhrase = null; return null; }
    const ms = i.ms;
    if (!ms || !ms.ok) return null;
    const bar = 4 * ms.beat, eps = 0.05;
    if (!a.started) { a.started = true; a.pending = { kind: "set start", evidence: "" }; a.pendingAt = Infinity; }
    if (i.moment && i.moment.kind) { a.pending = i.moment; a.pendingAt = now; }
    if (a.pending && a.pendingAt !== Infinity && now - a.pendingAt > AUTO.LATCH_BARS * bar) a.pending = null;
    const newLine = a.lastPhrase !== null && ms.phraseIdx !== a.lastPhrase;
    const first = a.lastPhrase === null;
    a.lastPhrase = ms.phraseIdx;
    const inFirstBar = ms.phrasePhase * 8 < 1;
    const dwellOk = now - a.at >= AUTO.MIN_BARS * bar - eps;
    const gapOk = now - a.lastSwitch >= AUTO.GAP_BARS * bar - eps;
    if (a.pending && (newLine || ((first || i.moment) && inFirstBar))) {
      const p = a.pending, holdBars = p.kind === "set start" ? AUTO.START_BARS : AUTO.FULL_BARS;
      if (a.mode === "full") {
        a.holdUntil = Math.max(a.holdUntil, now + holdBars * bar - eps); a.pending = null;
        // already on the stage: an Anyma drop is still worth a log line (kept: no switch)
        return p.kind === "anyma drop" ? { mode: "full", why: "anyma drop (already full)", evidence: p.evidence || "", kept: true } : null;
      }
      if (dwellOk && (gapOk || p.kind === "supermove")) {
        a.pending = null; a.holdUntil = now + holdBars * bar - eps;
        return autoSwitch(a, "full", p.kind, now, p.evidence);
      }
      return null;
    }
    if (newLine && a.mode === "full" && now >= a.holdUntil && dwellOk && gapOk) {
      const calm = ms.cls === "calm" || (ms.cls === "breakdown" && ms.energy < 0.4);
      if (calm) return autoSwitch(a, "pip", ms.cls === "calm" ? "calm section" : "low-energy breakdown", now);
    }
    return null;
  }

  const core = { SCENES, PREF, HOLD_BARS, BURST_BARS, BURST_EVERY_BEATS, FLASH_GAP_S, DROP_DEDUP_S, QUALITY,
    WORK_BUDGET_MS, DOWN_FRAMES, UP_FRAMES, REC, lastLE, sectionClass, prepTrack, musicState, musicStateNew,
    pickScene, eventTrigger, createDirector, queueEvent, stepDirector, follow, stemsNew, stemsStep, drives,
    qualityNew, qualityStep, cameraPose, buildScenes, onAirDeck, energyAt,
    ANYMA_NAMES, DROP_T, anymaHint, anymaDrops, dropCrossed, dropEvidence, danceNew, dancePose, POSES,
    DANCE_MIN_BARS, DANCE_MAX_BARS, AUTO, autoNew, autoStep };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof document === "undefined" || typeof root.addEventListener !== "function") return;

  // ---- DOM / WebGL glue --------------------------------------------------
  // Thin on purpose: reads deck state and console events through the Host
  // port, feeds the pure core, draws. Nothing is allocated per frame.
  // SHOW (#ap-show-toggle, autopilot drawer) turns it on in a small window;
  // STAGE (#show-stage, top bar) fills the page with it. Default off.
  const doc = root.document;
  const toggle = doc.getElementById("ap-show-toggle"), stageBtn = doc.getElementById("show-stage");
  root.anymaShow = { core };
  if (!toggle && !stageBtn) return;

  const hostOf = () => (root.Engine && root.Engine.host) || null;
  const decksNow = () => { const h = hostOf(); return (h && h.decks) || root.decks || {}; };
  const audioNow = () => {
    const h = hostOf();
    try { if (h) return h.clock.audioNow(); } catch (_) { /* no audio yet */ }
    return typeof audioCtx !== "undefined" ? audioCtx.currentTime : NaN;
  };
  const perf = () => root.performance.now();
  const nowS = () => perf() / 1000;

  const mq = root.matchMedia ? root.matchMedia("(prefers-reduced-motion: reduce)") : null;
  let reduced = !!(mq && mq.matches);
  if (mq && mq.addEventListener) mq.addEventListener("change", (e) => { reduced = e.matches; });

  // stage: canvas + LED-wall overlay + a small control bar + debug readout
  const stage = doc.createElement("div");
  stage.className = "anyma-stage"; stage.hidden = true;
  stage.setAttribute("role", "region"); stage.setAttribute("aria-label", "SHOW stage visuals");
  const cv = doc.createElement("canvas"); cv.setAttribute("aria-hidden", "true");
  const led = doc.createElement("div"); led.className = "anyma-led";
  const bar = doc.createElement("div"); bar.className = "anyma-bar";
  const mkBtn = (label, title, fn) => { const b = doc.createElement("button"); b.type = "button"; b.textContent = label; b.title = title; b.addEventListener("click", fn); bar.appendChild(b); return b; };
  const dbg = doc.createElement("pre"); dbg.className = "anyma-debug"; dbg.hidden = true;
  mkBtn("STATS", "Frame time, quality level and the director's state", () => { dbg.hidden = !dbg.hidden; });
  const sizeBtn = mkBtn("STAGE", "Stage (fill the page) or window", () => setMode(mode === "full" ? "pip" : "full"));
  mkBtn("FULLSCREEN", "Browser full screen", () => {
    if (doc.fullscreenElement) { if (doc.exitFullscreen) doc.exitFullscreen().catch(() => {}); }
    else { setMode("full"); if (stage.requestFullscreen) stage.requestFullscreen().catch(() => {}); }
  });
  mkBtn("OFF", "Turn SHOW off", () => setMode("off"));
  stage.append(cv, led, bar, dbg);
  doc.body.appendChild(stage);

  // ---- state (all allocated once) ----
  const dir = core.createDirector((Date.now() & 0xffff) | 1);
  const ms = core.musicStateNew(), st = core.stemsNew(), q = core.qualityNew();
  const dv = { pulse: 0, weight: 0, eye: 0, colour: 0, intensity: 0 };
  const raw = { drums: 0, bass: 0, vocals: 0, other: 0 };
  const STEMS = ["drums", "bass", "vocals", "other"];
  const tracks = { a: { an: null, pt: null, drops: [], prev: NaN }, b: { an: null, pt: null, drops: [], prev: NaN } };
  const dance = core.danceNew();
  let danceAmt = 0;
  // SHOW AUTO (#ap-show-auto, VISUALS group, default on): the AI picks stage / window
  const autoBox = doc.getElementById("ap-show-auto");
  const auto = core.autoNew();
  let userHit = false, escHit = false, manualHit = false, evMoment = null, dropMoment = null, mergePh = null;
  const autoIn = { on: false, driving: false, mode: "off", ms, moment: null, user: false, esc: false, manual: false };
  const pose = new Float64Array(6), mvp = new Float32Array(16), proj = new Float32Array(16), view = new Float32Array(16);
  let mode = "off", active = false, gl = null, prog = null, U = null, A = null, geo = null, raf = 0, lastT = 0;
  let onAir = null, lastCueT = -Infinity, arm = 0, travel = 0, psize = 2, W = 1, H = 1;
  let fpsN = 0, fpsT = 0, fps = 0, workAvg = 0, dbgT = 0;

  // ---- console events -> director queue ----
  const onEvt = (type) => (e) => {
    if (!active) return;
    const now = nowS(), an = audioNow();
    const ev = core.eventTrigger(type, e && e.detail, now, (at) => (Number.isFinite(an) ? now + (at - an) : now));
    if (!ev) return;
    if (ev.type === "transition") lastCueT = now;
    if (type === "ai-cue" && e && e.detail && e.detail.kind === "peak") evMoment = { kind: "peak move", evidence: e.detail.why || "" };
    core.queueEvent(dir, ev);
  };
  const h0 = hostOf();
  for (const t of ["ai-cue", "ai-supermove", "ai-activity"]) {
    if (h0 && h0.bus) h0.bus.on(t, onEvt(t)); else root.addEventListener(t, onEvt(t));
  }

  // ---- deck reading ----
  const gainOf = (n) => (n && n.gain && Number.isFinite(n.gain.value) ? n.gain.value : 1);
  const deckGain = (d) => (d && d.playing ? gainOf(d.crossfaderGain) * gainOf(d.volumeGain) : 0);
  function pickDeck() {
    const ds = decksNow(), id = core.onAirDeck(onAir, deckGain(ds.a), deckGain(ds.b));
    // a hand mix (no transition cue booked lately) still gets its long dissolve
    if (id !== onAir && onAir && id && nowS() - lastCueT > 20) core.queueEvent(dir, { type: "transition", at: nowS() });
    onAir = id;
    return id ? ds[id] : null;
  }
  function trackOf(id, d) {
    const tr = tracks[id];
    if (tr.an !== d.analysis) {
      tr.an = d.analysis; tr.pt = d.analysis ? core.prepTrack(d.analysis) : null; tr.prev = NaN;
      tr.drops = tr.pt ? core.anymaDrops(tr.pt, core.anymaHint(hintText(id, d))) : [];
    }
    return tr.pt;
  }
  // title / genre / tags the library knows, for the Anyma-drop hint (read once per track)
  function hintText(id, d) {
    const an = d.analysis || {}, el = doc.getElementById(`title-${id}`);
    const tags = Array.isArray(an.tags) ? an.tags.join(" ") : "";
    return [el ? el.textContent : "", an.genre, an.artist, an.title, tags, d.trackName].filter((x) => typeof x === "string").join(" ");
  }
  // merge -> hold on the booked Stem Merge (the vibe strip's phase math)
  function mergeHold() {
    const ap = root.autopilotState, vc = root.vibeUi && root.vibeUi.core, ds = decksNow();
    const out = ap && ap.active ? ds[ap.activeDeck] : null, inn = out ? (out === ds.a ? ds.b : ds.a) : null;
    const plan = inn && ap.next && /stem merge/i.test(ap.next.recipe || "") ? inn._mergePlan : null;
    if (!plan || !vc || typeof out._currentPosition !== "function") { mergePh = null; return null; }
    const ph = vc.mergePhase(plan, (out._currentPosition() - plan.aT) / (240 / (out.bpm || 128)));
    const hit = mergePh === "merge" && ph === "hold";
    mergePh = ph;
    return hit ? { kind: "merge -> hold", evidence: (plan.pick && plan.pick.label) || "" } : null;
  }
  function stepAuto(now) {
    const ap = root.autopilotState;
    let moment = dropMoment || evMoment || mergeHold();
    if (!moment && dir.last === "supermove") moment = { kind: "supermove", evidence: "" };
    else if (!moment && dir.last === "drop") moment = { kind: "drop", evidence: ms.section || "" };
    dropMoment = evMoment = null;
    autoIn.on = !!(autoBox && autoBox.checked) && active; autoIn.driving = !!(ap && ap.active === true);
    autoIn.mode = mode; autoIn.moment = moment; autoIn.user = userHit; autoIn.esc = escHit; autoIn.manual = manualHit;
    const r = core.autoStep(auto, autoIn, now);
    userHit = escHit = manualHit = false;
    if (!r) return;
    if (r.mode !== mode) setMode(r.mode);
    const line = `show: ${r.mode === "full" ? "full" : "window"}: ${r.why}`;
    if (typeof root.aiStep === "function") root.aiStep("show", { decision: line, why: r.evidence || r.why, phase: "show" });
  }
  // RMS of each decoded stem around the play position (read-only, strided)
  function readStems(d, pos) {
    const s = d.stems;
    if (!s || !Number.isFinite(pos)) return false;
    const k = s.ratio || 1, lag = s.lag || 0, ss = d.stemState && typeof d.stemState === "object" ? d.stemState : null;
    for (let i = 0; i < 4; i++) {
      const n = STEMS[i], b = s[n];
      if (!b || typeof b.getChannelData !== "function") return false;
      const ch = b.getChannelData(0), i0 = Math.max(0, Math.floor((pos + lag) * k * b.sampleRate)), i1 = Math.min(ch.length, i0 + 2048);
      let sum = 0, c = 0;
      for (let j = i0; j < i1; j += 8) { sum += ch[j] * ch[j]; c++; }
      const g = ss && Number.isFinite(ss[n]) ? ss[n] : 1;
      raw[n] = c ? Math.sqrt(sum / c) * g : 0;
    }
    return true;
  }

  // ---- WebGL ----
  const VS = `precision mediump float;
attribute vec3 a_p; attribute vec2 a_kr;
uniform mat4 u_mvp;
uniform float u_t, u_scatter, u_pulse, u_weight, u_eye, u_arm, u_travel, u_scan, u_glitch, u_psize, u_colour;
uniform float u_dance, u_sway, u_nod, u_hit, u_armL, u_armR, u_sink;
varying float v_b; varying float v_c;
float h(float n) { return fract(sin(n) * 43758.5453); }
void main() {
  vec3 p = a_p; float k = a_kr.x, r = a_kr.y;
  float b = 0.55 + 0.45 * h(r * 91.7), c = 0.0;
  if (k > 1.5 && k < 3.5) {
    float s = k < 2.5 ? 1.0 : -1.0, a = s * (u_arm + u_dance * (k < 2.5 ? u_armL : u_armR)), ca = cos(a), sa = sin(a);
    p = vec3(ca * p.x - sa * p.y, sa * p.x + ca * p.y, p.z) + vec3(s * 0.43, 1.28, 0.0);
  }
  if (u_dance > 0.001 && k < 3.5) {
    // DANCE (figure only): bend with the sway, sink on the kick and the bass, nod the head
    float hy = clamp((p.y + 1.9) / 3.85, 0.0, 1.0);
    p.x += u_dance * u_sway * 0.3 * hy * hy;
    p.y -= u_dance * (0.07 * u_hit + 0.12 * u_sink) * hy;
    if (p.y > 1.5 && k < 1.5) { float nd = u_dance * u_nod * 0.4 * (p.y - 1.5); p.z += nd; p.y -= nd * 0.35; }
  }
  if (k < 3.5 || k > 6.5) { p *= 1.0 + 0.035 * u_weight + 0.012 * sin(u_t * 0.5); b *= 0.55 + 0.6 * u_pulse; }
  if (k > 0.5 && k < 1.5) { b = 0.25 + 1.8 * u_eye; c = 1.0; }
  if (k > 3.5 && k < 4.5) { p.z = mod(p.z + u_travel, 24.0) - 22.0; b *= smoothstep(2.0, 0.3, p.z) * smoothstep(-22.0, -14.0, p.z) * (0.6 + 0.5 * u_pulse); }
  if (k > 4.5 && k < 5.5) { b *= 0.18 + 0.9 * u_pulse * exp(-length(p.xz) * 0.22); c = 0.6 * u_pulse; }
  if (k > 5.5 && k < 6.5) { float e = exp(-abs(p.y - u_scan) * 5.0); b *= 0.12 + 1.2 * e + 0.25 * u_weight; c = e; }
  if (k > 6.5) b *= 0.6;
  vec3 dir = normalize(vec3(h(r * 12.9) - 0.5, h(r * 78.2) - 0.5, h(r * 37.7) - 0.5) + 1e-4);
  p += dir * u_scatter * (1.5 + 5.0 * h(r * 5.3));
  p.y += u_scatter * u_scatter * 1.2 * h(r * 3.3);
  b *= 1.0 - 0.6 * u_scatter;
  if (u_glitch > 0.01) { float band = floor(p.y * 5.0 + floor(u_t * 24.0) * 1.7); if (h(band) > 0.72) p.x += (h(band * 3.1) - 0.5) * u_glitch * 0.9; }
  gl_Position = u_mvp * vec4(p, 1.0);
  gl_PointSize = clamp(u_psize * (0.6 + 0.8 * h(r * 3.1)) * (1.0 + 0.6 * u_colour * h(r * 9.9)) / max(gl_Position.w, 0.1), 1.0, 14.0);
  v_b = b; v_c = max(c, u_colour * 0.35 * h(r * 2.2));
}`;
  const FS = `precision mediump float;
uniform float u_line, u_alpha, u_red, u_flash;
varying float v_b; varying float v_c;
void main() {
  float a = 1.0;
  if (u_line < 0.5) { vec2 q = gl_PointCoord - 0.5; a = smoothstep(0.25, 0.0, dot(q, q)); }
  vec3 col = mix(vec3(0.78, 0.9, 1.0), vec3(0.25, 0.85, 1.0), clamp(v_c, 0.0, 1.0));
  col = mix(col, vec3(1.0, 0.12, 0.18), u_red * 0.75);
  gl_FragColor = vec4(col * v_b * a * u_alpha * (1.0 + u_flash), 1.0);
}`;
  const UNIFORMS = ["u_mvp", "u_t", "u_scatter", "u_pulse", "u_weight", "u_eye", "u_arm", "u_travel", "u_scan",
    "u_glitch", "u_psize", "u_colour", "u_line", "u_alpha", "u_red", "u_flash",
    "u_dance", "u_sway", "u_nod", "u_hit", "u_armL", "u_armR", "u_sink"];

  function initGL() {
    const g = cv.getContext("webgl", { alpha: false, antialias: false, depth: false, stencil: false, powerPreference: "high-performance" });
    if (!g) return false;
    const sh = (type, src) => {
      const s = g.createShader(type); g.shaderSource(s, src); g.compileShader(s);
      if (!g.getShaderParameter(s, g.COMPILE_STATUS)) { console.warn("SHOW shader:", g.getShaderInfoLog(s)); return null; }
      return s;
    };
    const vs = sh(g.VERTEX_SHADER, VS), fs = sh(g.FRAGMENT_SHADER, FS);
    if (!vs || !fs) return false;
    const p = g.createProgram(); g.attachShader(p, vs); g.attachShader(p, fs); g.linkProgram(p);
    if (!g.getProgramParameter(p, g.LINK_STATUS)) { console.warn("SHOW program:", g.getProgramInfoLog(p)); return false; }
    g.useProgram(p);
    U = {}; for (const n of UNIFORMS) U[n] = g.getUniformLocation(p, n);
    A = { p: g.getAttribLocation(p, "a_p"), kr: g.getAttribLocation(p, "a_kr") };
    g.enableVertexAttribArray(A.p); g.enableVertexAttribArray(A.kr);
    g.enable(g.BLEND); g.blendFunc(g.ONE, g.ONE); g.disable(g.DEPTH_TEST);
    const data = core.buildScenes(0x5eed);
    geo = {};
    for (const name of core.SCENES) {
      const s = data[name], pb = g.createBuffer(), lb = g.createBuffer();
      g.bindBuffer(g.ARRAY_BUFFER, pb); g.bufferData(g.ARRAY_BUFFER, s.points, g.STATIC_DRAW);
      g.bindBuffer(g.ARRAY_BUFFER, lb); g.bufferData(g.ARRAY_BUFFER, s.lines, g.STATIC_DRAW);
      geo[name] = { pb, pn: s.pointCount, lb, ln: s.lineCount };
    }
    gl = g; prog = p;
    return true;
  }
  cv.addEventListener("webglcontextlost", (e) => { e.preventDefault(); setMode("off"); gl = null; });

  // layout is read here only (toggle, window resize, quality step), never per frame
  function resize() {
    const dpr = Math.min(root.devicePixelRatio || 1, 1.5), res = core.QUALITY[q.level].res;
    const r = stage.getBoundingClientRect();
    W = Math.max(1, Math.round((r.width || root.innerWidth) * dpr * res));
    H = Math.max(1, Math.round((r.height || root.innerHeight) * dpr * res));
    cv.width = W; cv.height = H;
    psize = 9 * H / 900;                                    // px at 1 unit from the camera
  }
  root.addEventListener("resize", () => { if (active) resize(); });
  doc.addEventListener("fullscreenchange", () => { if (active) resize(); });

  // column-major perspective * lookAt, written into mvp
  function camera(scene, shot, shotT) {
    core.cameraPose(scene, shot, shotT, reduced, pose);
    const ex = pose[0], ey = pose[1], ez = pose[2];
    let fx = pose[3] - ex, fy = pose[4] - ey, fz = pose[5] - ez;
    let l = Math.hypot(fx, fy, fz) || 1; fx /= l; fy /= l; fz /= l;
    let sx = -fz, sy = 0, sz = fx;                              // f x up(0,1,0)
    l = Math.hypot(sx, sz) || 1; sx /= l; sz /= l;
    const ux = sy * fz - sz * fy, uy = sz * fx - sx * fz, uz = sx * fy - sy * fx;
    view[0] = sx; view[1] = ux; view[2] = -fx; view[3] = 0;
    view[4] = sy; view[5] = uy; view[6] = -fy; view[7] = 0;
    view[8] = sz; view[9] = uz; view[10] = -fz; view[11] = 0;
    view[12] = -(sx * ex + sy * ey + sz * ez); view[13] = -(ux * ex + uy * ey + uz * ez); view[14] = fx * ex + fy * ey + fz * ez; view[15] = 1;
    const f = 1 / Math.tan(0.44), n = 0.1, far = 80;
    proj.fill(0); proj[0] = f / (W / H); proj[5] = f; proj[10] = (far + n) / (n - far); proj[11] = -1; proj[14] = 2 * far * n / (n - far);
    for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
      mvp[c * 4 + r] = proj[r] * view[c * 4] + proj[4 + r] * view[c * 4 + 1] + proj[8 + r] * view[c * 4 + 2] + proj[12 + r] * view[c * 4 + 3];
    }
  }
  function bindRec(buf) {
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.vertexAttribPointer(A.p, 3, gl.FLOAT, false, core.REC * 4, 0);
    gl.vertexAttribPointer(A.kr, 2, gl.FLOAT, false, core.REC * 4, 12);
  }
  function drawScene(name, shot, shotT, scatter, alpha) {
    const g = geo[name];
    if (!g || alpha < 0.01) return;
    camera(name, shot, shotT);
    gl.uniformMatrix4fv(U.u_mvp, false, mvp);
    gl.uniform1f(U.u_scatter, scatter);
    gl.uniform1f(U.u_dance, name === "figure" ? danceAmt : 0);
    const am = alpha * (0.4 + 0.6 * dv.intensity);
    bindRec(g.pb); gl.uniform1f(U.u_line, 0); gl.uniform1f(U.u_alpha, 0.33 * am);
    gl.drawArrays(gl.POINTS, 0, Math.floor(g.pn * core.QUALITY[q.level].pts));
    const la = 0.22 * am * (1 - scatter) * (1 - scatter);
    if (g.ln && la > 0.005) { bindRec(g.lb); gl.uniform1f(U.u_line, 1); gl.uniform1f(U.u_alpha, la); gl.drawArrays(gl.LINES, 0, g.ln); }
  }

  // ---- loop: only while SHOW is on and the page is visible ----
  function wake() { if (!raf && active && gl && !doc.hidden) raf = root.requestAnimationFrame(frame); }
  function frame() {
    raf = 0;
    if (!active || !gl || doc.hidden) return;
    raf = root.requestAnimationFrame(frame);
    const t0 = perf(), now = t0 / 1000;
    const gapMs = lastT ? (now - lastT) * 1000 : 1000 / 60;
    const dt = lastT ? Math.min(0.1, now - lastT) : 1 / 60;
    lastT = now;

    const d = pickDeck(), pt = d ? trackOf(onAir, d) : null;
    const pos = d && typeof d._currentPosition === "function" ? d._currentPosition() : NaN;
    core.musicState(pt, pos, d && d.bpm, ms);
    // Anyma drop crossed on the on-air deck: the figure dances, SHOW AUTO may go full
    const tr = d ? tracks[onAir] : null;
    if (tr) {
      const dr = ms.ok ? core.dropCrossed(tr.drops, tr.prev, pos) : null;
      tr.prev = ms.ok ? pos : NaN;
      if (dr) { core.queueEvent(dir, { type: "anyma", at: now }); dropMoment = { kind: "anyma drop", evidence: core.dropEvidence(dr) }; }
    }
    core.stepDirector(dir, ms, now, dt, reduced);
    stepAuto(now);
    core.stemsStep(st, d && ms.ok && readStems(d, pos) ? raw : null, dt);
    core.drives(ms, st, dv);

    // scene drives: arms rise with a build, corridor speed follows energy, scan line once per bar
    const armTo = !ms.ok ? 0.1 : ms.cls === "build" ? 0.2 + 0.6 * ms.phrasePhase : ms.cls === "drop" ? 0.95 : ms.cls === "groove" ? 0.3 : 0.08;
    arm = core.follow(arm, armTo, dt, 0.35, 1.2);
    travel = (travel + dt * (reduced ? 0.4 : 1) * (1.2 + (ms.ok ? 9 * ms.energy : 0) + 4 * dv.pulse)) % 24;
    const scan = 4.2 * (1 - (ms.ok ? ms.barPhase : (now * 0.25) % 1));

    gl.viewport(0, 0, W, H);
    const fl = reduced ? 0 : Math.min(0.5, dir.flash * 0.5);             // tinted, never white
    gl.clearColor(fl * 0.62 + dir.red * fl * 0.3, fl * 0.72, fl * 0.8, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.uniform1f(U.u_t, now % 3600); gl.uniform1f(U.u_pulse, dv.pulse); gl.uniform1f(U.u_weight, dv.weight);
    gl.uniform1f(U.u_eye, dv.eye); gl.uniform1f(U.u_colour, dv.colour); gl.uniform1f(U.u_arm, 0.2 + 2.3 * arm);
    gl.uniform1f(U.u_travel, travel); gl.uniform1f(U.u_scan, scan); gl.uniform1f(U.u_glitch, dir.glitch);
    gl.uniform1f(U.u_psize, psize); gl.uniform1f(U.u_red, dir.red);
    gl.uniform1f(U.u_flash, (reduced ? 0 : dir.flash * 0.6) + dir.accent * 0.25);
    // DANCE: beat-locked pose of the figure (only drawn into the figure scene)
    core.dancePose(ms, dv, reduced, dance);
    danceAmt = dir.dance * dance.amp;
    const P = core.POSES[dance.poseIdx];
    gl.uniform1f(U.u_sway, dance.sway); gl.uniform1f(U.u_nod, dance.nod); gl.uniform1f(U.u_hit, dance.hit);
    gl.uniform1f(U.u_armL, P[0] * dance.pose + 0.6 * dance.hit); gl.uniform1f(U.u_armR, P[1] * dance.pose + 0.6 * dance.hit);
    gl.uniform1f(U.u_sink, dance.weight);
    if (dir.prev) drawScene(dir.prev, dir.prevShot, dir.prevShotT, dir.mix, 1 - dir.mix);
    drawScene(dir.scene, dir.shot, dir.shotT, dir.prev ? 1 - dir.mix : dir.assemble * 0.8, dir.prev ? dir.mix : 1);

    const work = perf() - t0;
    if (core.qualityStep(q, work, gapMs, 1000 / 60)) resize();
    workAvg = workAvg * 0.95 + work * 0.05; fpsN++;
    if (now - fpsT >= 1) { fps = fpsN / (now - fpsT); fpsN = 0; fpsT = now; }
    if (!dbg.hidden && now - dbgT > 0.25) {
      dbgT = now;
      dbg.textContent = `${fps.toFixed(0)} fps  work ${workAvg.toFixed(2)} ms  quality ${q.level} (${W}x${H})\n`
        + `scene ${dir.scene}/${dir.shot}${dir.prev ? ` <- ${dir.prev} ${(dir.mix * 100).toFixed(0)}%` : ""}  last ${dir.last || "-"}\n`
        + `deck ${onAir || "-"}  ${ms.ok ? `${ms.cls} bar ${ms.barIdx} phrase ${ms.phraseIdx} e ${ms.energy.toFixed(2)}${ms.vocal ? " vocal" : ""}` : "idle"}\n`
        + `stems ${st.live ? "live" : "grid"}  pulse ${dv.pulse.toFixed(2)} weight ${dv.weight.toFixed(2)} eye ${dv.eye.toFixed(2)}\n`
        + `auto ${autoBox && autoBox.checked ? `${auto.mode} (${auto.why || "-"})${now < auto.pausedUntil ? " paused" : ""}` : "off"}`
        + `  dance ${dir.dance.toFixed(2)}  drops ${onAir ? tracks[onAir].drops.length : 0}`;
    }
  }

  // mode: "off" | "pip" (small window over the console) | "full" (stage)
  function setMode(m) {
    if (m !== "off" && m !== "pip" && m !== "full") return;
    if (m !== "off" && !gl && !initGL()) {
      m = "off";
      if (toggle) toggle.title = "SHOW needs WebGL, which this browser did not give";
      if (typeof setStatus === "function") setStatus("SHOW needs WebGL");
    }
    mode = m; active = m !== "off";
    stage.hidden = !active;
    stage.classList.toggle("anyma-full", m === "full");
    sizeBtn.textContent = m === "full" ? "WINDOW" : "STAGE";
    if (toggle) toggle.checked = active;
    if (stageBtn) { stageBtn.setAttribute("aria-pressed", String(m === "full")); stageBtn.classList.toggle("vfx-on", m === "full"); }
    if (m !== "full" && doc.fullscreenElement === stage && doc.exitFullscreen) doc.exitFullscreen().catch(() => {});
    if (active) { resize(); lastT = 0; fpsT = nowS(); wake(); return; }
    if (raf) root.cancelAnimationFrame(raf);
    raf = 0; dir.queue.length = 0;
  }
  if (toggle) { toggle.checked = false; toggle.addEventListener("change", () => setMode(toggle.checked ? (mode === "full" ? "full" : "pip") : "off")); }
  if (stageBtn) stageBtn.addEventListener("click", () => { manualHit = true; setMode(mode === "full" ? "pip" : "full"); });
  // the stage bar's own buttons are the user's choice too: SHOW AUTO pauses 2 minutes
  bar.addEventListener("click", () => { manualHit = true; }, true);
  // Esc leaves the stage for the window (the browser's own full screen eats the first Esc)
  root.addEventListener("keydown", (e) => { if (mode === "full" && e.key === "Escape") { escHit = true; setMode("pip"); } });
  // any hand on the decks or the mixer: back to the window, SHOW AUTO pauses 2 minutes
  const onHand = (e) => {
    if (!e.isTrusted || !active || !e.target || typeof e.target.closest !== "function") return;
    if (e.target.closest(".deck-panel, .mixer")) userHit = true;
  };
  for (const t of ["pointerdown", "input"]) doc.addEventListener(t, onHand, { capture: true, passive: true });
  doc.addEventListener("visibilitychange", () => {
    if (doc.hidden) { if (raf) root.cancelAnimationFrame(raf); raf = 0; lastT = 0; } else wake();
  });
  root.anymaShow = { core, setMode, get mode() { return mode; } };
})(typeof window !== "undefined" ? window : globalThis);
