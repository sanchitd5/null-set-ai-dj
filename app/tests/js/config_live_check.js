// config-live.js: live overrides apply only to bundled live keys, clamp to the bundled bounds, and fall back.
"use strict";
const assert = require("assert");
const cfg = require("../../ui/static/generated/dj_config.js");
const { applyLive } = require("../../ui/static/config-live.js");
const live = globalThis.DJ_CONFIG_LIVE;

assert.ok(Object.isFrozen(cfg) && Object.keys(live).length > 10);
const body = (values) => ({ config_hash: "x", values });
const one = (name, value) => ({ [name]: { value, min: 0, max: 1e9, unit: "u" } });

// applies a live key
let out = applyLive(cfg, live, body(one("KEY_SAFE_MIN", 0.7)));
assert.strictEqual(out.KEY_SAFE_MIN, 0.7);
assert.strictEqual(out.BYPASS_KEY_SCORE, cfg.BYPASS_KEY_SCORE);

// clamps to the BUNDLED bounds, not the server's
out = applyLive(cfg, live, body(one("KEY_SAFE_MIN", 5)));
assert.strictEqual(out.KEY_SAFE_MIN, live.KEY_SAFE_MIN.max);
out = applyLive(cfg, live, body(one("KEY_SAFE_MIN", -5)));
assert.strictEqual(out.KEY_SAFE_MIN, live.KEY_SAFE_MIN.min);

// plain numbers (README shape) work too
assert.strictEqual(applyLive(cfg, live, { values: { KEY_SAFE_MIN: 0.65 } }).KEY_SAFE_MIN, 0.65);

// non-live, unknown, non-number and NaN are ignored
out = applyLive(cfg, live, body({ ...one("KEYLOCK_RANGE_PCT", 1), ...one("NOPE", 1), ...one("BYPASS_KEY_SCORE", "x"), ...one("ENERGY_MIN_RAW", NaN) }));
assert.strictEqual(out.KEYLOCK_RANGE_PCT, cfg.KEYLOCK_RANGE_PCT);
assert.strictEqual(out.NOPE, undefined);
assert.strictEqual(out.BYPASS_KEY_SCORE, cfg.BYPASS_KEY_SCORE);
assert.strictEqual(out.ENERGY_MIN_RAW, cfg.ENERGY_MIN_RAW);

// no / bad payload keeps the bundled object
for (const bad of [null, undefined, "x", {}, { values: null }]) assert.strictEqual(applyLive(cfg, live, bad), cfg);
// the server-served script: DJ_CONFIG_SERVER set, then the file runs and applies it (live keys only, clamped)
{
  const vm = require("vm"), fs = require("fs");
  const g = { module: undefined };
  g.window = g;
  g.DJ_CONFIG = cfg; g.DJ_CONFIG_LIVE = live;
  const src = `window.DJ_CONFIG_SERVER = ${JSON.stringify(body({ ...one("KEY_SAFE_MIN", 5), ...one("KEYLOCK_RANGE_PCT", 1) }))};\n` +
    fs.readFileSync(require("path").join(__dirname, "../../ui/static/config-live.js"), "utf8");
  vm.runInNewContext(src, g);
  assert.strictEqual(g.DJ_CONFIG.KEY_SAFE_MIN, live.KEY_SAFE_MIN.max);
  assert.strictEqual(g.DJ_CONFIG.KEYLOCK_RANGE_PCT, cfg.KEYLOCK_RANGE_PCT);
  const g2 = { DJ_CONFIG: cfg, DJ_CONFIG_LIVE: live, DJ_CONFIG_SERVER: {} }; g2.window = g2;   // error case: {}
  vm.runInNewContext(fs.readFileSync(require("path").join(__dirname, "../../ui/static/config-live.js"), "utf8"), g2);
  assert.strictEqual(g2.DJ_CONFIG, cfg);
}
console.log("config_live_check: ok");
