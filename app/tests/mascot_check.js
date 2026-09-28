// Node check for the mascot mood logic (app/ui/static/mascot.js). Run by test_keylock.py.
const assert = require("assert");
const { mood, LABEL } = require("../ui/static/mascot.js");
const base = { now: 10, hypeUntil: 0, grooveUntil: 0, holdLoop: false, llm: 0, omni: 0 };
assert.strictEqual(mood(base), "idle");
assert.strictEqual(mood({ ...base, llm: 1 }), "think");
assert.strictEqual(mood({ ...base, llm: 1, omni: 1 }), "listen");
assert.strictEqual(mood({ ...base, omni: 1, grooveUntil: 12 }), "groove");
assert.strictEqual(mood({ ...base, grooveUntil: 12, holdLoop: true }), "sweat");
assert.strictEqual(mood({ ...base, holdLoop: true, hypeUntil: 11 }), "hype");
assert.strictEqual(mood({ ...base, grooveUntil: 9 }), "idle");            // expired
assert.ok(Object.keys(LABEL).length === 6);

// ---- SUPERMOVE takeover ----------------------------------------------------
const m = require("../ui/static/mascot.js");
const cue = (kind, why, at = 12.5, deck = "b") => ({ type: "ai-cue", detail: { at, kind, why, deck, bar: 1.875 } });
// the real "why" strings the engines dispatch (stem-moves.js, autopilot.js, riff-over-rap.js)
const YES = [
  [cue("drop", "everything slams back after the strip & rebuild"), "STRIP & REBUILD"],
  [cue("drop", "B takes every stem on the line after the merge"), "MERGE"],
  [cue("drop", "B's beat takes over after the mashup"), "MASHUP"],
  [cue("drop", 'the beat slams back after "never let you go"'), "HOOK DROP"],
  [cue("transition", "Double Drop: B's first downbeat"), "DOUBLE DROP"],
  [cue("transition", "Drop Swap: B's first downbeat"), "DROP SWAP"],
  [cue("line", "B's rap arrives: open hat on the line"), "RIFF OVER RAP"],
  [{ type: "ai-supermove", detail: { at: 3, name: "layer", deck: "a" } }, "LAYER"],
];
for (const [e, name] of YES) {
  assert.ok(m.isSupermove(e), name);
  assert.deepStrictEqual(m.supermoveFor(e), { name, at: e.detail.at, deck: e.detail.deck });
}
const NO = [
  cue("transition", "Bass Swap: B's first downbeat"),            // ordinary blend
  cue("transition", "LAYER 16+8 bars: B's first downbeat"),
  cue("drop", "B's beat lands after the stem bridge"),             // stem bridge: not a supermove
  cue("drop", undefined),                                          // the song's own drop
  cue("drop", "everything slams back after the strip & rebuild", NaN),   // no audio time
  cue("line", "a new layer arrives"),
  { type: "ai-activity", detail: { kind: "stem-move", at: 5, label: "MERGE → B", why: "x" } },  // label: booked, not the moment
  { type: "ai-activity", detail: { kind: "stem-move", at: 5, label: "HOOK DROP · \"x\"" } },
  { type: "ai-activity", detail: { kind: "stems", at: 5 } },       // plain stem move
  { type: "ai-supermove", detail: { at: 3, name: "  " } },
  { type: "ai-cue" }, null,
];
for (const e of NO) assert.strictEqual(m.isSupermove(e), false, JSON.stringify(e));
assert.strictEqual(m.supermoveFor(cue("drop", "B's beat takes over after the mashup", 1, "zz")).deck, "");

// gates: AI driving, VFX on, de-dup window (never twice within ~8 s)
const g = { aiActive: true, vfxOn: true, booked: [], hitS: 100 };
assert.strictEqual(m.supermoveGate(g), "");
assert.strictEqual(m.supermoveGate({ ...g, aiActive: false }), "AI not driving");
assert.strictEqual(m.supermoveGate({ ...g, vfxOn: false }), "VFX off");
assert.strictEqual(m.SUPERMOVE_WINDOW_S, 8);
assert.strictEqual(m.supermoveGate({ ...g, booked: [93] }), "de-dup");
assert.strictEqual(m.supermoveGate({ ...g, booked: [107.9] }), "de-dup");
assert.strictEqual(m.supermoveGate({ ...g, booked: [92] }), "");
assert.strictEqual(m.supermoveGate({ ...g, booked: [108.5] }), "");
// VFX toggle: localStorage "nul.vfx" wins, else the button's aria-pressed
assert.strictEqual(m.vfxOn("off", "true"), false);
assert.strictEqual(m.vfxOn("on", "false"), true);
assert.strictEqual(m.vfxOn(null, "false"), false);
assert.strictEqual(m.vfxOn(null, "true"), true);
assert.strictEqual(m.vfxOn(null, null), true);

// clock offset: audio time -> performance.now() ms (+ output latency)
assert.strictEqual(m.hitPerfMs(12, 10, 5000, 0), 7000);
assert.strictEqual(m.hitPerfMs(12, 10, 5000, 0.02), 7020);
assert.strictEqual(m.hitPerfMs(12, 10, 5000, undefined), 7000);
// tempo: bpm * playbackRate, folded into 0.3-0.75 s; cue bar / 4 as fallback
const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} != ${b}`);
near(m.beatSeconds(128, 1), 60 / 128);
near(m.beatSeconds(120, 1.05), 60 / 126);
near(m.beatSeconds(174, 1), 60 / 174);
near(m.beatSeconds(70, 1), 60 / 140);          // half time -> nods on every half beat
near(m.beatSeconds(240, 1), 0.5);              // 0.25 s -> doubled
near(m.beatSeconds(0, 1, 2), 0.5);             // bar 2 s -> beat 0.5 s
near(m.beatSeconds(undefined, 1, undefined), 60 / 124);

// timeline: the hit lands on the cue; total 2.5-4 s; late / far cues skipped
const beat = 60 / 128, now = 1000;
const p = m.takeoverPlan(now + 5000, now, beat);
assert.strictEqual(p.hit, now + 5000);         // biggest hit exactly on the cue
near(p.start, p.hit - 2 * beat * 1000 - 550);  // fly in, 2 beats of dancing, then the hit
near(p.outAt, p.hit + 3 * beat * 1000);
const total = p.end - p.start;
assert.ok(total >= 2500 && total <= 4000, `total ${total}`);
for (const b of [0.3, 0.5, 0.75]) {            // any tempo stays under ~4 s
  const q = m.takeoverPlan(now + 9000, now, b);
  assert.ok(q.end - q.start <= 4000 && q.end - q.start >= 2000, `beat ${b}: ${q.end - q.start}`);
  assert.strictEqual(q.hit, now + 9000);
}
const soon = m.takeoverPlan(now + 300, now, beat);   // little lead: starts now, hit after the fly-in
assert.strictEqual(soon.start, now);
assert.strictEqual(soon.hit, now + 550);
assert.strictEqual(m.takeoverPlan(now - 500, now, beat), null);        // missed
assert.ok(m.takeoverPlan(now - 100, now, beat));                        // just now: still shown
assert.strictEqual(m.takeoverPlan(now + 200000, now, beat), null);     // absurdly far
assert.strictEqual(m.takeoverPlan(NaN, now, beat), null);
// beat phase: a beat loop started at `now` is at progress 0 on the hit
for (const [n, h, b] of [[1000, 6000, 468.75], [1000, 1200, 500], [0, 0, 400], [7000, 5000, 500]]) {
  const d = m.beatPhaseMs(n, h, b);
  assert.ok(d <= 0 && d > -b, `phase ${d}`);
  near((((h - n - d) % b) + b) % b, 0);
}

console.log("mascot ok");
