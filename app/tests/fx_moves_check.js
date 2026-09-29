// Node check for the artist FX moves (app/ui/static/fx-moves.js, batch A S2-S6 + the supermove replay):
// the pure planners on synthetic envelopes / stems, their named gates, and the runtime through a fake Host
// on a virtual clock (no rewind without a preceding supermove, on-demand runNow gates).
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const STATIC = path.join(__dirname, "..", "ui", "static");
const Engine = require(path.join(STATIC, "engine.js"));
require(path.join(STATIC, "learned-moves.js"));
const fx = require(path.join(STATIC, "fx-moves.js"));

const BPM = 120, BAR = 240 / BPM, BEAT = BAR / 4;
const near = (a, b, e = 1e-6) => Math.abs(a - b) <= e;

// ---- S3 band edges ------------------------------------------------------------------------------------------
{
  const sr = 8000, ch = new Float32Array(sr);
  for (let i = 0; i < sr; i++) ch[i] = Math.sin((2 * Math.PI * 1000 * i) / sr);
  assert(near(fx.rmsFreqHz(ch, sr, 0, sr), 1000, 30), "RMS frequency of a 1 kHz sine");
  assert.strictEqual(fx.rmsFreqHz(new Float32Array(100), sr, 0, 100), null, "silence: unmeasured");
  assert.deepStrictEqual([fx.bandEdges(1800).low, fx.bandEdges(1800).high], [300, 3600]);
  assert.deepStrictEqual([fx.bandEdges(100).low, fx.bandEdges(100).high], [250, 3000], "clamped 250 Hz / 3 kHz");
  assert.deepStrictEqual([fx.bandEdges(9000).low, fx.bandEdges(9000).high], [500, 6000], "clamped 500 Hz / 6 kHz");
  assert(fx.bandEdges(null).fallbacks.length === 1, "unmeasured brightness is a listed fallback");
}

// ---- S2 mid blend ---------------------------------------------------------------------------------------------
{
  const aOther = [0.2, 0.2, 0.2, 0.2, 0.1, 0.02, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01];
  const c = { a: { other: aOther }, b: { other: new Array(16).fill(0.1), vocals: new Array(16).fill(0) }, total: 16, swapBar: 8, camelot: 0.9 };
  const r = fx.planMidBlend(c);
  assert(r.plan && r.plan.dipDb <= -6 && r.plan.dipDb >= -10, "mid dip 6-10 dB");
  assert.strictEqual(r.plan.unmaskBar, 5, "B's mids come back where A's melody falls under 15 % of its peak");
  assert.strictEqual(fx.planMidBlend({ ...c, camelot: 0.6 }).refusal.gate, "key", "melodies need Camelot >= 0.8");
  assert.strictEqual(fx.planMidBlend({ ...c, a: null }).refusal.gate, "stems");
  assert.strictEqual(fx.planMidBlend({ ...c, a: { other: new Array(16).fill(0) } }).refusal.gate, "no_melody", "no-op on silence");
  assert.strictEqual(fx.planMidBlend({ ...c, mashupActive: true }).refusal.gate, "mashup_layer");
  const flat = fx.planMidBlend({ ...c, a: { other: new Array(16).fill(0.2) } });
  assert(flat.plan.unmaskBar === 8 && flat.plan.fallbacks.length === 1, "unresolved melody: the swap line, listed as a fallback");
}

// ---- S4 kick roll ----------------------------------------------------------------------------------------------
{
  const sr = 4000, ch = new Float32Array(sr * 40);
  for (let k = 0; k < 60; k++) {                          // a 60 Hz kick on every beat, the one at beat 20 louder
    const t0 = k * BEAT, amp = k === 20 ? 0.9 : 0.4;
    for (let i = 0; i < 0.12 * sr; i++) ch[Math.floor(t0 * sr) + i] += amp * Math.exp(-i / (0.03 * sr)) * Math.sin((2 * Math.PI * 60 * i) / sr);
  }
  const kick = fx.kickSlice(ch, sr, 16 * BEAT, 24 * BEAT);
  assert(kick && near(kick.from, 20 * BEAT, 0.02), "the loudest kick of the window");
  assert(kick.dur >= 0.06 && kick.dur <= 0.25, "slice length clamped");
  assert.strictEqual(fx.kickSlice(new Float32Array(sr * 40), sr, 0, 10), null, "silent drum stem: no kick");
  const drop = 32 * BEAT;
  const r = fx.planKickRoll({ dropT: drop, beatS: BEAT, kick });
  assert(near(r.plan.start + 8 * BEAT, drop), "the roll ends on the drop downbeat");
  const offs = r.plan.slices.map((s) => s.off / (BEAT / 4));
  assert(offs.every((x) => near(x, Math.round(x), 1e-9)), "every hit on the 1/16 grid");
  assert.strictEqual(r.plan.hits, 12, "4 x 1/4 + 4 x 1/8 + 4 x 1/16");
  const last = r.plan.slices[r.plan.slices.length - 1];
  assert(last.gain === 0 && near(last.off + last.dur, 8 * BEAT), "a beat of silence before the drop, span capped at 8 beats");
  const steps = r.plan.slices.slice(1).map((s, i) => s.off - r.plan.slices[i].off);
  assert(steps.every((s, i) => i === 0 || s <= steps[i - 1] + 1e-9), "the grid only shrinks (accelerates)");
  assert.strictEqual(fx.planKickRoll({ dropT: drop, beatS: BEAT, kick: null }).refusal.gate, "stems", "no drum stem: the old roll");
  assert.strictEqual(fx.planKickRoll({ dropT: drop, beatS: BEAT, kick, inTransition: true }).refusal.gate, "transition");
}

// ---- S5 vocal throw ----------------------------------------------------------------------------------------------
{
  const lm = require(path.join(STATIC, "learned-moves.js"));
  const hop = BEAT / 4, t0 = 10, n = Math.round((4 * BAR) / hop), v = new Array(n).fill(0.0005);
  for (let i = 0; i < n; i++) { const t = t0 + i * hop; if (t >= 11 && t < 14.5) v[i] = t >= 14 ? 0.3 : 0.25; if (t >= 13.8 && t < 14) v[i] = 0.05; }
  const env = { t0, hop, v };
  const c = { env, lines: lm.vocalLines(env), t0, swapT: t0 + 4 * BAR, bVocalT: 20, beatS: BEAT };
  const r = fx.planVocalThrow(c);
  assert(r.plan, "a line ends before the swap: thrown");
  assert(near(r.plan.wordS, 14, hop + 1e-6), "the last word starts after the dip");
  assert(r.plan.delayS === BEAT || r.plan.delayS === BEAT / 2, "echo 1/2 or 1 beat");
  assert(Math.pow(r.plan.feedback, r.plan.gapS / r.plan.delayS) <= 0.0101, "tail under -40 dB before B's vocal");
  assert.strictEqual(fx.planVocalThrow({ ...c, bVocalT: 14.6 }).refusal.gate, "gap_short");
  assert.strictEqual(fx.planVocalThrow({ ...c, lines: [] }).refusal.gate, "no_line", "no-op on a silent vocal");
}

// ---- S6 sweep direction --------------------------------------------------------------------------------------
assert.strictEqual(fx.sweepDirection(1000, 2000).dir, "lowpass");
assert.strictEqual(fx.sweepDirection(2000, 1000).dir, "highpass");
assert.strictEqual(fx.sweepDirection(null, 1000).fallbacks.length, 1, "unmeasured: fixed low-pass, listed");

// ---- supermove events: the same rule as mascot.js --------------------------------------------------------------
{
  const sb = { module: { exports: {} }, console };
  vm.createContext(sb);
  vm.runInContext(fs.readFileSync(path.join(STATIC, "mascot.js"), "utf8"), sb);
  const mascot = sb.module.exports;
  const evs = [["ai-supermove", { at: 5, name: "layer", deck: "b" }], ["ai-cue", { at: 3, kind: "drop", why: "strip & rebuild", deck: "a" }],
    ["ai-cue", { at: 3, kind: "transition", why: "Drop Swap: x" }], ["ai-cue", { at: 3, kind: "drop", why: "a plain drop" }], ["ai-activity", { at: 1 }]];
  for (const [type, d] of evs) assert.deepStrictEqual(JSON.stringify(fx.supermoveOf(type, d)), JSON.stringify(mascot.supermoveFor({ type, detail: d })), `${type} ${d.why || d.name}`);
}

// ---- replay planner + run-now gates -----------------------------------------------------------------------------------
{
  const ok = { name: "LAYER", songPos: 64, lineT: 80, bar: BAR, now: 79, energy: 0.9, energyQ3: 0.7, inTransition: false, holding: false,
    relaxed: false, otherPlaying: false, exitT: 300, duration: 320, replaysThisSong: 0, sinceLastS: 1e9, barWall: BAR, onGrid: true };
  const r = fx.planReplay(ok);
  assert(r.plan && r.plan.to === 64 && r.plan.line === 80, "back to the supermove's downbeat on the phrase line");
  for (const [k, v, gate] of [["replaysThisSong", 1, "once_per_song"], ["sinceLastS", 10, "cooldown"], ["inTransition", true, "transition"],
    ["otherPlaying", true, "other_deck"], ["energy", 0.2, "energy"], ["onGrid", false, "grid"], ["exitT", 100, "room"], ["relaxed", true, "relaxed"],
    ["songPos", 78, "short"], ["songPos", 10, "cap"]]) {
    assert.strictEqual(fx.planReplay({ ...ok, [k]: v }).refusal.gate, gate, gate);
  }
  assert(fx.planReplay({ ...ok, force: true, replaysThisSong: 1, sinceLastS: 0, energy: 0 }).plan, "on demand skips the rate gates");
  assert.strictEqual(fx.planReplay({ ...ok, force: true, otherPlaying: true }).refusal.gate, "other_deck", "never the safety gates");
  const st = { playing: true, inTransition: false, mashupActive: false, stemsReady: true, otherOnAir: false, supermove: true };
  assert.strictEqual(fx.runNowGate("kick_roll", st), null);
  assert.strictEqual(fx.runNowGate("kick_roll", { ...st, inTransition: true }).gate, "transition");
  assert.strictEqual(fx.runNowGate("kick_roll", { ...st, stemsReady: false }).gate, "stems");
  assert.strictEqual(fx.runNowGate("supermove_replay", { ...st, supermove: false }).gate, "no_supermove");
  assert.strictEqual(fx.runNowGate("mid_blend", st).gate, "no_blend");
  assert.strictEqual(fx.runNowGate("sweep_dir", { ...st, playing: false }).gate, "deck");
}

// ---- runtime: no rewind without a supermove; the replay lands on the line ------------------------------------------
{
  const timers = [], handlers = {}, steps = [], events = [], calls = [];
  const audio = { currentTime: 100 };
  const flags = {};
  const origInfo = console.info;
  console.info = () => {};
  const host = {
    clock: { now: () => audio.currentTime * 1000, perfNow: () => 0, setTimeout: (fn, ms) => { timers.push({ fn, due: audio.currentTime + ms / 1000 }); return timers.length; },
      clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].dead = true; }, setInterval: () => 1, clearInterval() {}, raf: () => 1, cancelRaf() {}, audioNow: () => audio.currentTime },
    api: { fetch: async () => ({ json: async () => ({}) }) },
    bus: { emit: (t, d) => { events.push({ t, d }); (handlers[t] || []).forEach((f) => f({ detail: d })); }, on: (t, f) => (handlers[t] = handlers[t] || []).push(f), off() {} },
    log: { step: (k, o) => steps.push([k, o]) },
    ui: { flag: (id, dflt) => (id in flags ? flags[id] : dflt), el: () => null, query: () => null, queryAll: () => [], create() {}, fire() {}, status() {}, cssVar: () => "" },
    storage: { get: () => null, set() {} }, random: { uuid: () => "u", next: () => 0.5 },
    audio, decks: {}, state: {}, session: { relaxed: false }, mod: {}, expose() {}, loadIntoDeck() {},
  };
  const run = (until) => {
    for (;;) {
      const t = timers.filter((x) => !x.dead && !x.done && x.due <= until).sort((a, b) => a.due - b.due)[0];
      if (!t) break;
      t.done = true; audio.currentTime = Math.max(audio.currentTime, t.due); t.fn();
    }
    audio.currentTime = until;
  };
  const down = Array.from({ length: 200 }, (_, i) => i * BAR);
  const analysis = { downbeat_times: down, phrase_boundaries_8bar: down.filter((_, i) => i % 8 === 0),
    energy_times: [0, 60, 70, 200], energy_curve: [0.3, 0.95, 0.4, 0.4] };
  const t0 = audio.currentTime, p0 = 60;                  // song 60 s at audio 100 s
  const d = { id: "a", playing: true, bpm: BPM, analysis, buffer: { duration: 400 }, stemsReady: false, loopOn: false,
    _currentPosition: () => p0 + (audio.currentTime - t0), _positionAt: (t) => p0 + (t - t0), _playbackRate: () => 1, _onAir: () => true,
    crossfaderGain: { gain: { value: 1 } },
    _rampToStop: (s) => { calls.push(["brake", audio.currentTime, s]); return true; }, brake: () => calls.push(["brake", audio.currentTime, 0.8]),
    play: (to, spin, when) => calls.push(["play", to, when]) };
  host.decks = { a: d, b: { id: "b", playing: false } };
  host.mod.djMind = { transitioning: false, fireAt: () => 300 };
  Engine.use(host);
  const api = Engine.create("fxMoves");
  assert(api && host.mod.fxMoves === api, "registers with the Engine");

  let res = api.runNow("supermove_replay");
  assert(!res.ok && /no_supermove/.test(res.why), "on demand, no supermove: refused");
  run(t0 + 2);                                             // song 62: a phrase and more, nothing booked
  assert.strictEqual(api.rewinds, 0, "no rewind / spinback without a preceding supermove");
  assert(!calls.length, "the deck was never braked or restarted");

  // a LAYER supermove at song 64 (a downbeat, peak energy) -> replay booked on the next phrase line (song 80)
  const at = audio.currentTime + (64 - d._currentPosition());
  host.bus.emit("ai-supermove", { at, name: "LAYER", deck: "a" });
  run(at + 20);
  assert.strictEqual(api.rewinds, 1, "one replay after the supermove");
  const brake = calls.find((c) => c[0] === "brake"), play = calls.find((c) => c[0] === "play");
  const line = at + 16;                                    // song 80 = the phrase line after 64 (8 bars of 2 s)
  assert(brake && brake[2] <= fx.REWIND_MAX_S + 1e-9 && near(brake[1] + brake[2], line, 0.05), "a capped brake that ends on the line");
  assert(play && near(play[1], 64, 1e-6) && near(play[2], line, 1e-6), "the deck restarts at the supermove's downbeat exactly on the line");
  assert(events.some((e) => e.t === "ai-activity" && e.d.kind === "artist_move" && e.d.move === "supermove_replay"), "logged as artist_move: supermove_replay");

  // same song, another supermove: once per song
  calls.length = 0;
  const at2 = audio.currentTime + (100 - d._currentPosition());
  host.bus.emit("ai-supermove", { at: at2, name: "LAYER", deck: "a" });
  run(at2 + 20);
  assert.strictEqual(api.rewinds, 1, "once per song");
  assert(!calls.length, "nothing armed when refused");

  // on demand now that a supermove exists, but during a transition: refused (a safety gate)
  host.mod.djMind.transitioning = true;
  res = api.runNow("supermove_replay");
  assert(!res.ok && /transition/.test(res.why), "on demand never rewinds during a transition");
  host.mod.djMind.transitioning = false;
  assert(steps.some((s) => s[0] === "artist_move_now" && s[1].decision === "refused"), "on-demand runs are step-logged");

  // kick roll on demand without stems: the named refusal
  res = api.runNow("kick_roll");
  assert(!res.ok && /stems/.test(res.why), "kick roll needs the drum stem");
  console.info = origInfo;

  // the only rewind call site is the replay decision
  const src = fs.readFileSync(path.join(STATIC, "fx-moves.js"), "utf8");
  assert.strictEqual((src.match(/[^n] rewind\(d, /g) || []).length, 1, "rewind() is called from one place (decide)");
  assert(!/_rampToStop|\.brake\(|\.play\(/.test(src.replace(/function rewind[\s\S]*?\n    }\n/, "")), "no brake / restart outside the rewind primitive");
}
console.log("fx moves check: ok");
