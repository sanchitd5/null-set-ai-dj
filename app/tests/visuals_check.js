// Node check for the visuals cue mapping (app/ui/static/visuals.js). Run by test_keylock.py.
const assert = require("assert");
const { effectFor, env, FLASH_GAP_S, aiDriving, energyPeaks, nextPeakIdx, stepPeaks,
        peakAllowed, DROP_PEAK_GAP_S, SEEK_JUMP_S } = require("../ui/static/visuals.js");

assert.deepStrictEqual(effectFor({ kind: "drop", deck: "b" }), { type: "drop", deck: "b", dur: 1.6 });
assert.strictEqual(effectFor({ kind: "transition", deck: "a", bar: 1.875 }).type, "sweep");
assert.strictEqual(effectFor({ kind: "transition", bar: 100 }).dur, 3);          // capped
assert.strictEqual(effectFor({ kind: "line", deck: "x" }).deck, null);         // unknown deck -> AI colour
assert.strictEqual(effectFor({ kind: "nope" }), null);
assert.strictEqual(effectFor(null), null);
const r = effectFor({ kind: "drop", deck: "a" }, true);
assert.ok(r.still && r.dur >= 1.2);                                              // reduced motion: still glow

assert.strictEqual(env(-0.1, 1), 0);
assert.strictEqual(env(1, 1), 0);
assert.strictEqual(env(0.06, 1), 1);
assert.ok(env(0.5, 1) > 0 && env(0.5, 1) < 1);
assert.ok(FLASH_GAP_S >= 1 / 3);                                                 // <= 3 flashes/s
assert.deepStrictEqual(effectFor({ kind: "peak", deck: "a" }), { type: "peak", deck: "a", dur: 2.4 });

// ---- AI gate: only a live autopilot counts
assert.strictEqual(aiDriving({ active: true }), true);
assert.strictEqual(aiDriving({ active: false }), false);
assert.strictEqual(aiDriving({ get active() { return true; } }), true);          // autopilotState getter
assert.strictEqual(aiDriving({ active: "yes" }), false);
assert.strictEqual(aiDriving(undefined), false);                                 // autopilot.js not loaded
assert.strictEqual(aiDriving(null), false);

// ---- energy high points. 120 BPM: bar = 2 s, 32 bars = 64 s, 8-bar rise window = 16 s
const BPM = 120;
const song = (n, fn) => { const c = [], t = []; for (let i = 0; i < n; i++) { t.push(i); c.push(fn(i)); } return [c, t]; };
{ // two drops 100 s apart, low elsewhere: both found
  const [c, t] = song(300, (i) => (i === 60 ? 1 : i === 160 ? 0.95 : i === 59 || i === 159 ? 0.5 : 0.2));
  assert.deepStrictEqual(energyPeaks(c, t, BPM), [60, 160]);
}
{ // spacing: a weaker high point 30 s after a stronger one (< 64 s) is dropped
  const [c, t] = song(300, (i) => (i === 60 ? 1 : i === 90 ? 0.95 : 0.2));
  assert.deepStrictEqual(energyPeaks(c, t, BPM), [60]);
  // the stronger one wins even when it comes second
  const [c2, t2] = song(300, (i) => (i === 60 ? 0.95 : i === 90 ? 1 : 0.2));
  assert.deepStrictEqual(energyPeaks(c2, t2, BPM), [90]);
}
{ // top percentile: a local max in the middle of the energy range is not a high point
  const [c, t] = song(200, (i) => (i < 100 ? 0.2 : 0.9) + (i === 50 ? 0.3 : 0));
  assert.ok(!energyPeaks(c, t, BPM).includes(50));
}
{ // local max: a sample still climbing is not the peak, the top of the climb is
  const [c, t] = song(300, (i) => (i >= 95 && i <= 100 ? 0.5 + (i - 95) * 0.1 : 0.2));
  assert.deepStrictEqual(energyPeaks(c, t, BPM), [100]);
}
{ // plateau counts once, at its first sample
  const [c, t] = song(300, (i) => (i >= 100 && i < 110 ? 1 : 0.2));
  assert.deepStrictEqual(energyPeaks(c, t, BPM), [100]);
}
{ // rising edge: a louder wiggle inside a loud section (no climb in the last 8 bars) is not
  // a high point, even though it is stronger; the climb into the section is kept
  const [c, t] = song(400, (i) => (i === 130 ? 0.99 : i >= 100 && i < 140 ? 0.95 : 0.2));
  assert.deepStrictEqual(energyPeaks(c, t, BPM), [100]);
}
{ // bad input never throws
  assert.deepStrictEqual(energyPeaks(null, null, BPM), []);
  assert.deepStrictEqual(energyPeaks([1, 2], [0, 1], BPM), []);
  assert.deepStrictEqual(energyPeaks([0.5, 0.5, 0.5, 0.5], [0, 1, 2, 3], BPM), []);          // flat
  assert.deepStrictEqual(energyPeaks([0, 1, 0, 0], [3, 2, 1, 0], BPM), []);                  // unsorted times
  const [c, t] = song(300, (i) => (i === 60 ? 1 : 0.2));
  c[10] = NaN;
  assert.deepStrictEqual(energyPeaks(c, t, 0), [60]);                                        // bpm fallback
}

// ---- pointer: fires once per crossing, never on seek / loop / load
assert.strictEqual(nextPeakIdx([10, 20, 30], 5), 0);
assert.strictEqual(nextPeakIdx([10, 20, 30], 20), 2);
assert.strictEqual(nextPeakIdx([10, 20, 30], 99), 3);
{
  const tr = { peaks: [10, 20], idx: 0, prev: NaN };
  assert.strictEqual(stepPeaks(tr, 9.9), null);          // first step only finds the pointer
  assert.strictEqual(stepPeaks(tr, 10.01), 10);          // crossed
  assert.strictEqual(stepPeaks(tr, 10.05), null);        // once
  assert.strictEqual(stepPeaks(tr, 25), null);           // jump over 20 (seek): no fire
  assert.strictEqual(tr.idx, 2);
  assert.strictEqual(stepPeaks(tr, 19.9), null);         // back (loop / seek): pointer re-found
  assert.strictEqual(tr.idx, 1);
  assert.strictEqual(stepPeaks(tr, 20 + SEEK_JUMP_S / 2), 20);
  assert.strictEqual(stepPeaks(null, 1), null);
  assert.strictEqual(stepPeaks({ peaks: [5], idx: 0, prev: 4 }, NaN), null);
}

// ---- drop / peak de-dup: nothing within 4 s of a drop, before or after
assert.ok(DROP_PEAK_GAP_S >= 3 && DROP_PEAK_GAP_S <= 5);
assert.strictEqual(peakAllowed(100, []), true);
assert.strictEqual(peakAllowed(100, undefined), true);
assert.strictEqual(peakAllowed(100, [97]), false);        // drop 3 s ago
assert.strictEqual(peakAllowed(100, [103]), false);       // drop booked 3 s ahead
assert.strictEqual(peakAllowed(100, [95.9]), true);
assert.strictEqual(peakAllowed(100, [104]), true);
console.log("visuals ok");
