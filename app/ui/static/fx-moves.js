// AI Music Brain - ARTIST MOVES, batch A (FX, tails, risers): the artist-signature techniques of
// research/notes/artist-signature-techniques.md that live in the FX path, one checkbox each
// (ap-fx-<id>, all on by default; none of these is GUESS-only in the note):
//   mid_blend        S2  melodic long blend: the incoming deck's mids dipped until A's melody
//                        resolves, a reverb swell on A's exit (Anyma / Afterlife, mechanism SECONDARY)
//   wet_band         S3  every reverb / echo send band-limited to the mids (Angello, SOURCED;
//                        fx-rack.js does the filtering, bandEdges here picks the edges)
//   kick_roll        S4  accelerating kick retrigger into the drop, one kick slice of the drum
//                        stem on a shrinking grid (Angello / Cox, SOURCED)
//   vocal_throw      S5  the last word of A's vocal line thrown into a stem echo as the vocal
//                        stem mutes, the tail dead before B sings (Serato stem FX, SOURCED)
//   sweep_dir        S6  outgoing filter sweep direction from B's first-bars brightness (SECONDARY)
//   supermove_replay     OWNER DECISION: the only rewind / spinback the autopilot may play. After a
//                        big NULL-BOT supermove (events "ai-supermove" / "ai-cue", the same table as
//                        mascot.js supermoveFor) the deck brakes on the phrase line and restarts on the
//                        supermove's own downbeat, once per song, rarely.
//
// Every decision is a pure function of measured inputs (`core`, node-tested): stem energies per bar,
// the vocal envelope, the drum stem's loudest kick, a buffer's RMS frequency, the energy curve.
// A number that is not measured is a named constant listed in the plan's `fallbacks` and logged.
// The runtime reaches the world only through the Host port (engine.js). Refusals name their gate
// and are logged once per phrase: `artist move <id> skipped: <gate>: <reason>`.
(function (root) {
  "use strict";

  const LM = root.learnedMovesCore || (typeof require === "function" ? require("./learned-moves.js") : null);
  const IDS = ["mid_blend", "wet_band", "kick_roll", "vocal_throw", "sweep_dir", "supermove_replay"];
  const LABEL = { mid_blend: "MID BLEND", wet_band: "MID-ONLY SEND", kick_roll: "KICK ROLL", vocal_throw: "VOCAL THROW",
                  sweep_dir: "SWEEP", supermove_replay: "REWIND REPLAY" };
  const VIS_SUPER = { kick_roll: "t1", vocal_throw: "t0", supermove_replay: "t1" };   // hit time field; others: accent
  const MELODY_KEY_MIN = 0.8;   // G2: overlapped melodies need a +-1 hour / same key match (KB Acapella Overlay)
  const MIN_RMS = 0.01;         // stem-moves INTRO_MIN_RMS (-40 dBFS): below this a stem is not playing
  const UNMASK_SHARE = 0.15;    // S2: A's melody "resolved" once its `other` RMS < 15 % of the section peak
  const WET_PEAK = 0.35;        // S1/S2 wet cap
  const THROW_WET = 0.35;       // S5: echo wet 0.3-0.4
  const TAIL_FLOOR = 0.01;      // S5: the echo tail is below -40 dB before B's first vocal
  const KICK_HZ = 150;          // S4: the kick lives under this (one-pole low-pass for the onset search)
  const SUPERMOVE_WINDOW_S = 8; // mascot.js: one takeover per 8 s
  const REPLAY_COOLDOWN_BARS = 64; // replay: at least 8 phrases between two replays
  const REPLAY_ROOM_BARS = 16;
  const REPLAY_MAX_BARS = 32;
  const REWIND_MAX_S = 0.75;       // hard cap of the brake (one beat at 80 BPM)      // hard cap: the replayed stretch (supermove .. line) is at most 32 bars     // replay: 2 phrases of song left after the replayed section before the exit
  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
  const mean = (a) => (a && a.length ? a.reduce((s, x) => s + (x || 0), 0) / a.length : 0);
  const db = (r) => 20 * Math.log10(Math.max(1e-9, r));

  // ---------------------------------------------------------------- S3: band-limited wet send
  // RMS frequency of samples [s0, s1): sqrt(E[x'^2] / E[x^2]) of the first difference, mapped
  // back through |1 - e^{-jw}| = 2 sin(w/2). A cheap, measured stand-in for the spectral centroid.
  function rmsFreqHz(ch, sr, s0, s1) {
    let ex = 0, ed = 0;
    const a = Math.max(0, s0 | 0), b = Math.min(ch.length - 1, s1 | 0);
    // stride 7 (odd, prime): a regular stride locked to a tone's period would alias it to silence
    for (let j = a; j < b; j += 7) { const x = ch[j], d = ch[j + 1] - x; ex += x * x; ed += d * d; }
    if (!(ex > 1e-9)) return null;
    return (sr / Math.PI) * Math.asin(Math.min(1, Math.sqrt(ed / ex) / 2));
  }
  // Band edges of a wet send from the source's brightness: low edge 250-500 Hz, high 3-6 kHz.
  function bandEdges(centroidHz) {
    if (!(centroidHz > 0)) return { low: 300, high: 4000, fallbacks: ["band 300-4000 Hz (source brightness unmeasured)"] };
    return { low: Math.round(clamp(centroidHz / 6, 250, 500)), high: Math.round(clamp(centroidHz * 2, 3000, 6000)), fallbacks: [] };
  }

  // ---------------------------------------------------------------- S2: mid suppression in a long blend
  // c: { a, b: stemEnergyBars of A and B over the blend (per bar), total, swapBar, camelot, mashupActive }
  function planMidBlend(c) {
    if (c.mashupActive) return { refusal: { gate: "mashup_layer", reason: "a mashup layer is running" } };
    if (!c.a || !c.b || !c.a.other || !c.b.other) return { refusal: { gate: "stems", reason: "no melody-stem measurement on both decks" } };
    if (!(c.camelot >= MELODY_KEY_MIN)) return { refusal: { gate: "key", reason: `Camelot ${c.camelot} < ${MELODY_KEY_MIN}: melodies would clash` } };
    const n = Math.min(c.total, c.a.other.length), swap = Math.min(c.swapBar, n);
    const midA = mean(c.a.other.slice(0, swap));
    const midB = mean(c.b.other.slice(0, swap).map((x, i) => x + (c.b.vocals ? c.b.vocals[i] || 0 : 0)));
    if (midA < MIN_RMS) return { refusal: { gate: "no_melody", reason: "A has no melody in the blend to protect" } };
    if (midB < MIN_RMS) return { refusal: { gate: "silent", reason: "B's mids are silent: nothing to dip" } };
    // B's mids louder than A's melody -> deeper dip; the 6-10 dB range is the note's
    const dipDb = -Math.round(clamp(8 + db(midB / midA), 6, 10) * 10) / 10;
    const peak = Math.max(...c.a.other.slice(0, n));
    const fallbacks = [];
    let unmaskBar = -1;
    for (let i = 1; i <= swap && i < n; i++) if (c.a.other[i] < UNMASK_SHARE * peak) { unmaskBar = i; break; }
    if (unmaskBar < 0) { unmaskBar = swap; fallbacks.push(`unmask on the swap line (bar ${swap}): A's melody never resolved`); }
    return { plan: { dipDb, unmaskBar, midRatioDb: +db(midB / midA).toFixed(1), fallbacks,
      why: `B's mids ${dipDb} dB under A's melody until bar ${unmaskBar}` } };
  }

  // ---------------------------------------------------------------- S4: accelerating kick roll
  // The loudest kick of the drum stem in song [t0, t1): low-passed at KICK_HZ, 5 ms RMS hops.
  // lag / ratio map song time to the stem buffer like deck-controller _startStem.
  function kickSlice(ch, sr, t0, t1, lag = 0, ratio = 1) {
    const hop = 0.005, n = Math.floor((t1 - t0) / hop), a = 1 - Math.exp(-2 * Math.PI * KICK_HZ / sr);
    const s0 = Math.max(0, Math.floor((t0 + lag) * ratio * sr)), step = Math.max(1, Math.round(hop * ratio * sr));
    if (!(n > 2) || s0 >= ch.length) return null;
    const env = new Float64Array(n);
    let y = 0;
    for (let i = 0; i < n; i++) {
      let sum = 0;
      for (let j = s0 + i * step, e = Math.min(ch.length, j + step); j < e; j++) { y += a * (ch[j] - y); sum += y * y; }
      env[i] = Math.sqrt(sum / step);
    }
    let pk = 0;
    for (let i = 1; i < n; i++) if (env[i] > env[pk]) pk = i;
    if (!(env[pk] >= MIN_RMS)) return null;
    let on = pk;
    while (on > 0 && env[on - 1] >= 0.3 * env[pk] && pk - on < 10) on--;
    let off = pk;
    while (off < n - 1 && env[off + 1] >= 0.2 * env[pk]) off++;
    return { from: t0 + on * hop, dur: clamp((off - on + 1) * hop, 0.06, 0.25), peak: env[pk] };
  }
  // Last 8 beats before the drop: kicks on 1/4 notes for 4 beats, 1/8 for 2, 1/16 for 1, then one
  // silent beat; the drop lands on the downbeat. -> slices for deck.stemSlices("drums", ...).
  const ROLL = [[1, 4], [0.5, 2], [0.25, 1]];   // [grid step (beats), beats]
  function planKickRoll(c) {
    if (c.mashupActive) return { refusal: { gate: "mashup_layer", reason: "a mashup layer is running" } };
    if (c.inTransition) return { refusal: { gate: "transition", reason: "a transition is running" } };
    if (!c.kick) return { refusal: { gate: "stems", reason: "no drum stem kick to retrigger" } };
    const beat = c.beatS, start = c.dropT - 8 * beat, slices = [];
    const hits = ROLL.reduce((s, [st, b]) => s + b / st, 0);
    let off = 0, k = 0;
    for (const [st, b] of ROLL) {
      for (let i = 0; i < b / st; i++, k++) {
        slices.push({ from: c.kick.from, dur: Math.min(c.kick.dur, 0.9 * st * beat), off: off + i * st * beat,
                      gain: +(0.7 + 0.3 * k / (hits - 1)).toFixed(3) });
      }
      off += b * beat;
    }
    slices.push({ from: c.kick.from, dur: beat, off, gain: 0 });       // the beat of silence before the drop
    return { plan: { start, slices, beats: 8, hits, kickDur: +c.kick.dur.toFixed(3), fallbacks: [],
      why: `${hits} kick hits 1/4 > 1/8 > 1/16, a beat of silence, drop on the downbeat` } };
  }

  // ---------------------------------------------------------------- S5: vocal echo throw
  // c: { env (A's vocal envelope over the window), lines (learned vocalLines), t0, swapT (song s),
  //      bVocalT (A-song time of B's first vocal, Infinity when none), beatS }
  function planVocalThrow(c) {
    const beat = c.beatS;
    const line = (c.lines || []).filter((l) => l.e > c.t0 + 0.5 && l.e <= c.swapT + beat).pop();
    if (!line) return { refusal: { gate: "no_line", reason: "no vocal line ends before the swap line" } };
    const fallbacks = [];
    // the last word: from the last dip under 40 % of the line's peak to the line end
    let ws = null;
    const i1 = Math.floor((line.e - c.env.t0) / c.env.hop) - 1;
    for (let i = i1; i >= 0 && c.env.t0 + i * c.env.hop > line.s; i--) {
      if (c.env.v[i] < 0.4 * line.peak) { ws = c.env.t0 + (i + 1) * c.env.hop; break; }
    }
    if (ws == null || line.e - ws < 0.15 || line.e - ws > beat) {
      ws = Math.max(line.s, line.e - Math.min(beat, 0.5));
      fallbacks.push("last word = the line's final 0.5 s / beat (no dip found)");
    }
    let gap = c.bVocalT - line.e;
    if (!Number.isFinite(gap)) { gap = 8 * beat; fallbacks.push("tail dies within 2 bars (B has no vocal ahead)"); }
    const delayS = gap < 4 * beat ? beat / 2 : beat;
    if (gap < 2 * delayS) return { refusal: { gate: "gap_short", reason: `${gap.toFixed(2)} s to B's vocal: no room for a tail` } };
    // fb^(gap / delay) <= TAIL_FLOOR: the tail is under -40 dB before B's first vocal
    const feedback = +Math.min(0.6, Math.pow(TAIL_FLOOR, delayS / gap)).toFixed(3);
    return { plan: { wordS: +ws.toFixed(3), lineEnd: +line.e.toFixed(3), delayS, feedback, wet: THROW_WET, gapS: +gap.toFixed(2),
      fallbacks: fallbacks.concat([`wet ${THROW_WET} (note: 0.3-0.4)`]),
      why: `last word into a ${delayS === beat ? "1" : "1/2"}-beat echo, fb ${feedback}, gap ${gap.toFixed(1)} s` } };
  }

  // ---------------------------------------------------------------- S6: sweep direction
  // B brighter than A (or equal): A washes out through a low-pass (out of B's highs); B darker:
  // A thins out through a high-pass (leaves the body to B). Unmeasured: the fixed low-pass.
  function sweepDirection(aHz, bHz) {
    if (!(aHz > 0) || !(bHz > 0)) return { dir: "lowpass", ratio: null, fallbacks: ["fixed low-pass (brightness unmeasured)"] };
    const ratio = +(bHz / aHz).toFixed(3);
    return { dir: ratio < 0.9 ? "highpass" : "lowpass", ratio, fallbacks: [] };
  }

  // ---------------------------------------------------------------- supermove replay
  // mascot.js CUE_MOVES / supermoveFor, mirrored (fx_moves_check.js asserts they agree).
  const CUE_MOVES = [
    ["drop", /strip & rebuild/i, "STRIP & REBUILD"], ["drop", /after the merge/i, "MERGE"],
    ["drop", /after the mashup/i, "MASHUP"], ["drop", /^the beat slams back after /i, "HOOK DROP"],
    ["transition", /^Double Drop:/, "DOUBLE DROP"], ["transition", /^Drop Swap:/, "DROP SWAP"],
    ["line", /^B's rap arrives/, "RIFF OVER RAP"],
  ];
  const deckOf = (x) => (x === "a" || x === "b" ? x : "");
  function supermoveOf(type, d) {
    if (!d || !Number.isFinite(d.at)) return null;
    if (type === "ai-supermove") {
      const name = String(d.name || "").trim().toUpperCase().slice(0, 24);
      return name ? { name, at: d.at, deck: deckOf(d.deck) } : null;
    }
    if (type !== "ai-cue") return null;
    const why = String(d.why || "");
    for (const [kind, re, name] of CUE_MOVES) if (d.kind === kind && re.test(why)) return { name, at: d.at, deck: deckOf(d.deck) };
    return null;
  }
  // c: { songPos (the supermove's song time), lineT (the phrase line one phrase later), bar, now (song
  //      time now), energy / energyQ3 at songPos, inTransition, holding, relaxed, otherPlaying,
  //      exitT (the planned exit, song s), duration, replaysThisSong, sinceLastS (audio s), onGrid }
  function planReplay(c) {
    const no = (gate, reason) => ({ refusal: { gate, reason } });
    if (!c.force && c.relaxed) return no("relaxed", "relaxed session: no replays");
    if (!c.force && c.replaysThisSong > 0) return no("once_per_song", "this song already replayed a supermove");
    if (!c.force && c.sinceLastS < REPLAY_COOLDOWN_BARS * c.barWall) return no("cooldown", `${Math.round(c.sinceLastS / c.barWall)} bars since the last replay (min ${REPLAY_COOLDOWN_BARS})`);
    if (c.inTransition || c.holding) return no("transition", "a transition / merge hold is running");
    if (c.otherPlaying) return no("other_deck", "the other deck is playing: two decks would double the sub");
    if (!c.force && !(c.energy >= c.energyQ3)) return no("energy", "the supermove was not a big moment (energy under the song's upper quartile)");
    if (!c.onGrid) return no("grid", "the supermove is not on the downbeat grid");
    const replayS = c.lineT - c.songPos;
    if (!(replayS >= 4 * c.bar)) return no("short", "less than 4 bars to replay");
    if (replayS > REPLAY_MAX_BARS * c.bar + 0.5) return no("cap", `the supermove is ${Math.round(replayS / c.bar)} bars back (cap ${REPLAY_MAX_BARS})`);
    if (c.lineT + replayS + REPLAY_ROOM_BARS * c.bar > Math.min(c.exitT, c.duration)) return no("room", "no room before the exit for the replayed section");
    if (c.lineT - c.now < 1.5 * c.bar / 4) return no("late", "the phrase line is too close to brake on it");
    return { plan: { line: c.lineT, to: c.songPos, replayS: +replayS.toFixed(3), brakeBeats: 1,
      fallbacks: [`cooldown ${REPLAY_COOLDOWN_BARS} bars`, `room ${REPLAY_ROOM_BARS} bars`],
      why: `brake on the phrase line, back to the ${c.name || "supermove"} downbeat (${(replayS / c.bar).toFixed(0)} bars again)` } };
  }
  // Song-time value of a curve at t (step lookup) and its upper quartile.
  function energyAt(times, curve, t) {
    if (!times || !curve || !times.length) return null;
    let i = 0;
    while (i + 1 < times.length && times[i + 1] <= t) i++;
    return curve[i];
  }

  // On demand (AI ACTIONS): the safety gates only. Toggles, relaxed session, FX budget, cooldown and
  // once-per-song are skipped; a playing analysed deck, no transition, no mashup layer, stems where the
  // move needs them, and one sub-bass owner are not.
  // s: { playing, inTransition, mashupActive, stemsReady, otherOnAir, supermove }
  const NEEDS_STEMS = ["kick_roll", "vocal_throw"];
  function runNowGate(id, s) {
    const no = (gate, reason) => ({ gate, reason });
    if (!IDS.includes(id)) return no("unknown", `no artist move ${id}`);
    if (!s.playing) return no("deck", "no analysed deck is playing");
    if (id !== "mid_blend" && s.inTransition) return no("transition", "a transition or merge hold is running");
    if (id !== "wet_band" && id !== "sweep_dir" && s.mashupActive) return no("mashup_layer", "a mashup layer is running");
    if (NEEDS_STEMS.includes(id) && !s.stemsReady) return no("stems", "this deck's stems aren't ready");
    if (id === "mid_blend" && !s.otherOnAir) return no("no_blend", "mid blend needs both decks on the master");
    if (id === "supermove_replay" && !s.supermove) return no("no_supermove", "no NULL-BOT supermove on this song yet");
    if (id === "supermove_replay" && s.otherOnAir) return no("other_deck", "the other deck is on the master: two subs");
    return null;
  }

  // A REPLAY / LIKED throw (macro-mode.js storedMove): the stored echo (delay, feedback, wet) on the
  // live last word. The live gates keep their say: the tail must still be dead before B's first
  // vocal (fb^(gap/delay) <= TAIL_FLOOR on the LIVE gap), else the live plan's own feedback is kept.
  // plan: planVocalThrow's plan; stored: the logged params {delayS, feedback, wet} -> {plan, why}
  function storedThrow(plan, stored) {
    if (!plan || !stored || typeof stored !== "object") return { plan, why: null };
    const num = (v, lo, hi) => (Number.isFinite(v) && v >= lo && v <= hi ? v : null);
    const delayS = num(stored.delayS, 0.05, 2), wet = num(stored.wet, 0, 1), fb = num(stored.feedback, 0, 0.9);
    const out = Object.assign({}, plan, { fallbacks: (plan.fallbacks || []).slice() });
    const notes = [];
    if (delayS != null) out.delayS = delayS;
    if (wet != null) out.wet = wet;
    if (fb != null) {
      if (Math.pow(fb, plan.gapS / out.delayS) <= TAIL_FLOOR + 1e-9) out.feedback = fb;
      else {
        out.feedback = +Math.min(plan.feedback, Math.pow(TAIL_FLOOR, out.delayS / plan.gapS)).toFixed(3);
        notes.push(`stored fb ${fb} would ring into B's vocal (gap ${plan.gapS} s): fb ${out.feedback}`);
      }
    }
    out.stored = true;
    out.fallbacks = out.fallbacks.concat(notes);
    out.why = `stored throw: last word into a ${out.delayS} s echo, fb ${out.feedback}, wet ${out.wet}, gap ${plan.gapS} s`;
    return { plan: out, why: notes[0] || null };
  }

  const core = { IDS, runNowGate, storedThrow, LABEL, MELODY_KEY_MIN, UNMASK_SHARE, WET_PEAK, THROW_WET, CUE_MOVES, SUPERMOVE_WINDOW_S,
                 REPLAY_COOLDOWN_BARS, REPLAY_ROOM_BARS, REPLAY_MAX_BARS, REWIND_MAX_S, rmsFreqHz, bandEdges, planMidBlend, kickSlice, planKickRoll,
                 planVocalThrow, sweepDirection, supermoveOf, planReplay, energyAt };
  root.fxMovesCore = core;
  if (typeof module !== "undefined" && module.exports) module.exports = core;

  // --------------------------------------------------------------------- runtime (Host port only)
  function create({ host }) {
    const audioCtx = host.audio;
    const { setTimeout, clearTimeout } = host.clock;
    const timers = [];
    const said = new Map();          // "deck:id:phrase" refusals already logged
    const wetAt = { a: -Infinity, b: -Infinity };   // audio time of the last wet FX move per deck
    // On demand (AI ACTIONS, runNow): the toggles, relaxed session and the FX budget are skipped; safety gates are not.
    let forcing = false, lastRefusal = null;
    const on = (id) => forcing || (host.ui.flag("ap-fx-toggle", true) && host.ui.flag(`ap-fx-${id}`, true));
    const later = (ms, fn) => { const t = setTimeout(fn, Math.max(0, ms)); timers.push(t); return t; };
    const relaxed = () => !forcing && !!(host.session && host.session.relaxed);
    const mashup = () => !!(host.mod.mashup && host.mod.mashup.active);
    const rateOf = (d) => (d && d._playbackRate && d._playbackRate()) || 1;
    const posAt = (d, t) => (d._positionAt ? d._positionAt(t) : d._currentPosition() + (t - audioCtx.currentTime) * rateOf(d));

    function refuse(deck, id, gate, reason, phrase) {
      lastRefusal = { gate, reason };
      const k = `${deck}:${id}:${phrase == null ? Math.floor(audioCtx.currentTime / 15) : phrase}`;
      if (said.has(k)) return null;
      if (said.size > 200) said.clear();
      said.set(k, 1);
      console.info(`artist move ${id} skipped: ${gate}: ${reason}`);
      return null;
    }
    function fire(deck, id, plan, extra = {}) {
      const fb = plan.fallbacks && plan.fallbacks.length ? ` (constants: ${plan.fallbacks.join(", ")})` : "";
      console.info(`artist move ${id}: ${plan.why}${fb}`);
      host.bus.emit("ai-activity", Object.assign({ kind: "artist_move", deck, label: `ARTIST MOVE · ${LABEL[id]}`,
        why: plan.why, move: id, fallbacks: plan.fallbacks || [] }, extra));
      // NULL-BOT / SHOW (mascot.js): kick roll hits on its drop, throw on the last word, rewind on the line; the rest pop
      const sup = VIS_SUPER[id], at = sup ? extra[sup] : (Number.isFinite(extra.t0) ? extra.t0 : audioCtx.currentTime);
      if (Number.isFinite(at)) host.bus.emit("vis-moment", { at, name: id === "supermove_replay" ? "REWIND" : LABEL[id], tier: sup ? "super" : "accent", deck });
    }
    // FX budget: at most one wet FX move per phrase and deck; batch D's fxBudget hook, if any, has the last word.
    function wetFree(deck, id, t, phraseS) {
      if (forcing) { wetAt[deck] = t; return true; }
      if (t - wetAt[deck] < phraseS) return refuse(deck, id, "fx_budget", "one wet FX move per phrase") || false;
      const fb = host.mod.fxBudget;
      if (fb && typeof fb.allow === "function" && !fb.allow(deck, id)) return refuse(deck, id, "fx_budget", "set FX budget spent") || false;
      wetAt[deck] = t;
      return true;
    }
    function common(deck, id) {
      if (!on(id)) return refuse(deck, id, "toggle", `ap-fx-${id} is off`) || false;
      if (relaxed()) return refuse(deck, id, "relaxed", "relaxed session: no artist moves") || false;
      return true;
    }

    // S2. t0: blend downbeat (audio s), barS: blend bar (audio s). -> {dipDb, unmaskBar} or null.
    function midBlend(out, inn, o) {
      const od = host.decks[out], id = host.decks[inn], sm = host.mod.stemMoves;
      if (!od || !id || !common(out, "mid_blend")) return null;
      const bars = (d) => (d.stemsReady && sm && sm.stemEnergyBars ? sm.stemEnergyBars(d, posAt(d, o.t0), o.barS * rateOf(d), o.total) : null);
      const djc = host.mod.djMind && host.mod.djMind.core;
      const ka = od.analysis && od.analysis.key && od.analysis.key.camelot, kb = id.analysis && id.analysis.key && id.analysis.key.camelot;
      const r = planMidBlend({ a: bars(od), b: bars(id), total: o.total, swapBar: o.swapBar, mashupActive: mashup(),
        camelot: djc && djc.camelotScore ? djc.camelotScore(ka, kb) : null });
      if (!r.plan) return refuse(out, "mid_blend", r.refusal.gate, r.refusal.reason);
      if (!applyMidDip(id, r.plan, o.t0, o.barS)) return refuse(inn, "mid_blend", "eq", "B has no mid EQ to dip");
      fire(inn, "mid_blend", r.plan, { params: { dipDb: r.plan.dipDb, unmaskBar: r.plan.unmaskBar, midRatioDb: r.plan.midRatioDb },
        t0: o.t0, t1: o.t0 + (r.plan.unmaskBar + 2) * o.barS });
      return r.plan;
    }
    // B's mid band (the deck's 1 kHz peaking EQ) down to dipDb over one bar, held, back over 2 bars from the unmask bar.
    function applyMidDip(d, plan, t0, barS) {
      const p = d && d.midFilter && d.midFilter.gain;
      if (!p) return false;
      const base = p.value, tU = Math.max(t0 + barS, t0 + plan.unmaskBar * barS);
      p.cancelScheduledValues(t0);
      p.setValueAtTime(base, t0);
      p.linearRampToValueAtTime(plan.dipDb, t0 + barS);
      p.setValueAtTime(plan.dipDb, tU);
      p.linearRampToValueAtTime(base, tU + 2 * barS);
      return true;
    }
    // Wet swell on deck `deck`'s FX unit: 0 at t0, `peak` at tPeak, closed at t1 (audio s). Ramps only.
    function swell(deck, type, t0, tPeak, t1, peak, id) {
      const u = host.mod.fxUnits && host.mod.fxUnits[deck];
      if (!u || !u.wetGain || !wetFree(deck, id, t0, (t1 - t0) * 2)) return false;
      u.setType(type); u.setWet(0); u.setActive(true);
      const g = u.wetGain.gain;
      g.cancelScheduledValues(t0);
      g.setValueAtTime(0, t0);
      g.linearRampToValueAtTime(peak, tPeak);
      g.linearRampToValueAtTime(0, t1);
      later((t1 - audioCtx.currentTime) * 1000 + 60, () => { if (u.type === type) { u.setActive(false); u.setType("none"); } });
      return true;
    }
    // S2 exit: reverb swell on A over the last bars of its fade, closed on the phrase line.
    function tailSwell(out, o) {
      if (!host.decks[out] || !on("mid_blend") || relaxed()) return false;
      const t1 = o.t0 + o.total * o.barS, t0 = t1 - 4 * o.barS;
      if (!swell(out, "reverb", t0, t1 - o.barS, t1, WET_PEAK, "mid_blend")) return false;
      fire(out, "mid_blend", { why: `reverb swell to ${WET_PEAK} on A's exit, closed on the phrase line`, fallbacks: [`peak wet ${WET_PEAK}`] },
        { params: { peak: WET_PEAK, bars: 4 } });
      return true;
    }

    // S3: band edges for deck d's wet send, measured on 10 s of its buffer around now.
    function bandFor(d) {
      const b = d && d.buffer;
      if (!b || !b.getChannelData) return bandEdges(null);
      const sr = b.sampleRate, p = d._currentPosition ? d._currentPosition() : 0;
      return bandEdges(rmsFreqHz(b.getChannelData(0), sr, p * sr, (p + 10) * sr));
    }

    // S4: the peak_roll riser of dj-mind as a kick retrigger. dropT: the drop (song s) on deck d.
    // -> {ok, until} when booked, else null (dj-mind falls back to its whole-deck loop roll).
    function kickRoll(deckId, dropT, phrase) {
      const d = host.decks[deckId], sm = host.mod.stemMoves;
      if (!d || !sm || !common(deckId, "kick_roll")) return null;
      const beatS = 60 / (d.bpm || 128), st = d.stems, b = st && st.drums;
      const inTransition = !!(host.mod.djMind && host.mod.djMind.transitioning);
      const kick = d.stemsReady && b && b.getChannelData
        ? kickSlice(b.getChannelData(0), b.sampleRate, dropT - 16 * beatS - 8 * beatS, dropT - 8 * beatS, st.lag || 0, st.ratio || 1) : null;
      const r = planKickRoll({ dropT, beatS, kick, mashupActive: mashup(), inTransition });
      if (!r.plan) return refuse(deckId, "kick_roll", r.refusal.gate, r.refusal.reason, phrase);
      const at = sm.audioAt(d, r.plan.start), lead = at - audioCtx.currentTime;
      if (lead < 0.35) return refuse(deckId, "kick_roll", "late", `the roll starts in ${lead.toFixed(2)} s`, phrase);
      if (!d.stemsLiveAt || !d.stemsLiveAt(at)) return refuse(deckId, "kick_roll", "stems_not_live", "the drum stem is not sounding", phrase);
      const until = at + 8 * beatS / rateOf(d);
      later(lead * 1000 - 250, () => {
        if (!d.playing) return;
        const wasMix = !d.stemState;
        if (wasMix && !d.stemMix({}, at - 0.01, 0.01)) return void console.info("artist move kick_roll skipped: stem mode refused");
        if (!d.stemSlices("drums", r.plan.slices, at)) {
          if (wasMix) d.stemMix(null, at, 0.02);
          return void console.info("artist move kick_roll skipped: slices refused");
        }
        if (wasMix) later((until - audioCtx.currentTime) * 1000 - 200, () => { if (d.playing) d.stemMix(null, until + 0.03, 0.02); });
      });
      fire(deckId, "kick_roll", r.plan, { params: { hits: r.plan.hits, kickDur: r.plan.kickDur, beats: 8 }, t0: at, t1: until });
      return { ok: true, at, until };
    }

    // S5. The last word of A's vocal line into a stem echo (a send tapped off the vocal stem before its
    // fader) as the vocal stem mutes; the tail rides A's FX output and is dead before B sings.
    function vocalThrow(out, inn, o) {
      const od = host.decks[out], id = host.decks[inn], sm = host.mod.stemMoves;
      if (!od || !sm || !LM || !common(out, "vocal_throw")) return false;
      const st = od.stems, vb = st && st.vocals;
      if (!od.stemsReady || !vb || !vb.getChannelData || !od.stemLive || !od.stemLive.vocals)
        return refuse(out, "vocal_throw", "stems", "no vocal stem on A (deck Echo Out instead)") || false;
      const rA = rateOf(od), p0 = posAt(od, o.t0), swapT = p0 + o.swapBar * o.barS * rA, beatS = 60 / (od.bpm || 128);
      const env = LM.envelope(vb.getChannelData(0), vb.sampleRate, p0, swapT + beatS, beatS / 4, st.lag || 0, st.ratio || 1);
      const pB = id && id.analysis ? posAt(id, o.t0) : 0, rB = rateOf(id), bv = (id && id.analysis && id.analysis.vocal_active_regions) || [];
      const next = bv.map((r) => r[0]).filter((s) => s >= pB).sort((x, y) => x - y)[0];
      const bVocalT = next == null ? Infinity : p0 + (next - pB) / rB * rA;
      const r = planVocalThrow({ env, lines: LM.vocalLines(env), t0: p0, swapT, bVocalT, beatS });
      if (!r.plan) return refuse(out, "vocal_throw", r.refusal.gate, r.refusal.reason) || false;
      // a replayed / liked transition performs its stored throw (never set on a normal autopilot pick)
      const mm = host.mod.macroMode, ids = host.state || {};
      const kept = mm && mm.storedMove ? mm.storedMove(out === "a" ? ids.trackA : ids.trackB, inn === "a" ? ids.trackA : ids.trackB, "vocal_throw") : null;
      if (kept && kept.params) r.plan = storedThrow(r.plan, kept.params).plan;
      const tw = o.t0 + (r.plan.wordS - p0) / rA, te = o.t0 + (r.plan.lineEnd - p0) / rA;
      const dlc = root.dropLineCore, dlB = dlc && id && id.analysis ? dlc.deckBusy(id, posAt(id, tw), posAt(id, te + r.plan.gapS / rA)) : null;   // the echo never rides B's drop line
      if (dlB) return refuse(out, "vocal_throw", dlB.gate, dlB.reason) || false;
      if (tw - audioCtx.currentTime < 0.05 || !od.stemsLiveAt || !od.stemsLiveAt(tw)) return refuse(out, "vocal_throw", "stems_not_live", "A's stems are not sounding at the last word") || false;
      if (!wetFree(out, "vocal_throw", tw, 8 * o.barS)) return false;
      const edges = bandFor(od), send = audioCtx.createGain(), delay = audioCtx.createDelay(2), fb = audioCtx.createGain();
      const hp = audioCtx.createBiquadFilter(), lp = audioCtx.createBiquadFilter(), wet = audioCtx.createGain();
      hp.type = "highpass"; hp.frequency.value = edges.low; lp.type = "lowpass"; lp.frequency.value = edges.high;
      delay.delayTime.value = r.plan.delayS / rA; fb.gain.value = r.plan.feedback; wet.gain.value = r.plan.wet;
      od.stemLive.vocals.connect(send); send.connect(delay); delay.connect(fb); fb.connect(delay);
      delay.connect(hp); hp.connect(lp); lp.connect(wet); wet.connect(od.fxOutput);
      send.gain.setValueAtTime(0, audioCtx.currentTime);
      send.gain.setValueAtTime(0, tw);
      send.gain.linearRampToValueAtTime(1, tw + 0.01);
      send.gain.setValueAtTime(1, te);
      send.gain.linearRampToValueAtTime(0, te + 0.03);
      later((te - audioCtx.currentTime) * 1000 - 150, () => { if (od.playing) od.stemMix({ vocals: 0 }, te, 0.03); });
      const tailEnd = te + r.plan.gapS / rA + 1;
      // on demand the song plays on: its voice comes back once the tail has died (a 1-beat ramp)
      if (o.restore) later((te + r.plan.gapS / rA - audioCtx.currentTime) * 1000 - 200, () => { if (od.playing) od.stemMix(null, te + r.plan.gapS / rA, beatS / rA); });
      later((tailEnd - audioCtx.currentTime) * 1000, () => { for (const n of [send, delay, fb, hp, lp, wet]) try { n.disconnect(); } catch (e) { /* gone */ } });
      fire(out, "vocal_throw", r.plan, { params: { delayS: r.plan.delayS, feedback: r.plan.feedback, wet: r.plan.wet, gapS: r.plan.gapS, band: [edges.low, edges.high],
          stored: !!r.plan.stored },
        t0: tw, t1: te + r.plan.gapS / rA });
      return true;
    }

    // S6: sweep direction for the outgoing deck from B's first 4 bars vs A's current 4 bars.
    function sweepDir(out, inn, t0) {
      const od = host.decks[out], id = host.decks[inn];
      if (!od || !id || !common(out, "sweep_dir")) return "lowpass";
      const hz = (d, p) => {
        const b = d.buffer;
        if (!b || !b.getChannelData) return null;
        return rmsFreqHz(b.getChannelData(0), b.sampleRate, p * b.sampleRate, (p + 16 * 60 / (d.bpm || 128)) * b.sampleRate);
      };
      const r = sweepDirection(hz(od, posAt(od, t0)), hz(id, posAt(id, t0)));
      fire(out, "sweep_dir", Object.assign({ why: `${r.dir} on A (B/A brightness ${r.ratio == null ? "?" : r.ratio})` }, r),
        { params: { dir: r.dir, ratio: r.ratio } });
      return r.dir;
    }

    // ---- supermove replay: rewind primitive + planner, hooked on the supermove events.
    const replay = { last: -Infinity, songs: new Map(), pending: [] };
    // Brake deck d over one beat into audio time `line`, restart at song time `to` exactly on `line`.
    // The rewind primitive, the ONLY rewind / spinback the autopilot plays (fx_moves_check.js asserts it
    // never runs without a supermove). A turntable brake over the last beat before the line (a playbackRate
    // ramp, never a step, capped at REWIND_MAX_S), then the deck restarts on the line at song time `to`.
    // The deck's own play(when) starts the new source sample-accurately on the line: no dead air past the brake.
    function rewind(d, line, to) {
      const brakeS = Math.min(REWIND_MAX_S, 60 / ((d.bpm || 128) * rateOf(d)));
      const a = d.analysis;
      later((line - brakeS - audioCtx.currentTime) * 1000, () => {
        if (!d.playing || d.analysis !== a) return;
        if (typeof d._rampToStop === "function") d._rampToStop(Math.max(0.1, line - audioCtx.currentTime - 0.02));
        else d.brake();
      });
      later((line - audioCtx.currentTime) * 1000 - 50, () => { if (d.analysis === a && (d.playing || d._braking)) d.play(to, false, line); });
      replay.rewinds = (replay.rewinds || 0) + 1;
    }
    // Hooked here: host.bus.on("ai-supermove") and host.bus.on("ai-cue") below, the same events mascot.js books
    // its NULL-BOT takeover from (autopilot.js emits "ai-supermove" for LAYER, stem-moves.js / riff-over-rap.js
    // / autopilot.js the "ai-cue" drops); the decision runs 2 beats before the phrase line after the supermove.
    function onSupermove(type, e) {
      const sm = supermoveOf(type, e && e.detail !== undefined ? e.detail : e);
      if (!sm) return;
      const deckId = sm.deck || ["a", "b"].find((k) => host.decks[k] && host.decks[k].playing);
      const d = deckId && host.decks[deckId];
      if (!d || !d.analysis) return;
      const songPos = posAt(d, sm.at), bar = 240 / (d.bpm || 128), rate = rateOf(d);
      replay.pending.push(Object.assign({ songPos, analysis: d.analysis, deckId }, sm));
      if (replay.pending.length > 20) replay.pending.shift();
      const lineT = nextLine(d.analysis, songPos + 8 * bar - bar / 2, songPos + 8 * bar);
      const lineAudio = sm.at + (lineT - songPos) / rate;
      later((lineAudio - audioCtx.currentTime - 2 * bar / 4 / rate) * 1000, () => decide(deckId, d, sm, songPos, lineT, lineAudio, d.analysis, false));
    }
    function nextLine(a, from, dflt) {
      const ph = ((a && a.phrase_boundaries_8bar) || []).filter((t) => t >= from)[0];
      return ph != null ? ph : dflt;
    }
    // -> null when the replay is booked, else the refusal {gate, reason}
    function decide(deckId, d, sm, songPos, lineT, lineAudio, a, force) {
      if (!d.playing || d.analysis !== a) return { gate: "deck", reason: "the song changed" };
      if (!on("supermove_replay")) { refuse(deckId, "supermove_replay", "toggle", "ap-fx-supermove_replay is off"); return { gate: "toggle", reason: "off" }; }
      const bar = 240 / (d.bpm || 128), rate = rateOf(d), other = host.decks[deckId === "a" ? "b" : "a"];
      const dm = host.mod.djMind, en = energyAt(a.energy_times, a.energy_curve, songPos);
      const q3 = LM && a.energy_curve ? LM.quantile(a.energy_curve, 0.75) : null;
      const onGrid = (a.downbeat_times || []).some((t) => Math.abs(t - songPos) < 0.08);
      const exitT = dm && dm.fireAt ? dm.fireAt(Infinity) : Infinity;
      const r = planReplay({ name: sm.name, songPos, lineT, bar, now: d._currentPosition(), energy: en, energyQ3: q3, force,
        inTransition: !!(dm && dm.transitioning), holding: mashup() || !!d.loopOn, relaxed: relaxed(),
        otherPlaying: !!(other && other.playing && other._onAir && other._onAir()), exitT: Number.isFinite(exitT) ? exitT : Infinity,
        duration: d.buffer ? d.buffer.duration : Infinity, replaysThisSong: replay.songs.get(a) || 0,
        sinceLastS: audioCtx.currentTime - replay.last, barWall: bar / rate, onGrid });
      if (!r.plan) { refuse(deckId, "supermove_replay", r.refusal.gate, r.refusal.reason); return r.refusal; }
      replay.songs.set(a, 1);
      replay.last = lineAudio;
      rewind(d, lineAudio, r.plan.to);
      fire(deckId, "supermove_replay", r.plan, { params: { to: r.plan.to, replayS: r.plan.replayS, supermove: sm.name, on_demand: !!force },
        t0: lineAudio - bar / 4 / rate, t1: lineAudio });
      return null;
    }
    host.bus.on("ai-supermove", (e) => onSupermove("ai-supermove", e));
    host.bus.on("ai-cue", (e) => onSupermove("ai-cue", e));

    // ---- on demand (AI ACTIONS bar): run one move now on the playing deck / loaded pair. -> {ok, why}
    function onAirDecks() {
      const ds = ["a", "b"].map((k) => host.decks[k]).filter((d) => d && d.playing && d.analysis && (!d._onAir || d._onAir()));
      ds.sort((x, y) => ((y.crossfaderGain && y.crossfaderGain.gain.value) || 0) - ((x.crossfaderGain && x.crossfaderGain.gain.value) || 0));
      return ds;
    }
    function runNow(id) {
      const air = onAirDecks(), d = air[0], o = air[1] || null;
      const dm = host.mod.djMind;
      const st = { playing: !!d, inTransition: !!(dm && dm.transitioning), mashupActive: mashup(), stemsReady: !!(d && d.stemsReady),
        otherOnAir: !!o, supermove: !!(d && replay.pending.some((p) => p.analysis === d.analysis)) };
      const done = (ok, why) => {
        host.log.step("artist_move_now", { deck: d && d.id, decision: ok ? id : "refused", why });
        host.ui.status(`${LABEL[id] || id}: ${ok ? why : `no - ${why}`}`);
        return { ok, why };
      };
      const g = runNowGate(id, st);
      if (g) return done(false, `${g.gate}: ${g.reason}`);
      const bar = 240 / (d.bpm || 128), rate = rateOf(d), pos = d._currentPosition(), barW = bar / rate;
      const nextBar = (d.analysis.downbeat_times || []).filter((t) => t >= pos + 0.4 * rate)[0];
      const tBar = audioCtx.currentTime + ((nextBar != null ? nextBar : pos + bar) - pos) / rate;
      const other = o ? o.id : (d.id === "a" ? "b" : "a");
      forcing = true; lastRefusal = null;
      try {
        let res = null;
        if (id === "mid_blend") res = midBlend(d.id, o.id, { t0: tBar, barS: barW, total: 16, swapBar: 8 });
        else if (id === "wet_band") {
          const band = bandFor(d);
          res = swell(d.id, "reverb", tBar, tBar + barW, tBar + 2 * barW, WET_PEAK, "wet_band");
          if (res) fire(d.id, "wet_band", { why: `a 2-bar reverb throw on the mids only (${band.low}-${band.high} Hz)`, fallbacks: band.fallbacks },
            { params: { low: band.low, high: band.high }, t0: tBar, t1: tBar + 2 * barW });
        } else if (id === "kick_roll") {
          const drop = nextLine(d.analysis, pos + (2 * bar + 1.5) * rate, null);
          if (drop == null) lastRefusal = { gate: "no_line", reason: "no phrase line ahead for a drop" };
          else res = kickRoll(d.id, drop, null);
        } else if (id === "vocal_throw") res = vocalThrow(d.id, other, { t0: tBar, barS: barW, swapBar: 4, restore: true });
        else if (id === "sweep_dir") res = sweepNow(d, other, tBar, barW);
        else if (id === "supermove_replay") {
          const sm = replay.pending.filter((p) => p.analysis === d.analysis).pop();
          const lineT = nextLine(d.analysis, pos + (bar / 2) * rate + 0.2, null);
          if (lineT == null) lastRefusal = { gate: "no_line", reason: "no phrase line ahead" };
          else {
            const no = decide(d.id, d, sm, sm.songPos, lineT, audioCtx.currentTime + (lineT - pos) / rate, d.analysis, true);
            res = !no;
            if (no) lastRefusal = no;
          }
        }
        if (res) return done(true, `armed on deck ${d.id.toUpperCase()}`);
        const r = lastRefusal || { gate: "refused", reason: "see console" };
        return done(false, `${r.gate}: ${r.reason}`);
      } finally { forcing = false; }
    }
    // S6 on demand: the measured direction as a 4-bar sweep on the louder deck, back over one bar (EQ ramps).
    function sweepNow(d, other, t0, barW) {
      const dir = sweepDir(d.id, other, t0);
      const p = dir === "lowpass" ? d.highFilter && d.highFilter.gain : d.lowFilter && d.lowFilter.gain;
      if (!p) { lastRefusal = { gate: "eq", reason: "no EQ on this deck" }; return false; }
      const base = p.value, to = dir === "lowpass" ? -18 : -26;
      p.cancelScheduledValues(t0);
      p.setValueAtTime(base, t0);
      p.linearRampToValueAtTime(to, t0 + 4 * barW);
      p.linearRampToValueAtTime(base, t0 + 5 * barW);
      return true;
    }
    // Two wiring paths (whichever the console has after merge): aiActions.register (ai-actions.js loads after this
    // file, so it is tried once the scripts are in), and "ai-action" {id} events on djEvents. A run is de-duplicated
    // (one per id per 300 ms) in case both paths fire for one click. Until one exists, the buttons are bound here.
    const lastRun = {};
    const runOnce = (id) => {
      const t = host.clock.now();
      if (t - (lastRun[id] || -1e9) < 300) return { ok: false, why: "duplicate" };
      lastRun[id] = t;
      return runNow(id);
    };
    const registered = () => !!(root.aiActions && typeof root.aiActions.register === "function");
    setTimeout(() => { if (registered()) for (const id of IDS) root.aiActions.register(id, () => runOnce(id)); }, 0);
    if (root.djEvents && root.djEvents.addEventListener) root.djEvents.addEventListener("ai-action", (e) => {
      const id = e && e.detail && e.detail.id;
      if (IDS.includes(id)) runOnce(id);
    });
    const btns = host.ui.queryAll ? host.ui.queryAll("[data-ai-action]") : [];
    for (const b of Array.from(btns || [])) {
      const id = b.dataset && b.dataset.aiAction;
      if (!IDS.includes(id) || !b.addEventListener) continue;
      // capture: runs before ai-actions.js's own handler, which has no entry for these ids yet
      b.addEventListener("click", (e) => { if (registered()) return; e.stopImmediatePropagation(); runOnce(id); }, true);
    }

    function stop() { timers.forEach(clearTimeout); timers.length = 0; }
    const api = { core, midBlend, tailSwell, bandFor, kickRoll, vocalThrow, sweepDir, runNow, stop,
                  get rewinds() { return replay.rewinds || 0; }, get supermoves() { return replay.pending.length; } };
    host.mod.fxMoves = api;
    return api;
  }
  if (root.Engine) root.Engine.mount("fxMoves", create);
})(typeof window !== "undefined" ? window : globalThis);
