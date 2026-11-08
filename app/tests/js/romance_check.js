// node app/tests/js/romance_check.js: ROMANCE (bedtime) mode in the console's pure cores:
// energy never rises, hype recipes become gentle ones, the mind rides, the sleep timer plan.
const assert = require("assert");
const ap = require("../../ui/static/autopilot.js");
const dm = require("../../ui/static/dj-mind.js");

let n = 0;
const ok = (c, m) => { assert.ok(c, m); n++; };
const eq = (a, b, m) => { assert.strictEqual(a, b, m); n++; };

// ---- energy: romance never climbs, falls at most 1 a song
{
  eq(ap.energyStepOk(5, 6, { romance: true }).ok, false);
  eq(ap.energyStepOk(5, 6, { romance: true, force: true }).ok, false);   // not even the last-round force
  eq(ap.energyStepOk(5, 5, { romance: true }).ok, true);
  eq(ap.energyStepOk(5, 4, { romance: true }).ok, true);
  eq(ap.energyStepOk(5, 3, { romance: true }).ok, false);
  eq(ap.energyStepOk(5, 6, { relaxed: true }).ok, true);                 // plain relaxed still allows +1
  eq(ap.energyStepOk(5, 6, {}).ok, true);
}

// ---- decideRecipe: only gentle recipes play
{
  const tr = { planFit: () => ({ beat: true, oneSong: false, smooth: true, why: "lock", lock: { why: "lock" } }) };
  const base = { recipe: "Long Blend", blend: { entry: 10, b_vocal_coverage: 0.05 }, layer: false, aStems: true, bStems: true,
    aEff: 128, bBpm: 128, keyScore: 1, mashupFits: () => true };
  const gentle = ap.decideRecipe(Object.assign({}, base, { romance: true }), tr);
  eq(gentle.recipe, "Long Blend"); ok(!gentle.romanceRewrite);
  for (const hype of ["Drop Swap", "Double Drop", "Mashup → Transition", "Loop Roll", "Stutter Transition", "Backspin (Spinback)"]) {
    const r = ap.decideRecipe(Object.assign({}, base, { recipe: hype, romance: true }), tr);
    ok(r.recipe === "Long Blend" || r.recipe === "Bass Swap" || r.recipe === "Echo Out", `${hype} -> ${r.recipe}`);
    ok(r.recipe !== "Mashup → Transition");
  }
  // a fitting mashup is not taken in romance (it is outside romance)
  eq(ap.decideRecipe(base, tr).recipe === "Mashup → Transition", true, "control: no romance, mashup plays");
  eq(ap.decideRecipe(Object.assign({}, base, { romance: true }), tr).recipe === "Mashup → Transition", false);
  // key clash still goes to Echo Out
  eq(ap.decideRecipe(Object.assign({}, base, { recipe: "Drop Swap", keyScore: 0.3, romance: true }), tr).recipe, "Echo Out");
}

// ---- dj-mind: romance rides (no layer, no instant swap), relaxed alone still allows them
{
  const s = { layerActive: true, layerSource: "RULE" };
  const full = (o) => Object.assign({ barsToExit: 4, overlapStyle: "instant", setMode: "hybrid", section: "verse" }, o);
  const plain = dm.decide(Object.assign({}, full({ relaxed: true }), s));
  eq(plain.action, "layer");
  const rom = dm.decide(Object.assign({}, full({ relaxed: true, romance: true }), s));
  eq(rom.action, "ride");
  ok(/romance/.test(rom.why));
}

// ---- sleep timer plan
{
  eq(ap.sleepPlan({ minutes: 0, elapsedS: 9999, remainingS: 100 }).phase, "off");
  const run = ap.sleepPlan({ minutes: 30, elapsedS: 600, remainingS: 100 });
  eq(run.phase, "running"); eq(run.leftS, 1200);
  // out of time, plenty of song left: wait, then fade the last SLEEP_FADE_S
  const fin = ap.sleepPlan({ minutes: 15, elapsedS: 900, remainingS: 200 });
  eq(fin.phase, "final"); eq(fin.fadeS, ap.SLEEP_FADE_S); eq(fin.fadeInS, 200 - ap.SLEEP_FADE_S);
  // out of time, short tail: fade what is left, now
  const tail = ap.sleepPlan({ minutes: 15, elapsedS: 1000, remainingS: 20 });
  eq(tail.fadeS, 20); eq(tail.fadeInS, 0);
  eq(ap.SLEEP_FADE_S, 60);
}

console.log(`romance_check: ${n} checks passed`);
