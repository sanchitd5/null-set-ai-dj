// Node check for the remix mode core (app/ui/static/remix-mode.js). Run by test_keylock.py.
const assert = require("assert");
const { loopRegion, nextGridPoint, nextBeat, sliceBounds, filterFreqs } = require("../ui/static/remix-mode.js");

const near = (a, b) => Math.abs(a - b) < 1e-6;
const db = [0, 2, 4, 6, 8, 10, 12, 14]; // 120 BPM, 2 s bars

// 1 bar: the bar holding the playhead
let r = loopRegion(5, 1, db, 120, 100);
assert.ok(near(r[0], 4) && near(r[1], 6), `1 bar ${r}`);
// 4 bars: aligned to the 4-bar group (bars 0-3), not "from here"
r = loopRegion(5, 4, db, 120, 100);
assert.ok(near(r[0], 0) && near(r[1], 8), `4 bars ${r}`);
// end too close to the playhead -> next region
r = loopRegion(5.99, 1, db, 120, 100);
assert.ok(near(r[0], 6) && near(r[1], 8), `guard ${r}`);
// half bar: splits the current bar
r = loopRegion(4.5, 0.5, db, 120, 100);
assert.ok(near(r[0], 4) && near(r[1], 5), `half ${r}`);
// past the analysed grid: extrapolated at the bar length
r = loopRegion(17, 1, db, 120, 100);
assert.ok(near(r[0], 16) && near(r[1], 18), `extrapolated ${r}`);
// no downbeats: flat grid from the BPM
r = loopRegion(3, 1, [], 120, 100);
assert.ok(near(r[0], 2) && near(r[1], 4), `flat ${r}`);
assert.strictEqual(loopRegion(3, 0, db, 120, 100), null);

// grid snapping: a slightly late hit keeps the point it missed (caller plays it now), else the next point
assert.ok(near(nextGridPoint(2.01, 0, 0.5), 2));
assert.ok(near(nextGridPoint(2.2, 0, 0.5), 2.5));
assert.ok(near(nextBeat(1.2, [], 120), 1.5));

// slices cover the region evenly
const s = sliceBounds([4, 6], 8, 3);
assert.ok(near(s[0], 4.75) && near(s[1], 5));

// filter knob: centre bypass, left low-pass, right high-pass
assert.ok(filterFreqs(0).bypass);
assert.ok(filterFreqs(-1).lp < 300 && !filterFreqs(-1).bypass);
assert.ok(filterFreqs(1).hp > 7000);
assert.ok(filterFreqs(-0.5).lp > filterFreqs(-1).lp);
console.log("remix mode ok");
