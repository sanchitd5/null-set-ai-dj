// The headless console: index.html's own scripts, unmodified, run in this node process against a
// virtual clock, a small DOM, a recording Web Audio API and the real API over HTTP.
//
//   env = await createEnv({ staticDir, port, ... });   // globals installed, scripts loaded
//   await env.clock.run(untilVirtualSeconds);            // the console lives on the virtual clock
//
// Nothing of the console is replaced except the browser it sits in.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { VirtualClock } = require("./clock");
const { Document, inert } = require("./dom");
const { FakeAudioContext, FakeAudioBuffer, FakeAudioWorkletNode } = require("./webaudio");
const { Net } = require("./net");
const { parseWav } = require("./wav");

class Listeners {
  constructor() { this.map = new Map(); }
  add(t, fn) { if (!fn) return; let l = this.map.get(t); if (!l) this.map.set(t, (l = [])); if (!l.includes(fn)) l.push(fn); }
  remove(t, fn) { const l = this.map.get(t); if (l) this.map.set(t, l.filter((f) => f !== fn)); }
  fire(target, ev) { for (const fn of [...(this.map.get(ev.type) || [])]) { try { typeof fn === "function" ? fn.call(target, ev) : fn.handleEvent(ev); } catch (e) { if (target.__onError) target.__onError(e, ev); else throw e; } } }
}

function makeFakeWaveSurfer(clock) {
  class WS {
    constructor(o) { this.opts = o; this._l = {}; this._dur = 0; this._t = 0; this._plugins = []; this._ready = false; }
    static create(o) { return new WS(o); }
    on(evt, cb) { (this._l[evt] || (this._l[evt] = [])).push(cb); return () => { this._l[evt] = (this._l[evt] || []).filter((f) => f !== cb); }; }
    once(evt, cb) { const off = this.on(evt, (...a) => { off(); cb(...a); }); return off; }
    un(evt, cb) { this._l[evt] = (this._l[evt] || []).filter((f) => f !== cb); }
    _emit(evt, ...a) { for (const f of [...(this._l[evt] || [])]) f(...a); }
    load(url, channels, duration) {
      this._dur = duration || 0; this._ready = false;
      // WaveSurfer 7 with decoded channels answers "ready" within microtasks: the console plays the
      // deck right after `await loadIntoDeck`, so the deck must already hold its buffer then
      return Promise.resolve().then(() => { this._ready = true; this._emit("ready"); });
    }
    loadBlob() { return this.load(); }
    getDecodedData() { return null; }
    getDuration() { return this._dur; }
    getCurrentTime() { return this._t; }
    seekTo(p) { this._t = p * this._dur; } setTime(t) { this._t = t; }
    play() { return Promise.resolve(); } pause() {} stop() {} playPause() {}
    zoom() {} setOptions() {} destroy() {} empty() {} isPlaying() { return false; }
    getWidth() { return 800; } getWrapper() { return document.createElement("div"); } getScroll() { return 0; } setScroll() {}
    registerPlugin(p) { this._plugins.push(p); return p; }
    exportPeaks() { return []; } setMuted() {} setVolume() {} getMediaElement() { return null; }
  }
  const Regions = { create() { return { on() {}, addRegion(o) { return Object.assign({ remove() {}, setOptions() {} }, o); }, clearRegions() {}, getRegions() { return []; }, enableDragSelection() {} }; } };
  WS.Regions = Regions;
  return WS;
}

class FakeWorker {
  constructor(clock, staticDir, url) {
    this._clock = clock; this.onmessage = null; this.onerror = null;
    const file = path.join(staticDir, String(url).replace(/^\//, ""));
    const posts = [];
    const sandbox = { self: null, module: { exports: {} }, console };
    sandbox.self = { postMessage: (data) => { this._clock.setTimeout(() => this.onmessage && this.onmessage({ data }), 5); }, onmessage: null };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(file, "utf8"), sandbox, { filename: file });
    this._sb = sandbox;
  }
  postMessage(data) { this._clock.setTimeout(() => { try { this._sb.self.onmessage({ data }); } catch (e) { this.onerror && this.onerror(e); } }, 0); }
  terminate() {}
  addEventListener() {}
}

async function createEnv(opts) {
  const staticDir = opts.staticDir;
  const clock = new VirtualClock();
  const logs = { console: [], events: [], errors: [], missing: new Set() };
  const g = globalThis;

  // ---- time -----------------------------------------------------------------------------
  const RealDate = Date;
  class VDate extends RealDate {
    constructor(...a) { if (a.length === 0) super(clock.dateNow()); else super(...a); }
    static now() { return clock.dateNow(); }
  }
  g.Date = VDate;
  Object.defineProperty(g, "performance", { value: { now: () => clock.ms(), timeOrigin: clock.dateNow(), mark() {}, measure() {}, getEntriesByName() { return []; } }, configurable: true, writable: true });
  g.setTimeout = (fn, ms, ...a) => clock.setTimeout(fn, ms, ...a);
  g.setInterval = (fn, ms, ...a) => clock.setInterval(fn, ms, ...a);
  g.clearTimeout = (id) => clock.clear(id);
  g.clearInterval = (id) => clock.clear(id);
  // frames are painting only (meters, jog wheels): a coarse 4 Hz "display" keeps the run fast
  g.requestAnimationFrame = (fn) => clock.setTimeout(() => fn(clock.ms()), opts.frameMs || 250);
  g.cancelAnimationFrame = (id) => clock.clear(id);
  g.requestIdleCallback = (fn) => clock.setTimeout(() => fn({ timeRemaining: () => 10, didTimeout: false }), 1);
  g.cancelIdleCallback = (id) => clock.clear(id);
  clock.onError = (e, t) => { logs.errors.push({ t: +clock.now.toFixed(3), where: "timer", error: String(e && e.stack || e).slice(0, 600) }); };
  // seeded randomness: the console's Math.random (ids, jitter) must not vary between runs
  let seed = (opts.seed || 1) >>> 0;
  Math.random = () => { seed = (seed + 0x6D2B79F5) >>> 0; let t = seed; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };

  // a browser logs an unhandled rejection and carries on; so does the sim (and reports it)
  process.on("unhandledRejection", (e) => { logs.errors.push({ t: +clock.now.toFixed(3), where: "unhandledRejection", error: String(e && e.stack || e).slice(0, 500) }); });
  process.on("uncaughtException", (e) => { logs.errors.push({ t: +clock.now.toFixed(3), where: "uncaughtException", error: String(e && e.stack || e).slice(0, 500) }); });

  // ---- console capture ------------------------------------------------------------------------
  const realConsole = console;
  for (const lvl of ["log", "info", "warn", "error", "debug"]) {
    g.console[lvl] = (...args) => {
      const text = args.map((a) => (typeof a === "string" ? a : a instanceof Error ? a.message : safe(a))).join(" ");
      logs.console.push({ t: +clock.now.toFixed(3), level: lvl, text: text.slice(0, 400) });
      if (opts.echo) process.stderr.write(`[${clock.now.toFixed(1)}s ${lvl}] ${text.slice(0, 300)}\n`);
    };
  }

  // ---- events: plain, writable objects (node's own Event has a read-only target) -----------------------------------
  class SimEvent {
    constructor(type, init = {}) {
      this.type = String(type); this.bubbles = !!init.bubbles; this.cancelable = !!init.cancelable; this.composed = false;
      this.defaultPrevented = false; this.target = null; this.currentTarget = null; this.timeStamp = clock.ms(); this.isTrusted = false;
      this.detail = init.detail === undefined ? null : init.detail;
    }
    preventDefault() { if (this.cancelable) this.defaultPrevented = true; }
    stopPropagation() {} stopImmediatePropagation() {}
  }
  class SimEventTarget {
    constructor() { Object.defineProperty(this, "_l", { value: new Listeners(), enumerable: false }); }
    addEventListener(t, fn, o) { this._l.add(t, fn); } removeEventListener(t, fn) { this._l.remove(t, fn); }
    dispatchEvent(ev) { ev.target = ev.target || this; ev.currentTarget = this; this._l.fire(this, ev); return !ev.defaultPrevented; }
  }
  g.Event = SimEvent; g.CustomEvent = SimEvent; g.EventTarget = SimEventTarget;

  // ---- window / document ---------------------------------------------------------------------------------
  const html = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
  const doc = new Document();
  doc.load(html);
  g.document = doc;
  g.window = g; g.self = g; g.top = g; g.parent = g; g.globalThis = g;
  doc._view = g;
  const wl = new Listeners();
  g.__windowListeners = wl;
  g.addEventListener = (t, fn) => wl.add(t, fn);
  g.removeEventListener = (t, fn) => wl.remove(t, fn);
  g.dispatchEvent = (ev) => { ev.target = ev.target || g; wl.fire(g, ev); return !ev.defaultPrevented; };
  wl.__onError = null;
  g.__onError = (e, ev) => logs.errors.push({ t: +clock.now.toFixed(3), where: `event ${ev && ev.type}`, error: String(e && e.stack || e).slice(0, 600) });
  doc.__onError = g.__onError;
  Object.assign(g, {
    innerWidth: 1600, innerHeight: 900, outerWidth: 1600, outerHeight: 900, devicePixelRatio: 1, scrollX: 0, scrollY: 0, pageXOffset: 0, pageYOffset: 0,
    screen: { width: 1600, height: 900, availWidth: 1600, availHeight: 900 }, location: { href: "http://sim/", origin: "http://sim", protocol: "http:", host: "sim", hostname: "sim", pathname: "/", search: "", hash: "" },
    history: { pushState() {}, replaceState() {}, back() {}, state: null },
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
    getComputedStyle: () => new Proxy({}, { get: (t, k) => (k === "getPropertyValue" ? () => "" : "") }),
    scrollTo() {}, scrollBy() {}, focus() {}, blur() {}, open() { return null; }, alert() {}, confirm() { return true; }, prompt() { return null; },
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
    IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
    MutationObserver: class { observe() {} disconnect() {} takeRecords() { return []; } },
    HTMLElement: class {}, Element: class {}, Node: class {}, HTMLCanvasElement: class {}, HTMLInputElement: class {}, HTMLAudioElement: class {},
    KeyboardEvent: class extends SimEvent { constructor(t, o = {}) { super(t, o); Object.assign(this, o); } },
    MouseEvent: class extends SimEvent { constructor(t, o = {}) { super(t, o); Object.assign(this, o); } },
    PointerEvent: class extends SimEvent { constructor(t, o = {}) { super(t, o); Object.assign(this, o); } },
    WheelEvent: class extends SimEvent { constructor(t, o = {}) { super(t, o); Object.assign(this, o); } },
    TouchEvent: class extends SimEvent { constructor(t, o = {}) { super(t, o); Object.assign(this, o); } },
    Image: class { constructor() { this.style = {}; } set src(v) { this._s = v; } },
    Audio: class { constructor() { this.style = {}; this.paused = true; } play() { return Promise.resolve(); } pause() {} load() {} set src(v) {} addEventListener() {} removeEventListener() {} },
    FileReader: class { readAsArrayBuffer() {} readAsDataURL() {} readAsText() {} },
    localStorage: memoryStorage(), sessionStorage: memoryStorage(),
    webkitAudioContext: undefined,
  });
  Object.defineProperty(g, "navigator", { value: { userAgent: "sim-headless", platform: "sim", language: "en", languages: ["en"], hardwareConcurrency: 4, mediaDevices: undefined, clipboard: { writeText: () => Promise.resolve() }, requestMIDIAccess: undefined, getGamepads: () => [], onLine: true }, configurable: true, writable: true });

  // ---- audio ------------------------------------------------------------------------------------------------------------
  const audioOpts = { sampleRate: 4000 };
  g.AudioContext = function (o) {
    const ctx = new FakeAudioContext(clock, audioOpts);
    ctx.decode = (ab) => {
      const w = parseWav(ab);
      return new FakeAudioBuffer({ sampleRate: w.sampleRate, numberOfChannels: w.channels.length, length: w.channels[0].length, channels: w.channels, tag: w.tag });
    };
    env.audio = ctx;
    return ctx;
  };
  g.OfflineAudioContext = g.AudioContext;
  g.AudioWorkletNode = FakeAudioWorkletNode;
  g.AudioBuffer = FakeAudioBuffer;
  g.MediaRecorder = class { constructor() { this.state = "inactive"; } start() {} stop() {} addEventListener() {} static isTypeSupported() { return false; } };
  g.WaveSurfer = makeFakeWaveSurfer(clock);
  g.WaveSurfer.Regions = g.WaveSurfer.Regions;
  g.Worker = class extends FakeWorker { constructor(url) { super(clock, staticDir, url); } };

  // ---- network ------------------------------------------------------------------------------------------------------------------
  const net = new Net({ host: "127.0.0.1", port: opts.port, clock });
  g.fetch = (input, o) => net.fetch(input, o);
  g.XMLHttpRequest = class { open() {} send() {} setRequestHeader() {} };
  if (typeof g.crypto === "undefined" || !g.crypto.randomUUID) Object.defineProperty(g, "crypto", { value: { randomUUID: () => `sim-${Math.floor(Math.random() * 1e9).toString(16)}`, getRandomValues: (a) => a }, configurable: true, writable: true });
  else Object.defineProperty(g, "crypto", { value: { randomUUID: () => `sim-${Math.floor(Math.random() * 1e9).toString(16)}`, getRandomValues: (a) => a, subtle: g.crypto.subtle }, configurable: true, writable: true });

  const env = { clock, doc, logs, net, audio: null, staticDir, opts, window: g, scripts: [] };

  // ---- the console's own scripts, in index.html order ----------------------------------------------------------------------------------------------
  env.loadScripts = () => {
    const order = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map((m) => m[1]).filter((s) => !/^https?:/.test(s));
    const skip = new Set(opts.skip || []);
    for (const src of order) {
      const name = src.replace(/^\//, "");
      if (skip.has(name)) { env.scripts.push({ name, skipped: true }); continue; }
      const file = path.join(staticDir, name);
      try {
        vm.runInThisContext(fs.readFileSync(file, "utf8"), { filename: file });
        env.scripts.push({ name, ok: true });
      } catch (e) {
        env.scripts.push({ name, ok: false, error: String(e && e.stack || e).split("\n").slice(0, 3).join(" | ") });
        logs.errors.push({ t: +clock.now.toFixed(3), where: `load ${name}`, error: String(e && e.stack || e).slice(0, 800) });
      }
    }
    // the page is "ready": the console's DOMContentLoaded / load handlers run
    for (const t of ["DOMContentLoaded", "load"]) { const ev = new Event(t); doc.dispatchEvent(ev); g.dispatchEvent(ev); }
  };
  // record every console CustomEvent the engine emits (ai-activity, ai-supermove, ai-cue, ai-energy ...)
  for (const t of ["ai-activity", "ai-supermove", "ai-cue", "ai-energy", "ai-step", "dj-move"]) {
    g.addEventListener(t, (ev) => logs.events.push({ t: +clock.now.toFixed(3), type: t, detail: safe(ev.detail) }));
  }
  return env;
}

function memoryStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => { m.set(k, String(v)); }, removeItem: (k) => { m.delete(k); }, clear: () => m.clear(), key: (i) => [...m.keys()][i] || null, get length() { return m.size; } };
}
function safe(o) { try { return JSON.parse(JSON.stringify(o, (k, v) => (typeof v === "function" ? undefined : v))); } catch (e) { return String(o); } }

module.exports = { createEnv, safe };
