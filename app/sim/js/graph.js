// What the console's audio graph would have played, sampled on the virtual clock.
//
// Every 250 ms of virtual time the sampler walks the recording Web Audio graph backwards from
// the destination: each live BufferSource contributes the measured power of the song / stem it is
// playing (the synthetic audio carries the real stems' energy, see app/sim/synth.py) times the
// gains along its path (deck stem gains, EQ shelves, volume, crossfader, master). Two bands are
// kept: LOW (the sub the kick and bass own, below ~120 Hz) and AUDIBLE (above 150 Hz).
//
// From the samples: per-deck level, dead air (the master silent while a source that would have
// been audible is muted), bass overlap (two decks both carrying sub-bass), vocal clash (two decks
// both singing). All of it is estimated from energy, never from ears.
"use strict";

const HOP_S = 0.25;
const BAND_SPLIT_HZ = 150;

// 4th-order Butterworth high-pass, two biquads (the filter stem-moves.js audibleRms uses)
function highpass(x, sr, hz) {
  const w0 = (2 * Math.PI * hz) / sr, cw = Math.cos(w0), sw = Math.sin(w0);
  const stages = [0.5412, 1.3066].map((Q) => {
    const al = sw / (2 * Q), a0 = 1 + al;
    return { b0: ((1 + cw) / 2) / a0, b1: -(1 + cw) / a0, b2: ((1 + cw) / 2) / a0, a1: (-2 * cw) / a0, a2: (1 - al) / a0, x1: 0, x2: 0, y1: 0, y2: 0 };
  });
  const y = new Float32Array(x.length);
  for (let i = 0; i < x.length; i++) {
    let v = x[i];
    for (const s of stages) {
      const o = s.b0 * v + s.b1 * s.x1 + s.b2 * s.x2 - s.a1 * s.y1 - s.a2 * s.y2;
      s.x2 = s.x1; s.x1 = v; s.y2 = s.y1; s.y1 = o; v = o;
    }
    y[i] = v;
  }
  return y;
}

// per-hop power of a decoded buffer: {low[], aud[]} (mean square)
function bufferCurves(buf) {
  if (buf._curves) return buf._curves;
  const x = buf.getChannelData(0), sr = buf.sampleRate, n = Math.max(1, Math.floor(buf.duration / HOP_S));
  const hp = highpass(x, sr, BAND_SPLIT_HZ);
  const per = Math.max(1, Math.floor(HOP_S * sr));
  const low = new Float32Array(n), aud = new Float32Array(n);
  for (let k = 0; k < n; k++) {
    let f = 0, a = 0;
    const s0 = k * per, s1 = Math.min(x.length, s0 + per);
    for (let i = s0; i < s1; i++) { f += x[i] * x[i]; a += hp[i] * hp[i]; }
    const m = Math.max(1, s1 - s0);
    aud[k] = a / m; low[k] = Math.max(0, f / m - a / m);
  }
  return (buf._curves = { low, aud });
}

function nodeTransfer(node, t) {
  // -> [lowMul, audMul] (power) for the band each node passes
  switch (node._kind) {
    case "gain": { const g = node.gain.valueAt(t); return [g * g, g * g]; }
    case "biquad": {
      const f = node.type;
      if (f === "lowshelf") return [Math.pow(10, node.gain.valueAt(t) / 10), 1];
      if (f === "highpass") return [0.02, 1];
      if (f === "lowpass") return [1, 0.02];
      return [1, 1];
    }
    case "panner": return [1, 1];
    default: return [1, 1];
  }
}

class Sampler {
  constructor(env) { this.env = env; this.series = []; this.ctx = null; }
  start() {
    const clock = this.env.clock;
    this.timer = clock.setInterval(() => this.sample(clock.now), HOP_S * 1000);
  }
  _deckOfNode() {
    const map = new Map();
    const decks = (this.env.window.decks) || {};
    for (const [id, d] of Object.entries(decks)) {
      const add = (n) => { if (n && typeof n === "object" && n._id) map.set(n, id); };
      ["inputGain", "mixGain", "vocalBusGain", "crossfaderGain", "volumeGain"].forEach((k) => add(d[k]));
      Object.values(d.stemGain || {}).forEach(add); Object.values(d.stemLive || {}).forEach(add);
      (d._layers || []).forEach((r) => add(r.hp));   // layerPieces (artist moves): source -> gain -> hp -> inputGain
    }
    return map;
  }
  sample(t) {
    const ctx = this.env.audio;
    if (!ctx) return;
    const dest = ctx.destination;
    // sources that ended or were disconnected can no longer sound
    if (ctx._nodes.length > 400) ctx._nodes = ctx._nodes.filter((n) => !(n._kind === "source" && (n._sEnded || (n._sStart !== null && n._out.length === 0))));
    // reverse adjacency of the live graph
    const preds = new Map();
    for (const n of ctx._nodes) for (const o of n._out) { let l = preds.get(o); if (!l) preds.set(o, (l = [])); l.push(n); }
    const deckOf = this._deckOfNode();
    const contrib = new Map();   // source -> [lowP, audP]
    const tf = new Map();
    const walk = (node, mLow, mAud, seen) => {
      if (node._kind === "source") {
        if (!node.buffer || !node.aliveAt(t)) return;
        const pos = node.positionAt(t);
        if (pos === null) return;
        const cv = bufferCurves(node.buffer);
        const k = Math.min(cv.aud.length - 1, Math.max(0, Math.floor(pos / HOP_S)));
        const c = contrib.get(node) || [0, 0];
        c[0] += cv.low[k] * mLow; c[1] += cv.aud[k] * mAud;
        contrib.set(node, c);
        return;
      }
      let x = tf.get(node);
      if (!x) { x = nodeTransfer(node, t); tf.set(node, x); }
      const ml = mLow * x[0], ma = mAud * x[1];
      if (ml < 1e-14 && ma < 1e-14) return;
      const ps = preds.get(node);
      if (!ps) return;
      for (const p of ps) { if (seen.has(p)) continue; seen.add(p); walk(p, ml, ma, seen); seen.delete(p); }
    };
    walk(dest, 1, 1, new Set([dest]));
    const s = { t: +t.toFixed(3), P: 0, low: 0, decks: {}, avail: {} };
    for (const [src, [lo, au]] of contrib) {
      s.P += lo + au; s.low += lo;
      const deck = this._sourceDeck(src, deckOf) || "x";
      const stem = (src.buffer.tag && src.buffer.tag.stem) || "mix";
      const d = s.decks[deck] || (s.decks[deck] = { P: 0, low: 0, vocals: 0 });
      d.P += lo + au; d.low += lo;
      if (stem === "vocals") d.vocals += lo + au;
    }
    // what each deck WOULD play with nothing muted: the untouched power of its live sources
    for (const n of ctx._nodes) {
      if (n._kind !== "source" || !n.buffer || !n.aliveAt(t)) continue;
      const pos = n.positionAt(t); if (pos === null) continue;
      const deck = this._sourceDeck(n, deckOf) || "x";
      const cv = bufferCurves(n.buffer);
      const k = Math.min(cv.aud.length - 1, Math.max(0, Math.floor(pos / HOP_S)));
      const stem = (n.buffer.tag && n.buffer.tag.stem) || "mix";
      const a = s.avail[deck] || (s.avail[deck] = { mix: 0, stems: 0 });
      a[stem === "mix" ? "mix" : "stems"] += cv.low[k] + cv.aud[k];
    }
    // the decks themselves: where each is in its song, at what heard tempo, whether it plays
    const decks = this.env.window.decks || {};
    s.deck = {};
    for (const id of ["a", "b"]) {
      const d = decks[id];
      if (!d) continue;
      let rate = 1, pos = 0;
      try { rate = d._playbackRate(); pos = d._currentPosition(); } catch (e) { /* deck without a buffer */ }
      s.deck[id] = { playing: !!d.playing, rate: +rate.toFixed(4), pos: +pos.toFixed(2), bpm: +(+d.bpm || 0).toFixed(2), stems: !!d.stemsReady, tempoStems: !!d.tempoStems };
    }
    this.series.push(s);
    if (this.series.length > 60000) this.series.shift();
  }
  _sourceDeck(src, deckOf) {
    for (const o of src._out) { const d = deckOf.get(o); if (d) return d; }
    for (const o of src._out) for (const o2 of o._out || []) { const d = deckOf.get(o2); if (d) return d; }
    return null;
  }
}

// ---- analysis of a window of samples -----------------------------------------------------------------
const db = (p, ref) => 10 * Math.log10(Math.max(1e-18, p) / Math.max(1e-18, ref));

// t0..t1: the window; ref: reference power (the outgoing deck's level just before the window)
function analyseWindow(series, t0, t1, opts = {}) {
  const w = series.filter((s) => s.t >= t0 && s.t <= t1);
  const before = series.filter((s) => s.t >= t0 - 8 && s.t < t0).map((s) => s.P);
  const ref = opts.ref || (before.length ? before.reduce((a, b) => a + b, 0) / before.length : (w.length ? Math.max(...w.map((s) => s.P)) : 0));
  const out = { window: [t0, t1], samples: w.length, ref_db: ref > 0 ? +db(ref, 1).toFixed(1) : null,
    dead_air_s: 0, min_db: 0, bass_overlap_s: 0, vocal_clash_s: 0, silent_run_s: 0, peak_over_db: 0, holes: [],
    unlocked_overlap_s: 0, in_silent_s: 0, rate_max_pct: { a: 0, b: 0 }, start: null };
  if (!w.length || !(ref > 0)) return out;
  const SIL = 15, QUIET = 6, LOWFLOOR = -20, VOC = -20;
  let run = 0, runStart = null;
  const s0 = w[0];
  out.start = { t: s0.t, deck: s0.deck };
  for (const s of w) {
    // beats of two decks at tempos that do not lock, both audible: a tempo clash
    const da = s.deck && s.deck.a, dbk = s.deck && s.deck.b;
    if (da && dbk && da.playing && dbk.playing && s.decks.a && s.decks.b) {
      const both = db(Math.min(s.decks.a.P, s.decks.b.P), ref) > -25;
      const ea = da.bpm * da.rate, eb = dbk.bpm * dbk.rate;
      if (both && ea > 0 && eb > 0) {
        const gap = Math.min(...[1, 2, 0.5].map((m) => Math.abs(ea / (eb * m) - 1)));
        if (gap > 0.04) out.unlocked_overlap_s += HOP_S;
      }
    }
    for (const id of ["a", "b"]) if (s.deck && s.deck[id]) out.rate_max_pct[id] = Math.max(out.rate_max_pct[id], Math.abs(s.deck[id].rate - 1) * 100);
    if (opts.inDeck && s.t - t0 <= 12 && s.t >= t0) {
      const inP = (s.decks[opts.inDeck] || { P: 0 }).P;
      if (db(inP, ref) < -40) out.in_silent_s += HOP_S;
    }
    const rel = db(s.P, ref);
    out.min_db = Math.min(out.min_db, rel);
    out.peak_over_db = Math.max(out.peak_over_db, rel);
    // dead air: master far under the reference while some deck could have played louder
    const avail = Object.values(s.avail).reduce((a, d) => a + Math.max(d.mix, d.stems), 0);
    const silent = rel < -SIL && (avail <= 0 || db(avail, s.P) > QUIET);
    if (silent) { out.dead_air_s += HOP_S; run += HOP_S; if (runStart === null) runStart = s.t; }
    else { if (run > out.silent_run_s) out.silent_run_s = run; if (run >= 1) out.holes.push([+runStart.toFixed(2), +(runStart + run).toFixed(2)]); run = 0; runStart = null; }
    // one sub-bass owner: both decks carry the sub at the same moment
    const ds = Object.entries(s.decks).filter(([k]) => k === "a" || k === "b");
    if (ds.length === 2) {
      const lo = ds.map(([, d]) => d.low), tot = lo[0] + lo[1];
      if (tot > 0 && db(Math.min(lo[0], lo[1]), ref) > LOWFLOOR && Math.min(lo[0], lo[1]) / tot > 0.15) out.bass_overlap_s += HOP_S;
      const vc = ds.map(([, d]) => d.vocals);
      if (db(Math.min(vc[0], vc[1]), ref) > VOC && Math.min(vc[0], vc[1]) > 0) out.vocal_clash_s += HOP_S;
    }
  }
  if (run > out.silent_run_s) out.silent_run_s = run;
  if (run >= 1 && runStart !== null) out.holes.push([+runStart.toFixed(2), +(runStart + run).toFixed(2)]);
  for (const k of ["dead_air_s", "bass_overlap_s", "vocal_clash_s", "silent_run_s", "unlocked_overlap_s", "in_silent_s"]) out[k] = +out[k].toFixed(2);
  out.rate_max_pct = { a: +out.rate_max_pct.a.toFixed(2), b: +out.rate_max_pct.b.toFixed(2) };
  out.min_db = +out.min_db.toFixed(1);
  return out;
}

module.exports = { Sampler, analyseWindow, bufferCurves, HOP_S };
