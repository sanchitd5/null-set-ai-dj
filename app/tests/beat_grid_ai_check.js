// node app/tests/beat_grid_ai_check.js — pure-logic checks for beat-grid-ai.js
const fs = require("fs");
const src = fs.readFileSync(__dirname + "/../ui/static/beat-grid-ai.js", "utf8");
const pick = (name) => new Function(src.match(new RegExp(`(const ${name} = [\\s\\S]*?\\];)`))[1] + `; return ${name};`)();
const GENRE_PRESETS = pick("GENRE_PRESETS");
const presetFor = new Function("GENRE_PRESETS", src.match(/(function presetFor[\s\S]*?\n  \})/)[1] + "; return presetFor;")(GENRE_PRESETS);
const cases = [["bhangra-house", 124, "bhangra"], ["Punjabi pop", 96, "bhangra"], ["melodic house", 123, "house"],
  ["hyperpop", 140, "halftime"], ["drum & bass", 174, "dnb"], ["reggaeton", 96, "dembow"], ["", 172, "dnb"], ["", 90, "halftime"], ["", 124, "tops"]];
let bad = 0;
for (const [g, b, want] of cases) { const got = presetFor(g, b); if (got !== want) { bad++; console.log("BAD", g, b, got, "want", want); } }
console.log(bad ? `${bad} failures` : "beat-grid-ai presets ok");
process.exit(bad ? 1 : 0);
