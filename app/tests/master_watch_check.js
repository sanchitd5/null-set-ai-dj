// master watchdog core: silence (report + recover), dropouts, clipping, de-dup
const assert = require("assert");
const w = require("../ui/static/master-watch.js");
const quiet = Math.pow(10, -70 / 20), loud = Math.pow(10, -12 / 20);
const st = {};
let out = [];
for (let t = 0; t <= 2.0 + 1e-9; t += 0.25) out = out.concat(w.step(st, { t, rms: t < 0.25 ? loud : quiet, clips: 0, onAir: true }).map((g) => ({ ...g, t })));
const kinds = out.map((g) => g.kind);
assert.ok(kinds.includes("dropout"));                                   // the fall itself
const sil = out.find((g) => g.kind === "silence"), rec = out.find((g) => g.kind === "recover");
assert.ok(sil && sil.t >= 0.25 + w.SILENT_S - 1e-9 && sil.t < 0.25 + w.SILENT_S + 0.26);
assert.ok(rec && rec.t >= 0.25 + w.RECOVER_S - 1e-9);
assert.strictEqual(kinds.filter((k) => k === "silence").length, 1);    // once per silence
const end = w.step(st, { t: 2.25, rms: loud, clips: 0, onAir: true });
assert.strictEqual(end[0].kind, "silence_end");
// nobody on air (paused / between songs): silence is not a glitch
const idle = {};
for (let t = 0; t < 3; t += 0.25) assert.deepStrictEqual(w.step(idle, { t, rms: quiet, clips: 0, onAir: false }), []);
// a fade is not a dropout: 3 dB per step
const fade = {};
let f = [];
for (let i = 0; i < 20; i++) f = f.concat(w.step(fade, { t: i * 0.25, rms: Math.pow(10, (-12 - 3 * i) / 20), clips: 0, onAir: true }));
assert.ok(!f.some((g) => g.kind === "dropout"));
assert.strictEqual(w.step({}, { t: 0, rms: loud, clips: 20, onAir: true })[0].kind, "clipping");
const d = {};
assert.ok(w.allow(d, "clipping", 0) && !w.allow(d, "clipping", 3) && w.allow(d, "clipping", 9));
assert.ok(w.allow(d, "recover", 0) && w.allow(d, "recover", 0.1));
console.log("master watch ok");
