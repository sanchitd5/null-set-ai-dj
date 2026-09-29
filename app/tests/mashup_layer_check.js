// Node check for the guest-vocal parameters of the live mashup layer (app/ui/static/mashup-layer.js).
const assert = require("assert");
const { layerParams } = require("../ui/static/mashup-layer.js");

// nothing measured: the old constants
assert.deepStrictEqual(layerParams({}, 64), { level: 0.9, hp: 120, measured: false });
assert.deepStrictEqual(layerParams({ host_entries: [64], host_levels: [0.5] , hp_hz: 100 }, 64).hp, 120);   // never under the KB sub crossover

// measured: the level of the host entry that was chosen, the corner from the host's own low end
const plan = { host_entries: [32, 64, 96], host_levels: [0.8, 0.3, 0.55], hp_hz: 165, param_sources: { guest_level: "measured", hp_hz: "measured" } };
assert.deepStrictEqual(layerParams(plan, 64.2), { level: 0.3, hp: 165, measured: true });
assert.strictEqual(layerParams(plan, 95).level, 0.55);
// a level outside (0, 1] or a corner outside the sane range is ignored
assert.strictEqual(layerParams({ host_entries: [1], host_levels: [1.7] }, 1).level, 0.9);
assert.strictEqual(layerParams({ hp_hz: 900 }, 1).hp, 120);
// the server marked its level as a fallback: not counted as measured
assert.strictEqual(layerParams({ ...plan, param_sources: { guest_level: "fallback" } }, 64).measured, false);
console.log("mashup layer params ok");
