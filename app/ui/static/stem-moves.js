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
  // -> "other" | "drums" | "vocals", or null when keys clash and B's drums are silent there
  // (no key-safe layer to bring in: the caller skips the intro rather than book a silent stem).
  function pickIntro(c) {
    if (c.keyClash) return !c.energy || (c.energy.drums || 0) >= INTRO_MIN_RMS ? "drums" : null;
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
  // true when the measured intro stem really plays (unmeasured energy: cannot judge, ok)
  function introAudible(intro, energy) { return !!intro && (!energy || (energy[intro] || 0) >= INTRO_MIN_RMS); }
  // Loudness + EQ gate for the intro stem, on top of the silence check. The intro plays
  // at INTRO_LEVEL under A's full mix: if its measured level sits more than
  // INTRO_MAX_UNDER_DB below A's mix over the same window it is inaudible (a stem
  // -26 dB under the mix booked a silent-sounding entry), and a drums-only intro with
  // B's low EQ cut has no kick, only hats. -> {ok, why}
  // c: {intro, energy (B per stem), outEnergy (A per stem, same window) | null, lowCut}
  const INTRO_MAX_UNDER_DB = 20;
  const mixRms = (e) => (e ? Math.sqrt(STEMS.reduce((s, n) => s + (e[n] || 0) ** 2, 0)) : 0);
  function introGate(c) {
    if (!introAudible(c.intro, c.energy)) return { ok: false, why: "no audible intro stem on B" };
    if (c.intro === "drums" && c.lowCut) return { ok: false, why: "drums-only intro while the low EQ is cut (no kick)" };
    const out = mixRms(c.outEnergy), lvl = c.energy ? (c.energy[c.intro] || 0) * (INTRO_LEVEL[c.intro] || 0.8) : 0;
    if (out > 0 && c.energy && lvl < out * Math.pow(10, -INTRO_MAX_UNDER_DB / 20)) {
      return { ok: false, why: `intro ${c.intro} ${(20 * Math.log10(Math.max(lvl, 1e-9) / out)).toFixed(0)} dB under A's mix (max -${INTRO_MAX_UNDER_DB})` };
    }
    return { ok: true, why: "" };
  }
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
    if (kind !== "double" && !introAudible(intro, c.introEnergy)) {
      return { events: [], intro, fix: {}, check: { ok: false, reason: "no audible intro stem on B" }, refused: true };
    }
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

  // ---- ARTIST SIGNATURE MOVES, batch B (research/notes/artist-signature-techniques.md) ----------------
  // Both run in the mashup slot as variants of the mashup transition (user: "mashup beats every other move").
  //  S1 filter_loop  Guetta on Erick Morillo (SOURCED): "make a loop on four bars and then play with the external
  //                  filters, delays, and acapellas live". A's last vocal-free bars loop (stemSlices on drums, bass,
  //                  other), A's low shelf sweeps to the kill and its mids dip (the console's filter is its EQ) with a
  //                  short echo, B's vocal enters on the fresh pass; on the line B's beat drops, A's loop fades over
  //                  one bar. Booked when the full mashup has no room left in A: the loop IS the room.
  //  S9 drums_host   Dom Dolla edits (SOURCED), Four Tet / Anyma mechanics GUESS: A strips to drums only, B's vocal
  //                  rides it. Nothing pitched from A sounds under B, so the Camelot gate is waived (drumsWaiver,
  //                  owner rule) when A's drums are measured energetic.
  const ARTIST_SLICE_MAX_S = 38;       // deck-controller SLICE_MAX_S is 40 audio s: one slice window stays under it
  const medianOf = (a) => {
    const s = (a || []).filter(Number.isFinite).sort((x, y) => x - y);
    if (!s.length) return null;
    const m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  };
  // median and median absolute deviation (the margin: a track's own spread, not a constant)
  function spread(a) {
    const med = medianOf(a);
    return med == null ? null : { med, mad: medianOf(a.filter(Number.isFinite).map((x) => Math.abs(x - med))) };
  }
  // Drums per bar from a drums-stem envelope ({t0, hop, v}): level = RMS over the bar, density = share of its
  // 16th-note hops at or above `thr` (the track's median active hop). -> {level: [], density: []}
  function drumsBars(env, t0, bar, n, thr) {
    const level = [], density = [];
    if (!env || !env.v || !env.v.length || !(bar > 0)) return { level, density };
    for (let i = 0; i < n; i++) {
      const a = Math.floor((t0 + i * bar - env.t0) / env.hop + 1e-9), b = Math.floor((t0 + (i + 1) * bar - env.t0) / env.hop + 1e-9);
      let sq = 0, on = 0, c = 0;
      for (let k = Math.max(0, a); k < Math.min(env.v.length, b); k++) { sq += env.v[k] ** 2; if (env.v[k] >= thr) on++; c++; }
      if (!c) break;
      level.push(Math.sqrt(sq / c)); density.push(on / c);
    }
    return { level, density };
  }
  // The track-wide active-hop threshold of a drums envelope: median of the hops above INTRO_MIN_RMS.
  const activeThr = (env) => medianOf(((env && env.v) || []).filter((x) => x >= INTRO_MIN_RMS)) || INTRO_MIN_RMS;
  // OWNER RULE (drumsOnlyKeyWaiver, default on): the Camelot gate is waived for a DRUMS-ONLY layer when
  //  (1) role "host": the drums stem is energetic over the window (its median bar level AND density strictly above
  //      the same track's median + median absolute deviation) and the other, pitched track is not on the master
  //      at that moment; or
  //  (2) role "enter": the drums enter over a stem of the other track that is already sounding on the master.
  // A pitched stem of the drums track still audible: the key gate stands. Sub-bass owner, vocal clash and the
  // loudness gates are the caller's, unchanged.
  // c: {enabled, role, win: {level, density}, track: {level, density}, pitchedOn, otherOnMaster, otherStemSounding}
  // -> {granted, why, text: "drums_waiver: granted|refused: <why>", measured}
  function drumsWaiver(c) {
    const res = (granted, why, measured = null) => ({ granted, why, text: `drums_waiver: ${granted ? "granted" : "refused"}: ${why}`, measured });
    if (!c || !c.enabled) return res(false, "drumsOnlyKeyWaiver is off");
    if (c.pitchedOn) return res(false, "a pitched stem of the drums track is audible: the key gate stands");
    const w = c.win || {}, lvl = medianOf(w.level), den = medianOf(w.density);
    if (lvl == null || den == null) return res(false, "the drums stem could not be measured over the window");
    if (lvl < INTRO_MIN_RMS) return res(false, "the drums stem is silent over the window");
    const m = { level: +lvl.toFixed(4), density: +den.toFixed(3) };
    if (c.role === "enter") {
      return c.otherStemSounding ? res(true, "drums enter over the other track's stem already on the master", m)
        : res(false, "no stem of the other track is sounding on the master for the drums to enter over", m);
    }
    if (c.otherOnMaster) return res(false, "the pitched track is already on the master", m);
    const tl = spread((c.track || {}).level), td = spread((c.track || {}).density);
    if (!tl || !td) return res(false, "the track's own drums could not be measured", m);
    Object.assign(m, { track_level: +tl.med.toFixed(4), level_margin: +tl.mad.toFixed(4), track_density: +td.med.toFixed(3), density_margin: +td.mad.toFixed(3) });
    if (!(lvl > tl.med + tl.mad)) return res(false, `drums level ${lvl.toFixed(3)} not above the track's median ${tl.med.toFixed(3)} + margin ${tl.mad.toFixed(3)}`, m);
    if (!(den > td.med + td.mad)) return res(false, `drums density ${den.toFixed(2)} not above the track's median ${td.med.toFixed(2)} + margin ${td.mad.toFixed(2)}`, m);
    return res(true, `energetic drums (level ${lvl.toFixed(3)}, density ${den.toFixed(2)} over the track's median + margin), the pitched track is off the master`, m);
  }
  // S9 plan, bars from A's line (the mashup's bar 0). A strips to drums (lifted ~+2 dB so the host does not
  // sag); B starts silent and its vocal only rises once A's pitched stems are gone (rule 1: the pitched track
  // is not on the master while A still has one up). On the line B's beat and tones drop and A's drums leave
  // with the same short ramp (a drum swap, not a cut). Holds as in the mashup.
  function drumsHostPlan(M, v) {
    const ev = [
      { bar: 0, deck: "out", stems: { vocals: 0, bass: 0, other: 0, drums: 1.25 }, ramp: 0.25 },
      { bar: 0, deck: "in", start: true, stems: { drums: 0, bass: 0, vocals: 0, other: 0 }, ramp: 0 },
      { bar: 0.25, deck: "in", stems: { vocals: v }, ramp: 0.75 },
      { bar: M / 2, deck: "in", stems: { vocals: Math.min(1, v * 1.3) }, ramp: 2 },
      { bar: M, deck: "out", stems: { drums: 0 }, ramp: 0.05 },
      { bar: M, deck: "in", stems: { drums: 1, bass: 1, other: 1, vocals: 1 }, ramp: 0.05 },
      { bar: M + 1, deck: "in", stems: null, ramp: 0.05 },
    ];
    for (let s = 0; s + 16 <= M; s += 16) {
      if (s + 16 === M) continue;
      ev.push({ bar: s + 12, deck: "in", hold: { stem: "vocals", fromBar: s + 11, bars: 1, untilBar: s + 16 } });
    }
    return { events: ev.sort((a, b) => a.bar - b.bar), total: M + 8, kind: "drums_host" };
  }
  // S1 plan. c: {pA (A's song time at bar 0, a bar line), barA (A song s per bar), rate (A's playback rate),
  //   bpmEff (A's heard BPM), before: A's per-stem RMS per bar over the 4 bars before pA ({drums, bass, vocals,
  //   other}: arrays, oldest first) | null, aLeftBars (A song bars after pA), centroidHz (A's mix over the loop,
  //   measured) | null, bLineBeats (B's first sung line from its entry, beats) | null, v (B vocal gain)}
  // Loop length: 4 bars when all 4 bars before the line are vocal-free with the drums up, else 2 (the last 2),
  // else refused. Passes: 2 when B's line is longer than the loop (or unknown), else 1 (4 + 4 lands on B's
  // 8-bar line). -> plan | {ok: false, gate, reason}
  function filterLoopPlan(c) {
    const no = (gate, reason) => ({ ok: false, gate, reason });
    const fallbacks = [];
    const b = c.before;
    if (!b || !Array.isArray(b.vocals) || b.vocals.length < 2) return no("unmeasured", "A's stems before the line could not be measured");
    const clean = (i) => (b.vocals[i] || 0) < INTRO_MIN_RMS && (b.drums[i] || 0) >= INTRO_MIN_RMS;
    let cleanBars = 0;
    for (let i = b.vocals.length - 1; i >= 0 && clean(i); i--) cleanBars++;
    const L = cleanBars >= 4 ? 4 : cleanBars >= 2 ? 2 : 0;
    if (!L) return no("vocal_in_loop", `only ${cleanBars} vocal-free bars with drums before the line (need 2)`);
    const lv = b.drums.slice(-L).map((x, i) => Math.hypot(x, b.bass[b.bass.length - L + i] || 0, b.other[b.other.length - L + i] || 0));
    const hi = Math.max(...lv), lo = Math.min(...lv);
    if (hi / Math.max(lo, 1e-9) > 3) return no("unsteady", `the loop's bars swing ${(hi / Math.max(lo, 1e-9)).toFixed(1)}x in level`);
    let bLine = c.bLineBeats;
    if (!(bLine > 0)) { bLine = null; fallbacks.push("b_line"); }
    let passes = L === 4 ? (bLine == null || bLine > 16 ? 2 : 1) : 2;
    const loopS = L * c.barA;
    // the loop plays passes + 1 times: the last pass fades under B's drop, so A's live song never returns
    while (passes >= 1 && ((passes + 1) * loopS) / (c.rate || 1) > ARTIST_SLICE_MAX_S) passes--;
    if (passes < 1) return no("slice_cap", `a ${L}-bar loop is longer than the ${ARTIST_SLICE_MAX_S} s slice cap`);
    const M = L * passes;
    if (!(c.aLeftBars >= (passes + 1) * L + 1)) return no("a_ends", `${Math.floor(c.aLeftBars || 0)} bars left in A (need ${(passes + 1) * L + 1})`);
    const w0 = c.pA - loopS;
    const slices = Array.from({ length: passes + 1 }, (_, i) => ({ from: w0, dur: loopS, off: i * loopS }));
    // echo: 1/2 beat when that is a slapback (<= 0.35 s), else 1/4 beat; wet under the 0.35 cap
    const beatS = 60 / (c.bpmEff || 128);
    const delay = { time: +(beatS / 2 <= 0.35 ? beatS / 2 : beatS / 4).toFixed(4), division: beatS / 2 <= 0.35 ? "1/2" : "1/4", wet: 0.3 };
    fallbacks.push("delay_wet");
    // filter: the low shelf (120 Hz) always sweeps to the kill (sub to B on the line); the mids dip deeper the
    // brighter A's loop is (the high-pass "end" = half A's spectral centroid, 150 Hz..1 kHz)
    let hpHz;
    if (c.centroidHz > 0) hpHz = Math.round(Math.max(150, Math.min(1000, c.centroidHz / 2)));
    else { hpHz = 400; fallbacks.push("centroid"); }
    const filter = { lowDb: -26, midDb: -Math.round(12 * Math.max(0, Math.min(1, (hpHz - 150) / 850))), hpHz, fromBar: 0, toBar: M - 0.25 };
    const events = [
      { bar: 0, deck: "out", stems: { vocals: 0 }, ramp: 0.25 },
      { bar: 0, deck: "in", start: true, stems: { drums: 0, bass: 0, vocals: c.v, other: 0 }, ramp: 0 },
      { bar: M, deck: "in", stems: { drums: 1, bass: 1, other: 1, vocals: 1 }, ramp: 0.05 },
      { bar: M, deck: "out", stems: { drums: 0, bass: 0, other: 0 }, ramp: 1 },
      { bar: M + 2, deck: "in", stems: null, ramp: 0.05 },
    ];
    return { ok: true, kind: "filter_loop", L, passes, M, total: M + 8, events, slices, sliceStems: ["drums", "bass", "other"], filter, delay, fallbacks,
      params: { loop_bars: L, passes, mashup_bars: M, clean_bars: cleanBars, b_line_beats: bLine, hp_hz: hpHz, mid_db: filter.midDb, delay_s: delay.time, delay_div: delay.division, wet: delay.wet },
      why: `filter loop: A's last ${L} vocal-free bars looped x${passes + 1} under a low-shelf sweep (mids ${filter.midDb} dB) + ${delay.division}-beat echo, B's vocal on the fresh pass, B's beat on bar ${M}` };
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
  // energy (optional, mean RMS per stem over the move's window, see remixEnergy): the kept tonal
  // stem must really play there. An unzeroed stem that is silent (synth hold on an empty "other")
  // is dead air, not a vibe. Unmeasured (null): judged by gains alone.
  const REMIX_FEATURE = { synth_hold: "other", acapella: "vocals", vocal_hold: "vocals" };
  const REMIX_REL_FLOOR = 0.15;      // a kept stem needs >= 15 % of the section's mix RMS
  function stemPlays(energy, n) {
    if (!energy) return true;
    let mix = 0;
    for (const s of STEMS) mix += (energy[s] || 0) ** 2;
    return (energy[n] || 0) >= Math.max(INTRO_MIN_RMS, REMIX_REL_FLOOR * Math.sqrt(mix));
  }
  function keepsVibe(kind, len = 16, energy = null) {
    const ok = remixEvents(kind, len).every((e) => !e.stems || ["bass", "vocals", "other"].some((n) => e.stems[n] !== 0 && stemPlays(energy, n)));
    return ok && (!REMIX_FEATURE[kind] || stemPlays(energy, REMIX_FEATURE[kind]));
  }
  // Strip & rebuild leaves the voice alone: it needs a vocal that really sings in the window.
  function breakdownVocalOk(energy) { return stemPlays(energy, "vocals"); }
  function remixPick(ctx) {
    if ((ctx.count || 0) >= REMIX_MAX_PER_SONG) return null;
    if ((ctx.barsOnTrack || 0) < 32 || (ctx.barsLeft || 0) < 48) return null;
    if (ctx.lastAtBar != null && ctx.atBar - ctx.lastAtBar < REMIX_GAP_BARS) return null;
    const used = new Set(ctx.used || []);
    const menu = ctx.vocal >= 0.5 ? ["vocal_hold", "acapella", "bass_out"]
      : ctx.vocal >= 0.15 ? ["bass_out", "drum_break", "synth_hold"] : ["drum_break", "synth_hold", "bass_out"];
    return menu.find((k) => !used.has(k) && (ctx.othersCarry || keepsVibe(k, 16, ctx.energy))) || null;
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
  // Both gates on one plan: the full-band floor (levelCheck) and the audible band
  // (masterAudibility, a silent run of 1 s fails). e: {eOut, eIn, aOut, aIn}.
  function gates(plan, fader, span, e, minRun, extra = {}) {
    const lv = levelCheck(Object.assign({ events: plan.events, fader, dir: 1, span, eOut: e.eOut, eIn: e.eIn }, extra));
    if (!lv.ok) return lv;
    const au = masterAudibility(Object.assign({ events: plan.events, fader, dir: 1, span, eOut: e.aOut, eIn: e.aIn, minRun }, extra));
    return au.ok ? lv : au;
  }
  // ---- MERGE -> HOLD -> TRANSITION (user: "when transitioning it should try to do the
  // merge of two tracks, hold, and then transition"; "best transition is when tracks
  // merge and play"). The Stem Merge plan (mergeTransitionPlan) IS the three phases;
  // what this adds is the HOLD length: whole 8-bar phrases picked from the two tracks'
  // measured stems and voices instead of a fixed 16 / 32. Bars count from B's entry:
  //   merge_start  0..2      B's stems layered in, fader to the centre (mergeFader)
  //   hold         2..M      both records playing together, tempo locked, key >= 0.8
  //   handover     M..M+8    kick + bass change hands on the line, A's tones fade, B full
  const HOLD_PHRASE_BARS = 8, HOLD_MAX_PHRASES = 6, HANDOVER_BARS = 8, MERGE_START_BARS = 2;
  const HOLD_ROOM_BARS = 2, HOLD_TEMPO_CAP = 0.08, HOLD_KEY_MIN = 0.8, SUB_OVERLAP_MAX_BARS = 0.3;
  // vocal regions [[s, e]] in song seconds -> bars from t0 (barSong = song seconds per bar)
  function regionsToBars(regions, t0, barSong) {
    return (regions || []).map(([s, e]) => [(s - t0) / barSong, (e - t0) / barSong]);
  }
  // bars where both region lists sing inside [from, to] (each list non-overlapping)
  function overlapBars(x, y, from, to) {
    let sum = 0;
    for (const [a0, a1] of x || []) {
      const s = Math.max(a0, from), e = Math.min(a1, to);
      if (e <= s) continue;
      for (const [b0, b1] of y || []) sum += Math.max(0, Math.min(e, b1) - Math.max(s, b0));
    }
    return sum;
  }
  const meanArr = (e, a, b) => {
    const m = {};
    for (const n of STEMS) { const v = (e[n] || []).slice(a, b); m[n] = v.reduce((x, y) => x + y, 0) / (v.length || 1); }
    return m;
  };
  // First phrase of a hold of k phrases in which a stem the combo needs stops playing
  // (a break, a drop-out): {phrase, stem, deck} or null when all k phrases stay clean.
  function holdUnclean(combo, eA, eB, k, floor = INTRO_MIN_RMS) {
    for (let p = 0; p < k; p++) {
      for (const n of STEMS) {
        const src = combo[n] === "a" ? eA : eB;
        const v = (src[n] || []).slice(p * HOLD_PHRASE_BARS, (p + 1) * HOLD_PHRASE_BARS);
        if ((v.reduce((x, y) => x + y, 0) / (v.length || 1)) < floor) return { phrase: p, stem: n, deck: combo[n] };
      }
    }
    return null;
  }
  // Sub-bass owners over all three phases of a merge plan: the bars in which BOTH decks
  // play their bass at an audible level (stem gain x equal-power fader). One-sample
  // swap edges (<= SUB_OVERLAP_MAX_BARS) are the line itself. -> {ok, overlapBars, none}
  function subOwnerCheck(plan, M, step = 1 / 16) {
    const fader = mergeFader(M);
    let both = 0, none = 0;
    for (let t = 0; t <= plan.total + 1e-9; t += step) {
      const x = (faderAt(fader, 1, t) + 1) / 2;
      const fo = Math.cos((x * Math.PI) / 2), fi = Math.sin((x * Math.PI) / 2);
      const a = fo * gainsAt(plan.events, "out", t).bass, b = t >= 0 ? fi * gainsAt(plan.events, "in", t).bass : 0;
      if (a >= AUDIBLE_GAIN && b >= AUDIBLE_GAIN) both += step;
      if (a < AUDIBLE_GAIN && b < AUDIBLE_GAIN) none += step;
    }
    return { ok: both <= SUB_OVERLAP_MAX_BARS, overlapBars: Math.round(both * 100) / 100, none: Math.round(none * 100) / 100 };
  }
  // Plan the merged stretch. o: {gap (tempo gap fraction after half/double), keyScore (null
  // unknown), roomBars (A's bars left from the merge start), eA, eB (per-bar stem RMS,
  // stemEnergyBars, >= the longest hold + 9 bars), aVox, bVox (vocal regions, song s),
  // aT, bT (song times at the merge start), barA, barB (song s per bar), bRap, barS
  // (real s per bar, for the seconds), aud {aA, aB} (audible band, optional)}
  // -> {ok, gate, reason} refused (gate: tempo | key | room | stems | no_combo | unclean |
  //     sub_owner | level | vocal_clash), else {ok, M, holdBars, holdPhrases, pick, ranked,
  //     phases, tried} with the three phases and their measured params.
  function holdPlan(o) {
    const no = (gate, reason, tried) => ({ ok: false, gate, reason, tried: tried || [] });
    if (o.gap != null && o.gap > HOLD_TEMPO_CAP) return no("tempo", `tempo gap ${(o.gap * 100).toFixed(1)} % over the ${HOLD_TEMPO_CAP * 100} % cap`);
    if (o.keyScore != null && o.keyScore < HOLD_KEY_MIN) return no("key", `camelot ${o.keyScore} < ${HOLD_KEY_MIN}`);
    if (!o.eA || !o.eB) return no("stems", "stem energy unmeasured");
    const kMax = Math.min(HOLD_MAX_PHRASES, Math.floor((o.roomBars - HANDOVER_BARS - HOLD_ROOM_BARS) / HOLD_PHRASE_BARS));
    if (kMax < 1) return no("room", `${Math.round(o.roomBars)} bars left, need ${HOLD_PHRASE_BARS + HANDOVER_BARS + HOLD_ROOM_BARS}`);
    const aBars = regionsToBars(o.aVox, o.aT, o.barA), bBars = regionsToBars(o.bVox, o.bT, o.barB);
    const cands = [], tried = [];
    for (let k = 1; k <= kMax; k++) {
      const M = k * HOLD_PHRASE_BARS;
      const ranked = mergeRank({ eA: meanArr(o.eA, 0, M), eB: meanArr(o.eB, 0, M), keyScore: o.keyScore, bRap: o.bRap });
      if (!ranked.length) { tried.push({ M, gate: "no_combo" }); continue; }
      const pick = ranked[0], bad = holdUnclean(pick.combo, o.eA, o.eB, k);
      if (bad) { tried.push({ M, gate: "unclean", stem: bad.stem, phrase: bad.phrase }); continue; }
      const plan = mergeTransitionPlan(M, pick.combo, false);
      const sub = subOwnerCheck(plan, M);
      if (!sub.ok) { tried.push({ M, gate: "sub_owner", overlap: sub.overlapBars }); continue; }
      const lv = o.aud ? gates(plan, mergeFader(M), plan.total, { eOut: o.eA, eIn: o.eB, aOut: o.aud.aA, aIn: o.aud.aB }, o.minRun || 1)
                       : levelCheck({ events: plan.events, fader: mergeFader(M), dir: 1, span: plan.total, eOut: o.eA, eIn: o.eB });
      if (!lv.ok) { tried.push({ M, gate: "level", reason: lv.reason }); continue; }
      // both voices singing through the handover is the clash the plan can only hide
      const ov = overlapBars(aBars, bBars, M, M + HANDOVER_BARS) / HANDOVER_BARS;
      if (ov > 0.5) { tried.push({ M, gate: "vocal_clash", overlap: Math.round(ov * 100) / 100 }); continue; }
      // longer holds win up to 4 phrases (diminishing after), a handover in a vocal gap wins ties
      const score = 10 * Math.min(k, 4) - 30 * ov + 0.1 * pick.score + (ov === 0 ? 5 : 0);
      cands.push({ k, M, pick, ranked, ov, sub, score: Math.round(score * 10) / 10 });
    }
    if (!cands.length) {
      const last = tried[tried.length - 1] || {};
      return no(last.gate || "no_combo", `no clean hold: ${tried.map((t) => `${t.M} bars ${t.gate}${t.stem ? ` (${t.stem})` : ""}`).join("; ")}`, tried);
    }
    cands.sort((x, y) => y.score - x.score || y.M - x.M);
    const b = cands[0], barS = o.barS || 0;
    return {
      ok: true, M: b.M, holdBars: b.M - MERGE_START_BARS, holdPhrases: b.k, pick: b.pick, ranked: b.ranked, tried,
      phases: {
        merge_start: { at_bar: 0, bars: MERGE_START_BARS, combo: b.pick.label },
        hold: { from_bar: MERGE_START_BARS, to_bar: b.M, bars: b.M - MERGE_START_BARS, phrases: b.k, seconds: Math.round((b.M - MERGE_START_BARS) * barS * 10) / 10 },
        handover: { at_bar: b.M, bars: HANDOVER_BARS, vocal_overlap: Math.round(b.ov * 100) / 100, sub_overlap_bars: b.sub.overlapBars },
      },
    };
  }
  const core = { holdPlan, subOwnerCheck, holdUnclean, overlapBars, regionsToBars, HOLD_PHRASE_BARS, HOLD_MAX_PHRASES, HANDOVER_BARS, MERGE_START_BARS, HOLD_TEMPO_CAP, HOLD_KEY_MIN, gates, keepsVibe, breakdownVocalOk, introAudible, introGate, INTRO_MAX_UNDER_DB, mergeCombos, mergeRank, mergeLabel, mergeTransitionPlan, mergeWithEar, keepOneStem, hookDropEvents, hookDropDue, HOOK_OTHER, BREAKDOWN, breakdownFits, handoffFits, vocalShare, stemBlendPlan, STEM_BLEND_KINDS, remixEvents, remixPick, stemBridgePlan, mashupTransitionPlan,
                 pickIntro, introBars, INTRO_LEVEL, levelCheck, gainsAt, faderAt, fitStemBlend, breakdownEvents,
                 masterAudibility, audibleRms, mergeFader, rawFader, deckFaderGains, mergeBooking, onTime, AUDIBLE_HZ, SILENCE_DB,
                 LEVEL_FLOOR_DB, AUDIBLE_GAIN, FADER_PARK_BARS, TYPICAL_SHARE, DIP_ALLOWED,
                 drumsBars, activeThr, drumsWaiver, drumsHostPlan, filterLoopPlan, ARTIST_SLICE_MAX_S };
   if (typeof module !== "undefined" && module.exports) module.exports = core;

  // ---- runtime: reaches the world only through the Host port (engine.js) -------------------------------
  function create({ host }) {
  const { setTimeout, clearTimeout, setInterval } = host.clock;
  const audioCtx = host.audio;
  const ui = host.ui;

  // Mean RMS per stem over the last quarter of a len-bar section from song time lineT (where a remix move acts).
  function remixEnergy(d, lineT, barSong, len = 16) {
    return meanOver(stemEnergyBars(d, lineT, barSong, len), Math.floor(len * 0.75), len);
  }

   // Minimum ramp seconds for stem moves: route through tempoRule if available,
   // else fallback. kind = "stem" or "level"; barS = seconds/bar; drop = exempt
   // (drop landing on its downbeat); deck = audible deck check.
   function minStemRamp(kind, barS, drop, deck) {
     if (typeof host.mod.tempoRule !== "undefined" && host.mod.tempoRule.minRampSeconds) {
       return host.mod.tempoRule.minRampSeconds(kind, barS, { drop, deck });
     }
     // Fallback: drop or silent deck -> instant (0.005), else minimum ramp
     if (drop || !deck || !deck.playing) return 0.005;
     return kind === "stem" ? barS * 0.25 : barS / 4;
   }

   // ------------------------------------------------------------ browser --
  const timers = { a: [], b: [] };
  function note(deckId, label, why) {
    host.bus.emit("ai-activity", { kind: "stem-move", deck: deckId, label, why });
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
    const cs = host.mod.djMind && host.mod.djMind.core && host.mod.djMind.core.camelotScore;
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
    const bar = 240 / (d.bpm || 128), rate = (d._playbackRate && d._playbackRate()) || 1;
    if (!breakdownVocalOk(meanOver(stemEnergyBars(d, startTrackT, bar, bars), 0, bars))) {
      console.info(`breakdown ${d.id} skipped: no vocal energy for "voice alone"`);
      return false;
    }
    cancel(d.id);
    // the strip IS the move: its dip is allowed, and says so
    dipReport(d, "breakdown", breakdownEvents(bars), bars, startTrackT, bar, DIP_ALLOWED.breakdown);
    const t0 = audioAt(d, startTrackT), beat = bar / 4 / rate;
    for (const [b, target, rampBeats] of plan) book(d, t0 + (b * bar) / rate, target, rampBeats * beat);
    host.bus.emit("ai-cue", { at: t0 + (plan[plan.length - 1][0] * bar) / rate, kind: "drop",
      deck: d.id, bar: bar / rate, why: "everything slams back after the strip & rebuild" });
    note(d.id, `STRIP & REBUILD · ${bars} bars`, why || "drums out, bass out, voice alone, rebuild, drop on the line");
    return true;
  }

  // Is deck `id`'s low EQ knob cut (dB slider at or under -12)? Unknown knob: not cut.
  function lowCutOf(id) {
    const el = ui.query(`.eq-knob[data-deck="${id}"][data-band="low"]`);
    return !!el && Number(el.value) <= -12;
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
    const out = host.decks[outId], inn = host.decks[innId];
    if (!out || !inn || !(totalS >= 2)) return false;
    const rA = (out._playbackRate && out._playbackRate()) || 1;
    const bar = 240 / (out.bpm || 128) / rA;
    const pA = out._positionAt ? out._positionAt(t0) : out._currentPosition();
    const aSings = vocalShare(out.analysis && out.analysis.vocal_active_regions, pA, pA + totalS * rA) >= 0.3;
    const pB = inn.cuePoint || 0;
    const bSings = vocalShare(inn.analysis && inn.analysis.vocal_active_regions, pB, pB + totalS) >= 0.3;
    if (inn.stemsReady) {
      const barB = 240 / (inn.bpm || 128);
      const introE = meanOver(stemEnergyBars(inn, pB, barB, 4), 0, 4);
      let intro = pickIntro({ keyClash: camelotClash(out, inn), aSings, bSings, energy: introE });
      if (intro === "vocals" && aSings) intro = "other";
      const gate = introGate({ intro, energy: introE, lowCut: lowCutOf(innId),
        outEnergy: meanOver(stemEnergyBars(out, pA, 240 / (out.bpm || 128), 4), 0, 4) });
      if (!gate.ok) {                              // A stays the full mix: no silent, quiet or kickless intro
        console.info(`stem intro ${innId} skipped: ${gate.why}`);
        return false;
      }
      cancel(innId);
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
    const out = host.decks[outId], inn = host.decks[innId];
    if (!out || !inn) return false;
    if (!out.stemsReady && out.rearmStems) out.rearmStems("vocal handoff");
    if (!out.stemsReady || !inn.stemsReady) return false;
    cancel(outId); cancel(innId);
    const bar = 240 / (out.bpm || 128) / ((out._playbackRate && out._playbackRate()) || 1);
    // B enters as its instrumental; A's vocal leaves A's strip for the bus.
    let intro = null;
    if (swapS >= 2 * bar) {
      const pB = inn.cuePoint || 0, barB = 240 / (inn.bpm || 128);
      const introE = meanOver(stemEnergyBars(inn, pB, barB, 4), 0, 4);
      intro = pickIntro({ keyClash: camelotClash(out, inn), aSings: true, bSings: false, energy: introE });
      if (intro === "vocals") intro = "other";
      const gate = introGate({ intro, energy: introE, lowCut: lowCutOf(innId),
        outEnergy: meanOver(stemEnergyBars(out, out._positionAt ? out._positionAt(t0) : out._currentPosition(), 240 / (out.bpm || 128), 4), 0, 4) });
      if (!gate.ok) { console.info(`stem intro ${innId} skipped: ${gate.why}`); intro = null; }
    }
    if (intro) {
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
    const out = host.decks[outId], inn = host.decks[innId];
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
    const out = host.decks[outId], inn = host.decks[innId];
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
    host.bus.emit("ai-cue", { at: t0 + plan.bEntry, kind: "drop", deck: innId, bar: barB,
      why: "B's beat lands after the stem bridge" });
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
  // (gates: pure, defined above the core so node can drive it too)
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
    const out = host.decks[outId], inn = host.decks[innId];
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
    host.bus.emit("ai-cue", { at: t0 + M * barS, kind: "drop", deck: innId, bar: barS,
      why: "B takes every stem on the line after the merge" });
    note(outId, `MERGE → ${innId.toUpperCase()} · ${pick.label} · ${M} bars`, why ||
      `${pick.label}${pick.ear ? `; ear ${pick.ear.score}/10: ${pick.ear.why}` : ""}`);
    return plan.total * barS;
  }

  // ---- artist moves in the mashup slot (S1 filter_loop, S9 drums_host): measured fits -------------------------
  // Refusal lines once per deck, phrase and reason; toggles ap-artist-<id> (all on by default).
  const artistSaid = new Map();
  function artistSay(key, line) {
    if (artistSaid.has(key)) return false;
    if (artistSaid.size > 300) artistSaid.clear();
    artistSaid.set(key, 1);
    console.info(line);
    return true;
  }
  const artistOn = (id) => ui.flag(`ap-artist-${id}`, true);
  const lmCore = () => (host.mod.learnedMoves && host.mod.learnedMoves.core) || root.learnedMovesCore || null;
  // the first 8-bar line at or after song time t (the booking does not know its line yet)
  function lineAfter(d, t) {
    const ph = (d.analysis && d.analysis.phrase_boundaries_8bar) || [];
    for (const p of ph) if (p >= t - 0.05) return p;
    return t;
  }
  // Whole-track drums stats of deck d (16th-note envelope of its drums stem), cached per song + stem set.
  function drumsStats(d) {
    const st = d.stems, lm = lmCore();
    if (!st || !st.drums || !st.drums.getChannelData || !lm || !(d.bpm > 0)) return null;
    if (d._drumStats && d._drumStats.stems === st && d._drumStats.ana === d.analysis) return d._drumStats;
    const bar = 240 / d.bpm, dur = (d.buffer && d.buffer.duration) || st.drums.duration || 0;
    const env = lm.envelope(st.drums.getChannelData(0), st.drums.sampleRate, 0, dur, bar / 16, st.lag || 0, st.ratio || 1);
    const thr = activeThr(env);
    const db = (d.analysis && d.analysis.downbeat_times) || [];
    const t0 = db.length ? db[0] : 0;
    d._drumStats = { stems: st, ana: d.analysis, env, thr, bar, track: drumsBars(env, t0, bar, Math.floor((dur - t0) / bar), thr) };
    return d._drumStats;
  }
  // S9: may deck od host B's vocal on its drums alone, with the key gate waived? M: the mashup bars.
  // pA: A's song time at bar 0 (null: A's next 8-bar line). -> {ok, kind, M, waiver, why} (logged either way)
  function drumsHostFits(od, idk, M, pA) {
    if (!od || !idk || !(M > 0)) return { ok: false, why: "no pair or no room" };
    const p = Number.isFinite(pA) ? pA : lineAfter(od, od._currentPosition());
    const barA = 240 / (od.bpm || 128), key = `${od.id}:${Math.floor(p / (8 * barA))}`;
    const ds = od.stemsReady ? drumsStats(od) : null;
    const w = drumsWaiver({ enabled: artistOn("drumsOnlyKeyWaiver"), role: "host", win: ds ? drumsBars(ds.env, p, barA, M, ds.thr) : null,
      track: ds ? ds.track : null, pitchedOn: false, otherOnMaster: !!(idk.playing && idk._onAir && idk._onAir()) });
    if (artistSay(`${key}:${w.text}`, `${w.text} (deck ${od.id.toUpperCase()} drums under ${idk.id.toUpperCase()}'s vocal, ${M} bars)`)) {
      host.log.step("drums_waiver", { deck: od.id, decision: w.granted ? "granted" : "refused", why: w.why, result: w.measured || {} });
    }
    return w.granted ? { ok: true, kind: "drums_host", M, waiver: w, why: `${M}-bar drums-only host: B's vocal over A's drums alone, key gate waived (${w.why})` }
      : { ok: false, waiver: w, why: w.text };
  }
  // zero-crossing estimate of the spectral centroid of a mix over [t0, t1) song s (8 windows of 2048 samples)
  function centroidHz(d, t0, t1) {
    const b = d.buffer;
    if (!b || !b.getChannelData) return null;
    const ch = b.getChannelData(0), sr = b.sampleRate, s0 = Math.max(1, Math.floor(t0 * sr)), s1 = Math.min(ch.length, Math.floor(t1 * sr));
    if (s1 - s0 < 4096) return null;
    let z = 0, n = 0;
    for (let w = 0; w < 8; w++) {
      const a = s0 + Math.floor(((s1 - s0 - 2048) * w) / 7);
      for (let j = a; j < a + 2048; j++) { if ((ch[j] >= 0) !== (ch[j - 1] >= 0)) z++; n++; }
    }
    return n ? (z / n) * sr / 2 : null;
  }
  // S1: does a filter loop fit A -> B at A's song time pA (null: A's next line)? force: on demand (the toggle
  // is the user's own click). -> filterLoopPlan result ({ok: true, ...} | {ok: false, gate, reason}; refusal logged)
  function filterLoopFits(od, idk, ve, v, pA, force) {
    if (!od || !idk || !ve) return { ok: false, gate: "pair", reason: "no pair with a known vocal entry" };
    if (!force && !artistOn("filter_loop")) return { ok: false, gate: "toggle", reason: "filter loop is off" };
    const p = Number.isFinite(pA) ? pA : lineAfter(od, od._currentPosition());
    const barA = 240 / (od.bpm || 128), rate = (od._playbackRate && od._playbackRate()) || 1;
    const key = `${od.id}:${Math.floor(p / (8 * barA))}`;
    const refuse = (gate, reason) => { artistSay(`${key}:${gate}`, `artist move filter_loop skipped: ${gate}: ${reason}`); return { ok: false, gate, reason }; };
    if (!od.stemsReady || !idk.stems) return refuse("stems", "stems on both decks are needed");
    const e = stemEnergyBars(od, p - 4 * barA, barA, 4);
    let bLine = null;
    const lm = lmCore(), sv = idk.stems.vocals;
    if (lm && sv && sv.getChannelData && idk.bpm > 0) {
      const beatB = 60 / idk.bpm;
      const env = lm.envelope(sv.getChannelData(0), sv.sampleRate, ve.entry, ve.entry + 32 * beatB, beatB / 4, idk.stems.lag || 0, idk.stems.ratio || 1);
      const ln = lm.vocalLines(env)[0];
      if (ln) bLine = Math.round((ln.e - Math.max(ln.s, ve.entry)) / beatB);
    }
    const plan = filterLoopPlan({ pA: p, barA, rate, bpmEff: od.bpm * rate, before: e, aLeftBars: od.buffer ? (od.buffer.duration - p) / barA : 0,
      centroidHz: centroidHz(od, p - 4 * barA, p), bLineBeats: bLine, v });
    return plan.ok ? plan : refuse(plan.gate, plan.reason);
  }
  // ---- on demand (AI ACTIONS "filter_loop" / "drums_host"): the loaded pair, now, on the host's next 8-bar line.
  // Skips only the autopilot's own booking; every safety gate stands (stems, tempo lock, key or waiver, room,
  // loudness). The crossfader follows mergeFader like the autopilot's mashup. -> {ok, why}
  function artistRunNow(id) {
    const ds = host.decks || {};
    const hId = ["a", "b"].find((k) => ds[k] && ds[k].playing && (!ds[k]._onAir || ds[k]._onAir()));
    if (!hId) return { ok: false, why: "nothing is playing on the master" };
    const nId = hId === "a" ? "b" : "a", out = ds[hId], inn = ds[nId];
    if (host.mod.djMind && host.mod.djMind.transitioning) return { ok: false, why: "a transition is running" };
    if (!inn || !inn.buffer || !inn.analysis) return { ok: false, why: `load a song on deck ${nId.toUpperCase()} first` };
    if (inn.playing) return { ok: false, why: `deck ${nId.toUpperCase()} is already playing` };
    if (!out.stemsReady || !inn.stems) return { ok: false, why: "stems on both decks are needed" };
    const ve = inn._vocalEntry;
    if (!ve || ve.entry == null) return { ok: false, why: `deck ${nId.toUpperCase()}'s vocal entry is not measured yet` };
    const rate = (out._playbackRate && out._playbackRate()) || 1, aEff = out.bpm * rate, gap = Math.abs(aEff / inn.bpm - 1);
    if (!(gap <= HOLD_TEMPO_CAP)) return { ok: false, why: `tempo gap ${(gap * 100).toFixed(1)} % over the 8 % cap` };
    if (gap > 0.02 && !(inn.tempoStems && Math.abs(inn.tempoStems.bpm / aEff - 1) < 0.01)) return { ok: false, why: "B needs key-locked tempo stems at A's tempo" };
    const barA = 240 / out.bpm, barS = barA / rate, pos = out._currentPosition();
    const line = ((out.analysis && out.analysis.phrase_boundaries_8bar) || []).find((p) => (p - pos) / rate >= 2 * barS);
    if (line == null) return { ok: false, why: "no 8-bar line left in this song" };
    const ks = keyScoreOf(out, inn);
    let variant, M;
    if (id === "filter_loop") {
      if (ks != null && ks < 0.8 && !ve.rap) return { ok: false, why: `keys clash (camelot ${ks}): a sung vocal over A's loop needs 0.8` };
      variant = filterLoopFits(out, inn, ve, 0.7, line, true);
      if (!variant.ok) return { ok: false, why: `${variant.gate}: ${variant.reason}` };
      M = variant.M;
    } else {
      const left = (out.buffer.duration - line) / barA;
      M = left >= 26 ? 16 : left >= 17 ? 8 : 0;
      if (!M) return { ok: false, why: `${Math.floor(left)} bars left in A (need 17)` };
      variant = ks != null && ks < 0.8 ? drumsHostFits(out, inn, M, line) : { ok: true, kind: "drums_host", M, why: `${M}-bar drums-only host (keys agree: no waiver needed)` };
      if (!variant.ok) return { ok: false, why: variant.why };
    }
    const t0 = audioAt(out, line);
    const secs = mashupTransition(hId, nId, t0, ve.entry, M, 0.7, `on demand: ${variant.why}`, variant);
    if (!secs) return { ok: false, why: "the loudness / audibility gates refused it (see the console log)" };
    const xf = ui.el("crossfader"), dir = hId === "a" ? 1 : -1, steps = 16;
    for (const m of mergeFader(M)) {
      for (let i = 1; i <= steps; i++) {
        const v = (m.from + (m.to - m.from) * (i / steps)) * dir;
        setTimeout(() => { if (xf) { xf.value = String(v); ui.fire(xf, "input", true); } }, Math.max(0, (t0 + (m.bar + (m.bars * i) / steps) * barS - audioCtx.currentTime) * 1000));
      }
    }
    setTimeout(() => { if (out.playing) out.stopNow(); }, Math.max(0, (t0 + secs - audioCtx.currentTime) * 1000 + 300));
    return { ok: true, why: variant.why };
  }
  // S1 side of A: loop A's window (stemSlices on drums / bass / other, vocal out), the EQ sweep on the audio
  // clock (ramps, never steps) and the echo; all undone two bars after the line (A is silent by then).
  function armFilterLoop(out, t0, barS, plan) {
    const endT = t0 + (plan.M + 2) * barS;
    timers[out.id].push(setTimeout(() => {
      if (!out.playing) return;
      if (!out.stemMix({ vocals: 0 }, t0 - 0.03, 0.02)) return void console.info("artist move filter_loop: A's stem mode refused, plain mashup");
      const booked = plan.sliceStems.filter((n) => out.stemSlices(n, plan.slices, t0));
      if (booked.length < plan.sliceStems.length) {
        booked.forEach((n) => out.releaseSlices(n));
        console.info(`artist move filter_loop: A's loop refused (${booked.length}/${plan.sliceStems.length} stems), plain mashup`);
      }
      const f = plan.filter, sweepEnd = t0 + f.toBar * barS;
      for (const [node, db] of [[out.lowFilter, f.lowDb], [out.midFilter, f.midDb]]) {
        if (!node || !node.gain) continue;
        node.gain.cancelScheduledValues(t0);
        node.gain.setValueAtTime(0, t0);
        node.gain.linearRampToValueAtTime(db, sweepEnd);
        node.gain.setValueAtTime(db, endT);
        node.gain.linearRampToValueAtTime(0, endT + 0.1);
      }
    }, Math.max(0, (t0 - audioCtx.currentTime) * 1000 - 300)));
    const fx = host.mod.fxUnits && host.mod.fxUnits[out.id];
    if (!fx) { plan.fallbacks.push("delay:no_fx_unit"); return; }
    timers[out.id].push(setTimeout(() => {
      if (!out.playing) return;
      fx.setType("echo");
      const dl = fx.effect && (fx.effect.nodes || []).find((n) => n && n.delayTime);
      if (dl) dl.delayTime.value = plan.delay.time;
      fx.setWet(plan.delay.wet);
      fx.setActive(true);
    }, Math.max(0, (t0 - audioCtx.currentTime) * 1000)));
    timers[out.id].push(setTimeout(() => fx.setActive(false), Math.max(0, (endT - audioCtx.currentTime) * 1000)));
  }

  // variant: null (the mashup), {kind: "drums_host", ...} (S9) or a filterLoopPlan (S1).
  function mashupTransition(outId, innId, t0, bEntry, M, vox, why, variant) {
    const out = host.decks[outId], inn = host.decks[innId];
    if (!out || !inn) return 0;
    if (!out.stemsReady && out.rearmStems) out.rearmStems("mashup");
    if (!out.stemsReady || !inn.stems) return 0;
    const barS = 240 / (out.bpm || 128) / ((out._playbackRate && out._playbackRate()) || 1);
    const bRate = (out.bpm * out._playbackRate()) / inn.bpm;          // B follows A's tempo (key-locked stems)
    const vk = variant && variant.kind;
    const plan = vk === "filter_loop" ? variant : vk === "drums_host" ? drumsHostPlan(M, vox) : mashupTransitionPlan(M, vox);
    const barB = 240 / inn.bpm;
    // B's voice is its intro stem (A's is out on bar 0: one singer). Floor check
    // (full band + audible band) against the fader the autopilot runs (mergeFader).
    const pA = out._positionAt ? out._positionAt(t0) : out._currentPosition();
    const lv = gates(plan, mergeFader(M), plan.total, planEnergies(out, pA, inn, bEntry, plan.total + 1), 1 / barS);
    if (!lv.ok) {
      console.info(`mashup ${outId}->${innId} refused: ${lv.reason}`);
      if (vk) console.info(`artist move ${vk} skipped: gates: ${lv.reason}`);
      return 0;
    }
    cancel(outId); cancel(innId);
    if (vk === "filter_loop") armFilterLoop(out, t0, barS, plan);
    for (const e of plan.events) {
      const d = e.deck === "out" ? out : inn, at = t0 + e.bar * barS;
      if (vk === "filter_loop" && e.deck === "out" && e.bar === 0) continue;   // armFilterLoop mutes A's vocal before the line
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
    host.bus.emit("ai-cue", { at: t0 + M * barS, kind: "drop", deck: innId, bar: barS,
      why: "B's beat takes over after the mashup" });
    if (vk) {
      const label = vk === "filter_loop" ? "FILTER LOOP" : "DRUMS HOST";
      const w = `${why || plan.why || variant.why}${plan.fallbacks && plan.fallbacks.length ? ` (constants: ${plan.fallbacks.join(", ")})` : ""}`;
      console.info(`artist move ${vk}: ${w}`);
      host.log.step("artist_move", { deck: outId, decision: `artist_move: ${vk}`, why: w,
        result: { params: plan.params || { mashup_bars: M, waiver: variant.waiver && variant.waiver.measured }, fallbacks: plan.fallbacks || [], t0, t1: t0 + plan.total * barS } });
      note(outId, `MASHUP → ${innId.toUpperCase()} · ${label} · ${M} bars`, w);
    } else {
      note(outId, `MASHUP → ${innId.toUpperCase()} · ${M} bars`, why ||
        `A's instrumental under B's vocal, hold vox, A's beat drops out, B's beat takes over on the line, 8-bar crossfade`);
    }
    return plan.total * barS;
  }

  function hookDrop(d, item, why) {
    if (!d || !d.stemsReady || !item) return false;
    cancel(d.id);
    const bar = 240 / (d.bpm || 128), rate = (d._playbackRate && d._playbackRate()) || 1;
    for (const e of hookDropEvents(item, bar)) book(d, audioAt(d, e.t), e.stems, e.ramp / rate);
    host.bus.emit("ai-cue", { at: audioAt(d, item.drop_at), kind: "drop",
      deck: d.id, bar: bar / rate, why: `the beat slams back after "${item.text}"` });
    note(d.id, `HOOK DROP · "${item.text}"`, why || "beat out under the emotional line, then the drop");
    return true;
  }

  // AI ACTIONS wiring (owner contract): aiActions.register when it exists, else the "ai-action" {id} bus event;
  // one run per click (a second path within 1 s is the same click). The run is
  // logged to the step log and its result (or the named refusal) goes to the status line.
  const ARTIST_LABEL = { filter_loop: "FILTER LOOP", drums_host: "DRUMS HOST" };
  const onDemand = {}, lastRun = {};
  for (const id of Object.keys(ARTIST_LABEL)) {
    onDemand[id] = () => {
      const now = host.clock.now();
      if (lastRun[id] && now - lastRun[id].at < 1000) return lastRun[id].r;
      let r;
      try { r = artistRunNow(id); } catch (e) { r = { ok: false, why: String((e && e.message) || e) }; }
      lastRun[id] = { at: now, r };
      host.log.step("artist_run_now", { deck: "", decision: r.ok ? `artist_move: ${id}` : "refused", why: r.why });
      ui.status(`${ARTIST_LABEL[id]}: ${r.ok ? r.why : `refused - ${r.why}`}`);
      if (!r.ok) note("", `✗ ${ARTIST_LABEL[id]}`, r.why);
      return r;
    };
  }
  setTimeout(() => { const aa = host.mod.aiActions; if (aa && typeof aa.register === "function") for (const id of Object.keys(onDemand)) aa.register(id, onDemand[id]); }, 0);
  host.bus.on("ai-action", (e) => { const id = e && e.detail && e.detail.id; if (onDemand[id]) onDemand[id](); });

  const api = { core, mergeTransition, hookDrop, breakdown, handoff, instrumental, reset, audioAt, vocalShare, stemBlend, remix, REMIX_LABEL, mashupBreak, stemBridge, mashupTransition,
                     filterLoopFits, drumsHostFits, artistRunNow, runNow: artistRunNow, onDemand,
                     bridgeFader, stemEnergyBars, remixEnergy, eqIntro };
  host.mod.stemMoves = api;       // the rail UI below reads deck state; other engine code reaches this module through host.mod

  // ------------------------------------------------------ stem rail UI --
  // Per deck, under the loop rail: separation status + one toggle per stem.
  // Lit = playing, dim = muted, gold flash = the AI moved it.
  const LABEL = { drums: "DRUMS", bass: "BASS", vocals: "VOX", other: "SYNTH" };
  let userClick = false;
  function buildRail(id) {
    const panel = ui.el(`deck-${id}`);
    const loopRail = panel && panel.querySelector(".rail");
    if (!loopRail || panel.querySelector(".stem-rail")) return;
    const rail = ui.create("div");
    rail.className = "rail stem-rail";
    rail.innerHTML = `<span class="hud-label">STEMS</span><span class="stem-status" id="stem-status-${id}">—</span>` +
      ["drums", "bass", "vocals", "other"].map((n) =>
        `<button class="hw-btn stem-btn" data-deck="${id}" data-stem="${n}" disabled title="Mute / unmute the ${LABEL[n].toLowerCase()} stem">` +
        `<span class="stem-name">${LABEL[n]}</span><canvas class="stem-scope" data-deck="${id}" data-stem="${n}" width="120" height="28"></canvas></button>`).join("") +
      `<button class="hw-btn stem-btn stem-all" data-deck="${id}" data-stem="all" disabled title="Back to the full mix">MIX</button>`;
    loopRail.after(rail);
    rail.addEventListener("click", (e) => {
      const b = e.target.closest(".stem-btn");
      const d = host.decks && host.decks[id];
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
    const d = host.decks && host.decks[id];
    const st = ui.el(`stem-status-${id}`);
    if (!d || !st) return;
    const ready = d.stemsReady, has = !!d.stems;
    st.textContent = d._meterGain ? "RIFF · KEY-LOCKED" : ready ? (d.stemState ? "LIVE STEMS" : "READY · full mix") : has ? "LOADED" : d.buffer ? "SEPARATING…" : "—";
    st.className = `stem-status${ready || d._meterGain ? " stem-ready" : d.buffer && !has ? " stem-wait" : ""}`;
    ui.queryAll(`.stem-btn[data-deck="${id}"]`).forEach((b) => {
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
  ui.queryAll(".stem-scope").forEach((c) => {
    const key = `${c.dataset.deck}:${c.dataset.stem}`;
    hist[key] = new Float32Array(COLS);
    scopes.push({ c, ctx: c.getContext("2d"), key, deck: c.dataset.deck, stem: c.dataset.stem });
  });
  function accent(deck) {
    return ui.cssVar(`deck-${deck}`, "--accent") || "#0f6";
  }
  const colour = { a: null, b: null };
  let last = 0, frame = 0;
  function drawScopes(t) {
    host.clock.raf(drawScopes);
    if (t - last < 33) return;                   // ~30 fps
    last = t;
    if (!colour.a) { colour.a = accent("a"); colour.b = accent("b"); }
    frame++;
    for (const s of scopes) {
      const d = host.decks && host.decks[s.deck];
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
  host.clock.raf(drawScopes);
  host.bus.on("ai-activity", (e) => {
    const d = e.detail || {};
    if (d.kind === "stems") paint(d.deck, !userClick);
  });
  setInterval(() => { paint("a"); paint("b"); }, 1000);
  return api;
  }
  if (root.Engine) root.Engine.mount("stemMoves", create);
})(typeof window !== "undefined" ? window : globalThis);
