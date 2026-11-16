// Node check for the lyric remix songs panel helpers (app/ui/static/lyric-remix-songs.js): filter, formatting, badges, row markup,
// deck choice, beat grid mapping. Exits non-zero on the first failed assertion.
"use strict";
const assert = require("assert");
const path = require("path");
const L = require(path.join(__dirname, "..", "..", "ui", "static", "lyric-remix-songs.js"));

const songs = [
  { id: "lr-p1", plan_id: "p1", title: "T1 - lyric remix", text: "alpha / beta", score: 0.91, state: "none" },
  { id: "lr-p2", plan_id: "p2", title: "T2 x T3 - lyric remix", text: "gamma", score: 0.5, state: "rendering", progress: 0.4, stage: "mix" },
  { id: "lr-p3", plan_id: "p3", title: "T4 - lyric remix", text: "<b>x</b>", score: 0.2, state: "error", error: "boom" },
];
assert.strictEqual(L.filter(songs, "").length, 3);
assert.deepStrictEqual(L.filter(songs, " BETA ").map((s) => s.id), ["lr-p1"]);
assert.deepStrictEqual(L.filter(songs, "t3").map((s) => s.id), ["lr-p2"]);
assert.strictEqual(L.planId({ id: "lr-zz" }), "zz");
assert.strictEqual(L.fmtScore(0.914), "91"); assert.strictEqual(L.fmtScore(NaN), "--"); assert.strictEqual(L.fmtScore(2), "100");
assert.strictEqual(L.fmtTime(125), "2:05"); assert.strictEqual(L.fmtTime(null), "--:--");
assert.deepStrictEqual(L.badge(songs[1]), { label: "RENDERING 40% · mix", pct: 40, cls: "rendering" });
assert.strictEqual(L.badge(songs[0]).label, "NOT RENDERED"); assert.strictEqual(L.badge({ state: "ready" }).cls, "ready");
assert.strictEqual(L.audioUrl(songs[0]), "/api/audio/remix/p1?q=phone");
assert.strictEqual(L.deckFor("play"), "a"); assert.strictEqual(L.deckFor("queue"), "b");
assert.strictEqual(L.gridFrom({ beat_times: [] }), null); assert.strictEqual(L.gridFrom(null), null);
assert.deepStrictEqual(L.gridFrom({ bpm: 120, duration: 9, beat_times: [0.5], downbeat_times: [0.5] }), { bpm: 120, duration: 9, beat_times: [0.5], downbeat_times: [0.5] });
const r0 = L.row(songs[0]); assert(r0.includes('data-lrs="play"') && r0.includes('data-lrs="queue"') && !r0.includes("disabled"));
const r1 = L.row(songs[1]); assert(r1.includes("disabled") && r1.includes('style="width:40%"'), "progress bar, actions disabled while rendering");
const r2 = L.row(songs[2]); assert(r2.includes('data-lrs="retry"') && r2.includes("boom") && r2.includes("&lt;b&gt;x&lt;/b&gt;"), "error row: message + retry; text escaped");
console.log("lyric_remix_songs_check: ok");
