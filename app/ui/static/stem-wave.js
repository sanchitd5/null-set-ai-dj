// AI Music Brain - stem-shaded deck waveforms.
//
// Once a deck's stems are loaded, its waveform is drawn as four stacked layers,
// each stem in its own shade of the deck colour (bass deepest, drums, synths,
// vocals brightest), following WaveSurfer's scroll and zoom. A muted stem is
// drawn dim in real time, so every stem move and remix shows on the wave.
// Envelopes are computed in the DSP worker (dsp-worker.js), 100 bins/s.
//
// Depends on globals: audioCtx, decks, waveformA/B (app.js), dsp() (deck-controller.js).
(function (root) {
  "use strict";
  const ORDER = ["bass", "drums", "other", "vocals"];              // bottom -> top of the stack
  const SHADE = { bass: 0.32, drums: 0.6, other: 0.95, vocals: 1.55 }; // < 1 darker, > 1 toward white
  const BINS = 100;

  // Pure: the stack at one column: [{stem, y0, h}] (0..1 of half-height), normalised by the song's loudest mix.
  function column(levels, gains, norm) {
    let y = 0;
    const out = [];
    for (const n of ORDER) {
      const h = (levels[n] || 0) * (gains[n] == null ? 1 : gains[n]) / (norm || 1);
      out.push({ stem: n, y0: y, h });
      y += h;
    }
    return out;
  }
  const core = { column, ORDER };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof root.document === "undefined" || typeof audioCtx === "undefined") return;

  const state = { a: null, b: null };   // {peaks: {name: Float32Array}, norm, dur, key}

  function shade(hex, f) {
    const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex.trim());
    if (!m) return hex;
    const c = [1, 2, 3].map((i) => parseInt(m[i], 16));
    const v = f <= 1 ? c.map((x) => x * f) : c.map((x) => x + (255 - x) * Math.min(1, f - 1));
    return `rgb(${v.map(Math.round).join(",")})`;
  }
  function colours(id) {
    const acc = getComputedStyle(document.getElementById(`deck-${id}`) || document.body).getPropertyValue("--accent").trim()
      || (id === "a" ? "#00ff66" : "#ff2bd6");
    return Object.fromEntries(ORDER.map((n) => [n, shade(acc, SHADE[n])]));
  }

  async function compute(id) {
    const d = root.decks && root.decks[id];
    const st = d && (d._nativeStems || d.stems);
    if (!st || !st.drums) return;
    const key = st.drums;
    if (state[id] && state[id].key === key) return;
    const sr = st.drums.sampleRate;
    const chans = ORDER.map((n) => st[n].getChannelData(0).slice());
    const w = typeof dsp === "function" ? dsp() : null;
    let peaks;
    if (w) {
      peaks = await new Promise((resolve) => {
        const idn = Math.random();
        const on = (e) => { if (e.data.id === idn) { w.removeEventListener("message", on); resolve(e.data.peaks); } };
        w.addEventListener("message", on);
        w.postMessage({ id: idn, op: "peaks", stems: chans, sr, bins: BINS }, chans.map((c) => c.buffer));
      });
    } else return;
    if (!peaks) return;
    const byName = Object.fromEntries(ORDER.map((n, i) => [n, peaks[i]]));
    // norm: the loudest summed column, so a full-mix peak reaches the edge
    let norm = 1e-6;
    for (let i = 0; i < peaks[0].length; i++) { let s = 0; for (const p of peaks) s += p[i] || 0; if (s > norm) norm = s; }
    state[id] = { peaks: byName, norm, dur: st.drums.duration / (st.ratio || 1), key };
    const ws = id === "a" ? root.waveformA : root.waveformB;
    // the stem layers replace WaveSurfer's own wave (its cursor and regions stay)
    if (ws) ws.setOptions({ waveColor: "rgba(0,0,0,0)", progressColor: "rgba(0,0,0,0)" });
  }

  const canvases = {};
  function canvasFor(id) {
    if (canvases[id]) return canvases[id];
    const host = document.getElementById(`waveform-${id}`);
    if (!host) return null;
    const c = document.createElement("canvas");
    c.className = "stem-wave";
    host.prepend(c);
    return (canvases[id] = c);
  }
  function reset(id) {
    state[id] = null;
    const c = canvases[id];
    if (c) c.getContext("2d").clearRect(0, 0, c.width, c.height);
    const ws = id === "a" ? root.waveformA : root.waveformB;
    if (ws) ws.setOptions(id === "a" ? { waveColor: "#00803a", progressColor: "#00ff66" } : { waveColor: "#8a1a76", progressColor: "#ff2bd6" });
  }

  function draw(id) {
    const s = state[id], d = root.decks && root.decks[id];
    const ws = id === "a" ? root.waveformA : root.waveformB;
    const c = canvasFor(id);
    if (!s || !d || !ws || !c) return;
    const W = c.clientWidth, H = c.clientHeight, dpr = root.devicePixelRatio || 1;
    if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
    const ctx = c.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    // visible time range from WaveSurfer's scroll / zoom
    const wrap = ws.getWrapper ? ws.getWrapper() : null;
    const total = wrap ? wrap.scrollWidth || W : W;
    const left = ws.getScroll ? ws.getScroll() : 0;
    const dur = s.dur;
    const pos = d._currentPosition ? d._currentPosition() : 0;
    const col = colours(id);
    const gains = d._meterGain ? Object.fromEntries(ORDER.map((n) => [n, d._meterGain[n].gain.value]))
      : d.stemState ? Object.fromEntries(ORDER.map((n) => [n, d.stemGain[n].gain.value + (n === "vocals" ? d.vocalBusGain.gain.value : 0)]))
      : {};
    const mid = H / 2, half = H / 2 - 1;
    for (let x = 0; x < W; x++) {
      const t = ((left + x) / total) * dur;
      const i = Math.floor(t * BINS);
      if (i < 0 || i >= s.peaks.bass.length) continue;
      const lv = {};
      for (const n of ORDER) lv[n] = s.peaks[n][i];
      const played = t < pos;
      for (const seg of column(lv, gains, s.norm)) {
        if (seg.h <= 0.002) continue;
        ctx.globalAlpha = played ? 0.45 : 0.95;
        ctx.fillStyle = col[seg.stem];
        const y0 = seg.y0 * half, h = Math.max(0.5, seg.h * half);
        ctx.fillRect(x, mid - y0 - h, 1, h);            // mirrored around the centre line
        ctx.fillRect(x, mid + y0, 1, h);
      }
    }
    ctx.globalAlpha = 1;
  }

  let last = 0;
  function frame(t) {
    requestAnimationFrame(frame);
    if (t - last < 33) return;
    last = t;
    for (const id of ["a", "b"]) {
      const d = root.decks && root.decks[id];
      if (d && (d._nativeStems || d.stems) && !state[id]) compute(id);
      if (d && !d.stems && state[id]) reset(id);
      if (state[id]) draw(id);
    }
  }
  requestAnimationFrame(frame);
  root.stemWave = { core, reset };
})(typeof window !== "undefined" ? window : globalThis);
