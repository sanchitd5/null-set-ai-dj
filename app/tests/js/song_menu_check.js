// Node check for the song menu helpers (app/ui/static/song-menu.js): plain-language states, batching, booking steps. Exits non-zero on failure.
"use strict";
const assert = require("assert");
const path = require("path");
const S = require(path.join(__dirname, "..", "..", "ui", "static", "song-menu.js"));

// states in plain words
assert.deepStrictEqual(S.describe({ state: "available", reason: "ready" }), { cls: "sm-available", chip: "Ready", text: "ready", enabled: true });
const p = S.describe({ state: "preparing", reason: "the stems are being made", eta_s: 11.6 });
assert.strictEqual(p.chip, "Getting ready"); assert.strictEqual(p.text, "the stems are being made (about 12 s)"); assert(p.enabled);
assert.strictEqual(S.describe({ state: "preparing", reason: "the stems are not made yet", eta_s: null }).text, "the stems are not made yet");
const n = S.describe({ state: "not_possible", reason: "the keys do not fit" });
assert.deepStrictEqual([n.chip, n.text, n.enabled], ["Not possible", "the keys do not fit", false]);
assert.strictEqual(S.describe(null).enabled, false);
assert(!/camelot|eta_s|plan_needed|gate/i.test(JSON.stringify([p, n])), "no jargon");

assert.strictEqual(S.etaText(41.6), " (about 42 s)"); assert.strictEqual(S.etaText(null), ""); assert.strictEqual(S.etaText(0), "");
assert.strictEqual(S.describe({ state: "preparing", reason: "making the lyric remix plan", eta_s: 30 }).text, "making the lyric remix plan (about 30 s)");

// batching: only ids without a fresh answer, never the playing song, no duplicates, capped
const cache = new Map([["A|b1", { at: 1000 }], ["A|b2", { at: 100 }]]);
assert.deepStrictEqual(S.missing(["A", "b1", "b2", "b3", "b3", ""], cache, "A", 1000 + S.TTL_MS - 1), ["b2", "b3"]);
assert.deepStrictEqual(S.missing(["b1", "b2", "b3"], cache, "A", 1000 + S.TTL_MS + 1, 2), ["b1", "b2"]);

// booking steps
const ok = { state: "available", reason: "x", plan: { id: "p9" } };
const avail = { state: "available", reason: "x", plan: { kind: "vocal_swap", a: "A", b: "B" } };
assert.deepStrictEqual(S.steps("lyricRemix", "B", ok, { a: "A", b: null, playing: "a" }),
  { steps: [{ load: "b", id: "B" }, { lyric: { plan: "p9", a: "A", b: "B" } }] });
assert.deepStrictEqual(S.steps("lyricRemix", "B", ok, { a: "Z", b: "A", playing: "b" }).steps[0], { load: "a", id: "B" });
assert.deepStrictEqual(S.steps("lyricRemix", "B", ok, { a: "A", b: "B", playing: "a" }), { steps: [{ lyric: { plan: "p9", a: "A", b: "B" } }] }, "already on the idle deck: no reload");
assert.deepStrictEqual(S.steps("vocalSwap", "B", avail, { a: "A", b: null, playing: "a" }),
  { steps: [{ load: "b", id: "B" }, { swap: { a: "A", b: "B", deck: "b" } }] });
assert(S.steps("vocalSwap", "B", avail, { a: null, b: null, playing: null }).error.startsWith("Start a song first"));
assert(S.steps("vocalSwap", "A", avail, { a: "A", b: null, playing: "a" }).error);
assert(/not possible: the keys do not fit/.test(S.steps("vocalSwap", "B", { state: "not_possible", reason: "the keys do not fit" }, { a: "A", b: null, playing: "a" }).error));
assert(S.steps("lyricRemix", "B", { state: "available", plan: null, plan_needed: true }, { a: "A", b: null, playing: "a" }).error, "no plan to book yet");
// preparing is bookable (stems / plan in flight): the engines wait for it
assert(S.steps("vocalSwap", "B", { state: "preparing", reason: "the stems are being made" }, { a: "A", b: null, playing: "a" }).steps);

// popover markup: both actions, escaped, disabled when not possible
const h = S.html("B<1>", { lyricRemix: ok, vocalSwap: { state: "not_possible", reason: "<b>no</b>" } });
assert(h.includes('data-songmenu-act="lyricRemix"') && h.includes('data-songmenu-act="vocalSwap"'));
assert(h.includes("&lt;b&gt;no&lt;/b&gt;") && h.includes('aria-disabled="true"') && h.includes("B&lt;1&gt;"));
assert(S.html("x", null).includes("Not possible"), "missing answer: both shown as not possible");
console.log("song_menu_check ok");
