// node app/tests/js/artist_spacing_check.js: the one artist-spacing rule (artist-spacing.js),
// same cases as the Python twin (test_artist_spacing_parity.py) and every JS path that uses it.
const assert = require("assert");
const sp = require("../../ui/static/artist-spacing.js");
const mm = require("../../ui/static/macro-mode.js");
const cases = require("./artist_spacing_cases.json");

for (const c of cases) assert.strictEqual(sp.spacingKind(c.name, c.recent, c.relax).kind, c.kind, c.why);

assert.deepStrictEqual(sp.creditedArtists("Fred again.. x I. Jordan - Admit"), ["Fred again..", "I. Jordan"]);
assert.ok(/artist spacing/.test(sp.spacingBlock("Fred again.. - Jungle", ["Fred again.. - Kyle"])));

// combos and macro steps obey it (they used a first-credit-only check that missed title credits)
const recent = cases[0].recent;
const row = { a: "A", b: "B", b_name: "Fred again.. - Delilah (pull me out of this)", works: 90, combo: "merge" };
const combo = mm.comboCandidates({ aId: "A", partners: [row], played: [], recent });
assert.strictEqual(combo.list.length, 0, "combo repeating a recent artist is refused");
assert.ok(/artist spacing/.test(combo.skipped[0].why), combo.skipped[0].why);
assert.strictEqual(mm.comboCandidates({ aId: "A", partners: [row], played: [], recent: ["Bicep - Glue", "Lane 8 - X", "Q - y"] }).list.length, 1);

console.log("artist_spacing_check: ok");
