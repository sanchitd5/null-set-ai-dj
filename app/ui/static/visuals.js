// Full-screen visuals: one fixed canvas over the console that answers the
// music. Pure presentation, like mascot.js: it listens to the events the
// engines already emit and taps the master output read-only.
//
//   "ai-cue" drop        flash + shockwave + particle burst on the drop downbeat
//   "ai-cue" transition  a colour sweep from the outgoing deck's side to B's
//   "ai-cue" line        a scan line (a new layer arrives, e.g. B's rap)
//   "ai-activity"        stem moves: edge glow; hold loop: a breathing frame
//   master low band      a soft vignette pulse on every kick
//   energy high point    a gentle bloom when the on-air deck plays into one of
//                        its song's peaks (top 10 %, rising, 32+ bars apart)
//
// AI gate: every effect, the kick pulse included, runs ONLY while the AI is
// driving the decks (window.autopilotState.active). Hand mixing stays dark.
//
// Cues carry an audio-clock time (audioCtx.currentTime); the effect is
// scheduled to start on it, so the drop visual lands on the drop.
// PERFORMANCE_AUDIT rules: one canvas, one rAF loop that sleeps when nothing
// is on screen, half-resolution backing store, no DOM writes per frame.
// prefers-reduced-motion: no flash, no movement; a soft still glow instead.
// Toggle: the VFX button in the top bar, remembered in localStorage.
(function (root) {
  "use strict";

  const MAX_EFFECTS = 12;
  const FLASH_GAP_S = 1;          // never more than one flash per second (WCAG 2.3.1)

  // Pure: what one cue draws. deck = the incoming deck ("a" / "b").
  function effectFor(cue, reduced) {
    if (!cue || typeof cue !== "object") return null;
    const deck = cue.deck === "a" || cue.deck === "b" ? cue.deck : null;
    const bar = Number.isFinite(cue.bar) && cue.bar > 0 ? cue.bar : 1.875;   // 128 BPM
    let fx = null;
    if (cue.kind === "drop") fx = { type: "drop", deck, dur: 1.6 };
    else if (cue.kind === "transition") fx = { type: "sweep", deck, dur: Math.min(3, Math.max(1.2, bar * 2)) };
    else if (cue.kind === "line") fx = { type: "scan", deck, dur: 0.9 };
    else if (cue.kind === "stem-move") fx = { type: "edge", deck, dur: 0.8 };
    else if (cue.kind === "peak") fx = { type: "peak", deck, dur: 2.4 };
    if (fx && reduced) { fx.still = true; fx.dur = Math.max(fx.dur, 1.2); }
    return fx;
  }

  // Pure: the AI gate. state = window.autopilotState (read-only getters).
  function aiDriving(state) {
    return !!state && state.active === true;
  }

  // Pure: a song's energy high points, in song seconds, ascending.
  // curve / times = analysis.energy_curve / energy_times. A high point is a
  // local max (a plateau counts once, at its first sample) that sits in the
  // song's top 10 %, is reached by rising (above its left neighbour, and the
  // energy climbed at least 10 % of the song's range within the last 8 bars,
  // so a wiggle inside a long loud section is not a new high point), and is
  // at least 32 bars from any stronger high point.
  const PEAK_PCT = 0.9, PEAK_GAP_BARS = 32, PEAK_RISE_BARS = 8, PEAK_RISE_FRAC = 0.1;
  function energyPeaks(curve, times, bpm) {
    if (!Array.isArray(curve) || !Array.isArray(times)) return [];
    const bar = 240 / (Number.isFinite(bpm) && bpm > 0 ? bpm : 128);
    const v = [], t = [];
    for (let i = 0; i < Math.min(curve.length, times.length); i++) {
      if (!Number.isFinite(curve[i]) || !Number.isFinite(times[i])) continue;
      if (t.length && times[i] < t[t.length - 1]) return [];    // times must ascend
      v.push(curve[i]); t.push(times[i]);
    }
    if (v.length < 3) return [];
    const sorted = v.slice().sort((x, y) => x - y);
    if (!(sorted[sorted.length - 1] > sorted[0])) return [];    // flat song: no high point
    const thr = sorted[Math.floor(PEAK_PCT * (sorted.length - 1))];
    const rise = PEAK_RISE_FRAC * (sorted[sorted.length - 1] - sorted[0]);
    const cand = [];
    for (let i = 1; i < v.length - 1; i++) {
      if (v[i] < thr || !(v[i] > v[i - 1]) || v[i + 1] > v[i]) continue;
      let lo = Infinity;
      for (let j = i - 1; j >= 0 && t[i] - t[j] <= PEAK_RISE_BARS * bar; j--) lo = Math.min(lo, v[j]);
      if (v[i] - lo >= rise) cand.push(i);
    }
    cand.sort((x, y) => v[y] - v[x] || t[x] - t[y]);             // strongest first
    const gap = PEAK_GAP_BARS * bar, out = [];
    for (const i of cand) if (out.every((s) => Math.abs(s - t[i]) >= gap)) out.push(t[i]);
    return out.sort((x, y) => x - y);
  }
  // Pure: index of the first peak strictly after pos (binary search).
  function nextPeakIdx(peaks, pos) {
    let lo = 0, hi = peaks.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (peaks[m] <= pos) lo = m + 1; else hi = m; }
    return lo;
  }
  // Pure: move one deck's pointer to playback position pos; O(1) per frame.
  // tr = {peaks, idx, prev}. Returns the peak crossed since the last step, or
  // null. A backwards move or a jump (seek, loop, beat jump, load) re-finds
  // the pointer without firing.
  const SEEK_JUMP_S = 1.5;
  function stepPeaks(tr, pos) {
    if (!tr || !Array.isArray(tr.peaks) || !Number.isFinite(pos)) return null;
    const prev = tr.prev;
    tr.prev = pos;
    if (!Number.isFinite(prev) || pos < prev || pos - prev > SEEK_JUMP_S) {
      tr.idx = nextPeakIdx(tr.peaks, pos);
      return null;
    }
    if (tr.idx < tr.peaks.length && tr.peaks[tr.idx] <= pos) {
      const hit = tr.peaks[tr.idx];
      tr.idx = nextPeakIdx(tr.peaks, pos);
      return hit;
    }
    return null;
  }
  // Pure: drop/peak de-dup. No peak within DROP_PEAK_GAP_S of a drop effect,
  // before or after (drops are booked ahead, so their future times count).
  const DROP_PEAK_GAP_S = 4;
  function peakAllowed(now, dropTimes, gap = DROP_PEAK_GAP_S) {
    return (dropTimes || []).every((d) => !(Math.abs(now - d) < gap));
  }
  // Pure: attack/decay envelope, t and dur in seconds -> 0..1.
  function env(t, dur, attack = 0.06) {
    if (!(t >= 0) || !(t < dur)) return 0;
    if (t < attack) return t / attack;
    const k = (t - attack) / (dur - attack);
    return (1 - k) * (1 - k);
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { effectFor, env, FLASH_GAP_S, aiDriving, energyPeaks, nextPeakIdx, stepPeaks,
                       peakAllowed, DROP_PEAK_GAP_S, SEEK_JUMP_S };
  }
  if (typeof document === "undefined") return;

  // ---- DOM --------------------------------------------------------------------
  const cv = document.createElement("canvas");
  cv.className = "vfx";
  cv.setAttribute("aria-hidden", "true");
  document.body.appendChild(cv);
  const cx = cv.getContext("2d");
  if (!cx) return;

  const btn = document.getElementById("vfx-toggle");
  const KEY = "nul.vfx";
  let enabled = true;
  try { enabled = localStorage.getItem(KEY) !== "off"; } catch (_) { /* private mode */ }
  const mq = root.matchMedia ? root.matchMedia("(prefers-reduced-motion: reduce)") : null;
  let reduced = !!(mq && mq.matches);
  if (mq && mq.addEventListener) mq.addEventListener("change", (e) => { reduced = e.matches; });

  function setEnabled(on) {
    enabled = on;
    try { localStorage.setItem(KEY, on ? "on" : "off"); } catch (_) { /* quota */ }
    if (btn) { btn.setAttribute("aria-pressed", String(on)); btn.classList.toggle("vfx-on", on); }
    if (!on) { effects.length = 0; clear(); } else wake();
  }
  if (btn) btn.addEventListener("click", () => setEnabled(!enabled));

  const css = getComputedStyle(document.documentElement);
  const col = (name, fb) => (css.getPropertyValue(name) || "").trim() || fb;
  const COLOR = { a: col("--a", "#00ff66"), b: col("--b", "#ff2bd6"), ai: col("--ai", "#00e5ff") };
  const colorOf = (deck) => COLOR[deck] || COLOR.ai;

  let W = 0, H = 0;
  function resize() {
    const scale = Math.min(1, (root.devicePixelRatio || 1)) * 0.5;   // half-res: glows hide it
    W = Math.max(1, Math.round(root.innerWidth * scale));
    H = Math.max(1, Math.round(root.innerHeight * scale));
    cv.width = W; cv.height = H;
  }
  resize();
  let resizePending = false;
  root.addEventListener("resize", () => {
    if (resizePending) return;
    resizePending = true;
    requestAnimationFrame(() => { resizePending = false; resize(); wake(); });
  });
  function clear() { cx.clearRect(0, 0, W, H); }

  // ---- effects ----------------------------------------------------------------
  const effects = [];
  let lastFlash = -Infinity;
  const nowS = () => performance.now() / 1000;
  const aiOn = () => aiDriving(root.autopilotState);
  const dropTimes = [];                           // performance-clock seconds, booked + fired
  function spawn(fx) {
    if (!enabled || !fx || document.hidden || !aiOn()) return;
    fx.t0 = nowS();
    if (fx.type === "drop") {
      fx.flash = !fx.still && fx.t0 - lastFlash >= FLASH_GAP_S;
      if (fx.flash) lastFlash = fx.t0;
      if (!fx.still) {
        const n = 56;
        fx.ang = new Float32Array(n); fx.spd = new Float32Array(n);
        for (let i = 0; i < n; i++) { fx.ang[i] = Math.random() * Math.PI * 2; fx.spd[i] = 0.35 + Math.random() * 0.65; }
      }
    }
    effects.push(fx);
    while (effects.length > MAX_EFFECTS) effects.shift();
    wake();
  }
  function schedule(fx, at) {
    if (!fx) return;
    const ms = Number.isFinite(at) && typeof audioCtx !== "undefined"
      ? Math.max(0, (at - audioCtx.currentTime) * 1000) : 0;
    if (ms > 60000) return;                       // stale or bogus clock: skip
    if (fx.type === "drop") {
      const now = nowS();
      while (dropTimes.length && (dropTimes.length > 8 || dropTimes[0] < now - 10)) dropTimes.shift();
      dropTimes.push(now + ms / 1000);
    }
    if (ms < 4) spawn(fx); else setTimeout(() => spawn(fx), ms);
  }

  root.addEventListener("ai-cue", (e) => {
    if (!enabled || !aiOn()) return;
    const d = e.detail || {};
    schedule(effectFor(d, reduced), d.at);
  });
  let holding = false;
  root.addEventListener("ai-activity", (e) => {
    const d = e.detail || {};
    if (d.kind === "decision") { holding = d.action === "holdloop"; if (holding) wake(); }
    else if (d.kind === "stem-move") spawn(effectFor({ kind: "stem-move", deck: d.deck }, reduced));
  });

  // ---- master tap: kick pulse ------------------------------------------------
  let an = null, bins = null, lowN = 3, prevLow = 0, pulse = 0;
  function tap() {
    if (an || typeof audioCtx === "undefined") return;
    const src = root.masterOut || (typeof masterGain !== "undefined" ? masterGain : null);
    if (!src) return;
    an = audioCtx.createAnalyser();
    an.fftSize = 1024;
    an.smoothingTimeConstant = 0.5;
    src.connect(an);                               // analyser has no output: read-only
    bins = new Uint8Array(an.frequencyBinCount);
    lowN = Math.max(1, Math.round(150 / (audioCtx.sampleRate / an.fftSize)));
  }
  function audibleDeck() {
    const ds = root.decks || {};
    const a = ds.a && ds.a.playing, b = ds.b && ds.b.playing;
    return a && !b ? "a" : b && !a ? "b" : a && b ? "ai" : null;
  }
  function kick(dt) {
    pulse *= Math.exp(-dt * 7);
    if (reduced || !an) return;
    an.getByteFrequencyData(bins);
    let s = 0;
    for (let i = 1; i <= lowN; i++) s += bins[i];
    const low = s / (lowN * 255);
    if (low - prevLow > 0.07 && low > 0.45) pulse = Math.min(1, pulse + low);
    prevLow = low;
  }

  // ---- energy high points: one pointer per deck, peaks found once per load --
  const tracks = { a: null, b: null };
  function checkPeaks() {
    const ds = root.decks || {};
    for (const id of ["a", "b"]) {
      const d = ds[id], an = d && d.analysis;
      if (!an || typeof d._currentPosition !== "function") { tracks[id] = null; continue; }
      let tr = tracks[id];
      if (!tr || tr.analysis !== an) {             // new track loaded: precompute once
        tr = tracks[id] = { analysis: an, idx: 0, prev: NaN,
                            peaks: energyPeaks(an.energy_curve, an.energy_times, d.bpm || an.bpm) };
      }
      if (!tr.peaks.length) continue;
      const g = d.crossfaderGain && d.crossfaderGain.gain;
      if (!d.playing || !g || !(g.value > 0.05)) { tr.prev = NaN; continue; }   // off air
      if (stepPeaks(tr, d._currentPosition()) !== null && peakAllowed(nowS(), dropTimes)) {
        spawn(effectFor({ kind: "peak", deck: id }, reduced));
      }
    }
  }

  // ---- drawing ------------------------------------------------------------------
  function rgba(hex, a) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex);
    if (!m) return `rgba(0,229,255,${a})`;
    const n = parseInt(m[1], 16);
    return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
  }
  function edgeGlow(color, a) {
    if (a <= 0.003) return;
    const g = cx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.35, W / 2, H / 2, Math.hypot(W, H) / 2);
    g.addColorStop(0, rgba(color, 0));
    g.addColorStop(1, rgba(color, a));
    cx.fillStyle = g;
    cx.fillRect(0, 0, W, H);
  }
  function drawDrop(fx, t, c) {
    const e = env(t, fx.dur);
    if (fx.still) { edgeGlow(c, 0.35 * e); return; }
    if (fx.flash && t < 0.3) { cx.fillStyle = rgba(c, 0.28 * (1 - t / 0.3)); cx.fillRect(0, 0, W, H); }
    const k = t / fx.dur, ease = 1 - Math.pow(1 - k, 3), R = Math.hypot(W, H) / 2;
    cx.strokeStyle = rgba(c, 0.75 * (1 - k));
    cx.lineWidth = Math.max(1, 14 * (1 - k));
    cx.beginPath(); cx.arc(W / 2, H / 2, R * ease, 0, Math.PI * 2); cx.stroke();
    cx.fillStyle = rgba(c, 0.9 * (1 - k));
    const sz = Math.max(1, 3 * (1 - k));
    for (let i = 0; i < fx.ang.length; i++) {
      const r = R * 0.9 * ease * fx.spd[i];
      cx.fillRect(W / 2 + Math.cos(fx.ang[i]) * r, H / 2 + Math.sin(fx.ang[i]) * r, sz, sz);
    }
    edgeGlow(c, 0.3 * e);
  }
  function drawSweep(fx, t, c) {
    const e = env(t, fx.dur, 0.2);
    if (fx.still) { edgeGlow(c, 0.25 * e); return; }
    // B enters from its own side: deck A lives left, deck B right
    const k = t / fx.dur, fromRight = fx.deck === "b";
    const x = (fromRight ? 1 - k : k) * (W * 1.4) - W * 0.2, bw = W * 0.35;
    const g = cx.createLinearGradient(x - bw, 0, x + bw, 0);
    g.addColorStop(0, rgba(c, 0)); g.addColorStop(0.5, rgba(c, 0.22 * e + 0.05)); g.addColorStop(1, rgba(c, 0));
    cx.fillStyle = g;
    cx.fillRect(0, 0, W, H);
  }
  function drawScan(fx, t, c) {
    const e = env(t, fx.dur);
    if (fx.still) { edgeGlow(c, 0.2 * e); return; }
    const y = (t / fx.dur) * H, bh = Math.max(4, H * 0.04);
    const g = cx.createLinearGradient(0, y - bh, 0, y + bh);
    g.addColorStop(0, rgba(c, 0)); g.addColorStop(0.5, rgba(c, 0.45 * e + 0.1)); g.addColorStop(1, rgba(c, 0));
    cx.fillStyle = g;
    cx.fillRect(0, y - bh, W, bh * 2);
  }
  // gentler than a drop: no flash, no ring, a slow bloom from the centre
  function drawPeak(fx, t, c) {
    const e = env(t, fx.dur, 0.5);
    if (fx.still) { edgeGlow(c, 0.25 * e); return; }
    const R = (Math.hypot(W, H) / 2) * (0.6 + 0.4 * (t / fx.dur));
    const g = cx.createRadialGradient(W / 2, H / 2, 0, W / 2, H / 2, R);
    g.addColorStop(0, rgba(c, 0.1 * e)); g.addColorStop(1, rgba(c, 0));
    cx.fillStyle = g;
    cx.fillRect(0, 0, W, H);
    edgeGlow(c, 0.28 * e);
  }
  const DRAW = { drop: drawDrop, sweep: drawSweep, scan: drawScan, peak: drawPeak,
                 edge: (fx, t, c) => edgeGlow(c, 0.22 * env(t, fx.dur)) };

  // ---- loop: runs only while something is on screen ------------------------
  let raf = 0, lastT = 0;
  function wake() {
    if (raf || !enabled || document.hidden || !aiOn()) return;
    lastT = nowS();
    raf = requestAnimationFrame(frame);
  }
  function frame() {
    raf = 0;
    if (!aiOn()) { effects.length = 0; pulse = 0; clear(); return; }   // user took over: dark, asleep
    const now = nowS(), dt = Math.min(0.1, now - lastT);
    lastT = now;
    tap();
    kick(dt);
    checkPeaks();
    clear();
    const deck = audibleDeck();
    if (deck && pulse > 0.01) edgeGlow(colorOf(deck), 0.14 * pulse);
    if (holding) {
      const beat = 60 / (((root.decks && deck && root.decks[deck]) || {}).bpm || 124);
      edgeGlow(COLOR.ai, reduced ? 0.12 : 0.08 + 0.08 * (0.5 + 0.5 * Math.cos((now / beat) * Math.PI * 2)));
    }
    for (let i = effects.length - 1; i >= 0; i--) {
      const fx = effects[i], t = now - fx.t0;
      if (t >= fx.dur) { effects.splice(i, 1); continue; }
      DRAW[fx.type](fx, t, fx.type === "scan" || fx.type === "edge" ? COLOR.ai : colorOf(fx.deck));
    }
    if (effects.length || holding || deck || pulse > 0.01) wake();
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { if (raf) cancelAnimationFrame(raf); raf = 0; effects.length = 0; clear(); }
    else wake();
  });
  // the AI taking over (or a deck starting under it) wakes the loop; wake() gates on aiOn()
  setInterval(() => { if (!raf && audibleDeck()) wake(); }, 1000);

  setEnabled(enabled);
  root.nulVisuals = { setEnabled, get enabled() { return enabled; }, spawn: (kind, deck) => spawn(effectFor({ kind, deck }, reduced)) };
})(typeof window !== "undefined" ? window : globalThis);
