// Node check for the AI MOVES / MORE ACTIONS drawers (app/ui/static/toggle-drawer.js core) and the on-demand
// learned gate (learned-moves.js songGate onDemand). Exits non-zero on the first failed assertion.
const assert = require("assert");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "ui", "static");
const td = require(path.join(STATIC, "toggle-drawer.js"));
const lm = require(path.join(STATIC, "learned-moves.js"));

// grouping: data-group wins, known ids default, learned kinds nest under LEARNED, unknown -> OTHER
assert.deepStrictEqual(td.classify("ap-merge-toggle"), { group: "TRANSITIONS", parent: null });
assert.deepStrictEqual(td.classify("ap-remix-toggle"), { group: "IN-SONG MOVES", parent: null });
assert.deepStrictEqual(td.classify("ap-ear-toggle"), { group: "AI", parent: null });
assert.deepStrictEqual(td.classify("ap-learned-vocal_loop"), { group: "LEARNED", parent: "ap-learned-toggle" });
assert.deepStrictEqual(td.classify("ap-learned-toggle"), { group: "LEARNED", parent: null });
assert.deepStrictEqual(td.classify("ap-fx-tails", "fx / tails"), { group: "FX / TAILS", parent: null }, "a new group on the fly");
assert.strictEqual(td.classify("ap-something-new").group, "OTHER");

const items = [
  { id: "ap-x", label: "X", title: "", group: "OTHER" },
  { id: "ap-fx", label: "TAILS", title: "echo tails", group: "FX" },
  { id: "ap-ai-toggle", label: "AI ASSIST", title: "One LLM plan", group: "AI" },
  { id: "ap-merge-toggle", label: "MERGE", title: "Song merge", group: "TRANSITIONS" },
  { id: "ap-learned-toggle", label: "LEARNED", title: "", group: "LEARNED" },
  { id: "ap-learned-vocal_loop", label: "VOX LOOP", title: "vocal line looped", group: "LEARNED", parent: "ap-learned-toggle" },
];
const groups = td.groupItems(items);
assert.deepStrictEqual(groups.map((g) => g.name), ["TRANSITIONS", "LEARNED", "AI", "FX", "OTHER"], "known order, new groups after, OTHER last");

// filter by label and tooltip, every word
assert.deepStrictEqual(td.filterItems(items, "vocal").map((i) => i.id), ["ap-learned-vocal_loop"]);
assert.deepStrictEqual(td.filterItems(items, "song merge").map((i) => i.id), ["ap-merge-toggle"]);
assert.strictEqual(td.filterItems(items, "").length, items.length);
assert.strictEqual(td.filterItems(items, "nothing-like-this").length, 0);

// counts and nested greying
const st = { "ap-merge-toggle": true, "ap-learned-toggle": false, "ap-learned-vocal_loop": true, "ap-ai-toggle": true };
const c = td.counts(groups, st);
assert.deepStrictEqual(c.LEARNED, { on: 1, total: 2 });
assert.deepStrictEqual(c.TRANSITIONS, { on: 1, total: 1 });
assert.strictEqual(td.greyed(items[5], st), true, "LEARNED off greys its kinds");
assert.strictEqual(td.greyed(items[5], Object.assign({}, st, { "ap-learned-toggle": true })), false);

// persistence round trip: only changed ids are saved, restore flips only what differs, junk is ignored
let saved = td.parseSaved(null);
saved = td.withSaved(saved, "ap-merge-toggle", false);
const back = td.parseSaved(JSON.stringify(saved));
assert.deepStrictEqual(back, { "ap-merge-toggle": false });
assert.deepStrictEqual(td.restorePlan(back, { "ap-merge-toggle": true, "ap-ai-toggle": true }), [["ap-merge-toggle", false]]);
assert.deepStrictEqual(td.restorePlan(back, { "ap-merge-toggle": false }), [], "already matching: nothing fires");
assert.deepStrictEqual(td.restorePlan({ "ap-gone": true }, { "ap-merge-toggle": true }), [], "a saved id not on the page is skipped");
assert.deepStrictEqual(td.parseSaved("{not json"), {});
assert.deepStrictEqual(td.parseSaved(JSON.stringify({ a: "yes", b: true })), { b: true });
// a toggle added later: same classify path, lands in its own group
assert(td.groupItems(items.concat([{ id: "ap-show", label: "SHOW", title: "", group: td.classify("ap-show", "show").group }]))
  .some((g) => g.name === "SHOW"));

// MORE ACTIONS split: favourites stay on the bar
assert.deepStrictEqual(td.splitActions(["mix", "strip", "merge_hold", "learned_vocal_chop", "riff", "mashup", "pads"]),
  { bar: ["mix", "merge_hold", "riff", "mashup"], more: ["strip", "learned_vocal_chop", "pads"] });
assert.strictEqual(td.actionGroup("learned_vocal_loop"), "LEARNED");
assert.strictEqual(td.actionGroup("pads", "fred pads"), "FRED PADS");
assert.strictEqual(td.actionGroup("pads"), "OTHER");

// on demand learned: rate gates skipped, safety gates kept
const base = { count: 99, used: ["vocal_loop"], lastAtBar: 10, atBar: 11, barsOnTrack: 0 };
assert(lm.songGate(base, "vocal_loop"), "autopilot path: the cap refuses");
assert.strictEqual(lm.songGate(base, "vocal_loop", true), null, "on demand: cap / cooldown / early skipped");
assert.strictEqual(lm.songGate(Object.assign({}, base, { relaxed: true }), "vocal_loop", true).gate, "relaxed");
assert.strictEqual(lm.songGate(Object.assign({}, base, { mashupActive: true }), "vocal_chop", true).gate, "vocal_layer");
assert.strictEqual(lm.songGate(Object.assign({}, base, { deferring: true }), "vocal_chop", true).gate, "b_deferred");

console.log("toggle drawer check ok");
