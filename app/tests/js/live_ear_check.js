// Node check for the pure live-ear core (app/ui/static/live-ear.js). Run by test_live_ear.py.
const assert = require("assert");
const { gridOffsetMs, loopLengthErrMs, seamClickRatio, flags, wavBytes } = require("../../ui/static/live-ear.js");

const bar = 2, db = Array.from({ length: 40 }, (_, i) => i * bar);
assert.strictEqual(gridOffsetMs(db, 10), 0);
assert.ok(Math.abs(gridOffsetMs(db, 10.03) - 30) < 1e-6);
assert.strictEqual(gridOffsetMs([], 3), null);
assert.strictEqual(loopLengthErrMs(db, 10, 8, bar), 0);
assert.ok(Math.abs(loopLengthErrMs(db, 10, 8, 2.005) - 40) < 1e-6);   // tempo off by 0.25 %
assert.strictEqual(loopLengthErrMs(db, 76, 8, bar), null);               // runs past the grid

// steady tone with a hard level jump at sample 8000 -> big ratio there only
const rate = 16000, s = new Float32Array(rate);
for (let i = 0; i < s.length; i++) s[i] = (i < 8000 ? 0.1 : 0.8) * Math.sin(i / 3) + 0.001 * Math.sin(i * 1.7);
assert.ok(seamClickRatio(s, [8000], 80) > 10);
assert.ok(seamClickRatio(s, [4000], 80) < 4);

assert.deepStrictEqual(flags({ seam_shift_ms: 5, grid_err_ms: 3 }), []);
assert.deepStrictEqual(flags({ grid_err_ms: 40, secs_looping: 90 }), ["loop_length", "fatigue"]);
assert.deepStrictEqual(flags({ peak_dbfs: 0 }), ["clipping"]);

const w = wavBytes(new Float32Array([0, 1, -1]), 16000);
assert.strictEqual(String.fromCharCode(...w.slice(0, 4)), "RIFF");
assert.strictEqual(w.length, 44 + 6);
console.log("live ear core ok");

// ---- silent pre-check -------------------------------------------------------
{
  const { seamScore } = require("../../ui/static/live-ear.js");
  const sr = 8000, bar = 1.0, secs = 40, x = new Float32Array(sr * secs);
  // 1-bar pattern: kick on the downbeat, quieter hits on beats 2-4, plus a slow level swell every 4 bars
  for (let i = 0; i < x.length; i++) {
    const t = i / sr, ph = t % bar, beat = ph % 0.25;
    const hit = Math.exp(-beat * 60) * (ph < 0.25 ? 1 : 0.4);
    x[i] = hit * Math.sin(2 * Math.PI * 80 * t) * (0.6 + 0.4 * Math.sin((2 * Math.PI * t) / (4 * bar)));
  }
  const good = seamScore(x, sr, 8, 24, bar);        // 16 bars, 4-bar swell aligned
  const off = seamScore(x, sr, 8.37, 24, bar);      // start mid-beat
  assert.ok(good.score > 0.9, `aligned seam ${good.score}`);
  assert.ok(off.score < good.score - 0.2, `off-grid seam ${off.score} vs ${good.score}`);
  const nearEnd = seamScore(x, sr, 20, 39.9, bar);   // no audio after `end`: compares what precedes
  assert.ok(nearEnd.score >= 0 && nearEnd.score <= 1);
}
console.log("silent pre-check ok");
