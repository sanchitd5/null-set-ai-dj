// OWNER RULE "dhol drop only for punjabi songs, shouldn't experiment": the one gate (scene-profile.js dholOk)
// and every AI dhol path that reads it: beat-grid-ai.js presetFor (the bhangra / dhol chaal pattern),
// auto-sampler.js dholBlock (a dhol sample on a pad), artist-moves.js planDholDrop (both songs Punjabi, full).
const assert = require("assert");
const SP = require("../../ui/static/scene-profile.js");
const BG = require("../../ui/static/beat-grid-ai.js");
const AS = require("../../ui/static/auto-sampler.js");
const AM = require("../../ui/static/artist-moves.js");

// the gate
assert.strictEqual(SP.dholOk("punjabi pop").ok, true);
assert.strictEqual(SP.dholOk("Bhangra").ok, true);
for (const g of ["melodic techno", "uk garage", "indie pop", "bollywood", "", null, "desire pop", "electronic"]) {
  const v = SP.dholOk(g);
  assert.strictEqual(v.ok, false, `refused on ${g}`);
  assert(/dhol only/.test(v.why), v.why);
}
assert.strictEqual(SP.dholOk("punjabi", "punjabi hip hop").ok, true, "a Punjabi -> Punjabi transition");
assert.strictEqual(SP.dholOk("punjabi", "melodic house").ok, false, "never into a non-Punjabi song");
assert.strictEqual(SP.dholOk("punjabi", "").ok, false, "an unlabelled incoming song is not proven Punjabi");

// beat grid AI: bhangra only on Punjabi songs, never a guess
assert.strictEqual(BG.presetFor("punjabi", 100), "bhangra");
assert.strictEqual(BG.presetFor("Bhangra house", 128), "bhangra");
assert.notStrictEqual(BG.presetFor("desire pop", 100), "bhangra", "a word containing 'desi' is not the scene");
assert.notStrictEqual(BG.presetFor("dholak funk", 100), "bhangra");
assert.strictEqual(BG.presetFor("melodic techno", 124), "house");
for (const bpm of [80, 100, 124, 174]) assert.notStrictEqual(BG.presetFor("", bpm), "bhangra", `no experiment at ${bpm} BPM`);

// auto sampler: a pad holding a dhol sample
assert.strictEqual(AS.dholBlock("dhol_hit_01", "drop", "punjabi", null), null, "Punjabi drop: fires");
assert(AS.dholBlock("Dhol Hit 01", "drop", "melodic techno", null), "refused on an electronic song");
assert(AS.dholBlock("bhangra-loop", "drop", "", null), "refused on an unlabelled song");
assert(AS.dholBlock("dhol_hit", "transition", "punjabi", "indie pop"), "refused into a non-Punjabi song");
assert.strictEqual(AS.dholBlock("dhol_hit", "transition", "punjabi", "bhangra"), null);
assert.strictEqual(AS.dholBlock("clap_tight", "drop", "melodic techno", null), null, "other samples untouched");
assert.strictEqual(AS.dholBlock(null, "drop", "melodic techno", null), null, "a synth pad untouched");

// artist dhol drop-in: never unless both songs are Punjabi at the full level
const base = { relaxed: false, onDemand: true, sceneLevel: "full", aPunjabi: true, bPunjabi: false };
assert.strictEqual(AM.planDholDrop(base).gate, "scene");
assert.strictEqual(AM.planDholDrop(Object.assign({}, base, { aPunjabi: false, bPunjabi: true })).gate, "scene");
assert.strictEqual(AM.planDholDrop(Object.assign({}, base, { bPunjabi: true, sceneLevel: "handover" })).gate, "scene");

console.log("dhol_gate_check ok");
