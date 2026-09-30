// Fixed decideRecipe / playWindowFor vectors for the Punjabi scene profile's "off == today" proof.
// `node app/tests/js/profile_off_vectors.js [static dir]` prints the results as JSON for that
// directory's autopilot.js + tempo-rule.js. The hashes in app/tests/fixtures/punjabi_off_golden.json
// ("js") were made from the main checkout's app/ui/static BEFORE the profile existed;
// scene_profile_check.js recomputes them on this tree (no o.profile) and compares.
"use strict";
const path = require("path");

function compute(staticDir) {
  const ap = require(path.join(staticDir, "autopilot.js"));
  const tr = require(path.join(staticDir, "tempo-rule.js"));
  const dec = [];
  const recipes = ["Long Blend", "Bass Swap", "Echo Out", "Quick Cut", "Hard Cut", "Blend", "Filter Transition"];
  const blends = [null, { entry: 10, b_vocal_coverage: 0 }, { entry: 10, b_vocal_coverage: 0.5, b_vocal_in_bars: 8 }];
  const tempos = [[128, 128], [88, 176], [88, 130], [128, 140]];
  for (const recipe of recipes) for (const blend of blends) for (const [aEff, bBpm] of tempos)
    for (const keyScore of [null, 0, 0.3, 0.6, 1]) for (const aStems of [false, true]) for (const bStems of [false, true])
      for (const layer of [false, true]) for (const fits of [false, true]) {
        const o = { recipe, blend: blend && Object.assign({}, blend), layer, aStems, bStems, aEff, bBpm, keyScore,
          mashupFits: () => fits };
        dec.push(JSON.parse(JSON.stringify(ap.decideRecipe(o, tr))));
      }
  const win = [];
  for (const steering of ["stay", "move"]) for (const famous of [false, true]) for (const finish of [false, true])
    for (const mode of ["long", "quick", "hybrid"]) for (const score of [null, 50, 80]) for (const energy of [3, 6, 8])
      win.push(ap.playWindowFor({ steering, famous, finish, rem: 200, mode, score, energy }));
  return { decide: dec, window: win };
}

module.exports = { compute };
if (require.main === module) {
  const dir = process.argv[2] || path.join(__dirname, "..", "..", "ui", "static");
  process.stdout.write(JSON.stringify(compute(dir)));
}
