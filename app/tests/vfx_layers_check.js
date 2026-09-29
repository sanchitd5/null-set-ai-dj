// NULL-BOT's VFX layer always draws: whatever SHOW (off / elements / full) and
// ANYMA LOOK say, the bass band and the classic kick / drop / transition effects
// still fire. ANYMA only ADDS its motifs; the manual STAGE only dims the canvas.
// Runs the real visuals.js in a vm against a recording 2D context.
const assert = require("assert");
const fs = require("fs"), path = require("path"), vm = require("vm");
const SRC = fs.readFileSync(process.env.VFX_SRC || path.join(__dirname, "../ui/static/visuals.js"), "utf8");
const A = require("../ui/static/anyma-show.js");

function run({ style, show, cue }) {
  let t = 1000, raf = null;
  const calls = {}, low = [];
  const rec = (name, args) => { calls[name] = (calls[name] || 0) + 1; if (name === "fillRect" || name === "lineTo") low.push(args[1] + (args[3] || 0)); };
  const grad = { addColorStop() {} };
  const ctx = new Proxy({}, {
    get(o, k) {
      if (k in o) return o[k];
      if (k === "createLinearGradient" || k === "createRadialGradient") return (...a) => { rec(k, a); return grad; };
      return (...a) => rec(k, a);
    },
    set(o, k, v) { o[k] = v; return true; },
  });
  const canvas = { className: "", style: {}, width: 0, height: 0, setAttribute() {}, getContext: () => ctx };
  const listeners = {};
  const cls = new Set();
  const doc = {
    hidden: false,
    createElement: () => canvas,
    body: { appendChild() {} },
    getElementById: () => null,
    documentElement: { classList: { toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)), contains: (c) => cls.has(c) } },
    addEventListener() {},
  };
  const store = { "djAiToggles.v1": JSON.stringify({ "vfx-anyma-toggle": style === "anyma" }) };
  const sb = {
    document: doc, performance: { now: () => t },
    localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem() {} },
    matchMedia: () => ({ matches: false }),
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    requestAnimationFrame: (cb) => { raf = cb; return 1; }, cancelAnimationFrame() { raf = null; },
    setInterval() {}, setTimeout: (f) => f(), innerWidth: 1600, innerHeight: 800, devicePixelRatio: 1,
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    audioCtx: { currentTime: 0, sampleRate: 44100,
      createAnalyser: () => ({ frequencyBinCount: 512, connect() {}, getByteFrequencyData: (b) => b.fill(Math.floor(t / 250) % 2 ? 240 : 40) }) },
    masterOut: { connect() {} },
    decks: { a: { playing: true, bpm: 124, crossfaderGain: { gain: { value: 1 } } }, b: { playing: false } },
    autopilotState: { active: true },
    anymaShow: { mode: show, core: A },
    Math, Number, JSON, Float32Array, Uint8Array, Object, Array, String, Map, Set, Infinity, NaN, parseInt,
  };
  sb.window = sb;
  vm.createContext(sb);
  vm.runInContext(SRC, sb);
  const frame = () => { const f = raf; raf = null; t += 1000 / 60; if (f) f(); };
  for (let i = 0; i < 60; i++) frame();                       // 1 s of music: bass band + kicks
  const before = { ...calls };
  if (cue) for (const fn of listeners["ai-cue"] || []) fn({ detail: { kind: cue, deck: "a", at: NaN } });
  for (let i = 0; i < 20; i++) frame();
  const after = {};
  for (const k of Object.keys(calls)) after[k] = calls[k] - (before[k] || 0);
  return { calls, after, low, opacity: canvas.style.opacity, anyma: cls.has("anyma-look"), nul: sb.nulVisuals };
}

const H = 400;                                                  // innerHeight * 0.5
for (const cue of ["drop", "transition"]) {
  const base = run({ style: "classic", show: "off", cue });
  assert.ok(base.low.some((y) => y > 0.8 * H), "classic: the bass band draws at the bottom");
  const quiet = run({ style: "classic", show: "off", cue: null });
  const extra = Object.keys(base.after).reduce((n, k) => n + base.after[k] - (quiet.after[k] || 0), 0);
  assert.ok(extra > 0, `classic: the ${cue} effect draws`);
  for (const show of ["off", "elements", "full", "embed"]) {   // "embed": the old default that hid it
    for (const style of ["classic", "anyma"]) {
      const r = run({ style, show, cue });
      assert.strictEqual(r.anyma, style === "anyma", "style class");
      assert.ok(r.low.some((y) => y > 0.8 * H), `${style} + SHOW ${show}: bass band still drawn`);
      // the classic calls all still happen (ANYMA only adds on top)
      for (const [k, n] of Object.entries(base.calls)) {
        assert.ok((r.calls[k] || 0) >= n, `${style} + SHOW ${show} + ${cue}: ${k} ${r.calls[k] || 0} < classic ${n}`);
      }
      if (process.env.VFX_SRC) continue;                      // (old-source sanity run: stop here)
      assert.strictEqual(r.opacity || "", show === "full" ? "0.5" : "", "only the manual STAGE dims the layer");
      assert.strictEqual(typeof r.nul.addTick, "function");
    }
  }
}
console.log("vfx layers ok");
