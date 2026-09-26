// Node check for the pure DJ-mind core (app/ui/static/dj-mind.js).
// Run by test_dj_mind.py; exits non-zero on the first failed assertion.
const assert = require("assert");
const core = require("../ui/static/dj-mind.js");
const { decide, mergeSections, phraseAt, vocalShare, subdropBars, energyNote } = core;

const bar = 240 / 128; // 1.875 s
const base = {
  barSecs: bar, section: "verse", sectionEnergy: 0.5, sectionBarsLeft: 32,
  vocalAhead: 0, barsOnTrack: 40, barsToExit: null, holdRoomBars: 0,
  overlapStyle: null, preClearBars: 8, preCleared: false, instantShown: false,
  holdsUsed: 0, mashupActive: false, subdropThisTrack: false,
  subdropLastTrack: false, secsSinceMove: 1e9, setMode: "hybrid",
};
const s = (o) => Object.assign({}, base, o);

// default: ride
assert.strictEqual(decide(s({})).action, "ride");
// rule 4.1 hold: build running into exit with room
assert.strictEqual(decide(s({ section: "build", barsToExit: 6, holdRoomBars: 16 })).action, "hold");
// quick mode caps holds at 1
assert.strictEqual(decide(s({ section: "build", barsToExit: 6, holdRoomBars: 16, holdsUsed: 1, setMode: "quick" })).action, "preclear");
// rule 1 instant swap near exit
assert.strictEqual(decide(s({ barsToExit: 4, overlapStyle: "instant" })).action, "instant");
// instant never pre-clears
assert.strictEqual(decide(s({ barsToExit: 20, overlapStyle: "instant" })).action, "ride");
// rule 3 pre-clear inside window, once
assert.strictEqual(decide(s({ barsToExit: 10, preClearBars: 8 })).action, "preclear");
assert.strictEqual(decide(s({ barsToExit: 10, preCleared: true })).action, "ride");
// not when exit too close
assert.strictEqual(decide(s({ barsToExit: 2 })).action, "ride");
// rule 2 layer
assert.strictEqual(decide(s({ mashupActive: true })).action, "layer");
// rule 4 subdrop on vocal-forward low-energy verse
const vocal = { vocalAhead: 0.9, sectionEnergy: 0.5 };
assert.strictEqual(decide(s(vocal)).action, "subdrop");
// restraint: cooldown, per-track, consecutive tracks, full energy, near exit, too early
assert.strictEqual(decide(s({ ...vocal, secsSinceMove: 100 })).action, "ride");
assert.strictEqual(decide(s({ ...vocal, subdropThisTrack: true })).action, "ride");
assert.strictEqual(decide(s({ ...vocal, subdropLastTrack: true })).action, "ride");
assert.strictEqual(decide(s({ ...vocal, sectionEnergy: 0.9 })).action, "ride");
assert.strictEqual(decide(s({ ...vocal, barsToExit: 20 })).action, "ride");
assert.strictEqual(decide(s({ ...vocal, barsOnTrack: 4 })).action, "ride");
assert.strictEqual(decide(s({ ...vocal, section: "drop" })).action, "ride");

// subdrop length: 10-20 s, whole bars
assert.strictEqual(subdropBars(bar), 8);        // 15 s
assert.strictEqual(subdropBars(240 / 90), 4);   // 8 bars = 21 s > 20 -> half = 10.7 s
assert.strictEqual(subdropBars(240 / 60), 4);   // 8 bars = 32 s -> half = 16 s
assert.strictEqual(subdropBars(240 / 200), 8);  // 8 bars = 9.6 s: one whole phrase, never past it

// sections: slivers merged, runs < 8 bars dropped
const merged = mergeSections([
  { label: "verse", start: 0, end: 2, energy: 0.4 },
  { label: "verse", start: 2, end: 30, energy: 0.6 },
  { label: "build", start: 30, end: 32, energy: 0.8 },
], bar);
assert.strictEqual(merged.length, 1);
assert.strictEqual(merged[0].end, 30);

// phrase index from downbeats
const downs = Array.from({ length: 40 }, (_, i) => i * bar);
assert.strictEqual(phraseAt(downs, 0.1, bar), 0);
assert.strictEqual(phraseAt(downs, 8 * bar + 0.1, bar), 1);
assert.strictEqual(phraseAt([], 8 * bar * 3 + 0.1, bar), 3);

// vocal share
assert.ok(Math.abs(vocalShare([[0, 5]], 0, 10) - 0.5) < 1e-9);
assert.strictEqual(vocalShare([], 0, 10), 0);

// energy note: dip after sustained peak outranks late callback
assert.strictEqual(energyNote([9, 9], 0.9, false), "dip");
assert.strictEqual(energyNote([5, 6], 0.9, false), "callback");
assert.strictEqual(energyNote([5, 6], 0.9, true), null);
assert.strictEqual(energyNote([9], 0.2, false), null);

// ---- AI plan + remix moves ------------------------------------------------
const { aiVeto, remixBlock, needsHoldLoop, holdLoopAnchor, holdLoopBars, phraseBounds } = core;
const rx = (o) => s(Object.assign({ phraseIdx: 6, lastRemixPhrase: null, remixUsed: [], remixCount: 0,
                                    preDrop: false, skipHitsDrop: { 8: false, 16: false } }, o));

// AI move allowed -> tagged AI, reason kept
let d = decide(rx({ preDrop: true, aiMove: { move: "filter_build", at: 0, reason: "tension" } }));
assert.strictEqual(d.action, "filter_build"); assert.strictEqual(d.source, "AI"); assert.strictEqual(d.why, "tension");
// AI move vetoed (drop lead-in without a drop next) -> rule choice, veto noted
d = decide(rx({ aiMove: { move: "stutter", at: 0 } }));
assert.strictEqual(d.action, "ride"); assert.ok(/vetoed/.test(d.why));
// rules alone: pre-drop phrase -> a drop lead-in, tagged RULE
d = decide(rx({ preDrop: true }));
assert.ok(["stutter", "filter_build", "echo_freeze"].includes(d.action)); assert.strictEqual(d.source, "RULE");
// kinds rotate: used lead-in is skipped
d = decide(rx({ preDrop: true, remixUsed: ["stutter"], remixCount: 1 }));
assert.strictEqual(d.action, "filter_build");
// restraint: never two remix phrases in a row, none in first 16 / last 24 bars, per-song cap
assert.strictEqual(decide(rx({ preDrop: true, lastRemixPhrase: 5 })).action, "ride");
assert.strictEqual(decide(rx({ preDrop: true, barsOnTrack: 8 })).action, "ride");
assert.strictEqual(decide(rx({ preDrop: true, barsToExit: 20 })).action, "ride");
assert.strictEqual(decide(rx({ preDrop: true, remixCount: 3, remixUsed: ["loop_extend", "beat_jump", "echo_freeze"] })).action, "ride");
assert.ok(remixBlock("filter_build", rx({ preDrop: true, remixCount: 4 })));
// AI may use the 4th remix slot, rules stop at 3
d = decide(rx({ preDrop: true, remixCount: 3, remixUsed: ["a", "b", "c"], aiMove: { move: "echo_freeze", at: 0 } }));
assert.strictEqual(d.action, "echo_freeze");
// beat jump: weak intro/verse only, never over a drop; loop extend on a hot drop
const weak = { phraseSection: "verse", phraseEnergy: 0.3, nextPhraseSection: "verse" };
assert.strictEqual(decide(rx(weak)).action, "beat_jump");
assert.strictEqual(decide(rx({ ...weak, nextPhraseSection: "build" })).action, "ride");
assert.strictEqual(decide(rx({ ...weak, skipHitsDrop: { 8: true } })).action, "ride");
assert.strictEqual(decide(rx({ ...weak, setMode: "long" })).action, "ride");
const hot = { phraseSection: "drop", phraseEnergy: 0.9, nextPhraseSection: "drop" };
assert.strictEqual(decide(rx(hot)).action, "loop_extend");
assert.strictEqual(decide(rx({ ...hot, setMode: "quick" })).action, "ride");
// phrase label: the section covering most of the phrase wins
const pl = core.phraseLabel([{ label: "verse", start: 0, end: 4, energy: 0.4 },
  { label: "drop", start: 4, end: 16, energy: 0.8 }], 0, 16);
assert.strictEqual(pl[0], "drop"); assert.ok(Math.abs(pl[1] - 0.7) < 1e-9);
assert.deepStrictEqual(core.phraseLabel([], 0, 16), [null, null]);
assert.strictEqual(core.isPreDrop("verse", "drop"), true);
assert.strictEqual(core.isPreDrop("build", "verse"), true);
assert.strictEqual(core.isPreDrop("build", "build"), false);
assert.strictEqual(core.isPreDrop("chorus", "drop"), false);
// safety rules still veto AI transition moves
assert.ok(aiVeto({ move: "preclear" }, rx({ barsToExit: 10, overlapStyle: "instant" })));
assert.strictEqual(aiVeto({ move: "preclear" }, rx({ barsToExit: 10 })), null);
assert.ok(aiVeto({ move: "subdrop" }, rx({ section: "verse", subdropLastTrack: true })));
assert.ok(aiVeto({ move: "hold" }, rx({ barsToExit: 30 })));
assert.ok(aiVeto({ move: "beat_layer" }, rx({})));
assert.strictEqual(decide(rx({ mashupActive: true, aiMove: { move: "beat_layer", at: 0 } })).source, "AI");
// instant swap is recipe-forced even with an AI move pending
assert.strictEqual(decide(rx({ barsToExit: 4, overlapStyle: "instant", aiMove: { move: "hold", at: 0 } })).action, "instant");

// phrase bounds from downbeats / grid
assert.deepStrictEqual(phraseBounds(downs, 1, bar), [8 * bar, 16 * bar]);
assert.deepStrictEqual(phraseBounds([], 2, 2), [32, 48]);

// ---- hold loop safety net --------------------------------------------------
assert.strictEqual(needsHoldLoop(20, false, false), true);
assert.strictEqual(needsHoldLoop(20, true, false), false);   // transition scheduled
assert.strictEqual(needsHoldLoop(40, false, false), false);  // not near the end
assert.strictEqual(needsHoldLoop(20, false, true), false);   // already looping
// last clean phrase before the outro (grid 2 s bars: phrases every 16 s, outro at 192)
const hsecs = [{ label: "drop", start: 160, end: 192, energy: 0.9 }, { label: "outro", start: 192, end: 224, energy: 0.4 }];
assert.strictEqual(holdLoopAnchor([], hsecs, 205, 224, 2), 176);
// no outro: last whole phrase that has started
assert.strictEqual(holdLoopAnchor([], [], 205, 224, 2), 192);
assert.strictEqual(holdLoopBars(0), 8); assert.strictEqual(holdLoopBars(3), 4);

console.log("dj-mind core ok");
