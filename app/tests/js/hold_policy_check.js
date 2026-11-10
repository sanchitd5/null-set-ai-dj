// Node check for G17 hold policy (hold-policy.js) and the long holdPlan (stem-moves.js). Mirrors dj-core test/hold_policy_test.dart.
const assert = require("assert");
const HP = require("../../ui/static/hold-policy.js");
const sm = require("../../ui/static/stem-moves.js");

const STEMS = ["drums", "bass", "vocals", "other"];
const env = (n, f) => Object.fromEntries(STEMS.map((s) => [s, Array.from({ length: n }, (_, i) => f(s, i))]));

// hold lengths: 90 s target, BPM scaled, 16-bar grid, 32..64, capped by room
assert.strictEqual(HP.mergeHoldBars(240 / 128, 200), 48);
assert.strictEqual(HP.mergeHoldBars(240 / 174, 200), 64);
assert.strictEqual(HP.mergeHoldBars(240 / 90, 200), 32);
assert.strictEqual(HP.mergeHoldBars(240 / 60, 200), 32);
assert.strictEqual(HP.mergeHoldBars(1.38, 40), 32);
assert.strictEqual(HP.mergeHoldBars(1.38, 20), 16);
assert.strictEqual(HP.mergeHoldBars(1.38, 12), 8);
assert.strictEqual(HP.mergeHoldBars(1.38, 4), 0);
assert.strictEqual(HP.mergeHoldBars(1.875, 200, HP.settings({ merge_hold_bars: 128 })), 128);
assert.strictEqual(HP.settings({ merge_hold_bars: 999 }).mergeHoldBars, 128);
assert.strictEqual(HP.settings({ merge_hold_bars: 3 }).mergeHoldBars, 16);
assert.strictEqual(HP.settings("junk").stemMergeShare, 0.6);
assert.strictEqual(HP.inSongHoldBars(64), 32);
assert.strictEqual(HP.inSongHoldBars(12), 0);
assert.strictEqual(HP.inSongHoldBars(64, HP.settings({ insong_holds: false })), 0);

// recipe mix
assert.strictEqual(HP.enforceRecipeMix("Long Blend", ["Stem Merge", "Long Blend"], true).recipe, "Stem Merge");
assert.strictEqual(HP.enforceRecipeMix("Long Blend", ["Stem Merge", "Stem Merge", "Stem Merge"], true).recipe, "Long Blend");
assert.strictEqual(HP.enforceRecipeMix("Echo Out", ["Stem Merge", "Stem Merge", "Stem Merge", "Stem Merge"], false).recipe, "Echo Out");
assert.strictEqual(HP.enforceRecipeMix("Echo Out", ["Stem Merge", "Stem Merge", "Stem Merge", "Stem Merge", "Echo Out"], false).recipe, "Basic Blend");
{
  const picks = ["Echo Out", "Bass Swap", "Long Blend", "Backspin (Spinback)", "Stem Merge", "Echo Out"], hist = [];
  for (let i = 0; i < 30; i++) hist.push(HP.enforceRecipeMix(picks[i % 6], hist, i % 5 !== 4).recipe);
  const t = HP.holdTelemetry(hist.map((recipe) => ({ recipe })));
  assert.ok(t.merge_share >= 0.6 && t.accent_share <= 0.2, JSON.stringify(t));
  for (let i = 1; i < hist.length; i++) assert.ok(!(HP.isAccent(hist[i]) && HP.isAccent(hist[i - 1])));
}

// level matching: the gain fix. A quiet stem refused the hold ("master -x dB"); matched, it books.
{
  const base = (o) => ({ gap: 0.01, keyScore: 0.9, roomBars: 100, barS: 2, aT: 0, bT: 0, barA: 2, barB: 2, aVox: [], bVox: [], ...o });
  const eA = env(80, (s) => (s === "drums" || s === "bass" ? 0.45 : 0.3)), eB = env(80, (s) => (s === "vocals" || s === "other" ? 0.02 : 0.45));
  const off = sm.holdPlan(base({ eA, eB, levelMatch: false })), on = sm.holdPlan(base({ eA, eB }));
  assert.strictEqual(off.ok, false);
  assert.strictEqual(off.gate, "level");
  assert.ok(/^(master -\d+\.\d dB|no stem audible)/.test(off.tried[0].reason), off.tried[0].reason);
  assert.ok(on.ok, on.reason);
  assert.strictEqual(on.M, 32);
  assert.ok(on.trim);
  const dead = env(80, () => 0);
  assert.strictEqual(sm.holdPlan(base({ eA: dead, eB: dead })).ok, false);                 // the master floor still refuses silence
  const e = env(80, () => 0.3);
  assert.strictEqual(sm.holdPlan(base({ eA: e, eB: e, barS: 240 / 128 })).M, 48);          // BPM scaled long hold
  assert.strictEqual(sm.holdPlan(base({ eA: e, eB: e, barS: 240 / 174 })).M, 64);
  const long = env(200, () => 0.3);
  assert.strictEqual(sm.holdPlan(base({ eA: long, eB: long, roomBars: 200, holdMin: 128, holdMax: 128 })).M, 128);
}

// safety + telemetry
assert.strictEqual(HP.holdEndReason({ vocalClash: true, energyNow: 1, energyAtStart: 1 }), "vocal clash");
assert.strictEqual(HP.holdEndReason({ vocalClash: false, energyNow: 0.4, energyAtStart: 1 }), "energy drop");
assert.strictEqual(HP.holdEndReason({ vocalClash: false, energyNow: 0.9, energyAtStart: 1 }), null);
assert.strictEqual(HP.holdEndReason({ userAction: true, energyNow: 1, energyAtStart: 1 }), "user action");
assert.strictEqual(HP.holdEndBar(17), 24);
const tl = HP.holdTelemetry([{ recipe: "Stem Merge", holdBars: 30, holdS: 56.2 }, { recipe: "Stem Merge", holdBars: 62, holdS: 85.3, early: "vocal clash" }, { recipe: "Echo Out" }, { recipe: "Long Blend" }]);
assert.deepStrictEqual([tl.merge_share, tl.hold_count, tl.hold_seconds, tl.early_ends], [0.5, 2, 141.5, 1]);

// running hold ends early on the next phrase line (bars from the line, polled like the console's 250 ms interval)
{
  const run = (o) => {
    const g = HP.holdGuard(2, 32, 8);
    for (let b = 0; b < 32; b += 0.25) {
      const r = g.check({ bar: b, vocalClash: o.clash != null && b >= o.clash, energy: o.drop != null && b >= o.drop ? 0.3 : 1, userAction: o.touch != null && b >= o.touch });
      if (r != null && b >= r) return [r, g.reason];
    }
    return null;
  };
  assert.strictEqual(run({}), null);
  assert.deepStrictEqual(run({ clash: 10 }), [16, "vocal clash"]);
  assert.deepStrictEqual(run({ drop: 17 }), [24, "energy drop"]);
  assert.deepStrictEqual(run({ touch: 8 }), [8, "user action"]);
  const g = HP.holdGuard(2, 32, 8);
  assert.strictEqual(g.check({ bar: 25, vocalClash: true, energy: 1 }), null);               // the next line is the hold's own end
}

// "no stem audible at 0.06": a dropout bin of a held stem no longer refuses, a fully dead window still does
{
  const base = (o) => ({ gap: 0.01, keyScore: 0.9, roomBars: 100, barS: 2, aT: 0, bT: 0, barA: 2, barB: 2, aVox: [], bVox: [], ...o });
  const eA = env(80, (s, i) => (s === "drums" || s === "bass" ? (i < 1 ? 0 : 0.45) : 0.3)), eB = env(80, (s) => (s === "vocals" || s === "other" ? 0.3 : 0.45));
  const off = sm.holdPlan(base({ eA, eB, levelMatch: false })), on = sm.holdPlan(base({ eA, eB }));
  assert.strictEqual(off.ok, false);
  assert.strictEqual(off.tried[0].reason, "no stem audible at 0.06");
  assert.ok(on.ok, on.reason);
  const dead = env(80, () => 0);
  assert.strictEqual(sm.holdPlan(base({ eA: dead, eB: dead })).ok, false);
}
console.log("hold_policy_check ok");
