// Node check for the deadline's macro fallback (owner 2026-09-30: "in recent scenario where song was
// compatible in the live set, the vibe check rejected; ... if macro is coming from studied set it should
// be allowed, specially if no other track is identified, the macro defined track should be the fallback").
// Session 2026-09-30_225021: Martin Roth playing, studied-set macro step 1 -> Alex Wann (Stem Bridge),
// every model suggest failed lookup, the set sat in HOLD LOOP.
const assert = require("assert");
const core = require("../../ui/static/autopilot.js");
const mm = require("../../ui/static/macro-mode.js");
const { macroFallback, measuredWaiver } = core;
const { deadlineStepOf } = mm;

const MARTIN = "0b1d2975041c407a", WANN = "106093461d28b09f", DAFT = "d8cc418d469c43fa";
const studied = { name: "studied-set-n_gfh09ip9c", title: "Lane 8 Summer 2026 Mixtape",
  steps: [{ n: 1, a: MARTIN, b: WANN, b_name: "Alex Wann", recipe: "Stem Bridge" },
          { n: 2, a: WANN, b: DAFT, b_name: "Daft Punk", recipe: "Bass Swap" }] };
const yours = Object.assign({}, studied, { name: "set-2026-09-30" });

// 1) deadlineStepOf: only a running or armed STUDIED-SET macro defines the fallback
const ms = deadlineStepOf({ macro: studied, running: true, cursor: 0, aId: MARTIN });
assert.deepStrictEqual(ms, { name: studied.name, step: studied.steps[0], run: true });
assert.strictEqual(deadlineStepOf({ macro: studied, running: false, cursor: 0, aId: MARTIN }), null, "not running: none");
assert.strictEqual(deadlineStepOf({ macro: yours, running: true, cursor: 0, aId: MARTIN }), null, "not a studied set: none");
assert.strictEqual(deadlineStepOf({ macro: studied, running: true, cursor: 0, aId: "off" }), null, "playing song not in it");
assert.strictEqual(deadlineStepOf({ macro: studied, running: true, cursor: 0, aId: DAFT }), null, "last song: none");
assert.strictEqual(deadlineStepOf({ macro: null, running: false, cursor: 0, aId: MARTIN }), null);
assert.strictEqual(deadlineStepOf(null), null);
// armed (PLAY STEP) from a studied set, for this song: byUser; an armed step of another song or kind: none
const armed = { name: studied.name, kind: "studied", step: studied.steps[0] };
assert.deepStrictEqual(deadlineStepOf({ macro: studied, running: false, cursor: 0, aId: MARTIN, armed }),
  { name: studied.name, step: studied.steps[0], byUser: true });
assert.strictEqual(deadlineStepOf({ macro: studied, running: false, aId: WANN, armed }), null);
assert.strictEqual(deadlineStepOf({ running: false, aId: MARTIN, armed: Object.assign({}, armed, { kind: "yours" }) }), null);

// 2) macroFallback books the macro song, gates waived, logged as "macro"
(async () => {
  const logs = [];
  const log = (k, o) => logs.push(Object.assign({ kind: k }, o));
  let tried = null;
  const tryCand = async (c) => {
    tried = c;
    assert.ok(measuredWaiver(c), "the measured gates (vibe, remembered reject, energy step) are waived for it");
    return true;
  };
  const r = await macroFallback(ms, { currentId: MARTIN, played: [MARTIN], reason: "deadline", tryCand, log });
  assert.strictEqual(r, "macro");
  assert.strictEqual(tried.track_id, WANN);
  assert.ok(/PLAY MACRO: studied-set-n_gfh09ip9c step 1 is a known compatible pair/.test(measuredWaiver(tried)));
  assert.strictEqual(logs.length, 1);
  assert.strictEqual(logs[0].kind, "deadline_fallback");
  assert.strictEqual(logs[0].decision, "macro");
  assert.ok(/studied-set-n_gfh09ip9c step 1: Alex Wann \(deadline\)/.test(logs[0].why));

  // armed step: the waiver names the owner
  logs.length = 0;
  await macroFallback({ name: studied.name, step: studied.steps[0], byUser: true },
    { currentId: MARTIN, played: [], reason: "deadline", tryCand, log });
  assert.ok(/armed by the owner/.test(measuredWaiver(tried)));

  // 3) the macro song can't play (veto / tempo / stems / missing): logged, false -> the chain carries on
  logs.length = 0;
  const refuse = async (c) => { c._rejectWhy = "owner veto"; return false; };
  assert.strictEqual(await macroFallback(ms, { currentId: MARTIN, played: [], reason: "deadline", tryCand: refuse, log }), false);
  assert.strictEqual(logs[0].decision, "macro refused");
  assert.ok(/owner veto/.test(logs[0].why) && /falling through/.test(logs[0].why));
  // already played / the playing song: not even tried
  logs.length = 0; tried = null;
  assert.strictEqual(await macroFallback(ms, { currentId: MARTIN, played: [WANN], reason: "deadline", tryCand, log }), false);
  assert.strictEqual(tried, null);
  assert.ok(/already played/.test(logs[0].why));
  // 4) no macro: nothing tried, nothing logged (deadlineFallback unchanged)
  logs.length = 0;
  assert.strictEqual(await macroFallback(null, { currentId: MARTIN, played: [], reason: "deadline", tryCand, log }), false);
  assert.strictEqual(await macroFallback({ name: "m", step: null }, { currentId: MARTIN, reason: "x", tryCand, log }), false);
  assert.strictEqual(tried, null);
  assert.strictEqual(logs.length, 0);

  // 5) wiring: deadlineFallback asks the macro right after the prepared-candidate wait, before the atlas
  const src = require("fs").readFileSync(require("path").join(__dirname, "../../ui/static/autopilot.js"), "utf8");
  const body = src.slice(src.indexOf("async function deadlineFallback("), src.indexOf("async function waitPrepared("));
  const iPrep = body.indexOf("waitPrepared("), iMacro = body.indexOf("autopilotCore.macroFallback("),
        iAtlas = body.indexOf("preplanBackup(currentId)");
  assert.ok(iPrep > 0 && iMacro > iPrep && iAtlas > iMacro, "prepared wait -> macro -> atlas");
  assert.ok(/macroMode\.deadlineStep/.test(body));
  console.log("macro fallback: studied-set macro song is the deadline's first fallback ok");
})().catch((e) => { console.error(e); process.exit(1); });
