// node app/tests/rule_vectors_check.js: the console's copies of the shared rules
// against the golden vectors in fixtures/rule_vectors.json (test_rule_vectors.py
// checks the Python copies against the same file). Exact equality: both sides do
// the same float operations, so a difference is drift, not rounding noise.
const assert = require("assert");
const path = require("path");
const fs = require("fs");

const ap = require("../ui/static/autopilot.js");
const sm = require("../ui/static/stem-moves.js");
const V = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "rule_vectors.json"), "utf8"));

let n = 0;
for (const c of V.energy_step.cases) {
  const r = ap.energyStepOk(c.cur, c.nxt, c.o);
  assert.deepStrictEqual({ ok: r.ok, step: r.step, why: r.why }, c.expect, `energyStepOk(${c.cur}, ${c.nxt}, ${JSON.stringify(c.o)})`);
  n++;
}
V.high_spans.cases.forEach((c, i) => {
  assert.deepStrictEqual(ap.highSpans(c.times, c.curve, c.bar), c.expect, `highSpans case ${i}`);
  n++;
});
V.breakdown_spans.cases.forEach((c, i) => {
  assert.deepStrictEqual(ap.breakdownSpans(c.times, c.curve, c.bar), c.expect, `breakdownSpans case ${i}`);
  n++;
});
V.merge_rank.cases.forEach((c, i) => {
  const r = sm.mergeRank({ eA: c.eA, eB: c.eB, keyScore: c.keyScore, aRap: c.aRap, bRap: c.bRap });
  assert.deepStrictEqual(r.map((x) => ({ combo: x.combo, score: x.score })), c.expect, `mergeRank case ${i}`);
  n++;
});
assert.ok(n > 100, "vectors missing");
console.log(`rule vectors OK (${n})`);
