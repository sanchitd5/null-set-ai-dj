// Node check for the scheduleTransition seams in autopilot.js (decideRecipe, playWindowFor,
// exitBounds / exitPick / exitTiming / exitHighPush, tempoLockableAt). The virtual set
// (app/sim) drives these same functions. REF below is the inline code they were cut out of
// (verbatim logic); a seeded sweep proves the extraction kept the behaviour.
const assert = require("assert");
const core = require("../ui/static/autopilot.js");
const tempoRule = require("../ui/static/tempo-rule.js");
const djCore = require("../ui/static/dj-mind.js");

// ---- reference: the pre-extraction inline scheduleTransition logic --------------
function refDecide(o) {
  let recipe = o.recipe || "Blend", blend = o.blend, layer = o.layer;
  let vocalShort = false, vocalCut = "", vocalRule = false, blendClean = null;
  const aStems = !!o.aStems, bStems = !!o.bStems, stemsBoth = aStems && bStems;
  const lockS = tempoRule.planFit({ aEff: o.aEff, bBpm: o.bBpm, stemsBoth, tempoStemsBpm: o.tempoStemsBpm });
  const oneSong = lockS.oneSong;
  if (!lockS.beat) { blend = null; layer = false; }
  if (blend) {
    const bClean = blend.b_vocal_coverage == null || blend.b_vocal_coverage <= 0.15;
    const k = core.recipeKind(recipe);
    if (!bClean) recipe = "Bass Swap";
    else if (!["bass", "blend", "default"].includes(k)) recipe = "Long Blend";
    blendClean = bClean;
    if (oneSong) { recipe = "Long Blend"; blendClean = true; }
    const vr = core.vocalRecipe({ vIn: blend.b_vocal_in_bars, oneSong, aStems, bStems });
    if (vr) { vocalRule = true; recipe = vr.recipe; vocalShort = vr.short; vocalCut = vr.why; }
  } else if (oneSong) recipe = "Long Blend";
  else if (stemsBoth) recipe = "Stem Bridge";
  else if (!["echo", "filter"].includes(core.recipeKind(recipe))) recipe = "Echo Out";
  if (!layer) {
    const safe = core.keySafeRecipe(recipe, o.keyScore);
    if (safe !== recipe) { recipe = safe; blend = null; vocalShort = false; }
  }
  if (/\bcut\b/i.test(String(recipe || ""))) { recipe = "Bass Swap"; vocalShort = true; }
  if (lockS.beat && stemsBoth && o.mashupFits && o.mashupFits()) recipe = "Mashup → Transition";
  return { recipe, hasBlend: !!blend, vocalShort, vocalCut, vocalRule, oneSong, blendClean };
}

// seeded LCG: deterministic sweep
let seed = 12345;
const rnd = () => ((seed = (seed * 1664525 + 1013904223) % 4294967296) / 4294967296);
const pick = (a) => a[Math.floor(rnd() * a.length)];
const RECIPES = ["Blend", "Long Blend", "Bass Swap", "Echo Out", "Drop Swap", "Backspin", "Hard Cut", "Quick Cut", "Filter Sweep", "Breakdown", "Loop Roll", "Double Drop", "Stem Bridge"];
for (let i = 0; i < 4000; i++) {
  const aEff = 80 + rnd() * 100, bBpm = 80 + rnd() * 100;
  const blend = rnd() < 0.5 ? null : { b_vocal_coverage: pick([null, 0.05, 0.4]), b_vocal_in_bars: pick([null, 2, 6, 12, 30]) };
  const o = { recipe: pick(RECIPES), blend, layer: rnd() < 0.2, aStems: rnd() < 0.7, bStems: rnd() < 0.7,
    aEff, bBpm: rnd() < 0.2 ? aEff * pick([0.5, 1, 2, 1.03, 1.07, 1.12]) : bBpm,
    tempoStemsBpm: rnd() < 0.3 ? aEff : undefined, keyScore: pick([null, 0, 0.6, 0.75, 0.8, 0.9, 1]),
    mashupFits: rnd() < 0.5 ? () => true : () => false };
  const want = refDecide(o), got = core.decideRecipe(o, tempoRule);
  assert.strictEqual(got.recipe, want.recipe, JSON.stringify(o));
  assert.strictEqual(!!got.blend, want.hasBlend);
  assert.strictEqual(got.vocalShort, want.vocalShort);
  assert.strictEqual(got.vocalCut, want.vocalCut);
  assert.strictEqual(got.vocalRule, want.vocalRule);
  assert.strictEqual(got.oneSong, want.oneSong);
  assert.strictEqual(got.blendClean, want.blendClean);
}

// ---- named cases ------------------------------------------------------------------
// clashing keys never get a tonal blend (CLAUDE.md s4): Long Blend becomes an Echo Out
{
  const d = core.decideRecipe({ recipe: "Blend", blend: { b_vocal_coverage: 0 }, layer: false, aStems: true, bStems: true,
    aEff: 124, bBpm: 126, keyScore: 0, mashupFits: () => false }, tempoRule);
  assert.strictEqual(d.recipe, "Echo Out");
  assert.strictEqual(d.blend, null);
  assert.deepStrictEqual(d.keyRewrite, { from: "Long Blend", to: "Echo Out" });
  // ... and the same pair with compatible keys stays the stem "one song" Long Blend
  const ok = core.decideRecipe({ recipe: "Blend", blend: null, layer: false, aStems: true, bStems: true,
    aEff: 124, bBpm: 126, keyScore: 0.9, mashupFits: () => false }, tempoRule);
  assert.strictEqual(ok.recipe, "Long Blend");
  assert.strictEqual(ok.oneSong, true);
}
// tempo gap with stems on both decks: a beatless Stem Bridge, layer dropped
{
  const d = core.decideRecipe({ recipe: "Long Blend", blend: { entry: 8 }, layer: true, aStems: true, bStems: true,
    aEff: 174, bBpm: 125, keyScore: 1, mashupFits: () => false }, tempoRule);
  assert.strictEqual(d.recipe, "Stem Bridge");
  assert.strictEqual(d.dropLayer, true);
  assert.strictEqual(d.blend, null);
}
// no stems, tempo gap: Echo Out
assert.strictEqual(core.decideRecipe({ recipe: "Blend", blend: null, layer: false, aStems: false, bStems: false,
  aEff: 174, bBpm: 125, keyScore: 1 }, tempoRule).recipe, "Echo Out");
// a cut never plays: without stems a locked pair falls to Echo Out, never a Hard Cut
assert.strictEqual(core.decideRecipe({ recipe: "Hard Cut", blend: null, layer: false, aStems: false, bStems: false,
  aEff: 124, bBpm: 125, keyScore: 1 }, tempoRule).recipe, "Echo Out");
// camelotScore is the same table the console uses for keyScore
assert.strictEqual(djCore.camelotScore("6A", "1A"), 0);

// tempo lock incl. half / double time
assert.ok(core.tempoLockableAt(128, 64, 0.08));
assert.ok(core.tempoLockableAt(128, 137, 0.08));
assert.ok(!core.tempoLockableAt(128, 145, 0.08));

// play windows
assert.strictEqual(core.playWindowFor({ mode: "long", score: 90 }), core.WINDOWS.long);
assert.strictEqual(core.playWindowFor({ mode: "long", score: 10 }), core.WINDOWS.long);
assert.strictEqual(core.playWindowFor({ mode: "quick", score: 50 }), core.WINDOWS.bail);
assert.strictEqual(core.playWindowFor({ mode: "quick", score: 80 }), core.WINDOWS.quick);
assert.strictEqual(core.playWindowFor({ mode: "hybrid", score: 80, energy: 8 }), core.WINDOWS.quick);
assert.strictEqual(core.playWindowFor({ mode: "hybrid", score: 80, energy: 5 }), core.WINDOWS.long);
assert.strictEqual(core.playWindowFor({ mode: "hybrid", score: 80, energy: 2 }), core.WINDOWS.medium);
assert.strictEqual(core.playWindowFor({ mode: "long", score: 80, steering: "move" }), core.WINDOWS.bridge);
{
  const f = core.playWindowFor({ mode: "long", score: 80, famous: true, rem: 300 });
  assert.strictEqual(f.label, "FULL·famous");
  assert.strictEqual(f.min, 250);
  assert.strictEqual(f.max, 294);
  assert.strictEqual(core.playWindowFor({ mode: "long", score: 80, famous: true, rem: 80 }), core.WINDOWS.long);
}

// exit window, pick and phrase timing
{
  const w = core.WINDOWS.long;
  const b = core.exitBounds({ w, entryPos: 10, trackDur: 200 });
  assert.strictEqual(b.trackEnd, 200 - 24 - 2);
  assert.strictEqual(b.lo, 174);              // min(190, 174): the song is shorter than the window
  assert.strictEqual(b.hi, 174);
  const open = core.exitBounds({ w, entryPos: 0, trackDur: Infinity });
  assert.strictEqual(open.lo, 180);
  assert.strictEqual(open.hi, 360);
  // matcher point outside the window is clamped in; a blend point is kept as is
  assert.strictEqual(core.exitPick({ lo: 180, hi: 360, trackEnd: 500, hasBlend: false, candidateATime: 90 }), 180);
  assert.strictEqual(core.exitPick({ lo: 180, hi: 360, trackEnd: 500, hasBlend: true, blendExit: 90 }), 90);
  assert.strictEqual(core.exitPick({ lo: 180, hi: 360, trackEnd: 500, hasBlend: false, candidateATime: 200, minExit: 250 }), 250);
  assert.strictEqual(core.exitPick({ lo: 180, hi: 360, trackEnd: 500, layerStart: 222, hasBlend: true }), 222);
  // whole phrases, never before now + 15 s
  const phraseS = 32 * 60 / 128;
  const t = core.exitTiming({ exitAt: 100, nowPos: 120, phraseS, w, oneSong: false, vocalShort: false, peak: false });
  assert.ok(t.effectiveATime >= 135);
  assert.ok(Math.abs(((t.effectiveATime - 100) / phraseS) % 1) < 1e-9);
  assert.strictEqual(t.xfDuration, 24);
  assert.strictEqual(core.exitTiming({ exitAt: 100, nowPos: 0, phraseS, w, oneSong: true, vocalShort: false }).xfDuration, 24);
  assert.strictEqual(core.exitTiming({ exitAt: 100, nowPos: 0, phraseS, w: core.WINDOWS.quick, oneSong: true, vocalShort: false }).xfDuration, 16);
  assert.strictEqual(core.exitTiming({ exitAt: 100, nowPos: 0, phraseS, w, oneSong: false, vocalShort: true }).xfDuration, 8);
  // no analysis: nothing to push past
  assert.deepStrictEqual(core.exitHighPush({ t: 100, phraseS, bpm: 128, trackEnd: 300 }), { t: 100, moved: 0 });
}
// plan LLM skip (fix2-A): only when stems on both decks + beat lock + no peak moves
{
  const why = core.planSkipReason({ stemsBoth: true, lockBeat: true, peakOn: false });
  assert.ok(/rule-decided/.test(why));
  assert.strictEqual(core.planSkipReason({ stemsBoth: true, lockBeat: true, peakOn: true }), null);
  assert.strictEqual(core.planSkipReason({ stemsBoth: false, lockBeat: true, peakOn: false }), null);
  assert.strictEqual(core.planSkipReason({ stemsBoth: true, lockBeat: false, peakOn: false }), null);
  assert.strictEqual(core.planSkipReason(null), null);
}
console.log("sim seam checks ok");
