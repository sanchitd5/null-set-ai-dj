// Node checks for the set-energy recipe choice (energy-recipe-choice): the choice table, the set
// energy formula, the mashup stem-energy classifier, B-entry picking, and that stored / liked /
// Punjabi / vocal-rule / key paths are untouched by it.
const assert = require("assert");
const ap = require("../../ui/static/autopilot.js");
const sm = require("../../ui/static/stem-moves.js");
let n = 0;

// ---- set energy: 0.5 x arc target + 0.5 x weighted last-4 played levels -----------------------
{
  const e = ap.setEnergy({ setPos: 0.5, recent: [3, 3, 3, 3] });            // peak 8, played 3 -> 5.5 -> 6
  assert.strictEqual(e.arc, "peak"); assert.strictEqual(e.level, 6); assert.strictEqual(e.band, "middle"); n++;
  assert.strictEqual(ap.setEnergy({ setPos: 0.05, recent: [2, 2] }).band, "relaxed"); n++;   // (4+2)/2 = 3
  assert.strictEqual(ap.setEnergy({ setPos: 0.5, recent: [8, 9] }).band, "high"); n++;       // (8+8.67)/2 -> 8
  assert.strictEqual(ap.setEnergy({ setPos: null, recent: [] }).band, null); n++;            // nothing known
  assert.strictEqual(ap.setEnergy({ setPos: null, recent: [7] }).level, 7); n++;             // played only
  // newest weighs most: [9, 1] (1 newest) sits under [1, 9]
  assert.ok(ap.setEnergy({ setPos: null, recent: [9, 1] }).level < ap.setEnergy({ setPos: null, recent: [1, 9] }).level); n++;
}

// ---- choice table: band x allowed options x clean overlap -------------------------------------
{
  const c = (band, mashupKind, cleanBoth, levels = {}, setLevel = null) =>
    ap.energyRecipeChoice({ band, setLevel, mashupFits: mashupKind != null, mashupKind, blendOpen: true, cleanBoth, longBlendOk: cleanBoth, levels }).recipe;
  const M = "Mashup → Transition";
  assert.strictEqual(c("relaxed", "low", false), M); n++;
  assert.strictEqual(c("relaxed", "beat", false), "Bass Swap"); n++;     // beat-keeping mashup only at middle / high
  assert.strictEqual(c("middle", "beat", false), M); n++;
  assert.strictEqual(c("middle", "low", false), "Bass Swap"); n++;       // low-energy only when relaxed
  assert.strictEqual(c("high", "beat", true), M); n++;
  assert.strictEqual(c("high", "low", true), "Long Blend"); n++;         // clean -> Long Blend over Bass Swap
  assert.strictEqual(c("high", null, false), "Bass Swap"); n++;
  assert.strictEqual(c("relaxed", null, true), "Long Blend"); n++;
  // 5b veto: a mashup window 4 levels off the set is skipped for the next allowed option
  assert.strictEqual(c("middle", "beat", false, { mashup: 10, bass: 6 }, 6), "Bass Swap"); n++;
  // every option mismatched: priority wins
  assert.strictEqual(c("middle", "beat", false, { mashup: 10, bass: 1 }, 6), M); n++;
  // blend not open (vocal rule / key rewrite / cut): only the mashup is re-ordered, else unchanged
  assert.strictEqual(ap.energyRecipeChoice({ band: "high", mashupFits: false, blendOpen: false, cleanBoth: true, levels: {} }).recipe, null); n++;
}

// ---- decideRecipe wiring: rules outside the choice stay as they were --------------------------
{
  const tr = { planFit: () => ({ beat: true, oneSong: false, smooth: true, why: "lock", lock: { why: "lock" } }) };
  const base = { recipe: "Long Blend", blend: { entry: 10, b_vocal_coverage: 0.5 }, layer: false, aStems: true, bStems: true,
    aEff: 128, bBpm: 128, keyScore: 1, mashupFits: () => false };
  const noEn = ap.decideRecipe(base, tr);
  assert.strictEqual(noEn.recipe, "Bass Swap"); n++;                     // vocal overlap -> Bass Swap (as before)
  // relaxed + low-energy mashup: now a mashup where it used to be a mashup too (priority kept)
  const withM = ap.decideRecipe(Object.assign({}, base, { mashupFits: () => true, energy: { band: "relaxed", mashupKind: "low", levels: {} } }), tr);
  assert.strictEqual(withM.recipe, "Mashup → Transition"); n++;
  // relaxed + beat-keeping mashup: not allowed -> the blend decideRecipe had
  const beatRelax = ap.decideRecipe(Object.assign({}, base, { mashupFits: () => true, energy: { band: "relaxed", mashupKind: "beat", levels: {} } }), tr);
  assert.strictEqual(beatRelax.recipe, "Bass Swap"); n++;
  // key clash still rewrites to Echo Out whatever the energy
  const clash = ap.decideRecipe(Object.assign({}, base, { keyScore: 0.3, energy: { band: "high", mashupKind: null, levels: {} } }), tr);
  assert.strictEqual(clash.recipe, "Echo Out"); n++;
  // Punjabi profile at its full level: energy ignored
  const pj = ap.decideRecipe(Object.assign({}, base, { mashupFits: () => true, profile: { level: "full", fallback: "Quick Cut" }, energy: { band: "relaxed", mashupKind: "beat", levels: {} } }), tr);
  assert.strictEqual(pj.energyPick, undefined); n++;
  // no energy facts: identical output to before
  assert.deepStrictEqual(ap.decideRecipe(Object.assign({}, base, { energy: null }), tr), noEn); n++;
}

// ---- mashup classifier on stem energy: the Neverland -> No Control plain mashup is low-energy ---
{
  const M = 16, plan = sm.mashupTransitionPlan(M, 0.7), T = plan.total;
  const flat = (v) => Array.from({ length: T }, () => v);
  const eA = { drums: flat(0.2), bass: flat(0.15), vocals: flat(0.1), other: flat(0.1) };
  const eB = { drums: flat(0.25), bass: flat(0.2), vocals: flat(0.1), other: flat(0.1) };
  const k = sm.mashupEnergyKind(plan, eA, eB);
  assert.strictEqual(k.kind, "low"); assert.ok(k.gapBars >= 1); n++;   // A's beat out at M-2, B's beat at M
  // a drums-host plan keeps a beat under B the whole way
  const dh = sm.drumsHostPlan(M, 0.7);
  assert.strictEqual(sm.mashupEnergyKind(dh, eA, eB).kind, "beat"); n++;
  assert.strictEqual(sm.mashupEnergyKind(plan, null, eB), null); n++;
}

// ---- B entry: the window nearest the set level, ties keep the console's line ------------------
{
  const cands = [{ t: 10, level: 5 }, { t: 40, level: 8 }, { t: 70, level: 3 }];
  assert.strictEqual(ap.pickEntryByEnergy(cands, 8), 40); n++;
  assert.strictEqual(ap.pickEntryByEnergy(cands, 3), 70); n++;
  assert.strictEqual(ap.pickEntryByEnergy(cands, 5.4), 10); n++;
  assert.strictEqual(ap.pickEntryByEnergy([{ t: 10, level: 5 }, { t: 40, level: 5.3 }], 5.6), 10); n++; // under half a level
  assert.strictEqual(ap.pickEntryByEnergy(cands, null), 10); n++;
}

// ---- window level scaling ----------------------------------------------------------------------
assert.strictEqual(ap.windowLevel(6, 2, 1), 10); n++;
assert.strictEqual(ap.windowLevel(6, 0.5, 1), 3); n++;
assert.strictEqual(ap.windowLevel(null, 1, 1), null); n++;

// ---- golden: owner-liked pairs play as stored at every set energy -------------------------
{
  const fs = require("fs"), path = require("path");
  const tr = require("../../ui/static/tempo-rule.js");
  const mm = require("../../ui/static/macro-mode.js");
  const gold = JSON.parse(fs.readFileSync(path.join(__dirname, "../fixtures/owner_liked_pairs.json"), "utf8"));
  const bands = { relaxed: 4, middle: 6, high: 8 };
  for (const [band, setLevel] of Object.entries(bands)) {
    for (const kind of ["low", "beat", null]) {
      const energy = { band, setLevel, mashupKind: kind, levels: { mashup: setLevel, bass: setLevel, blend: setLevel } };
      // the atlas / decideRecipe path on the liked rows' own facts: recipe unchanged
      for (const [k, p] of Object.entries(gold.pairs)) {
        const fa = gold.tracks[p.a].bpm, fbpm = gold.tracks[p.b].bpm, r0 = tr.lockRate(fa, fbpm);
        const both = p.stems[0] && p.stems[1], lockGap = Math.abs(r0 - 1);
        const d = ap.decideRecipe({ recipe: "Long Blend", blend: p.blend && p.blend.ok ? Object.assign({}, p.blend) : null, layer: false,
          aStems: p.stems[0], bStems: p.stems[1], aEff: fa, bBpm: fbpm, tempoStemsBpm: both && lockGap <= 0.08 ? fbpm * r0 : null,
          keyScore: p.key, mashupFits: () => !!p.mashup.ok, energy }, tr);
        assert.strictEqual(d.recipe, p.recipe, `${k} at ${band}/${kind}: stored recipe`); n++;
      }
      // the liked Neverland -> Nocturnal step, booked forced: Echo Out with the stored exit / entry
      const nv = gold.pairs["c4a392ce13e82bc9>3ef9ad4b3fd01c79"];
      const eo = ap.forcedBooking({ forced: mm.forcedOf({ n: 1, a: nv.a, b: nv.b, recipe: "Echo Out", a_time: nv.exit, b_time: nv.entry }),
        nowPos: 0, phraseS: 7.5, trackEnd: 400, liveATime: 1, liveBTime: 2, beat: true, stemsBoth: true, keyScore: nv.key,
        mashupFits: true, mergeOn: true, mergeGate: () => null, energy });
      assert.strictEqual(eo.recipe, "Echo Out", `liked Echo Out at ${band}`); assert.strictEqual(eo.refused, null);
      assert.strictEqual(eo.aT, nv.exit); assert.strictEqual(eo.bT, nv.entry); n++;
    }
  }
  // the booking never computes set energy for a forced (macro / studied / FOLLOW SET / liked) step
  const src = fs.readFileSync(path.join(__dirname, "../../ui/static/autopilot.js"), "utf8");
  assert.ok(/if \(!forced\) \{\s*try \{ setEn = liveSetEnergy/.test(src), "set energy skipped when forced"); n++;
  // the fire-time mashup upgrade honours the booking's set-energy refusal, and only that booking's
  assert.ok(src.includes('energyNoMashup = !!(dec.energyPick && dec.recipe !== "Mashup → Transition")'), "booking sets the veto"); n++;
  assert.ok(/function executeTransition\([^)]*\) \{\s*const noMash = energyNoMashup; energyNoMashup = false;/.test(src), "veto consumed per transition"); n++;
  assert.ok(src.includes("mashupFits(od1, id1, xT0) : null") && src.includes("&& !noMash ? mashupFits(od1, id1, xT0)"), "fire-time upgrade gated"); n++;
  assert.ok(src.includes("energyNoMashup = !!f.energy_no_mashup"), "performNow: preview-only field"); n++;
}

console.log(`energy_choice_check: ${n} checks ok`);
