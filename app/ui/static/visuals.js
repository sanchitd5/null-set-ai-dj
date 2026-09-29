// Full-screen visuals: one fixed canvas over the console that answers the
// music. Pure presentation, like mascot.js: it listens to the events the
// engines already emit and taps the master output read-only.
//
// Always on (VFX toggle on + a deck playing, AI driving OR hand mixing):
//   bass band            a glow band along the bottom edge driven by the
//                        master's low band (< ~150 Hz): height and brightness
//                        follow the sub energy (fast attack, slow release), a
//                        brighter core on each detected kick, coloured by the
//                        deck(s) carrying the low end (blended mid-transition)
//
// AI only (window.autopilotState.active), dark while the user mixes by hand:
//   "ai-cue" drop        tinted flash + double shockwave + particles + centre
//                        bloom, then beat-synced edge pulses for 2 bars
//   "ai-cue" transition  a sweep from the outgoing deck's side, its colour
//                        blending from the outgoing deck's into the incoming's
//   "ai-cue" line        a scan line (a new layer arrives, e.g. B's rap)
//   "ai-activity"        stem moves: edge glow; hold loop: a breathing frame
//   master kick          an edge vignette on every kick
//   energy high point    a bloom when the on-air deck plays into one of its
//                        song's peaks (top 10 %, rising, 32+ bars apart)
//
// Cues carry an audio-clock time (audioCtx.currentTime); the effect is
// scheduled to start on it, so the drop visual lands on the drop.
// PERFORMANCE_AUDIT rules: one canvas, one rAF loop that sleeps when nothing
// is on screen (nothing playing and the bass band faded out), half-resolution
// backing store, no DOM writes per frame, gradients cached per colour.
// Photosensitivity: at most one flash per second, flashes tinted (never
// white), pulses and kick hits never faster than 3 per second.
// prefers-reduced-motion: no flash, no movement; soft still glows instead, and
// the bass band is a flat glow whose brightness changes slowly.
// Toggle: the VFX button in the top bar, remembered in localStorage. Off means
// nothing is drawn at all, the bass band included.
// Style: ANYMA LOOK (#vfx-anyma-toggle, VISUALS drawer) ADDS the SHOW look's
// cyan motifs (see the ANYMA section) on top of the classic layer, same gates,
// same loop; it never replaces the bass band or the classic effects. SHOW never
// hides this layer either; only the manual STAGE takeover dims it to half.
(function (root) {
  "use strict";

  const MAX_EFFECTS = 12;
  const FLASH_GAP_S = 1;          // never more than one flash per second (WCAG 2.3.1)
  const MIN_PULSE_GAP_S = 1 / 3 + 0.01;   // pulses / kick hits: under 3 per second

  // Pure: beat length for beat-synced pulses, doubled (half-time) until it is
  // slower than 3 per second. Bad input -> 128 BPM.
  function safeBeat(beat) {
    let b = Number.isFinite(beat) && beat > 0 ? beat : 60 / 128;
    while (b < MIN_PULSE_GAP_S) b *= 2;
    return b;
  }
  const DROP_BURST_S = 2.2, DROP_AFTER_BEATS = 8;   // 2 bars of after-pulses

  // Pure: what one cue draws. deck = the incoming deck ("a" / "b").
  function effectFor(cue, reduced) {
    if (!cue || typeof cue !== "object") return null;
    const deck = cue.deck === "a" || cue.deck === "b" ? cue.deck : null;
    const bar = Number.isFinite(cue.bar) && cue.bar > 0 ? cue.bar : 1.875;   // 128 BPM
    let fx = null;
    if (cue.kind === "drop") {
      const beat = safeBeat(bar / 4);
      fx = { type: "drop", deck, beat, dur: Math.min(6, Math.max(DROP_BURST_S, beat * (DROP_AFTER_BEATS + 1))) };
    }
    else if (cue.kind === "transition") fx = { type: "sweep", deck, dur: Math.min(3.5, Math.max(1.4, bar * 2)) };
    else if (cue.kind === "line") fx = { type: "scan", deck, dur: 1.1 };
    else if (cue.kind === "stem-move") fx = { type: "edge", deck, dur: 1 };
    else if (cue.kind === "peak") fx = { type: "peak", deck, dur: 3 };
    if (fx && reduced) {
      fx.still = true;
      fx.dur = fx.type === "drop" ? DROP_BURST_S : Math.max(fx.dur, 1.2);
    }
    return fx;
  }

  // Pure: the AI gate. state = window.autopilotState (read-only getters).
  function aiDriving(state) {
    return !!state && state.active === true;
  }
  // Pure: which layers draw this frame. o = {enabled, hidden, playing,
  // bassAlive, autopilot}. The bass band needs no AI; every other effect does.
  // The bass band keeps drawing while it fades out after the music stops.
  // o.show = window.anymaShow.mode: SHOW never hides this layer (NULL-BOT's bass
  // band and effects always draw); only the manual STAGE takeover dims it.
  function vfxLayers(o) {
    const on = !!o && o.enabled === true && o.hidden !== true;
    return { bass: on && (o.playing === true || o.bassAlive === true), ai: on && aiDriving(o.autopilot) };
  }
  // Pure: the photosensitivity cap. last = time of the last flash (s).
  function flashAllowed(now, last, gap = FLASH_GAP_S) {
    return Number.isFinite(now) && !(now - last < gap);
  }

  // Pure: bass envelope follower. low = master low band 0..1 this frame,
  // dt = frame seconds, now = clock seconds. level follows the low band (fast
  // attack, slow release); hit jumps on a kick (a sharp rise into a loud low
  // band, at most ~3 per second) and decays fast. Reduced motion: slow both
  // ways, no hits. Tiny values snap to 0 so idle decays to exactly zero.
  const BASS_ATTACK_S = 0.03, BASS_RELEASE_S = 0.35, BASS_STILL_S = 0.6;
  const KICK_RISE = 0.07, KICK_MIN = 0.45, KICK_DECAY = 7;
  function bassState() { return { level: 0, hit: 0, prev: 0, lastKick: -Infinity }; }
  function bassFollow(st, low, dt, now, reduced) {
    if (!st) return st;
    const x = Number.isFinite(low) ? Math.min(1, Math.max(0, low)) : 0;
    const d = Number.isFinite(dt) && dt > 0 ? Math.min(0.1, dt) : 0;
    const tau = reduced ? BASS_STILL_S : x > st.level ? BASS_ATTACK_S : BASS_RELEASE_S;
    st.level += (x - st.level) * (1 - Math.exp(-d / tau));
    st.hit *= Math.exp(-d * KICK_DECAY);
    if (!reduced && x - st.prev > KICK_RISE && x > KICK_MIN && !(now - st.lastKick < MIN_PULSE_GAP_S)) {
      st.hit = Math.min(1, st.hit + x);
      st.lastKick = now;
    }
    if (reduced) st.hit = 0;
    st.prev = x;
    if (st.level < 1e-3) st.level = 0;
    if (st.hit < 1e-3) st.hit = 0;
    return st;
  }
  function bassAlive(st) { return !!st && (st.level > 0 || st.hit > 0); }

  // Pure: how much low end one deck puts on the master, from what the deck
  // already exposes (read-only): playing, crossfader and channel gain, the low
  // shelf EQ (dB), and the bass / drums stems when stems are live.
  function deckBass(d) {
    if (!d || !d.playing) return 0;
    const g = (p) => (p && Number.isFinite(p.value) ? Math.max(0, p.value) : 1);
    const xf = g(d.crossfaderGain && d.crossfaderGain.gain), vol = g(d.volumeGain && d.volumeGain.gain);
    const db = d.lowFilter && d.lowFilter.gain && Number.isFinite(d.lowFilter.gain.value) ? d.lowFilter.gain.value : 0;
    const st = d.stemState;
    const stem = st && typeof st === "object"
      ? Math.max(Number.isFinite(st.bass) ? st.bass : 0, Number.isFinite(st.drums) ? st.drums : 0) : 1;
    return xf * vol * 10 ** (Math.min(12, db) / 20) * Math.max(0, stem);
  }
  // Pure: share of the low end that is deck B's, 0 (all A) .. 1 (all B), in
  // 1/16 steps (bounded colour cache); null when neither deck carries any.
  function bassMix(wa, wb) {
    const a = Number.isFinite(wa) && wa > 0 ? wa : 0, b = Number.isFinite(wb) && wb > 0 ? wb : 0;
    if (a + b < 0.02) return null;
    return Math.round((b / (a + b)) * 16) / 16;
  }
  // Pure: blend two #rrggbb colours, t = 0 -> a, 1 -> b.
  function mixHex(a, b, t) {
    const pa = /^#?([0-9a-f]{6})$/i.exec(a || ""), pb = /^#?([0-9a-f]{6})$/i.exec(b || "");
    if (!pa || !pb) return pa ? `#${pa[1]}` : pb ? `#${pb[1]}` : "#00e5ff";
    const k = Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0));
    const na = parseInt(pa[1], 16), nb = parseInt(pb[1], 16);
    let out = "#";
    for (const s of [16, 8, 0]) {
      const ca = (na >> s) & 255, cb = (nb >> s) & 255;
      out += Math.round(ca + (cb - ca) * k).toString(16).padStart(2, "0");
    }
    return out;
  }
  // Pure: strength 0..1 of the after-drop pulse at t seconds into the drop:
  // one decaying pulse on each beat 1..beats after the downbeat, fading out.
  function afterPulse(t, beat, beats = DROP_AFTER_BEATS) {
    const b = safeBeat(beat);
    if (!(t >= 0)) return 0;
    const k = Math.floor(t / b);
    if (k < 1 || k > beats) return 0;
    return Math.exp(-(t - k * b) * 9) * (1 - (k - 1) / beats);
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
  // ---- ANYMA look (a VFX style; default stays "classic") -------------------
  // Black space, ice-white / cyan thin lines and a particle field, a rare red
  // on supermoves. All timing comes from the SHOW core (anyma-show.js): the
  // same trigger mapping, director (cuts on phrase lines, capped flash, long
  // dissolve on transitions) and music state; this file only draws it in 2D.
  // The choice is the drawer toggle #vfx-anyma-toggle (drawer localStorage).
  const STYLE_ID = "vfx-anyma-toggle", STORE_KEY = "djAiToggles.v1";
  const ANYMA_COL = { ice: "#cfe9ff", cyan: "#3fd8ff", red: "#ff2238" };   // never pure white
  // Pure: the saved drawer blob -> "anyma" | "classic" (bad blob = classic).
  function styleFromStore(raw) {
    if (!raw) return "classic";
    try { const o = JSON.parse(raw); return o && typeof o === "object" && o[STYLE_ID] === true ? "anyma" : "classic"; }
    catch (e) { return "classic"; }
  }
  // Pure: the manual SHOW stage (full) is up -> this layer is dimmed (never hidden).
  function showYields(mode) { return mode === "full"; }
  // Pure: SHOW is on in the DJ elements -> the page-wide Anyma motifs stand down
  // (the look lives in the platters / lanes then); the bass band stays.
  function anymaYields(mode) { return mode === "elements"; }
  // Pure: alpha of the tinted flash overlay from the director's flash level.
  // Capped at 0.5, tinted cyan (never white), none under reduced motion.
  function anymaFlash(level, reduced) {
    if (reduced || !Number.isFinite(level) || level <= 0) return 0;
    return Math.min(0.5, level * 0.5);
  }
  // Pure: SHOW scene -> the 2D motif that stands for it on the VFX layer.
  const MOTIF = { head: "eyes", figure: "rings", corridor: "frames", monolith: "scan" };
  function anymaMotif(scene) { return MOTIF[scene] || "scan"; }
  // The SHOW core, shared (never forked): node require, or the page's copy.
  function anymaCore() {
    if (root.anymaShow && root.anymaShow.core) return root.anymaShow.core;
    if (typeof module !== "undefined" && module.exports && typeof require === "function") {
      try { return require("./anyma-show.js"); } catch (e) { return null; }
    }
    return null;
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { effectFor, env, FLASH_GAP_S, aiDriving, energyPeaks, nextPeakIdx, stepPeaks,
                       peakAllowed, DROP_PEAK_GAP_S, SEEK_JUMP_S, vfxLayers, flashAllowed, safeBeat,
                       MIN_PULSE_GAP_S, bassState, bassFollow, bassAlive, deckBass, bassMix, mixHex,
                       afterPulse, DROP_BURST_S, DROP_AFTER_BEATS,
                       STYLE_ID, STORE_KEY, ANYMA_COL, styleFromStore, showYields, anymaYields, anymaFlash, anymaMotif, anymaCore };
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
  let enabled = true, style = "classic";
  try { enabled = localStorage.getItem(KEY) !== "off"; } catch (_) { /* private mode */ }
  try { style = styleFromStore(localStorage.getItem(STORE_KEY)); } catch (_) { /* private mode */ }
  const mq = root.matchMedia ? root.matchMedia("(prefers-reduced-motion: reduce)") : null;
  let reduced = !!(mq && mq.matches);
  if (mq && mq.addEventListener) mq.addEventListener("change", (e) => { reduced = e.matches; });

  function setEnabled(on) {
    enabled = on;
    try { localStorage.setItem(KEY, on ? "on" : "off"); } catch (_) { /* quota */ }
    if (btn) { btn.setAttribute("aria-pressed", String(on)); btn.classList.toggle("vfx-on", on); }
    if (!on) { effects.length = 0; Object.assign(bass, bassState()); clear(); } else wake();
  }
  if (btn) btn.addEventListener("click", () => setEnabled(!enabled));

  const css = getComputedStyle(document.documentElement);
  const col = (name, fb) => (css.getPropertyValue(name) || "").trim() || fb;
  const COLOR = { a: col("--a", "#00ff66"), b: col("--b", "#ff2bd6"), ai: col("--ai", "#00e5ff") };
  const colorOf = (deck) => COLOR[deck] || COLOR.ai;
  const other = (deck) => (deck === "a" ? "b" : deck === "b" ? "a" : null);

  let W = 0, H = 0;
  function resize() {
    const scale = Math.min(1, (root.devicePixelRatio || 1)) * 0.5;   // half-res: glows hide it
    W = Math.max(1, Math.round(root.innerWidth * scale));
    H = Math.max(1, Math.round(root.innerHeight * scale));
    cv.width = W; cv.height = H;
    grads.clear();
    fullDirty = true;
  }
  let resizePending = false;
  root.addEventListener("resize", () => {
    if (resizePending) return;
    resizePending = true;
    requestAnimationFrame(() => { resizePending = false; resize(); wake(); });
  });
  // Last frame drew only the bass band: clearing the bottom strip is enough.
  let fullDirty = true, bandTop = 0;
  function clear() {
    if (fullDirty) cx.clearRect(0, 0, W, H);
    else if (bandTop < H) cx.clearRect(0, bandTop, W, H - bandTop);
    fullDirty = false; bandTop = H;
  }

  // ---- effects ----------------------------------------------------------------
  const effects = [];
  let lastFlash = -Infinity;
  const nowS = () => performance.now() / 1000;
  const aiOn = () => aiDriving(root.autopilotState);
  const dropTimes = [];                           // performance-clock seconds, booked + fired
  function spawn(fx) {
    if (!enabled || !fx || document.hidden || !aiOn()) return;   // every style: ANYMA layers on top
    fx.t0 = nowS();
    if (fx.type === "drop") {
      fx.flash = !fx.still && flashAllowed(fx.t0, lastFlash);
      if (fx.flash) lastFlash = fx.t0;
      if (!fx.still) {
        const n = 72;
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
    anymaEvent("ai-cue", d);
    schedule(effectFor(d, reduced), d.at);
  });
  root.addEventListener("ai-supermove", (e) => anymaEvent("ai-supermove", e.detail || {}));
  let holding = false;
  root.addEventListener("ai-activity", (e) => {
    const d = e.detail || {};
    anymaEvent("ai-activity", d);
    if (d.kind === "decision") { holding = d.action === "holdloop"; if (holding) wake(); }
    else if (d.kind === "stem-move") spawn(effectFor({ kind: "stem-move", deck: d.deck }, reduced));
  });

  // ---- master tap: the low band (read-only analyser, no output) -------------
  let an = null, bins = null, lowN = 3;
  const bass = bassState();
  function tap() {
    if (an || typeof audioCtx === "undefined") return;
    const src = root.masterOut || (typeof masterGain !== "undefined" ? masterGain : null);
    if (!src) return;
    an = audioCtx.createAnalyser();
    an.fftSize = 1024;
    an.smoothingTimeConstant = 0.5;
    // headroom above the default -30 dB ceiling, so a loud master's sub bins
    // are not pinned at full scale (same 70 dB span as the default range)
    an.minDecibels = -80;
    an.maxDecibels = -10;
    src.connect(an);                               // analyser has no output: read-only
    bins = new Uint8Array(an.frequencyBinCount);
    lowN = Math.max(1, Math.round(150 / (audioCtx.sampleRate / an.fftSize)));
  }
  function readLow() {
    if (!an) return 0;
    an.getByteFrequencyData(bins);
    let s = 0;
    for (let i = 1; i <= lowN; i++) s += bins[i];
    return s / (lowN * 255);
  }
  function anyPlaying() {
    const ds = root.decks || {};
    return !!((ds.a && ds.a.playing) || (ds.b && ds.b.playing));
  }
  // colour of whoever carries the low end; master (AI accent) colour if unknown
  function bassColor() {
    const ds = root.decks || {};
    const t = bassMix(deckBass(ds.a), deckBass(ds.b));
    return t === null ? COLOR.ai : t === 0 ? COLOR.a : t === 1 ? COLOR.b : mixHex(COLOR.a, COLOR.b, t);
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
  // Gradients are built once per (kind, colour) at full strength and drawn
  // with globalAlpha, so a steady frame allocates none. Cleared on resize;
  // colours are 1/16-step blends, so the cache stays small.
  const grads = new Map();
  function grad(kind, color, make) {
    const k = kind + color;
    let g = grads.get(k);
    if (!g) { if (grads.size > 96) grads.clear(); g = make(); grads.set(k, g); }
    return g;
  }
  function edgeGlow(color, a) {
    if (a <= 0.003) return;
    cx.globalAlpha = Math.min(1, a);
    cx.fillStyle = grad("edge", color, () => {
      const g = cx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.35, W / 2, H / 2, Math.hypot(W, H) / 2);
      g.addColorStop(0, rgba(color, 0)); g.addColorStop(1, rgba(color, 1));
      return g;
    });
    cx.fillRect(0, 0, W, H);
    cx.globalAlpha = 1;
    fullDirty = true;
  }
  // radial bloom from the centre, radius r (canvas px)
  function bloom(color, a, r) {
    if (a <= 0.003 || !(r > 0)) return;
    const g = grad("bloom", color, () => {
      const g = cx.createRadialGradient(0, 0, 0, 0, 0, 1);
      g.addColorStop(0, rgba(color, 1)); g.addColorStop(0.45, rgba(color, 0.45)); g.addColorStop(1, rgba(color, 0));
      return g;
    });
    cx.setTransform(r, 0, 0, r, W / 2, H / 2);
    cx.globalAlpha = Math.min(1, a);
    cx.fillStyle = g;
    cx.fillRect(-1, -1, 2, 2);
    cx.setTransform(1, 0, 0, 1, 0, 0);
    cx.globalAlpha = 1;
    fullDirty = true;
  }

  // Bass band: bottom edge, at most ~12 % of the height, fading upward to
  // nothing, so it sits in the margin under the decks and never hides text.
  const BAND_PTS = 32;
  function drawBass(color, now) {
    const drive = bass.level * bass.level;       // contrast: quiet stays low
    const hit = bass.hit;
    const h = H * (reduced ? 0.06 : 0.025 + 0.06 * drive + 0.035 * hit);
    const a = reduced ? 0.06 + 0.25 * drive : Math.min(0.65, 0.08 + 0.3 * drive + 0.35 * hit);
    if (a <= 0.003 || h < 1) return;
    const g = grad("band", color, () => {
      const g = cx.createLinearGradient(0, 0, 0, -1);
      g.addColorStop(0, rgba(color, 1)); g.addColorStop(0.35, rgba(color, 0.45)); g.addColorStop(1, rgba(color, 0));
      return g;
    });
    cx.setTransform(1, 0, 0, h, 0, H);             // y: 0 = bottom edge, -1 = band top
    cx.globalAlpha = a;
    cx.fillStyle = g;
    cx.beginPath();
    cx.moveTo(0, 0);
    if (reduced) cx.lineTo(0, -1), cx.lineTo(W, -1);
    else {
      // a slow travelling swell on the top edge, deeper with more bass
      const amp = 0.25 * drive + 0.1 * hit, ph = now * 1.3;
      for (let i = 0; i <= BAND_PTS; i++) {
        const u = i / BAND_PTS;
        cx.lineTo(u * W, -(1 - amp * (0.5 + 0.5 * Math.sin(u * 11 + ph))));
      }
    }
    cx.lineTo(W, 0);
    cx.closePath();
    cx.fill();
    if (hit > 0.02) {                               // kick: a brighter, lower core
      cx.setTransform(1, 0, 0, h * 0.45, 0, H);
      cx.globalAlpha = Math.min(0.7, 0.6 * hit);
      cx.fillRect(0, -1, W, 1);
    }
    cx.setTransform(1, 0, 0, 1, 0, 0);
    cx.globalAlpha = 1;
    bandTop = Math.min(bandTop, Math.max(0, Math.floor(H - h - 1)));
  }

  function drawDrop(fx, t, c) {
    const e = env(t, DROP_BURST_S);
    if (fx.still) { edgeGlow(c, 0.45 * e); return; }
    if (fx.flash && t < 0.3) {                      // tinted, never white, capped at 1/s
      cx.fillStyle = rgba(c, 0.38 * (1 - t / 0.3)); cx.fillRect(0, 0, W, H); fullDirty = true;
    }
    const R = Math.hypot(W, H) / 2;
    if (t < DROP_BURST_S) {
      const k = t / DROP_BURST_S, ease = 1 - Math.pow(1 - k, 3);
      bloom(c, 0.4 * e, R * (0.35 + 0.75 * ease));
      cx.strokeStyle = rgba(c, 0.95 * (1 - k));
      cx.lineWidth = Math.max(1, 18 * (1 - k));
      cx.beginPath(); cx.arc(W / 2, H / 2, R * ease, 0, Math.PI * 2); cx.stroke();
      // trailing ring in a lighter tint of the same colour: contrast, still no white
      cx.strokeStyle = rgba(mixHex(c, "#ffffff", 0.4), 0.6 * (1 - k));
      cx.lineWidth = Math.max(1, 7 * (1 - k));
      cx.beginPath(); cx.arc(W / 2, H / 2, R * 0.72 * ease, 0, Math.PI * 2); cx.stroke();
      cx.fillStyle = rgba(c, 0.95 * (1 - k));
      const sz = Math.max(1, 4 * (1 - k));
      for (let i = 0; i < fx.ang.length; i++) {
        const r = R * 0.95 * ease * fx.spd[i];
        cx.fillRect(W / 2 + Math.cos(fx.ang[i]) * r, H / 2 + Math.sin(fx.ang[i]) * r, sz, sz);
      }
      edgeGlow(c, 0.45 * e);
    }
    edgeGlow(c, 0.3 * afterPulse(t, fx.beat));     // beat-synced, 2 bars, fading
    fullDirty = true;
  }
  function drawSweep(fx, t, c) {
    const e = env(t, fx.dur, 0.2), k = t / fx.dur;
    const cIn = c, cOut = colorOf(other(fx.deck));
    const cur = mixHex(cOut, cIn, Math.round(Math.min(1, k * 1.25) * 16) / 16);
    if (fx.still) { edgeGlow(cIn, 0.3 * e); return; }
    // B enters from its own side: deck A lives left, deck B right. The trailing
    // edge carries the outgoing deck's colour, the leading edge the incoming's.
    const fromRight = fx.deck === "b";
    const x = (fromRight ? 1 - k : k) * (W * 1.4) - W * 0.2, bw = W * 0.38;
    const g = cx.createLinearGradient(x - bw, 0, x + bw, 0);
    const a = 0.34 * e + 0.08;
    const left = fromRight ? cIn : cOut, right = fromRight ? cOut : cIn;
    g.addColorStop(0, rgba(left, 0)); g.addColorStop(0.3, rgba(left, a * 0.6));
    g.addColorStop(0.5, rgba(cur, a)); g.addColorStop(0.7, rgba(right, a * 0.6)); g.addColorStop(1, rgba(right, 0));
    cx.fillStyle = g;
    cx.fillRect(0, 0, W, H);
    edgeGlow(cur, 0.22 * e);
    fullDirty = true;
  }
  function drawScan(fx, t, c) {
    const e = env(t, fx.dur);
    if (fx.still) { edgeGlow(c, 0.25 * e); return; }
    const y = (t / fx.dur) * H, bh = Math.max(4, H * 0.05);
    const g = cx.createLinearGradient(0, y - bh, 0, y + bh);
    g.addColorStop(0, rgba(c, 0)); g.addColorStop(0.5, rgba(c, 0.6 * e + 0.12)); g.addColorStop(1, rgba(c, 0));
    cx.fillStyle = g;
    cx.fillRect(0, y - bh, W, bh * 2);
    fullDirty = true;
  }
  // gentler than a drop: no flash, no ring, a slow bloom from the centre
  function drawPeak(fx, t, c) {
    const e = env(t, fx.dur, 0.5);
    if (fx.still) { edgeGlow(c, 0.3 * e); return; }
    bloom(c, 0.22 * e, (Math.hypot(W, H) / 2) * (0.6 + 0.5 * (t / fx.dur)));
    edgeGlow(c, 0.4 * e);
  }
  const DRAW = { drop: drawDrop, sweep: drawSweep, scan: drawScan, peak: drawPeak,
                 edge: (fx, t, c) => edgeGlow(c, 0.35 * env(t, fx.dur)) };

  // ---- ANYMA look: the SHOW core's director, drawn as thin lines + particles --
  // Everything below is allocated once; a frame only writes numbers.
  const PN = 180;                                   // particles at full quality
  const AN = { dir: null, ms: null, dv: null, q: null, pts: null, onAir: null, travel: 0, sceneAt: -1,
               tracks: { a: { an: null, pt: null }, b: { an: null, pt: null } } };
  function anymaInit(A) {
    if (AN.dir) return true;
    if (!A) return false;
    AN.dir = A.createDirector(0x9e37); AN.ms = A.musicStateNew(); AN.q = A.qualityNew();
    AN.dv = { pulse: 0, weight: 0, eye: 0, colour: 0, intensity: 0 };
    AN.pts = new Float32Array(PN * 4);
    seedPts();
    return true;
  }
  function seedPts() {
    const p = AN.pts;
    for (let i = 0; i < PN; i++) {
      p[i * 4] = Math.random(); p[i * 4 + 1] = Math.random();
      const a = Math.random() * Math.PI * 2, s = 0.004 + Math.random() * 0.012;
      p[i * 4 + 2] = Math.cos(a) * s; p[i * 4 + 3] = Math.sin(a) * s;
    }
  }
  // console event -> the SHOW core's trigger mapping -> this layer's director
  function anymaEvent(type, detail) {
    if (style !== "anyma" || !enabled || !aiOn()) return;
    const A = anymaCore();
    if (!anymaInit(A)) return;
    const now = nowS(), an = typeof audioCtx !== "undefined" ? audioCtx.currentTime : NaN;
    const ev = A.eventTrigger(type, detail, now, (at) => (Number.isFinite(an) ? now + (at - an) : now));
    if (ev) { A.queueEvent(AN.dir, ev); wake(); }
  }
  const gainVal = (n) => (n && n.gain && Number.isFinite(n.gain.value) ? n.gain.value : 1);
  const airGain = (d) => (d && d.playing ? gainVal(d.crossfaderGain) * gainVal(d.volumeGain) : 0);
  function motif(scene, a, dir, ms, dv) {
    if (a <= 0.003) return;
    const beatA = 0.55 + 0.6 * dv.pulse, m = anymaMotif(scene);
    cx.lineWidth = 1;
    if (m === "frames") {                            // corridor: frames rushing out from the centre
      for (let i = 0; i < 5; i++) {
        const s = ((i + AN.travel) % 5) / 5, w = W * (0.12 + 0.88 * s), h = H * (0.12 + 0.88 * s);
        cx.globalAlpha = Math.min(1, a * beatA * 0.24 * s);
        cx.strokeRect(W / 2 - w / 2, H / 2 - h / 2, w, h);
      }
    } else if (m === "scan") {                       // monolith: side rails, a scan line once per bar
      cx.globalAlpha = Math.min(1, a * 0.2 * beatA);
      cx.fillRect(Math.round(W * 0.035), 0, 1, H); cx.fillRect(Math.round(W * 0.965), 0, 1, H);
      const y = Math.round(H * (1 - (ms.ok ? ms.barPhase : 0.5)));
      cx.globalAlpha = Math.min(1, a * (0.1 + 0.28 * dv.pulse));
      cx.fillRect(0, y, W, 1);
    } else if (m === "rings") {                      // figure: rings of light, scaled by the bass
      const sway = dir.dance > 0 && ms.ok ? Math.cos(Math.PI * (ms.beatIdx + ms.beatPhase)) * dir.dance * W * 0.02 : 0;
      cx.globalAlpha = Math.min(1, a * 0.2 * beatA);
      cx.beginPath();
      for (let i = 0; i < 3; i++) {
        const r = H * (0.16 + 0.09 * i) * (1 + 0.14 * dv.weight), x = W / 2 + sway * (i + 1) / 3;
        cx.moveTo(x + r * 1.7, H * 0.56); cx.ellipse(x, H * 0.56, r * 1.7, r * 0.32, 0, 0, Math.PI * 2);
      }
      cx.stroke();
    } else drawEyes(a, dv.eye);                      // head: the face / eye motif
  }
  function drawEyes(a, eye) {
    const k = Math.min(1, a * (0.12 + 0.75 * eye));
    if (k <= 0.003) return;
    cx.globalAlpha = k;
    cx.beginPath();
    for (let s = -1; s <= 1; s += 2) {
      const x = W / 2 + s * W * 0.055, y = H * 0.18;
      cx.moveTo(x + W * 0.03, y); cx.ellipse(x, y, W * 0.03, H * 0.012, 0, 0, Math.PI * 2);
    }
    cx.stroke();
    cx.globalAlpha = Math.min(1, a * eye);
    for (let s = -1; s <= 1; s += 2) cx.fillRect(W / 2 + s * W * 0.055 - 1, H * 0.18 - 1, 3, 3);
  }
  function anymaFrame(now, dt) {
    const A = anymaCore();
    if (!anymaInit(A)) return;
    const t0 = performance.now(), ds = root.decks || {}, dir = AN.dir, ms = AN.ms, dv = AN.dv;
    const was = AN.onAir;
    AN.onAir = A.onAirDeck(AN.onAir, airGain(ds.a), airGain(ds.b));
    if (was && AN.onAir && was !== AN.onAir) A.queueEvent(dir, { type: "transition", at: now });   // hand-over: long dissolve
    const d = AN.onAir ? ds[AN.onAir] : null, tr = d ? AN.tracks[AN.onAir] : null;
    if (tr && tr.an !== d.analysis) { tr.an = d.analysis; tr.pt = d.analysis ? A.prepTrack(d.analysis) : null; }
    const pos = d && typeof d._currentPosition === "function" ? d._currentPosition() : NaN;
    A.musicState(tr ? tr.pt : null, pos, d && d.bpm, ms);
    A.stepDirector(dir, ms, now, dt, reduced);
    A.drives(ms, null, dv);
    // a hard cut re-seeds the field; a dissolve keeps it drifting
    if (dir.sceneAt !== AN.sceneAt) { AN.sceneAt = dir.sceneAt; if (!dir.prev && !reduced) seedPts(); }
    const drift = reduced ? 0 : dt * (0.4 + (ms.ok ? ms.energy : 0));
    AN.travel = (AN.travel + dt * (reduced ? 0.04 : 0.12 + 0.5 * (ms.ok ? ms.energy : 0))) % 5;

    const fl = anymaFlash(dir.flash, reduced);
    if (fl > 0.003) { cx.globalAlpha = fl * 0.6; cx.fillStyle = ANYMA_COL.cyan; cx.fillRect(0, 0, W, H); }
    const line = dir.red > 0.3 ? ANYMA_COL.red : ANYMA_COL.ice;
    cx.strokeStyle = line; cx.fillStyle = line;
    if (dir.prev) motif(dir.prev, 1 - dir.mix, dir, ms, dv);
    motif(dir.scene, dir.prev ? dir.mix : 1 - 0.8 * dir.assemble, dir, ms, dv);
    if (dir.scene !== "head" && dv.eye > 0.05) drawEyes(0.7, dv.eye);   // a vocal lights the eyes anywhere

    const p = AN.pts, n = Math.floor(PN * A.QUALITY[AN.q.level].pts), sz = 1 + 1.5 * dv.weight;
    cx.fillStyle = ANYMA_COL.cyan;
    cx.globalAlpha = Math.min(1, 0.16 + 0.34 * dv.pulse);
    for (let i = 0; i < n; i++) {
      let x = p[i * 4] + p[i * 4 + 2] * drift, y = p[i * 4 + 1] + p[i * 4 + 3] * drift;
      x -= Math.floor(x); y -= Math.floor(y);
      p[i * 4] = x; p[i * 4 + 1] = y;
      cx.fillRect(x * W, y * H, sz, sz);
    }
    if (bass.hit > 0.01 && !reduced) {               // kick: a thin frame, not a colour wash
      cx.globalAlpha = Math.min(1, 0.35 * bass.hit); cx.strokeStyle = ANYMA_COL.cyan;
      cx.strokeRect(0.5, 0.5, W - 1, H - 1);
    }
    cx.globalAlpha = 1;
    fullDirty = true;
    A.qualityStep(AN.q, performance.now() - t0, dt * 1000, 1000 / 60);
  }

  // ---- loop: runs only while something is on screen ------------------------
  let raf = 0, lastT = 0;
  const lay = { enabled: false, hidden: false, playing: false, bassAlive: false, autopilot: null, show: "off" };
  // shared ticks (the SHOW's in-element Anyma look): run on this one loop, even
  // with the VFX layer off; a tick returns true while it still needs frames
  const ticks = [];
  let dimmed = false;
  function wake() {
    if (raf || (!enabled && !ticks.length) || document.hidden) return;
    lastT = nowS();
    raf = requestAnimationFrame(frame);
  }
  function frame() {
    raf = 0;
    const now = nowS(), dt = Math.min(0.1, now - lastT);
    let keep = false;
    for (let i = 0; i < ticks.length; i++) if (ticks[i](now, dt)) keep = true;
    if (enabled) draw(now, dt);
    else lastT = now;
    if (keep) wake();
  }
  function draw(now, dt) {
    lastT = now;
    const ai = aiOn();
    if (!ai) effects.length = 0;                     // user took over: AI effects gone
    tap();
    const playing = anyPlaying();
    bassFollow(bass, readLow(), dt, now, reduced);
    lay.enabled = enabled; lay.hidden = document.hidden; lay.playing = playing; lay.bassAlive = bassAlive(bass);
    lay.autopilot = root.autopilotState; lay.show = root.anymaShow ? root.anymaShow.mode : "off";
    const L = vfxLayers(lay);
    clear();
    // the manual STAGE dims this layer (CSS opacity, set on change only), never hides it
    const dim = showYields(lay.show);
    if (dim !== dimmed) { dimmed = dim; cv.style.opacity = dimmed ? "0.5" : ""; }
    // the classic NULL-BOT layer ALWAYS draws (bass band, kick, drop / transition /
    // energy-peak effects); ANYMA LOOK only adds its cyan motifs on top of it
    if (L.ai) checkPeaks();
    const bc = L.bass ? bassColor() : null;
    if (L.bass) {
      drawBass(bc, now);
      if (style === "anyma" && bandTop < H) { cx.globalAlpha = Math.min(1, 0.25 + 0.5 * bass.level); cx.fillStyle = ANYMA_COL.ice; cx.fillRect(0, bandTop + 1, W, 1); cx.globalAlpha = 1; }
    }
    if (L.ai && style === "anyma" && !anymaYields(lay.show)) anymaFrame(now, dt);
    if (L.ai) {
      if (bass.hit > 0.01 && !reduced) edgeGlow(bc || COLOR.ai, 0.2 * bass.hit);   // kick vignette
      if (holding) {
        const ds = root.decks || {}, d = (ds.a && ds.a.playing && ds.a) || (ds.b && ds.b.playing && ds.b) || {};
        const beat = 60 / (d.bpm || 124);
        edgeGlow(COLOR.ai, reduced ? 0.14 : 0.1 + 0.1 * (0.5 + 0.5 * Math.cos((now / beat) * Math.PI * 2)));
      }
      for (let i = effects.length - 1; i >= 0; i--) {
        const fx = effects[i], t = now - fx.t0;
        if (t >= fx.dur) { effects.splice(i, 1); continue; }
        DRAW[fx.type](fx, t, fx.type === "scan" || fx.type === "edge" ? COLOR.ai : colorOf(fx.deck));
      }
    }
    // idle (nothing playing, band faded, no effect): this frame drew nothing
    // after its clear, so the canvas is blank and the loop sleeps
    if (effects.length || (L.ai && holding) || playing || bassAlive(bass)) wake();
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (raf) cancelAnimationFrame(raf);
      raf = 0; effects.length = 0; Object.assign(bass, bassState()); fullDirty = true; clear();
    }
    else wake();
  });
  // a deck starting (hand or AI) or a hold loop wakes the sleeping loop
  setInterval(() => { if (!raf && (anyPlaying() || (enabled && holding && aiOn()))) wake(); }, 1000);

  // VFX style: the drawer toggle, restored early from the drawer's own store so
  // the first frame already has the right look (the drawer re-applies it later)
  function setStyle(s) {
    style = s === "anyma" ? "anyma" : "classic";
    document.documentElement.classList.toggle("anyma-look", style === "anyma");
    effects.length = 0; fullDirty = true; clear(); wake();
  }
  const styleBox = document.getElementById(STYLE_ID);
  if (styleBox) {
    styleBox.checked = style === "anyma";
    styleBox.addEventListener("change", () => setStyle(styleBox.checked ? "anyma" : "classic"));
  }

  resize();
  setEnabled(enabled);
  setStyle(style);
  root.nulVisuals = { setEnabled, get enabled() { return enabled; }, setStyle, get style() { return style; },
                      spawn: (kind, deck) => spawn(effectFor({ kind, deck }, reduced)),
                      addTick(fn) { if (typeof fn === "function" && !ticks.includes(fn)) ticks.push(fn); wake(); },
                      removeTick(fn) { const i = ticks.indexOf(fn); if (i >= 0) ticks.splice(i, 1); },
                      wake };
})(typeof window !== "undefined" ? window : globalThis);
