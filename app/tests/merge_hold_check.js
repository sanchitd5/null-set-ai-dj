// Node check for MERGE -> HOLD -> TRANSITION (stem-moves core.holdPlan and friends).
const assert = require("assert");
const sm = require("../ui/static/stem-moves.js");

const STEMS = ["drums", "bass", "vocals", "other"];
const bars = (n, f) => Array.from({ length: n }, (_, i) => f(i));
const env = (n, f = () => 0.3) => Object.fromEntries(STEMS.map((s) => [s, bars(n, (i) => f(s, i))]));
const N = 60, BAR = 2;                                  // 60 bars of 2 s (120 BPM)
const base = () => ({ gap: 0.01, keyScore: 0.9, roomBars: 64, barS: BAR, aT: 0, bT: 0, barA: BAR, barB: BAR,
  eA: env(N), eB: env(N), aVox: [], bVox: [] });

// -- happy path: whole 8-bar phrases, three phases with measured params
{
  const hp = sm.holdPlan(base());
  assert.ok(hp.ok, hp.reason);
  assert.strictEqual(hp.M % 8, 0);
  assert.ok(hp.M >= 8 && hp.M <= 48);
  assert.strictEqual(hp.holdBars, hp.M - 2);
  assert.strictEqual(hp.phases.merge_start.bars, 2);
  assert.strictEqual(hp.phases.hold.from_bar, 2);
  assert.strictEqual(hp.phases.hold.to_bar, hp.M);
  assert.strictEqual(hp.phases.hold.seconds, (hp.M - 2) * BAR);
  assert.strictEqual(hp.phases.handover.at_bar, hp.M);
  assert.strictEqual(hp.phases.handover.bars, 8);
  assert.ok(hp.phases.handover.sub_overlap_bars <= 0.3);
}

// -- hold is bounded by the room in A: hold + 8 handover + 2 spare
{
  const hp = sm.holdPlan({ ...base(), roomBars: 27 });
  assert.ok(hp.ok);
  assert.ok(hp.M + 8 + 2 <= 27, `M ${hp.M}`);
  assert.strictEqual(sm.holdPlan({ ...base(), roomBars: 17 }).gate, "room");
  assert.ok(sm.holdPlan({ ...base(), roomBars: 18 }).ok);          // one phrase is the minimum hold
}

// -- stops early when a stem the combo needs stops playing: both decks lose drums from bar 16
{
  const e = env(N, (s, i) => (s === "drums" && i >= 16 ? 0 : 0.3));
  const hp = sm.holdPlan({ ...base(), eA: e, eB: e });
  assert.ok(hp.ok);
  assert.ok(hp.M <= 16, `M ${hp.M}`);
  assert.ok(hp.tried.some((t) => t.gate === "unclean" && t.stem === "drums"));
  assert.deepStrictEqual(sm.holdUnclean({ drums: "a", bass: "a", vocals: "b", other: "b" }, e, e, 2), null);
  assert.deepStrictEqual(sm.holdUnclean({ drums: "a", bass: "a", vocals: "b", other: "b" }, e, e, 3), { phrase: 2, stem: "drums", deck: "a" });
}

// -- gates: tempo, key, unmeasured stems, no combination that plays
assert.strictEqual(sm.holdPlan({ ...base(), gap: 0.1 }).gate, "tempo");
assert.strictEqual(sm.holdPlan({ ...base(), keyScore: 0.5 }).gate, "key");
assert.ok(sm.holdPlan({ ...base(), keyScore: null }).ok);        // unknown key: not a gate (as everywhere else)
assert.strictEqual(sm.holdPlan({ ...base(), eA: null }).gate, "stems");
{
  const dead = env(N, () => 0);
  const hp = sm.holdPlan({ ...base(), eA: dead, eB: dead });
  assert.strictEqual(hp.ok, false);
  assert.strictEqual(hp.gate, "no_combo");
}

// -- vocal clash: both decks sing through the handover line => the handover moves to a vocal gap
{
  const o = { ...base(), aVox: [[0, 24 * BAR]], bVox: [[0, 60 * BAR]] };     // A stops singing at bar 24
  const hp = sm.holdPlan(o);
  assert.ok(hp.ok);
  assert.ok(hp.M >= 24, `M ${hp.M}`);
  assert.strictEqual(hp.phases.handover.vocal_overlap, 0);
  const all = sm.holdPlan({ ...base(), aVox: [[0, 200]], bVox: [[0, 200]] });    // never a gap: refused, not forced
  assert.strictEqual(all.ok, false);
  assert.strictEqual(all.gate, "vocal_clash");
}

// -- region maths
assert.deepStrictEqual(sm.regionsToBars([[10, 14]], 4, 2), [[3, 5]]);
assert.strictEqual(sm.overlapBars([[0, 10]], [[5, 20]], 0, 8), 3);
assert.strictEqual(sm.overlapBars([[0, 3]], [[5, 20]], 0, 8), 0);

// -- sub-bass single owner across merge_start, hold and handover for every combo and hold length
for (const M of [8, 16, 24, 32, 48]) {
  for (const combo of sm.mergeCombos()) {
    const plan = sm.mergeTransitionPlan(M, combo, false);
    const r = sm.subOwnerCheck(plan, M);
    assert.ok(r.ok, `M ${M} ${sm.mergeLabel(combo)} overlaps the sub for ${r.overlapBars} bars`);
  }
}
{
  // two subs at once is detected
  const bad = { events: [{ bar: 0, deck: "in", start: true, stems: { drums: 1, bass: 1, vocals: 1, other: 1 }, ramp: 0 }], total: 16 };
  assert.strictEqual(sm.subOwnerCheck(bad, 8).ok, false);
}

// -- the plan holdPlan books is exactly the one mergeTransitionPlan plays (same events)
{
  const hp = sm.holdPlan(base());
  const plan = sm.mergeTransitionPlan(hp.M, hp.pick.combo, false);
  assert.strictEqual(plan.total, hp.M + 8);
}
console.log("merge_hold_check ok");
