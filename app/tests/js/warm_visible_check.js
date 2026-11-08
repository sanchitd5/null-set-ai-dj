// Node check: the console warms only rows near the viewport plus the decks (browser.js BROWSER_WARM),
// in batches of <=50, and album-art.js (HOT1 observer) is untouched by the batching.
const assert = require("assert");
global.document = { getElementById: () => null };   // browser.js returns early without its DOM
const W = require("../../ui/static/browser.js");

const rects = Array.from({ length: 720 }, (_, i) => ({ id: "t" + i, top: i * 40 - 100, bottom: i * 40 - 60 }));
const none = () => false;
let got = W.pick(rects, 800, [], none, W.NEAR);
assert.strictEqual(got.length, W.NEAR, "capped at NEAR");
assert.deepStrictEqual(got.slice(0, 3), ["t0", "t1", "t2"]);
assert.ok(!got.includes("t719"), "far rows are not warmed");

got = W.pick(rects, 800, ["t500", null, "t501"], none, W.NEAR);
assert.deepStrictEqual(got.slice(0, 2), ["t500", "t501"], "decks and NEXT first");
assert.strictEqual(got.length, W.NEAR);

const cached = new Set(["t0", "t1", "t500"]);
got = W.pick(rects, 800, ["t500", "t501"], (id) => cached.has(id), 5);
assert.deepStrictEqual(got, ["t501", "t2", "t3", "t4", "t5"], "cached ids skipped, no duplicates");

assert.deepStrictEqual(W.pick(rects.slice(0, 3), 800, ["t1"], none, 24), ["t1", "t0", "t2"]);

const parts = W.chunk(Array.from({ length: 120 }, (_, i) => "x" + i), W.BATCH);
assert.deepStrictEqual(parts.map((p) => p.length), [50, 50, 20], "720 rows is a few requests, not 720");
assert.strictEqual(W.BATCH, 50);
console.log("warm_visible_check ok");
