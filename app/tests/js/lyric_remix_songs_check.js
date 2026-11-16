// Node check for the lyric remixes panel helpers (app/ui/static/lyric-remix-songs.js): filter, formatting, row markup and the booking steps of
// a tap (no rendered track: the real song(s) load and the remix is requested from the live engine). Exits non-zero on the first failed assertion.
"use strict";
const assert = require("assert");
const path = require("path");
const L = require(path.join(__dirname, "..", "..", "ui", "static", "lyric-remix-songs.js"));

const within = { plan_id: "p1", kind: "within", a: "s1", b: null, title: "T1 - lyric remix", text: "alpha / beta", score: 0.91 };
const pair = { plan_id: "p2", kind: "pair", a: "s1", b: "s2", title: "T1 x T2 - lyric remix", text: "<b>x</b>", score: 0.5 };
const songs = [within, pair];
assert.strictEqual(L.filter(songs, "").length, 2);
assert.deepStrictEqual(L.filter(songs, " BETA ").map((s) => s.plan_id), ["p1"]);
assert.deepStrictEqual(L.filter(songs, "t2").map((s) => s.plan_id), ["p2"]);
assert.strictEqual(L.planId(within), "p1");
assert.strictEqual(L.fmtScore(0.914), "91"); assert.strictEqual(L.fmtScore(NaN), "--"); assert.strictEqual(L.fmtScore(2), "100");
assert.strictEqual(L.kindLabel(within), "LIVE IN THE SONG"); assert.strictEqual(L.kindLabel(pair), "PAIR · LIVE BLEND");
const r0 = L.row(within); assert(r0.includes('data-lrs="play"') && r0.includes('data-lrs="queue"') && r0.includes("BOOK NEXT"));
assert(!/RENDER|render/.test(r0), "nothing is rendered any more");
assert(L.row(pair).includes("&lt;b&gt;x&lt;/b&gt;"), "text escaped");

// steps: nothing plays -> load on deck a and play; the request is always last and carries the plan
assert.deepStrictEqual(L.steps(within, "play", { a: null, b: null, playing: null }),
  { steps: [{ load: "a", id: "s1", play: true }, { request: { plan: "p1", a: "s1", b: null } }] });
assert.deepStrictEqual(L.steps(pair, "play", { a: null, b: null, playing: null }),
  { steps: [{ load: "a", id: "s1", play: true }, { load: "b", id: "s2" }, { request: { plan: "p2", a: "s1", b: "s2" } }] });
// another song plays on b: PLAY NOW replaces that deck, B goes on the other
assert.deepStrictEqual(L.steps(pair, "play", { a: "z", b: "y", playing: "b" }).steps.slice(0, 2), [{ load: "b", id: "s1", play: true }, { load: "a", id: "s2" }]);
// the song is already on air: only B (pair) is loaded, the remix is requested on top
assert.deepStrictEqual(L.steps(within, "play", { a: "s1", b: null, playing: "a" }), { steps: [{ request: { plan: "p1", a: "s1", b: null } }] });
assert.deepStrictEqual(L.steps(pair, "queue", { a: "s1", b: null, playing: "a" }), { steps: [{ load: "b", id: "s2" }, { request: { plan: "p2", a: "s1", b: "s2" } }] });
// book next: a within song goes on the idle deck; a pair needs A on air
assert.deepStrictEqual(L.steps(within, "queue", { a: "z", b: null, playing: "a" }), { steps: [{ load: "b", id: "s1" }, { request: { plan: "p1", a: "s1", b: null } }] });
assert(L.steps(pair, "queue", { a: "z", b: null, playing: "a" }).error);
console.log("lyric_remix_songs_check: ok");
