// Node check: NULL-BOT reacts to every move and works hand in hand with the Anyma SHOW.
// Runs the real mascot.js browser block in a vm with a fake DOM on a virtual clock.
//   - every move family emits a vis-moment (SUPER or ACCENT) with its hit time
//   - SUPER: gate (AI driving + VFX on), the takeover hit lands on the moment's audio time, vis-moment re-announced
//   - ACCENT: one pop per 2 bars
//   - DANCE: the dock NULL dances on the drop's beat grid; the dock is up in full SHOW
//   - z-order: NULL-BOT (.nul-super) above the SHOW stage
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const S = (f) => fs.readFileSync(path.join(__dirname, "../ui/static", f), "utf8");
const near = (a, b, eps = 1) => assert.ok(Math.abs(a - b) <= eps, `${a} != ${b}`);

// ---- fake DOM ------------------------------------------------------------------------------------------------------
class El {
  constructor(id) { this.id = id; this.cls = new Set(); this.props = {}; this.kids = {}; this.hidden = false; this.textContent = ""; this.attrs = {};
    this.parentNode = null; this.offsetWidth = 380;
    this.classList = { add: (c) => this.cls.add(c), remove: (c) => this.cls.delete(c), contains: (c) => this.cls.has(c),
      toggle: (c, on) => { if (on === undefined ? !this.cls.has(c) : on) this.cls.add(c); else this.cls.delete(c); } };
    this.style = { setProperty: (k, v) => { this.props[k] = v; } };
    this.rect = { left: 0, top: 0, right: 40, bottom: 40, width: 40, height: 40 }; }
  set innerHTML(_) { this.kids = {}; }
  querySelector(sel) { return this.kids[sel] || (this.kids[sel] = new El(sel)); }
  getBoundingClientRect() { return this.rect; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  appendChild(c) { c.parentNode = this; return c; }
  cloneNode() { return new El("clone"); }
  contains() { return false; }
  addEventListener() {}
  getContext() { return null; }
}

function boot(o = {}) {
  let clock = 1000;                                  // perf ms
  const timers = [];
  const addT = (fn, ms, every) => { timers.push({ fn, due: clock + Math.max(0, ms || 0), every, dead: false }); return timers.length; };
  const els = { "nul-mascot": new El("nul-mascot"), "nul-state": new El("nul-state"), "nul-super": new El("nul-super"), "vfx-toggle": new El("vfx-toggle") };
  els["vfx-toggle"].setAttribute("aria-pressed", "true");
  const stage = new El("anyma-stage");
  stage.rect = { left: 12, top: 500, right: 432, bottom: 788, width: 420, height: 288 };
  const body = new El("body");
  els["nul-super"].parentNode = body;
  const store = Object.assign({}, o.store || {});
  const et = new EventTarget();
  const ctx = {
    document: { getElementById: (id) => els[id] || null, querySelector: (s) => (s === ".anyma-stage" ? stage : null),
      addEventListener() {}, body, documentElement: new El("html"), fullscreenElement: null },
    localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
    performance: { now: () => clock },
    setTimeout: (fn, ms) => addT(fn, ms), clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].dead = true; },
    setInterval: (fn, ms) => addT(fn, ms, ms), clearInterval: (id) => { if (timers[id - 1]) timers[id - 1].dead = true; },
    requestAnimationFrame: () => 0, fetch: async () => ({}),
    addEventListener: et.addEventListener.bind(et), removeEventListener: et.removeEventListener.bind(et), dispatchEvent: et.dispatchEvent.bind(et),
    CustomEvent, innerWidth: 1000, innerHeight: 800, getComputedStyle: () => ({ getPropertyValue: () => "" }),
    autopilotState: { active: o.ai !== false },
    decks: { a: { playing: true, bpm: 128, _playbackRate: () => 1 }, b: { playing: false } },
    anymaShow: o.show || { mode: "off" },
  };
  // audio clock = perf clock - 900 s offset (any fixed offset: hitPerfMs maps between them)
  ctx.audioCtx = { get currentTime() { return clock / 1000 - 0.9; }, outputLatency: 0 };
  ctx.window = ctx; ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(S("mascot.js"), ctx, { filename: "mascot.js" });
  const moments = [];
  ctx.addEventListener("vis-moment", (e) => moments.push(e.detail));
  const advance = (ms) => {
    const until = clock + ms;
    for (;;) {
      const t = timers.filter((x) => !x.dead && x.due <= until).sort((a, b) => a.due - b.due)[0];
      if (!t) break;
      clock = Math.max(clock, t.due);
      if (t.every) t.due += t.every; else t.dead = true;
      t.fn();
    }
    clock = until;
  };
  const emit = (type, detail) => ctx.dispatchEvent(new CustomEvent(type, { detail }));
  const audioNow = () => ctx.audioCtx.currentTime;
  return { ctx, els, stage, store, moments, advance, emit, audioNow, sup: els["nul-super"], bot: els["nul-mascot"], now: () => clock };
}

// ---- SUPER: gate passes, hit on the audio time, one vis-moment at the same `at` -------------------------------------
{
  const B = boot();
  const at = B.audioNow() + 5;
  B.emit("ai-supermove", { at, name: "LAYER", deck: "b" });
  assert.strictEqual(B.moments.length, 1, "the legacy supermove is re-announced as one vis-moment");
  assert.deepStrictEqual({ at: B.moments[0].at, tier: B.moments[0].tier, name: B.moments[0].name }, { at, tier: "super", name: "LAYER" });
  let shownAt = null;
  for (let i = 0; i < 100 && shownAt == null; i++) { B.advance(50); if (B.sup.cls.has("nul-sm-on")) shownAt = B.now(); }
  assert.ok(shownAt != null, "the takeover runs with AI driving + VFX on");
  const hitPerf = shownAt + parseFloat(B.sup.props["--sm-hit"]);
  near((hitPerf / 1000 - 0.9), at, 0.06);           // the HIT lands on the moment's audio time
  assert.strictEqual(B.sup.kids[".nul-sm-cap"].textContent, "LAYER");
  B.advance(6000);
  assert.ok(!B.sup.cls.has("nul-sm-on"), "the takeover ends");
}
// gate: AI not driving / VFX off -> nothing
for (const o of [{ ai: false }, { store: { "nul.vfx": "off" } }]) {
  const B = boot(o);
  B.emit("vis-moment", { at: B.audioNow() + 3, name: "MERGE", tier: "super" });
  B.advance(8000);
  assert.ok(!B.sup.cls.has("nul-sm-on") && !B.sup.kids[".nul-sm-cap"].textContent, JSON.stringify(o));
}
// a named moment on the same hit renames the legacy cue's takeover (MERGE -> HOLD->DROP); de-dup keeps one
{
  const B = boot();
  const at = B.audioNow() + 4;
  B.emit("ai-cue", { at, kind: "drop", why: "B takes every stem on the line after the merge", deck: "b", bar: 1.875 });
  B.emit("vis-moment", { at: at + 0.01, name: "HOLD->DROP", tier: "super", deck: "b" });
  B.emit("vis-moment", { at: at + 3, name: "OTHER", tier: "super" });     // inside the 8 s window: de-duped
  let shows = 0, was = false;
  for (let i = 0; i < 200; i++) { B.advance(50); const on = B.sup.cls.has("nul-sm-on"); if (on && !was) shows++; was = on; }
  assert.strictEqual(shows, 1);
  assert.strictEqual(B.sup.kids[".nul-sm-cap"].textContent, "HOLD->DROP");
}

// ---- ACCENT: in-place pop, one per 2 bars ----------------------------------------------------------------------------
{
  const B = boot();
  let pops = 0, was = false;
  const t0 = B.audioNow();
  for (const dt of [1, 2, 3.5, 6]) B.emit("vis-moment", { at: t0 + dt, name: "MID BLEND", tier: "accent", deck: "a" });
  for (let i = 0; i < 400; i++) { B.advance(25); const on = B.bot.cls.has("nul-react"); if (on && !was) pops++; was = on; }
  assert.strictEqual(pops, 2, "1 s and 6 s pop; 2 s and 3.5 s are inside 2 bars of 1.875 s");
  assert.ok(!B.sup.cls.has("nul-sm-on"), "an accent never flies in");
}

// ---- SHOW: dock in full, dance beside a window stage -------------------------------------------------------------------
{
  const show = { mode: "full" };
  const B = boot({ show });
  B.advance(200);
  assert.ok(B.sup.cls.has("nul-sm-dock"), "full SHOW: NULL stays visible, docked in front");
  show.mode = "off"; B.advance(200);
  assert.ok(!B.sup.cls.has("nul-sm-dock"));
  show.mode = "window"; B.advance(200);
  assert.ok(!B.sup.cls.has("nul-sm-dock"), "window SHOW, no dance: NULL stays in the top bar");
  const at = B.audioNow() + 1;
  B.emit("vis-moment", { at, name: "ANYMA DROP", tier: "dance", bar: 1.875, until: at + 30 });
  B.advance(1200);
  assert.ok(B.sup.cls.has("nul-sm-dance") && B.sup.cls.has("nul-sm-dock"), "the dock NULL dances beside the stage");
  // phase: a beat loop started now is at progress 0 on the drop (same beat grid as the figure)
  const beat = parseFloat(B.sup.props["--sm-beat"]), ph = parseFloat(B.sup.props["--sm-phase"]);
  near(beat, 468.75 * 1, 1);
  B.advance(31000);
  assert.ok(!B.sup.cls.has("nul-sm-dance"), "the dance ends at `until`");
  assert.ok(Number.isFinite(ph) && ph <= 0);
}
// Anyma drops announced AHEAD from the SHOW's own detector, once per drop
{
  const show = { mode: "full", core: { prepTrack: (an) => ({ an }), anymaDrops: () => [{ at: 64 }], anymaHint: () => "" } };
  const B = boot({ show });
  let pos = 58;
  Object.assign(B.ctx.decks.a, { analysis: {}, crossfaderGain: { gain: { value: 1 } }, _currentPosition: () => pos });
  B.advance(600); assert.strictEqual(B.moments.filter((m) => m.tier === "dance").length, 0, "6 s out: not yet");
  pos = 61; B.advance(600);
  const d = B.moments.filter((m) => m.tier === "dance");
  assert.strictEqual(d.length, 1, "3 s out: announced");
  near(d[0].at, B.audioNow() + 3, 0.7);
  B.advance(1500);
  assert.strictEqual(B.moments.filter((m) => m.tier === "dance").length, 1, "once per drop");
}

// ---- z-order: NULL-BOT above the SHOW stage and the VFX canvas --------------------------------------------------------
{
  const z = (css, sel) => Number((new RegExp(`\\${sel}\\s*\\{[^}]*z-index:\\s*(\\d+)`).exec(css) || [])[1]);
  const nb = z(S("null-bot.css"), ".nul-super"), st = z(S("anyma-show.css"), ".anyma-stage"), vfx = z(S("style.css"), ".vfx");
  assert.ok(nb > st && nb > vfx, `nul-super ${nb} > anyma-stage ${st}, vfx ${vfx}`);
  assert.ok(/\.nul-super\.nul-sm-dock \{ visibility: visible; \}/.test(S("null-bot.css")));
  assert.ok(/fullscreenchange/.test(S("mascot.js")), "browser full screen: NULL moves into the full-screen element");
}

// ---- every move family emits a vis-moment (SUPER or ACCENT) with its hit time ------------------------------------------
{
  const fams = {
    "stem-moves.js": [/"vis-moment", \{ at: t0 \+ plan\.bEntry, name: "STEM BRIDGE", tier: "super"/, /name: vk === "filter_loop" \? "FILTER LOOP" : "DRUMS HOST", tier: "super"/,
      /"vis-moment", \{ at: at\(len \* 0\.75\)[^}]*tier: "accent"/],
    "autopilot.js": [/name: "HOLD->DROP", tier: "super"/, /name: "MASHUP", tier: "super"/, /name: String\(ranMove\), tier: "accent"/, /"ai-supermove", \{ at: t0, name: "LAYER"/],
    "artist-moves.js": [/"vis-moment", kind === "slip_loop" \? \{ at: extra\.t1[^}]*tier: "super"[^}]*\} : \{ at: extra\.t0[^}]*tier: "accent"/],
    "fx-moves.js": [/VIS_SUPER = \{ kick_roll: "t1", vocal_throw: "t0", supermove_replay: "t1" \}/],
    "learned-moves.js": [/name: extra\.move === "vocal_swap" \? "VOCAL SWAP"[^}]*tier: "super"/, /tier: "accent"/],
    "dj-mind.js": [/VIS_MOVES = \{ fakeout: \["FAKEOUT", "super"\], peak_roll: \["PEAK ROLL", "super"\]/, /host\.bus\.emit\("vis-moment"/],
    "macro-mode.js": [/name: `COMBO x\$\{streak\.n\}`, tier: "accent"/],
  };
  for (const [f, res] of Object.entries(fams)) for (const re of res) assert.ok(re.test(S(f)), `${f}: ${re}`);
  // the legacy cues still map (strip & rebuild, merge, mashup, hook drop, riff over rap)
  const m = require("../ui/static/mascot.js");
  for (const [kind, why, name] of [["drop", "everything slams back after the strip & rebuild", "STRIP & REBUILD"], ["drop", "B takes every stem on the line after the merge", "MERGE"],
    ["drop", "B's beat takes over after the mashup", "MASHUP"], ["drop", 'the beat slams back after "x"', "HOOK DROP"], ["line", "B's rap arrives: open hat on the line", "RIFF OVER RAP"]]) {
    const vm1 = m.legacyMoment({ type: "ai-cue", detail: { at: 9, kind, why } });
    assert.ok(vm1 && vm1.name === name && vm1.tier === "super" && vm1.source === "cue", name);
  }
  assert.strictEqual(m.legacyMoment({ type: "vis-moment", detail: { at: 9, name: "X" } }), null, "never re-announces itself");
  assert.ok(m.renameSameHit([{ hitS: 10, sm: { name: "MERGE" } }], 10.1, "HOLD->DROP"));
  assert.ok(!m.renameSameHit([{ hitS: 10, sm: { name: "MERGE" } }], 11, "HOLD->DROP"));
}

console.log("nullbot moves ok");
