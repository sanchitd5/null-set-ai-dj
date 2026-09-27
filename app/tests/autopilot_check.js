// Node check for the pure autopilot core (app/ui/static/autopilot.js).
// Run by test_autopilot_core.py; exits non-zero on the first failed assertion.
const assert = require("assert");
const { stemBlendBars, stemBlendFader, phraseWaitS } = require("../ui/static/autopilot.js");

const bar128 = 240 / 128;   // 1.875 s
const bar90 = 240 / 90;     // 2.667 s

// mashup kinds: >= 30 s, 16-bar multiples (swap at L/2 on an 8-bar line)
for (const kind of ["blend", "filter", "loop"]) {
  const b = stemBlendBars(kind, bar128, 20, 1);
  assert.strictEqual(b, 16, kind);
  assert.ok(b * bar128 >= 30);
  assert.strictEqual((b / 2) % 8, 0);
}
assert.strictEqual(stemBlendBars("blend", bar128, 100, 1), 32);          // room -> 32
assert.strictEqual(stemBlendBars("blend", 30 / 17, 100, 1), 32);         // 30 s = 17 bars -> up to 32
assert.ok(stemBlendBars("blend", bar90, 20, 1) * bar90 >= 30);
// capped by A's remaining room, never past it
assert.strictEqual(stemBlendBars("blend", bar128, 12, 1), 8);
assert.strictEqual(stemBlendBars("blend", 30 / 17, 30, 1), 16);          // want 32, room 29 -> 16
// scale applied once (old code applied it twice: 16 * 0.5 bars of half bars)
assert.strictEqual(stemBlendBars("blend", bar128, 100, 0.5), 16);
assert.strictEqual(stemBlendBars("blend", bar128, 20, 0.5), 8);
// bass / double stay short
assert.strictEqual(stemBlendBars("bass", bar128, 100, 1), 16);
assert.strictEqual(stemBlendBars("double", bar128, 100, 1), 8);
assert.strictEqual(stemBlendBars("double", bar128, 100, 0.5), 4);

// fader: body to centre, then an 8-bar crossfade that ends on B's side
let f = stemBlendFader("blend", 32, 1);
assert.deepStrictEqual(f, [{ bar: 0, from: -1, to: 0, bars: 16 }, { bar: 24, from: 0, to: 1, bars: 8 }]);
f = stemBlendFader("blend", 16, -1);
assert.deepStrictEqual(f, [{ bar: 0, from: 1, to: 0, bars: 8 }, { bar: 8, from: 0, to: -1, bars: 8 }]);
f = stemBlendFader("bass", 16, 1);
assert.strictEqual(f[1].bar + f[1].bars, 16);
f = stemBlendFader("double", 8, 1);
assert.deepStrictEqual(f, [{ bar: 0, from: -1, to: 1, bars: 8 }]);
// every move is continuous with the previous one (no jumps)
for (const [k, n] of [["blend", 32], ["blend", 16], ["bass", 16], ["blend", 8]]) {
  const m = stemBlendFader(k, n, 1);
  for (let i = 1; i < m.length; i++) assert.strictEqual(m[i].from, m[i - 1].to);
  assert.ok(m[m.length - 1].bar + m[m.length - 1].bars <= n);
}

// phrase wait: next 8-bar line of the entry grid
const ph = 8 * bar128;
assert.strictEqual(phraseWaitS(10, 10, ph), 0);
assert.strictEqual(phraseWaitS(10 + ph, 10, ph), 0);
assert.ok(Math.abs(phraseWaitS(11, 10, ph) - (ph - 1)) < 1e-9);
assert.strictEqual(phraseWaitS(8, 10, ph), 2);

console.log("autopilot core ok");
