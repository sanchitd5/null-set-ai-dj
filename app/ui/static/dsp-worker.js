// AI Music Brain - DSP worker: heavy per-song analysis off the main thread.
//
// Measured on the main thread (2026-09-27): stem lag search 74 ms per song load,
// long enough to make an audio-clock booking (made 150-200 ms ahead) late.
// Everything else measured under 10 ms and stays on the main thread.
//
// Messages: {id, op: "stemLag", sr, at, mix: Float32Array, stems: [Float32Array x4]}
//   mix / stems are the same window of the song (mix from `at`, stems from
//   `at - 4096` so lags of +-4096 samples fit). Reply {id, lag} (seconds).
"use strict";

function stemLagWindow(m, parts, sr) {
  const PAD = 4096, W = Math.min(m.length, Math.floor(3 * sr));
  const sum = (i) => { let s = 0; for (let k = 0; k < parts.length; k++) s += parts[k][i] || 0; return s; };
  const score = (lag, step) => {
    let dot = 0, a2 = 0, b2 = 0;
    for (let i = 0; i < W; i += step) {
      const a = m[i], b = sum(i + PAD + lag);
      dot += a * b; a2 += a * a; b2 += b * b;
    }
    return a2 > 0 && b2 > 0 ? dot / Math.sqrt(a2 * b2) : -1;
  };
  let best = 0, bestS = -2;
  for (let lag = -PAD; lag <= PAD; lag += 16) { const s = score(lag, 8); if (s > bestS) { bestS = s; best = lag; } }
  for (let lag = best - 16; lag <= best + 16; lag++) { const s = score(lag, 1); if (s > bestS) { bestS = s; best = lag; } }
  return bestS > 0.5 ? best / sr : 0;
}

// Peak envelope of each stem, `bins` per second: [Float32Array per stem].
function stemPeaks(stems, sr, bins) {
  const step = Math.max(1, Math.round(sr / bins));
  return stems.map((x) => {
    const n = Math.ceil(x.length / step), out = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      let m = 0;
      for (let k = i * step, e = Math.min(x.length, k + step); k < e; k++) { const v = x[k] < 0 ? -x[k] : x[k]; if (v > m) m = v; }
      out[i] = m;
    }
    return out;
  });
}

self.onmessage = (e) => {
  const { id, op } = e.data || {};
  try {
    if (op === "stemLag") self.postMessage({ id, lag: stemLagWindow(e.data.mix, e.data.stems, e.data.sr) });
    else if (op === "peaks") {
      const peaks = stemPeaks(e.data.stems, e.data.sr, e.data.bins);
      self.postMessage({ id, peaks }, peaks.map((p) => p.buffer));
    }
    else self.postMessage({ id, error: `unknown op ${op}` });
  } catch (err) {
    self.postMessage({ id, error: String(err && err.message || err) });
  }
};

if (typeof module !== "undefined" && module.exports) module.exports = { stemLagWindow, stemPeaks };
