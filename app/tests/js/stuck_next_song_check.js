// Node check for the stuck next song (session 2026-09-30_225021, owner: "current set stuck on picking
// up next song, even when it is a macro prestudied set triggered"). PLAY MACRO step 1 (Martin Roth ->
// Alex Wann, Stem Bridge) was vibe-rejected once, the pair reject was remembered, every later PLAY MACRO
// step died on it silently, the atlas backups died on the same measured gates and every model suggest
// failed lookup: HOLD LOOP forever.
const assert = require("assert");
const core = require("../../ui/static/autopilot.js");
const { measuredWaiver, deadlineStuck, stuckBackups, STUCK_AFTER_HOLDS, rankAtlasBackups,
        rememberPairReject, pairRejected } = core;

const MARTIN = "0b1d2975041c407a", WANN = "106093461d28b09f", DAFT = "d8cc418d469c43fa";

// 1) the remembered vibe reject is still there for the next PLAY MACRO round...
const store = new Map();
rememberPairReject(store, MARTIN, WANN, "brighter tone (811 Hz -> 2203 Hz); energy jump too big (0.23 -> 0.41)", false);
assert.ok(pairRejected(store, MARTIN, WANN, false), "the pair reject is remembered");
// ...but a running macro step is a known compatible pair: the measured gates are waived for it
const step = { _macro: { name: "studied-set-n_gfh09ip9c", step: { n: 1, b_name: "Alex Wann" }, run: true } };
assert.ok(/PLAY MACRO: studied-set-n_gfh09ip9c step 1 is a known compatible pair/.test(measuredWaiver(step)));
assert.ok(/armed by the owner/.test(measuredWaiver({ _macro: { name: "m", step: { n: 3 }, byUser: true } })));
// a macro step the autopilot merely offered (MACRO MODE preference, not PLAY MACRO) keeps the gates
assert.strictEqual(measuredWaiver({ _macro: { name: "m", step: { n: 1 } } }), null);
assert.strictEqual(measuredWaiver({ track_id: "x" }), null);                      // a model pick: gates stay
assert.strictEqual(measuredWaiver(null), null);

// 2) the atlas backup: every row fails a measured gate (the suggest loop only returns invented songs)
const rows = [
  { b: WANN, b_name: "Alex Wann - Allo", studied: true, works: 90, b_level: 9, scene_rel: "scene" },
  { b: DAFT, b_name: "Daft Punk - Da Funk", combo: true, works: 90, b_level: 9, scene_rel: "family" },   // energy step fails
  { b: "played", b_name: "Played", combo: true, works: 99, scene_rel: "scene" },
  { b: "cross", b_name: "Skrillex", works: 80, scene_rel: "cross", b_genre: "dubstep" },
];
const r = rankAtlasBackups(rows, { aId: MARTIN, played: ["played"], energyA: 3,
  rejected: (b) => pairRejected(store, MARTIN, b, false) });
assert.strictEqual(r.list.length, 0, "no row passes the measured gates: the deadline held");
// not stuck yet: the normal list (empty) -> hold
assert.deepStrictEqual(deadlineStuck({ id: MARTIN, n: STUCK_AFTER_HOLDS - 1 }, MARTIN), { n: STUCK_AFTER_HOLDS - 1, stuck: false });
assert.strictEqual(deadlineStuck({ id: "other", n: 9 }, MARTIN).n, 0);            // holds count per song
// stuck: the rows set aside only by a measured gate come back, best first; never played / cross
const st = deadlineStuck({ id: MARTIN, n: STUCK_AFTER_HOLDS }, MARTIN);
assert.ok(st.stuck);
const back = stuckBackups(r).map((c) => c.track_id);
assert.deepStrictEqual(back, [WANN, DAFT], "studied combo first, then the atlas combo");
assert.ok(!back.includes("played") && !back.includes("cross"));
// and those rows go through tryCandidate with the gates waived
assert.ok(/stuck deadline: 2 holds on this song/.test(measuredWaiver({ _stuck: "2 holds on this song" })));

console.log("stuck next song: macro step + stuck deadline book a known pair ok");
