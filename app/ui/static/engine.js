// Engine composition root: the one seam between the AI DJ's decision layer and the world it runs in.
//
//   Engine = the decision code (autopilot.js, dj-mind.js, stem-moves.js, riff-over-rap.js, ai-actions.js,
//            tempo-rule.js). It touches nothing outside the HOST PORT below: no window, document,
//            AudioContext, Date.now, setTimeout or fetch of its own.
//   Host   = whatever supplies the port. Two ship today:
//              browser   createWindowHost(window), used by the live console (host-browser.js)
//              sim       app/sim/js/host-sim.js: virtual clock, recording audio graph, the real API over HTTP
//   Ai     = the model client the engine talks to for its two LLM jobs (suggestion, transition plan):
//            {suggest(body), plan(body)}. The default is HTTP to the server's /api/autopilot/*; which
//            model answers (local LLM, a replay fixture, a stub) is the SERVER's Engine (app/ui/engine.py).
//
// Plain dependency injection, no framework: every engine module is `create({host, ai})` and returns its API;
// this file's `mount(name, create)` is how a module file hands its factory to the composition root of
// whichever host loaded it. Modules keep their pure `core` exports for node checks either way.
(function (root) {
  "use strict";

  // ---- the port ----------------------------------------------------------------------------------------------
  // name -> member names the engine calls (functions unless marked). assertHost() checks a host against it.
  const HOST_PORT = {
    clock: ["now", "perfNow", "setTimeout", "clearTimeout", "setInterval", "clearInterval", "raf", "cancelRaf", "audioNow"],
    api: ["fetch"],                                   // transport to the server (the real API in both hosts)
    bus: ["emit", "on", "off"],                       // console events: ai-activity, ai-cue, ai-supermove, ai-energy ...
    log: ["step"],                                    // the AI's step log (aiStep)
    ui: ["el", "query", "queryAll", "create", "fire", "status", "flag", "cssVar"],   // presentation hooks; inert in the sim
    storage: ["get", "set"],
    random: ["uuid", "next"],
  };
  // plain-value members: audio (AudioContext-like), decks ({a, b}), state ({trackA, trackB}), session ({relaxed}),
  // mod (engine modules + UI modules by name), expose(name, value), loadIntoDeck(deckId, trackId, name, blob)
  const HOST_VALUES = ["audio", "decks", "state", "session", "mod"];
  const HOST_FUNCS = ["expose", "loadIntoDeck"];

  // The deck API the engine calls (deck-controller.js Deck implements it; the sim runs that same class on a
  // recording audio graph). Documented here so a new host knows the contract; the contract check verifies it.
  const DECK_API = {
    fields: ["bpm", "analysis", "buffer", "playing", "stems", "stemsReady", "tempoStems", "cuePoint", "startOffset", "stemState",
      "crossfaderGain", "volumeGain", "lowFilter", "midFilter", "highFilter", "inputGain", "mixGain", "fame", "hookDrops"],
    methods: ["_playbackRate", "_currentPosition", "_positionAt", "play", "stopNow", "stopSourcesAt", "stemMix", "holdStem",
      "rearmStems", "rampPitchPercent", "aiSetPitch", "setEQ", "setLoopBeats", "useTempoStems", "fetchTempoStems",
      "swapTempoStemsAt", "onMaster", "brake", "seek", "jumpBeats", "toggleLoop"],
  };

  function assertHost(host, what = "host") {
    const missing = [];
    for (const [group, names] of Object.entries(HOST_PORT)) {
      const g = host && host[group];
      if (!g) { missing.push(group); continue; }
      for (const n of names) if (typeof g[n] !== "function") missing.push(`${group}.${n}`);
    }
    for (const v of HOST_VALUES) if (!host || !(v in host)) missing.push(v);
    for (const f of HOST_FUNCS) if (!host || typeof host[f] !== "function") missing.push(f);
    if (missing.length) throw new Error(`${what} does not satisfy the Host port; missing: ${missing.join(", ")}`);
    return host;
  }

  // ---- the model client (default: HTTP to the server) --------------------------------------------------------------------------
  function createHttpAi(api) {
    const post = async (url, body) => {
      const res = await api.fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      return data;
    };
    return { suggest: (body) => post("/api/autopilot/suggest", body), plan: (body, opts) => api.fetch("/api/autopilot/plan", Object.assign({
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }, opts || {})) };
  }

  // ---- composition root ----------------------------------------------------------------------------------------------------------
  const Engine = { HOST_PORT, DECK_API, assertHost, createHttpAi, host: null, ai: null, mods: {}, factories: {} };

  // A host loads engine.js, builds itself, and calls use(host): from then on every mounted module is created
  // with it, at the moment its script loads (same timing as when the modules created themselves).
  Engine.use = function use(host, ai) {
    Engine.host = assertHost(host);
    Engine.ai = ai || createHttpAi(host.api);
    return Engine;
  };

  // Called by each engine module file at load: register the factory; create it now when a host is present.
  Engine.mount = function mount(name, create) {
    Engine.factories[name] = create;
    if (!Engine.host) return null;                       // node checks: the module stays a pure `core` export
    return Engine.create(name);
  };
  Engine.create = function create(name) {
    const host = Engine.host;
    const api = Engine.factories[name]({ host, ai: Engine.ai });
    Engine.mods[name] = api;
    host.mod[name] = api;
    return api;
  };

  // The browser host, built over a window-like object. Thin on purpose: every member is one line of the world.
  // Anything the live console gets from `window` / `document` goes through here and nowhere else.
  Engine.createWindowHost = function createWindowHost(win, getAudio, getLoad, getStatus) {
    const doc = win.document;
    const ui = {
      el: (id) => doc.getElementById(id),
      query: (sel) => doc.querySelector(sel),
      queryAll: (sel) => doc.querySelectorAll(sel),
      create: (tag) => doc.createElement(tag),
      fire: (el, type, bubbles) => el.dispatchEvent(new win.Event(type, { bubbles: !!bubbles })),
      cssVar: (id, name) => { const el = doc.getElementById(id); return el ? String(win.getComputedStyle(el).getPropertyValue(name)).trim() : ""; },
      status: (msg) => { const f = getStatus && getStatus(); if (typeof f === "function") f(msg); },
      flag: (id, dflt = true) => { const el = doc.getElementById(id); return el ? !!el.checked : dflt; },
    };
    const store = win.localStorage;
    return {
      clock: {
        now: () => win.Date.now(), perfNow: () => win.performance.now(),
        setTimeout: win.setTimeout.bind(win), clearTimeout: win.clearTimeout.bind(win),
        setInterval: win.setInterval.bind(win), clearInterval: win.clearInterval.bind(win),
        raf: (fn) => win.requestAnimationFrame(fn), cancelRaf: (id) => win.cancelAnimationFrame(id),
        audioNow: () => getAudio().currentTime,
      },
      get audio() { return getAudio(); },
      get decks() { return win.decks; },
      get state() { return win.state; },
      get session() { return win.djSession || (win.djSession = { relaxed: false }); },
      mod: new Proxy({}, { get: (t, k) => (k in t ? t[k] : win[k]), set: (t, k, v) => { t[k] = v; win[k] = v; return true; } }),
      api: { fetch: (url, opts) => win.fetch(url, opts) },
      bus: {
        emit: (type, detail) => win.dispatchEvent(new win.CustomEvent(type, { detail })),
        on: (type, fn) => win.addEventListener(type, fn), off: (type, fn) => win.removeEventListener(type, fn),
      },
      log: { step: (kind, o) => { if (typeof win.aiStep === "function") win.aiStep(kind, o); } },
      ui,
      storage: { get: (k) => { try { return store.getItem(k); } catch (e) { return null; } }, set: (k, v) => { try { store.setItem(k, v); } catch (e) { /* private mode */ } } },
      random: { uuid: () => (win.crypto && win.crypto.randomUUID ? win.crypto.randomUUID() : `${win.Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`), next: () => Math.random() },
      expose: (name, value) => { win[name] = value; },
      loadIntoDeck: (...a) => getLoad()(...a),
    };
  };

  root.Engine = Engine;
  if (typeof module !== "undefined" && module.exports) module.exports = Engine;
})(typeof window !== "undefined" ? window : globalThis);
