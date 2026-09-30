// Node check for the auto sampler core (app/ui/static/auto-sampler.js). Run by test_keylock.py.
const assert = require("assert");
const { planHits, allowed, MIN_GAP_BARS, MAX_PER_SONG } = require("../../ui/static/auto-sampler.js");
const bar = 2;
const drop = planHits("drop", 100, bar);
assert.strictEqual(drop[0].pad, "sweep"); assert.strictEqual(drop[0].at, 98);          // riser 1 bar before
const roll = drop.filter((h) => h.pad === "snare");
assert.strictEqual(roll.length, 4);
assert.ok(roll.every((h, i) => i === 0 || h.gain > roll[i - 1].gain));                  // rising
assert.ok(roll.every((h) => h.at >= 99 && h.at < 100));                                  // last half bar
assert.ok(drop.some((h) => h.pad === "clap" && h.at === 100));                           // on the line
assert.ok(drop.every((h) => h.pad !== "kick" && h.pad !== "tom"));                        // never a second kick/low end
assert.deepStrictEqual(planHits("line", 50, bar).map((h) => h.pad), ["open"]);
assert.ok(!planHits("transition", 50, bar).some((h) => h.pad === "snare"));             // the beat layer fills transitions
// rationing
assert.ok(allowed([], 10, bar, "s"));
assert.ok(!allowed([{ at: 10, song: "s" }], 10 + (MIN_GAP_BARS - 1) * bar, bar, "s"));
assert.ok(allowed([{ at: 10, song: "s" }], 10 + MIN_GAP_BARS * bar, bar, "s"));
const full = Array.from({ length: MAX_PER_SONG }, (_, i) => ({ at: i * 100, song: "s" }));
assert.ok(!allowed(full, 10000, bar, "s")); assert.ok(allowed(full, 10000, bar, "t"));
console.log("auto sampler ok");
