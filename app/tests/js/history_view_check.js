// node app/tests/js/history_view_check.js: REPLAY, TIME TRAVEL and LIKED in the console
// (history-view.js core, macro-mode.js likedFor / storedMove / defaultPlan, fx-moves.js storedThrow).
const assert = require("assert");
const hv = require("../../ui/static/history-view.js");
const mm = require("../../ui/static/macro-mode.js");
const fx = require("../../ui/static/fx-moves.js");
const ap = require("../../ui/static/autopilot.js");

const CATCH = "dd3f0c201c0c0f03", NOCTRL = "407c498ddd3a6dab", NEVER = "c4a392ce13e82bc9", NOCT = "3ef9ad4b3fd01c79";
const THROW = { delayS: 0.46875, feedback: 0.6, wet: 0.35, gapS: 7.32, band: [250, 3000] };
const NEVER_NOCT = { a: NEVER, b: NOCT, a_name: "Anyma & Baset - Neverland (From Japan) (HNTR Remix)", b_name: "Adam Sellouk & Doriann - Nocturnal",
  recipe: "Echo Out", a_time: 157.47, b_time: 18.79, tempo: { lock: "pitched" },
  moves: [{ kind: "artist_move", move: "vocal_throw", dt: -0.003, side: "a", params: THROW }] };
const LIKED = [
  { a: CATCH, b: NOCTRL, a_name: "Adapter - Catchaman", b_name: "PACS & Ruiz (BR) - No Control", replay: false,
    step: { a: CATCH, b: NOCTRL, recipe: "Long Blend", a_time: 194.93, b_time: 3.32 } },
  { a: NEVER, b: NOCT, a_name: NEVER_NOCT.a_name, b_name: NEVER_NOCT.b_name, replay: true, step: NEVER_NOCT },
];

// 1) history rows
{
  const rows = hv.historyRows({ transitions: [Object.assign({ n: 11, at: "00:16:40", planned: null, liked: true }, NEVER_NOCT)] });
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].label, "00:16:40 Neverland > Nocturnal");
  assert.strictEqual(rows[0].detail, "Echo Out · exit 2:37 / entry 0:18 · vocal_throw");
  assert.ok(rows[0].liked);
  assert.deepStrictEqual(hv.historyRows(null), []);
}

// 2) the replay schedule: the Echo Out performs the vocal throw itself; a roll is called its lead ahead
{
  const step = { moves: NEVER_NOCT.moves.concat([{ move: "roll", dt: 5, side: "a" }, { move: "AUTO SAMPLER · drop", dt: 9.7 },
    { move: "slip_loop", dt: 2, side: "b" }, { move: "kick_roll" }]) };
  const r = hv.replaySchedule(step, { recipe: "Echo Out", barS: 1.875 });
  assert.deepStrictEqual(r.fire, [{ move: "roll", via: "artist", side: "a", callDt: 3.125 }, { move: "slip_loop", via: "artist", side: "b", callDt: 0 }]);
  assert.deepStrictEqual(r.left.map((x) => x.move), ["vocal_throw", "AUTO SAMPLER · drop", "kick_roll"]);
  assert.ok(/recipe/.test(r.left[0].why));
  // a throw that played under another recipe has no on-demand seam in a transition: left, said so
  assert.strictEqual(hv.replaySchedule(step, { recipe: "Bass Swap" }).left[0].move, "vocal_throw");
}

// 3) POST /api/replay answer -> action
{
  const res = { replay: { macro: { name: "replay-x", steps: [NEVER_NOCT] }, gaps: [{ n: 11, gaps: [] }], cut: null },
                load: { deck: "a", track_id: NEVER, name: NEVER_NOCT.a_name, pos: 149.47 }, restarted_transition: true };
  const a = hv.travelAction(res);
  assert.deepStrictEqual(a.load, { track_id: NEVER, name: NEVER_NOCT.a_name, pos: 149.47 });
  assert.ok(/from Neverland at 2:29 \(the transition restarts whole\)/.test(a.line), a.line);
  assert.ok(hv.travelAction({ replay: null, load: { track_id: NEVER } }).error);
  assert.ok(hv.travelAction(Object.assign({}, res, { load: { track_id: "../etc" } })).error);
}

// 4) liked: a stored step for the pair; replay false is a record only (never steers)
{
  const s = mm.likedFor(LIKED, NEVER, NOCT);
  assert.strictEqual(s.recipe, "Echo Out");
  assert.strictEqual(mm.likedFor(LIKED, CATCH, NOCTRL), null, "Catchaman -> No Control: kept, not steering");
  assert.strictEqual(mm.likedFor(LIKED, NOCT, NEVER), null);
  assert.deepStrictEqual(mm.storedMoveOf(s, "vocal_throw"), { move: "vocal_throw", params: THROW, dt: -0.003 });
  assert.strictEqual(mm.storedMoveOf(s, "roll"), null);
  const f = mm.forcedOf(s, { source: "liked", a: NEVER, b: NOCT });
  assert.strictEqual(f.moves[0].params, THROW, "the forced plan carries the stored moves");
  // the booking performs it as stored: Echo Out at the stored exit and entry
  const fb = ap.forcedBooking({ forced: f, nowPos: 120, phraseS: 15, trackEnd: 300, liveATime: 142, liveBTime: 0, beat: true, stemsBoth: true,
    keyScore: 0.3, mashupFits: false, mergeOn: true, planMerge: () => null, mergeGate: () => null });
  assert.strictEqual(fb.recipe, "Echo Out");
  assert.strictEqual(fb.aT, 157.47);
  assert.strictEqual(fb.bT, 18.79);
  assert.ok(!fb.refused);
}

// 5) the stored throw: the logged echo on the live word; the live tail gate still has the last say
{
  const live = { wordS: 156.9, lineEnd: 157.4, delayS: 0.46875, feedback: 0.55, wet: 0.35, gapS: 7.32, fallbacks: [], why: "live" };
  const k = fx.storedThrow(live, THROW).plan;
  assert.strictEqual(k.feedback, 0.6);
  assert.strictEqual(k.delayS, 0.46875);
  assert.ok(k.stored && /stored throw/.test(k.why));
  const short = fx.storedThrow(Object.assign({}, live, { gapS: 1.0, feedback: 0.02 }), THROW);
  assert.ok(short.plan.feedback <= 0.02 && /ring into B's vocal/.test(short.why), "a short live gap keeps the tail under -40 dB");
  assert.strictEqual(fx.storedThrow(live, null).plan, live, "no stored params: the live plan untouched");
  assert.strictEqual(fx.storedThrow(live, { feedback: 7 }).plan.feedback, 0.55, "a bad stored value is ignored");
}

// 6) runtime: a liked pair comes up -> forced as stored; storedMove feeds the throw; others untouched
(async () => {
  const logs = [];
  const el = () => ({ value: "", checked: false, innerHTML: "", textContent: "", addEventListener() {} });
  const host = {
    ui: { el, flag: (id, d) => d, status() {} }, clock: { now: () => 0, setTimeout() {} }, random: { next: () => 0.5, uuid: () => "x" },
    log: { step: (k, o) => logs.push([k, o.decision, o.why]) }, bus: { on() {} }, mod: {}, state: {}, decks: {},
    api: { fetch: async (url) => ({ ok: true, json: async () => (url === "/api/liked" ? { liked: LIKED } : url === "/api/macros" ? { macros: [] } : {}) }) },
  };
  const rt = mm.createRuntime({ host });
  await rt.refreshList();
  assert.strictEqual(rt.liked.length, 2);
  const c = rt.defaultPlan(NEVER, { track_id: NOCT, name: NEVER_NOCT.b_name }, { recipe: "Bass Swap", a_time: 140, b_time: 0 });
  assert.strictEqual(c.recipe, "Echo Out");
  assert.strictEqual(c.forced.source, "liked");
  assert.strictEqual(c.forced.a_time, 157.47);
  assert.ok(logs.some(([k, , w]) => k === "atlas" && /liked, as stored/.test(w)));
  assert.strictEqual(rt.storedMove(NEVER, NOCT, "vocal_throw").params.feedback, 0.6);
  assert.strictEqual(rt.storedStep(NEVER, NOCT).recipe, "Echo Out");
  // a pair that is not liked (or liked as a record only) keeps the normal plan, never forced
  const n = rt.defaultPlan(CATCH, { track_id: NOCTRL }, { recipe: "Bass Swap", a_time: 90, b_time: 0 });
  assert.ok(!n.forced, "record-only like: not forced");
  assert.strictEqual(rt.storedMove(CATCH, NOCTRL, "vocal_throw"), null);
  assert.strictEqual(rt.storedStep(NOCT, NEVER), null);
  // a replay macro's own step wins for its pair
  await rt.startReplay({ name: "r", source: "replay:s", steps: [Object.assign({ n: 1 }, NEVER_NOCT, { moves: [{ move: "vocal_throw", params: { feedback: 0.5 } }] })] },
    { track_id: NEVER, name: "x", pos: 10 }).catch(() => {});
  assert.strictEqual(rt.storedMove(NEVER, NOCT, "vocal_throw").params.feedback, 0.5);
  console.log("history view OK");
})().catch((e) => { console.error(e); process.exit(1); });
