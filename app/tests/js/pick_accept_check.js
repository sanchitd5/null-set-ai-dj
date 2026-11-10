// Node check for the console's candidate acceptance (app/ui/static/pick_accept.js): every golden vector that needs no hooks.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
require("../../ui/static/generated/dj_config.js");
const { acceptCandidate, resolveGenre } = require("../../ui/static/pick_accept.js");

const goldens = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "fixtures", "pick_rules_goldens.json"), "utf8"));
let ran = 0, bad = 0;
for (const [file, body] of Object.entries(goldens.files)) {
  if (body.kind === "resolve_genre") {
    for (const c of body.cases) {
      ran++;
      try { assert.deepStrictEqual(resolveGenre(c.input), c.expect); } catch (e) { bad++; console.error(`FAIL genre: ${c.name}`); }
    }
    continue;
  }
  for (const c of body.cases) {
    if (c.needs_hooks) continue;
    const got = acceptCandidate(c.candidate, c.state);
    ran++;
    try {
      assert.strictEqual(got.ok, c.expect.ok);
      assert.strictEqual(got.reason, c.expect.reason);
    } catch (e) {
      bad++;
      console.error(`FAIL ${file}: ${c.name}: got ${JSON.stringify(got)} want ${JSON.stringify(c.expect)}`);
    }
  }
}
// no hooks: scene and era gates skipped
assert.deepStrictEqual(acceptCandidate({ ids: ["a"], genre: "metal" }, { scene_genre: "house" }), { ok: true, reason: null });
assert.ok(ran > 0, "no vectors ran");
if (bad) process.exit(1);
console.log(`pick_accept_check: ${ran} vectors ok`);
