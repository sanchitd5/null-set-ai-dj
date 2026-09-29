// Punjabi scene profile, console half (app/ui/static/scene-profile.js + its hooks in autopilot.js
// decideRecipe / playWindowFor and toggle-drawer.js).
const assert = require("assert");
const crypto = require("crypto");
const path = require("path");
const SP = require("../ui/static/scene-profile.js");
const ap = require("../ui/static/autopilot.js");
const tr = require("../ui/static/tempo-rule.js");
const td = require("../ui/static/toggle-drawer.js");
const vectors = require("./profile_off_vectors.js");
const golden = require("./fixtures/punjabi_off_golden.json");

// resolution: auto both / one side / neither / unknown; on; off
assert.strictEqual(SP.level("auto", "punjabi hip hop", "bhangra"), "full");
assert.strictEqual(SP.level("auto", "Desi pop", "Punjabi"), "full");
assert.strictEqual(SP.level("auto", "punjabi", "pop"), "handover");
assert.strictEqual(SP.level("auto", "bollywood", "bhangra"), "handover", "bollywood is a neighbour, not Punjabi");
assert.strictEqual(SP.level("auto", "house", "techno"), null);
assert.strictEqual(SP.level("auto", "", ""), null, "unknown genre is not Punjabi");
assert.strictEqual(SP.level("auto", "punjabi", ""), "handover", "unknown B counts as not Punjabi");
assert.strictEqual(SP.level("on", "house", "house"), "full");
assert.strictEqual(SP.level("off", "punjabi", "bhangra"), null);
assert.strictEqual(SP.normalizeMode("bogus"), "auto");
assert.strictEqual(SP.isPunjabi("desire"), false, "whole words: desi != desire");

// octave fold: 176 sits on 88; 130 stays 130
assert.strictEqual(SP.foldBpm(88, 176), 88);
assert.strictEqual(SP.foldBpm(88, 130), 130);

// decideRecipe: Quick Cut instead of Echo Out under the profile
const base = { recipe: "Long Blend", blend: null, layer: false, aStems: false, bStems: false, aEff: 88, bBpm: 130, keyScore: 1 };
const prof = { level: "full", fallback: "Quick Cut" };
let d = ap.decideRecipe(base, tr);
assert.strictEqual(d.recipe, "Echo Out", "no profile: no tempo lock -> Echo Out (today)");
d = ap.decideRecipe(Object.assign({}, base, { profile: prof }), tr);
assert.strictEqual(d.recipe, "Quick Cut"); assert.strictEqual(d.profileCut, true); assert.strictEqual(d.quickCut, true);
// key clash on a locked pair: today Echo Out, profile Quick Cut
const clash = { recipe: "Long Blend", blend: { entry: 10, b_vocal_coverage: 0 }, aStems: false, bStems: false, aEff: 128, bBpm: 128, keyScore: 0 };
assert.strictEqual(ap.decideRecipe(clash, tr).recipe, "Echo Out");
assert.strictEqual(ap.decideRecipe(Object.assign({}, clash, { profile: prof }), tr).recipe, "Quick Cut");
// 88 vs 176 locks already (tempo-rule folds x2): no fallback needed, stays a blend
d = ap.decideRecipe(Object.assign({}, base, { bBpm: 176, blend: { entry: 10, b_vocal_coverage: 0 }, profile: prof }), tr);
assert.strictEqual(d.lockS.beat, true); assert.notStrictEqual(d.recipe, "Quick Cut");
// the profile never turns a stems-both no-lock into a cut (Stem Bridge stays)
d = ap.decideRecipe(Object.assign({}, base, { aStems: true, bStems: true, profile: prof }), tr);
assert.strictEqual(d.recipe, "Stem Bridge");
// a matcher Quick Cut: kept as a cut under the profile, a Bass Swap without it (today's rule)
assert.strictEqual(ap.decideRecipe(Object.assign({}, base, { recipe: "Quick Cut" }), tr).recipe, "Echo Out");
assert.strictEqual(ap.decideRecipe(Object.assign({}, base, { recipe: "Quick Cut", profile: prof }), tr).recipe, "Quick Cut");
assert.strictEqual(ap.decideRecipe(Object.assign({}, base, { recipe: "Quick Cut", bBpm: 88, keyScore: 1 }), tr).cutRewrite, false);

// play window: the full profile's 45-90 s snippet beats famous / finish; steering still wins
const pw = SP.playWindow("full", "stay");
assert.deepStrictEqual([pw.min, pw.max], [45, 90]);
assert.strictEqual(SP.playWindow("handover", "stay"), null, "one-side handover: no snippet length");
assert.strictEqual(SP.playWindow("full", "move"), null);
assert.strictEqual(ap.playWindowFor({ steering: "stay", famous: true, rem: 300, mode: "long", score: 80, profileWindow: pw }).label, "PUNJABI");
assert.strictEqual(ap.playWindowFor({ steering: "move", profileWindow: pw }).label, ap.WINDOWS.bridge.label);

// status line
assert.strictEqual(SP.statusLabel("auto", null), "");
assert.strictEqual(SP.statusLabel("auto", "full"), "Punjabi profile active (auto, full)");
assert.strictEqual(SP.statusLabel("off", "full"), "");

// toggle drawer: the select persists its string value; booleans unchanged
let saved = td.withSaved({}, "ap-punjabi-profile", "on");
assert.deepStrictEqual(td.parseSaved(JSON.stringify(saved)), { "ap-punjabi-profile": "on" });
assert.deepStrictEqual(td.parseSaved(JSON.stringify({ "ap-other": "on" })), {}, "strings only for known selects");
assert.deepStrictEqual(td.withSaved({}, "ap-merge-toggle", "x"), { "ap-merge-toggle": true });
assert.strictEqual(td.isOn("ap-punjabi-profile", "off"), false);
assert.strictEqual(td.isOn("ap-punjabi-profile", "auto"), true);
assert.deepStrictEqual(td.restorePlan({ "ap-punjabi-profile": "off" }, { "ap-punjabi-profile": "auto" }), [["ap-punjabi-profile", "off"]]);

// off == before the profile: the same vectors hash to what main produced before this change
const canon = (v) => JSON.stringify(v, (k, x) => (x && typeof x === "object" && !Array.isArray(x)
  ? Object.fromEntries(Object.keys(x).sort().map((q) => [q, x[q]])) : x));
const now = vectors.compute(path.join(__dirname, "..", "ui", "static"));
for (const k of ["decide", "window"]) {
  assert.strictEqual(now[k].length, golden.js[k].n, k);
  assert.strictEqual(crypto.createHash("sha256").update(canon(now[k])).digest("hex"), golden.js[k].sha256, `${k}: profile off differs from before`);
}

console.log("scene_profile_check: ok");
