// Node check for G17 hold early-end: stem-moves endMergeHold (the handover re-booked on a phrase line when a long merge hold
// ends early). Runs the real module through a fake Host: no audio, no playwright. Synthetic decks only.
"use strict";
const assert = require("assert");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "ui", "static");
const Engine = require(path.join(STATIC, "engine.js"));
const sm = require(path.join(STATIC, "stem-moves.js"));

const BPM = 120, BAR = 240 / BPM, T0 = 100, M = 48;
const COMBO = { drums: "a", bass: "a", vocals: "b", other: "b" };
const pick = { combo: COMBO, label: "test merge" };

(async () => {
  const timers = [], notes = [], mixes = [];
  const origInfo = console.info;
  console.info = () => {};
  const host = {
    clock: { now: () => 0, setTimeout: (fn, ms) => { timers.push({ fn, ms, dead: false }); return timers.length; },
      clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].dead = true; }, perfNow: () => 0, setInterval: () => 1, clearInterval() {}, raf: () => 1, cancelRaf() {}, audioNow: () => 0 },
    api: { fetch: async () => ({ json: async () => ({}) }) },
    bus: { emit: (t, d) => { if (t === "ai-activity") notes.push(d); }, on() {}, off() {} },
    log: { step() {} }, ui: { flag: (id, d) => d, el: () => null, query: () => null, queryAll: () => [], create() {}, fire() {}, status() {}, cssVar: () => "" },
    storage: { get: () => null, set() {} }, random: { uuid: () => "u", next: () => 0.5 },
    audio: { currentTime: T0 - 10 }, decks: {}, state: {}, session: { relaxed: false }, mod: {}, expose() {}, loadIntoDeck() {},
  };
  const deck = (id) => ({ id, playing: true, bpm: BPM, analysis: {}, _playbackRate: () => 1, _currentPosition: () => 0, stemMix: (t, when, r) => mixes.push({ deck: id, t, when, r }) });
  host.decks = { a: deck("a"), b: deck("b") };
  Engine.use(host);
  const api = Engine.create("stemMoves");

  assert.strictEqual(typeof api.endMergeHold, "function");
  // refused without a pair or a pick: nothing booked
  assert.strictEqual(api.endMergeHold("a", "z", T0, 32, M, pick), 0);
  assert.strictEqual(api.endMergeHold("a", "b", T0, 32, M, null), 0);

  // the hold of M=48 bars was booked to hand over at bar 48; the guard ends it at bar 32 -> the handover is re-booked there
  timers.length = 0;
  api.endMergeHold("a", "b", T0, M, M, pick);               // the original booking (stands in for mergeTransition's)
  const original = timers.length;
  assert.ok(original > 0, "the handover events are booked");
  const END = 32;
  const secs = api.endMergeHold("a", "b", T0, END, M, pick);
  const plan = sm.mergeTransitionPlan(END, COMBO, false);
  assert.strictEqual(secs, plan.total * BAR, "the merge is done endBar + the 8-bar handover from t0");
  assert.ok(secs > END * BAR - 1e-6 && secs <= (END + 9) * BAR, `secs ${secs}`);
  assert.ok(timers.slice(0, original).every((t) => t.dead), "the old booking is forgotten (cancelled), nothing of bar 48 stays armed");

  // only events at the new handover (bar >= endBar - a quarter bar) are booked; the merge start (bar 0, 'start') is not
  const fresh = timers.slice(original).filter((t) => !t.dead);
  const want = plan.events.filter((e) => e.bar >= END - 0.25 - 1e-6 && !e.start);
  assert.strictEqual(fresh.length, want.length, `${fresh.length} timers for ${want.length} events`);
  assert.ok(want.length > 0);
  host.audio.currentTime = T0 + END * BAR - 1;
  fresh.forEach((t) => t.fn());
  assert.strictEqual(mixes.length, want.length, "every booked move reaches a deck's stemMix");
  for (const m of mixes) {
    assert.ok(m.when >= T0 + (END - 0.25) * BAR - 1e-6, `move at ${m.when} is before the handover line`);
    assert.ok(m.when <= T0 + (END + 8) * BAR + 1e-6);
  }
  // the owner sees it
  assert.ok(notes.some((n) => /MERGE HOLD ended early · 32 of 48 bars/.test(n.label)), JSON.stringify(notes));

  // a deck that stopped playing gets no move (book() skips a stopped deck)
  mixes.length = 0; timers.length = 0;
  api.endMergeHold("a", "b", T0, END, M, pick);
  host.decks.a.playing = false; host.decks.b.playing = false;
  timers.filter((t) => !t.dead).forEach((t) => t.fn());
  assert.strictEqual(mixes.length, 0, "stopped decks are not touched");

  console.info = origInfo;
  console.log("end merge hold check ok");
})().catch((e) => { console.error(e); process.exit(1); });
