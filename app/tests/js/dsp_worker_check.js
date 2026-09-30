// Node check: the worker's lag search finds a known offset (app/ui/static/dsp-worker.js).
const assert = require("assert");
global.self = {};
const { stemLagWindow } = require("../../ui/static/dsp-worker.js");
const sr = 8000, PAD = 4096, W = 3 * sr, LAG = 184;           // stems 23 ms late
// music-like: noise bursts (onsets) with a slow envelope, not a pure tone (periodic = ambiguous lag)
let seed = 7; const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647) * 2 - 1;
const raw = new Float32Array(W + 2 * PAD + 1000).map((_, i) => rnd() * Math.exp(-((i % 1999) / 300)));
const src = new Float32Array(raw.length); let acc = 0;
for (let i = 0; i < raw.length; i++) { acc = acc * 0.97 + raw[i]; src[i] = acc; }   // low-passed, like music
const mix = src.slice(PAD, PAD + W);
const shifted = new Float32Array(src.length); for (let i = LAG; i < src.length; i++) shifted[i] = src[i - LAG];
const stems = [0.4, 0.3, 0.2, 0.1].map((g) => shifted.map((v) => v * g));
const got = stemLagWindow(mix, stems, sr) * sr;
assert.ok(Math.abs(got - LAG) < 1.01, `lag found ${got}`);
console.log("dsp worker ok");
