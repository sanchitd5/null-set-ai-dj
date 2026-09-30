// Gradient rule + beat-lock gate (app/ui/static/tempo-rule.js).
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const R = require("../../ui/static/tempo-rule.js");

// Audible vs silent deck
assert.strictEqual(R.isAudible({ playing: false }), false);
assert.strictEqual(R.isAudible({ playing: true, side: 0, volume: 1 }), false);
assert.strictEqual(R.isAudible({ playing: true, side: 1, volume: 1 }), true);
assert.deepStrictEqual(R.tempoMove({ fromPct: 0, toPct: 6, bpm: 120, rate: 1, playing: false }).instant, true);

// Glide splitting: 0.25 %/bar -> 6 % takes 24 bars; small moves still ramp
assert.strictEqual(R.glideBars(0, 6), 24);
assert.strictEqual(R.glideBars(0, 0.01), 0);
const mv = R.tempoMove({ fromPct: 0, toPct: 2, bpm: 120, rate: 1, playing: true });
assert.strictEqual(mv.instant, false);
assert.ok(Math.abs(mv.seconds - 8 * 2) < 1e-6, "2 % = 8 bars at 120 BPM = 16 s");

// ADDENDUM pairs: 174 -> 125 never beat-locks (28 %, half-time 87 is 44 % off)
let b = R.beatRecipe({ aEff: 174, bBpm: 125, stemsBoth: true, tempoStemsBpm: 174 });
assert.strictEqual(b.beat, false); assert.strictEqual(b.fallback, "Stem Bridge");
b = R.beatRecipe({ aEff: 125, bBpm: 174, stemsBoth: false });
assert.strictEqual(b.beat, false); assert.strictEqual(b.fallback, "Echo Out");
// 128 -> 124: 3.2 % inside the +-8 % pitched range
b = R.beatRecipe({ aEff: 128, bBpm: 124, stemsBoth: false });
assert.strictEqual(b.beat, true); assert.ok(!b.lock.keyLocked);
// 87 -> 174: half-time counts as a lock
b = R.beatRecipe({ aEff: 87, bBpm: 174, stemsBoth: true });
assert.strictEqual(b.beat, true); assert.ok(Math.abs(b.lock.pct) < 0.01);
// key-lock stretch capped at 8 %: 12 % never locks, even with stems rendered for the tempo
assert.strictEqual(R.beatLock({ aEff: 112, bBpm: 100 }).ok, false);
assert.strictEqual(R.beatLock({ aEff: 112, bBpm: 100, tempoStemsBpm: 112 }).ok, false);
assert.strictEqual(R.beatLock({ aEff: 117, bBpm: 102, tempoStemsBpm: 117 }).ok, false);   // 14.7 %
// 7 % is inside the pitch range; 9 % is past both ranges
assert.strictEqual(R.beatLock({ aEff: 107, bBpm: 100 }).ok, true);
assert.strictEqual(R.beatLock({ aEff: 109, bBpm: 100, tempoStemsBpm: 109 }).ok, false);

// Pitch-range clamp: -18 % never reaches a deck
assert.strictEqual(R.clampPitch(-18, false), -8);
assert.strictEqual(R.clampPitch(-18, true), -8);

// Minimum ramps; a drop on its downbeat and a silent deck are exempt
assert.ok(R.minRampSeconds("level", 2, { deck: { playing: true } }) >= 0.49);
assert.strictEqual(R.minRampSeconds("stem", 2, { drop: true, playing: true }), 0.005);

// shouldWaitForTempoStems: decide whether to wait for B's render
assert.strictEqual(R.shouldWaitForTempoStems({}).wait, false, "no render in flight");
assert.strictEqual(R.shouldWaitForTempoStems({ renderInFlight: true, transitionSeconds: 3, phraseSeconds: 8, songLeftSeconds: 30 }).wait, true, "3s transition > 2.5s render");
assert.strictEqual(R.shouldWaitForTempoStems({ renderInFlight: true, transitionSeconds: 1, phraseSeconds: 8, songLeftSeconds: 30 }).wait, true, "defer 1s + 8s phrase, track has 30s");
assert.strictEqual(R.shouldWaitForTempoStems({ renderInFlight: true, transitionSeconds: 1, phraseSeconds: 8, songLeftSeconds: 8 }).wait, false, "defer would outlive track");
// Scan: AI tempo moves in autopilot.js go through setDeckPitch/aiSetPitch, never
// a bare setPitchPercent on a deck object.
const src = fs.readFileSync(path.join(__dirname, "../../ui/static/autopilot.js"), "utf8");
const bare = src.split("\n").filter((l) => /\bd\.setPitchPercent\(/.test(l) && !/aiSetPitch/.test(l));
assert.deepStrictEqual(bare, [], "instant setPitchPercent in autopilot.js:\n" + bare.join("\n"));
console.log("tempo_rule_check ok");
