// Anyma look baked into the DJ elements (brief: research/notes/anyma-visual-brief.md).
// No stage, no overlay, no translucent panels: the platters, waveform lanes, VIBE
// strip and meters themselves carry the look, driven by the SHOW core (anyma-show.js:
// beat grid, phrases, energy, stems, vocal regions, drops, supermoves, anticipation).
//   platter   centre label -> a small android-face "eye" lit by that deck's vocal;
//             a ring of light pulses on its kick and scales with its bass; a clean
//             cyan ring burst on a drop / supermove
//   waveform  ice / cyan line colours, a scan line on each bar downbeat, a build
//             brightening toward the drop, full brightness on the drop cut
//   figure    during an Anyma drop only: a SMALL dancing figure inside the waveform
//             lane of the deck that dropped, on the beat, never over the controls
//   VIBE      the scene name + "DROP IN N BARS" from the anticipation queue
//   meters    cyan light, red only while a supermove is up (html.an-red)
// Only in the ANYMA LOOK style (html.anyma-look) with SHOW on (html.anyma-el); the
// classic look is untouched. Moments (drops, anticipation, figure) need SHOW AUTO.
// Performance: no rAF of its own (anyma-show.js calls update() from the shared
// visuals.js loop), fixed-size absolutely placed layers, only opacity / transform
// written per frame (compositor only, no reflow), nothing allocated per frame.
(function (root) {
  "use strict";

  const fin = Number.isFinite;
  const clamp01 = (x) => (x > 0 ? (x < 1 ? x : 1) : 0);
  const SCENE_LABEL = { head: "ANDROID HEAD", figure: "FIGURE", corridor: "CORRIDOR", monolith: "MONOLITH" };
  const COUNTDOWN_MAX_BARS = 16;       // anticipate at most two phrases ahead
  const BURST_DECAY = 2.8;             // 1/s: the drop ring burst fades in ~0.6 s

  // ---- pure core --------------------------------------------------------------
  function deckLookNew() { return { ring: 0, eye: 0, scale: 1, burst: 0, bright: 0, scan: 0, figure: 0 }; }
  function lookNew() {
    return { a: deckLookNew(), b: deckLookNew(), on: false, scene: "", countdown: "", red: 0, dropDeck: null, danceDeck: null, hits: 0 };
  }
  // one deck's input slot: ms = SHOW musicState of that deck; eye / weight = stem
  // drives 0..1 when the deck's stems are live (NaN = use the analysis instead)
  function deckInNew() { return { playing: false, ms: null, eye: NaN, weight: NaN }; }
  function inputNew() {
    return { on: false, auto: false, onAir: null, a: deckInNew(), b: deckInNew(),
      last: "", red: 0, dance: 0, next: null, now: 0, scene: "" };
  }

  // Pure: bar-lines until a moment at clock time `at` (1 = the next bar line).
  function barsUntil(at, now, beat) {
    if (!fin(at) || !fin(now) || !(beat > 0) || at <= now) return 0;
    return Math.ceil((at - now) / (4 * beat) - 1e-6);
  }
  // Pure: the VIBE countdown text for an anticipated moment ("" = none shown).
  function countdownText(kind, bars) {
    if (!(bars >= 1) || bars > COUNTDOWN_MAX_BARS) return "";
    const k = String(kind || "").toUpperCase().replace(/\s+/g, " ").trim();
    const name = !k || /ANYMA/.test(k) ? "DROP" : k.slice(0, 16);
    return `${name} IN ${bars} ${bars === 1 ? "BAR" : "BARS"}`;
  }
  function sceneLabel(scene) { return SCENE_LABEL[scene] || ""; }

  function decay(x, dt, k) { return x * Math.exp(-(dt > 0 ? dt : 0) * k); }
  // Pure step: writes every element level into L (no allocation). reduced =
  // prefers-reduced-motion: rings hold steady, no scan sweep, no burst growth.
  function lookStep(L, i, dt, reduced) {
    const on = !!(i && i.on);
    L.on = on;
    const moment = on && i.auto && (i.last === "drop" || i.last === "anyma" || i.last === "supermove");
    if (moment && (i.onAir === "a" || i.onAir === "b")) {
      L.dropDeck = i.onAir; L.hits++;
      if (i.last === "anyma") L.danceDeck = i.onAir;           // only an Anyma drop brings the figure
    }
    for (const id of ["a", "b"]) {
      const o = L[id], d = on ? i[id] : null, ms = d && d.playing && d.ms && d.ms.ok ? d.ms : null;
      if (!ms) {
        o.ring = decay(o.ring, dt, 6); o.eye = decay(o.eye, dt, 3); o.scan = 0;
        o.bright = decay(o.bright, dt, 3); o.burst = decay(o.burst, dt, BURST_DECAY);
        o.scale = 1 + (o.scale - 1) * Math.exp(-(dt > 0 ? dt : 0) * 4);
        o.figure = decay(o.figure, dt, 4);
        for (const k of ["ring", "eye", "bright", "burst", "figure"]) if (o[k] < 1e-3) o[k] = 0;
        continue;
      }
      const e = clamp01(ms.energy);
      // kick: a sharp envelope on every beat of the grid, louder with energy
      o.ring = reduced ? 0.25 + 0.35 * e : clamp01(Math.exp(-clamp01(ms.beatPhase) * 8) * (0.35 + 0.65 * e));
      // vocal: the stem when live, else the analysed vocal regions; soft attack / release
      const vx = fin(d.eye) ? clamp01(d.eye) : ms.vocal ? 0.85 : 0;
      o.eye += (vx - o.eye) * (1 - Math.exp(-(dt > 0 ? dt : 0) / (vx > o.eye ? 0.08 : 0.45)));
      const w = fin(d.weight) ? clamp01(d.weight) : e * 0.7;
      o.scale = 1 + (reduced ? 0.02 : 0.06) * w;
      o.scan = reduced ? 0 : clamp01(1 - clamp01(ms.barPhase) * 5);        // first fifth of each bar
      const base = ms.cls === "build" ? 0.2 + 0.6 * clamp01(ms.phrasePhase) : ms.cls === "drop" ? 0.55
        : ms.cls === "groove" ? 0.3 : 0.12;
      o.bright += (base - o.bright) * (1 - Math.exp(-(dt > 0 ? dt : 0) * 2));
      o.burst = decay(o.burst, dt, BURST_DECAY);
      if (moment && L.dropDeck === id) { o.burst = 1; o.bright = 1; }       // the drop cuts to full
      if (o.burst < 1e-3) o.burst = 0;
      const fig = on && i.auto && L.danceDeck === id ? clamp01(i.dance) : 0;
      o.figure = fig > o.figure ? fig : decay(o.figure, dt, 4);
      if (o.figure < 1e-3) o.figure = 0;
    }
    L.red = on && i.auto ? clamp01(i.red) : 0;
    L.scene = on ? sceneLabel(i.scene) : "";
    const oa = on && i.auto && (i.onAir === "a" || i.onAir === "b") ? i[i.onAir] : null;
    const beat = oa && oa.ms && oa.ms.ok ? oa.ms.beat : NaN;
    L.countdown = oa && i.next && fin(beat) ? countdownText(i.next.kind, barsUntil(i.next.at, i.now, beat)) : "";
    return L;
  }

  // Pure: where the figure lives inside an element of w x h px: a narrow box at
  // the right end, inset from every edge, at most 1/5 of the width.
  function figureBox(w, h) {
    const W = fin(w) && w > 0 ? w : 0, H = fin(h) && h > 0 ? h : 0;
    const bh = Math.max(0, H - 8), bw = Math.min(W * 0.2, bh * 0.55);
    return { x: Math.max(0, W - bw - 6), y: 4, w: Math.max(0, bw), h: bh };
  }
  // Pure: the figure's 7 line segments (14 points, x/y pairs) for a dance pose
  // (core.dancePose output) and arm pose [armL, armR], clamped inside box.
  const FIG_SEGS = 7;
  function figurePoints(box, dn, P, out) {
    const o = out || new Float32Array(FIG_SEGS * 4);
    const amp = dn ? clamp01(dn.amp) : 0, sway = dn && fin(dn.sway) ? dn.sway : 0;
    const hit = dn ? clamp01(dn.hit) : 0, pose = dn ? clamp01(dn.pose) : 0, sink = dn ? clamp01(dn.weight) : 0;
    const aL = (P && fin(P[0]) ? P[0] : 0) * pose + 0.6 * hit, aR = (P && fin(P[1]) ? P[1] : 0) * pose + 0.6 * hit;
    const cx = box.x + box.w / 2, u = box.h / 10;
    const dy = amp * (0.25 * hit + 0.35 * sink) * u, sx = amp * sway * box.w * 0.18;
    const hip = { x: cx + sx * 0.4, y: box.y + 6.2 * u + dy }, neck = { x: cx + sx, y: box.y + 2.4 * u + dy };
    let k = 0;
    const seg = (x0, y0, x1, y1) => { o[k++] = x0; o[k++] = y0; o[k++] = x1; o[k++] = y1; };
    seg(hip.x, hip.y, neck.x, neck.y);                                           // spine
    const arm = (s, a) => { const ang = Math.PI / 2 + s * (0.35 + amp * a); seg(neck.x, neck.y, neck.x + s * Math.sin(ang) * 2.6 * u, neck.y - Math.cos(ang) * 2.6 * u); };
    arm(-1, aL); arm(1, aR);
    seg(hip.x, hip.y, cx - 1.1 * u, box.y + box.h);                              // legs
    seg(hip.x, hip.y, cx + 1.1 * u, box.y + box.h);
    const hx = neck.x + amp * (dn && fin(dn.nod) ? dn.nod : 0) * 0.3 * u, hy = neck.y - 1.2 * u;
    seg(hx - 0.7 * u, hy, hx + 0.7 * u, hy);                                     // head: a visor line
    seg(hx, hy - 0.5 * u, hx, neck.y);
    for (let j = 0; j < o.length; j += 2) {
      o[j] = Math.min(box.x + box.w, Math.max(box.x, o[j]));
      o[j + 1] = Math.min(box.y + box.h, Math.max(box.y, o[j + 1]));
    }
    return o;
  }

  const core = { lookNew, inputNew, deckInNew, lookStep, barsUntil, countdownText, sceneLabel,
    figureBox, figurePoints, FIG_SEGS, COUNTDOWN_MAX_BARS };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof document === "undefined" || typeof root.addEventListener !== "function") return;

  // ---- DOM glue: builds fixed layers once, writes opacity / transform per frame ----
  const doc = root.document, html = doc.documentElement;
  const L = lookNew();
  const decks = {};
  const mk = (cls, tag) => { const e = doc.createElement(tag || "i"); e.className = cls; e.setAttribute("aria-hidden", "true"); return e; };
  for (const id of ["a", "b"]) {
    const wheel = doc.querySelector(`.jog-wheel[data-deck="${id}"]`), lane = doc.querySelector(`.wave-lane.deck-${id}`);
    const label = wheel && wheel.querySelector(".jog-label-ring");
    const d = { ring: null, burst: null, face: null, glow: null, scan: null, fig: null, fx: null, fw: 0, fh: 0, figOn: false, pts: null };
    if (wheel) { d.ring = mk("an-ring"); d.burst = mk("an-burst"); wheel.append(d.ring, d.burst); }
    if (label) { d.face = mk("an-face"); d.face.append(doc.createElement("b"), doc.createElement("b")); label.prepend(d.face); }
    if (lane) {
      const clip = mk("an-clip", "div");
      d.glow = mk("an-glow"); d.scan = mk("an-scan"); d.fig = mk("an-fig", "canvas");
      clip.append(d.glow, d.scan, d.fig);
      lane.append(clip);
      d.fx = d.fig.getContext("2d");
      d.pts = new Float32Array(FIG_SEGS * 4);
    }
    decks[id] = d;
  }
  // figure canvas size: read on resize only, never per frame
  if (typeof root.ResizeObserver === "function") {
    const ro = new root.ResizeObserver(() => {
      for (const id of ["a", "b"]) {
        const d = decks[id];
        if (!d.fig) continue;
        const r = d.fig.getBoundingClientRect(), k = Math.min(1.5, root.devicePixelRatio || 1);
        d.fw = Math.max(1, Math.round(r.width * k)); d.fh = Math.max(1, Math.round(r.height * k));
        d.fig.width = d.fw; d.fig.height = d.fh; d.figOn = false;
      }
    });
    for (const id of ["a", "b"]) if (decks[id].fig) ro.observe(decks[id].fig);
  }

  // write only on a visible change (2 decimals): no style churn on steady values
  const last = new Map();
  function put(el, prop, v) {
    if (!el) return;
    const s = typeof v === "number" ? v.toFixed(2) : v;
    const key = last.get(el) || {};
    if (key[prop] === s) return;
    key[prop] = s; last.set(el, key);
    if (prop === "opacity") el.style.opacity = s;
    else el.style.transform = s;
  }
  const scaleOf = (x) => `scale(${x.toFixed(3)})`;

  let active = false, red = false;
  // WaveSurfer line colours: set once on entering / leaving the look (a re-render,
  // so never per frame); the classic colours are restored exactly
  const WF = { a: "waveformA", b: "waveformB" }, saved = {};
  const AN_WAVE = { waveColor: "rgba(207, 233, 255, 0.34)", progressColor: "#3fd8ff", cursorColor: "#cfe9ff" };
  function waveColours(on) {
    for (const id of ["a", "b"]) {
      const wf = root[WF[id]];
      if (!wf || typeof wf.setOptions !== "function") continue;
      try {
        if (on) {
          if (!saved[id]) { const o = wf.options || {}; saved[id] = { waveColor: o.waveColor, progressColor: o.progressColor, cursorColor: o.cursorColor }; }
          wf.setOptions(AN_WAVE);
        } else if (saved[id]) { wf.setOptions(saved[id]); saved[id] = null; }
      } catch (_) { /* waveform not ready yet: the next toggle retries */ }
    }
  }
  function setActive(on) {
    if (on === active) return;
    active = on;
    html.classList.toggle("anyma-el", on);
    waveColours(on);
    if (!on) {
      if (red) { red = false; html.classList.remove("an-red"); }
      Object.assign(L, lookNew());
      for (const id of ["a", "b"]) clearFig(decks[id]);
    }
  }
  function clearFig(d) { if (d.fx && d.figOn) { d.fx.clearRect(0, 0, d.fw, d.fh); d.figOn = false; } }
  function drawFig(d, lv, dn, P) {
    if (!d.fx) return;
    if (lv < 0.01) { clearFig(d); return; }
    const cx = d.fx;
    cx.clearRect(0, 0, d.fw, d.fh);
    figurePoints(figureBox(d.fw, d.fh), dn, P, d.pts);
    cx.globalAlpha = Math.min(1, 0.25 + 0.75 * lv);
    cx.strokeStyle = "#cfe9ff"; cx.lineWidth = Math.max(1, d.fh / 60); cx.lineCap = "round";
    cx.beginPath();
    const p = d.pts;
    for (let j = 0; j < p.length; j += 4) { cx.moveTo(p[j], p[j + 1]); cx.lineTo(p[j + 2], p[j + 3]); }
    cx.stroke();
    cx.globalAlpha = 1;
    d.figOn = true;
  }

  // Called by anyma-show.js once per shared frame. Returns true while anything
  // is still moving (the shared loop keeps waking), false once settled.
  function update(inp, dt, reduced, dn, P) {
    const on = !!(inp && inp.on) && html.classList.contains("anyma-look");
    setActive(on);
    if (!on) return false;
    const hit = L.hits;
    lookStep(L, inp, dt, reduced);
    // the drop line: platter burst + waveform full brightness (above) + the VIBE strip, together
    if (L.hits !== hit && root.vibeUi && typeof root.vibeUi.pulse === "function") root.vibeUi.pulse(inp.last === "supermove");
    let moving = false;
    for (const id of ["a", "b"]) {
      const d = decks[id], o = L[id];
      put(d.ring, "opacity", o.ring);
      put(d.ring, "transform", scaleOf(o.scale));
      put(d.burst, "opacity", o.burst);
      put(d.burst, "transform", scaleOf(reduced ? 1 : 1 + 0.22 * (1 - o.burst)));
      put(d.face, "opacity", 0.25 + 0.75 * o.eye);
      put(d.glow, "opacity", Math.min(1, 0.35 * o.bright + 0.5 * o.burst));
      put(d.scan, "opacity", o.scan);
      put(d.scan, "transform", `translateY(${(100 * (1 - o.scan)).toFixed(0)}%)`);
      drawFig(d, o.figure, dn, P);
      if (o.ring || o.eye || o.burst || o.bright > 1e-3 || o.figure || o.scale > 1.0005) moving = true;
    }
    const r = L.red > 0.3;
    if (r !== red) { red = r; html.classList.toggle("an-red", r); }
    return moving;
  }

  root.anymaUi = { core, update, look: L, get active() { return active; } };
})(typeof window !== "undefined" ? window : globalThis);
