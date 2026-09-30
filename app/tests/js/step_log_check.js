// step-log.js pure core: payload shape, bounded queue, batching, song start / end.
const assert = require("assert");
const s = require("../../ui/static/step-log.js");

const p = s.payload("candidate_reject", { track_id: "abc", deck: "a", why: "x".repeat(900), at_song: 12.3456, inputs: { a: 1 }, junk: 5 }, 100);
assert.strictEqual(p.kind, "candidate_reject");
assert.strictEqual(p.track_id, "abc");
assert.strictEqual(p.why.length, 300);
assert.strictEqual(p.at_song, 12.35);
assert.deepStrictEqual(p.inputs, { a: 1 });
assert.ok(!("junk" in p));
assert.strictEqual(typeof s.payload("x", { result: { big: "y".repeat(5000) } }, 0).result, "string");
assert.strictEqual(s.payload(null, null, 0).kind, "step");

const q = { items: [] };
for (let i = 0; i < 10; i++) s.push(q, { i }, 4);
assert.strictEqual(q.items.length, 4);
assert.strictEqual(q.dropped, 6);
assert.strictEqual(q.items[0].i, 6);

const big = { items: [] };
for (let i = 0; i < 120; i++) s.push(big, { why: "z".repeat(1000) });
const b1 = s.takeBatch(big, 50, 20000);
assert.ok(b1.length > 0 && b1.length < 50);
assert.ok(JSON.stringify({ steps: b1 }).length <= 20000);
assert.strictEqual(big.items.length, 120 - b1.length);
assert.strictEqual(s.takeBatch({ items: [] }).length, 0);

const st = {};
let ev = s.songTick(st, [{ deck: "a", track_id: "T1", on: true, pos: 1 }]);
assert.deepStrictEqual(ev.map((e) => e.kind), ["song_start"]);
assert.strictEqual(s.songTick(st, [{ deck: "a", track_id: "T1", on: true, pos: 2 }]).length, 0);
for (let i = 0; i < s.OFF_POLLS - 1; i++) assert.strictEqual(s.songTick(st, [{ deck: "a", track_id: "T1", on: false }]).length, 0);
ev = s.songTick(st, [{ deck: "a", track_id: "T1", on: false }]);
assert.deepStrictEqual(ev.map((e) => [e.kind, e.at_song]), [["song_end", 2]]);
s.songTick(st, [{ deck: "b", track_id: "T2", on: true, pos: 0 }]);
ev = s.songTick(st, [{ deck: "b", track_id: "T3", on: true, pos: 0 }]);
assert.deepStrictEqual(ev.map((e) => e.kind + ":" + e.track_id), ["song_end:T2", "song_start:T3"]);
console.log("step log ok");
