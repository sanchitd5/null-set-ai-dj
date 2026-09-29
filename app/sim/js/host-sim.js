// The virtual set's Host: the same Host port (app/ui/static/engine.js) as the live console, supplied by the
// sim's parts (virtual clock, recording Web Audio graph, the real API over HTTP) instead of the browser.
// Deck objects are the console's own Deck class (deck-controller.js) running on that recording graph.
"use strict";

function createSimHost(env) {
  const { clock, net } = env;
  const g = env.window;                       // the console's globals (decks, state, ...) live here in node too
  const doc = env.doc;
  const store = g.localStorage;
  const mod = {};
  return {
    clock: {
      now: () => clock.dateNow(), perfNow: () => clock.ms(),
      setTimeout: (fn, ms, ...a) => clock.setTimeout(fn, ms, ...a), clearTimeout: (id) => clock.clear(id),
      setInterval: (fn, ms, ...a) => clock.setInterval(fn, ms, ...a), clearInterval: (id) => clock.clear(id),
      raf: (fn) => clock.setTimeout(() => fn(clock.ms()), env.opts.frameMs || 250), cancelRaf: (id) => clock.clear(id),
      audioNow: () => (env.audio ? env.audio.currentTime : 0),
    },
    get audio() { return env.audio || g.audioCtx; },
    get decks() { return g.decks; },
    get state() { return g.state; },
    get session() { return g.djSession || (g.djSession = { relaxed: false }); },
    mod: new Proxy(mod, { get: (t, k) => (k in t ? t[k] : g[k]), set: (t, k, v) => { t[k] = v; g[k] = v; return true; } }),
    api: { fetch: (url, opts) => g.fetch(url, opts) },     // late lookup: the console wraps window.fetch (mascot "thinking")
    bus: {
      emit: (type, detail) => g.dispatchEvent(new g.CustomEvent(type, { detail })),
      on: (type, fn) => g.addEventListener(type, fn), off: (type, fn) => g.removeEventListener(type, fn),
    },
    log: { step: (kind, o) => { if (typeof g.aiStep === "function") g.aiStep(kind, o); } },
    ui: {
      el: (id) => doc.getElementById(id), query: (s) => doc.querySelector(s), queryAll: (s) => doc.querySelectorAll(s),
      create: (t) => doc.createElement(t),
      fire: (el, type, bubbles) => el.dispatchEvent(new g.Event(type, { bubbles: !!bubbles })),
      cssVar: () => "",
      status: (msg) => { if (typeof g.setStatus === "function") g.setStatus(msg); },
      flag: (id, dflt = true) => { const el = doc.getElementById(id); return el ? !!el.checked : dflt; },
    },
    storage: { get: (k) => { try { return store.getItem(k); } catch (e) { return null; } }, set: (k, v) => { try { store.setItem(k, v); } catch (e) { /* ignore */ } } },
    random: { uuid: () => g.crypto.randomUUID(), next: () => Math.random() },
    expose: (name, value) => { g[name] = value; },
    loadIntoDeck: (...a) => g.loadIntoDeck(...a),
  };
}

module.exports = { createSimHost };
