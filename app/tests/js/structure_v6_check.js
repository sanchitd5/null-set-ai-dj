// v6 analysis drops in the console (twin of app/tests/py/test_structure_v6.py): dj-mind trackDropLines and
// drop-line.js dropSpans read a v6 record's phrase-grid `drops`; a v5 record keeps the energy-curve rule.
// argv[2] = JSON {v6, v5, bar, lines, spans} written by the Python test from the same synthetic track.
const assert = require("assert");
const fs = require("fs");
const core = require("../../ui/static/dj-mind.js");
const DL = require("../../ui/static/drop-line.js");

if (!process.argv[2]) {
  console.log("structure_v6: run through app/tests/py/test_structure_v6.py (it writes the fixture)");
  process.exit(0);
}
const fx = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const r3 = (xs) => xs.map((x) => +x.toFixed(3));

assert.deepStrictEqual(r3(core.trackDropLines(fx.v6, fx.bar).map((x) => x.t)), fx.lines, "v6: the stored drops");
assert.deepStrictEqual(core.trackDropLines(fx.v5, fx.bar),
  core.dropLines(fx.v5.phrase_boundaries_8bar, fx.v5.energy_times, fx.v5.energy_curve, fx.bar), "v5: the curve rule");
const spans = DL.dropSpans(fx.v6, fx.v6.bpm).map(r3).sort((a, b) => a[0] - b[0]);
assert.deepStrictEqual(spans, fx.spans, "dropSpans matches Python drop_line.drop_spans on a v6 record");
console.log("structure_v6 ok");
