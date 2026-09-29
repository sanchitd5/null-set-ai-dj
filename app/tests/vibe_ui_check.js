// Node check for the VIBE strip pure core (app/ui/static/vibe-ui.js). Run by test_keylock.py.
const assert = require("assert");
const v = require("../ui/static/vibe-ui.js");

// -- AI state orb: mixing > planning > gate job > hold loop / ear > idle
assert.strictEqual(v.aiState({}).state, "idle");
assert.strictEqual(v.aiState({ gate: { in_flight: "plan", queued: ["suggest", "ear"] } }).state, "thinking");
assert.strictEqual(v.aiState({ gate: { in_flight: "plan", queued: ["suggest", "ear"] } }).queue, 2);
assert.strictEqual(v.aiState({ gate: { in_flight: "suggest" } }).detail, "picking next songs");
assert.strictEqual(v.aiState({ gate: { in_flight: "ear" } }).state, "listening");
assert.strictEqual(v.aiState({ gate: { in_flight: "live" } }).state, "listening");
assert.strictEqual(v.aiState({ holdLoop: true }).state, "listening");
assert.strictEqual(v.aiState({ preplanning: "Song B", gate: { in_flight: "ear" } }).state, "planning");
assert.ok(v.aiState({ preplanning: "Song B" }).detail.includes("Song B"));
assert.strictEqual(v.aiState({ transitioning: true, preplanning: "x", gate: { in_flight: "plan" } }).state, "mixing");
assert.ok(v.aiState({ model: { backend: "mlx", ready: false, detail: "starting" } }).detail.includes("starting"));
assert.strictEqual(v.aiState({ model: { backend: null, ready: false, detail: "starting" } }).detail, "riding the mix");

// -- countdown: deck time over playback rate
assert.strictEqual(v.countdown(100, 90, 1), 10);
assert.strictEqual(v.countdown(100, 90, 2), 5);
assert.strictEqual(v.countdown(null, 90, 1), null);
assert.strictEqual(v.fmtSecs(4.25), "4.3s");
assert.strictEqual(v.fmtSecs(75), "1:15");

// -- merge lanes from a _mergePlan: combo owners up to M, B from the handover line
const plan = { entry: 30, M: 8, aT: 200, heard: true, preplanned: true,
  pick: { combo: { drums: "a", bass: "a", vocals: "b", other: "b" }, label: "A drums + A bass + B vox + B synth", ear: { score: 8 } } };
const lanes = v.mergeLanes(plan);
assert.strictEqual(lanes.total, 16);
assert.strictEqual(lanes.handover, 8);
assert.deepStrictEqual(lanes.lanes.map((l) => l.stem), ["drums", "bass", "vocals", "other"]);
assert.deepStrictEqual(lanes.lanes[0].segs, [{ from: 0, to: 8, owner: "a" }, { from: 8, to: 16, owner: "b" }]);
assert.deepStrictEqual(lanes.lanes[2].segs, [{ from: 0, to: 16, owner: "b" }]);   // B's vocal all the way: one segment
assert.strictEqual(lanes.earScore, 8);
assert.ok(lanes.heard && lanes.preplanned);
assert.strictEqual(v.mergeLanes(null), null);
assert.strictEqual(v.mergeLanes({ M: 0, pick: { combo: {} } }), null);
// playhead: bars since A reached aT, clamped to the move
const bar = 240 / 120;
assert.strictEqual(v.mergePlayhead(plan, 200 - bar, bar).frac, 0);
assert.strictEqual(v.mergePlayhead(plan, 200 + 8 * bar, bar).frac, 0.5);
assert.strictEqual(v.mergePlayhead(plan, 200 + 8 * bar, bar).live, true);
assert.strictEqual(v.mergePlayhead(plan, 200 + 40 * bar, bar).frac, 1);
// phase: merge (0..2 bars), hold (2..M), handover (M..M+8), none outside
assert.strictEqual(v.mergePlayhead(plan, 200 + 1 * bar, bar).phase, "merge");
assert.strictEqual(v.mergePlayhead(plan, 200 + 4 * bar, bar).phase, "hold");
assert.strictEqual(v.mergePlayhead(plan, 200 + 8 * bar, bar).phase, "handover");
assert.strictEqual(v.mergePlayhead(plan, 200 - bar, bar).phase, null);
assert.strictEqual(v.mergePlayhead(plan, 200 + 40 * bar, bar).phase, null);

// -- bar / phrase counter
const dbs = Array.from({ length: 40 }, (_, i) => 1 + i * 2);          // a bar every 2 s from 1 s
const phr = [1, 17, 33, 49, 65];                                        // 8-bar phrases
assert.strictEqual(v.phraseAt(dbs, phr, 0.5), null);                    // before the first downbeat
assert.deepStrictEqual(v.phraseAt(dbs, phr, 1.1), { bar: 1, phrase: 1, beat: 1 });
assert.deepStrictEqual(v.phraseAt(dbs, phr, 4.6), { bar: 2, phrase: 1, beat: 4 });
assert.deepStrictEqual(v.phraseAt(dbs, phr, 17.0), { bar: 1, phrase: 2, beat: 1 });
assert.deepStrictEqual(v.phraseAt(dbs, phr, 31.2), { bar: 8, phrase: 2, beat: 1 });
// phrases that start after the song's first downbeat (a pickup): numbering is still 1-based
assert.deepStrictEqual(v.phraseAt(dbs, [5, 21], 3.0), { bar: 2, phrase: 1, beat: 1 });
assert.deepStrictEqual(v.phraseAt(dbs, [5, 21], 5.0), { bar: 1, phrase: 2, beat: 1 });
// no phrase list: counted from the first downbeat
assert.deepStrictEqual(v.phraseAt(dbs, null, 19.0), { bar: 2, phrase: 2, beat: 1 });

// -- harmony, from the mind's Camelot table
const cam = (a, b) => (a === b ? 1 : 0);
assert.strictEqual(v.harmony("8A", "8A", cam).label, "SAME KEY");
assert.strictEqual(v.harmony("8A", "3B", cam).ok, false);
assert.strictEqual(v.harmony("8A", "3B", cam).label, "CLASH");
assert.strictEqual(v.harmony(null, "8A", cam), null);
assert.strictEqual(v.harmony("8A", "8A", undefined), null);

// -- energy chips and set trail
assert.strictEqual(v.energyChips(6.6), 7);
assert.strictEqual(v.energyChips(14), 10);
assert.strictEqual(v.energyChips(null), 0);
let trail = [];
for (let i = 1; i <= 12; i++) trail = v.trailPush(trail, i);
assert.strictEqual(trail.length, 10);
assert.strictEqual(trail[9], 12);
assert.deepStrictEqual(v.trailPush([1], NaN), [1]);

// -- stem meters: null = full mix
assert.deepStrictEqual(v.stemLevels(null), { drums: 1, bass: 1, vocals: 1, other: 1 });
assert.deepStrictEqual(v.stemLevels({ drums: 0, bass: 1, vocals: 0.5, other: 2, bus: 0 }), { drums: 0, bass: 1, vocals: 0.5, other: 1 });

// -- hook / cue countdowns
const hooks = [{ cut_at: 50, text: "old" }, { cut_at: 120, text: "far" }, { cut_at: 70, text: "next one" }];
assert.strictEqual(v.nextHook(hooks, 60).text, "next one");
assert.strictEqual(v.nextHook(hooks, 60).in, 10);
assert.strictEqual(v.nextHook(hooks, 130), null);
const cues = [{ at: 10, kind: "drop" }, { at: 14, kind: "drop" }, { at: 12, kind: "transition" }];
assert.strictEqual(v.nextCue(cues, 11, "drop").at, 14);
assert.strictEqual(v.nextCue(cues, 11).at, 12);
assert.strictEqual(v.pruneCues(cues, 11.5).length, 2);

// -- event feed: entries, warnings, dedupe, expiry, cap
assert.strictEqual(v.feedEntry("ai-activity", { kind: "stems", deck: "a" }), null);
assert.strictEqual(v.feedEntry("ai-activity", { kind: "decision", action: "ride" }), null);
assert.strictEqual(v.feedEntry("ai-activity", { kind: "stem-move", deck: "a", label: "HOOK DROP", why: "w" }).label, "HOOK DROP");
assert.strictEqual(v.feedEntry("ai-activity", { kind: "decision", deck: "b", action: "pre_clear" }).label, "B PRE CLEAR");
assert.strictEqual(v.feedEntry("seek-refused", { deck: "a", why: "on air" }).warn, true);
const g = v.feedEntry("glitch", { kind: "dropout", where: "A song @ 1:02", move: { label: "Stem Merge" } });
assert.ok(g.warn && g.label.includes("DROPOUT") && g.why.includes("A song @ 1:02") && g.why.includes("Stem Merge"));
assert.strictEqual(v.feedEntry("glitch", { kind: "recover", where: "x" }).warn, false);
assert.strictEqual(v.feedEntry("ear-flush", { reason: "song changed" }).why, "song changed");
assert.strictEqual(v.feedEntry("nope", {}), null);

let feed = [];
const e1 = { kind: "stem-move", label: "HOOK DROP", why: "a" };
feed = v.feedPush(feed, e1, 1000);
feed = v.feedPush(feed, { ...e1, why: "b" }, 2000);
assert.strictEqual(feed.length, 1);                                     // deduped
assert.strictEqual(feed[0].count, 2);
assert.strictEqual(feed[0].why, "b");
feed = v.feedPush(feed, e1, 2000 + v.FEED_DEDUPE_MS + 1);              // past the window: a new row
assert.strictEqual(feed.length, 2);
for (let i = 0; i < 10; i++) feed = v.feedPush(feed, { kind: "k", label: `L${i}` }, 7000);
assert.strictEqual(feed.length, v.FEED_MAX);
assert.strictEqual(feed[0].label, "L9");                                // newest first
assert.strictEqual(v.feedPrune(feed, 7000 + v.FEED_TTL_MS + 1).length, 0);
const w = v.feedPush([], { kind: "glitch-dropout", label: "GLITCH", warn: true }, 0);
assert.strictEqual(v.feedPrune(w, v.FEED_TTL_MS + 1).length, 1);        // warnings stay longer

console.log("vibe ui ok");
