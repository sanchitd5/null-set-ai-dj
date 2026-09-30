// AI Music Brain - ARTIST MOVES, batch C of research/notes/artist-signature-techniques.md
// (loops, teases). One pure planner per technique in `core` (node-tested), the runtime reaches the
// world only through the Host port (engine.js). A plan that fails a gate arms nothing and says why
// once per phrase; every constant fallback a plan used is listed in its `fallbacks` and logged.
//
//   slip_loop    S13 (Carl Cox, Beat Masher / Flux, SOURCED): the last 2 or 4 bars before a phrase
//                line into a lift play their first half twice (loop of 4 or 8 beats) while the
//                song keeps advancing under the loop; the release lands where the track would have
//                been, which is the phrase line (deck-controller.js slipLoop / slipRelease).
//   cue_tease    S14 (Cox cue-button builds, SECONDARY): 1 or 2 beat stabs of B's strongest drum
//                hit on the backbeat of A's last bars before B enters. Drums only (a stab never
//                doubles a vocal), high-passed at 150 Hz (A keeps the sub), inside the 8 % tempo cap.
//   roll         S12 (Cox roll effect, SECONDARY): 1/2-beat repeats of A's drum stem at low wet
//                over A's last 1/2 or 1 bar before B enters, ending on the phrase line (B's entry).
//   perc_bridge  S11 (third-deck percussion bridge): gated on a third deck. The console has two,
//                so the planner refuses and nothing runs; kept so the gate is explicit and logged.
//   S15 (tempo swerve at a break) is skipped by the spec itself (covered by the prior note).
//   pad_lead     Lane 8 "pads first" (learned stem_intro store: other-first is the most seen order,
//                5 of 5 Lane 8 intros lead with other or drums, bass never first; set study
//                N_GfH09iP9c): B's own pads ("other" stem) from the 4 or 8 bars before its entry
//                point rise in under A's last bars, high-passed at 150 Hz, so B's pads arrive first,
//                its drums / bass land with the transition. Key >= 0.8 (two melodies overlap, G2).
//   chant_gate   S20 (Argy / Anyma melodic techno gated vocal, SECONDARY + GUESS artist link; the
//                learned vocal_loop "chant" sightings): A's own vocal stem gated on straight 16ths
//                over the last 1 or 2 bars of a build, opened fully again on the phrase line.
//                One per song, costs one "vocal" unit of the S21 FX budget.
(function (root) {
  "use strict";

  const lm = root.learnedMovesCore || (typeof require === "function" ? require("./learned-moves.js") : null);
  const { envelope, snapBeat, median } = lm;

  const KINDS = ["slip_loop", "cue_tease", "roll", "perc_bridge", "pad_lead", "chant_gate"];
  const SPEC = { slip_loop: "S13", cue_tease: "S14", roll: "S12", perc_bridge: "S11", pad_lead: "Lane8", chant_gate: "S20" };
  const LABEL = { slip_loop: "SLIP LOOP", cue_tease: "CUE TEASE", roll: "ROLL", perc_bridge: "PERC BRIDGE",
    pad_lead: "PAD LEAD", chant_gate: "CHANT GATE" };
  const MIN_LEAD_S = 0.6;             // a move is booked at least this far ahead (learned-moves.js)
  const SLIP_WINDOW_BEATS = [16, 8];  // window before the line; the loop is its first half (4 or 8 beats)
  const SLIP_CAP_BEATS = 16;          // hard cap on the slip window (KB 16-beat hold)
  const SLIP_PER_SONG = 2, SLIP_GAP_BARS = 32;
  const SLIP_EXIT_GUARD_BARS = 4;     // the release lands at least this far before the planned exit
  const MAX_VOCAL_SHARE = 0.25;       // a loop over a sung stretch cuts a word (spec risk)
  const TEASE_BARS = 8;               // the tease lives in A's last 8 bars
  const TEASE_MAX_STABS = 4;
  const TEMPO_CAP_PCT = 8;            // tempo-rule.js KEYLOCK_RANGE_PCT
  const HP_HZ = 150;                  // layered stabs / rolls stay above the 120 Hz sub owner line
  const STAB_REL = 0.35;              // a stab sits about 9 dB under A's drums (UNVERIFIED by ear)
  const STAB_GAIN_FALLBACK = 0.25;
  const ROLL_WET = 0.25;              // low wet (UNVERIFIED by ear)
  const ROLL_SLICE_BEATS = 0.5;
  const MIN_RMS = 0.01;               // stem-moves.js floor
  const PAD_BARS = [8, 4];            // pad lead window before B's entry (one phrase, else half)
  const PAD_KEY_MIN = 0.8;            // G2: overlapped melodies need a +-1 hour / same key match
  const PAD_REL = 0.6;                // B's pads under A's own pads (UNVERIFIED by ear)
  const PAD_GAIN_FALLBACK = 0.4;      // B's pads when A's "other" stem is not measured (UNVERIFIED)
  const PAD_RISE_SHARE = 0.5;         // the pads rise over the first half of the window
  const PAD_EVERY = 3;                // autopilot: at most one pad lead every 3 transitions (conservative rate)
  // the blends B's pads can lead into: B's full song enters over bars (never a cut, echo out or peak swap)
  const PAD_RECIPES = /^(long blend|bass swap|drop swap|blend|stem bridge|learned:stem_intro)$/i;
  const CHANT_BARS = [2, 1];          // gated window before the line
  const CHANT_PER_SONG = 1;
  const CHANT_MIN_VOCAL = 0.5;        // A must sing at least half the window (else nothing to gate)
  const CHANT_STEP_BEATS = 0.25;      // straight 16ths
  const CHANT_FLOOR = 0.1;            // closed gate level, about -20 dB (UNVERIFIED by ear)

  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
  const no = (gate, reason) => ({ ok: false, gate, reason });
  const fin = Number.isFinite;

  function vocalShare(regions, t0, t1) {
    if (!(t1 > t0)) return 0;
    let c = 0;
    for (const r of regions || []) { const a = Math.max(t0, r[0]), b = Math.min(t1, r[1]); if (b > a) c += b - a; }
    return Math.min(1, c / (t1 - t0));
  }
  // mean of the analysis energy curve over [t0, t1) (null when unmeasured)
  function meanEnergy(curve, times, t0, t1) {
    if (!Array.isArray(curve) || !Array.isArray(times)) return null;
    let s = 0, n = 0;
    for (let i = 0; i < Math.min(curve.length, times.length); i++) if (times[i] >= t0 && times[i] < t1 && fin(curve[i])) { s += curve[i]; n++; }
    return n ? s / n : null;
  }
  // the analysed beat nearest t (grid fallback when no beat list): -> {t, err}
  function nearestBeat(beats, t, t0, beatS) {
    let best = null;
    for (const b of beats || []) if (best == null || Math.abs(b - t) < Math.abs(best - t)) best = b;
    if (best == null || Math.abs(best - t) > beatS / 2) return { t: snapBeat(t, t0, beatS), err: 0, grid: true };
    return { t: best, err: Math.abs(best - t), grid: false };
  }
  // Where the song is under a slip loop: the shadow playhead. s = {pos, at, rate}
  const shadowAt = (s, now) => s.pos + (now - s.at) * s.rate;

  // ---- S13 slip loop ---------------------------------------------------------------------------
  // c: {pos, lineT (next phrase line, song s), bpm, rate, beats[], exitT, inTransition, holdActive,
  //     mashupActive, loopOn, reversed, busySlices, slipSupported, vocals (regions), energyNow,
  //     energyNext, count, atBar, lastAtBar, quiet (the mind rides this phrase), relaxed, onDemand}
  // onDemand (AI ACTIONS button) skips only the choice gates: per-song cap, spacing, build, quiet, relaxed.
  function planSlipLoop(c) {
    if (c.inTransition || c.holdActive) return no("transition", "a transition or merge hold owns the deck");
    if (c.mashupActive) return no("vocal_layer", "a vocal layer is running");
    if (!c.slipSupported) return no("no_slip", "the deck has no slip loop; nothing armed (loop_extend is the learned fallback)");
    if (c.loopOn || c.reversed || c.busySlices) return no("deck_busy", "a loop, reverse or stem slice already runs");
    if (!c.onDemand) {
      if (c.relaxed) return no("relaxed", "relaxed session: no artist moves");
      if (c.quiet === false) return no("phrase_busy", "the mind booked another move on this phrase");
      if ((c.count || 0) >= SLIP_PER_SONG) return no("cap", `${SLIP_PER_SONG} slip loops this song`);
      if (c.lastAtBar != null && c.atBar - c.lastAtBar < SLIP_GAP_BARS) return no("spacing", `last slip ${Math.round(c.atBar - c.lastAtBar)} bars ago (< ${SLIP_GAP_BARS})`);
      if (!(c.energyNow != null && c.energyNext != null)) return no("unmeasured", "no energy curve: cannot tell a build from a fade");
      if (!(c.energyNext > c.energyNow)) return no("no_build", `next phrase energy ${c.energyNext.toFixed(2)} <= ${c.energyNow.toFixed(2)}: a slip loop is a build`);
    }
    if (!Array.isArray(c.vocals)) return no("unmeasured", "no vocal regions: cannot rule out a loop inside a word");
    const build = c.energyNow != null && c.energyNext != null ? ` (energy ${c.energyNow.toFixed(2)} -> ${c.energyNext.toFixed(2)})` : "";
    const fallbacks = [];
    const bpm = c.bpm > 0 ? c.bpm : (fallbacks.push("bpm=128"), 128);
    const beatS = 60 / bpm, rate = c.rate > 0 ? c.rate : 1;
    if (c.exitT != null && c.lineT > c.exitT - SLIP_EXIT_GUARD_BARS * 4 * beatS) return no("exit_guard", "the release would sit inside the exit guard");
    for (const W of SLIP_WINDOW_BEATS) {
      const nb = nearestBeat(c.beats, c.lineT - W * beatS, c.lineT, beatS);
      if (nb.grid) fallbacks.push("beat grid from bpm");
      const start = nb.t;
      if (start < c.pos + MIN_LEAD_S * rate) continue;
      const vs = vocalShare(c.vocals, start, c.lineT);
      if (vs > MAX_VOCAL_SHARE) return no("vocal_seam", `${Math.round(vs * 100)}% of the window is sung: a loop would cut a word`);
      return { ok: true, kind: "slip_loop", start, release: c.lineT, loop_beats: W / 2, window_beats: W,
        cap_beats: SLIP_CAP_BEATS, extension_beats: 0, grid_err_s: nb.err, fallbacks,
        why: `last ${W / 4} bars before the line: ${W / 8} bar${W > 8 ? "s" : ""} looped twice, the song runs on under it${build}` };
    }
    return no("late", "no 2 bar window left before the line");
  }

  // ---- S14 cue tease --------------------------------------------------------------------------
  // c: {pos, exitT (A song s at B's entry), aBpm, aRate, aBeats[], bBpm, bEntry, bPlaying, bDrumEnv
  //     ({t0, hop, v} beat bins of B's drum stem from bEntry), aDrumRms, inTransition, mashupActive}
  function planCueTease(c) {
    if (c.inTransition) return no("transition", "the transition is running");
    if (c.relaxed && !c.onDemand) return no("relaxed", "relaxed session: no artist moves");
    if (c.mashupActive) return no("vocal_layer", "a vocal layer is running");
    if (c.exitT == null) return no("no_plan", "no planned entry for B");
    if (c.bPlaying) return no("b_rolling", "B already plays: its entry point moves");
    if (!c.bDrumEnv || !c.bDrumEnv.v || c.bDrumEnv.v.length < 2) return no("no_stems", "B's drum stem is not loaded");
    const fallbacks = [];
    const aBpm = c.aBpm > 0 ? c.aBpm : (fallbacks.push("aBpm=128"), 128);
    const bBpm = c.bBpm > 0 ? c.bBpm : (fallbacks.push("bBpm=128"), 128);
    const aRate = c.aRate > 0 ? c.aRate : 1, heard = aBpm * aRate;
    const v = heard / bBpm;                         // B song seconds per real second, beat-locked to A
    if (Math.abs(v - 1) * 100 > TEMPO_CAP_PCT) return no("tempo", `B stabs need a ${((v - 1) * 100).toFixed(1)}% stretch (cap ${TEMPO_CAP_PCT}%)`);
    const env = c.bDrumEnv.v;
    let k = 0;
    for (let i = 1; i < env.length; i++) if (env[i] > env[k]) k = i;
    if (!(env[k] >= MIN_RMS)) return no("no_stems", "B's drum stem is silent in its first bars");
    const beats = k + 1 < env.length && env[k + 1] >= 0.6 * env[k] ? 2 : 1;   // a sustained hit gets 2 beats
    const beatS = 60 / aBpm;                        // A song seconds per beat
    const bars = Math.floor((c.exitT - c.pos - MIN_LEAD_S * aRate) / (4 * beatS));
    const n = Math.min(TEASE_MAX_STABS, bars);
    if (n < 1) return no("late", "less than a bar left before B enters");
    const off = beats === 1 ? 3 : 1;                // backbeat: beat 4 (1 beat) or beats 2-3 (2 beats)
    const stabs = [];
    for (let j = n; j >= 1; j--) {
      const bar0 = nearestBeat(c.aBeats, c.exitT - j * 4 * beatS, c.exitT, beatS);
      if (bar0.grid && !fallbacks.includes("A beat grid from bpm")) fallbacks.push("A beat grid from bpm");
      stabs.push({ a_t: bar0.t + off * beatS, b_from: c.bDrumEnv.t0 + k * c.bDrumEnv.hop, beats, grid_err_s: bar0.err });
    }
    let gain;
    if (fin(c.aDrumRms) && c.aDrumRms > 0) gain = clamp(STAB_REL * c.aDrumRms / env[k], 0.05, 0.6);
    else { gain = STAB_GAIN_FALLBACK; fallbacks.push(`gain=${STAB_GAIN_FALLBACK}`); }
    return { ok: true, kind: "cue_tease", stabs, gain, hp_hz: HP_HZ, b_rate: v, stab_beats: beats,
      cap_beats: TEASE_MAX_STABS * 2, fallbacks,
      why: `${n} ${beats}-beat stab${n > 1 ? "s" : ""} of B's drum hit (beat ${k + 1} of its entry) on A's backbeat, high-passed ${HP_HZ} Hz` };
  }

  // ---- S12 roll under the blend ---------------------------------------------------------------
  // c: {pos, exitT, bpm, rate, beats[], aDrumBars ([rms] of A's drum stem per beat over the last bar
  //     before exitT), inTransition, mashupActive, fxOk}
  function planRoll(c) {
    if (c.inTransition) return no("transition", "the transition is running");
    if (c.mashupActive) return no("vocal_layer", "a vocal layer is running");
    if (!c.fxOk && !c.onDemand) return no("fx_budget", "the FX budget (dj-mind fxAllowed, relaxed session) says no roll here");
    if (c.exitT == null) return no("no_plan", "no planned entry for B");
    const bars = c.aDrumBars;
    if (!Array.isArray(bars) || !bars.length) return no("no_stems", "A's drum stem is not live");
    const lo = Math.min(...bars), hi = Math.max(...bars);
    if (!(median(bars) >= MIN_RMS)) return no("drums_silent", "A's drums do not play in the last bar");
    const fallbacks = [];
    const bpm = c.bpm > 0 ? c.bpm : (fallbacks.push("bpm=128"), 128);
    const beatS = 60 / bpm, rate = c.rate > 0 ? c.rate : 1;
    const steady = lo >= hi / 3;                    // steady drums take the full bar, a fill only half
    for (const W of steady ? [4, 2] : [2]) {
      const nb = nearestBeat(c.beats, c.exitT - W * beatS, c.exitT, beatS);
      if (nb.grid) fallbacks.push("beat grid from bpm");
      if (nb.t < c.pos + MIN_LEAD_S * rate) continue;
      const slice = ROLL_SLICE_BEATS * beatS, count = Math.round((c.exitT - nb.t) / slice);
      const pieces = [];
      for (let i = 0; i < count; i++) pieces.push({ a_t: nb.t + i * slice, from: nb.t, dur: slice });
      return { ok: true, kind: "roll", start: nb.t, release: c.exitT, window_beats: W, slice_beats: ROLL_SLICE_BEATS,
        pieces, wet: ROLL_WET, hp_hz: HP_HZ, cap_beats: 4, grid_err_s: nb.err, fallbacks,
        why: `${W / 4} bar roll of A's drums (1/2 beat, wet ${ROLL_WET}) into B's entry, released on the line` };
    }
    return no("late", "the last bar before B is already under way");
  }

  // ---- S11 percussion bridge ------------------------------------------------------------------
  // c: {deckCount, tempoGapPct, bLoading}
  function planPercBridge(c) {
    if (!(c.deckCount >= 3)) return no("no_third_deck", "the console has no third deck for a percussion loop");
    if (!(Math.abs(c.tempoGapPct || 0) > 6) && !c.bLoading) return no("no_need", "no tempo gap > 6% and B is ready");
    return { ok: true, kind: "perc_bridge", fallbacks: [], why: "third-deck drum loop bridges the gap" };
  }

  // mean of an envelope {t0, hop, v} over [t0, t1] (null when nothing is inside)
  function envMean(env, t0, t1) {
    if (!env || !Array.isArray(env.v) || !(env.hop > 0)) return null;
    const i0 = Math.max(0, Math.floor((t0 - env.t0) / env.hop)), i1 = Math.min(env.v.length, Math.ceil((t1 - env.t0) / env.hop));
    let s = 0, n = 0;
    for (let i = i0; i < i1; i++) if (fin(env.v[i])) { s += env.v[i]; n++; }
    return n ? s / n : null;
  }

  // ---- Lane 8 pad lead -------------------------------------------------------------------------
  // c: {pos, exitT (A song s at B's entry), aBpm, aRate, bBpm, bEntry (B song s), bPlaying,
  //     bOtherEnv ({t0, hop, v} of B's "other" stem before its entry), aOtherRms (A's "other"
  //     stem median over its last bars, or null), keyScore, inTransition, mashupActive, relaxed, onDemand,
  //     style (dj-mind plan style), recipe (the booked recipe), sinceLast (transitions since the last pad lead)}
  function planPadLead(c) {
    if (c.inTransition) return no("transition", "the transition is running");
    if (!c.onDemand) {
      if (c.relaxed) return no("relaxed", "relaxed session: no artist moves");
      if (c.style === "instant" || c.style === "peak" || c.style === "layer") return no("style", `a ${c.style} transition: B does not enter over bars`);
      if (!PAD_RECIPES.test(String(c.recipe || ""))) return no("recipe", `${c.recipe || "no recipe"} is not a blend B's pads can lead into`);
      if (c.sinceLast != null && c.sinceLast < PAD_EVERY) return no("spacing", `last pad lead ${c.sinceLast} transition(s) ago (< ${PAD_EVERY})`);
    }
    if (c.mashupActive) return no("vocal_layer", "a vocal layer is running");
    if (!fin(c.exitT) || !fin(c.bEntry)) return no("no_plan", "no planned entry for B");
    if (c.bPlaying) return no("b_rolling", "B already plays: its entry point moves");
    if (!fin(c.keyScore)) return no("unmeasured", "no Camelot score for the pair: two pads need a key match");
    if (c.keyScore < PAD_KEY_MIN) return no("key", `Camelot ${c.keyScore.toFixed(2)} < ${PAD_KEY_MIN}: B's pads would clash with A's melody`);
    const fallbacks = [];
    const aBpm = c.aBpm > 0 ? c.aBpm : (fallbacks.push("aBpm=128"), 128);
    const bBpm = c.bBpm > 0 ? c.bBpm : (fallbacks.push("bBpm=128"), 128);
    const aRate = c.aRate > 0 ? c.aRate : 1, v = aBpm * aRate / bBpm;
    if (Math.abs(v - 1) * 100 > TEMPO_CAP_PCT) return no("tempo", `B's pads need a ${((v - 1) * 100).toFixed(1)}% stretch (cap ${TEMPO_CAP_PCT}%)`);
    const env = c.bOtherEnv;
    if (!env || !Array.isArray(env.v) || env.v.length < 2) return no("no_stems", "B's other stem is not loaded");
    const beatS = 60 / aBpm, bBeatS = 60 / bBpm;
    let last = no("late", "less than half a phrase left before B enters");
    for (const W of PAD_BARS) {
      const start = c.exitT - W * 4 * beatS, from = c.bEntry - W * 4 * bBeatS;
      if (from < 0) { last = no("no_room", `B's entry point leaves no ${W} bars before it`); continue; }
      if (start < c.pos + MIN_LEAD_S * aRate) { last = no("late", `no ${W} bar window left before B enters`); continue; }
      const bMean = envMean(env, from, c.bEntry);
      if (!(bMean >= MIN_RMS)) { last = no("no_pads", `B's pads are silent in the ${W} bars before its entry`); continue; }
      let gain;
      if (c.aOtherRms > 0) gain = clamp(PAD_REL * c.aOtherRms / bMean, 0.1, 0.7);
      else { gain = PAD_GAIN_FALLBACK; fallbacks.push(`gain=${PAD_GAIN_FALLBACK}`); }
      return { ok: true, kind: "pad_lead", start, release: c.exitT, window_beats: W * 4, cap_beats: 32,
        piece: { a_t: start, b_from: from, b_beats: W * 4 }, b_rate: v, gain, rise_share: PAD_RISE_SHARE, hp_hz: HP_HZ,
        fallbacks, why: `B's pads alone under A's last ${W} bars (key ${c.keyScore.toFixed(2)}), drums and bass with the transition` };
    }
    return last;
  }

  // ---- S20 chant gate --------------------------------------------------------------------------
  // c: {pos, lineT, bpm, rate, beats, vocals (regions), energyNow, energyNext, count, quiet,
  //     vocalLive (A's vocal stem sounds), vocalBusy (sliced / held / swapped), inTransition,
  //     holdActive, mashupActive, relaxed, onDemand}
  function planChantGate(c) {
    if (c.inTransition || c.holdActive) return no("transition", "a transition or merge hold owns the deck");
    if (c.mashupActive) return no("vocal_layer", "a vocal layer is running");
    if (!c.vocalLive) return no("no_stems", "A's vocal stem is not live");
    if (c.vocalBusy) return no("busy", "A's vocal stem is already sliced / held / swapped");
    if (!c.onDemand) {
      if (c.relaxed) return no("relaxed", "relaxed session: no artist moves");
      if (c.quiet === false) return no("phrase_busy", "the mind booked another move on this phrase");
      if ((c.count || 0) >= CHANT_PER_SONG) return no("cap", `${CHANT_PER_SONG} chant gate this song`);
      if (!(fin(c.energyNow) && fin(c.energyNext))) return no("unmeasured", "no energy curve: cannot tell a build from a fade");
      if (!(c.energyNext > c.energyNow)) return no("no_build", "the next phrase does not lift: a gate is a build move");
    }
    if (!Array.isArray(c.vocals)) return no("unmeasured", "no vocal regions: cannot tell if A sings");
    const fallbacks = [];
    const bpm = c.bpm > 0 ? c.bpm : (fallbacks.push("bpm=128"), 128);
    const beatS = 60 / bpm, rate = c.rate > 0 ? c.rate : 1;
    let last = no("late", "less than a bar left before the line");
    for (const W of CHANT_BARS) {
      const nb = nearestBeat(c.beats, c.lineT - W * 4 * beatS, c.lineT, beatS);
      if (nb.t < c.pos + MIN_LEAD_S * rate) { last = no("late", `no ${W} bar window left before the line`); continue; }
      const share = vocalShare(c.vocals, nb.t, c.lineT);
      if (share < CHANT_MIN_VOCAL) { last = no("no_vocal", `A sings ${Math.round(share * 100)}% of the ${W} bars: nothing to gate`); continue; }
      if (nb.grid && !fallbacks.includes("beat grid from bpm")) fallbacks.push("beat grid from bpm");
      const n = Math.round(W * 4 / CHANT_STEP_BEATS), stepS = (c.lineT - nb.t) / n, steps = [];
      for (let i = 0; i < n; i += 2) steps.push({ t0: nb.t + i * stepS, t1: nb.t + (i + 1) * stepS });
      return { ok: true, kind: "chant_gate", start: nb.t, release: c.lineT, window_beats: W * 4, cap_beats: 8,
        step_beats: CHANT_STEP_BEATS, steps, floor: CHANT_FLOOR, grid_err_s: nb.err, fallbacks,
        why: `A's vocal gated on 16ths over the last ${W} bar${W > 1 ? "s" : ""} of the build, open on the line` };
    }
    return last;
  }

  const PLANNERS = { slip_loop: planSlipLoop, cue_tease: planCueTease, roll: planRoll, perc_bridge: planPercBridge,
    pad_lead: planPadLead, chant_gate: planChantGate };

  const core = { KINDS, SPEC, LABEL, MIN_LEAD_S, SLIP_WINDOW_BEATS, SLIP_CAP_BEATS, SLIP_PER_SONG, SLIP_GAP_BARS,
    TEASE_BARS, TEASE_MAX_STABS, TEMPO_CAP_PCT, HP_HZ, ROLL_WET, PLANNERS,
    PAD_BARS, PAD_KEY_MIN, PAD_EVERY, PAD_RECIPES, CHANT_BARS, CHANT_PER_SONG, CHANT_MIN_VOCAL, CHANT_STEP_BEATS, CHANT_FLOOR,
    vocalShare, meanEnergy, nearestBeat, shadowAt, envMean, planSlipLoop, planCueTease, planRoll, planPercBridge,
    planPadLead, planChantGate };
  root.artistMovesCore = core;
  if (typeof module !== "undefined" && module.exports) module.exports = core;

  // ---- runtime (Host port only) ---------------------------------------------------------------
  function create({ host }) {
    const audioCtx = host.audio;
    const { setTimeout, clearTimeout } = host.clock;
    const timers = { a: [], b: [] };
    const said = new Map();                         // "deck:kind:key" refusals already logged
    const other = (id) => (id === "a" ? "b" : "a");
    const on = (k) => host.ui.flag(`ap-artist-${k}`, true);
    const later = (id, ms, fn) => timers[id].push(setTimeout(fn, Math.max(0, ms)));
    const rateOf = (d) => (d._playbackRate && d._playbackRate()) || 1;
    const relaxed = () => !!(host.session && host.session.relaxed);

    function say(d, kind, why, extra = {}) {
      host.bus.emit("ai-activity", Object.assign({ kind: "artist_move", deck: d.id, label: `artist_move: ${kind}`, move: kind, spec: SPEC[kind], why }, extra));
      // NULL-BOT / SHOW (mascot.js): the slip loop lands on its release (super), the rest pop in place
      if (Number.isFinite(extra.t0)) host.bus.emit("vis-moment", kind === "slip_loop" ? { at: extra.t1, name: LABEL[kind], tier: "super", deck: d.id } : { at: extra.t0, name: LABEL[kind], tier: "accent", deck: d.id });
    }
    // a refusal is said once per phrase (key = the line / entry it was planned for)
    function refuse(d, kind, key, p) {
      const k = `${d.id}:${kind}:${key}`;
      if (said.has(k)) return;
      if (said.size > 300) said.clear();
      said.set(k, 1);
      console.info(`artist move ${kind} skipped: ${p.gate}: ${p.reason}`);
      host.log.step("artist_move_refused", { deck: d.id, decision: kind, why: `${p.gate}: ${p.reason}` });
    }
    function logPlan(kind, p) {
      console.info(`artist move ${kind}: ${p.why}${p.fallbacks.length ? ` (constants: ${p.fallbacks.join(", ")})` : ""}`);
    }
    // beat-hop RMS of one stem from song time t0 (the stem's own lag / ratio)
    function stemEnv(d, name, t0, t1, hop) {
      const st = d && d.stems, b = st && st[name];
      if (!d.stemsReady || !b || !b.getChannelData) return null;
      return envelope(b.getChannelData(0), b.sampleRate, Math.max(0, t0), t1, hop, st.lag || 0, st.ratio || 1);
    }
    const audioAt = (d, o, t) => audioCtx.currentTime + (t - o.pos) / rateOf(d);

    // ---- execution of an ok plan (returns {busyS, why} for the caller) ----
    function runSlip(d, p, o) {
      const lead = (p.start - o.pos) / rateOf(d);
      later(d.id, lead * 1000 - 5, () => {
        if (!d.playing || !d.slipLoop(p.loop_beats, p.start)) { console.info("artist move slip_loop skipped: deck: slip loop refused at its start"); return; }
        later(d.id, ((p.release - p.start) / rateOf(d)) * 1000, () => {
          const shadow = d.slipPosition(), to = d.slipRelease();
          if (to == null) return;
          host.bus.emit("ai-activity", { kind: "artist_move_release", deck: d.id, move: "slip_loop", land_s: to, release_s: p.release,
            shadow_err_s: +Math.abs(to - shadow).toFixed(4), line_err_s: +Math.abs(to - p.release).toFixed(4) });
        });
      });
      say(d, "slip_loop", p.why, { t0: audioAt(d, o, p.start), t1: audioAt(d, o, p.release), beats: p.window_beats, cap_beats: p.cap_beats,
        grid_err_s: +p.grid_err_s.toFixed(4), params: { loop_beats: p.loop_beats, window_beats: p.window_beats, start: p.start, release: p.release }, fallbacks: p.fallbacks });
      return { busyS: (p.release - o.pos) / rateOf(d) + 0.1, why: p.why };
    }
    // pieces [{a_t (A song s), from (buffer s), dur (buffer s)}] -> audio clock, layered into deck d
    function book(d, buf, pieces, rate, gain, o) {
      return d.layerPieces(buf, pieces.map((x) => ({ from: x.from, dur: x.dur, at: audioAt(d, o, x.a_t) })), { rate, gain, hpHz: HP_HZ });
    }
    function runTease(d, b, p, o) {
      const st = b.stems, k = st.ratio || 1, lag = st.lag || 0, bBeatS = 60 / (b.bpm || 128);
      const pieces = p.stabs.map((s) => ({ a_t: s.a_t, from: (s.b_from + lag) * k, dur: s.beats * bBeatS * k }));
      const res = book(d, st.drums, pieces, p.b_rate * k, p.gain, o);
      if (!res) return null;
      say(d, "cue_tease", p.why, { t0: audioAt(d, o, p.stabs[0].a_t), t1: res.until, beats: p.stabs.length * p.stab_beats,
        cap_beats: p.cap_beats, grid_err_s: +Math.max(...p.stabs.map((s) => s.grid_err_s)).toFixed(4), hp_hz: p.hp_hz,
        params: { stabs: p.stabs.length, stab_beats: p.stab_beats, gain: +p.gain.toFixed(3), b_rate: +p.b_rate.toFixed(4) }, fallbacks: p.fallbacks });
      return { busyS: 0, why: p.why };
    }
    function runRoll(d, p, o) {
      const st = d.stems, k = st.ratio || 1, lag = st.lag || 0;
      const pieces = p.pieces.map((x) => ({ a_t: x.a_t, from: (x.from + lag) * k, dur: x.dur * k }));
      const res = book(d, st.drums, pieces, rateOf(d) * k, p.wet, o);
      if (!res) return null;
      say(d, "roll", p.why, { t0: audioAt(d, o, p.start), t1: res.until, beats: p.window_beats, cap_beats: p.cap_beats,
        grid_err_s: +p.grid_err_s.toFixed(4), hp_hz: p.hp_hz, params: { window_beats: p.window_beats, slice_beats: p.slice_beats, wet: p.wet }, fallbacks: p.fallbacks });
      return { busyS: 0, why: p.why };
    }
    // Lane 8 pad lead: B's "other" stem layered into deck A (high-passed), half gain over the first
    // rise_share of the window, full gain after; B's drums and bass come in with the transition.
    function runPad(d, b, p, o) {
      const st = b.stems, k = st.ratio || 1, lag = st.lag || 0, bBeatS = 60 / (b.bpm || 128), aBeatS = 60 / (d.bpm || 128);
      const pc = p.piece, half = pc.b_beats * p.rise_share;
      const lo = book(d, st.other, [{ a_t: pc.a_t, from: (pc.b_from + lag) * k, dur: half * bBeatS * k }], p.b_rate * k, p.gain * 0.5, o);
      if (!lo) return null;
      const hi = book(d, st.other, [{ a_t: pc.a_t + half * aBeatS, from: (pc.b_from + half * bBeatS + lag) * k,
        dur: (pc.b_beats - half) * bBeatS * k }], p.b_rate * k, p.gain, o);
      if (!hi) return null;
      say(d, "pad_lead", p.why, { t0: audioAt(d, o, pc.a_t), t1: hi.until, beats: p.window_beats, cap_beats: p.cap_beats, hp_hz: p.hp_hz,
        params: { window_beats: p.window_beats, gain: +p.gain.toFixed(3), b_rate: +p.b_rate.toFixed(4), rise_share: p.rise_share }, fallbacks: p.fallbacks });
      return { busyS: 0, why: p.why };
    }
    // S20 chant gate: A's live vocal stem gain chopped on straight 16ths (open, floor, open, ...),
    // fully open again on the phrase line. Same stem-mode entry / exit as learned-moves.js runSwap.
    function runChant(d, p, o) {
      const at = audioAt(d, o, p.start), until = audioAt(d, o, p.release), lead = at - audioCtx.currentTime;
      if (lead < 0.35 || !(until > at)) return null;
      if (!d.stemsLiveAt || !d.stemsLiveAt(at)) return null;
      const live = d.stemLive && d.stemLive.vocals;
      if (!live || !live.gain) return null;
      const e = 0.006, rec = d._artistChant = { until };
      // audio times now: audioAt reads the clock, so it must not run inside the timer
      const steps = p.steps.map((s) => [audioAt(d, o, s.t0), audioAt(d, o, s.t1)]);
      later(d.id, lead * 1000 - 250, () => {
        if (!d.playing || d._artistChant !== rec) return;
        if (!d.stemMix({}, at - 0.01, 0.01)) return void console.info("artist move chant_gate skipped: stem mode refused");
        const lg = live.gain;
        lg.cancelScheduledValues(at); lg.setValueAtTime(1, at);
        for (const [a0, a1] of steps) {
          const reopen = Math.min(until, a1 + (a1 - a0));
          if (a1 - e <= a0 || reopen - e <= a1) continue;
          lg.setValueAtTime(1, a1 - e); lg.linearRampToValueAtTime(p.floor, a1);
          lg.setValueAtTime(p.floor, reopen - e); lg.linearRampToValueAtTime(1, reopen);
        }
        lg.setValueAtTime(1, until);
        later(d.id, (until - audioCtx.currentTime) * 1000 - 200, () => {
          if (d._artistChant === rec) d._artistChant = null;
          if (d.playing) d.stemMix(null, until + 0.03, 0.02);
        });
      });
      say(d, "chant_gate", p.why, { t0: at, t1: until, beats: p.window_beats, cap_beats: p.cap_beats,
        grid_err_s: +p.grid_err_s.toFixed(4), params: { window_beats: p.window_beats, step_beats: p.step_beats, floor: p.floor, steps: p.steps.length },
        fallbacks: p.fallbacks });
      return { busyS: 0, why: p.why };
    }

    // ---- one attempt of one kind on deck d. o: {pos, bar, entryT, lineT, exitT, bEntry, quiet, holdActive,
    //      mashupActive, fxOk, inTransition, onDemand} -> {plan, res} (res null when refused or not armed)
    // per-song state on the deck (a new song = a new analysis object = fresh counters)
    const songOf = (d) => (d._artist && d._artist.ana === d.analysis ? d._artist
      : (d._artist = { ana: d.analysis, slips: 0, lastSlipBar: null, teaseFor: null, rollFor: null, slipLine: null, bridged: false,
          padFor: null, chants: 0, chantLine: null, slipBooked: null }));
    function attempt(kind, d, o) {
      const a = d.analysis || {}, b = host.decks && host.decks[other(d.id)], r = songOf(d);
      const base = { pos: o.pos, inTransition: !!o.inTransition, mashupActive: !!o.mashupActive, relaxed: relaxed(), onDemand: !!o.onDemand };
      let p = null, res = null;
      if (kind === "perc_bridge") {
        p = planPercBridge({ deckCount: Object.keys(host.decks || {}).length });
      } else if (kind === "slip_loop") {
        const len = PHRASE_S(o);
        p = planSlipLoop(Object.assign(base, { lineT: o.lineT, bpm: d.bpm, rate: rateOf(d), beats: a.beat_times, exitT: o.exitT,
          holdActive: !!o.holdActive, loopOn: !!d.loopOn, reversed: !!d.reversed, quiet: o.quiet,
          busySlices: Object.keys(d._slices || {}).length + Object.keys(d._holds || {}).length > 0,
          slipSupported: typeof d.slipLoop === "function", vocals: a.vocal_active_regions,
          energyNow: meanEnergy(a.energy_curve, a.energy_times, o.lineT - len, o.lineT),
          energyNext: meanEnergy(a.energy_curve, a.energy_times, o.lineT, o.lineT + len),
          count: r.slips, atBar: (o.pos - (o.entryT || 0)) / o.bar, lastAtBar: r.lastSlipBar }));
        if (p.ok) { r.slips++; r.lastSlipBar = (o.pos - (o.entryT || 0)) / o.bar; logPlan(kind, p); res = runSlip(d, p, o); }
      } else if (kind === "cue_tease") {
        const hop = 60 / ((b && b.bpm) || 128);
        const aEnv = stemEnv(d, "drums", o.exitT - o.bar, o.exitT, o.bar / 4);
        p = planCueTease(Object.assign(base, { exitT: o.exitT, aBpm: d.bpm, aRate: rateOf(d), aBeats: a.downbeat_times,
          bBpm: b && b.bpm, bPlaying: !!(b && b.playing),
          bDrumEnv: b && fin(o.bEntry) ? stemEnv(b, "drums", o.bEntry, o.bEntry + 8 * hop, hop) : null,
          aDrumRms: aEnv && aEnv.v.length ? median(aEnv.v) : null }));
        if (p.ok) { logPlan(kind, p); res = runTease(d, b, p, o); }
      } else if (kind === "roll") {
        const env = o.exitT != null ? stemEnv(d, "drums", o.exitT - o.bar, o.exitT, o.bar / 4) : null;
        p = planRoll(Object.assign(base, { exitT: o.exitT, bpm: d.bpm, rate: rateOf(d), beats: a.beat_times,
          aDrumBars: env ? env.v : null, fxOk: !!o.fxOk }));
        if (p.ok) { logPlan(kind, p); res = runRoll(d, p, o); }
      } else if (kind === "pad_lead") {
        const bBeatS = 60 / ((b && b.bpm) || 128), from = fin(o.bEntry) ? o.bEntry - 8 * 4 * bBeatS : null;
        const aEnv = fin(o.exitT) ? stemEnv(d, "other", o.exitT - 8 * o.bar, o.exitT, o.bar / 4) : null;
        const cs = host.mod.djMind && host.mod.djMind.core && host.mod.djMind.core.camelotScore;
        const ka = a.key && a.key.camelot, kb = b && b.analysis && b.analysis.key && b.analysis.key.camelot;
        p = planPadLead(Object.assign(base, { exitT: o.exitT, aBpm: d.bpm, aRate: rateOf(d), bBpm: b && b.bpm, bEntry: o.bEntry,
          style: o.style, recipe: o.recipe, sinceLast: padEntries - padLast,
          bPlaying: !!(b && b.playing), keyScore: cs && ka && kb ? cs(ka, kb) : null,
          bOtherEnv: b && from != null ? stemEnv(b, "other", Math.max(0, from), o.bEntry, bBeatS / 4) : null,
          aOtherRms: aEnv && aEnv.v.length ? median(aEnv.v) : null }));
        if (p.ok) { logPlan(kind, p); res = runPad(d, b, p, o); if (res) padLast = padEntries; }
      } else if (kind === "chant_gate") {
        const len = PHRASE_S(o);
        p = planChantGate(Object.assign(base, { lineT: o.lineT, bpm: d.bpm, rate: rateOf(d), beats: a.beat_times,
          vocals: a.vocal_active_regions, holdActive: !!o.holdActive, quiet: o.quiet, count: r.chants,
          energyNow: meanEnergy(a.energy_curve, a.energy_times, o.lineT - len, o.lineT),
          energyNext: meanEnergy(a.energy_curve, a.energy_times, o.lineT, o.lineT + len),
          vocalLive: !!(d.stems && d.stems.vocals && d.stemGain && d.stemGain.vocals),
          vocalBusy: !!((d._slices && d._slices.vocals) || (d._holds && d._holds.vocals) || d._artistSwap || d._artistChant) }));
        // S21: one "vocal" unit of the FX budget (a choice gate, so AI ACTIONS skip it)
        const fb = host.mod.fxBudget;
        if (p.ok && !o.onDemand && fb && typeof fb.spend === "function") {
          const s = fb.spend("vocal", 1, { phraseS: len, song: d.trackId || d.id });
          if (s && !s.ok) p = no("fx_budget", `FX budget: ${s.why}`);
        }
        if (p.ok) { r.chants++; logPlan(kind, p); res = runChant(d, p, o); }
      }
      if (p && p.ok && !res) p = no("deck", "the deck refused the booking (nothing armed)");
      return { plan: p, res, r };
    }
    const PHRASE_S = (o) => 8 * o.bar;
    let padEntries = 0, padLast = -1e9;              // planned entries seen / the one the last pad lead played on

    // Called by dj-mind on its ticks between phrase lines (never during a transition / hold / layer).
    // -> {busyS, why} when a slip loop was booked (the deck position is spoken for), else null.
    function tick(d, o) {
      if (!d || !d.playing) return null;
      const r = songOf(d);
      if (on("perc_bridge") && !r.bridged) {        // S11: the gate is the console itself, said once per song
        r.bridged = true;
        const x = attempt("perc_bridge", d, o);
        if (!x.plan.ok) refuse(d, "perc_bridge", "song", x.plan);
      }
      // Lane 8 pad lead: planned once per entry, when the entry is at most 9 bars ahead (8-bar window next)
      if (on("pad_lead") && o.exitT != null && r.padFor !== o.exitT && o.exitT > o.pos && (o.exitT - o.pos) / o.bar <= 9) {
        r.padFor = o.exitT;
        padEntries++;
        const x = attempt("pad_lead", d, o);
        if (!x.plan.ok) refuse(d, "pad_lead", o.exitT.toFixed(1), x.plan);
      }
      // S14 / S12: A's last bars before B's planned entry
      if (o.exitT != null && o.exitT > o.pos && (o.exitT - o.pos) / o.bar <= TEASE_BARS) {
        if (on("cue_tease") && r.teaseFor !== o.exitT) {
          r.teaseFor = o.exitT;                     // one plan per entry
          const x = attempt("cue_tease", d, o);
          if (!x.plan.ok) refuse(d, "cue_tease", o.exitT.toFixed(1), x.plan);
        }
        if (on("roll") && r.rollFor !== o.exitT && (o.exitT - o.pos) / o.bar <= 2) {
          r.rollFor = o.exitT;
          const x = attempt("roll", d, o);
          if (!x.plan.ok) refuse(d, "roll", o.exitT.toFixed(1), x.plan);
        }
        return null;
      }
      // S13: planned once per phrase, when the line is at most 4.5 bars ahead (the 16-beat window is next)
      if (on("slip_loop") && r.slipLine !== o.lineT && o.lineT - o.pos <= 4.5 * o.bar) {
        r.slipLine = o.lineT;
        const x = attempt("slip_loop", d, o);
        if (!x.plan.ok) refuse(d, "slip_loop", o.lineT.toFixed(1), x.plan);
        else { r.slipBooked = o.lineT; return x.res; }
      }
      // S20: planned once per phrase, 3 bars ahead (the 2-bar window next), never on a line a slip loop already owns
      if (on("chant_gate") && r.chantLine !== o.lineT && r.slipBooked !== o.lineT && o.lineT > o.pos && o.lineT - o.pos <= 3 * o.bar) {
        r.chantLine = o.lineT;
        const x = attempt("chant_gate", d, o);
        if (!x.plan.ok) refuse(d, "chant_gate", o.lineT.toFixed(1), x.plan);
      }
      return null;
    }

    // ---- AI ACTIONS: run one move now on the audible deck (choice gates skipped, safety gates kept) ----
    function hostDeck() {
      let best = null, lv = -1;
      for (const id of ["a", "b"]) {
        const d = host.decks && host.decks[id];
        if (!d || !d.playing) continue;
        const g = (d.crossfaderGain ? d.crossfaderGain.gain.value : 1) * (d.volumeGain ? d.volumeGain.gain.value : 1);
        if (g > lv) { lv = g; best = d; }
      }
      return best;
    }
    // ctx (optional): {deck, inTransition}; defaults read from the console
    function runNow(kind, ctx = {}) {
      let d = null;
      const done = (ok, why) => {
        host.ui.status(`${LABEL[kind] || kind}: ${ok ? "" : "refused, "}${why}`);
        host.log.step("artist_move_now", { deck: d ? d.id : undefined, decision: kind, why: ok ? why : `refused: ${why}` });
        return { ok, why };
      };
      if (!PLANNERS[kind]) return done(false, "unknown move");
      d = ctx.deck || hostDeck();
      if (!d) return done(false, "nothing is playing");
      const mind = host.mod.djMind;
      const bar = 240 / (d.bpm || 128), pos = d._currentPosition(), a = d.analysis || {};
      const lines = a.phrase_boundaries_8bar || [];
      const leadBars = { slip_loop: 4, pad_lead: 4, chant_gate: 2 }[kind] || 1;
      const lead = leadBars * bar + MIN_LEAD_S * rateOf(d);
      let lineT = lines.find((t) => t - pos >= lead);
      if (lineT == null) { lineT = pos; while (lineT - pos < lead) lineT += 8 * bar; }
      const planned = mind && mind.fireAt ? mind.fireAt(null) : null;
      const b = host.decks && host.decks[other(d.id)];
      const exitT = kind === "slip_loop" ? planned : fin(planned) && planned > pos + lead && planned - pos <= (kind === "pad_lead" ? 9 : TEASE_BARS) * bar ? planned : lineT;
      const inTransition = ctx.inTransition != null ? ctx.inTransition : !!(mind && typeof mind.busy === "function" && mind.busy());
      const x = attempt(kind, d, { pos, bar, entryT: d._mindEntry || 0, lineT, exitT, onDemand: true, inTransition,
        bEntry: b ? (b.playing ? null : b.startOffset || 0) : null, fxOk: true,
        mashupActive: !!(host.mod.mashup && host.mod.mashup.active), holdActive: false });
      return x.plan.ok ? done(true, x.res.why) : done(false, `${x.plan.gate}: ${x.plan.reason}`);
    }

    // cancel what is not booked yet; a running slip loop releases onto its shadow now. Booked stabs / rolls
    // are left to finish: they end on B's entry by construction and cutting them would click.
    function stop(d) {
      if (!d) return;
      (timers[d.id] || []).forEach(clearTimeout);
      timers[d.id] = [];
      if (d._slip && d.slipRelease) d.slipRelease();
      if (d._artistChant) {                           // an armed / running chant gate opens again now
        d._artistChant = null;
        const lg = d.stemLive && d.stemLive.vocals && d.stemLive.vocals.gain;
        if (lg) { const t = audioCtx.currentTime; lg.cancelScheduledValues(t); lg.setValueAtTime(1, t); if (d.playing && d.stemMix) d.stemMix(null, t + 0.03, 0.02); }
      }
    }

    // on-demand wiring: the AI ACTIONS register API when present, and `ai-action` events on djEvents
    const ACTION_IDS = { "artist-slip": "slip_loop", "artist-tease": "cue_tease", "artist-roll": "roll", "artist-perc": "perc_bridge",
      "artist-pad": "pad_lead", "artist-chant": "chant_gate" };
    let registered = false;
    const register = () => {
      const reg = host.mod.aiActions;
      if (registered || !reg || typeof reg.register !== "function") return;
      registered = true;
      for (const [id, kind] of Object.entries(ACTION_IDS)) reg.register(id, () => runNow(kind));
    };
    register();
    setTimeout(register, 0);                        // aiActions may mount after this module
    // the event path and the buttons' own clicks, used only when nothing registered; one run per 500 ms per move
    let lastRun = { kind: null, at: -1e9 };
    const fromUi = (id) => {
      const k = ACTION_IDS[id], t = host.clock.perfNow();
      if (!k || registered || (lastRun.kind === k && t - lastRun.at < 500)) return;
      lastRun = { kind: k, at: t };
      runNow(k);
    };
    const ev = host.mod.djEvents;
    if (ev && ev.addEventListener) ev.addEventListener("ai-action", (e) => fromUi(e && e.detail && e.detail.id));
    for (const btn of host.ui.queryAll("[data-ai-action]") || []) {
      if (ACTION_IDS[btn.dataset.aiAction]) btn.addEventListener("click", () => fromUi(btn.dataset.aiAction));
    }

    const api = { core, tick, stop, runNow, ACTION_IDS };
    host.mod.artistMoves = api;
    return api;
  }
  if (root.Engine) root.Engine.mount("artistMoves", create);
})(typeof window !== "undefined" ? window : globalThis);
