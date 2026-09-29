// Node check for the FX budget (app/ui/static/fx-budget.js core): spend, refusal, decay, per-song and
// per-transition caps, the 30-minute cap and input validation.
const assert = require("assert");
const path = require("path");
const fb = require(path.join(__dirname, "..", "ui", "static", "fx-budget.js"));

const P = 15;                                   // phrase seconds (8 bars at 128)
const run = (s, kind, ctx, cost) => fb.record(s, kind, cost, ctx, fb.decide(s, kind, cost, ctx));
let n = 0;

// spend up to MAX_LEVEL, then refused until the level decays
let s = fb.newState();
assert.ok(run(s, "wet", { now: 0, phraseS: P, song: 1 }).ok); n++;
assert.ok(run(s, "loop", { now: 1, phraseS: P, song: 1 }).ok); n++;
let r = run(s, "vocal", { now: 2, phraseS: P, song: 2 });
assert.strictEqual(r.ok, false); assert.ok(/too many FX lately/.test(r.why), r.why); n++;
assert.deepStrictEqual(s.refused, { vocal: 1 }); n++;
// decay: one half-life (2 phrases) later the level is ~1, one more fits
r = run(s, "vocal", { now: 2 + fb.HALF_LIFE_PHRASES * P, phraseS: P, song: 2 });
assert.ok(r.ok, r.why); n++;
assert.deepStrictEqual(s.spent, { wet: 1, loop: 1, vocal: 1 }); n++;
assert.ok(Math.abs(fb.level([{ t: 0, cost: 2 }], 30, 30) - 1) < 1e-9); n++;

// per song cap (far apart in time so decay is not the reason)
s = fb.newState();
for (let i = 0; i < fb.MAX_PER_SONG; i++) { assert.ok(run(s, "wet", { now: i * 600, phraseS: P, song: "a" }).ok); n++; }
r = run(s, "wet", { now: 5000, phraseS: P, song: "a" });
assert.strictEqual(r.ok, false); assert.ok(/song budget/.test(r.why), r.why); n++;
assert.ok(run(s, "wet", { now: 5000, phraseS: P, song: "b" }).ok); n++;

// one FX per transition
s = fb.newState();
assert.ok(run(s, "wet", { now: 0, phraseS: P, song: 1, transition: 7 }).ok); n++;
r = run(s, "wet", { now: 500, phraseS: P, song: 2, transition: 7 });
assert.strictEqual(r.ok, false); assert.ok(/one FX per transition/.test(r.why), r.why); n++;
assert.ok(run(s, "wet", { now: 500, phraseS: P, song: 2, transition: 8 }).ok); n++;

// cost 2 (stacked) needs a free level of 2
s = fb.newState();
assert.ok(run(s, "wet", { now: 0, phraseS: P, song: 1 }).ok); n++;
assert.strictEqual(run(s, "wet", { now: 1, phraseS: P, song: 2 }, 2).ok, false); n++;

// 30-minute cap: many songs, each move one half-life apart x 4 so decay never binds
s = fb.newState();
let ok = 0;
for (let i = 0; i < 20; i++) if (run(s, "wet", { now: i * 120, phraseS: P, song: i }).ok) ok++;
assert.ok(ok <= fb.MAX_PER_30MIN + 5, `30 min cap ${ok}`); n++;
const within = s.events.filter((e) => e.t < fb.WINDOW_30MIN_S).length;
assert.ok(within <= fb.MAX_PER_30MIN, `within 30 min ${within}`); n++;

// validation: unknown kind, bad cost, no clock; missing phraseS falls back and says so
s = fb.newState();
assert.strictEqual(fb.decide(s, "laser", 1, { now: 0 }).ok, false); n++;
assert.strictEqual(fb.decide(s, "wet", -1, { now: 0 }).ok, false); n++;
assert.strictEqual(fb.decide(s, "wet", 1, {}).ok, false); n++;
r = fb.decide(s, "wet", 1, { now: 0 });
assert.ok(r.ok && r.fallbacks.includes(`phraseS=${fb.FALLBACK_PHRASE_S}`)); n++;
assert.ok(r.fallbacks.some((f) => f.startsWith("MAX_LEVEL="))); n++;
// decide is pure
assert.strictEqual(s.events.length, 0); n++;

console.log(`fx budget OK (${n})`);
