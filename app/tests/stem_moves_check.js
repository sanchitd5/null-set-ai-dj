// Node check for the pure stem-move core (app/ui/static/stem-moves.js). Run by test_live_ear.py.
const assert = require("assert");
const { BREAKDOWN, breakdownFits, handoffFits, vocalShare } = require("../ui/static/stem-moves.js");

// schedules: sorted, end back on the full mix on a phrase line, vocal never touched
for (const [bars, plan] of Object.entries(BREAKDOWN)) {
  const at = plan.map((x) => x[0]);
  assert.deepStrictEqual(at, [...at].sort((a, b) => a - b), `breakdown ${bars} sorted`);
  assert.strictEqual(plan[plan.length - 1][1], null, "ends on the full mix");
  assert.strictEqual(plan[plan.length - 1][0] % 8, 0, "drop on a phrase line");
  assert.ok(plan.every(([, t]) => !t || !("vocals" in t)), "the voice carries the breakdown");
}
const bar = 2, voc = [[0, 400]];
const base = { bar, pos: 100, duration: 400, exitAt: null, barsOnTrack: 40, vocals: voc, famous: true, used: false };
assert.strictEqual(breakdownFits(base), 40);
assert.strictEqual(breakdownFits({ ...base, famous: false }), null);
assert.strictEqual(breakdownFits({ ...base, used: true }), null);
assert.strictEqual(breakdownFits({ ...base, barsOnTrack: 10 }), null);               // too early
assert.strictEqual(breakdownFits({ ...base, exitAt: 100 + 50 * bar }), 24);          // room for 24 only
assert.strictEqual(breakdownFits({ ...base, exitAt: 100 + 30 * bar }), null);
assert.strictEqual(breakdownFits({ ...base, vocals: [[0, 20]] }), null);             // no voice to carry it
assert.ok(Math.abs(vocalShare([[0, 5], [8, 10]], 0, 10) - 0.7) < 1e-9);
assert.ok(handoffFits({ outStems: true, inStems: true, keyScore: 0.9, outVocal: 0.6 }));
assert.ok(!handoffFits({ outStems: true, inStems: false, keyScore: 0.9, outVocal: 0.6 }));
assert.ok(!handoffFits({ outStems: true, inStems: true, keyScore: 0.6, outVocal: 0.6 }));   // keys clash
assert.ok(!handoffFits({ outStems: true, inStems: true, keyScore: 1, outVocal: 0.1 }));     // A isn't singing
console.log("stem moves core ok");

// ---- stem blend: one owner per layer -----------------------------------------
{
  const { stemBlendPlan } = require("../ui/static/stem-moves.js");
  for (const [bars, aS, bS] of [[16, true, true], [8, false, true], [16, false, false]]) {
    const ev = stemBlendPlan("blend", bars, aS, bS);
    const swap = bars / 2;
    // B's kick + bass arrive exactly on the swap line; A's leave just before it
    const bIn = ev.find((e) => e.deck === "in" && e.stems && e.stems.drums === 1);
    assert.strictEqual(bIn.bar, swap); assert.strictEqual(bIn.stems.bass, 1);
    const aOut = ev.find((e) => e.deck === "out" && e.stems && e.stems.drums === 0);
    assert.ok(aOut.bar < swap && aOut.bar + aOut.ramp <= swap + 1e-9);
    // before the swap B has no kick, no bass, no voice
    assert.ok(ev.filter((e) => e.deck === "in" && e.bar < swap && e.stems).every((e) => !e.stems.drums && !e.stems.bass && !e.stems.vocals));
    // one singer: B's voice only after A's has started fading out
    const aVox = ev.find((e) => e.deck === "out" && e.stems && e.stems.vocals === 0);
    const bVox = ev.find((e) => e.deck === "in" && e.stems && e.stems.vocals === 1);
    assert.ok(bVox.bar >= aVox.bar);
    assert.strictEqual(ev[ev.length - 1].stems, null);
  }
  // double drop: B's drop (drums + bass) IS its entry, A keeps its tops and
  // voice; B's voice waits until A's leaves (one singer), never a full mix at bar 0
  const dd = stemBlendPlan("double", 8, true, true);
  assert.deepStrictEqual(dd.find((e) => e.deck === "out" && e.bar === 0).stems, { drums: 0, bass: 0, vocals: 1, other: 1 });
  assert.deepStrictEqual(dd.find((e) => e.deck === "in" && e.bar === 0).stems, { drums: 1, bass: 1, vocals: 0, other: 0 });
  console.log("stem blend ok");
}

// ---- intro stem + loudness floor (Ask 1) ------------------------------------
{
  const sm = require("../ui/static/stem-moves.js");
  const { pickIntro, stemBlendPlan, levelCheck, fitStemBlend, gainsAt, introBars, AUDIBLE_GAIN, LEVEL_FLOOR_DB, DIP_ALLOWED, breakdownEvents } = sm;
  const { stemBlendFader } = require("../ui/static/autopilot.js");
  // keys agree: B's synths, or its drums when that is where its energy is
  assert.strictEqual(pickIntro({ keyClash: false, energy: { drums: 0.2, bass: 0.3, vocals: 0, other: 0.15 } }), "other");
  assert.strictEqual(pickIntro({ keyClash: false, energy: { drums: 0.3, bass: 0.3, vocals: 0, other: 0.02 } }), "drums");
  assert.strictEqual(pickIntro({ keyClash: false, energy: null }), "other");
  // keys clash: percussion only, whatever the energy says
  assert.strictEqual(pickIntro({ keyClash: true, energy: { drums: 0.01, bass: 0.5, vocals: 0.5, other: 0.5 } }), "drums");
  // B's voice only when it has nothing else there and A is not singing
  assert.strictEqual(pickIntro({ keyClash: false, aSings: false, bSings: true, energy: { drums: 0, bass: 0, vocals: 0.2, other: 0 } }), "vocals");
  assert.strictEqual(pickIntro({ keyClash: false, aSings: true, bSings: true, energy: { drums: 0, bass: 0, vocals: 0.2, other: 0 } }), "other");
  // never bass, in any case
  for (const e of [{ drums: 0, bass: 1, vocals: 0, other: 0 }, { drums: 0, bass: 1, vocals: 1, other: 0 }]) {
    for (const keyClash of [true, false]) for (const aSings of [true, false]) {
      assert.notStrictEqual(pickIntro({ keyClash, aSings, bSings: true, energy: e }), "bass");
    }
  }

  const FULL = { drums: 1, bass: 1, vocals: 1, other: 1 };
  for (const kind of ["blend", "filter", "loop", "bass"]) {
    for (const L of [8, 16, 32]) {
      for (const keyClash of [false, true]) {
        const fit = fitStemBlend(kind, L, { aSings: true, bSings: true, keyClash, fader: stemBlendFader(kind, L, 1), dir: 1 });
        assert.ok(!fit.refused, `${kind} ${L} clash=${keyClash}: ${fit.check.reason}`);
        assert.ok(fit.check.minDb >= -LEVEL_FLOOR_DB, `${kind} ${L} floor ${fit.check.minDb}`);
        const ev = fit.events, fader = stemBlendFader(kind, L, 1), P = introBars(L);
        assert.strictEqual(fit.intro, keyClash ? "drums" : "other");
        // the fader's move toward B starts only after the intro phrase
        const cross = fader.find((m) => m.to > 0);
        assert.ok(cross.bar >= P, `${kind} ${L}: crossfade at ${cross.bar} < intro ${P}`);
        // B's ONE intro stem is audible (through the parked fader) for >= 1 phrase before it
        const x = (0 + 1) / 2, fi = Math.sin((x * Math.PI) / 2);
        const heard = (t) => { const g = gainsAt(ev, "in", t); return Object.keys(FULL).filter((n) => fi * g[n] >= AUDIBLE_GAIN); };
        for (let t = 1; t < cross.bar; t += 0.25) {
          const h = heard(t);
          if (t < L / 2 - 1e-9) assert.deepStrictEqual(h, [fit.intro], `${kind} ${L} bar ${t}: ${h}`);
          assert.ok(!(t < L / 2) || !h.includes("bass"), "never B's bass before the swap");
        }
        assert.ok(cross.bar - 1 >= P - 1, "intro alone for a phrase");
      }
    }
  }
  // double drop too: no silence, no dip
  const dd2 = fitStemBlend("double", 8, { aSings: true, bSings: true, fader: stemBlendFader("double", 8, 1), dir: 1 });
  assert.ok(!dd2.refused, dd2.check.reason);

  // the zero-audible invariant is enforced, not assumed: a plan that mutes
  // everything on both decks is refused
  const hole = [
    { bar: 0, deck: "in", stems: { drums: 0, bass: 0, vocals: 0, other: 0 }, ramp: 0 },
    { bar: 2, deck: "out", stems: { drums: 0, bass: 0, vocals: 0, other: 0 }, ramp: 0.25 },
    { bar: 4, deck: "in", stems: null, ramp: 0 },
  ];
  const hc = levelCheck({ events: hole, fader: [{ bar: 0, from: -1, to: 0, bars: 1 }], dir: 1, span: 8 });
  assert.ok(!hc.ok && /no stem audible/.test(hc.reason), hc.reason);
  // ... and B's first stem booked after A's last one leaves
  const gap = [
    { bar: 0, deck: "in", stems: { drums: 0, bass: 0, vocals: 0, other: 0 }, ramp: 0 },
    { bar: 1, deck: "out", stems: { drums: 0, bass: 0, vocals: 0, other: 0 }, ramp: 0.5 },
    { bar: 3, deck: "in", stems: { other: 1 }, ramp: 1 },
  ];
  assert.ok(!levelCheck({ events: gap, fader: [{ bar: 0, from: -1, to: 0, bars: 1 }], dir: 1, span: 8 }).ok);

  // a thin B (quiet pad, no beat at its entry) + A stripped: the plan fixes
  // itself (louder intro / A's synths held) or is refused, never a near-silent master
  const eOut = { drums: 0.3, bass: 0.3, vocals: 0.2, other: 0.1 };
  const eIn = { drums: 0.05, bass: 0.05, vocals: 0.01, other: 0.03 };
  const thin = fitStemBlend("blend", 16, { aSings: false, bSings: false, keyClash: false, eOut, eIn, introEnergy: eIn, fader: stemBlendFader("blend", 16, 1), dir: 1 });
  assert.ok(thin.refused || thin.check.minDb >= -LEVEL_FLOOR_DB);
  assert.ok(thin.refused, `a pad at -20 dB against A's full mix can't hold the floor: ${thin.check.minDb}`);
  const okish = fitStemBlend("blend", 16, { aSings: false, bSings: false, keyClash: false, eOut, eIn: { drums: 0.25, bass: 0.25, vocals: 0.1, other: 0.06 },
    fader: stemBlendFader("blend", 16, 1), dir: 1 });
  assert.ok(!okish.refused, okish.check.reason);
  assert.ok(okish.check.minDb >= -LEVEL_FLOOR_DB);

  // deliberate dips: allowed only when tagged, and the check still reports them
  // (a beat-heavy song: measured energy mostly in drums + bass)
  const heavy = { drums: 0.5, bass: 0.5, vocals: 0.1, other: 0.1 };
  const bd = breakdownEvents(24);
  const plain = levelCheck({ events: bd, span: 24, eOut: heavy });
  assert.ok(!plain.ok, "a breakdown does dip");
  const tagged = levelCheck({ events: bd, span: 24, eOut: heavy, dipAllowed: DIP_ALLOWED.breakdown });
  assert.ok(tagged.ok && tagged.dipAllowed === DIP_ALLOWED.breakdown && tagged.reason);
  // subdrop (sub out on purpose) on one deck: tagged -> allowed, untagged -> not
  const sub = [{ bar: 0, deck: "out", stems: { bass: 0, drums: 0, other: 0.3 }, ramp: 0 }, { bar: 4, deck: "out", stems: null, ramp: 0 }];
  assert.ok(!levelCheck({ events: sub, span: 4, eOut: heavy }).ok);
  assert.ok(levelCheck({ events: sub, span: 4, eOut: heavy, dipAllowed: DIP_ALLOWED.subdrop }).ok);
  // a plain blend never gets a dip pass even if tagged plans exist
  const blend = fitStemBlend("blend", 16, { aSings: true, bSings: true, fader: stemBlendFader("blend", 16, 1), dir: 1 });
  assert.strictEqual(blend.check.dipAllowed, null);
  console.log("intro stem + loudness floor ok");
}

// ---- stem bridge + mashup: floor and intro ----------------------------------
{
  const { stemBridgePlan, mashupTransitionPlan, levelCheck, LEVEL_FLOOR_DB } = require("../ui/static/stem-moves.js");
  for (const clash of [false, true]) for (const sings of [false, true]) {
    const barA = 240 / 128, barB = 240 / 174;
    const p = stemBridgePlan(barA, barB, clash, sings);
    const lv = levelCheck({ events: p.events, fader: p.fader, dir: 1, span: p.total, inStart: p.bStart, step: barA / 16, win: barA / 4 });
    assert.ok(lv.ok, `bridge clash=${clash} sings=${sings}: ${lv.reason}`);
    // the crossfade proper starts after B's intro stem has had >= 2 of its bars alone
    const cross = p.fader.find((m) => m.to > 0);
    assert.ok(cross.t >= p.bStart + 2 * barB - 1e-9);
    assert.ok(!p.events.some((e) => e.deck === "in" && e.stems && e.stems.bass && e.t < p.bStart + barB));
  }
  const mp = mashupTransitionPlan(16, 0.6);
  const ml = levelCheck({ events: mp.events, fader: [{ bar: 0, from: -1, to: 0, bars: 2 }, { bar: 16, from: 0, to: 1, bars: 8 }], dir: 1, span: mp.total });
  assert.ok(ml.ok && ml.minDb >= -LEVEL_FLOOR_DB, ml.reason);
  console.log("bridge + mashup floor ok");
}

// ---- remix on the go --------------------------------------------------------
{
  const { remixEvents, remixPick } = require("../ui/static/stem-moves.js");
  for (const k of ["vocal_hold", "acapella", "drum_break", "bass_out", "synth_hold"]) {
    for (const len of [16, 32]) {
      const ev = remixEvents(k, len);
      assert.ok(ev.length, k);
      assert.ok(ev.every((e) => e.bar >= len / 2 && e.bar <= len), `${k} stays in the section's second half`);
      assert.ok(ev.every((e) => !e.hold || e.hold.untilBar === len), `${k} hold ends on the line`);
      const last = ev[ev.length - 1];
      assert.ok(last.stems === null || last.hold, `${k} resolves on the line`);
    }
  }
  const base = { vocal: 0.8, used: [], count: 0, barsOnTrack: 40, barsLeft: 80, lastAtBar: null, atBar: 40 };
  assert.strictEqual(remixPick(base), "vocal_hold");
  assert.strictEqual(remixPick({ ...base, used: ["vocal_hold"] }), "acapella");
  assert.strictEqual(remixPick({ ...base, vocal: 0 }), "drum_break");
  assert.strictEqual(remixPick({ ...base, barsOnTrack: 16 }), null);            // let the song establish itself
  assert.strictEqual(remixPick({ ...base, barsLeft: 30 }), null);               // not near the exit
  assert.strictEqual(remixPick({ ...base, lastAtBar: 20 }), null);              // 32 bars apart
  assert.strictEqual(remixPick({ ...base, count: 3 }), null);                   // 3 per song
  console.log("stem remix ok");
}

// keys clash: one tonal owner, B's synths only after A's are half-faded
{
  const { stemBlendPlan } = require("../ui/static/stem-moves.js");
  const ev = stemBlendPlan("blend", 32, false, false, true);
  const bOtherUp = ev.filter((e) => e.deck === "in" && e.stems && e.stems.other > 0);
  assert.ok(bOtherUp.every((e) => e.bar >= 24), `B synths wait for A's to fade: ${bOtherUp.map((e) => e.bar)}`);
  const aOther = ev.find((e) => e.deck === "out" && e.stems && e.stems.other === 0);
  assert.ok(aOther.bar + aOther.ramp <= 24 + 1e-9);
  console.log("key clash blend ok");
}

// ---- stem bridge: any tempo, no two beats ever overlap ------------------------
{
  const { stemBridgePlan } = require("../ui/static/stem-moves.js");
  for (const [barA, barB, clash, sings] of [[1.95, 1.38, false, true], [1.38, 1.95, true, false], [2.0, 2.0, false, false]]) {
    const p = stemBridgePlan(barA, barB, clash, sings);
    const aDrumsOut = p.events.find((e) => e.deck === "out" && e.stems && e.stems.drums === 0);
    const aBassOut = p.events.find((e) => e.deck === "out" && e.stems && e.stems.bass === 0);
    const bBeat = p.events.find((e) => e.deck === "in" && e.stems === null);
    // A's beat is gone (drums + bass faded) before B even starts
    assert.ok(aDrumsOut.t + aDrumsOut.ramp <= p.bStart + 1e-9 && aBassOut.t + aBassOut.ramp <= p.bStart + 1e-9);
    // keys agree: B has no drums until its entry line (its own grid). Keys clash:
    // B's drums ARE its intro stem (no key), and never before A's beat is gone.
    const bDrumsEarly = p.events.filter((e) => e.deck === "in" && e.stems && e.t < p.bEntry && e.stems.drums);
    if (clash) assert.ok(p.intro === "drums" && bDrumsEarly.every((e) => e.t >= p.bStart));
    else assert.strictEqual(bDrumsEarly.length, 0);
    assert.ok(p.events.filter((e) => e.deck === "in" && e.stems && e.t < p.bEntry).every((e) => !e.stems.bass || e.t >= p.bStart + barB - 1e-9));
    assert.ok(Math.abs(bBeat.t - p.bEntry) < 1e-9);
    // A's tones are gone by B's entry
    const aOut = p.events.find((e) => e.deck === "out" && e.stems && e.stems.vocals === 0);
    assert.ok(aOut.t + aOut.ramp <= p.bEntry + 1e-9);
    if (clash) assert.ok(p.events.filter((e) => e.deck === "in" && e.stems && e.stems.other > 0).every((e) => e.t >= aOut.t));
    // no gap: B's tones rise exactly while A's fade (same start, same ramp)
    const bIn = p.events.find((e) => e.deck === "in" && e.stems && e.stems.bass === 1);
    assert.ok(Math.abs(bIn.t - aOut.t) < 1e-9 && Math.abs(bIn.ramp - aOut.ramp) < 1e-9);
    if (sings) assert.ok(p.events.some((e) => e.hold));
  }
  console.log("stem bridge ok");
}

// ---- mashup transition ---------------------------------------------------------
{
  const { mashupTransitionPlan } = require("../ui/static/stem-moves.js");
  for (const M of [16, 32]) {
    const p = mashupTransitionPlan(M, 0.6);
    assert.strictEqual(p.total, M + 8);
    // one singer: A's voice leaves exactly as B's arrives
    assert.ok(p.events.find((e) => e.bar === 0 && e.deck === "out").stems.vocals === 0);
    // B's backing only from the swap line
    assert.ok(p.events.filter((e) => e.deck === "in" && e.stems && e.bar < M).every((e) => !e.stems.drums && !e.stems.bass && !e.stems.other));
    // A's beat leaves 2 bars before B's lands, never both
    const aOut = p.events.find((e) => e.deck === "out" && e.stems && e.stems.drums === 0);
    assert.ok(aOut.bar === M - 2);
    assert.strictEqual(p.events.find((e) => e.deck === "in" && e.stems && e.stems.drums === 1).bar, M);
    const holds = p.events.filter((e) => e.hold);
    assert.strictEqual(holds.length, M === 32 ? 1 : 0);
  }
  console.log("mashup transition ok");
}

// hook drop: beat out under the emotional line, slam back on the drop
{
  const sm = require("../ui/static/stem-moves.js");
  const bar = 240 / 104;                                   // Sabrina: ~2.31 s
  const item = { cut_at: 88.26, drop_at: 93.51, text: "I am a party" };
  const ev = sm.hookDropEvents(item, bar);
  assert.deepStrictEqual(ev[0].stems, { drums: 0, bass: 0, other: sm.HOOK_OTHER });
  assert.ok(Math.abs(ev[0].t + ev[0].ramp - item.cut_at) < 1e-9);          // gone exactly on the cut
  assert.deepStrictEqual(ev[1], { t: item.drop_at, stems: { drums: 1, bass: 1, other: 1 }, ramp: 0.005 });
  assert.strictEqual(sm.hookDropDue([item], 88.26 - 1.5 * bar, bar, 180), item);          // 1.5 bars ahead: book it
  assert.strictEqual(sm.hookDropDue([item], 88.26 - 0.5 * bar, bar, 180), null);          // too late to book cleanly
  assert.strictEqual(sm.hookDropDue([item], 88.26 - 3 * bar, bar, 180), null);            // not yet
  assert.strictEqual(sm.hookDropDue([item], 88.26 - 1.5 * bar, bar, 93.51 + 2 * bar), null);   // the exit is too close
  assert.strictEqual(sm.hookDropDue([{ ...item, drop_at: item.cut_at + 30 }], 88.26 - 1.5 * bar, bar, null), null);  // hold too long
  assert.strictEqual(sm.hookDropDue([], 10, bar, null), null);
  console.log("hook drop ok");
}
