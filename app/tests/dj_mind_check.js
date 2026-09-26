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

// ── PEAK mode ────────────────────────────────────────────────────────────────
{
  const { camelotScore, energyQ3, isPeak, bigMomentBlock, peakBlock, peakTransition, BIG_COOLDOWN_S,
          BEAT_BOOST_COOLDOWN_S, BACKSPIN_MAX } = core;
  // Camelot table (CLAUDE.md section 4)
  assert.strictEqual(camelotScore("8A", "8A"), 1); assert.strictEqual(camelotScore("12A", "1A"), 0.9);
  assert.strictEqual(camelotScore("8A", "8B"), 0.85); assert.strictEqual(camelotScore("8A", "10A"), 0.8);
  assert.strictEqual(camelotScore("8A", "11A"), 0); assert.strictEqual(camelotScore("8A", "9B"), 0);
  assert.strictEqual(camelotScore(null, "8A"), 0);
  // eligibility
  assert.strictEqual(energyQ3([{ energy: 0.2 }, { energy: 0.4 }, { energy: 0.6 }, { energy: 0.9 }, { energy: 1 }]), 0.9);
  assert.ok(isPeak({ profileEnergy: 8 }));
  assert.ok(!isPeak({ profileEnergy: 7, section: "verse" }));
  assert.ok(isPeak({ setMode: "quick", energyQ3: 0.8, sectionEnergy: 0.85 }));
  assert.ok(!isPeak({ setMode: "hybrid", energyQ3: 0.8, sectionEnergy: 0.85 }));
  assert.ok(isPeak({ section: "drop" }) && isPeak({ section: "build", nextSection: "drop" }));

  // big-moment restraint: per song, same kind back to back, 3-song window, cooldown, first 16 bars
  const T = 10000;
  assert.strictEqual(bigMomentBlock("fakeout", 5, [], 40, T), null);
  assert.match(bigMomentBlock("fakeout", 5, [], 12, T), /first 16 bars/);
  assert.match(bigMomentBlock("drop_swap", 5, [{ track: 5, kind: "fakeout", at: 0 }], 40, T), /one big moment per song/);
  assert.match(bigMomentBlock("fakeout", 5, [{ track: 4, kind: "fakeout", at: 0 }], 40, T), /same big move/);
  assert.strictEqual(bigMomentBlock("drop_swap", 5, [{ track: 4, kind: "fakeout", at: 0 }], 40, T), null);
  const two = [{ track: 3, kind: "double_drop", at: 0 }, { track: 4, kind: "fakeout", at: 1000 }];
  assert.match(bigMomentBlock("drop_swap", 5, two, 40, T), /2 big moments in the last 3 songs/);
  assert.strictEqual(bigMomentBlock("drop_swap", 6, two, 40, T), null);       // song 3 left the window
  assert.match(bigMomentBlock("drop_swap", 6, [{ track: 4, kind: "fakeout", at: T - 60 }], 40, T), /cooldown/);
  assert.strictEqual(bigMomentBlock("drop_swap", 6, [{ track: 4, kind: "fakeout", at: T - BIG_COOLDOWN_S }], 40, T), null);

  // in-song peak moves through decide()
  const pk = (o) => s(Object.assign({ peakOn: true, peak: true, phraseSection: "build", nextPhraseSection: "drop",
                                      nextSection: "drop", section: "build", bigBlock: null, remixUsed: [],
                                      remixCount: 0, lastRemixPhrase: -9, phraseIdx: 4, drumsOn: true,
                                      secsSinceBoost: 1e9, lastBarVocal: 0 }, o));
  let dec = decide(pk({}));
  assert.strictEqual(dec.action, "fakeout"); assert.ok(dec.peak); assert.strictEqual(dec.source, "RULE");
  assert.strictEqual(dec.bars, 0.25);                                          // 1 beat of silence
  assert.strictEqual(decide(pk({ lastBarVocal: 0.8 })).bars, 1);              // 1 bar, vocal only
  // big moment used -> roll instead; roll used -> nothing big
  dec = decide(pk({ bigBlock: "one big moment per song" }));
  assert.strictEqual(dec.action, "peak_roll"); assert.ok(dec.peak);
  assert.strictEqual(decide(pk({ bigBlock: "x", remixUsed: ["peak_roll"], remixCount: 1 })).action, "ride");
  // off / not peak / early / near exit / vocal layer -> nothing peak
  for (const o of [{ peakOn: false }, { peak: false }, { barsOnTrack: 8 }, { barsToExit: 20 }, { mashupActive: true }]) {
    assert.ok(!decide(pk(o)).peak, JSON.stringify(o));
  }
  assert.match(peakBlock("fakeout", pk({ phraseSection: "drop", section: "drop" })), /inside the drop/);
  assert.match(peakBlock("fakeout", pk({ nextSection: "verse" })), /no drop/);
  // beat boost: inside a drop, drums on, once per song, cooldown
  const inDrop = { phraseSection: "drop", section: "drop", nextPhraseSection: "drop" };
  dec = decide(pk(inDrop));
  assert.strictEqual(dec.action, "beat_boost"); assert.strictEqual(dec.bars, 8);
  assert.ok(!decide(pk({ ...inDrop, drumsOn: false })).peak);
  assert.ok(!decide(pk({ ...inDrop, boostThisTrack: true })).peak);
  assert.ok(!decide(pk({ ...inDrop, secsSinceBoost: BEAT_BOOST_COOLDOWN_S - 1 })).peak);
  // AI-proposed peak move: tagged AI + PEAK, rules keep the veto
  const aiF = { move: "fakeout", at: 0, reason: "hit them" };
  dec = decide(pk({ aiMove: aiF }));
  assert.strictEqual(dec.action, "fakeout"); assert.strictEqual(dec.source, "AI"); assert.ok(dec.peak);
  assert.ok(!(decide(pk({ aiMove: aiF, bigBlock: "cooldown" })).action === "fakeout" &&
              decide(pk({ aiMove: aiF, bigBlock: "cooldown" })).source === "AI"));
  // peak transition style never pre-clears; shows DOUBLE DROP near the exit
  assert.strictEqual(decide(s({ barsToExit: 20, overlapStyle: "peak" })).action, "ride");
  dec = decide(s({ barsToExit: 6, overlapStyle: "peak", peakKind: "drop_swap" }));
  assert.strictEqual(dec.action, "drop_swap"); assert.ok(dec.peak);

  // peakTransition: double drop needs key >= 0.8; else drop swap into a stronger drop
  const b2 = 2;                                                   // 120 BPM bar
  const aDrops = [{ t: 96, energy: 0.95, prevEnergy: 0.6 }];
  const base2 = { peakOn: true, peak: true, aDrops,
                  aVocal: [], lo: 50, hi: 150, plannedExit: 128, entryPos: 0, bar: b2, keyScore: 0.9,
                  bDropEnergy: 0.95, log: [], trackIdx: 5, now: 1e4, brakesUsed: 0, lastSwapBraked: false,
                  drop: { ok: true, entry_mode: "drop", drop: { start: 88, end: 104 }, entry: 88, b_vocal_coverage: 0 } };
  const pt = (o) => peakTransition(Object.assign({}, base2, o));
  let r = pt({});
  assert.strictEqual(r.kind, "double_drop"); assert.strictEqual(r.exitAt, 96); assert.strictEqual(r.bTime, 88);
  assert.strictEqual(r.recipe, "Double Drop");
  r = pt({ keyScore: 0.5 });
  assert.strictEqual(r.kind, "drop_swap"); assert.ok(r.brake);
  assert.strictEqual(pt({ keyScore: 0.5, bDropEnergy: 0.5 }), null);          // weaker drop: just blend
  assert.strictEqual(pt({ keyScore: 0.5, brakesUsed: BACKSPIN_MAX }).brake, false);
  assert.strictEqual(pt({ keyScore: 0.5, lastSwapBraked: true }).brake, false);
  assert.strictEqual(pt({ peakOn: false }), null);
  assert.strictEqual(pt({ peak: false }), null);
  assert.strictEqual(pt({ drop: null }), null);
  assert.strictEqual(pt({ drop: { ...base2.drop, entry_mode: "match" } }), null);   // intro entry: never a peak move
  assert.strictEqual(pt({ lo: 100 }), null);                                    // A's drop outside the window
  assert.strictEqual(pt({ aVocal: [[96, 160]], drop: { ...base2.drop, b_vocal_coverage: 0.9 }, keyScore: 0.9,
                          bDropEnergy: 0.5 }), null);                          // two vocals, weaker drop
  assert.strictEqual(pt({ log: [{ track: 5, kind: "fakeout", at: 0 }] }), null);   // song already had its moment
  assert.strictEqual(pt({ log: [{ track: 4, kind: "double_drop", at: 0 }] }).kind, "drop_swap"); // not twice in a row
}

// drop lines: energy jump into the top quartile, not the flickering labels
{
  const { dropLines, decide: dec2 } = core;
  const phr = Array.from({ length: 14 }, (_, i) => i * 16);
  const lvl = [0.3, 0.35, 0.4, 0.5, 0.9, 0.9, 0.4, 0.45, 0.5, 0.55, 0.95, 0.9, 0.4, 0.3];
  const times = Array.from({ length: 448 }, (_, i) => i * 0.5);
  const curve = times.map((t) => lvl[Math.floor(t / 16)]);
  assert.deepStrictEqual(dropLines(phr, times, curve, 2).map((x) => x.t), [64, 160]);
  assert.deepStrictEqual(dropLines(phr, times, times.map((t) => 0.5 + 0.01 * Math.floor(t / 16)), 2), []);
  assert.deepStrictEqual(dropLines([], [], [], 2), []);
  // flags from drop lines drive the peak moves even with sliver labels
  const st = s({ peakOn: true, peak: true, phraseSection: "verse", nextPhraseSection: "verse", nextDrop: true,
                 bigBlock: null, remixUsed: [], remixCount: 0, lastRemixPhrase: -9, phraseIdx: 4 });
  assert.strictEqual(dec2(st).action, "fakeout");
  assert.strictEqual(dec2({ ...st, bigBlock: "x" }).action, "peak_roll");
  assert.strictEqual(dec2({ ...st, nextDrop: false, inDrop: true, drumsOn: true, secsSinceBoost: 1e9 }).action, "beat_boost");
}

// LAYER transition: eligibility, caps, set-mode bars, pre-clear veto
{
  const { layerBars, layerVeto, layerDecision, LAYER_EVERY, aiVeto } = core;
  assert.deepStrictEqual(layerBars("long"), { maxHold: 64, unwind: 16 });
  assert.deepStrictEqual(layerBars("quick"), { maxHold: 16, unwind: 8 });
  assert.strictEqual(layerBars("hybrid").maxHold, 32);
  const ok = { ok: true, keyScore: 0.9, vocalClash: 0, groove: true, sinceLayer: LAYER_EVERY,
               steering: false, peak: false, energy: 6 };
  assert.strictEqual(layerVeto(ok), null);
  assert.deepStrictEqual(layerDecision(ok), { layer: true, source: "RULE", why: "locked tempo, keys fit, steady grooves" });
  assert.match(layerVeto({ ...ok, keyScore: 0.7 }), /keys/);
  assert.match(layerVeto({ ...ok, vocalClash: 0.2 }), /vocals/);
  assert.match(layerVeto({ ...ok, groove: false }), /groove/);
  assert.match(layerVeto({ ...ok, steering: true }), /steering/);
  assert.match(layerVeto({ ...ok, peak: true }), /peak/);
  assert.match(layerVeto({ ...ok, sinceLayer: LAYER_EVERY - 1 }), /one layer every/);   // restraint cap
  assert.match(layerVeto({ ok: false, why: "not tempo-locked" }), /tempo/);
  assert.match(layerVeto(null), /no layer/);
  // the AI proposes, the rules keep the veto
  assert.strictEqual(layerDecision({ ...ok, aiProposed: true, aiWhy: "grooves ride" }).source, "AI");
  const vetoed = layerDecision({ ...ok, aiProposed: true, sinceLayer: 0 });
  assert.strictEqual(vetoed.layer, false);
  assert.strictEqual(vetoed.source, "AI");
  // peak floor: rules alone don't layer, the AI may
  assert.strictEqual(layerDecision({ ...ok, energy: 9 }).layer, false);
  assert.strictEqual(layerDecision({ ...ok, energy: 9, aiProposed: true }).layer, true);
  // a LAYER never pre-clears A's bass and blocks phrase moves while it runs
  assert.strictEqual(decide(s({ barsToExit: 10, overlapStyle: "layer" })).action, "ride");
  assert.match(aiVeto({ move: "preclear" }, s({ barsToExit: 10, overlapStyle: "layer" })), /LAYER/);
  assert.strictEqual(decide(s({ layerActive: true, layerSource: "AI" })).action, "layer");
  assert.strictEqual(decide(s({ layerActive: true, layerSource: "AI" })).source, "AI");
}

console.log("dj-mind core ok");
