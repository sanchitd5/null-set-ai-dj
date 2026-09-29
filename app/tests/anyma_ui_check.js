// The Anyma look inside the DJ elements (anyma-ui.js) + the SHOW glue (anyma-show.js):
// no stage / no translucent panels by default, SHOW AUTO never opens the stage,
// element triggers from synthetic beat / drop / vocal streams, the figure only
// during an Anyma drop and inside its element, classic look unaffected.
const assert = require("assert");
const fs = require("fs"), path = require("path"), vm = require("vm");
const U = require("../ui/static/anyma-ui.js");
const A = require("../ui/static/anyma-show.js");
const V = require("../ui/static/vibe-ui.js");
const STATIC = path.join(__dirname, "../ui/static");

// ---- pure: synthetic streams -> element levels -----------------------------------
const MS = (o) => Object.assign(A.musicStateNew(), { ok: true, bpm: 120, beat: 0.5, energy: 0.6, cls: "groove" }, o);
function inp(o) {
  const i = U.inputNew();
  Object.assign(i, { on: true, auto: true, onAir: "a" }, o);
  i.a = Object.assign(U.deckInNew(), { playing: true, ms: MS({}) }, o.a || {});
  i.b = Object.assign(U.deckInNew(), o.b || {});
  return i;
}
{
  // platter ring on the kick: sharp on the beat, low just before the next one
  const L = U.lookNew();
  U.lookStep(L, inp({ a: { ms: MS({ beatPhase: 0 }) } }), 1 / 60, false);
  const onKick = L.a.ring;
  U.lookStep(L, inp({ a: { ms: MS({ beatPhase: 0.9 }) } }), 1 / 60, false);
  assert.ok(onKick > 0.6 && L.a.ring < 0.05, `ring pulses on the kick (${onKick} -> ${L.a.ring})`);
  assert.strictEqual(L.b.ring, 0, "the silent deck's ring stays dark");
  // ring scales with the bass (stem weight)
  U.lookStep(L, inp({ a: { ms: MS({}), weight: 1 } }), 1 / 60, false);
  const big = L.a.scale;
  U.lookStep(L, inp({ a: { ms: MS({}), weight: 0 } }), 1 / 60, false);
  assert.ok(big > L.a.scale && big <= 1.07, "ring scale follows the bass");
}
{
  // eye lights with the vocal (analysed region or live stem), fades without one
  const L = U.lookNew();
  for (let k = 0; k < 30; k++) U.lookStep(L, inp({ a: { ms: MS({ vocal: true }) } }), 1 / 60, false);
  assert.ok(L.a.eye > 0.7, `eye on the vocal (${L.a.eye})`);
  for (let k = 0; k < 120; k++) U.lookStep(L, inp({ a: { ms: MS({ vocal: false }) } }), 1 / 60, false);
  assert.ok(L.a.eye < 0.05, "eye fades after the vocal");
  const S = U.lookNew();
  for (let k = 0; k < 30; k++) U.lookStep(S, inp({ a: { ms: MS({ vocal: false }), eye: 1 } }), 1 / 60, false);
  assert.ok(S.a.eye > 0.7, "a live vocal stem lights the eye too");
}
{
  // drop burst exactly on the line (the director's "drop" step), then it fades
  const L = U.lookNew();
  U.lookStep(L, inp({ last: "" }), 1 / 60, false);
  assert.strictEqual(L.a.burst, 0);
  U.lookStep(L, inp({ last: "drop" }), 1 / 60, false);
  assert.strictEqual(L.a.burst, 1, "burst on the drop line"); assert.strictEqual(L.a.bright, 1, "waveform cuts to full");
  assert.strictEqual(L.b.burst, 0, "only the deck that drops");
  assert.strictEqual(L.hits, 1);
  for (let k = 0; k < 60; k++) U.lookStep(L, inp({ last: "" }), 1 / 60, false);
  assert.ok(L.a.burst < 0.1, "burst fades within a second");
  // SHOW AUTO off: no moments in the elements (the look itself stays)
  const M = U.lookNew();
  U.lookStep(M, inp({ auto: false, last: "drop" }), 1 / 60, false);
  assert.strictEqual(M.a.burst, 0); assert.ok(M.a.ring > 0, "the look without moments");
  // building section brightens toward the drop
  const B = U.lookNew();
  for (let k = 0; k < 200; k++) U.lookStep(B, inp({ a: { ms: MS({ cls: "build", phrasePhase: 0.1 }) } }), 1 / 60, false);
  const early = B.a.bright;
  for (let k = 0; k < 200; k++) U.lookStep(B, inp({ a: { ms: MS({ cls: "build", phrasePhase: 0.95 }) } }), 1 / 60, false);
  assert.ok(B.a.bright > early + 0.3, "build brightens toward the drop");
  // scan line only in the first part of each bar
  U.lookStep(B, inp({ a: { ms: MS({ barPhase: 0 }) } }), 1 / 60, false);
  assert.strictEqual(B.a.scan, 1);
  U.lookStep(B, inp({ a: { ms: MS({ barPhase: 0.5 }) } }), 1 / 60, false);
  assert.strictEqual(B.a.scan, 0);
  U.lookStep(B, inp({ a: { ms: MS({ barPhase: 0 }) } }), 1 / 60, true);
  assert.strictEqual(B.a.scan, 0, "reduced motion: no scan sweep");
}
{
  // "DROP IN N BARS" from the anticipation queue (120 bpm: a bar = 2 s)
  assert.strictEqual(U.barsUntil(16, 0, 0.5), 8);
  assert.strictEqual(U.barsUntil(15.9, 0, 0.5), 8);
  assert.strictEqual(U.barsUntil(0.5, 0, 0.5), 1);
  assert.strictEqual(U.barsUntil(-1, 0, 0.5), 0);
  assert.strictEqual(U.countdownText("anyma drop", 8), "DROP IN 8 BARS");
  assert.strictEqual(U.countdownText("drop", 1), "DROP IN 1 BAR");
  assert.strictEqual(U.countdownText("DOUBLE DROP", 4), "DOUBLE DROP IN 4 BARS");
  assert.strictEqual(U.countdownText("drop", 17), "", "beyond two phrases: nothing");
  assert.strictEqual(U.countdownText("drop", 0), "");
  const L = U.lookNew();
  U.lookStep(L, inp({ now: 100, next: { kind: "anyma drop", at: 116 } }), 1 / 60, false);
  assert.strictEqual(L.countdown, "DROP IN 8 BARS");
  U.lookStep(L, inp({ now: 100, next: null }), 1 / 60, false);
  assert.strictEqual(L.countdown, "");
  U.lookStep(L, inp({ auto: false, now: 100, next: { kind: "drop", at: 116 } }), 1 / 60, false);
  assert.strictEqual(L.countdown, "", "no anticipation without SHOW AUTO");
  // the queue's real shape: the director's booked moment feeds it
  const bm = A.bookedMoment("ai-cue", { kind: "drop", at: 116, deck: "a", why: "drop" });
  U.lookStep(L, inp({ now: 100, next: bm }), 1 / 60, false);
  assert.ok(/IN 8 BARS$/.test(L.countdown), L.countdown);
  // VIBE cell
  L.scene = "FIGURE";
  assert.deepStrictEqual(V.sceneCell(L), { on: true, scene: "FIGURE", drop: L.countdown });
  assert.deepStrictEqual(V.sceneCell(null), { on: false, scene: "", drop: "" });
  assert.strictEqual(U.sceneLabel("head"), "ANDROID HEAD");
}
{
  // the figure: only during an Anyma drop (dir.dance > 0 after an "anyma" step), on that deck
  const L = U.lookNew();
  U.lookStep(L, inp({ last: "drop", dance: 0 }), 1 / 60, false);
  assert.strictEqual(L.a.figure, 0, "a plain drop: no figure");
  U.lookStep(L, inp({ last: "anyma", dance: 0.2 }), 1 / 60, false);
  assert.ok(L.a.figure > 0 && L.b.figure === 0, "Anyma drop: the figure on the dropping deck");
  // the real director: an "anyma" event -> dance for 16-32 bars, then gone
  const d = A.createDirector(3), ms = MS({ cls: "drop", phraseIdx: 4, phrasePhase: 0 });
  A.queueEvent(d, { type: "anyma", at: 0 });
  const F = U.lookNew();
  let seenLast = "", maxFig = 0, endT = null;
  for (let t = 0; t < 200; t += 1 / 30) {
    ms.beatPhase = (t / 0.5) % 1; ms.barPhase = (t / 2) % 1; ms.beatIdx = Math.floor(t / 0.5); ms.barIdx = Math.floor(t / 2);
    ms.phraseIdx = 4 + Math.floor(t / 16); ms.phrasePhase = (t % 16) / 16; ms.cls = t < 70 ? "drop" : "calm";
    A.stepDirector(d, ms, t, 1 / 30, false);
    if (d.last) seenLast = seenLast || d.last;
    U.lookStep(F, inp({ last: d.last, dance: d.dance, a: { ms } }), 1 / 30, false);
    maxFig = Math.max(maxFig, F.a.figure);
    if (maxFig > 0.5 && F.a.figure === 0 && endT === null) endT = t;
  }
  assert.strictEqual(seenLast, "anyma");
  assert.ok(maxFig > 0.9, `figure dances (${maxFig})`);
  assert.ok(endT !== null && endT >= 16 * 2 && endT <= 32 * 2 + 6, `figure gone after 16-32 bars + its fade (${endT})`);
  // stays inside its element: every point of every pose inside the lane box
  const box = U.figureBox(300, 84), pts = new Float32Array(U.FIG_SEGS * 4);
  assert.ok(box.w <= 60 && box.x >= 0 && box.y >= 0 && box.x + box.w <= 300 && box.y + box.h <= 84, JSON.stringify(box));
  const dn = A.danceNew();
  for (let k = 0; k < 400; k++) {
    const m = MS({ beatPhase: (k % 10) / 10, barPhase: (k % 40) / 40, beatIdx: k, barIdx: k >> 2, energy: 1 });
    A.dancePose(m, { weight: 1, eye: 1 }, false, dn);
    U.figurePoints(box, dn, A.POSES[dn.poseIdx], pts);
    for (let j = 0; j < pts.length; j += 2) {
      assert.ok(pts[j] >= box.x - 1e-3 && pts[j] <= box.x + box.w + 1e-3 && pts[j + 1] >= box.y - 1e-3 && pts[j + 1] <= box.y + box.h + 1e-3, "inside the box");
    }
  }
  // supermove red for the meters; SHOW off: all dark
  const R = U.lookNew();
  U.lookStep(R, inp({ red: 1, last: "supermove" }), 1 / 60, false);
  assert.strictEqual(R.red, 1);
  U.lookStep(R, { on: false }, 1 / 60, false);
  assert.strictEqual(R.red, 0); assert.strictEqual(R.on, false); assert.strictEqual(R.scene, "");
}

// ---- glue in the real page DOM (sim DOM): idle + SHOW on -> no stage, no translucency --
const { Document } = require("../sim/js/dom.js");
function page(style) {
  const doc = new Document();
  doc.load(fs.readFileSync(path.join(STATIC, "index.html"), "utf8"));
  if (style === "anyma") doc.documentElement.classList.add("anyma-look");
  let t = 0, rafs = 0, cb = null;
  const win = {
    document: doc, performance: { now: () => t }, matchMedia: () => ({ matches: false, addEventListener() {} }),
    requestAnimationFrame: (f) => { cb = f; return ++rafs; }, cancelAnimationFrame() { cb = null; }, setInterval() {}, setTimeout() {},
    addEventListener() {}, dispatchEvent() {}, devicePixelRatio: 1, innerWidth: 1600, innerHeight: 900, scrollY: 0,
    Date, Math, Number, JSON, Float32Array, Float64Array, Uint8Array, Object, Array, String, Map, Set, Infinity, NaN,
  };
  win.window = win;
  vm.createContext(win);
  for (const f of ["anyma-ui.js", "anyma-show.js"]) vm.runInContext(fs.readFileSync(path.join(STATIC, f), "utf8"), win);
  const toggle = doc.getElementById("ap-show-toggle"), auto = doc.getElementById("ap-show-auto");
  // run the page's own frames for `secs` of wall clock (ms clock)
  const run = (secs) => { const end = t + secs * 1000; while (t < end) { t += 1000 / 30; const f = cb; cb = null; if (f) f(); } };
  return { doc, win, toggle, auto, rafs: () => rafs, run, now: () => t };
}
// a synthetic song: 120 bpm, quiet intro, a build, then the drop at 96 s on a phrase line
function song() {
  const beats = [], downs = [], phr = [], ec = [], et = [];
  for (let s = 0; s < 200; s += 0.5) beats.push(s);
  for (let s = 0; s < 200; s += 2) downs.push(s);
  for (let s = 0; s < 200; s += 16) phr.push(s);
  for (let s = 0; s < 200; s++) { et.push(s); ec.push(s < 64 ? 0.2 : s < 96 ? 0.15 + 0.1 * ((s - 64) / 32) : 0.95); }
  return { bpm: 120, duration: 200, beat_times: beats, downbeat_times: downs, phrase_boundaries_8bar: phr,
    energy_curve: ec, energy_times: et, vocal_active_regions: [[20, 40]],
    sections: [{ label: "intro", start: 0, end: 64 }, { label: "build", start: 64, end: 96 }, { label: "drop", start: 96, end: 200 }] };
}
const TRANSLUCENT = /show-embed|show-full|translucent/;
for (const style of ["anyma", "classic"]) {
  const P = page(style);
  const stage = P.doc.querySelector(".anyma-stage");
  assert.ok(stage && stage.hidden, `${style}: stage hidden at load`);
  P.toggle.checked = true; P.toggle.dispatchEvent({ type: "change", bubbles: true });
  assert.strictEqual(P.win.anymaShow.mode, "elements", "SHOW on = the elements");
  assert.ok(stage.hidden, `${style}: idle + SHOW on -> no stage`);
  assert.ok(!TRANSLUCENT.test(P.doc.body.className), `${style}: no translucent class on the body (${P.doc.body.className})`);
  for (const sel of [".wave-stage", ".wave-lane", ".deck-panel", ".mixer", ".workspace"]) {
    for (const el of P.doc.querySelectorAll(sel)) assert.ok(!TRANSLUCENT.test(el.className), `${sel} not translucent`);
  }
  // SHOW AUTO on, a set running: frames never open the stage
  P.auto.checked = true;
  P.win.autopilotState = { active: true };
  const t0 = P.now();
  const g = (v) => ({ gain: { value: v } });
  P.win.decks = { a: { playing: true, bpm: 120, analysis: song(), crossfaderGain: g(1), volumeGain: g(1),
    _currentPosition: () => 60 + (P.now() - t0) / 1000 }, b: { playing: false, crossfaderGain: g(0), volumeGain: g(1) } };
  P.toggle.checked = false; P.toggle.dispatchEvent({ type: "change", bubbles: true });
  P.toggle.checked = true; P.toggle.dispatchEvent({ type: "change", bubbles: true });   // wakes the loop
  let maxBurst = 0, sawCd = "", open = false;
  const ring = P.doc.querySelector('.jog-wheel[data-deck="a"] .an-ring');
  let ringVals = new Set();
  for (let k = 0; k < 60; k++) {                                // 60 s: build -> drop at 96 s -> groove
    P.run(1);
    const L = P.win.anymaUi.look;
    maxBurst = Math.max(maxBurst, L.a.burst); if (L.countdown) sawCd = L.countdown;
    if (!stage.hidden || P.win.anymaShow.mode !== "elements") open = true;
    ringVals.add(ring.style.opacity);
  }
  assert.ok(!open, `${style}: SHOW AUTO never opens the stage over a whole drop`);
  assert.ok(!TRANSLUCENT.test(P.doc.body.className));
  if (style === "anyma") {
    assert.ok(P.win.anymaUi.look.hits > 0 && maxBurst > 0, `the drop hit the platter (${P.win.anymaUi.look.hits})`);
    assert.ok(ringVals.size > 3, "platter ring opacity moves on the beat");
    assert.ok(/DROP IN \d+ BARS?/.test(sawCd), `countdown before the drop (${sawCd})`);
  } else {
    assert.strictEqual(P.win.anymaUi.look.hits, 0, "classic: no element moments");
    assert.ok(ringVals.size <= 1, "classic: element layers untouched");
  }
  // element layers exist inside the elements, hidden by CSS unless anyma-look + anyma-el
  assert.strictEqual(P.doc.querySelectorAll(".jog-wheel .an-ring").length, 2);
  assert.strictEqual(P.doc.querySelectorAll(".jog-label-ring .an-face").length, 2);
  assert.strictEqual(P.doc.querySelectorAll(".wave-lane .an-clip .an-fig").length, 2);
  // classic: one update -> the element class stays off (classic look untouched)
  P.win.anymaUi.update(Object.assign(U.inputNew(), { on: true }), 1 / 60, false);
  assert.strictEqual(P.doc.documentElement.classList.contains("anyma-el"), style === "anyma", `${style}: anyma-el`);
  // SHOW off: element class gone
  P.toggle.checked = false; P.toggle.dispatchEvent({ type: "change", bubbles: true });
  assert.strictEqual(P.win.anymaShow.mode, "off");
  assert.ok(!P.doc.documentElement.classList.contains("anyma-el"));
}
// CSS contract: every element rule is gated on .anyma-look.anyma-el (classic unaffected),
// layers are absolutely placed and never catch the pointer
{
  const css = fs.readFileSync(path.join(STATIC, "anyma-ui.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  for (const rule of css.split("}")) {
    const sel = rule.split("{")[0].trim();
    if (!sel || sel.startsWith("@") || !rule.includes("{")) continue;
    for (const s of sel.replace(/:is\([^)]*\)/g, ":is()").split(",").map((x) => x.trim())) {
      if (/^\.(an-ring|an-burst|an-face|an-clip|an-glow|an-scan|an-fig)\b/.test(s) || /^\.an-face b/.test(s)) continue;   // own layers (display:none unless gated)
      assert.ok(/^\.anyma-look\.anyma-el\b/.test(s), `ungated rule: ${s}`);
    }
  }
  assert.ok(/\.an-ring, \.an-burst, \.an-face, \.an-clip \{ display: none; \}/.test(css));
  assert.ok(/\.an-clip \{ position: absolute; inset: 0; overflow: hidden; pointer-events: none;/.test(css), "figure clipped to its lane");
  const js = fs.readFileSync(path.join(STATIC, "anyma-ui.js"), "utf8");
  assert.ok(!/requestAnimationFrame/.test(js), "no rAF of its own: the shared loop drives it");
  assert.ok(!/\.style\.(top|left|width|height|margin)/.test(js), "no layout writes per frame");
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  assert.ok(html.indexOf('src="/anyma-ui.js"') > 0 && html.includes('href="/anyma-ui.css"'));
}
console.log("anyma ui ok");
