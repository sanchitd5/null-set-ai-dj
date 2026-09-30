// Live-set fixes (session 2026-09-30_154332), console half:
//  - booking vet seams (autopilot.js storedMove / vetRefusal / vetStep): FOLLOW SET, macro steps and studied
//    combos obey the picks' rules, the refusal is logged with its gate; the owner armed step is his call
//  - OWNER VETO: badPairOf (the pair playing now), atlas backup / combo rows the server marked vetoed are skipped
//  - "never vocal mix a drop line" on the mashup transition (autopilot.js mashupGate / mashupBars), narrow: only
//    the drop window and the sung line into it, only where B's voice sounds (merges / holds are not gated)
const assert = require("assert");
const ap = require("../../ui/static/autopilot.js");
const mm = require("../../ui/static/macro-mode.js");
const DL = require("../../ui/static/drop-line.js");

// ---- stored moves
assert.strictEqual(ap.storedMove({ _follow: { set_id: "oRb_81stwy8" } }), true, "FOLLOW SET song");
assert.strictEqual(ap.storedMove({ _macro: { name: "m", step: {} } }), true, "macro step");
assert.strictEqual(ap.storedMove({ _combo: { studied: { djs: ["Anyma"] } } }), true, "studied combo");
assert.strictEqual(ap.storedMove({ _combo: { combo: "merge" } }), false, "an atlas combo is a scored pair, not a replay");
assert.strictEqual(ap.storedMove({ _macro: { byUser: true } }), false, "a step the owner armed by hand");
assert.strictEqual(ap.storedMove({ name: "pick" }), false);

// ---- the server's answer
assert.strictEqual(ap.vetRefusal({ results: [{ ok: true }] }), null);
assert.strictEqual(ap.vetRefusal(null), null, "no answer: today's behaviour");
const earlier = ap.vetRefusal({ results: [{ ok: false, gate: "earlier_set", why: "played in an earlier set" }] });
assert.deepStrictEqual(earlier, { gate: "earlier_set", why: "played in an earlier set" });

// ---- how a refusal is logged (the 16:37 replay: FOLLOW SET re-booked Masters At Work - Work)
let s = ap.vetStep({ _follow: { set_id: "oRb_81stwy8" } }, earlier);
assert.strictEqual(s.kind, "studied"); assert.strictEqual(s.decision, "refused"); assert.strictEqual(s.keep, false);
assert(/follow set refused \(earlier_set\): played in an earlier set/.test(s.why), s.why);
s = ap.vetStep({ _macro: {} }, { gate: "scene", why: "genre jump (melodic techno -> progressive house)" });
assert.strictEqual(s.kind, "macro"); assert.strictEqual(s.keep, true, "a pairwise refusal may fit after another song");
s = ap.vetStep({ name: "x" }, { gate: "veto", why: "owner veto: A -> B is a vibe killer" });
assert.strictEqual(s.kind, "candidate_reject"); assert.strictEqual(s.decision, "owner veto");

// ---- bad pair: the blend running, else the last pair
assert.strictEqual(ap.badPairOf({ history: ["A"], playedIds: ["a"] }), null);
assert.deepStrictEqual(ap.badPairOf({ history: ["A", "B"], playedIds: ["a", "b"] }), { a_id: "a", a_name: "A", b_id: "b", b_name: "B" });
assert.deepStrictEqual(ap.badPairOf({ history: ["A", "B"], playedIds: ["a", "b"], mixing: { aId: "b", aName: "B", bId: "c", bName: "C" } }),
  { a_id: "b", a_name: "B", b_id: "c", b_name: "C" }, "mid-blend: the pair sounding now");

// ---- vetoed atlas rows are skipped by every ranker, with the veto as the reason
const row = (b, extra) => Object.assign({ b, b_name: b, works: 90, combo: "merge", b_level: 5 }, extra);
const r = ap.rankAtlasBackups([row("santi", { vetoed: "owner veto: Hackney Pigeon -> Santigold is a vibe killer" }), row("ok1")], { aId: "hp" });
assert.deepStrictEqual(r.list.map((x) => x.track_id), ["ok1"]);
assert(/owner veto/.test(r.skipped[0].why));
const cc = mm.comboCandidates({ aId: "th", partners: [row("work", { vetoed: "owner veto: TH;EN - Bodyrock -> Work" }), row("fine")] });
assert.deepStrictEqual(cc.list.map((x) => x.track_id), ["fine"]);
assert(/owner veto/.test(cc.skipped[0].why));

// ---- mashup transition gate: narrow
assert.strictEqual(ap.mashupGate({ clash: null, busy: null }), null, "unknown genre passes");
assert.strictEqual(ap.mashupGate({ clash: false, busy: null }), null);
assert.strictEqual(ap.mashupGate({ clash: true, busy: null }).gate, "scene", "clear mismatch refuses");
const busy = DL.dropLineBusy([[64, 80]], [], 60, 70);
assert.strictEqual(ap.mashupGate({ clash: false, busy }).gate, "drop_line");

// a 32-bar mashup over A's drop window keeps its existing 16-bar variant when that one is clear
const at = (bad) => (m) => (bad.includes(m) ? { gate: "drop_line", reason: "x" } : null);
assert.deepStrictEqual(ap.mashupBars(32, { vocal16: 0.8 }, at([32])), { M: 16, busy: null });
assert.strictEqual(ap.mashupBars(32, { vocal16: 0.2 }, at([32])).busy.gate, "drop_line", "B's 16 bars do not sing enough: refused");
assert.strictEqual(ap.mashupBars(32, { vocal16: 0.8 }, at([32, 16])).busy.gate, "drop_line");
assert.deepStrictEqual(ap.mashupBars(16, { vocal16: 0.8 }, at([])), { M: 16, busy: null });

console.log("booking_vet_check ok");
