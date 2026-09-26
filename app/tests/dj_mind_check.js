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

console.log("dj-mind core ok");
