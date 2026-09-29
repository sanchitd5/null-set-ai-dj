// Host port contract (Rev 4): both hosts satisfy the one interface, the Engine registry composes modules with
// plain dependency injection, and the ported engine modules touch no browser global of their own.
//   node app/tests/host_contract_check.js
"use strict";
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const STATIC = path.join(__dirname, "..", "ui", "static");
const SIM = path.join(__dirname, "..", "sim", "js");

// fresh Engine per test (engine.js keeps its registry on the object it exports)
const loadEngine = () => { delete require.cache[require.resolve(path.join(STATIC, "engine.js"))]; return require(path.join(STATIC, "engine.js")); };

// ---- a window-like object for the browser host ---------------------------------------------------------------
function fakeWindow() {
  const listeners = {};
  const el = () => ({ dispatchEvent() { return true; }, checked: true });
  const win = {
    Event: class { constructor(t, o) { this.type = t; Object.assign(this, o); } },
    CustomEvent: class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } },
    Date, performance: { now: () => 1 }, crypto: { randomUUID: () => "u" }, localStorage: { getItem: () => null, setItem() {} },
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    fetch: async () => ({}), getComputedStyle: () => ({ getPropertyValue: () => " #0f6 " }),
    addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); }, removeEventListener() {},
    dispatchEvent: (ev) => { (listeners[ev.type] || []).forEach((f) => f(ev)); return true; },
    document: { getElementById: () => el(), querySelector: () => el(), querySelectorAll: () => [], createElement: () => el() },
    decks: { a: {}, b: {} }, state: {},
  };
  return win;
}

// ---- 1. the port: window host + sim host both satisfy it ---------------------------------------------------------------------
{
  const Engine = loadEngine();
  const winHost = Engine.createWindowHost(fakeWindow(), () => ({ currentTime: 0 }), () => async () => {}, () => () => {});
  Engine.assertHost(winHost, "window host");
  assert.strictEqual(winHost.ui.cssVar("deck-a", "--accent"), "#0f6");

  const { createSimHost } = require(path.join(SIM, "host-sim.js"));
  const w = fakeWindow();
  const clock = { dateNow: () => 0, ms: () => 0, setTimeout, setInterval, clear() {} };
  w.dispatchEvent = w.dispatchEvent.bind(w);
  const simHost = createSimHost({ clock, net: { fetch: async () => ({}) }, window: w, doc: w.document, opts: {}, audio: { currentTime: 0 } });
  Engine.assertHost(simHost, "sim host");
  assert.deepStrictEqual(Object.keys(simHost).sort(), Object.keys(winHost).sort(), "both hosts expose the same members");
  for (const g of Object.keys(Engine.HOST_PORT)) assert.deepStrictEqual(Object.keys(simHost[g]).sort(), Object.keys(winHost[g]).sort(), `same ${g} members`);

  // a host missing anything the port names is refused, and the error says what
  const broken = Object.assign({}, winHost, { clock: { now() {} } });
  assert.throws(() => Engine.assertHost(broken, "broken"), /missing: .*clock\.setTimeout/);
  assert.throws(() => Engine.assertHost({}, "empty"), /Host port/);
}

// ---- 2. the registry: factories get exactly {host, ai}; nothing runs before a host is chosen -----------------------
{
  const Engine = loadEngine();
  let made = 0;
  Engine.mount("early", () => { made++; return {}; });
  assert.strictEqual(made, 0, "no host yet: the module stays a pure export");
  const host = Engine.createWindowHost(fakeWindow(), () => ({}), () => async () => {}, () => () => {});
  const ai = { suggest() {}, plan() {} };
  let got;
  Engine.use(host, ai);
  Engine.mount("late", (deps) => { got = deps; return { hi: 1 }; });
  assert.deepStrictEqual(Object.keys(got).sort(), ["ai", "host"]);
  assert.strictEqual(got.host, host);
  assert.strictEqual(got.ai, ai);
  assert.strictEqual(host.mod.late.hi, 1, "mounted API reachable as host.mod.<name>");
  Engine.create("early");
  assert.strictEqual(made, 1);

  // the AI backend is swappable behind the port: the default posts to the server, a fake answers in-process
  const calls = [];
  const http = Engine.createHttpAi({ fetch: async (url, o) => { calls.push(url); return { ok: true, json: async () => ({ picks: [] }) }; } });
  return http.suggest({ x: 1 }).then(async () => {
    await http.plan({ y: 1 }, {});
    assert.deepStrictEqual(calls, ["/api/autopilot/suggest", "/api/autopilot/plan"]);
    step3();
  });
}

// ---- 3. purity: the ported modules' runtime reaches the world only through `host` ---------------------------------------
function step3() {
  // engine runtime = from the factory to the end of the file; the pure cores above it are checked by their own node checks
  const modules = { "autopilot.js": "function createAutopilotEngine", "dj-mind.js": "function create(", "stem-moves.js": "function create(",
    "riff-over-rap.js": "function create(", "ai-actions.js": "function create(" };
  const banned = [/\bwindow\./, /\bdocument\./, /\bDate\.now\b/, /\bnew Date\b/, /\bperformance\./, /\bnew (Custom)?Event\b/,
    /\brequestAnimationFrame\b/, /\blocalStorage\b/, /\bMath\.random\b/, /\bcrypto\./];
  const bad = [];
  for (const [file, marker] of Object.entries(modules)) {
    const src = fs.readFileSync(path.join(STATIC, file), "utf8");
    const i = src.indexOf(marker);
    assert.ok(i > 0, `${file}: factory not found`);
    src.slice(i).split("\n").forEach((line, n) => {
      const code = line.replace(/\/\/.*$/, "").replace(/"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`/g, '""');
      for (const re of banned) if (re.test(code)) bad.push(`${file}:${src.slice(0, i).split("\n").length + n} ${re} :: ${line.trim().slice(0, 100)}`);
    });
  }
  assert.deepStrictEqual(bad, [], "engine runtime touches browser globals:\n" + bad.join("\n"));
  console.log("host_contract_check ok");
}
