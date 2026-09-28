// AI Music Brain - stem moves on live decks (deck-controller.js stemMix).
//
//  breakdown  The USB002 move (Fred again.. & Bangalter, leavemealone at 74:22,
//             measured with 4-stem Demucs): the vocal never stops while the
//             song is stripped and rebuilt over 40 bars: drums out -> bass out
//             (voice alone) -> bass back, still no kick -> bass out again while
//             drums creep back (build) -> everything slams back on the phrase
//             line. Lets a famous song play in full without sounding long.
//  handoff    Crossfade with ONE singer: the incoming deck enters as its
//             instrumental while the outgoing vocal moves to the vocal bus
//             (past A's EQ/fader) and keeps singing over B's beat; B's own
//             vocal returns as A's fades on the last bar.
//  instrumental  Mute a deck's vocal (hold loops that cross a sung line).
//
// All times on the audio clock. Pure schedules are exported for node checks.
(function (root) {
  "use strict";

  // [bar, stem targets, ramp in beats]; bar counted from the phrase line.
  const BREAKDOWN = {
    40: [
      [0, { drums: 0 }, 1],
      [4, { bass: 0 }, 1],
      [12, { bass: 1 }, 0.25],
      [24, { bass: 0 }, 1],
      [32, { drums: 0.35 }, 16],       // drums creep back over 4 bars
      [39.75, { drums: 0.7 }, 1],
      [40, null, 0.1],                 // full drop on the line
    ],
    24: [
      [0, { drums: 0 }, 1],
      [4, { bass: 0 }, 1],
      [8, { bass: 1 }, 0.25],
      [16, { bass: 0 }, 1],
      [20, { drums: 0.4 }, 12],
      [24, null, 0.1],
    ],
  };
  const BREAKDOWN_MIN_VOCAL = 0.5;     // the voice carries the stripped bars
  const BREAKDOWN_MIN_BARS_IN = 32;    // let the song establish itself first
  const BREAKDOWN_TAIL_BARS = 16;      // room after the drop before the exit

  function vocalShare(regions, a, b) {
    if (!regions || !regions.length || b <= a) return 0;
    let cov = 0;
    for (const [s, e] of regions) cov += Math.max(0, Math.min(e, b) - Math.max(s, a));
    return cov / (b - a);
  }
  // Which breakdown fits here, or null. ctx: {bar, pos, duration, exitAt,
  // barsOnTrack, vocals, famous, used}
  function breakdownFits(ctx) {
    if (!ctx.famous || ctx.used || (ctx.barsOnTrack || 0) < BREAKDOWN_MIN_BARS_IN) return null;
    const limit = Math.min(ctx.duration - 8 * ctx.bar, ctx.exitAt != null ? ctx.exitAt : Infinity);
    for (const bars of [40, 24]) {
      const end = ctx.pos + (bars + BREAKDOWN_TAIL_BARS) * ctx.bar;
      if (end > limit) continue;
      if (vocalShare(ctx.vocals, ctx.pos, ctx.pos + bars * ctx.bar) < BREAKDOWN_MIN_VOCAL) continue;
      return bars;
    }
    return null;
  }
  // Handoff when both decks run stems, the keys sit together and the outgoing
  // song is actually singing in the blend window.
  function handoffFits(ctx) {
    return !!(ctx.outStems && ctx.inStems && ctx.keyScore >= 0.8 && ctx.outVocal >= 0.3);
  }
  // ---- intro stem ([[Stems Transition]], user: "at least one appropriate stem
  // should be playing before crossfade starts"). B never appears through the
  // crossfader as a full mix: it comes in on ONE layer under A first.
  //   keys agree   B's `other` (synths/pads) or drums, whichever B really has
  //                energy in at its entry (measured from the decoded stems)
  //   keys clash   B's drums (percussion has no key)
  //   never        B's bass: A owns the sub until the swap line ([[Bass Swap]])
  //   B's voice    only when B has nothing else there and A is not singing
  const STEMS = ["drums", "bass", "vocals", "other"];
  const INTRO_LEVEL = { other: 0.8, drums: 0.7, vocals: 0.8 };
  const INTRO_MIN_RMS = 0.01;          // ~ -40 dBFS: below this the stem is not really playing
  // c: {keyClash, aSings, bSings, energy: B's mean RMS per stem over the intro
  // window ({drums, bass, vocals, other}) or null when unmeasured}
  function pickIntro(c) {
    if (c.keyClash) return "drums";
    const e = c.energy;
    if (!e) return "other";
    const lvl = (n) => e[n] || 0;
    if (lvl("other") >= INTRO_MIN_RMS || lvl("drums") >= INTRO_MIN_RMS) {
      return lvl("other") >= 0.5 * lvl("drums") ? "other" : "drums";   // a pad beats a kick at equal weight
    }
    if (!c.aSings && c.bSings && lvl("vocals") >= INTRO_MIN_RMS) return "vocals";
    return "other";
  }
  // The intro stem plays alone (under A) for one phrase before the crossfade
  // moves: 8 bars, 4 in a short window.
  function introBars(bars) { return bars >= 16 ? 8 : Math.max(2, bars / 2); }

  // ---- loudness floor (user: "when crossfading it shouldn't mute stems where
  // the master goes almost silent"). Through a stem transition the master stays
  // within LEVEL_FLOOR_DB of A's level just before it (1-beat loudness window).
  // 8 dB: the equal-power centre (-3 dB) plus a stripped layer still passes;
  // both beats gone with only a quiet pad left (~ -10 dB and worse) does not.
  // And some stem, on some deck, is always up at >= -18 dB (AUDIBLE_GAIN).
  const LEVEL_FLOOR_DB = 8;
  const AUDIBLE_GAIN = 0.126;
  const FADER_PARK_BARS = 1;
  // No decoded stems to measure: typical RMS share of each stem in a mix. An
  // assumption, not a measurement (only the ratios matter).
  const TYPICAL_SHARE = { drums: 0.45, bass: 0.45, vocals: 0.35, other: 0.35 };
  // Moves whose dip IS the move: allowed only when that move was chosen, on a
  // phrase line. A plan carries `dipAllowed: DIP_ALLOWED.x`; the check reports
  // it instead of silently skipping.
  const DIP_ALLOWED = Object.freeze({
    breakdown: "strip & rebuild (breakdown)",
    subdrop: "dj-mind subdrop (sub out on purpose)",
    riffRelease: "riff over rap: release into A's own drumless breakdown",
    build: "pre-drop build (Drop Swap / Double Drop)",
    brake: "brake / spin-down",
    echo: "echo-out tail (the effect carries the energy)",
    hookDrop: "hook drop: beat out under the emotional line, voice alone, then the drop",
  });

  // Stem blend: a transition done with stems instead of EQ. Each layer has one
  // owner at a time. bars = 8 or 16 (32 for mashup kinds). aSings / bSings:
  // vocals in the window. o: {intro (pickIntro), introLevel, keepA (A's synths
  // stay through the second half: the loudness fix when B's beat is thin)}.
  // Returns [{bar, deck: "out"|"in", stems, ramp (bars)}], bars from B's entry.
  function stemBlendPlan(kind, bars, aSings, bSings, keyClash = false, o = {}) {
    const L = bars, swap = L / 2, ev = [];
    if (kind === "double") {
      // [[Double Drop]]: both drops land together on bar 0, so B's drums + bass
      // ARE its entry (the rule's deliberate exception): B owns drums + bass, A
      // keeps its tops and voice; B's tops and voice take over as A leaves (L-1)
      ev.push({ bar: 0, deck: "in", stems: { drums: 1, bass: 1, vocals: 0, other: 0 }, ramp: 0 });
      ev.push({ bar: 0, deck: "out", stems: { drums: 0, bass: 0, vocals: 1, other: 1 }, ramp: 0 });
      ev.push({ bar: L - 1, deck: "out", stems: { drums: 0, bass: 0, vocals: 0, other: 0 }, ramp: 1 });
      ev.push({ bar: L - 1, deck: "in", stems: { vocals: 1, other: 1 }, ramp: 1 });
      ev.push({ bar: L, deck: "in", stems: null, ramp: 0.05 });
      return ev;
    }
    const intro = o.intro || (keyClash ? "drums" : "other");
    const lvl = o.introLevel || INTRO_LEVEL[intro] || 0.8;
    // 0..swap   B's ONE intro stem under A (no bass; keys clash: no tones yet).
    // It rises while the fader parks at the centre (1 bar), then sits there
    // alone for the intro phrase before the crossfade proper moves.
    ev.push({ bar: 0, deck: "in", stems: { drums: 0, bass: 0, vocals: 0, other: 0 }, ramp: 0 });
    ev.push({ bar: 0.01, deck: "in", stems: { [intro]: lvl }, ramp: FADER_PARK_BARS });
    // swap line  kick + bass change hands together, in one beat (one sub owner)
    ev.push({ bar: swap - 0.25, deck: "out", stems: { drums: 0, bass: 0 }, ramp: 0.25 });
    ev.push({ bar: swap, deck: "in", stems: { drums: 1, bass: 1 }, ramp: 0.05 });
    // swap..L   A's synths fade (keepA: held full until 2 bars before the end);
    // one singer: A finishes its line, then B's voice
    if (o.keepA) ev.push({ bar: L - 2, deck: "out", stems: { other: 0 }, ramp: 2 });
    else ev.push({ bar: swap, deck: "out", stems: { other: 0 }, ramp: keyClash ? (L - swap) / 2 : L - swap });
    if (keyClash) ev.push({ bar: swap + (L - swap) / 2, deck: "in", stems: { other: 1 }, ramp: (L - swap) / 2 });
    else if (intro !== "other") ev.push({ bar: swap, deck: "in", stems: { other: 1 }, ramp: (L - swap) / 2 });
    const aVoxOut = aSings ? L - 2 : swap;
    ev.push({ bar: aVoxOut, deck: "out", stems: { vocals: 0 }, ramp: aSings ? 2 : 1 });
    ev.push({ bar: aSings ? L - 0.5 : swap, deck: "in", stems: keyClash || intro !== "other" ? { vocals: 1 } : { vocals: 1, other: 1 }, ramp: aSings ? 0.5 : 2 });
    ev.push({ bar: L, deck: "in", stems: null, ramp: 0.05 });
    return ev.sort((a, b) => a.bar - b.bar);
  }

  // energy source -> (stem, t) => RMS. null: typical shares; {stem: number};
  // {stem: [per plan-unit bin]} (stemEnergyBars); or a function.
  function energyFn(e) {
    if (typeof e === "function") return e;
    if (!e) return (n) => TYPICAL_SHARE[n] || 0;
    return (n, t) => {
      const v = e[n];
      if (Array.isArray(v)) return v.length ? v[Math.max(0, Math.min(v.length - 1, Math.floor(t)))] || 0 : 0;
      return v || 0;
    };
  }
  const evAt = (e) => (e.bar != null ? e.bar : e.t);
  const FULL = () => ({ drums: 1, bass: 1, vocals: 1, other: 1, bus: 0 });
  // Stem gains of one deck at plan time t (deck-controller stemMix semantics: a
  // move re-ramps only the stems it changes, each from the previous move's
  // target; the others keep the ramp an earlier move gave them; null = full mix,
  // which re-ramps every stem).
  function gainsAt(events, deck, t) {
    let logical = FULL();
    const seg = {};                                   // stem -> the last ramp that moved it
    const evs = events.filter((e) => e.deck === deck && !e.hold && "stems" in e).sort((a, b) => evAt(a) - evAt(b));
    for (const e of evs) {
      const a = evAt(e);
      if (a > t) break;
      const tgt = e.stems === null ? FULL() : Object.assign({}, logical, e.stems);
      for (const n in tgt) {
        if (e.stems !== null && (tgt[n] || 0) === (logical[n] || 0)) continue;
        seg[n] = { from: logical[n] || 0, to: tgt[n] || 0, a, r: e.ramp || 0 };
      }
      logical = tgt;
    }
    const cur = {};
    for (const n in logical) {
      const s = seg[n];
      const k = !s ? 1 : s.r > 0 ? Math.min(1, (t - s.a) / s.r) : 1;
      cur[n] = s ? s.from + (s.to - s.from) * k : logical[n];
    }
    return cur;
  }
  // Crossfader position at plan time t in B-ward units (-1 = A only, +1 = B only).
  // segs: [{bar|t, from, to, bars|dur}] in raw fader units; dir = +1 when B is right.
  function faderAt(segs, dir, t) {
    if (!segs || !segs.length) return -1;
    const s0 = [...segs].sort((a, b) => evAt(a) - evAt(b));
    let v = s0[0].from;
    for (const s of s0) {
      const a = evAt(s), d = s.bars != null ? s.bars : s.dur || 0;
      if (t < a) break;
      v = d > 0 && t < a + d ? s.from + ((s.to - s.from) * (t - a)) / d : s.to;
    }
    return v * (dir || 1);
  }
  // p: {events, fader, dir, span, inStart, eOut, eIn, step, win, dipAllowed}
  // -> {ok, minDb, at, reason, dipAllowed}. Level = incoherent sum of each
  // stem's RMS x its gain x the deck's equal-power fader gain (+ the vocal bus,
  // which bypasses the fader); 0 dB = A's full mix at the plan's start.
  function levelCheck(p) {
    const eo = energyFn(p.eOut), ei = energyFn(p.eIn);
    const step = p.step || 1 / 16, win = Math.max(1, Math.round((p.win || 0.25) / step));
    const inStart = p.inStart || 0;
    let ref = 0;
    for (const n of STEMS) ref += eo(n, 0) ** 2;
    const pw = [], audible = [];
    for (let t = 0; t <= p.span + 1e-9; t += step) {
      const x = (faderAt(p.fader, p.dir, t) + 1) / 2;
      const fo = Math.cos((x * Math.PI) / 2), fi = Math.sin((x * Math.PI) / 2);
      const ga = gainsAt(p.events, "out", t), gb = gainsAt(p.events, "in", t);
      let sum = (ga.bus * eo("vocals", t)) ** 2, any = ga.bus >= AUDIBLE_GAIN;
      for (const n of STEMS) {
        sum += (fo * ga[n] * eo(n, t)) ** 2;
        if (fo * ga[n] >= AUDIBLE_GAIN && eo(n, t) > 0) any = true;
      }
      if (t >= inStart) {
        sum += (gb.bus * ei("vocals", t)) ** 2;
        for (const n of STEMS) {
          sum += (fi * gb[n] * ei(n, t)) ** 2;
          if (fi * gb[n] >= AUDIBLE_GAIN && ei(n, t) > 0) any = true;
        }
      }
      pw.push(sum);
      audible.push(any);
    }
    let minDb = 0, at = 0, silentAt = null;
    for (let i = 0; i < pw.length; i++) {
      const j0 = Math.max(0, i - win + 1);
      let m = 0;
      for (let j = j0; j <= i; j++) m += pw[j];
      const db = ref > 0 ? 10 * Math.log10(Math.max(1e-12, m / (i - j0 + 1)) / ref) : 0;
      if (db < minDb) { minDb = db; at = i * step; }
      // one sample of nothing is a hand-over edge; two in a row is a hole
      if (silentAt == null && i > 0 && !audible[i] && !audible[i - 1]) silentAt = i * step;
    }
    const dip = silentAt != null || minDb < -LEVEL_FLOOR_DB;
    const reason = silentAt != null ? `no stem audible at ${silentAt.toFixed(2)}`
      : dip ? `master ${minDb.toFixed(1)} dB at ${at.toFixed(2)} (floor -${LEVEL_FLOOR_DB} dB)` : "";
    if (dip && p.dipAllowed) return { ok: true, minDb, at, reason, dipAllowed: p.dipAllowed };
    return { ok: !dip, minDb, at, reason, dipAllowed: null };
  }

  // ---- master audibility (user: "the master just went silent") --------------
  // levelCheck measures full-band RMS, which a sub kick owns: Yotto -> Ben Bohmer
  // (B drums + A bass/vox/synth, 16 bars) passed it at -3.8 dB while 97 % of B's
  // drum stem sat under 120 Hz and A was in its own break, so for two bars the
  // master played a bare sub kick: silence on any speaker without a sub, and a
  // hole anywhere. This check simulates both decks' planned stem gains (gainsAt)
  // x the equal-power fader (faderAt) in the AUDIBLE band (stem RMS above
  // AUDIBLE_HZ: stemEnergyBars(.., "audible") / audibleRms). A moment is silent
  // when the master is SILENCE_DB under A's own level at the plan's start AND at
  // least QUIETER_DB under what A would play there untouched (A's own breaks are
  // not the plan's doing). A silent run of >= p.minRun (plan units; callers pass
  // 1 s) fails the plan. Unmeasured energy (null) cannot be judged: ok.
  // p: {events, fader, dir, span, eOut, eIn, inStart, step, minRun}
  // -> {ok, measured, at, run, minDb, reason}
  const AUDIBLE_HZ = 150, SILENCE_DB = 15, QUIETER_DB = 6, SILENT_RMS = 0.002;
  function masterAudibility(p) {
    if (!p.eOut || !p.eIn) return { ok: true, measured: false, at: null, run: 0, minDb: 0, reason: "" };
    const eo = energyFn(p.eOut), ei = energyFn(p.eIn);
    const step = p.step || 1 / 16, minRun = p.minRun || 0.5, inStart = p.inStart || 0;
    let ref = 0;
    for (const n of STEMS) ref += eo(n, 0) ** 2;
    ref = Math.sqrt(ref);
    const floor = Math.max(SILENT_RMS, ref * Math.pow(10, -SILENCE_DB / 20)), quieter = Math.pow(10, -QUIETER_DB / 20);
    let run = 0, worst = 0, worstAt = null, minDb = 0;
    for (let t = 0; t <= p.span + 1e-9; t += step) {
      const x = (faderAt(p.fader, p.dir, t) + 1) / 2;
      const fo = Math.cos((x * Math.PI) / 2), fi = Math.sin((x * Math.PI) / 2);
      const ga = gainsAt(p.events, "out", t), gb = gainsAt(p.events, "in", t);
      let sum = (ga.bus * eo("vocals", t)) ** 2, alone = 0;
      for (const n of STEMS) { sum += (fo * ga[n] * eo(n, t)) ** 2; alone += eo(n, t) ** 2; }
      if (t >= inStart) {
        sum += (gb.bus * ei("vocals", t)) ** 2;
        for (const n of STEMS) sum += (fi * gb[n] * ei(n, t)) ** 2;
      }
      const lvl = Math.sqrt(sum);
      if (ref > 0) minDb = Math.min(minDb, 20 * Math.log10(Math.max(1e-9, lvl) / ref));
      const silent = lvl < floor && lvl < Math.sqrt(alone) * quieter;
      run = silent ? run + step : 0;
      if (run > worst + 1e-9) { worst = run; worstAt = t - run + step; }
    }
    const ok = worst < minRun - 1e-9;
    return { ok, measured: true, at: worstAt, run: worst, minDb,
      reason: ok ? "" : `master near silent (audible band) for ${worst.toFixed(2)} from ${worstAt.toFixed(2)}` };
  }
  // RMS of samples [s0, s1) of one channel above AUDIBLE_HZ (4th-order Butterworth
  // high-pass: two cascaded biquads), read in `windows` contiguous slices of `len`
  // samples spread over the range, each filter warmed up on `warm` samples first:
  // cheap enough to run at booking time, and a sub kick measures as what a
  // speaker without a sub plays of it (almost nothing).
  function audibleRms(ch, s0, s1, sr, windows = 8, len = 1024, warm = 1024, hz = AUDIBLE_HZ) {
    s0 = Math.max(0, Math.floor(s0)); s1 = Math.min(ch.length, Math.floor(s1));
    if (!(s1 > s0) || !(sr > 0)) return 0;
    const w0 = (2 * Math.PI * hz) / sr, cw = Math.cos(w0), sw = Math.sin(w0);
    // two high-pass biquads, the Butterworth Q pair of a 4th-order filter
    const al1 = sw / (2 * 0.5412), n1 = 1 + al1, al2 = sw / (2 * 1.3066), n2 = 1 + al2;
    const b01 = (1 + cw) / 2 / n1, b11 = -(1 + cw) / n1, a11 = (-2 * cw) / n1, a21 = (1 - al1) / n1;
    const b02 = (1 + cw) / 2 / n2, b12 = -(1 + cw) / n2, a12 = (-2 * cw) / n2, a22 = (1 - al2) / n2;
    const span = s1 - s0, L = Math.min(len, span), nWin = span <= L ? 1 : windows;
    let sum = 0, count = 0;
    for (let w = 0; w < nWin; w++) {
      const start = s0 + (nWin === 1 ? 0 : Math.floor(((span - L) * w) / (nWin - 1)));
      let x1 = 0, x2 = 0, y1 = 0, y2 = 0, u1 = 0, u2 = 0, z1 = 0, z2 = 0;
      for (let i = Math.max(0, start - warm); i < start + L; i++) {
        const x = ch[i];
        const y = b01 * x + b11 * x1 + b01 * x2 - a11 * y1 - a21 * y2;
        x2 = x1; x1 = x; y2 = y1; y1 = y;
        const z = b02 * y + b12 * u1 + b02 * u2 - a12 * z1 - a22 * z2;
        u2 = u1; u1 = y; z2 = z1; z1 = z;
        if (i >= start) { sum += z * z; count++; }
      }
    }
    return count ? Math.sqrt(sum / count) : 0;
  }
  // A move due at `at` over `ramp` s, run at audio time `now`: [when, ramp] that
  // still ends at at + ramp when `at` has passed (5 ms minimum ramp).
  function onTime(at, ramp, now) {
    if (!(now > at)) return [at, ramp];
    return [now, Math.max(0.005, at + ramp - now)];
  }
  // The merge / mashup crossfader in B-ward units (-1 = A only, +1 = B only):
  // centre over the first 2 bars, B's side over the 8 bars from the line M.
  // The level checks and executeTransition both run THIS curve (rawFader turns
  // it into the deck-ordered fader value for either direction).
  function mergeFader(M) { return [{ bar: 0, from: -1, to: 0, bars: 2 }, { bar: M, from: 0, to: 1, bars: 8 }]; }
  // B-ward fader segments -> the real crossfader (-1 = deck a, +1 = deck b)
  // for a transition out of deck `outId`.
  function rawFader(segs, outId) {
    const dir = outId === "a" ? 1 : -1;
    return (segs || []).map((s) => ({ ...s, from: s.from * dir, to: s.to * dir }));
  }
  // deck-controller applyCrossfader's equal-power law: fader value -> {a, b} gains.
  function deckFaderGains(v) {
    const x = (v + 1) / 2;
    return { a: Math.cos((x * Math.PI) / 2), b: Math.cos(((1 - x) * Math.PI) / 2) };
  }
  // The merge to book: the picked combo if its plan keeps the master audible,
  // else the first of `ranked` (mergeRank order: every combo there already obeys
  // one sub owner / one singer / the key rules) whose plan does, else null
  // (refused: the caller's next transition path runs). check(plan) -> {ok, reason}.
  // -> {plan, pick, repaired, reason}
  function mergeBooking(M, pick, keyClash, ranked, check) {
    const key = (c) => STEMS.map((n) => c[n]).join("");
    const tried = new Set(), why = [];
    for (const cand of [pick, ...(ranked || [])]) {
      if (!cand || !cand.combo || tried.has(key(cand.combo))) continue;
      tried.add(key(cand.combo));
      const plan = mergeTransitionPlan(M, cand.combo, keyClash);
      const r = check(plan);
      if (r.ok) return { plan, pick: cand, repaired: cand !== pick, reason: why.join("; ") };
      why.push(`${mergeLabel(cand.combo)}: ${r.reason}`);
    }
    return { plan: null, pick: null, repaired: false, reason: why.join("; ") };
  }

  // Plan a stem blend that passes the floor: intro stem from B's measured
  // energy, then the fixes in order: B's intro louder, A's synths kept up longer
  // (never A's bass past the swap line: one sub owner), both. None passes ->
  // refused (the caller falls back to the EQ path: a full mix that stays audible).
  // c: {aSings, bSings, aSingsIntro, keyClash, introEnergy, eOut, eIn, fader, dir}
  function fitStemBlend(kind, bars, c) {
    const intro = kind === "double" ? null
      : pickIntro({ keyClash: c.keyClash, aSings: c.aSingsIntro != null ? c.aSingsIntro : c.aSings, bSings: c.bSings, energy: c.introEnergy });
    const tries = kind === "double" ? [{}] : [{}, { introLevel: 1 }, { keepA: true }, { keepA: true, introLevel: 1 }];
    let last = null;
    for (const fix of tries) {
      const events = stemBlendPlan(kind, bars, c.aSings, c.bSings, c.keyClash, Object.assign({ intro }, fix));
      const check = levelCheck({ events, fader: c.fader, dir: c.dir, span: bars, eOut: c.eOut, eIn: c.eIn });
      last = { events, intro, fix, check, refused: !check.ok };
      if (check.ok) return last;
    }
    return last;
  }
  // BREAKDOWN plan as level-check events (bars; its ramps are in beats).
  function breakdownEvents(bars) {
    return (BREAKDOWN[bars] || []).map(([bar, stems, beats]) => ({ bar, deck: "out", stems, ramp: beats / 4 }));
  }
  const STEM_BLEND_KINDS = new Set(["bass", "blend", "filter", "loop", "double"]);

  // Mashup transition (user: "create a mashup then transition; leavemealone and
  // Victory Lap is a perfect example"). A = instrumental, B = vocal only (B on
  // key-locked tempo stems at A's tempo), M bars; then B's beat takes over on the
  // line and an 8-bar crossfade carries the rest. Bars at the common tempo.
  //   0        A's voice out, B's voice in (vox gain v)
  //   12..16   HOLD VOX on B (and again 28..32 in a 32-bar mashup)
  //   M/2      B's voice forward (x1.3)
  //   M-2      A's beat out: B's voice over A's synths alone
  //   M        B's drums + bass in; A's synths fade over 8, B's synths rise
  //   M+8      B full
  function mashupTransitionPlan(M, v) {
    const ev = [
      // A's synths fill the space its vocal leaves (+2 dB), so the mashup doesn't sag
      { bar: 0, deck: "out", stems: { vocals: 0, other: 1.25 }, ramp: 0.25 },
      { bar: 0, deck: "in", start: true, stems: { drums: 0, bass: 0, vocals: v, other: 0 }, ramp: 0 },
      { bar: M / 2, deck: "in", stems: { vocals: Math.min(1, v * 1.3) }, ramp: 2 },
      { bar: M - 2, deck: "out", stems: { drums: 0, bass: 0 }, ramp: 0.25 },
      { bar: M, deck: "in", stems: { drums: 1, bass: 1 }, ramp: 0.05 },
      { bar: M, deck: "out", stems: { other: 0 }, ramp: 8 },
      { bar: M, deck: "in", stems: { other: 1, vocals: 1 }, ramp: 8 },
      { bar: M + 8, deck: "in", stems: null, ramp: 0.05 },
    ];
    for (let s = 0; s + 16 <= M; s += 16) {
      if (s + 16 === M) continue;                        // the last section ends in the A-drop, no hold
      ev.push({ bar: s + 12, deck: "in", hold: { stem: "vocals", fromBar: s + 11, bars: 1, untilBar: s + 16 } });
    }
    return { events: ev.sort((a, b) => a.bar - b.bar), total: M + 8 };
  }

  // Stem bridge: across ANY tempo gap, no echo-out (user: "Echo Out is painful").
  // No two beats ever overlap, so the tempos never meet:
  //   A bar 0   A's drums out            (strip)
  //   A bar 2   A's bass out: voice + synths, beatless
  //   A bar 4   HOLD VOX: A's last vocal bar held; B starts on ONE intro stem (its
  //             pads when the keys agree; its drums when they clash: A is beatless
  //             by then, so still one beat), the fader parks at the centre
  //   B entry   B's bass back 2 B-bars before its line, B's drums ON its line: the
  //             rebuild lands on B's own grid; A has faded out just before, the
  //             fader finishes its move over the 2 B-bars into the line
  // Times in seconds from A's bar 0. barA / barB = seconds per bar of each song.
  // o.intro: "other" | "drums" (pickIntro; default by keyClash), o.introLevel.
  // fader: [{t, from, to, dur}] in B-ward units (-1 = A side; x the side of B).
  function stemBridgePlan(barA, barB, keyClash, aSings, o = {}) {
    const bStart = 4 * barA, bEntry = bStart + 4 * barB;
    const LIFT = 1.25;                 // ~+2 dB on A's voice + synths once its beat is gone (no sag)
    const swapAt = bEntry - 3 * barB;  // tonal layers change hands here, equal power over 2 B-bars
    const intro = o.intro === "drums" || (keyClash && o.intro !== "other") ? "drums" : "other";
    const lvl = o.introLevel || (intro === "drums" ? 0.5 : 0.7);
    const ev = [
      { t: 0, deck: "out", stems: { drums: 0, vocals: LIFT, other: LIFT }, ramp: barA },
      { t: 2 * barA, deck: "out", stems: { bass: 0 }, ramp: barA },
      { t: bStart, deck: "in", start: true, stems: { drums: 0, bass: 0, vocals: 0, other: 0 }, ramp: 0 },
      // B's intro stem rises under A from the start of the beatless stretch
      { t: bStart + 0.01, deck: "in", stems: intro === "drums" ? { drums: lvl, vocals: 0 } : { other: lvl, vocals: 0 }, ramp: 2 * barB },
      // the swap: A's tones out while B's bass + tones come in (one crossfade, no gap)
      { t: Math.max(bStart, swapAt), deck: "out", stems: { vocals: 0, other: 0 }, ramp: 2 * barB },
      { t: Math.max(bStart, swapAt), deck: "in", stems: { bass: 1, other: 1 }, ramp: 2 * barB },
      { t: bEntry, deck: "in", stems: null, ramp: 0.03 },   // B's beat, on its own line
    ];
    if (aSings) ev.push({ t: bStart, deck: "out", hold: { stem: "vocals", fromBar: 3, bars: 1 }, until: Math.max(bStart, swapAt) + 2 * barB });
    const fader = [
      { t: bStart, from: -1, to: 0, dur: barB },                // park: B only has its intro stem
      { t: bEntry - 2 * barB, from: 0, to: 1, dur: 2 * barB },  // the crossfade proper, into B's line
    ];
    return { events: ev.sort((x, y) => x.t - y.t), bStart, bEntry, total: bEntry + barB, intro, fader };
  }

  // ---- remix on the go: stem on/offs and holds inside 16 / 32-bar sections ----
  // Each move lives in the last quarter of its section and resolves on the next
  // line with everything back (the drop). Bars are from the section start.
  //   vocal_hold  the vocal's last bar held (looped) over the section's end: "hold on"
  //   acapella    everything but the vocal out, back on the line
  //   drum_break  drums alone, back on the line
  //   bass_out    bass out for the second half, back on the line
  //   synth_hold  the synth/riff bar before the break held under a drumless end
  function remixEvents(kind, len = 16) {
    const q = len * 0.75, e = len;
    switch (kind) {
      case "vocal_hold": return [{ bar: q, hold: { stem: "vocals", fromBar: q - 1, bars: 1, untilBar: e } }];
      case "acapella": return [{ bar: q, stems: { drums: 0, bass: 0, other: 0 }, ramp: 0.25 }, { bar: e, stems: null, ramp: 0.02 }];
      case "drum_break": return [{ bar: q, stems: { bass: 0, vocals: 0, other: 0 }, ramp: 0.25 }, { bar: e, stems: null, ramp: 0.02 }];
      case "bass_out": return [{ bar: len / 2, stems: { bass: 0 }, ramp: 0.5 }, { bar: e, stems: null, ramp: 0.02 }];
      case "synth_hold": return [{ bar: q, stems: { drums: 0, bass: 0 }, ramp: 0.25, hold: { stem: "other", fromBar: q - 1, bars: 1, untilBar: e } },
        { bar: e, stems: null, ramp: 0.02 }];
      default: return [];
    }
  }
  // Which move fits this section. ctx: {vocal (share 0..1), used [kinds this song], count, barsOnTrack, barsLeft, lastAtBar, atBar}
  const REMIX_MAX_PER_SONG = 3, REMIX_GAP_BARS = 32;
  // Vibe floor (user: drums alone on Get Lucky "killed the vibe"): an AI remix move
  // never leaves the MASTER without a tonal layer (bass, voice or synths). A drum
  // break is fine while the other deck is also playing into the master (it carries
  // the tones: ctx.othersCarry), never when this song is all the room hears.
  // -> true when the move keeps a tonal stem of its own.
  function keepsVibe(kind, len = 16) {
    return remixEvents(kind, len).every((e) => !e.stems || ["bass", "vocals", "other"].some((n) => e.stems[n] !== 0));
  }
  function remixPick(ctx) {
    if ((ctx.count || 0) >= REMIX_MAX_PER_SONG) return null;
    if ((ctx.barsOnTrack || 0) < 32 || (ctx.barsLeft || 0) < 48) return null;
    if (ctx.lastAtBar != null && ctx.atBar - ctx.lastAtBar < REMIX_GAP_BARS) return null;
    const used = new Set(ctx.used || []);
    const menu = ctx.vocal >= 0.5 ? ["vocal_hold", "acapella", "bass_out"]
      : ctx.vocal >= 0.15 ? ["bass_out", "drum_break", "synth_hold"] : ["drum_break", "synth_hold", "bass_out"];
    return menu.find((k) => !used.has(k) && (ctx.othersCarry || keepsVibe(k))) || null;
  }
  // Hook drop (app/music_brain/hook_drop.py plan items: {cut_at, drop_at, text}):
  // drums + bass leave over a quarter bar ending on cut_at, synths duck to
  // HOOK_OTHER, the voice carries the line alone, everything slams back on
  // drop_at (5 ms: no click). Same shape as hook_drop.render(), the audition file.
  const HOOK_OTHER = 0.35;
  function hookDropEvents(item, bar) {
    const fade = bar / 4;
    return [
      { t: item.cut_at - fade, stems: { drums: 0, bass: 0, other: HOOK_OTHER }, ramp: fade },
      { t: item.drop_at, stems: { drums: 1, bass: 1, other: 1 }, ramp: 0.005 },
    ];
  }
  // The plan item to play now, or null: the next item whose cut is 1-2 bars
  // ahead (room to book it on the audio clock), whose drop lands at least 4
  // bars before the planned exit, and whose hold is 1-8 bars.
  function hookDropDue(items, pos, bar, exitAt) {
    for (const it of items || []) {
      const lead = it.cut_at - pos, hold = it.drop_at - it.cut_at;
      if (lead < bar || lead > 2 * bar) continue;
      if (!(hold >= bar * 0.75 && hold <= 8 * bar)) continue;
      if (exitAt != null && it.drop_at > exitAt - 4 * bar) continue;
      return it;
    }
    return null;
  }
  // A deck on the master with nobody else carrying the sound must keep at least one
  // stem audible (user rule): a move that would take every stem (and the vocal
  // bus) below AUDIBLE_GAIN keeps the stem that was loudest before it at the
  // floor instead. -> {next, kept} (kept = the stem held up, or null).
  const KEEP_ORDER = ["vocals", "other", "drums", "bass"];     // ties: the voice first, never the bass
  function keepOneStem(next, before, othersCarry) {
    const up = (s) => STEMS.some((n) => (s[n] || 0) >= AUDIBLE_GAIN) || (s.bus || 0) >= AUDIBLE_GAIN;
    if (othersCarry || up(next)) return { next, kept: null };
    const b = before || { drums: 1, bass: 1, vocals: 1, other: 1 };
    const kept = KEEP_ORDER.reduce((best, n) => ((b[n] || 0) > (b[best] || 0) ? n : best), KEEP_ORDER[0]);
    return { next: { ...next, [kept]: Math.max(AUDIBLE_GAIN, Math.min(1, b[kept] || 1)) }, kept };
  }
  // ---- SONG MERGE (user: "A drums, A bass, B vox, B synth; different combinations
  // where possible"). For M bars each role plays from ONE deck (one sub owner,
  // one singer), then B takes everything on the line. Same rules as
  // app/music_brain/merge.py rank() (golden vectors: app/tests/fixtures/rule_vectors.json);
  // the silent ear (/api/merge/audition) re-ranks.
  const TONAL = ["bass", "vocals", "other"];
  const MERGE_KEY_OK = 0.8;
  function mergeCombos() {
    const out = [];
    for (let m = 1; m < 15; m++) {
      const c = {};
      STEMS.forEach((n, i) => { c[n] = (m >> (3 - i)) & 1 ? "b" : "a"; });
      out.push(c);
    }
    return out;
  }
  const mergeLabel = (c) => STEMS.map((n) => `${c[n].toUpperCase()} ${n === "other" ? "synth" : n === "vocals" ? "vox" : n}`).join(" + ");
  // c: {eA, eB: mean RMS per stem over each deck's merge window (null: unmeasured,
  // typical shares), keyScore (null: unknown), aRap, bRap}. -> [{combo, label, score, reasons}]
  function mergeRank(c) {
    const eA = c.eA || TYPICAL_SHARE, eB = c.eB || TYPICAL_SHARE, out = [];
    for (const m of mergeCombos()) {
      const e = (n) => (m[n] === "a" ? eA : eB)[n] || 0;
      if (STEMS.some((n) => e(n) < INTRO_MIN_RMS)) continue;                 // every chosen stem really plays
      const tonal = new Set(TONAL.filter((n) => !(n === "vocals" && ((m[n] === "b" && c.bRap) || (m[n] === "a" && c.aRap)))).map((n) => m[n]));
      if (tonal.size === 2 && c.keyScore != null && c.keyScore < MERGE_KEY_OK) continue;
      let score = 50;
      const why = [];
      if (m.drums === m.bass) { score += 20; why.push("kick + bass from one record"); }
      if (m.vocals === "b" && m.drums === "a") { score += 10; why.push("B's song over A's beat"); }
      score += 10;                                                            // a voice carries it (stems play: checked above)
      if (tonal.size === 2 && c.keyScore != null) score += 10 * c.keyScore;
      let ea = 0, eb = 0;
      for (const n of STEMS) { if (m[n] === "a") ea += eA[n] * eA[n]; else eb += eB[n] * eB[n]; }
      if (ea + eb > 0) score += 10 * (1 - Math.abs(ea - eb) / (ea + eb));
      out.push({ combo: m, label: mergeLabel(m), score: Math.round(score * 10) / 10, reasons: why });
    }
    return out.sort((x, y) => y.score - x.score);
  }
  // Plan events (bars from B's entry): 0..M the combo; M-0.25 A's kick+bass leave
  // (if they are A's) and B's land ON M; A's tones/voice fade over 8; M+8 B full.
  // keyClash: the two records' keys fight (camelot < 0.8): no tonal overlap even in
  // the handover; A's synths / voice leave a quarter bar before the line, B's land on it.
  function mergeTransitionPlan(M, combo, keyClash = false) {
    const on = (who) => Object.fromEntries(STEMS.map((n) => [n, combo[n] === who ? 1 : 0]));
    // Every stem A hands to B leaves a quarter bar before the line and B's lands ON
    // it (the bass-swap convention): one sub owner, one singer, never both.
    const give = Object.fromEntries(STEMS.filter((n) => combo[n] === "b").map((n) => [n, 0]));
    const ev = [
      { bar: -0.25, deck: "out", stems: give, ramp: 0.25 },
      { bar: 0, deck: "in", start: true, stems: on("b"), ramp: 0 },
    ];
    const aBeat = ["drums", "bass"].filter((n) => combo[n] === "a");
    if (aBeat.length) {
      ev.push({ bar: M - 0.25, deck: "out", stems: Object.fromEntries(aBeat.map((n) => [n, 0])), ramp: 0.25 });
      ev.push({ bar: M, deck: "in", stems: Object.fromEntries(aBeat.map((n) => [n, 1])), ramp: 0.05 });
    }
    // A's synths hand over across 8 bars; voices never overlap: A's voice leaves
    // over 2 bars, B's comes in after it (one singer)
    if (keyClash) {
      const tone = ["other", "vocals"].filter((n) => combo[n] === "a");
      if (tone.length) {
        ev.push({ bar: M - 0.25, deck: "out", stems: Object.fromEntries(tone.map((n) => [n, 0])), ramp: 0.25 });
        ev.push({ bar: M, deck: "in", stems: Object.fromEntries(tone.map((n) => [n, 1])), ramp: 0.05 });
      }
      ev.push({ bar: M, deck: "out", stems: { vocals: 0, other: 0 }, ramp: 0.05 });
      ev.push({ bar: M + 8, deck: "in", stems: null, ramp: 0.05 });
      return { events: ev.sort((a, b) => a.bar - b.bar), total: M + 8 };
    }
    ev.push({ bar: M, deck: "out", stems: { other: 0 }, ramp: 8 });
    if (combo.other === "a") ev.push({ bar: M, deck: "in", stems: { other: 1 }, ramp: 8 });
    if (combo.vocals === "a") {
      ev.push({ bar: M, deck: "out", stems: { vocals: 0 }, ramp: 2 });
      ev.push({ bar: M + 2, deck: "in", stems: { vocals: 1 }, ramp: 2 });
    } else ev.push({ bar: M, deck: "out", stems: { vocals: 0 }, ramp: 0.05 });
    ev.push({ bar: M + 8, deck: "in", stems: null, ramp: 0.05 });
    return { events: ev.sort((a, b) => a.bar - b.bar), total: M + 8 };
  }
  // Blend the algorithm's rank with the ear's scores (1-10): ear-heard combos
  // move by up to +-15; unheard keep their score. -> re-sorted copy
  function mergeWithEar(ranked, ear) {
    const key = (c) => STEMS.map((n) => c[n]).join("");
    const heard = new Map((ear || []).filter((r) => r && r.ear).map((r) => [key(r.combo), r.ear]));
    return ranked.map((r) => {
      const e = heard.get(key(r.combo));
      return e ? { ...r, score: r.score + (e.score - 5.5) * 3, ear: e } : r;
    }).sort((x, y) => y.score - x.score);
  }
  const core = { keepsVibe, mergeCombos, mergeRank, mergeLabel, mergeTransitionPlan, mergeWithEar, keepOneStem, hookDropEvents, hookDropDue, HOOK_OTHER, BREAKDOWN, breakdownFits, handoffFits, vocalShare, stemBlendPlan, STEM_BLEND_KINDS, remixEvents, remixPick, stemBridgePlan, mashupTransitionPlan,
                 pickIntro, introBars, INTRO_LEVEL, levelCheck, gainsAt, faderAt, fitStemBlend, breakdownEvents,
                 masterAudibility, audibleRms, mergeFader, rawFader, deckFaderGains, mergeBooking, onTime, AUDIBLE_HZ, SILENCE_DB,
                 LEVEL_FLOOR_DB, AUDIBLE_GAIN, FADER_PARK_BARS, TYPICAL_SHARE, DIP_ALLOWED };
   if (typeof module !== "undefined" && module.exports) module.exports = core;
   if (typeof root.document === "undefined" || typeof audioCtx === "undefined") return;

   // Minimum ramp seconds for stem moves: route through tempoRule if available,
   // else fallback. kind = "stem" or "level"; barS = seconds/bar; drop = exempt
   // (drop landing on its downbeat); deck = audible deck check.
   function minStemRamp(kind, barS, drop, deck) {
     if (typeof root.tempoRule !== "undefined" && root.tempoRule.minRampSeconds) {
       return root.tempoRule.minRampSeconds(kind, barS, { drop, deck });
     }
     // Fallback: drop or silent deck -> instant (0.005), else minimum ramp
     if (drop || !deck || !deck.playing) return 0.005;
     return kind === "stem" ? barS * 0.25 : barS / 4;
   }

   // ------------------------------------------------------------ browser --
  const timers = { a: [], b: [] };
  function note(deckId, label, why) {
    root.dispatchEvent(new CustomEvent("ai-activity", { detail: { kind: "stem-move", deck: deckId, label, why } }));
  }
  // Audio time at which deck `d` reaches track time `t` (no loop wrap).
  function audioAt(d, t) {
    const rate = (d._playbackRate && d._playbackRate()) || 1;
    return audioCtx.currentTime + Math.max(0, (t - d._currentPosition()) / rate);
  }
  function cancel(deckId) {
    (timers[deckId] || []).forEach(clearTimeout);
    timers[deckId] = [];
  }
  // Schedule gain moves; setTimeout only books them ~200 ms early, the ramps
  // themselves land on the audio clock. A move whose time has passed (booked
  // late: the merge's A-hands-over move sits a quarter bar before B's line, the
  // transition is booked 150 ms before it) still ENDS when planned (onTime).
  function book(d, at, target, ramp) {
    const lead = Math.max(0, (at - audioCtx.currentTime) * 1000 - 200);
    timers[d.id].push(setTimeout(() => {
      if (d.playing) { const [when, r] = onTime(at, ramp, audioCtx.currentTime); d.stemMix(target, when, r); }
    }, lead));
  }

  // Per-stem RMS of deck d's decoded stems, one bin per `barSong` song seconds
  // from song time songT (n bins). Measured, not guessed: reads the stem
  // buffers (strided), mapped like _startStem ((t + lag) * ratio). null when
  // the deck has no decoded stems (the level check then uses TYPICAL_SHARE).
  // band "audible": the RMS above AUDIBLE_HZ (core audibleRms), for masterAudibility.
  function stemEnergyBars(d, songT, barSong, n, band) {
    const st = d && d.stems;
    if (!st || !(barSong > 0)) return null;
    const k = st.ratio || 1, lag = st.lag || 0, out = {};
    for (const name of STEMS) {
      const b = st[name];
      if (!b || !b.getChannelData) return null;
      const ch = b.getChannelData(0), sr = b.sampleRate, arr = [];
      for (let i = 0; i < n; i++) {
        const s0 = Math.max(0, Math.floor((songT + i * barSong + lag) * k * sr));
        const s1 = Math.min(ch.length, Math.floor((songT + (i + 1) * barSong + lag) * k * sr));
        if (band === "audible") { arr.push(audibleRms(ch, s0, s1, sr)); continue; }
        let sum = 0, c = 0;
        for (let j = s0; j < s1; j += 64) { sum += ch[j] * ch[j]; c++; }
        arr.push(c ? Math.sqrt(sum / c) : 0);
      }
      out[name] = arr;
    }
    return out;
  }
  const meanOver = (e, a, b) => {
    if (!e) return null;
    const m = {};
    for (const n of STEMS) { const v = e[n].slice(a, Math.max(a + 1, b)); m[n] = v.reduce((x, y) => x + y, 0) / (v.length || 1); }
    return m;
  };
  // camelot score of the two decks' keys (null: unknown)
  function keyScoreOf(out, inn) {
    const ka = out.analysis && out.analysis.key && out.analysis.key.camelot, kb = inn.analysis && inn.analysis.key && inn.analysis.key.camelot;
    const cs = root.djMind && root.djMind.core && root.djMind.core.camelotScore;
    return cs && ka && kb ? cs(ka, kb) : null;
  }
  function camelotClash(out, inn) {
    const s = keyScoreOf(out, inn);
    return s != null && s < 0.8;
  }

  // A deliberate one-deck strip: run the floor check with its dipAllowed tag
  // and log it (the check is not skipped: its result says what dipped).
  function dipReport(d, label, events, bars, songT, barSong, dipAllowed) {
    const ev = events.filter((e) => e.stems !== undefined).map((e) => ({ bar: e.bar, deck: "out", stems: e.stems, ramp: e.ramp || 0 }));
    const lv = levelCheck({ events: ev, span: bars, eOut: stemEnergyBars(d, songT, barSong, bars + 1), dipAllowed });
    if (lv.dipAllowed) console.info(`${label} ${d.id}: dip allowed (${lv.dipAllowed}): ${lv.reason}`);
    return lv;
  }

  function breakdown(d, startTrackT, bars, why) {
    const plan = BREAKDOWN[bars];
    if (!plan || !d.stemsReady) return false;
    cancel(d.id);
    const bar = 240 / (d.bpm || 128), rate = (d._playbackRate && d._playbackRate()) || 1;
    // the strip IS the move: its dip is allowed, and says so
    dipReport(d, "breakdown", breakdownEvents(bars), bars, startTrackT, bar, DIP_ALLOWED.breakdown);
    const t0 = audioAt(d, startTrackT), beat = bar / 4 / rate;
    for (const [b, target, rampBeats] of plan) book(d, t0 + (b * bar) / rate, target, rampBeats * beat);
    root.dispatchEvent(new CustomEvent("ai-cue", { detail: { at: t0 + (plan[plan.length - 1][0] * bar) / rate, kind: "drop",
      deck: d.id, bar: bar / rate, why: "everything slams back after the strip & rebuild" } }));
    note(d.id, `STRIP & REBUILD · ${bars} bars`, why || "drums out, bass out, voice alone, rebuild, drop on the line");
    return true;
  }

  // out/inn: deck ids. t0: audio time the blend starts; totalS: its length (s).
  // EQ transition with stems on only one side (or a stem blend the floor
  // refused): still never B's full mix through the fader, still one singer.
  //   B has stems  B enters on ONE intro stem (no bass: A's sub, EQ-killed on B
  //                anyway), beat + tones on the swap line, its voice when A's
  //                is done (end of the blend if A sings, else the swap line)
  //   A only       A's voice leaves over a beat on bar 0 when both would sing
  // The EQ path's fader keeps A full-mix, so the master never dips here.
  function eqIntro(outId, innId, t0, totalS, swapS, why) {
    const out = root.decks[outId], inn = root.decks[innId];
    if (!out || !inn || !(totalS >= 2)) return false;
    const rA = (out._playbackRate && out._playbackRate()) || 1;
    const bar = 240 / (out.bpm || 128) / rA;
    const pA = out._positionAt ? out._positionAt(t0) : out._currentPosition();
    const aSings = vocalShare(out.analysis && out.analysis.vocal_active_regions, pA, pA + totalS * rA) >= 0.3;
    const pB = inn.cuePoint || 0;
    const bSings = vocalShare(inn.analysis && inn.analysis.vocal_active_regions, pB, pB + totalS) >= 0.3;
    if (inn.stemsReady) {
      cancel(innId);
      const barB = 240 / (inn.bpm || 128);
      let intro = pickIntro({ keyClash: camelotClash(out, inn), aSings, bSings, energy: meanOver(stemEnergyBars(inn, pB, barB, 4), 0, 4) });
      if (intro === "vocals" && aSings) intro = "other";
      const beatAt = swapS >= 2 * bar ? swapS : Math.max(bar, totalS / 2);
      inn.stemMix({ drums: 0, bass: 0, vocals: 0, other: 0 }, t0, minStemRamp("stem", bar, false, inn));
      book(inn, t0 + 0.01, { [intro]: INTRO_LEVEL[intro] || 0.8 }, bar);
      book(inn, t0 + beatAt, { drums: 1, bass: 1, other: 1 }, 0.05);
      if (intro !== "vocals") book(inn, aSings ? t0 + totalS - bar / 2 : t0 + beatAt, { vocals: 1 }, bar / 2);
      book(inn, t0 + totalS + bar, null, 0.05);
      note(innId, `STEM INTRO ${innId.toUpperCase()}`, why ||
        `B in on its ${intro === "other" ? "synths" : intro} under A, beat on the swap line${aSings ? ", its voice after A's" : ""}`);
      return "in";
    }
    if (!out.stemsReady && out.rearmStems) out.rearmStems("one singer");
    if (out.stemsReady && aSings && bSings) {
      cancel(outId);
      out.stemMix({ vocals: 0 }, t0, bar / 4);
      note(outId, `VOCAL OUT ${outId.toUpperCase()}`, why || "one singer: A's voice leaves as B's arrives");
      return "out";
    }
    return false;
  }

  // swapS: seconds from t0 to the EQ recipe's bass-swap line (0 = unknown).
  // With it, B enters on ONE intro stem (never its bass: A owns the sub) and
  // its beat + tones join on the swap line, instead of its whole instrumental
  // appearing through the crossfader.
  function handoff(outId, innId, t0, totalS, why, swapS = 0) {
    const out = root.decks[outId], inn = root.decks[innId];
    if (!out || !inn) return false;
    if (!out.stemsReady && out.rearmStems) out.rearmStems("vocal handoff");
    if (!out.stemsReady || !inn.stemsReady) return false;
    cancel(outId); cancel(innId);
    const bar = 240 / (out.bpm || 128) / ((out._playbackRate && out._playbackRate()) || 1);
    // B enters as its instrumental; A's vocal leaves A's strip for the bus.
    let intro = null;
    if (swapS >= 2 * bar) {
      const pB = inn.cuePoint || 0, barB = 240 / (inn.bpm || 128);
      intro = pickIntro({ keyClash: camelotClash(out, inn), aSings: true, bSings: false,
        energy: meanOver(stemEnergyBars(inn, pB, barB, 4), 0, 4) });
      if (intro === "vocals") intro = "other";
      inn.stemMix({ drums: 0, bass: 0, vocals: 0, other: 0 }, t0, minStemRamp("stem", bar, false, inn));
      book(inn, t0 + 0.01, { [intro]: INTRO_LEVEL[intro] }, bar);
      book(inn, t0 + swapS, { drums: 1, bass: 1, other: 1 }, 0.05);
    } else {
       inn.stemMix({ vocals: 0 }, t0, minStemRamp("stem", bar, false, inn));
    }
    out.stemMix({ vocals: 0, bus: 1 }, t0, bar / 4);
    // Last bar: A's voice fades, B's own vocal comes back in.
    book(out, t0 + totalS - bar, { bus: 0 }, bar);
    book(inn, t0 + totalS - bar / 2, { vocals: 1 }, bar / 2);
    book(inn, t0 + totalS + bar, null, 0.05);
    note(outId, `VOCAL HANDOFF ${outId.toUpperCase()} → ${innId.toUpperCase()}`, (why || "one singer: A's vocal rides B's beat, B's vocal enters as A's fades") +
      (intro ? `; B in on its ${intro === "other" ? "synths" : intro}, its beat on the swap line` : ""));
    return true;
  }

  function instrumental(d, on, atTrackT, why) {
    if (!d || !d.stemsReady) return false;
    const at = atTrackT == null ? 0 : audioAt(d, atTrackT);
    const ok = d.stemMix(on ? { vocals: 0 } : null, at, 0.25);
    if (ok) note(d.id, on ? "INSTRUMENTAL" : "VOCAL BACK", why || (on ? "loop runs as the instrumental: no chopped singer" : "full mix"));
    return ok;
  }

  // Deck stopped / reloaded: never leave its mix muted.
  function reset(d) { if (d) { cancel(d.id); const barS = 240 / ((d && d.bpm) || 128); d.stemMix(null, 0, minStemRamp("stem", barS, false, d)); } }

  // Run a stem blend from audio time t0 (B's first downbeat). barS = seconds per
  // bar. opts.fader / opts.dir: the crossfader moves the caller will run
  // (autopilotCore.stemBlendFader), so the loudness floor is checked against
  // what the crowd will actually hear. Returns the fitted plan
  // ({intro, fix, check}) or false: not both decks on live stems, or no plan
  // keeps the master above the floor (then the caller's EQ path runs instead).
  function stemBlend(kind, outId, innId, t0, bars, barS, why, opts = {}) {
    const out = root.decks[outId], inn = root.decks[innId];
    if (!out || !inn || !STEM_BLEND_KINDS.has(kind)) return false;
    if (!out.stemsReady && out.rearmStems) out.rearmStems("stem blend");
    if (!inn.stems || !out.stemsReady) return false;
    const pA = out._positionAt ? out._positionAt(t0) : out._currentPosition();
    const aSings = vocalShare(out.analysis && out.analysis.vocal_active_regions, pA, pA + bars * barS) >= 0.3;
    const pB = (inn.cuePoint || 0);
    const bSings = vocalShare(inn.analysis && inn.analysis.vocal_active_regions, pB, pB + bars * barS) >= 0.3;
    const keyClash = camelotClash(out, inn);
    const barSongA = 240 / (out.bpm || 128), barSongB = 240 / (inn.bpm || 128);
    let eOut = stemEnergyBars(out, pA, barSongA, bars + 1), eIn = stemEnergyBars(inn, pB, barSongB, bars + 1);
    if (!eOut || !eIn) eOut = eIn = null;                 // one scale for both decks, or typical shares
    const P = introBars(bars);
    const fit = fitStemBlend(kind, bars, {
      aSings, bSings, keyClash, eOut, eIn, fader: opts.fader, dir: opts.dir || 1,
      aSingsIntro: vocalShare(out.analysis && out.analysis.vocal_active_regions, pA, pA + P * barSongA) >= 0.3,
      introEnergy: meanOver(eIn, 0, P),
    });
    if (fit.refused) {
      console.info(`stem blend ${outId}->${innId} refused: ${fit.check.reason}; EQ path instead`);
      return false;
    }
    cancel(outId); cancel(innId);
    for (const e of fit.events) {
      const d = e.deck === "out" ? out : inn, at = t0 + e.bar * barS;
      if (e.bar === 0 && e.deck === "in") {
        // B must be silent-in-stems from its very first sample
        setTimeout(() => d.stemMix(e.stems, at - 0.005, 0.005), Math.max(0, (at - audioCtx.currentTime) * 1000 - 400));
      } else book(d, at, e.stems, Math.max(minStemRamp("stem", barS, false, d), e.ramp * barS));
    }
    const fixTxt = fit.fix.keepA ? ", A's synths held longer" : fit.fix.introLevel ? ", intro louder" : "";
    const introTxt = fit.intro ? `B in on its ${fit.intro === "other" ? "synths" : fit.intro} (${fit.intro === "drums" && keyClash ? "keys clash" : eIn ? "measured" : "typical"})` : "both drops together";
    console.info(`stem blend ${outId}->${innId}: ${introTxt}${fixTxt}; master floor ${fit.check.minDb.toFixed(1)} dB (${eOut ? "measured stem RMS" : "typical stem shares"})`);
    note(outId, `STEM ${kind === "double" ? "DOUBLE DROP" : "BLEND"} ${outId.toUpperCase()} → ${innId.toUpperCase()}`,
      why || `${bars} bars: ${introTxt}${fixTxt}, kick + bass swap on bar ${kind === "double" ? 0 : bars / 2}, one singer${aSings ? " (A finishes its line)" : ""}`);
    return fit;
  }

  // Run one remix move on deck d for the section starting at track time `lineT`.
  const REMIX_LABEL = { vocal_hold: "HOLD ON (vocal)", acapella: "ACAPELLA", drum_break: "DRUM BREAK",
                        bass_out: "BASS OUT", synth_hold: "SYNTH HOLD" };
  function remix(d, lineT, kind, len = 16, why) {
    if (!d || !d.stemsReady) return false;
    const rate = (d._playbackRate && d._playbackRate()) || 1, barS = 240 / (d.bpm || 128);
    const at = (b) => audioAt(d, lineT + b * barS);
    // stem mode from the section start (inaudible switch), full mix again after
    book(d, at(0), {}, 0.01);
    const tonal = ["bass", "vocals", "other"];
    for (const e of remixEvents(kind, len)) {
      const strips = e.stems && tonal.every((n) => e.stems[n] === 0);
      if (strips) {
        // vibe floor at the moment it plays: only while another deck carries the tones
        const T = at(e.bar), ramp = Math.max(minStemRamp("stem", barS / rate, false, d), (e.ramp * barS) / rate);
        timers[d.id].push(setTimeout(() => {
          if (d.playing && d._othersCarry && d._othersCarry()) d.stemMix(e.stems, T, ramp);
          else console.info(`remix ${d.id}: ${kind} skipped, nothing else is playing into the master`);
        }, Math.max(0, (T - audioCtx.currentTime) * 1000 - 200)));
      } else if (e.stems !== undefined) book(d, at(e.bar), e.stems, Math.max(minStemRamp("stem", barS / rate, false, d), (e.ramp * barS) / rate));
      if (e.hold) {
        const h = e.hold, T0 = at(e.bar), T1 = at(h.untilBar);
        timers[d.id].push(setTimeout(() => { if (d.playing) d.holdStem(h.stem, lineT + h.fromBar * barS, h.bars, T0, T1); },
          Math.max(0, (T0 - audioCtx.currentTime) * 1000 - 250)));
      }
    }
    book(d, at(len) + 0.03, null, 0.02);
    // a strip on purpose, on its section's last quarter: dip allowed, reported
    dipReport(d, "remix", remixEvents(kind, len), len, lineT, barS, DIP_ALLOWED.breakdown);
    note(d.id, `REMIX · ${REMIX_LABEL[kind] || kind}`, why || `bars ${len * 0.75}-${len} of this ${len}-bar section, back on the line`);
    return true;
  }

  // Remix inside a vocal-clip mashup on host deck d: drums + bass out for the
  // last quarter (the guest vocal over the host's synths), back on the line.
  function mashupBreak(d, entryT, bars, hostMuted) {
    if (!d || !d.stemsReady || bars < 8) return false;
    const barS = 240 / (d.bpm || 128), rate = (d._playbackRate && d._playbackRate()) || 1;
    const q = entryT + bars * 0.75 * barS, e = entryT + bars * barS;
    const keep = hostMuted ? { vocals: 0 } : {};
    book(d, audioAt(d, q), { ...keep, drums: 0, bass: 0 }, barS / 4 / rate);
    book(d, audioAt(d, e) - 0.01, { ...keep, drums: 1, bass: 1 }, 0.01);
    dipReport(d, "mashup break", [{ bar: bars * 0.75, stems: { ...keep, drums: 0, bass: 0 }, ramp: 0.25 },
      { bar: bars, stems: { ...keep, drums: 1, bass: 1 }, ramp: 0 }], bars, entryT, barS, DIP_ALLOWED.breakdown);
    note(d.id, "REMIX · MASHUP BREAK", `drums + bass out for the last ${bars / 4} bars of the mashup, back on the line`);
    return true;
  }

  // Run a stem bridge: A's bar 0 at audio time t0; B starts at its track time
  // bFrom (4 B-bars before its entry line) at native tempo. Returns seconds.
  function stemBridge(outId, innId, t0, bEntryTrack, why) {
    const out = root.decks[outId], inn = root.decks[innId];
    if (!out || !inn) return 0;
    if (!out.stemsReady && out.rearmStems) out.rearmStems("stem bridge");
    if (!out.stemsReady || !inn.stems) return 0;
    const rA = (out._playbackRate && out._playbackRate()) || 1;
    const barA = 240 / (out.bpm || 128) / rA, barB = 240 / (inn.bpm || 128);
    const keyClash = camelotClash(out, inn);
    const pA = out._positionAt ? out._positionAt(t0) : out._currentPosition();
    const aSings = vocalShare(out.analysis && out.analysis.vocal_active_regions, pA, pA + 4 * barA * rA) >= 0.3;
    const bFrom = Math.max(0, bEntryTrack - 4 * barB);
    // measured energy, binned in real seconds (plan time): A per its bar, B from bStart
    const span = 8 * Math.max(barA, barB) + 4 * barA;
    const nA = Math.ceil(span / barA) + 1, nB = Math.ceil(span / barB) + 1;
    const eA = stemEnergyBars(out, pA, barA * rA, nA), eB = stemEnergyBars(inn, bFrom, barB, nB);
    const aA = stemEnergyBars(out, pA, barA * rA, nA, "audible"), aB = stemEnergyBars(inn, bFrom, barB, nB, "audible");
    const both = eA && eB && aA && aB;
    const outFn = (E) => (both ? (n, t) => E[n][Math.max(0, Math.min(E[n].length - 1, Math.floor(t / barA)))] : null);
    const inFn = (E, bStart) => (both ? (n, t) => E[n][Math.max(0, Math.min(E[n].length - 1, Math.floor((t - bStart) / barB)))] : null);
    const intro0 = keyClash ? "drums" : pickIntro({ keyClash, aSings: true, bSings: false, energy: meanOver(eB, 0, 2) });
    let plan = null, check = null;
    for (const fix of [{}, { introLevel: 1 }]) {
      const p = stemBridgePlan(barA, barB, keyClash, aSings, Object.assign({ intro: intro0 === "vocals" ? "other" : intro0 }, fix));
      // full-band floor and the audible band (a silent second fails; plan units are seconds)
      check = gates(p, p.fader, p.total, { eOut: outFn(eA), eIn: inFn(eB, p.bStart), aOut: outFn(aA), aIn: inFn(aB, p.bStart) }, 1,
        { inStart: p.bStart, step: barA / 16, win: barA / 4 });
      if (check.ok) { plan = p; break; }
    }
    if (!plan) {
      console.info(`stem bridge ${outId}->${innId} refused: ${check.reason}`);
      return 0;
    }
    cancel(outId); cancel(innId);
    for (const e of plan.events) {
      const d = e.deck === "out" ? out : inn, at = t0 + e.t;
      if (e.start) {
        timers[innId].push(setTimeout(() => {
          inn.rampPitchPercent(0, 0.005);                       // B at its own tempo: no beat ever overlaps (fast glide, never a jump)
          inn.play(bFrom, false, at);
          setTimeout(() => inn.stemMix(e.stems, at - 0.005, 0.005), 150);
        }, Math.max(0, (at - audioCtx.currentTime) * 1000 - 600)));
      } else if (e.hold) {
        timers[outId].push(setTimeout(() => { if (out.playing) out.holdStem(e.hold.stem, pA + e.hold.fromBar * barA * rA, e.hold.bars, at, t0 + e.until); },
          Math.max(0, (at - audioCtx.currentTime) * 1000 - 250)));
       } else book(d, at, e.stems, Math.max(minStemRamp("stem", 1, false, d), e.ramp)); // e.ramp is in seconds (plan units)
    }
    root.dispatchEvent(new CustomEvent("ai-cue", { detail: { at: t0 + plan.bEntry, kind: "drop", deck: innId, bar: barB,
      why: "B's beat lands after the stem bridge" } }));
    console.info(`stem bridge ${outId}->${innId}: B in on its ${plan.intro === "drums" ? "drums" : "pads"}; master floor ${check.minDb.toFixed(1)} dB`);
    note(outId, `STEM BRIDGE ${outId.toUpperCase()} → ${innId.toUpperCase()}`, why ||
      `any tempo: strip A, ${aSings ? "hold its voice, " : ""}B's ${plan.intro === "drums" ? "drums (keys clash)" : "pads"} in beatless, B's beat drops on its own line`);
    lastBridgePlan = plan;
    return plan.total;
  }
  // The crossfader moves of the last stem bridge (seconds from t0, B-ward units).
  let lastBridgePlan = null;
  function bridgeFader() { return lastBridgePlan ? lastBridgePlan.fader : null; }

  // Run it. t0 = A's phrase line (audio time); bEntry = B's vocal phrase start
  // (track time); B must already carry tempo stems at A's tempo when they differ.
  // Both gates on one plan: the full-band floor (levelCheck) and the audible band
  // (masterAudibility, a silent run of 1 s fails). e: {eOut, eIn, aOut, aIn}.
  function gates(plan, fader, span, e, minRun, extra = {}) {
    const lv = levelCheck(Object.assign({ events: plan.events, fader, dir: 1, span, eOut: e.eOut, eIn: e.eIn }, extra));
    if (!lv.ok) return lv;
    const au = masterAudibility(Object.assign({ events: plan.events, fader, dir: 1, span, eOut: e.aOut, eIn: e.aIn, minRun }, extra));
    return au.ok ? lv : au;
  }
  // Measured energies of both decks over a plan, full + audible band (all null
  // when either deck has no decoded stems: typical shares, not judged for silence).
  function planEnergies(out, pA, inn, bEntry, bars) {
    const barA = 240 / (out.bpm || 128), barB = 240 / (inn.bpm || 128);
    const e = { eOut: stemEnergyBars(out, pA, barA, bars), eIn: stemEnergyBars(inn, bEntry, barB, bars),
                aOut: stemEnergyBars(out, pA, barA, bars, "audible"), aIn: stemEnergyBars(inn, bEntry, barB, bars, "audible") };
    return e.eOut && e.eIn && e.aOut && e.aIn ? e : { eOut: null, eIn: null, aOut: null, aIn: null };
  }

  // Plays mergeTransitionPlan like mashupTransition (B enters on its key-locked
  // tempo stems at A's tempo, floor-checked). Returns seconds, 0 = refused.
  // The picked combo's plan must keep the master audible (gates); else the best
  // other combo that does (ranked: the booking's own ranking, or mergeRank on the
  // measured stems), else refused (the autopilot's next path runs).
  function mergeTransition(outId, innId, t0, bEntry, M, pick, why, ranked) {
    const out = root.decks[outId], inn = root.decks[innId];
    if (!out || !inn || !pick) return 0;
    if (!out.stemsReady && out.rearmStems) out.rearmStems("merge");
    if (!out.stemsReady || !inn.stems) return 0;
    const barS = 240 / (out.bpm || 128) / ((out._playbackRate && out._playbackRate()) || 1);
    const bRate = (out.bpm * out._playbackRate()) / inn.bpm;
    const keyClash = camelotClash(out, inn);
    const pA = out._positionAt ? out._positionAt(t0) : out._currentPosition();
    const e = planEnergies(out, pA, inn, bEntry, M + 9);
    const others = ranked || (e.eOut ? mergeRank({ eA: meanOver(e.eOut, 0, M), eB: meanOver(e.eIn, 0, M), keyScore: keyScoreOf(out, inn),
      bRap: !!(inn._vocalEntry && inn._vocalEntry.rap) }) : []);
    const booked = mergeBooking(M, pick, keyClash, others, (p) => gates(p, mergeFader(M), p.total, e, 1 / barS));
    if (!booked.plan) { console.info(`merge ${outId}->${innId} refused: ${booked.reason}`); return 0; }
    if (booked.repaired) {
      console.info(`merge ${outId}->${innId}: ${mergeLabel(booked.pick.combo)} instead (${booked.reason})`);
      pick = booked.pick;
    }
    const plan = booked.plan;
    cancel(outId); cancel(innId);
    for (const e of plan.events) {
      const d = e.deck === "out" ? out : inn, at = t0 + e.bar * barS;
      if (e.start) {
        timers[innId].push(setTimeout(() => {
          inn.rampPitchPercent((bRate - 1) * 100, 0.005);
          inn.play(bEntry, false, at);
          setTimeout(() => inn.stemMix(e.stems, at - 0.005, 0.005), 150);
        }, Math.max(0, (at - audioCtx.currentTime) * 1000 - 700)));
      } else book(d, at, e.stems, Math.max(minStemRamp("stem", barS, false, d), e.ramp * barS));
    }
    root.dispatchEvent(new CustomEvent("ai-cue", { detail: { at: t0 + M * barS, kind: "drop", deck: innId, bar: barS,
      why: "B takes every stem on the line after the merge" } }));
    note(outId, `MERGE → ${innId.toUpperCase()} · ${pick.label} · ${M} bars`, why ||
      `${pick.label}${pick.ear ? `; ear ${pick.ear.score}/10: ${pick.ear.why}` : ""}`);
    return plan.total * barS;
  }

  function mashupTransition(outId, innId, t0, bEntry, M, vox, why) {
    const out = root.decks[outId], inn = root.decks[innId];
    if (!out || !inn) return 0;
    if (!out.stemsReady && out.rearmStems) out.rearmStems("mashup");
    if (!out.stemsReady || !inn.stems) return 0;
    const barS = 240 / (out.bpm || 128) / ((out._playbackRate && out._playbackRate()) || 1);
    const bRate = (out.bpm * out._playbackRate()) / inn.bpm;          // B follows A's tempo (key-locked stems)
    const plan = mashupTransitionPlan(M, vox);
    const barB = 240 / inn.bpm;
    // B's voice is its intro stem (A's is out on bar 0: one singer). Floor check
    // (full band + audible band) against the fader the autopilot runs (mergeFader).
    const pA = out._positionAt ? out._positionAt(t0) : out._currentPosition();
    const lv = gates(plan, mergeFader(M), plan.total, planEnergies(out, pA, inn, bEntry, plan.total + 1), 1 / barS);
    if (!lv.ok) {
      console.info(`mashup ${outId}->${innId} refused: ${lv.reason}`);
      return 0;
    }
    cancel(outId); cancel(innId);
    for (const e of plan.events) {
      const d = e.deck === "out" ? out : inn, at = t0 + e.bar * barS;
      if (e.start) {
        timers[innId].push(setTimeout(() => {
          inn.rampPitchPercent((bRate - 1) * 100, 0.005);
          inn.play(bEntry, false, at);
          setTimeout(() => inn.stemMix(e.stems, at - 0.005, 0.005), 150);
        }, Math.max(0, (at - audioCtx.currentTime) * 1000 - 700)));
      } else if (e.hold) {
        const until = t0 + e.hold.untilBar * barS;
        timers[innId].push(setTimeout(() => { if (inn.playing) inn.holdStem("vocals", bEntry + e.hold.fromBar * barB, 1, at, until); },
          Math.max(0, (at - audioCtx.currentTime) * 1000 - 250)));
      } else book(d, at, e.stems, Math.max(minStemRamp("stem", barS, false, d), e.ramp * barS));
    }
    root.dispatchEvent(new CustomEvent("ai-cue", { detail: { at: t0 + M * barS, kind: "drop", deck: innId, bar: barS,
      why: "B's beat takes over after the mashup" } }));
    note(outId, `MASHUP → ${innId.toUpperCase()} · ${M} bars`, why ||
      `A's instrumental under B's vocal, hold vox, A's beat drops out, B's beat takes over on the line, 8-bar crossfade`);
    return plan.total * barS;
  }

  function hookDrop(d, item, why) {
    if (!d || !d.stemsReady || !item) return false;
    cancel(d.id);
    const bar = 240 / (d.bpm || 128), rate = (d._playbackRate && d._playbackRate()) || 1;
    for (const e of hookDropEvents(item, bar)) book(d, audioAt(d, e.t), e.stems, e.ramp / rate);
    root.dispatchEvent(new CustomEvent("ai-cue", { detail: { at: audioAt(d, item.drop_at), kind: "drop",
      deck: d.id, bar: bar / rate, why: `the beat slams back after "${item.text}"` } }));
    note(d.id, `HOOK DROP · "${item.text}"`, why || "beat out under the emotional line, then the drop");
    return true;
  }

  root.stemMoves = { core, mergeTransition, hookDrop, breakdown, handoff, instrumental, reset, audioAt, vocalShare, stemBlend, remix, REMIX_LABEL, mashupBreak, stemBridge, mashupTransition,
                     bridgeFader, stemEnergyBars, eqIntro };

  // ------------------------------------------------------ stem rail UI --
  // Per deck, under the loop rail: separation status + one toggle per stem.
  // Lit = playing, dim = muted, gold flash = the AI moved it.
  const LABEL = { drums: "DRUMS", bass: "BASS", vocals: "VOX", other: "SYNTH" };
  let userClick = false;
  function buildRail(id) {
    const panel = document.getElementById(`deck-${id}`);
    const loopRail = panel && panel.querySelector(".rail");
    if (!loopRail || panel.querySelector(".stem-rail")) return;
    const rail = document.createElement("div");
    rail.className = "rail stem-rail";
    rail.innerHTML = `<span class="hud-label">STEMS</span><span class="stem-status" id="stem-status-${id}">—</span>` +
      ["drums", "bass", "vocals", "other"].map((n) =>
        `<button class="hw-btn stem-btn" data-deck="${id}" data-stem="${n}" disabled title="Mute / unmute the ${LABEL[n].toLowerCase()} stem">` +
        `<span class="stem-name">${LABEL[n]}</span><canvas class="stem-scope" data-deck="${id}" data-stem="${n}" width="120" height="28"></canvas></button>`).join("") +
      `<button class="hw-btn stem-btn stem-all" data-deck="${id}" data-stem="all" disabled title="Back to the full mix">MIX</button>`;
    loopRail.after(rail);
    rail.addEventListener("click", (e) => {
      const b = e.target.closest(".stem-btn");
      const d = root.decks && root.decks[id];
      if (!b || !d || !e.isTrusted) return;
      userClick = true;
      try {
        if (b.dataset.stem === "all") d.stemMix(null);
        else {
          const cur = d.stemState ? d.stemState[b.dataset.stem] : 1;
          d.stemMix({ [b.dataset.stem]: cur > 0.5 ? 0 : 1 }, 0, 0.03);
        }
      } finally { userClick = false; }
    });
  }
  function paint(id, flash) {
    const d = root.decks && root.decks[id];
    const st = document.getElementById(`stem-status-${id}`);
    if (!d || !st) return;
    const ready = d.stemsReady, has = !!d.stems;
    st.textContent = d._meterGain ? "RIFF · KEY-LOCKED" : ready ? (d.stemState ? "LIVE STEMS" : "READY · full mix") : has ? "LOADED" : d.buffer ? "SEPARATING…" : "—";
    st.className = `stem-status${ready || d._meterGain ? " stem-ready" : d.buffer && !has ? " stem-wait" : ""}`;
    document.querySelectorAll(`.stem-btn[data-deck="${id}"]`).forEach((b) => {
      b.disabled = !ready;
      const n = b.dataset.stem;
      const v = n === "all" ? (d.stemState ? 0 : 1) : d.stemState ? d.stemState[n] : 1;
      b.classList.toggle("stem-on", v > 0.5);
      b.classList.toggle("stem-part", v > 0.05 && v <= 0.5);
      if (n === "vocals") b.classList.toggle("stem-bus", !!(d.stemState && d.stemState.bus > 0.5));
      if (flash && n !== "all") { b.classList.remove("stem-ai"); void b.offsetWidth; b.classList.add("stem-ai"); }
    });
  }
  ["a", "b"].forEach(buildRail);

  // ---- mini players: a scrolling level trace per stem ------------------
  // Pre-fader level (what the stem is playing), drawn bright in proportion to
  // what the crowd hears of it; a muted stem keeps scrolling, dim.
  const COLS = 60, hist = {}, scopes = [];
  const buf = new Float32Array(512);
  document.querySelectorAll(".stem-scope").forEach((c) => {
    const key = `${c.dataset.deck}:${c.dataset.stem}`;
    hist[key] = new Float32Array(COLS);
    scopes.push({ c, ctx: c.getContext("2d"), key, deck: c.dataset.deck, stem: c.dataset.stem });
  });
  function accent(deck) {
    return getComputedStyle(document.getElementById(`deck-${deck}`)).getPropertyValue("--accent").trim() || "#0f6";
  }
  const colour = { a: null, b: null };
  let last = 0, frame = 0;
  function drawScopes(t) {
    requestAnimationFrame(drawScopes);
    if (t - last < 33) return;                   // ~30 fps
    last = t;
    if (!colour.a) { colour.a = accent("a"); colour.b = accent("b"); }
    frame++;
    for (const s of scopes) {
      const d = root.decks && root.decks[s.deck];
      const live = d && d.playing !== undefined && (d.stemsReady || d._meterGain);
      const h = hist[s.key];
      h.copyWithin(0, 1);
      let g = 0;
      if (live) {
        const r = d.meterRead(s.stem, buf);
        h[COLS - 1] = Math.min(1, r.level * 4);
        g = Math.max(0, Math.min(1, r.gain));
      } else h[COLS - 1] = 0;
      const { ctx, c } = s, W = c.width, H = c.height, bw = W / COLS;
      ctx.clearRect(0, 0, W, H);
      ctx.fillStyle = colour[s.deck];
      ctx.globalAlpha = live ? 0.18 + 0.82 * g : 0.08;
      for (let i = 0; i < COLS; i++) {
        const v = h[i] * (H - 2);
        ctx.fillRect(i * bw, (H - v) / 2, Math.max(1, bw - 1), Math.max(1, v));
      }
      ctx.globalAlpha = 1;
    }
  }
  requestAnimationFrame(drawScopes);
  root.addEventListener("ai-activity", (e) => {
    const d = e.detail || {};
    if (d.kind === "stems") paint(d.deck, !userClick);
  });
  setInterval(() => { paint("a"); paint("b"); }, 1000);
})(typeof window !== "undefined" ? window : globalThis);
