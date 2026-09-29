// AI Music Brain - LEARNED MOVES: the four techniques the set learner found in studied DJ sets that are
// played INSIDE a song, not as a transition (app/music_brain/set_learner.py, techniques.py learned_moves):
//
//   vocal_loop        one vocal line repeated back to back (chant / stutter): a vocal-stem slice of whole
//                     beats (1/2, 1 or 2 bars) on the beat grid, N plays, released on the phrase line.
//   vocal_resequence  the vocal re-cut in another order over the same beat: the phrase's vocal lines
//                     (cut where the vocal envelope enters a new line) replayed rotated, on the bar grid.
//   vocal_chop        short vocal hits re-triggered on the 1/8 or 1/16 grid under the beat: the loudest
//                     syllables of the vocal envelope, the original vocal ducked out for the window.
//   loop_extend       a break / intro extended by looping its last 16 (or 32) beats once more, hard cap 32
//                     beats of extension, released on the phrase line; also the filler while B's stems load.
//
// Everything with a decision is a pure function of measured inputs (`core`, node-tested): slice boundaries
// come from the vocal stem's envelope, the loop length from the beat grid and the room before the next
// phrase line, gains from the measured syllable / vocal levels, repeat counts from the sightings clamped by the
// room. A number that is NOT measured (no sighting, no envelope) is a constant and is listed in the plan's
// `fallbacks`, and logged. The runtime reaches the world only through the Host port (engine.js).
//
// Gates (a move that fails one does nothing): the kind is in the learned store and not disabled, no user rule
// forbids it, the toggles (ap-learned-toggle, ap-learned-<kind>) are on, a relaxed session, no transition /
// merge hold / vocal layer running, not while B's stems are being deferred (loop_extend excepted: it is the
// filler), the vocal really plays there (introGate / keepsVibe style stem energy checks), no other vocal on the
// master, room before the exit, per-song cap and spacing. Sub-bass: vocal moves only touch the vocal stem; the
// loop_extend loops ONE deck's whole mix. Ramps everywhere (>= 6 ms edges on slices, stemMix ramps), never a
// bare pitch move.
(function (root) {
  "use strict";

  const KINDS = ["vocal_loop", "vocal_resequence", "vocal_chop", "loop_extend"];
  const LABEL = { vocal_loop: "VOCAL LOOP", vocal_resequence: "VOCAL RE-CUT", vocal_chop: "VOCAL CHOPS", loop_extend: "LOOP EXTEND" };
  const PHRASE_BARS = 8;
  // Hard caps, in beats of the window / of the extension (KB [[Loops & Beat Jumps]]: 4, 8, 16, 32 beat loops).
  const CAP_BEATS = { vocal_loop: 32, vocal_resequence: 32, vocal_chop: 32, loop_extend: 32 };
  const MIN_RMS = 0.01;               // = stem-moves.js INTRO_MIN_RMS (-40 dBFS): below this a stem is not playing
  const REL_FLOOR = 0.15;             // = stem-moves.js REMIX_REL_FLOOR: a kept stem needs 15 % of the mix RMS
  const MAX_PER_SONG = 2, GAP_BARS = 32, MIN_BARS_ON_TRACK = 16, EXIT_GUARD_BARS = 24;
  const MIN_LEAD_S = 0.6;             // a window starts at least this far ahead (room to book it on the audio clock)
  const LOOP_LEN_BEATS = [8, 4, 2];   // vocal loop slice: 2 bars, 1 bar, 1/2 bar
  const EXTEND_LOOP_BEATS = 16;       // loop_extend step (32 while waiting for B)
  const LINE_MIN_S = 0.4, LINE_GAP_S = 0.25;
  const LOOP_LABELS = ["intro", "breakdown", "break"];

  // ------------------------------------------------------------------ small pure helpers
  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
  const num = (x) => (Number.isFinite(x) ? x : null);
  function median(a) {
    const s = a.filter(Number.isFinite).sort((x, y) => x - y);
    if (!s.length) return null;
    const m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }
  function quantile(a, q) {
    const s = a.filter(Number.isFinite).sort((x, y) => x - y);
    return s.length ? s[Math.min(s.length - 1, Math.floor(q * (s.length - 1) + 0.5))] : 0;
  }
  // the beat on the grid `t0 + k * beatS` nearest / at-or-before / at-or-after t
  function snapBeat(t, t0, beatS, mode = "round") {
    const x = (t - t0) / beatS, e = 1e-6;
    const k = mode === "floor" ? Math.floor(x + e) : mode === "ceil" ? Math.ceil(x - e) : Math.round(x);
    return t0 + k * beatS;
  }
  // stem-moves.js stemPlays (same floors): does stem n really play in this per-stem mean RMS? null energy: unmeasured, ok
  function stemPlays(energy, n) {
    if (!energy) return true;
    let mix = 0;
    for (const s of ["drums", "bass", "vocals", "other"]) mix += (energy[s] || 0) ** 2;
    return (energy[n] || 0) >= Math.max(MIN_RMS, REL_FLOOR * Math.sqrt(mix));
  }

  // RMS envelope of one stem over song time [t0, t1), one bin per hopS. Buffer position = (song t + lag) * ratio
  // (deck-controller.js _startStem). -> {t0, hop, v: [rms]}
  function envelope(ch, sr, t0, t1, hopS, lag = 0, ratio = 1) {
    const n = Math.max(0, Math.ceil((t1 - t0) / hopS - 1e-9)), v = new Array(n);
    for (let i = 0; i < n; i++) {
      const s0 = Math.max(0, Math.floor((t0 + i * hopS + lag) * ratio * sr));
      const s1 = Math.min(ch.length, Math.floor((t0 + (i + 1) * hopS + lag) * ratio * sr));
      let sum = 0, c = 0;
      for (let j = s0; j < s1; j += 4) { sum += ch[j] * ch[j]; c++; }
      v[i] = c ? Math.sqrt(sum / c) : 0;
    }
    return { t0, hop: hopS, v };
  }
  const envAt = (env, t) => { const i = Math.floor((t - env.t0) / env.hop + 1e-9); return i >= 0 && i < env.v.length ? env.v[i] : 0; };
  // share of [a, b) whose bins are above `thr`
  function coverage(env, a, b, thr) {
    let on = 0, n = 0;
    for (let t = a + env.hop / 2; t < b; t += env.hop) { n++; if (envAt(env, t) >= thr) on++; }
    return n ? on / n : 0;
  }
  // The vocal's lines: runs of bins above max(MIN_RMS, 20 % of the envelope's 90th percentile), gaps
  // shorter than gapS bridged, runs shorter than minS dropped. -> [{s, e, rms, peak}] (song s), plus the threshold.
  function vocalLines(env, o = {}) {
    if (!env || !env.v.length) return [];
    const thr = o.thr != null ? o.thr : Math.max(MIN_RMS, 0.2 * quantile(env.v, 0.9));
    const gapS = o.gapS != null ? o.gapS : LINE_GAP_S, minS = o.minS != null ? o.minS : LINE_MIN_S;
    const runs = [];
    let cur = null;
    env.v.forEach((x, i) => {
      const t = env.t0 + i * env.hop;
      if (x >= thr) {
        if (cur && t - cur.e <= gapS + env.hop) { cur.e = t + env.hop; cur.vs.push(x); }
        else { cur = { s: t, e: t + env.hop, vs: [x] }; runs.push(cur); }
      }
    });
    return runs.filter((r) => r.e - r.s >= minS).map((r) => ({ s: r.s, e: r.e, rms: r.vs.reduce((a, b) => a + b, 0) / r.vs.length, peak: Math.max(...r.vs), thr }));
  }

  // ------------------------------------------------------------------ the learned store
  // A user rule that says never / no / don't ... about a kind switches it off (heuristic, on the words the
  // rules use: `set_learner.add_user_rule`); the store's own `disabled` flag arrives as enabled:false.
  const KIND_WORDS = {
    vocal_loop: /vocal[ _-]?loop|chant|stutter/i, vocal_resequence: /re-?cut|re-?sequence|out of order|vocal[ _-]?resequence/i,
    vocal_chop: /chop/i, loop_extend: /loop[ _-]?extend|extend(ing)? (a |the )?(section|break|intro)|loop(ing)? (a |the )?(section|break|intro)/i,
  };
  const NEG = /\b(never|no|don'?t|do not|stop|avoid|without|not)\b/i;
  function ruleBlocks(rules, kind) {
    return (rules || []).find((r) => KIND_WORDS[kind] && KIND_WORDS[kind].test(String(r)) && NEG.test(String(r))) || null;
  }
  // The store payload (GET /api/learned/moves -> {moves}) as {kind: entry}; anything malformed is an empty store.
  function parseStore(payload) {
    const m = payload && typeof payload === "object" && payload.moves && typeof payload.moves === "object" ? payload.moves : {};
    const out = {};
    for (const k of KINDS) {
      const e = m[k];
      if (!e || typeof e !== "object") continue;
      out[k] = { kind: k, enabled: !!e.enabled, seen: Number(e.seen) || 0, rules: Array.isArray(e.rules) ? e.rules.map(String) : [],
        params: e.params && typeof e.params === "object" ? e.params : {} };
    }
    return out;
  }
  // Why kind may NOT run (null = allowed): store, disabled flag, user rules, toggles. flags: {all, [kind]: bool}
  function moveGate(store, kind, flags = {}) {
    if (flags.all === false) return { gate: "toggle", reason: "learned moves are off (ap-learned-toggle)" };
    if (flags[kind] === false) return { gate: "toggle", reason: `${kind} is off in the learned list` };
    const e = store && store[kind];
    if (!e || !(e.seen > 0)) return { gate: "store", reason: `${kind} was never seen in a studied set` };
    if (!e.enabled) return { gate: "store", reason: `${kind} is disabled in the learned store` };
    const r = ruleBlocks(e.rules, kind);
    if (r) return { gate: "user_rule", reason: `user rule: ${r}` };
    return null;
  }

  // ------------------------------------------------------------------ shared gates
  // c: {pos, rate, bar, barsOnTrack, barsToExit (null: none planned), atBar, used [kinds], count, lastAtBar,
  //     mashupActive, relaxed, deferring, onlyLoop (loop_extend may run while deferring)}
  function songGate(c, kind) {
    if (c.relaxed) return { gate: "relaxed", reason: "relaxed session: no learned moves" };
    if (c.mashupActive) return { gate: "vocal_layer", reason: "a vocal layer is running" };
    if (c.deferring && kind !== "loop_extend") return { gate: "b_deferred", reason: "B's stems are still loading" };
    if ((c.count || 0) >= MAX_PER_SONG) return { gate: "cap", reason: `learned move cap ${MAX_PER_SONG} per song reached` };
    if ((c.used || []).includes(kind)) return { gate: "cap", reason: `${kind} already played on this song` };
    if (c.lastAtBar != null && c.atBar - c.lastAtBar < GAP_BARS) return { gate: "cooldown", reason: `${Math.round(c.atBar - c.lastAtBar)} bars since the last learned move (min ${GAP_BARS})` };
    if ((c.barsOnTrack || 0) < MIN_BARS_ON_TRACK) return { gate: "early", reason: `first ${MIN_BARS_ON_TRACK} bars of the song` };
    return null;
  }
  const no = (gate, reason) => ({ ok: false, gate, reason });

  // Vocal moves: the vocal must really play here (measured stem energy) and nobody else may sing on the master.
  function vocalGate(c) {
    if (c.othersVocal) return no("vocal_clash", "the other deck's vocal is on the master");
    if (!c.env || !c.env.v.length) return no("no_envelope", "the vocal stem's envelope could not be measured");
    if (!stemPlays(c.energy, "vocals")) return no("silent_stem", "the vocal stem is silent or far under the mix here");
    return null;
  }
  function window0(c) {                      // the phrase's grid: first bar line at least MIN_LEAD_S ahead, and the phrase end
    const tMin = c.pos + MIN_LEAD_S * c.rate;
    const start = c.lineT + Math.max(0, Math.ceil((tMin - c.lineT) / c.bar - 1e-6)) * c.bar;
    return { tMin, start, end: c.lineT + PHRASE_BARS * c.bar };
  }
  // the move must end before the exit guard
  function exitBlock(c, endT) {
    if (c.exitT != null && endT > c.exitT - 4 * c.bar) return no("exit_guard", `the move would end ${((endT - c.exitT) / c.bar + 4).toFixed(1)} bars inside the exit guard (4 bars before the exit)`);
    if (c.barsToExit != null && c.barsToExit < EXIT_GUARD_BARS) return no("exit_guard", `${Math.round(c.barsToExit)} bars to the planned exit (min ${EXIT_GUARD_BARS})`);
    return null;
  }

  // ------------------------------------------------------------------ vocal_loop
  // c also: {env (vocal stem envelope over the phrase), params {repeats, line_s} from the store, energy}
  function planVocalLoop(c) {
    const g = vocalGate(c) || null;
    if (g) return g;
    const beat = c.bar / 4, P = c.params || {}, fallbacks = [];
    const { tMin, end } = window0(c);
    const lines = vocalLines(c.env).filter((l) => l.e > tMin && l.s < end - 4 * beat);
    if (!lines.length) return no("no_line", "no vocal line ahead in this phrase");
    let lineS = num(P.line_s);
    if (lineS == null) { lineS = 4 * beat; fallbacks.push("line_s"); }
    let best = null;
    for (const ln of lines) {
      let start = snapBeat(ln.s, c.lineT, beat, "floor");
      if (start < tMin) start = snapBeat(tMin, c.lineT, beat, "ceil");
      const avail = ln.e - start;
      for (const L of LOOP_LEN_BEATS) {
        const dur = L * beat;
        if (dur > avail + 0.5 * beat) continue;
        if (coverage(c.env, start, start + dur, ln.thr) < 0.6) continue;
        const score = Math.abs(dur - lineS);
        if (!best || score < best.score) best = { start, L, dur, score, line: ln };
      }
      if (best) break;                     // the earliest line that fits
    }
    if (!best) return no("no_slice", "no vocal line fills a whole 1/2 bar to 2 bars");
    let repeats = num(P.repeats);
    if (repeats == null) { repeats = 1; fallbacks.push("repeats"); }
    let plays = 1 + clamp(Math.round(repeats), 1, 3);
    const room = Math.floor((end - best.start) / beat + 1e-6);
    const cap = Math.min(CAP_BEATS.vocal_loop, room);
    while (plays > 1 && plays * best.L > cap) plays--;
    if (plays < 2) return no("no_room", `${room} beats to the phrase line, a ${best.L}-beat line does not repeat`);
    const beats = plays * best.L, endT = best.start + beats * beat;
    const x = exitBlock(c, endT);
    if (x) return x;
    const slices = Array.from({ length: plays }, (_, i) => ({ from: best.start, dur: best.dur, off: i * best.dur }));
    return { ok: true, kind: "vocal_loop", stem: "vocals", start: best.start, end: endT, beats, slices, fallbacks,
      grid_err_s: Math.abs(best.start - snapBeat(best.start, c.lineT, beat)), cap_beats: CAP_BEATS.vocal_loop,
      params: { loop_beats: best.L, plays, line_s: +best.dur.toFixed(2), sight_line_s: lineS, sight_repeats: repeats,
        line_rms: +best.line.rms.toFixed(4), ends_on_line: Math.abs(endT - end) < 0.05 },
      why: `${best.L}-beat vocal line x${plays} (${beats} beats), on the beat grid${Math.abs(endT - end) < 0.05 ? ", back on the phrase line" : ""}` };
  }

  // ------------------------------------------------------------------ vocal_resequence
  // c also: {variant (0..): which rotation}
  function planResequence(c) {
    const g = vocalGate(c);
    if (g) return g;
    const beat = c.bar / 4, P = c.params || {}, fallbacks = [];
    const { start, end } = window0(c);
    const winBeats = Math.floor((end - start) / beat + 1e-6);
    if (winBeats < 8) return no("no_room", `${winBeats} beats left in this phrase (need 8)`);
    const winBeatsCap = Math.min(winBeats, CAP_BEATS.vocal_resequence);
    const stop = start + winBeatsCap * beat;
    const lines = vocalLines(c.env).filter((l) => l.e > start && l.s < stop);
    // cuts: the window start, then every line onset at least 2 beats after the previous cut (at or before the onset)
    const cuts = [start];
    for (const ln of lines) {
      const cut = snapBeat(ln.s, c.lineT, beat, "floor");
      if (cut >= cuts[cuts.length - 1] + 2 * beat && cut <= stop - 2 * beat) cuts.push(cut);
    }
    cuts.push(stop);
    let tiles = cuts.slice(0, -1).map((a, i) => ({ from: a, beats: Math.round((cuts[i + 1] - a) / beat) }));
    let maxTiles = num(P.lines) != null ? clamp(Math.round(P.lines * 2), 3, 6) : (fallbacks.push("lines"), 4);
    while (tiles.length > maxTiles) {        // merge the shortest tile into its neighbour
      let i = tiles.reduce((b, t, k) => (t.beats < tiles[b].beats ? k : b), 0);
      const j = i === 0 ? 1 : i - 1, a = Math.min(i, j), b = Math.max(i, j);
      tiles.splice(a, 2, { from: tiles[a].from, beats: tiles[a].beats + tiles[b].beats });
    }
    if (tiles.length < 2) return no("no_lines", "fewer than two vocal lines in this phrase to re-order");
    if (tiles.some((t) => t.beats > 16)) return no("long_tile", "a vocal line longer than 4 bars: no clean cut");
    const voiced = tiles.filter((t) => coverage(c.env, t.from, t.from + t.beats * beat, lines[0] ? lines[0].thr : MIN_RMS) >= 0.3);
    if (voiced.length < 2) return no("no_lines", "fewer than two voiced tiles to re-order");
    const rot = 1 + ((c.variant || 0) % (tiles.length - 1));
    const order = tiles.slice(rot).concat(tiles.slice(0, rot));
    const slices = [];
    let off = 0;
    for (const t of order) { slices.push({ from: t.from, dur: t.beats * beat, off }); off += t.beats * beat; }
    const total = tiles.reduce((s, t) => s + t.beats, 0), endT = start + total * beat;
    const x = exitBlock(c, endT);
    if (x) return x;
    return { ok: true, kind: "vocal_resequence", stem: "vocals", start, end: endT, beats: total, slices, fallbacks,
      grid_err_s: Math.abs(start - snapBeat(start, c.lineT, c.bar)), cap_beats: CAP_BEATS.vocal_resequence,
      params: { tiles: tiles.length, tile_beats: tiles.map((t) => t.beats), order: order.map((t) => tiles.indexOf(t)), rotation: rot, window_beats: total },
      why: `${tiles.length} vocal lines re-cut in a new order over the same beat (${total} beats from a bar line)` };
  }

  // ------------------------------------------------------------------ vocal_chop
  // c also: {envFull (finer envelope over the source region, hop <= beat/4), params {frag_s, frags, span_s}}
  function planChop(c) {
    const g = vocalGate(c);
    if (g) return g;
    const beat = c.bar / 4, P = c.params || {}, fallbacks = [];
    const env = c.envFull || c.env;
    const { start, end } = window0(c);
    const room = Math.floor((end - start) / beat + 1e-6);
    if (room < 16) return no("no_room", `${room} beats left in this phrase (need 16 = 4 bars)`);
    let spanS = num(P.span_s);
    if (spanS == null) { spanS = 4 * c.bar; fallbacks.push("span_s"); }
    const bars = spanS / c.bar >= 7 && room >= 32 ? 8 : 4;
    const winBeats = Math.min(CAP_BEATS.vocal_chop, bars * 4, room);
    const stop = start + winBeats * beat;
    const x = exitBlock(c, stop);
    if (x) return x;
    // syllables: local maxima of the vocal envelope over the region before the window and the window itself
    const v = env.v, hop = env.hop, srcTo = stop;
    const i0 = Math.max(0, Math.floor((Math.max(c.entryT || 0, start - 16 * c.bar) - env.t0) / hop)), i1 = Math.min(v.length, Math.floor((srcTo - env.t0) / hop));
    const peakThr = Math.max(MIN_RMS, 0.5 * quantile(v.slice(i0, i1), 0.95));
    const peaks = [];
    for (let i = Math.max(1, i0); i < i1 - 1; i++) {
      if (v[i] >= peakThr && v[i] >= v[i - 1] && v[i] > v[i + 1]) {
        let a = i, b = i;
        while (a > 0 && v[a - 1] >= 0.5 * v[i]) a--;
        while (b < v.length - 1 && v[b + 1] >= 0.5 * v[i]) b++;
        peaks.push({ i, t: env.t0 + i * hop, val: v[i], s: env.t0 + a * hop, width: (b - a + 1) * hop });
      }
    }
    peaks.sort((p, q) => q.val - p.val);
    const picked = [];
    for (const p of peaks) if (picked.every((q) => Math.abs(q.t - p.t) >= beat)) picked.push(p);
    let nHits = num(P.frags) != null ? clamp(Math.round(P.frags), 3, 8) : (fallbacks.push("frags"), 6);
    const syl = picked.slice(0, nHits);
    if (syl.length < 3) return no("no_syllables", `${syl.length} clear vocal syllables (need 3)`);
    const grid = median(syl.map((p) => p.width)) < beat / 4 * 1.5 ? 16 : 8;     // slots per bar: 1/16 for very short syllables
    const pattern = grid === 8 ? [1, 3, 5, 7] : [3, 6, 11, 14];     // off-beat slots: under the kick
    const target = median(v.slice(Math.max(0, Math.floor((start - 8 * c.bar - env.t0) / hop)), Math.floor((start - env.t0) / hop)).filter((x) => x >= peakThr * 0.5)) || syl[syl.length - 1].val;
    const maxDur = beat, slices = [];
    let idx = 0;
    for (let b = 0; b < winBeats / 4; b++) {
      for (const slot of pattern) {
        const s = syl[idx % syl.length]; idx++;
        const dur = clamp(s.width, 2 * hop, maxDur);
        const off = b * c.bar + slot * (c.bar / grid);
        if (off + dur > winBeats * beat + 1e-6) continue;
        slices.push({ from: s.s, dur, off, gain: +clamp(target / s.val, 0.5, 1).toFixed(2) });
      }
    }
    if (slices.length < 4) return no("no_hits", "the pattern placed fewer than 4 hits");
    const endT = start + winBeats * beat;
    return { ok: true, kind: "vocal_chop", stem: "vocals", start, end: endT, beats: winBeats, slices, fallbacks,
      grid_err_s: Math.abs(start - snapBeat(start, c.lineT, c.bar)), cap_beats: CAP_BEATS.vocal_chop,
      params: { grid: `1/${grid === 16 ? 16 : 8}`, syllables: syl.length, hits: slices.length, hit_s: +median(slices.map((s) => s.dur)).toFixed(2),
        gain_median: median(slices.map((s) => s.gain)), window_beats: winBeats, sight_frag_s: num(P.frag_s) },
      why: `${slices.length} vocal chops from ${syl.length} syllables on the 1/${grid === 16 ? 16 : 8} grid over ${winBeats / 4} bars, original vocal ducked out` };
  }

  // ------------------------------------------------------------------ loop_extend
  // c also: {label (phrase section label), nextLabel, barRms [mix RMS per bar over the phrase] | null, vocals (regions), waiting}
  function planLoopExtend(c) {
    const beat = c.bar / 4, fallbacks = [], end = c.lineT + PHRASE_BARS * c.bar;
    if (!c.waiting && !LOOP_LABELS.includes(c.label)) return no("section", `a ${c.label || "unlabelled"} section: only a break or an intro is extended`);
    const L = EXTEND_LOOP_BEATS;               // one 16-beat step; while waiting for B up to the cap (2 steps)
    const cap = CAP_BEATS.loop_extend, passes = c.waiting ? clamp(Math.floor(cap / L), 1, 2) : 1;
    const ext = passes * L;
    const start = end - L * beat;
    if (start < c.pos + MIN_LEAD_S * c.rate) return no("late", "the loop start is already behind the playhead");
    const x = exitBlock(c, end + ext * beat);
    if (x && !c.waiting) return x;
    // the loop repeats: its bars must be steady, and free of sung lines (a chopped singer)
    const bars = (c.barRms || []).slice(-Math.ceil(L / 4));
    if (bars.length >= 2) {
      const hi = Math.max(...bars), lo = Math.min(...bars);
      if (lo < MIN_RMS || hi / Math.max(lo, 1e-9) > 3) return no("unsteady", `the loop's bars swing ${(hi / Math.max(lo, 1e-9)).toFixed(1)}x in level`);
    } else fallbacks.push("bar_levels");
    let sung = 0;
    for (const [a, b] of c.vocals || []) sung += Math.max(0, Math.min(b, end) - Math.max(a, start));
    if (sung / (end - start) > 0.25) return no("vocal_seam", `${Math.round((100 * sung) / (end - start))} % of the loop is sung: it would stutter the singer`);
    return { ok: true, kind: "loop_extend", stem: null, start, end, beats: ext, slices: null, fallbacks,
      grid_err_s: 0, cap_beats: cap, loop: { start, beats: L, passes, release: end },
      params: { loop_beats: L, passes, extension_beats: ext, waiting: !!c.waiting, label: c.label || null },
      why: `${c.waiting ? "waiting for B: " : ""}last ${L} beats of this ${c.label || "section"} looped ${passes + 1}x (+${ext} beats), release on the phrase line` };
  }

  // ------------------------------------------------------------------ choosing one
  const PLANNERS = { vocal_loop: planVocalLoop, vocal_resequence: planResequence, vocal_chop: planChop, loop_extend: planLoopExtend };
  // c.vocalShare (0..1 over the phrase) steers the order: a sung phrase prefers vocal moves, an instrumental one a loop.
  function ctxWeight(kind, share) {
    if (kind === "loop_extend") return share >= 0.5 ? 0.3 : 1;
    return share >= 0.5 ? { vocal_loop: 1, vocal_resequence: 0.9, vocal_chop: 0.8 }[kind] : share >= 0.3 ? { vocal_chop: 0.8, vocal_resequence: 0.7, vocal_loop: 0.6 }[kind] : 0.2;
  }
  // Every allowed kind is planned; the best-fitting plan wins. -> {plan | null, refusals: [{kind, gate, reason}]}
  // (a refusal names the gate that failed, for the step log and the sim's per-gate counts)
  function pick(c, store, flags = {}) {
    const refusals = [], plans = [];
    const maxSeen = Math.max(1, ...KINDS.map((k) => (store && store[k] && store[k].seen) || 0));
    for (const kind of KINDS) {
      const g = moveGate(store, kind, flags) || songGate(c, kind);
      if (g) { refusals.push({ kind, gate: g.gate, reason: g.reason }); continue; }
      let p;
      try { p = PLANNERS[kind](Object.assign({}, c, { params: (store[kind] && store[kind].params) || {} })); } catch (e) { p = no("error", String(e && e.message || e)); }
      if (!p.ok) { refusals.push({ kind, gate: p.gate, reason: p.reason }); continue; }
      plans.push({ p, w: ctxWeight(kind, c.vocalShare || 0) + 0.5 * (store[kind].seen / maxSeen) });
    }
    plans.sort((a, b) => b.w - a.w);
    return { plan: plans.length ? plans[0].p : null, refusals };
  }

  const core = { KINDS, LABEL, CAP_BEATS, MAX_PER_SONG, GAP_BARS, EXIT_GUARD_BARS, MIN_LEAD_S, MIN_RMS, LOOP_LEN_BEATS, EXTEND_LOOP_BEATS,
    median, quantile, snapBeat, stemPlays, envelope, envAt, coverage, vocalLines, ruleBlocks, parseStore, moveGate, songGate, vocalGate,
    planVocalLoop, planResequence, planChop, planLoopExtend, pick };
  root.learnedMovesCore = core;
  if (typeof module !== "undefined" && module.exports) module.exports = core;

  // ------------------------------------------------------------------ runtime (Host port only)
  function create({ host }) {
    const audioCtx = host.audio;
    const { setTimeout, clearTimeout } = host.clock;
    const timers = { a: [], b: [] };
    let store = null, loading = null, loadedAt = -Infinity;
    const tag = new Map();                                    // "deckId:kind:phrase" refusals already said

    // the learned store, once per set (and again after 5 minutes); until it lands nothing runs
    function load() {
      if (loading) return loading;
      loading = host.api.fetch("/api/learned/moves").then((r) => r.json()).then((j) => { store = parseStore(j); loadedAt = host.clock.now(); },
        (e) => { console.info("learned moves: store unavailable -", e && e.message); }).finally(() => { loading = null; });
      return loading;
    }
    const flags = () => {
      const f = { all: host.ui.flag("ap-learned-toggle", true) };
      for (const k of KINDS) f[k] = host.ui.flag(`ap-learned-${k}`, true);
      return f;
    };
    const cancel = (id) => { (timers[id] || []).forEach(clearTimeout); timers[id] = []; };
    const other = (id) => (id === "a" ? "b" : "a");
    // the other deck sings on the master: playing, on air, its vocal stem up (or a full mix over an analysed vocal)
    function othersVocal(d) {
      const o = host.decks && host.decks[other(d.id)];
      if (!o || !o.playing || !(o._onAir && o._onAir())) return false;
      if (o.stemState) return (o.stemState.vocals || 0) >= 0.126 || (o.stemState.bus || 0) >= 0.126;
      const vr = o.analysis && o.analysis.vocal_active_regions;
      return !!(vr && vr.length && host.mod.stemMoves && host.mod.stemMoves.vocalShare(vr, o._currentPosition(), o._currentPosition() + 4) > 0.3);
    }
    function vocalEnv(d, t0, t1, hop) {
      const st = d.stems, b = st && st.vocals;
      if (!b || !b.getChannelData) return null;
      return envelope(b.getChannelData(0), b.sampleRate, Math.max(0, t0), t1, hop, st.lag || 0, st.ratio || 1);
    }
    function say(d, label, why, extra = {}) { host.bus.emit("ai-activity", Object.assign({ kind: "learned_move", deck: d.id, label, why }, extra)); }

    // Run one slice plan on deck d: stem mode from the window start, the slices, the full mix again after.
    // Nothing is armed when the deck cannot play the window (stems not live, a hold / slice already running).
    function runSlices(d, plan) {
      const sm = host.mod.stemMoves;
      const at = sm.audioAt(d, plan.start), lead = at - audioCtx.currentTime;
      if (lead < 0.35) return { ok: false, gate: "late", reason: `the window starts in ${lead.toFixed(2)} s (min 0.35)` };
      if (!d.stemsLiveAt || !d.stemsLiveAt(at)) return { ok: false, gate: "stems_not_live", reason: "the deck's stems are not sounding at the window start" };
      const rate = (d._playbackRate && d._playbackRate()) || 1;
      const until = at + (plan.beats * (240 / (d.bpm || 128)) / 4) / rate;
      timers[d.id].push(setTimeout(() => {
        if (!d.playing) return;
        if (!d.stemMix({}, at - 0.01, 0.01)) return void console.info(`learned move ${plan.kind} skipped: stem mode refused`);
        const res = d.stemSlices(plan.stem, plan.slices, at);
        if (!res) { d.stemMix(null, at, 0.02); return void console.info(`learned move ${plan.kind} skipped: slices refused`); }
        timers[d.id].push(setTimeout(() => { if (d.playing) d.stemMix(null, until + 0.03, 0.02); }, Math.max(0, (until - audioCtx.currentTime) * 1000 - 200)));
      }, Math.max(0, lead * 1000 - 250)));
      return { ok: true, at, until };
    }

    // One call per phrase from dj-mind.js. o: {pos, bar, phrase, lineT, entryT, exitT, st (dj-mind state), loop (loop helper)}
    // -> {kind, busyS, why} when a move was booked, else null (refusals are logged, once per kind and phrase).
    function tick(d, o) {
      if (!store) { if (host.clock.now() - loadedAt > 300000) load(); return null; }
      const dl = host.ui.flag("ap-learned-toggle", true);
      if (!dl || !d || !d.playing) return null;
      const f = flags();
      const st = o.st || {}, rate = (d._playbackRate && d._playbackRate()) || 1;
      const r = d._learned || (d._learned = { used: [], count: 0, lastAtBar: null, variant: 0 });
      const beat = o.bar / 4, sm = host.mod.stemMoves;
      const atBar = (o.lineT - (o.entryT || 0)) / o.bar;
      const waiting = !!(host.session && host.session.deferring);
      const c = {
        pos: o.pos, rate, bar: o.bar, lineT: o.lineT, entryT: o.entryT || 0, exitT: o.exitT, barsOnTrack: st.barsOnTrack != null ? st.barsOnTrack : atBar,
        barsToExit: st.barsToExit != null ? st.barsToExit : null, atBar, used: r.used, count: r.count, lastAtBar: r.lastAtBar,
        mashupActive: !!st.mashupActive, relaxed: !!(host.session && host.session.relaxed), deferring: waiting, waiting,
        othersVocal: othersVocal(d), variant: r.variant, label: st.phraseSection, nextLabel: st.nextPhraseSection, onAir: st.onAir,
        vocalShare: sm ? sm.vocalShare(d.analysis && d.analysis.vocal_active_regions, o.lineT, o.lineT + PHRASE_BARS * o.bar) : 0,
        vocals: d.analysis && d.analysis.vocal_active_regions,
      };
      // measured inputs, only when a vocal kind could run (the envelope reads the stem buffers)
      if (d.stemsReady && sm) {
        c.env = vocalEnv(d, o.lineT, o.lineT + PHRASE_BARS * o.bar, beat / 4);
        c.envFull = vocalEnv(d, Math.max(o.entryT || 0, o.lineT - 16 * o.bar), o.lineT + PHRASE_BARS * o.bar, beat / 4);
        c.energy = sm.remixEnergy ? sm.remixEnergy(d, o.lineT, o.bar, PHRASE_BARS) : null;
        const eb = sm.stemEnergyBars ? sm.stemEnergyBars(d, o.lineT, o.bar, PHRASE_BARS) : null;
        if (eb) c.barRms = eb.drums.map((_, i) => Math.sqrt(["drums", "bass", "vocals", "other"].reduce((s, n) => s + eb[n][i] ** 2, 0)));
      }
      const res = pick(c, store, f);        // a stemless deck has no envelope: the vocal kinds refuse by name
      const gk = `${d.id}:${o.phrase}`;
      const refused = res.refusals.filter((x) => !(x.gate === "toggle" || x.gate === "store" || x.gate === "user_rule"));
      if (refused.length && !tag.has(gk)) {
        tag.set(gk, 1);
        if (tag.size > 200) tag.clear();
        for (const x of refused) console.info(`learned move ${x.kind} skipped: ${x.gate}: ${x.reason}`);
      }
      if (!res.plan) return null;
      const plan = res.plan;
      let run = { ok: true };
      if (plan.kind === "loop_extend") {
        if (!o.loop || !o.loop.extend) return null;
        o.loop.extend(plan);
        run = { ok: true, at: audioCtx.currentTime + (plan.start - o.pos) / rate, until: audioCtx.currentTime + (plan.end + plan.beats * beat - o.pos) / rate };
      } else {
        cancel(d.id);
        run = runSlices(d, plan);
      }
      if (!run.ok) { console.info(`learned move ${plan.kind} skipped: ${run.gate}: ${run.reason}`); return null; }
      r.used.push(plan.kind); r.count++; r.lastAtBar = atBar; r.variant++;
      const why = `${plan.why}${plan.fallbacks.length ? ` (constants: ${plan.fallbacks.join(", ")})` : ""}`;
      console.info(`learned move ${plan.kind}: ${why}`);
      say(d, `LEARNED MOVE · ${LABEL[plan.kind]}`, why, { move: plan.kind, params: plan.params, fallbacks: plan.fallbacks, t0: run.at, t1: run.until,
        beats: plan.beats, cap_beats: plan.cap_beats, grid_err_s: +plan.grid_err_s.toFixed(4), seen: store[plan.kind].seen });
      return { kind: plan.kind, why, busyS: Math.max(0, ((plan.end + (plan.kind === "loop_extend" ? plan.beats * beat : 0)) - o.pos) / rate) };
    }
    // the wait filler: dj-mind's hold loop engaged (nothing scheduled, B not ready). Logged as the learned kind when it is on.
    function noteFiller(d, span) {
      if (!store || !host.ui.flag("ap-learned-toggle", true) || moveGate(store, "loop_extend", flags())) return false;
      say(d, `LEARNED MOVE · ${LABEL.loop_extend}`, `waiting for the next song: ${span.bars} bars looped until it is ready`,
        { move: "loop_extend", params: { loop_beats: span.bars * 4, waiting: true }, fallbacks: [], seen: store.loop_extend.seen });
      return true;
    }
    function stop(d) {
      if (!d) return;
      cancel(d.id);
      if (d._slices && d._slices.vocals && d.releaseSlices) { d.releaseSlices("vocals"); if (d.stemMix) d.stemMix(null, 0, 0.02); }
    }
    load();
    const api = { core, tick, cancel, stop, noteFiller, load, get store() { return store; } };
    host.mod.learnedMoves = api;
    return api;
  }
  if (root.Engine) root.Engine.mount("learnedMoves", create);
})(typeof window !== "undefined" ? window : globalThis);
