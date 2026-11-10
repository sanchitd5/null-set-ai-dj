// Node check for R3 web console parity with the phone fixes (HF2 scene-safe fallback + language gate, HF3 booking identity,
// HF4 no self-next + clean pick state, owner "YouTube suggestions when nothing passes"). Mirrors dj-core
// test/scene_anchor_test.dart, language_gate_test.dart, booking_deadline_test.dart and the phone's hf3 / hf4 tests.
// Synthetic names only. Runs under plain node.
const assert = require("assert");
const c = require("../../ui/static/autopilot.js");
const { sceneAnchorNext, sceneAnchorBad, languageRefusal, deadlineTail, earlierInScene, deckIdentity, bookingMismatch, pickReset, candSource, deckEvent, vetStep } = c;

const song = (name, genre, families, extra = {}) => Object.assign({ name, genre, families }, extra);
const DEEP = song("x", "deep house", ["house"]), HIP = song("h", "hip hop", ["hiphop"]), TECH = song("t", "tech house", ["house"]), METAL = song("m", "metal", ["metal"]);

// ---- HF2: one off-scene pick never moves the anchor; three agreeing in a row do (dj-core SceneAnchorBook shiftAfter 3)
{
  let st = sceneAnchorNext(null, DEEP);
  assert.strictEqual(st.anchor.genre, "deep house");
  st = sceneAnchorNext(st, HIP);
  assert.strictEqual(st.why, "off-scene pick 1/3: anchor kept");
  assert.strictEqual(st.anchor.genre, "deep house");
  st = sceneAnchorNext(st, song("?", "", []));
  assert.strictEqual(st.why, "genre unknown: anchor kept");
  st = sceneAnchorNext(st, HIP);
  assert.strictEqual(st.why, "off-scene pick 2/3: anchor kept");
  assert.strictEqual(st.anchor.genre, "deep house");
  st = sceneAnchorNext(st, HIP);
  assert.strictEqual(st.why, "deliberate move: the anchor follows");
  assert.strictEqual(st.anchor.genre, "hip hop");
  assert.strictEqual(st.recover, null);
}
// an in-scene song or a disagreeing off-scene song restarts the count
{
  let st = sceneAnchorNext(null, DEEP);
  st = sceneAnchorNext(sceneAnchorNext(st, HIP), HIP);
  st = sceneAnchorNext(st, TECH);
  assert.strictEqual(st.why, "in the scene");
  assert.strictEqual(sceneAnchorNext(st, HIP).why, "off-scene pick 1/3: anchor kept");
  const z = sceneAnchorNext(sceneAnchorNext(st, HIP), METAL);
  assert.strictEqual(z.why, "off-scene pick 1/3: anchor kept");
  assert.strictEqual(z.anchor.genre, "tech house");
}
// a fallback mistake and BAD PAIR reset the count; the vetoed pair still reads as before
{
  let st = sceneAnchorNext(null, DEEP);
  st = sceneAnchorNext(sceneAnchorNext(st, HIP), HIP);
  const m = sceneAnchorNext(st, Object.assign({ fallback: "library" }, HIP));
  assert.ok(m.mistake && m.shiftN === 0 && m.recover.anchor.genre === "deep house");
  const b = sceneAnchorBad(st, "h");
  assert.ok(b.mistake && b.shiftN === 0 && b.shiftTo === null);
  assert.strictEqual(sceneAnchorNext(null, DEEP).anchor.genre, "deep house");
  assert.strictEqual(sceneAnchorNext(sceneAnchorNext(null, DEEP), Object.assign({ vetoed: true }, HIP)).why, "vetoed pair left deep house for hip hop");
}

// ---- HF2: language gate verdicts (dj-core languageRefusal)
assert.strictEqual(languageRefusal("block", "xx not in set", false), "xx not in set");
assert.strictEqual(languageRefusal("block", "xx not in set", true), "xx not in set");
assert.strictEqual(languageRefusal("block", "", false), "language jump");
assert.strictEqual(languageRefusal("unverified", "set is hi", false), null);
assert.strictEqual(languageRefusal("unverified", "set is hi", true), "language unverified: set is hi");
assert.strictEqual(languageRefusal("unverified", "", true), "language unverified: sung language not identified in time");
for (const v of ["ok", "unknown", "penalty", undefined, null]) assert.strictEqual(languageRefusal(v, "r", true), null, String(v));
// a language refusal is never kept for later
assert.strictEqual(vetStep({}, { gate: "language", why: "w" }).keep, false);
assert.strictEqual(vetStep({}, { gate: "tempo", why: "w" }).keep, true);

// ---- HF2: scene-led deadline never crosses the family; repeats an earlier in-scene song
assert.deepStrictEqual(deadlineTail(true), ["earlier_in_scene"]);
assert.deepStrictEqual(deadlineTail(false), ["library_outside", "atlas_cross"]);
{
  const rows = [
    { track_id: "cur", name: "playing", ok: true, scene_rel: "scene" },
    { track_id: "e1", name: "e1", ok: true, scene_rel: "scene" },
    { track_id: "e2", name: "e2", ok: true, scene_rel: "family" },
    { track_id: "e3", name: "e3", ok: true, scene_rel: "cross" },
    { track_id: "e4", name: "e4", ok: true, scene_rel: "unknown" },
    { track_id: "e5", name: "e5", ok: false, scene_rel: "scene" },
  ];
  assert.deepStrictEqual(earlierInScene(rows, "cur").map((r) => r.track_id), ["e1", "e2"]);
  assert.deepStrictEqual(earlierInScene(null, "cur"), []);
}

// ---- owner R3: a YouTube-related fallback pick logs its source as yt_fallback
assert.strictEqual(candSource({ _fallback: "yt_fallback" }), "yt_fallback");
assert.strictEqual(candSource({ _fallback: "library" }), "library fallback");

// ---- HF3: the song shown is the file the deck holds
assert.strictEqual(deckIdentity("A", "A", "a", "a"), null);
assert.strictEqual(deckIdentity("A", "B", "song a", "song b"), "deck holds song b, not the booked song a");
assert.ok(/holds nothing/.test(deckIdentity("A", null, "song a")));
assert.strictEqual(deckIdentity("A", undefined, "song a"), null, "a host without deck state has nothing to compare");
{
  const ok = { curId: "A", nextId: "B", nextName: "b", activeHeld: "A", stagingHeld: "B" };
  assert.strictEqual(bookingMismatch(ok), null);
  assert.ok(/not the booked b/.test(bookingMismatch(Object.assign({}, ok, { stagingHeld: "C" }))), "booking B, deck holds C");
  assert.ok(/playing deck holds another song/.test(bookingMismatch(Object.assign({}, ok, { activeHeld: "Z" }))));
  // HF4: the next song is never the playing one
  assert.strictEqual(bookingMismatch(Object.assign({}, ok, { nextId: "A", stagingHeld: "A" })), "the next song is the playing song");
}
// the deck_load event carries what the deck really holds, and flags a mismatch
{
  const ev = deckEvent("deck_load", { deck: "b", cand: { name: "n", track_id: "B" }, loaded: "C", mismatch: true });
  assert.strictEqual(ev.track_id, "B");
  assert.strictEqual(ev.loaded_id, "C");
  assert.strictEqual(ev.mismatch, true);
  const plain = deckEvent("deck_load", { deck: "b", cand: { name: "n", track_id: "B" } });
  assert.ok(!("loaded_id" in plain) && !("mismatch" in plain), "old callers keep the old shape");
}

// ---- HF4: a hand start begins with a clean pick state
{
  const r = pickReset();
  assert.deepStrictEqual(r, { failedSearches: 0, failedFor: null, emptyStreak: 0, deadlineHolds: { id: null, n: 0 } });
  assert.notStrictEqual(pickReset().deadlineHolds, r.deadlineHolds, "a fresh object each time (no shared hold counter)");
}
console.log("r3 parity check ok");
