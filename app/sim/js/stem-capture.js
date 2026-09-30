// Stem capture: plays ONE transition of a pair in the headless console (the console's own
// scripts, unmodified unless a preview-only override below is asked for, on the virtual clock
// with the recording Web Audio API of webaudio.js)
// and writes out everything the audio graph was told to do: every node, every AudioParam's
// automation timeline, every connect / disconnect with its time, every buffer source's song
// position over time. app/music_brain/render/graph_render.py renders that on the real audio.
//
//   node app/sim/js/stem-capture.js cfg.json
//   cfg = { port, a: {id, name}, b: {id, name}, recipe, aTime, bTime, pre, post, lead, out,
//           allowStemPath?, xf? }
//
// The page talks to the real API (app/sim/stem_capture.py runs it on a throwaway cache), but
// only to the per-track read endpoints of A and B: everything else (suggest, search, lyrics,
// hook drops, fame, session logs) answers 404 here and is listed in `unserved`.
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const crypto = require("crypto");
const { createEnv } = require("./env");

const STATIC = process.env.SIM_STATIC_DIR || path.resolve(__dirname, "..", "..", "ui", "static");

// PREVIEW-ONLY overrides, applied to a throwaway copy of the console's scripts that only this
// capture loads (app/ui/static on disk and the live console are never touched). Each one is an
// exact text swap that must match once, so a console change makes it fail loudly, not silently.
//   allowStemPath: the stem blend's loudness floor (stem-moves.js LEVEL_FLOOR_DB) is not applied
//   xf: PLAY STEP's crossfade budget (autopilot.js performNow books 16 s; < 16 halves every bar
//       count in executeTransition, as the running set's quick / vocal-short windows do)
const OVERRIDES = {
  allowStemPath: (on) => on && [["stem-moves.js", "const LEVEL_FLOOR_DB = 8;", "const LEVEL_FLOOR_DB = 1e9;   // stem-capture allowStemPath"]],
  xf: (v) => v != null && [["autopilot.js", "executeTransition(fb.recipe, o.out, o.inn, 16, o.t0)", `executeTransition(fb.recipe, o.out, o.inn, ${+v}, o.t0)`]],
};

function staticWith(cfg) {
  const swaps = [];
  for (const [k, f] of Object.entries(OVERRIDES)) { const s = f(cfg[k]); if (s) swaps.push(...s.map((x) => [k, ...x])); }
  if (!swaps.length) return { dir: STATIC, applied: [] };
  if (cfg.xf != null && !(Number.isFinite(+cfg.xf) && +cfg.xf > 0)) throw new Error(`xf must be a number > 0 (got ${cfg.xf})`);
  const dir = fs.mkdtempSync(path.join(require("os").tmpdir(), "stem-capture-static-"));
  fs.cpSync(STATIC, dir, { recursive: true });
  const applied = [];
  for (const [k, file, from, to] of swaps) {
    const p = path.join(dir, file), src = fs.readFileSync(p, "utf8");
    const n = src.split(from).length - 1;
    if (n !== 1) throw new Error(`override ${k}: expected "${from}" once in ${file}, found ${n}`);
    fs.writeFileSync(p, src.replace(from, to));
    applied.push({ override: k, value: cfg[k], file, from, to });
  }
  return { dir, applied };
}
const PAINT = ["stem-wave.js", "visuals.js", "anyma-show.js", "anyma-ui.js", "marquee.js"];
const POS_HZ = 1000;                     // song position samples per second of each buffer source
const GEN_MAX_S = 10;                    // generated buffers up to this long are written out sample by sample
const GEN = {};                          // buffer id -> {sr, channels}
const STEPS = [];                        // the console's POST /api/session/steps|event bodies

// GET paths a capture may send to the API -> {id, key}; key names the decoded file (id:stem / id:mix)
// The console-wide reads the live server answers from its cache alone (copied into the capture's
// throwaway cache by stem_capture.py): liked transitions (a stored vocal throw), macros, learned
// moves, the recipe list. Fame only for songs with a cached answer (a miss would ask YouTube).
// Still not served: hook-drops (a miss asks LRCLIB / the local model), POST /api/match (the
// suggestion panel's matcher, never read by a booked move), status polls and set history.
const SHARED = [/^\/api\/liked$/, /^\/api\/macros$/, /^\/api\/macros\/[^/]+$/, /^\/api\/learned\/moves$/, /^\/api\/recipes$/];
function allowed(p, ids, fameIds) {
  const bare = p.split("?")[0];
  if (SHARED.some((r) => r.test(bare))) return { key: null };
  const fm = bare.match(/^\/api\/tracks\/([^/]+)\/fame$/);
  if (fm) return fameIds.has(fm[1]) ? { key: null } : null;
  let m = bare.match(/^\/api\/tracks\/([^/]+)\/(analysis|vocals|vocal_entry|stems)$/);
  if (m && ids.has(m[1])) return { key: null };
  m = bare.match(/^\/api\/tracks\/([^/]+)\/stems\/([a-z]+)$/);
  if (m && ids.has(m[1])) return { key: `${m[1]}:${m[2]}` };
  m = bare.match(/^\/api\/audio\/tracks\/([^/]+)$/);
  if (m && ids.has(m[1])) return { key: `${m[1]}:mix` };
  return null;
}

const sha1 = (buf) => crypto.createHash("sha1").update(buf).digest("hex");

function serParam(p) {
  return { base: p._base, ev: p._ev.map((e) => (e.type === "curve" ? { type: e.type, time: e.time, dur: e.dur, curve: Array.from(e.curve) } : Object.assign({}, e))) };
}

// Song position of a buffer source, sampled on [w0, w1] at POS_HZ (null: not sounding)
function positions(n, w0, w1) {
  const t0 = Math.max(w0, n._sStart), t1 = Math.min(w1, n._sStop === null ? w1 : n._sStop);
  if (!(t1 > t0)) return null;
  const k0 = Math.ceil(t0 * POS_HZ), k1 = Math.floor(t1 * POS_HZ);
  const pos = [];
  for (let k = k0; k <= k1; k++) {
    const t = k / POS_HZ;
    const p = n.aliveAt(t) ? n.positionAt(t) : null;
    pos.push(p === null ? null : +p.toFixed(6));
  }
  return { k0, pos };
}

function serNode(n, w0, w1) {
  const o = { id: n._id, kind: n._kind, type: n.type || null, params: {} };
  for (const [k, v] of Object.entries(n)) if (v && v.constructor && v.constructor.name === "FakeAudioParam") o.params[k] = serParam(v);
  if (n._kind === "shaper" && n.curve) o.curve = Array.from(n.curve);
  if (n._kind === "osc") Object.assign(o, { start: n._start, stop: n._stop });
  if (n._kind === "source") {
    const b = n.buffer;
    const file = (b && b.tag && b.tag.file) || null;
    // a buffer the console generated itself (performance.js noise, reverb impulses): its samples
    if (b && !file && n._sStart !== null && n._sStart < w1 && b.length <= GEN_MAX_S * b.sampleRate && !(b._id in GEN))
      GEN[b._id] = { sr: b.sampleRate, channels: Array.from({ length: b.numberOfChannels }, (_, c) => Array.from(b.getChannelData(c), (v) => +v.toFixed(6))) };
    Object.assign(o, { file, buf: b ? b._id : null, buf_dur: b ? b.duration : null, start: n._sStart, stop: n._sStop,
                       offset: n._sOff, loop: n.loop, loop_start: n.loopStart, loop_end: n.loopEnd,
                       positions: n._sStart === null ? null : positions(n, w0, w1) });
  }
  return o;
}

// connect / disconnect log -> edges with the audio-clock interval each was live
function edges(cmds) {
  const open = new Map(), out = [];
  const close = (key, t) => { const e = open.get(key); if (e) { e.off = t; out.push(e); open.delete(key); } };
  for (const c of cmds) {
    if (c.kind === "connect") {
      if (c.to == null) { out.push({ from: c.from, to: null, on: c.t, off: null, param: true }); continue; }
      const key = `${c.from}>${c.to}`;
      if (!open.has(key)) open.set(key, { from: c.from, to: c.to, on: c.t, off: null });
    } else if (c.kind === "disconnect") {
      if (c.to != null) close(`${c.from}>${c.to}`, c.t);
      else for (const key of [...open.keys()]) if (key.startsWith(`${c.from}>`)) close(key, c.t);
    }
  }
  return out.concat([...open.values()]);
}

function labels(g) {
  const out = {};
  const put = (node, name) => { if (node && node._id && node._kind && !(node._id in out)) out[node._id] = name; };
  for (const id of ["a", "b"]) {
    const d = g.decks && g.decks[id];
    if (!d) continue;
    for (const [k, v] of Object.entries(d)) {
      if (!v || typeof v !== "object") continue;
      if (v._id && v._kind) put(v, `${id}.${k}`);
      else if (!Array.isArray(v) && Object.getPrototypeOf(v) === Object.prototype)
        for (const [k2, v2] of Object.entries(v)) if (v2 && typeof v2 === "object") put(v2, `${id}.${k}.${k2}`);
    }
  }
  for (const name of ["masterGain", "masterLimiter", "masterOut", "vocalBus"]) {
    try { put(vm.runInThisContext(`typeof ${name} !== "undefined" ? ${name} : null`), name); } catch (e) { /* not declared */ }
  }
  return out;
}

let ENV = null;
async function main() {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const ids = new Set([cfg.a.id, cfg.b.id]);
  const fameIds = new Set(cfg.fameIds || []);
  const st = staticWith(cfg);
  if (st.dir !== STATIC) process.on("exit", () => { try { fs.rmSync(st.dir, { recursive: true, force: true }); } catch (e) { /* tmp */ } });
  const env = await createEnv({ staticDir: st.dir, port: cfg.port, seed: cfg.seed || 1, skip: PAINT });
  ENV = env;
  const g = env.window;

  // the capture's only window on the API: A's and B's read endpoints, bodies fingerprinted
  const unserved = new Set(), bodyKey = new Map();
  const real = env.net._request.bind(env.net);
  env.net._request = (method, p, headers, body) => {
    const ok = method === "GET" ? allowed(p, ids, fameIds) : null;
    if (method === "POST" && /^\/api\/session\/(steps|event)/.test(p)) {
      // the console's own step / event log: kept in the capture (merge_gate, macro, ...), not persisted
      try { const b = JSON.parse(String(body)); for (const s of [].concat(b.steps || b)) STEPS.push({ t: +ENV.clock.now.toFixed(3), ...s }); } catch (e) { /* not JSON */ }
      return Promise.resolve({ status: 200, statusText: "OK", headers: { "content-type": "application/json" }, body: Buffer.from("{}"), real: 0 });
    }
    if (!ok) {
      unserved.add(`${method} ${p.split("?")[0]}`);
      return Promise.resolve({ status: 404, statusText: "Not Found", headers: { "content-type": "application/json" },
                               body: Buffer.from('{"detail":"not served in a stem capture"}'), real: 0 });
    }
    return real(method, p, headers, body).then((r) => { if (ok.key && r.status === 200) bodyKey.set(sha1(r.body), ok.key); return r; });
  };

  env.loadScripts();
  await env.clock.run(0.5);
  const ctx = env.audio;
  if (!ctx) throw new Error("the console created no AudioContext");
  const dec = ctx.decode;
  ctx.decode = (ab) => {
    const b = dec(ab);
    const key = bodyKey.get(sha1(Buffer.from(ab)));
    if (key) b.tag = Object.assign({}, b.tag || {}, { file: key });
    return b;
  };

  // run the virtual clock until p settles (or maxS virtual seconds pass)
  const settle = async (p, maxS) => {
    let done = false, val, err;
    p.then((v) => { done = true; val = v; }, (e) => { done = true; err = e; });
    await env.clock.run(env.clock.now + maxS, () => done);
    if (err) throw err;
    if (!done) throw new Error(`timed out after ${maxS} virtual s`);
    return val;
  };
  const waitFor = async (fn, maxS, what) => {
    await env.clock.run(env.clock.now + maxS, () => !!fn());
    if (!fn()) throw new Error(`${what}: not ready after ${maxS} virtual s`);
  };

  for (const [deck, t] of [["a", cfg.a], ["b", cfg.b]]) {
    const blob = await settle(g.fetch(`/api/audio/tracks/${t.id}`).then((r) => {
      if (!r.ok) throw new Error(`track ${t.id}: HTTP ${r.status}`);
      return r.blob();
    }), 30);
    await settle(Promise.resolve(g.loadIntoDeck(deck, t.id, t.name, blob)), 60);
  }
  const decks = g.decks;
  await waitFor(() => decks.a.buffer && decks.b.buffer && decks.a.analysis && decks.b.analysis, 60, "analysis");
  // stemsReady means "stems sounding now" (deck-controller.js), so a stopped deck only holds decoded .stems
  await waitFor(() => decks.a.stems && decks.b.stems, 120, "stems (A and B need cached 4-stem sets)");
  // what the running set's staging does once B's stems are on (autopilot.js, "where its vocal
  // phrase starts (for a mashup transition)"): PLAY STEP alone never asks, so a mashup would
  // always read "does not fit". Same request, same field.
  const ve = await settle(g.fetch(`/api/tracks/${cfg.b.id}/vocal_entry`).then((r) => (r.ok ? r.json() : null)).catch(() => null), 60);
  if (ve) decks.b._vocalEntry = ve;

  // A plays from `lead` s before the window, the move is booked like PLAY STEP pressed then
  const lead = cfg.full ? 0 : cfg.lead != null ? cfg.lead : 8;
  decks.a.play(Math.max(0, cfg.aTime - cfg.pre - lead));
  await env.clock.run(env.clock.now + 2);
  if (!decks.a.stemsReady) throw new Error("deck A plays but its stems are not live");
  const aPos = decks.a._currentPosition(), rate = decks.a._playbackRate ? decks.a._playbackRate() : 1;
  const t0 = ctx.currentTime + Math.max(0, cfg.aTime - aPos) / rate;
  const ap = g.Engine && g.Engine.mods && g.Engine.mods.autopilot;
  if (!ap || !ap.performNow) throw new Error("autopilot.performNow not mounted");
  const forced = { source: "stem-preview", macro: null, n: 1, a: cfg.a.id, b: cfg.b.id, a_name: cfg.a.name, b_name: cfg.b.name,
                   recipe: cfg.recipe, a_time: cfg.aTime, b_time: cfg.bTime, merge: null };
  const r = ap.performNow({ out: "a", inn: "b", aId: cfg.a.id, bId: cfg.b.id, aT: cfg.aTime, t0, forced });
  if (!r || !r.ok) throw new Error(`the console refused the move: ${r && r.why}`);

  // the transition runs to its end: performNow stops A 300 ms after the move's total length
  // (autopilot.js, set not running), so A's deck going quiet marks it; then `post` s of B alone
  await env.clock.run(t0 + 180, () => ctx.currentTime > t0 && !decks.a.playing);
  const end = !decks.a.playing;
  const tEnd = end ? Math.max(t0, ctx.currentTime - 0.3) : null;
  // full: A from its 0:00 (A has played since then), B to its own end
  let pre = cfg.pre, post = cfg.post;
  if (cfg.full) {
    pre = cfg.aTime / rate;
    const bRate = decks.b._playbackRate ? decks.b._playbackRate() : 1;
    const bLeft = decks.b.playing && decks.b.buffer ? (decks.b.buffer.duration - decks.b._currentPosition()) / bRate : 0;
    post = Math.max(0, ctx.currentTime + bLeft - (tEnd || t0 + 60));
  }
  const w0 = t0 - pre, w1 = (tEnd || t0 + 60) + post;
  await env.clock.run(w1 + 0.5);

  const out = {
    sample_rate_ctx: ctx.sampleRate, window: [+w0.toFixed(6), +w1.toFixed(6)], t0: +t0.toFixed(6),
    t_end: tEnd === null ? null : +tEnd.toFixed(6), end_marked: end, pos_hz: POS_HZ,
    a: Object.assign({}, cfg.a, { a_time: cfg.aTime }), b: Object.assign({}, cfg.b, { b_time: cfg.bTime }),
    sim_overrides: st.applied,
    vocal_entry_b: ve || null,
    steps: STEPS.slice(-400),
    recipe_asked: cfg.recipe, ran: r.ran, refused: r.refused || null, line: r.line || null,
    destination: ctx.destination._id,
    nodes: ctx._nodes.map((n) => serNode(n, w0, w1)),
    edges: edges(ctx._cmds),
    buffers: GEN,
    labels: labels(g),
    unserved: [...unserved].sort(),
    console: env.logs.console.filter((c) => c.level === "warn" || c.level === "error" || /transition|stem|merge|macro|mashup|variant|gate|refused|echo|throw/i.test(c.text)).slice(-200),
    errors: env.logs.errors.slice(0, 50),
    audio_errors: ctx._errors.slice(0, 20),
  };
  fs.writeFileSync(cfg.out, JSON.stringify(out));
  process.exit(0);
}

main().catch((e) => {
  process.stderr.write(`stem capture failed: ${e && e.stack}\n`);
  if (ENV) {   // what the console asked and said, for the failure report
    process.stderr.write(`net: ${JSON.stringify(ENV.net.log.filter((l) => /\/tracks\//.test(l.path)).slice(-12))}\n`);
    process.stderr.write(`console: ${JSON.stringify(ENV.logs.console.slice(-15).map((c) => c.text))}\n`);
    process.stderr.write(`errors: ${JSON.stringify(ENV.logs.errors.slice(0, 5))}\n`);
  }
  process.exit(1);
});
