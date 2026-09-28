// Node check for the visuals cue mapping (app/ui/static/visuals.js). Run by test_keylock.py.
const assert = require("assert");
const { effectFor, env, FLASH_GAP_S } = require("../ui/static/visuals.js");

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
console.log("visuals ok");
