// The ONE drop-line rule (app/ui/static/drop-line.js), owner: "never vocal mix a drop line", narrow: only the
// drop window and the sung line into it, only while the layered voice sounds there.
// Same fixture as app/tests/py/test_drop_line.py (parity with app/music_brain/render/drop_line.py).
const assert = require("assert");
const DL = require("../../ui/static/drop-line.js");
const fx = require("../fixtures/drop_line_cases.json");

const spans = DL.dropSpans(fx.analysis, fx.analysis.bpm);
assert.deepStrictEqual(spans.map((s) => s.map((x) => +x.toFixed(3))).sort((a, b) => a[0] - b[0]), fx.spans,
  "the drop windows: energy drop line + the long drop section's first bars; the 1.5 s sliver is not a drop");
const singsOf = (s) => (s ? DL.mappedSings(s.regions, s.t0, s.at0, s.ratio) : null);
for (const c of fx.cases) {
  const r = DL.deckBusy({ analysis: fx.analysis, bpm: fx.analysis.bpm }, c.t0, c.t1, singsOf(c.sings));
  assert.strictEqual(r ? r.gate : null, c.gate, `${c.name}: ${r ? r.reason : "clear"}`);
  if (r) assert(/never vocal mix a drop line/.test(r.reason), "the refusal names the owner rule");
}
assert.strictEqual(DL.deckBusy({ analysis: null }, fx.unmeasured.t0, fx.unmeasured.t1).gate, fx.unmeasured.gate, "no analysis: refused");
assert.strictEqual(DL.dropLineBusy([], null, 0, 10), null, "a measured song without drops is clear");

// the merged artist moves (chant_gate, chop_duck) use this predicate, not their own copy
const AM = require("../../ui/static/artist-moves.js");
assert.strictEqual(AM.dropHit, DL.dropHit);
assert.strictEqual(AM.dropVocalLine, DL.dropVocalLine);
const chop = AM.planChopDuck({ kind: "vocal_chop", start: 60, end: 70, drops: spans, vocals: [], drumsRms: 0.1, chopRms: 0.1 });
assert.strictEqual(chop.ok, false); assert.strictEqual(chop.gate, "drop_line");

// learned moves: a vocal plan over a drop window is refused by gate drop_line (learned-moves.js dropGate)
const LM = require("../../ui/static/learned-moves.js");
assert.strictEqual(LM.dropGate({ kind: "vocal_chop", start: 60, end: 70 }, { drops: spans, vocals: [] }).gate, "drop_line");
assert.strictEqual(LM.dropGate({ kind: "vocal_chop", start: 0, end: 16 }, { drops: spans, vocals: [] }), null);
assert.strictEqual(LM.dropGate({ kind: "vocal_chop", start: 68, end: 80 }, { drops: spans, vocals: [] }), null, "after the drop window");
assert.strictEqual(LM.dropGate({ kind: "loop_extend", start: 60, end: 70 }, { drops: spans, vocals: [] }), null, "no vocal: not gated");
assert.strictEqual(LM.dropGate({ kind: "vocal_loop", start: 0, end: 16 }, { drops: undefined }).gate, "unmeasured");
assert.strictEqual(LM.dropGate({ kind: "vocal_loop", start: 60, end: 70 }, {}), null, "a caller without a drop map is not gated");

console.log("drop_line_check ok");
