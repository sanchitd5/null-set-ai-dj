// The bass band during a crossfade: the incoming deck's side leads, its share is the front.
const assert = require("assert");
const { fadeStep } = require("../ui/static/visuals.js");

let s = null;
s = fadeStep(s, 0);          // A alone
assert.strictEqual(s.inc, null);
s = fadeStep(s, 0.25);       // B rising: B comes in (from the right)
assert.strictEqual(s.inc, "b"); assert.strictEqual(s.front, 0.25);
s = fadeStep(s, 0.75);
assert.strictEqual(s.inc, "b"); assert.strictEqual(s.front, 0.75);
s = fadeStep(s, 1);          // B alone: fade over
assert.strictEqual(s.inc, null);

s = fadeStep(null, 1);       // B alone
s = fadeStep(s, 0.8);        // A rising: A comes in (from the left)
assert.strictEqual(s.inc, "a"); assert.ok(Math.abs(s.front - 0.2) < 1e-9);
s = fadeStep(s, 0.8);        // held: direction kept
assert.strictEqual(s.inc, "a");
s = fadeStep(s, null);       // silence
assert.strictEqual(s.inc, null);
console.log("bass fade ok");
