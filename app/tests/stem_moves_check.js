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
  const dd = stemBlendPlan("double", 8, true, true);
  assert.deepStrictEqual(dd.find((e) => e.deck === "out" && e.bar === 0).stems, { drums: 0, bass: 0, vocals: 0, other: 1 });
  console.log("stem blend ok");
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
    // B has no drums until its entry line (its own grid)
    assert.ok(p.events.filter((e) => e.deck === "in" && e.stems && e.t < p.bEntry).every((e) => !e.stems.drums));
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
