// Node check for the pure autopilot core (app/ui/static/autopilot.js).
// Run by test_autopilot_core.py; exits non-zero on the first failed assertion.
const assert = require("assert");
const { stemBlendBars, stemBlendFader, phraseWaitS, introBars, vocalRecipe, homePlan, maskedGlideBars, maskedDropAt,
  LADDER_STEP_PCT } = require("../ui/static/autopilot.js");

const bar128 = 240 / 128;   // 1.875 s
const bar90 = 240 / 90;     // 2.667 s

// mashup kinds: >= 30 s, 16-bar multiples (swap at L/2 on an 8-bar line)
for (const kind of ["blend", "filter", "loop"]) {
  const b = stemBlendBars(kind, bar128, 20, 1);
  assert.strictEqual(b, 16, kind);
  assert.ok(b * bar128 >= 30);
  assert.strictEqual((b / 2) % 8, 0);
}
assert.strictEqual(stemBlendBars("blend", bar128, 100, 1), 32);          // room -> 32
assert.strictEqual(stemBlendBars("blend", 30 / 17, 100, 1), 32);         // 30 s = 17 bars -> up to 32
assert.ok(stemBlendBars("blend", bar90, 20, 1) * bar90 >= 30);
// capped by A's remaining room, never past it
assert.strictEqual(stemBlendBars("blend", bar128, 12, 1), 8);
assert.strictEqual(stemBlendBars("blend", 30 / 17, 30, 1), 16);          // want 32, room 29 -> 16
// scale applied once (old code applied it twice: 16 * 0.5 bars of half bars)
assert.strictEqual(stemBlendBars("blend", bar128, 100, 0.5), 16);
assert.strictEqual(stemBlendBars("blend", bar128, 20, 0.5), 8);
// bass / double stay short
assert.strictEqual(stemBlendBars("bass", bar128, 100, 1), 16);
assert.strictEqual(stemBlendBars("double", bar128, 100, 1), 8);
assert.strictEqual(stemBlendBars("double", bar128, 100, 0.5), 4);

// fader: parks at the centre in 1 bar (B's one intro stem does the
// introducing), crosses only after the intro phrase, over the last 8 bars
let f = stemBlendFader("blend", 32, 1);
assert.deepStrictEqual(f, [{ bar: 0, from: -1, to: 0, bars: 1 }, { bar: 24, from: 0, to: 1, bars: 8 }]);
f = stemBlendFader("blend", 16, -1);
assert.deepStrictEqual(f, [{ bar: 0, from: 1, to: 0, bars: 1 }, { bar: 8, from: 0, to: -1, bars: 8 }]);
f = stemBlendFader("bass", 16, 1);
assert.strictEqual(f[1].bar + f[1].bars, 16);
f = stemBlendFader("double", 8, 1);
assert.deepStrictEqual(f, [{ bar: 0, from: -1, to: 0, bars: 1 }, { bar: 6, from: 0, to: 1, bars: 2 }]);
// every move is continuous with the previous one (no jumps); the crossing
// never starts before the intro phrase (8 bars, 4 when short)
for (const [k, n] of [["blend", 32], ["blend", 16], ["bass", 16], ["blend", 8], ["bass", 8], ["filter", 4]]) {
  const m = stemBlendFader(k, n, 1);
  for (let i = 1; i < m.length; i++) assert.strictEqual(m[i].from, m[i - 1].to);
  assert.ok(m[m.length - 1].bar + m[m.length - 1].bars <= n);
  assert.ok(m[1].bar >= introBars(n), `${k} ${n}: crosses at ${m[1].bar}`);
  assert.strictEqual(m[0].to, 0);
}
assert.strictEqual(introBars(32), 8); assert.strictEqual(introBars(16), 8); assert.strictEqual(introBars(8), 4);

// vocal-driven recipe: stems on either side never cut (Open Eye Signal -> Delilah)
assert.strictEqual(vocalRecipe({ vIn: 3, oneSong: false, aStems: false, bStems: true }).recipe, "Bass Swap");
assert.strictEqual(vocalRecipe({ vIn: 3, oneSong: false, aStems: true, bStems: false }).recipe, "Bass Swap");
assert.strictEqual(vocalRecipe({ vIn: 3, oneSong: false, aStems: false, bStems: false }).recipe, "Bass Swap");   // never a hard cut
assert.strictEqual(vocalRecipe({ vIn: 3, oneSong: false, aStems: false, bStems: false }).short, true);
assert.strictEqual(vocalRecipe({ vIn: 6, oneSong: false, aStems: false, bStems: true }).short, false, "B's voice is held: no shortening");
assert.strictEqual(vocalRecipe({ vIn: 6, oneSong: false, aStems: false, bStems: false }).short, true);
assert.strictEqual(vocalRecipe({ vIn: 3, oneSong: true, aStems: true, bStems: true }), null);
assert.strictEqual(vocalRecipe({ vIn: 20, oneSong: false, aStems: false, bStems: false }), null);
assert.strictEqual(vocalRecipe({ vIn: null, oneSong: false, aStems: false, bStems: false }), null);

// tempo home: always ends at native, the path by gap
const ph16 = 16;
assert.strictEqual(homePlan({ gapPct: 0.01, tempoStems: false, songLeftS: 200, phraseS: ph16 }).path, "none");
assert.strictEqual(homePlan({ gapPct: -6, tempoStems: false, songLeftS: 200, phraseS: ph16 }).path, "glide");
assert.strictEqual(homePlan({ gapPct: 2.5, tempoStems: true, songLeftS: 200, phraseS: ph16 }).path, "drop");
let hp = homePlan({ gapPct: -12, tempoStems: true, songLeftS: 300, phraseS: ph16 });
assert.strictEqual(hp.path, "ladder");
assert.strictEqual(hp.steps[hp.steps.length - 1], 0, "ends native");
let prev = -12;
for (const s of hp.steps) { assert.ok(Math.abs(s - prev) <= LADDER_STEP_PCT + 1e-9, `step ${prev} -> ${s}`); prev = s; }
hp = homePlan({ gapPct: 20, tempoStems: true, songLeftS: 60, phraseS: ph16 });
assert.strictEqual(hp.path, "masked", "no time for 7 renders");
assert.deepStrictEqual(hp.steps, [0]);
// the old 4 % dead end is gone: every gap has a path home
for (const g of [4.5, 8, 15, 25, -25]) {
  for (const left of [30, 400]) assert.notStrictEqual(homePlan({ gapPct: g, tempoStems: true, songLeftS: left, phraseS: ph16 }).path, "none");
}
assert.strictEqual(maskedGlideBars(3), 32); assert.strictEqual(maskedGlideBars(25), 64);
// masked drop: first phrase line inside B's breakdown, else where its vocal is out
const secs = [{ label: "verse", start: 0, end: 64 }, { label: "breakdown", start: 64, end: 96 }];
let md = maskedDropAt(10, 0, 16, 200, secs, [[0, 60]]);
assert.deepStrictEqual(md, { at: 64, why: "B's breakdown" });
md = maskedDropAt(10, 0, 16, 200, [], [[0, 40]]);
assert.deepStrictEqual(md, { at: 48, why: "B's vocal is out" });
assert.strictEqual(maskedDropAt(10, 0, 16, 60, [], [[0, 100]]), null);

// phrase wait: next 8-bar line of the entry grid
const ph = 8 * bar128;
assert.strictEqual(phraseWaitS(10, 10, ph), 0);
assert.strictEqual(phraseWaitS(10 + ph, 10, ph), 0);
assert.ok(Math.abs(phraseWaitS(11, 10, ph) - (ph - 1)) < 1e-9);
assert.strictEqual(phraseWaitS(8, 10, ph), 2);

console.log("autopilot core ok");

// learned moves (/api/learned/pick) only switch to a recipe the console already allows
{
  const { learnedRecipe } = require("../ui/static/autopilot.js");
  const pick = (recipe, kind = "stem_intro") => ({ recipe, kind, seen: 5, source: "gfF8jzBVWvM 1:01" });
  const base = { blend: { clean: true }, oneSong: true, stemsBoth: true, vocalRule: false, mashupFits: false, recipe: "Bass Swap" };
  assert.strictEqual(learnedRecipe(pick("Long Blend"), base).recipe, "Long Blend");
  assert.match(learnedRecipe(pick("Long Blend"), base).why, /learned stem intro \(seen 5x/);
  assert.strictEqual(learnedRecipe(pick("Long Blend"), { ...base, vocalRule: true }), null);            // a vocal rule stands
  assert.strictEqual(learnedRecipe(pick("Long Blend"), { ...base, blend: { clean: false } }), null);     // B sings early
  assert.strictEqual(learnedRecipe(pick("Mashup → Transition", "acapella_over"), base), null);           // mashup doesn't fit
  assert.strictEqual(learnedRecipe(pick("Bass Swap", "bass_swap"), { ...base, recipe: "Long Blend" }).recipe, "Bass Swap");
  for (const k of ["layer", "peak", "riff"]) assert.strictEqual(learnedRecipe(pick("Bass Swap", "bass_swap"), { ...base, recipe: "Long Blend", [k]: {} }), null);
  assert.strictEqual(learnedRecipe(pick("Bass Swap", "bass_swap"), { ...base, recipe: "Mashup → Transition" }), null);  // mashup outranks
  assert.strictEqual(learnedRecipe(null, base), null);
  console.log("learned recipe ok");
}

// never transition while A is at, or building into, its energy high
{
  const { highSpans, exitPastHigh } = require("../ui/static/autopilot.js");
  const bar = 2, times = [], curve = [];
  for (let t = 0; t < 240; t++) { times.push(t); curve.push(t >= 150 && t < 190 ? 0.9 : 0.3); }
  const sp = highSpans(times, curve, bar);
  assert.strictEqual(sp.length, 1);
  assert.strictEqual(sp[0][0], 150 - 16 * bar);                                 // the 16-bar build is protected
  assert.ok(sp[0][1] >= 190);
  const phrase = 16;                                                             // 8 bars
  const r = exitPastHigh(110, 16 * bar, sp, phrase, 230);                        // 110..142 runs into the build
  assert.ok(r.clear && r.moved > 0 && r.t >= sp[0][1]);
  assert.deepStrictEqual(exitPastHigh(60, 16 * bar, sp, phrase, 230), { t: 60, clear: true, moved: 0 });   // clear already
  assert.strictEqual(exitPastHigh(110, 16 * bar, sp, phrase, 150).clear, false); // no room past it: not moved
  const loud = times.map(() => 0.9);
  assert.deepStrictEqual(highSpans(times, loud, bar), []);                       // loud all through: no "high" to protect
  console.log("energy high timing ok");
}

// next song: measured energy stays within reach of the one playing
{
  const { energyStepOk } = require("../ui/static/autopilot.js");
  assert.ok(energyStepOk(6, 8).ok && !energyStepOk(3, 8).ok);
  assert.match(energyStepOk(3, 8).why, /jump 3 -> 8 \(max 2/);
  assert.ok(!energyStepOk(6, 8, { relaxed: true }).ok && energyStepOk(6, 7, { relaxed: true }).ok);
  assert.ok(!energyStepOk(7, 5, { setPos: 0.1 }).ok && energyStepOk(7, 6, { setPos: 0.1 }).ok);   // building
  assert.ok(!energyStepOk(5, 7, { setPos: 0.9 }).ok);                                              // cooling
  assert.ok(energyStepOk(3, 6, { force: true }).ok && !energyStepOk(3, 7, { force: true }).ok);   // fallback: +1 only
  assert.ok(energyStepOk(3, 8, { rawDelta: 0.05 }).ok);                     // levels apart, measurements the same
  assert.ok(!energyStepOk(3, 8, { rawDelta: 0.3 }).ok);
  assert.ok(!energyStepOk(7, 5, { songs: 2 }).ok && energyStepOk(7, 5, { songs: 12 }).ok);   // warm-up builds, then open
  assert.ok(energyStepOk(5, 7, { songs: 40 }).ok);                          // no automatic "cooling" all night
  console.log("energy step ok");
}
