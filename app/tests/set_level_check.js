// Node check for the set-level rules (research/notes/artist-signature-techniques.md S16-S22), console side.
const assert = require("assert");
const ap = require("../ui/static/autopilot.js");

let n = 0;
// ---- S22 breakdown ownership: the exit never starts inside A's breakdown -------------------------------
{
  const bpm = 128, bar = 240 / bpm, phraseS = 8 * bar;          // 15 s phrases
  const times = [], curve = [];
  for (let t = 0; t < 300; t += 0.5) {
    times.push(t);
    curve.push(t < 10 || t >= 280 ? 0.1 : (t >= 100 && t < 140) || (t >= 220 && t < 260) ? 0.2 : t >= 140 && t < 200 ? 1.0 : 0.6);
  }
  const spans = ap.breakdownSpans(times, curve, bar);
  assert.deepStrictEqual(spans.map(([a, b]) => [Math.round(a), Math.round(b)]), [[100, 140], [220, 260]]); n++;
  const o = { phraseS, bpm, trackEnd: 290, energyTimes: times, energyCurve: curve };
  // outside a breakdown: untouched
  assert.deepStrictEqual(ap.exitBreakdownPush({ ...o, t: 90, lo: 0 }), { t: 90, moved: 0, clear: true }); n++;
  // inside: back to the phrase before it (the section before the breakdown)
  let r = ap.exitBreakdownPush({ ...o, t: 120, lo: 0 });
  assert.strictEqual(r.moved, -2); assert.strictEqual(r.t, 90); assert.ok(r.clear); n++;
  // `lo` forbids going back (too close to now): on to the drop
  r = ap.exitBreakdownPush({ ...o, t: 120, lo: 110 });
  assert.strictEqual(r.moved, 2); assert.strictEqual(r.t, 150); n++;
  // neither fits (lo and trackEnd both inside): unchanged, clear false
  r = ap.exitBreakdownPush({ ...o, t: 230, lo: 225, trackEnd: 255 });
  assert.deepStrictEqual(r, { t: 230, moved: 0, clear: false }); n++;
  // no analysis: untouched
  assert.deepStrictEqual(ap.exitBreakdownPush({ t: 120, phraseS, bpm }), { t: 120, moved: 0, clear: true }); n++;
  // flat / short curves: no breakdowns
  assert.deepStrictEqual(ap.breakdownSpans([0, 1, 2, 3, 4], [0.5, 0.5, 0.5, 0.5, 0.5], bar), []); n++;
}
// ---- let the song finish: a FULL window only when the set holds its energy, never in QUICK -----------
{
  const base = { mode: "hybrid", score: 80, energy: 8, rem: 240, finish: true };
  const w = ap.playWindowFor(base);
  assert.strictEqual(w.label, "FULL·finish"); assert.strictEqual(w.min, 190); assert.strictEqual(w.max, 234); n++;
  assert.notStrictEqual(ap.playWindowFor({ ...base, mode: "quick" }).label, "FULL·finish"); n++;          // user asked quick
  assert.strictEqual(ap.playWindowFor({ ...base, score: 40 }).label, "QUICK·bail"); n++;                  // weak match bails
  assert.notStrictEqual(ap.playWindowFor({ ...base, rem: ap.FINISH_MAX_S + 1 }).label, "FULL·finish"); n++; // too long left
  assert.notStrictEqual(ap.playWindowFor({ ...base, rem: 80 }).label, "FULL·finish"); n++;                // almost over anyway
  assert.notStrictEqual(ap.playWindowFor({ ...base, finish: false }).label, "FULL·finish"); n++;
  assert.strictEqual(ap.playWindowFor({ ...base, steering: "move" }).label, "BRIDGE"); n++;               // steering wins
  assert.strictEqual(ap.playWindowFor({ ...base, famous: true, rem: 300 }).label, "FULL·famous"); n++;
  // the gate itself (full coverage in rule_vectors)
  assert.strictEqual(ap.energyAtTarget([7, 8, 8], 6).ok, true); n++;
  assert.strictEqual(ap.energyAtTarget([5, 6, 8], 6).ok, false); n++;     // still moving
  assert.strictEqual(ap.energyAtTarget([8, 8, 8], 3).ok, false); n++;     // warm-up
  assert.strictEqual(ap.energyAtTarget([3, 3, 3], 2).ok, true); n++;      // a low set never builds
}
console.log(`set level OK (${n})`);
