// Node check for the deadline-fallback fix (session 2026-09-30_191133, owner: "Pretty girl walk to Four
// Tet - Two Thousand and Seventeen; bad choice, previously loaded hanumankind but backed off") and the
// SCENE ANCHOR ("if a decision is made mistakenly, the next song should go back to the original genre").
const assert = require("assert");
const core = require("../../ui/static/autopilot.js");
const { preparedWait, PREPARED_LEAD_S, gateWhy, deckEvent, candSource, sceneAnchorNext, sceneAnchorBad, sceneOrder,
  rankAtlasBackups, crossBackups, rememberPairReject, pairRejected, searchPlan } = core;

// ---- the session's rows (steps.jsonl, t = epoch - 1790761700), labels from genre_labels.json ----
const S = {
  playing: { name: "Big Boss Vette - Pretty Girls Walk", genre: "hip hop", families: ["hiphop"] },
  hanumankind: { track_id: "b6597bf9c8107464", name: "Hanumankind & Kalmi - Big Dawgs", genre: "hip hop", families: ["hiphop"] },
  fourTet: { track_id: "ft", name: "Four Tet - Two Thousand and Seventeen (Fred V Bootleg)", genre: "electronic", families: ["electronic"] },
  joy: { track_id: "jo", name: "Joy Orbison - flight fm", genre: "house", families: ["electronic"] },
  // 175.5 candidate_reject plan-fit: "tempo gap (21.6% outside +-8%), no stems on both decks: only a hard Echo Out"
  reject: { t: 175.5, why: "tempo gap (21.6% outside +-8%), no stems on both decks: only a hard Echo Out" },
  // 185.4 atlas plan Long Blend exit 121 s; at 192.8 Pretty Girls was at 86.91 s -> 82.8 s at 188.7 (Four Tet's match)
  exitLo: 121, posAtDeadline: 82.8,
};

// 1) the deadline came (45 s before the exit window) while Hanumankind was being prepared
assert.ok(searchPlan({ pos: S.posAtDeadline, exitLo: S.exitLo }).deadline);
{
  // a later exit line to ride to (the window's hi): the prepared song is waited for, not replaced
  const w = preparedWait({ pos: S.posAtDeadline, exitLo: S.exitLo, exitHi: 170 });
  assert.strictEqual(w.wait, true);
  assert.strictEqual(w.book, false);
  assert.ok(Math.abs(w.budgetS - (170 - S.posAtDeadline - PREPARED_LEAD_S)) < 1e-9);
  // its stems land: book it
  assert.deepStrictEqual([preparedWait({ pos: 100, exitLo: S.exitLo, exitHi: 170, ready: true }).book], [true]);
  // only the stored exit left (exitHi = exitLo): still 8 s to wait
  assert.ok(preparedWait({ pos: S.posAtDeadline, exitLo: S.exitLo, exitHi: S.exitLo }).wait);
  // it really cannot make it: the fallback runs
  const late = preparedWait({ pos: 140, exitLo: S.exitLo, exitHi: 160 });
  assert.strictEqual(late.wait, false);
  assert.strictEqual(late.book, false);
  assert.ok(/cannot be ready/.test(late.why));
  assert.strictEqual(preparedWait({ pos: NaN, exitLo: 121 }).wait, false);
}

// 2) the plan-fit reject on stems is PENDING: lifted when the stems land (the pair is tried again)
{
  const store = new Map();
  rememberPairReject(store, "pgw", S.hanumankind.track_id, S.reject.why, false, "stems");
  let ready = false;
  const cleared = (what) => what === "stems" && ready;
  assert.ok(pairRejected(store, "pgw", S.hanumankind.track_id, false, cleared));
  ready = true;
  assert.strictEqual(pairRejected(store, "pgw", S.hanumankind.track_id, false, cleared), null);
  assert.strictEqual(store.size, 0);
  // an ordinary reject is not lifted by the cleared callback (old behaviour)
  rememberPairReject(store, "pgw", "x", "energy drop 8 -> 5", false);
  assert.ok(pairRejected(store, "pgw", "x", false, () => true));
  assert.ok(pairRejected(store, "pgw", "x", false));
}

// 3) "merge_gate refused key: undefined": stem-moves holdPlan refusals carry `reason`, planHold's own `why`
assert.strictEqual(gateWhy({ gate: "key", reason: "camelot 0 < 0.6", tried: [] }), "camelot 0 < 0.6");
assert.strictEqual(gateWhy({ gate: "stems", why: "stems for B (incoming) still rendering" }), "stems for B (incoming) still rendering");
assert.strictEqual(gateWhy({ gate: "key" }), "refused");

// 4) deck_load / deck_unload events
{
  const cand = Object.assign({ _follow: { set_id: "aLWCv6MGyho" } }, S.hanumankind);
  assert.deepStrictEqual(deckEvent("deck_load", { deck: "a", cand }),
    { deck: "a", song: S.hanumankind.name, track_id: S.hanumankind.track_id, source: "follow set", reason: "follow set" });
  const un = deckEvent("deck_unload", { deck: "a", cand, reason: `stems not ready (${S.reject.why})` });
  assert.strictEqual(un.reason, `stems not ready (${S.reject.why})`);
  assert.strictEqual(deckEvent("deck_unload", { deck: "a", cand }).reason, "rejected after load");
  assert.strictEqual(candSource(Object.assign({ _fallback: "library" }, S.fourTet)), "library fallback");
  assert.strictEqual(candSource({ _combo: { studied: true } }), "studied combo");
  assert.strictEqual(candSource({ suggestion: {} }), "pick");
}

// 5) SCENE ANCHOR tracking
{
  let st = { anchor: null, prev: null, recover: null };
  st = sceneAnchorNext(st, { name: "Travis Scott - FE!N", genre: "hip hop", families: ["hiphop"] });
  assert.deepStrictEqual(st.anchor, { genre: "hip hop", families: ["hiphop"] });
  assert.strictEqual(st.mistake, false);
  // normal drift inside the family: the anchor follows
  st = sceneAnchorNext(st, { name: "A$AP Ferg - Plain Jane", genre: "trap", families: ["hiphop"] });
  assert.strictEqual(st.anchor.genre, "trap");
  // unknown genre: the anchor stays
  const unk = sceneAnchorNext(st, { name: "?", families: [] });
  assert.deepStrictEqual(unk.anchor, st.anchor);
  // a deliberate move outside (a pick / a bridge): the anchor moves
  const moved = sceneAnchorNext(st, Object.assign({}, S.joy));
  assert.strictEqual(moved.mistake, false);
  assert.strictEqual(moved.anchor.genre, "house");
  // THE SESSION: Four Tet brought in by the deadline's library fallback -> an off-scene mistake
  const pg = sceneAnchorNext(st, S.playing);
  const ft = sceneAnchorNext(pg, Object.assign({ fallback: "library" }, S.fourTet));
  assert.strictEqual(ft.mistake, true);
  assert.deepStrictEqual(ft.anchor, { genre: "hip hop", families: ["hiphop"] });         // the anchor does not move
  assert.deepStrictEqual(ft.recover, { anchor: ft.anchor, mistaken: S.fourTet.name });
  // the pick back lands in the anchor: recovery over
  const back = sceneAnchorNext(ft, S.hanumankind);
  assert.strictEqual(back.why, "back in the scene");
  assert.strictEqual(back.recover, null);
  // an atlas backup outside the scene is a mistake too; one inside it is not
  assert.ok(sceneAnchorNext(pg, Object.assign({ fallback: "atlas" }, S.joy)).mistake);
  assert.ok(!sceneAnchorNext(pg, Object.assign({ fallback: "atlas" }, S.hanumankind)).mistake);
  // a vetoed pair (BAD PAIR pressed while it blended) is a mistake even from a normal pick
  assert.ok(sceneAnchorNext(pg, Object.assign({ vetoed: true }, S.joy)).mistake);
  // BAD PAIR on a pair that already landed: back to the anchor before it
  const bad = sceneAnchorBad(moved, S.joy.name);
  assert.strictEqual(bad.mistake, true);
  assert.strictEqual(bad.anchor.genre, "trap");
  assert.strictEqual(bad.recover.mistaken, S.joy.name);
  assert.strictEqual(sceneAnchorBad({ anchor: null }, "x").mistake, false);
}

// 6) recovery ranking (stored moves / pool): anchor scene first, unknown after, cross last, stable
{
  const rel = { [S.joy.name]: "cross", [S.hanumankind.name]: "scene", "Nobody - Unlabelled": "unknown",
    "Beyonce - Naughty Girl": "cross", "Sage The Gemini - Gas Pedal": "family" };
  const pool = [S.joy.name, "Beyonce - Naughty Girl", "Nobody - Unlabelled", S.hanumankind.name, "Sage The Gemini - Gas Pedal"].map((name) => ({ name }));
  const out = sceneOrder(pool, (c) => rel[c.name]).map((c) => c.name);
  assert.deepStrictEqual(out, [S.hanumankind.name, "Sage The Gemini - Gas Pedal", "Nobody - Unlabelled", S.joy.name, "Beyonce - Naughty Girl"]);
  assert.deepStrictEqual(sceneOrder([{ name: "a" }, { name: "b" }], () => undefined).map((c) => c.name), ["a", "b"]);   // no field: unchanged
}

// 7) atlas backup ranking with labels: cross-family partners set aside for the last resort, unknown after
{
  const rows = [
    { b: "ft", b_name: S.fourTet.name, works: 90, combo: true, scene_rel: "cross", b_genre: "electronic" },
    { b: "un", b_name: "Nobody - Unlabelled", works: 80, scene_rel: "unknown" },
    { b: "bd", b_name: S.hanumankind.name, works: 40, scene_rel: "scene", b_genre: "hip hop" },
    { b: "gp", b_name: "Sage The Gemini - Gas Pedal", works: 30, scene_rel: "family", b_genre: "hip hop" },
  ];
  const r = rankAtlasBackups(rows, { aId: "pgw" });
  assert.deepStrictEqual(r.list.map((c) => c.track_id), ["bd", "gp", "un"]);
  assert.deepStrictEqual(r.skipped.map((s) => [s.b, s.why]), [["ft", "cross-family (electronic)"]]);
  assert.deepStrictEqual(crossBackups(r).map((c) => c.track_id), ["ft"]);
  // rows without scene_rel (an old server): today's order
  const old = rankAtlasBackups(rows.map((x) => Object.assign({}, x, { scene_rel: undefined })), { aId: "pgw" });
  assert.deepStrictEqual(old.list.map((c) => c.track_id), ["ft", "un", "bd", "gp"]);
}

console.log("fallback fix ok");
