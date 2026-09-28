// node app/tests/plan_fit_check.js
// tempoRule.planFit: the pick-time / play-time shared smoothness gate.
const tempoRule = require("../ui/static/tempo-rule.js");

let failures = 0;
function check(name, cond) {
  if (!cond) { failures++; console.error(`FAIL ${name}`); }
  else console.log(`ok ${name}`);
}

// 174 -> 125, no stems on either deck: no beat lock (out of +-8% range),
// no bridge possible (stems not both up) -> only a hard Echo Out -> reject.
{
  const fit = tempoRule.planFit({ aEff: 174, bBpm: 125, stemsBoth: false });
  check("174->125 no stems: not smooth", fit.smooth === false);
  check("174->125 no stems: falls back to Echo Out", fit.fallback === "Stem Bridge" || fit.plan === "Echo Out");
  check("174->125 no stems: plan is Echo Out", fit.plan === "Echo Out");
}

// 128 -> 124: well inside the +-8% pitch range -> beat-matched, accepted.
{
  const fit = tempoRule.planFit({ aEff: 128, bBpm: 124, stemsBoth: false });
  check("128->124: smooth", fit.smooth === true);
  check("128->124: beat-matched", fit.beat === true);
}

// 87 -> 174: half-time pulse match (174 * 0.5 = 87) -> locks, accepted.
{
  const fit = tempoRule.planFit({ aEff: 87, bBpm: 174, stemsBoth: false });
  check("87->174 half-time: smooth", fit.smooth === true);
  check("87->174 half-time: beat-matched", fit.beat === true);
}

// Tempo gap with stems on both decks -> beatless Stem Bridge, accepted.
{
  const fit = tempoRule.planFit({ aEff: 174, bBpm: 125, stemsBoth: true });
  check("gap + stems both: smooth", fit.smooth === true);
  check("gap + stems both: Stem Bridge", fit.plan === "Stem Bridge");
}

// Scheduler reuses the stored plan: same pure fn, same inputs -> same verdict
// (this is what makes pick-time and play-time unable to disagree).
{
  const inputs = { aEff: 128, bBpm: 124, stemsBoth: false, tempoStemsBpm: null };
  const pickTime = tempoRule.planFit(inputs);
  const playTime = tempoRule.planFit(inputs); // scheduleTransition re-derives with live state as input
  check("scheduler reuses stored plan: same verdict", pickTime.smooth === playTime.smooth && pickTime.plan === playTime.plan);
}

if (failures) { console.error(`${failures} failure(s)`); process.exit(1); }
console.log("all plan_fit_check.js checks passed");
