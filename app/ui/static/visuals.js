// Full-screen visuals: one fixed canvas over the console that answers the
// music. Pure presentation, like mascot.js: it listens to the events the
// engines already emit and taps the master output read-only.
//
//   "ai-cue" drop        flash + shockwave + particle burst on the drop downbeat
//   "ai-cue" transition  a colour sweep from the outgoing deck's side to B's
//   "ai-cue" line        a scan line (a new layer arrives, e.g. B's rap)
//   "ai-activity"        stem moves: edge glow; hold loop: a breathing frame
//   master low band      a soft vignette pulse on every kick
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
    if (fx && reduced) { fx.still = true; fx.dur = Math.max(fx.dur, 1.2); }
    return fx;
  }
  // Pure: attack/decay envelope, t and dur in seconds -> 0..1.
  function env(t, dur, attack = 0.06) {
    if (!(t >= 0) || !(t < dur)) return 0;
    if (t < attack) return t / attack;
    const k = (t - attack) / (dur - attack);
    return (1 - k) * (1 - k);
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { effectFor, env, FLASH_GAP_S };
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
  function spawn(fx) {
    if (!enabled || !fx || document.hidden) return;
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
    if (ms < 4) spawn(fx); else setTimeout(() => spawn(fx), ms);
  }

  root.addEventListener("ai-cue", (e) => {
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
  const DRAW = { drop: drawDrop, sweep: drawSweep, scan: drawScan,
                 edge: (fx, t, c) => edgeGlow(c, 0.22 * env(t, fx.dur)) };

  // ---- loop: runs only while something is on screen ------------------------
  let raf = 0, lastT = 0;
  function wake() {
    if (raf || !enabled || document.hidden) return;
    lastT = nowS();
    raf = requestAnimationFrame(frame);
  }
  function frame() {
    raf = 0;
    const now = nowS(), dt = Math.min(0.1, now - lastT);
    lastT = now;
    tap();
    kick(dt);
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
  // a deck starting to play wakes the kick pulse (no per-deck hook needed)
  setInterval(() => { if (!raf && audibleDeck()) wake(); }, 1000);

  setEnabled(enabled);
  root.nulVisuals = { setEnabled, get enabled() { return enabled; }, spawn: (kind, deck) => spawn(effectFor({ kind, deck }, reduced)) };
})(typeof window !== "undefined" ? window : globalThis);
