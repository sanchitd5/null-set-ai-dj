// Replay of the real case (session 2026-09-30_225021, fixture copied from its songs/*/steps.jsonl, the
// real macro studied-set-n_gfh09ip9c and the real analysis): Martin Roth - Simplicite playing, PLAY MACRO
// step 1 -> Alex Wann (Stem Bridge), the vibe gate refused the pair (811 -> 2203 Hz, energy 0.23 -> 0.41),
// the pair reject was remembered, every model suggest failed, and each deadline logged "hold".
//
// What runs for real: macro-mode.js createRuntime with the real macro (loadMacro + playMacro), the real
// autopilotCore, and the real deadlineFallback body cut out of autopilot.js. Modelled: tryCandidate is
// evaluateCandidate's first two gates only (remembered reject, measured vibe from the recorded match),
// each waived by autopilotCore.measuredWaiver as the source does (asserted below); the atlas backup and the
// library fallback return what the session logged (nothing passed).
//
// REPLAY_STATIC_DIR=<dir with autopilot.js + macro-mode.js> runs it against another copy (e.g. main).
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const STATIC = process.env.REPLAY_STATIC_DIR || path.join(__dirname, "..", "..", "ui", "static");
const apFile = path.join(STATIC, "autopilot.js"), mmFile = path.join(STATIC, "macro-mode.js");
const core = require(apFile);
const mm = require(mmFile);
const fx = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "session_2026-09-30_225021.json"), "utf8"));
const MARTIN = fx.playing.track_id, WANN = fx.next.track_id;
const src = fs.readFileSync(apFile, "utf8");
const hasMacroFallback = typeof core.macroFallback === "function";

// the modelled gates are the source's: both measured gates give way to measuredWaiver
assert.ok(src.includes('if (known && waived) waive("remembered reject", known.why);'), "remembered-reject waiver in evaluateCandidate");
assert.ok(src.includes("if (candidate.vibe && candidate.vibe.ok === false && waived)"), "vibe waiver in evaluateCandidate");
// the recorded facts
assert.strictEqual(fx.macro.steps[0].a, MARTIN);
assert.strictEqual(fx.macro.steps[0].b, WANN);
assert.strictEqual(fx.macro.steps[0].recipe, "Stem Bridge");
assert.strictEqual(fx.match.vibe.ok, false);
assert.deepStrictEqual(fx.match.vibe.reasons, ["brighter tone (811 Hz -> 2203 Hz)", "energy jump too big (0.23 -> 0.41)"]);
assert.strictEqual(Math.round(fx.playing.analysis.energy.brightness_hz), 811);
assert.strictEqual(Math.round(fx.next.analysis.energy.brightness_hz), 2203);
const recordedHolds = fx.martin_steps.filter((s) => s.kind === "deadline_fallback" && s.decision === "hold");
assert.ok(recordedHolds.length >= 2, "the session held at every deadline");

function cutDeadlineFallback() {
  const i = src.indexOf("  let deadlineHolds = { id: null, n: 0 };");
  const j = src.indexOf("  async function waitPrepared(");
  assert.ok(i > 0 && j > i, "deadlineFallback found in autopilot.js");
  return src.slice(i, j);
}

(async () => {
  const logs = [];
  const els = {};
  const el = (id) => els[id] || (els[id] = { id, value: "", dataset: {}, checked: false, textContent: "", hidden: true, innerHTML: "", addEventListener() {}, click() {} });
  const host = {
    ui: { el, flag: (id, d) => d, status() {} }, clock: { now: () => 0, setTimeout() {} },
    random: { next: () => 0.5, uuid: () => "x" }, log: { step: (k, o) => logs.push({ kind: k, decision: o.decision, why: o.why, track_id: o.track_id }) },
    bus: { on() {}, emit() {} }, mod: { autopilotState: { active: true, activeDeck: "a" } }, state: { trackA: MARTIN, trackB: null },
    decks: { a: { buffer: {}, isPlaying: true, bpm: fx.playing.analysis.bpm, analysis: { key: fx.playing.analysis.key } }, b: {} },
    api: { fetch: async (url) => ({ ok: true, json: async () => (url === "/api/macros" ? { macros: [{ name: fx.macro.name, songs: 44 }] }
      : url.startsWith("/api/macros/") ? { macro: fx.macro, validation: [] } : url.startsWith("/api/atlas/partners") ? { partners: [] }
      : url === "/api/studied/sets" ? { sets: [] } : {}) }) },
  };
  const rt = mm.createRuntime({ host });
  host.mod.macroMode = rt;
  await rt.loadMacro(fx.macro.name);
  await rt.playMacro();
  assert.ok(rt.running, "PLAY MACRO runs the real macro");
  const first = await rt.firstCandidates(MARTIN, { played: [MARTIN], recent: [fx.playing.name] });
  assert.strictEqual(first[0].track_id, WANN, "PLAY MACRO step 1 is Alex Wann");
  assert.ok(logs.some((l) => l.kind === "macro" && l.decision === "PLAY MACRO step 1"), "logged as in the session");

  // the vibe reject was remembered (session 22:50:38)
  const pairRejects = new Map();
  core.rememberPairReject(pairRejects, MARTIN, WANN, fx.vibe_reject.why, false);
  assert.ok(core.pairRejected(pairRejects, MARTIN, WANN, false), "remembered");

  const booked = [];
  const tryCandidate = async (currentId, cand) => {
    const waived = core.measuredWaiver(cand);
    const known = core.pairRejected(pairRejects, currentId, cand.track_id, false);
    if (known && !waived) { host.log.step("candidate_reject", { track_id: cand.track_id, decision: "refused (remembered)", why: known.why }); return false; }
    const vibe = cand.track_id === WANN ? fx.match.vibe : null;
    if (vibe && vibe.ok === false && !waived) { host.log.step("candidate_reject", { track_id: cand.track_id, decision: "vibe reject", why: vibe.reasons.join("; ") }); return false; }
    booked.push({ track_id: cand.track_id, waived });
    return true;
  };
  // the atlas backup the session logged: nothing in the scene, two cross-family rows set aside
  const skipped = (recordedHolds[0].why.match(/\((.*)\)$/) || [, ""])[1].split("; ").map((s) => { const k = s.indexOf(": "); return { name: s.slice(0, k), why: s.slice(k + 2) }; });
  const backup = { list: [], skipped, cross: [] };
  const deps = { host, autopilotCore: core, preparedCandidate: () => null, waitPrepared: async () => null, recoverGenre: () => "",
    currentGenre: "", preplanBackup: async () => backup, tryCandidate, tryLibraryLockable: async () => false, apStatus: () => {},
    playedIds: [MARTIN], active: true, prepGen: 1 };
  const make = new Function("deps", `let { host, autopilotCore, preparedCandidate, waitPrepared, recoverGenre, currentGenre, preplanBackup,
    tryCandidate, tryLibraryLockable, apStatus, playedIds, active, prepGen } = deps;\n${cutDeadlineFallback()}\nreturn deadlineFallback;`);
  const deadlineFallback = make(deps);

  logs.length = 0;
  const r = await deadlineFallback(MARTIN, 1, "deadline");
  const dl = logs.filter((l) => l.kind === "deadline_fallback");
  console.log(`[replay ${hasMacroFallback ? "branch" : "main"}] first deadline after PLAY MACRO -> ${r === null ? "null (HOLD LOOP)" : r}`);
  for (const l of dl) console.log(`  deadline_fallback ${l.decision}: ${l.why}`);
  if (hasMacroFallback) {
    assert.strictEqual(r, "macro", "the macro song is the deadline's first fallback");
    assert.deepStrictEqual(booked.map((b) => b.track_id), [WANN], "Alex Wann booked");
    assert.ok(/PLAY MACRO/.test(booked[0].waived), "booked with the measured gates waived");
    assert.strictEqual(dl.length, 1);
    assert.strictEqual(dl[0].decision, "macro");
    assert.ok(dl[0].why.includes(`${fx.macro.name} step 1: Alex Wann`), dl[0].why);
  } else {
    assert.strictEqual(r, null, "main: nothing booked, HOLD LOOP");
    assert.strictEqual(booked.length, 0);
    assert.strictEqual(dl.length, 1);
    assert.strictEqual(dl[0].decision, "hold");
    assert.ok(dl[0].why.includes("Frank Ocean - Chanel: cross-family (r&b)"), dl[0].why);
  }
  console.log("macro fallback replay (session 2026-09-30_225021) ok");
})().catch((e) => { console.error(e); process.exit(1); });
