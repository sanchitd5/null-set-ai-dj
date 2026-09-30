// node app/tests/js/beat_grid_ai_check.js — pure-logic checks for beat-grid-ai.js
// (presetFor is exported now: the bhangra pattern reads scene-profile.js dholOk, see dhol_gate_check.js)
const { presetFor } = require("../../ui/static/beat-grid-ai.js");
const cases = [["bhangra-house", 124, "bhangra"], ["Punjabi pop", 96, "bhangra"], ["melodic house", 123, "house"],
  ["hyperpop", 140, "halftime"], ["drum & bass", 174, "dnb"], ["reggaeton", 96, "dembow"], ["", 172, "dnb"], ["", 90, "halftime"], ["", 124, "tops"]];
let bad = 0;
for (const [g, b, want] of cases) { const got = presetFor(g, b); if (got !== want) { bad++; console.log("BAD", g, b, got, "want", want); } }
console.log(bad ? `${bad} failures` : "beat-grid-ai presets ok");
process.exit(bad ? 1 : 0);
