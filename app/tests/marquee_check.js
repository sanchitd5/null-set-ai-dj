// Node check for app/ui/static/marquee.js timing (run by test_keylock.py).
const assert = require("assert");
const { plan, SPEED_PX_S, MIN_DUR_S } = require("../ui/static/marquee.js");
assert.strictEqual(plan(0), null);
assert.strictEqual(plan(2), null);                        // fits: no marquee
assert.strictEqual(plan(20).dur, MIN_DUR_S);              // short overflow: calm minimum
const p = plan(400);
assert.strictEqual(p.dur, (2 * 400 / SPEED_PX_S) / 0.4);  // scrolling is 40 % of the cycle at SPEED_PX_S
assert.ok(plan(800).dur > p.dur);
console.log("marquee ok");
