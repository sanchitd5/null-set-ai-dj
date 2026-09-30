// Node checks for the late-entry rule (owner spec, "Neverland -> Nocturnal"): strong-downbeat
// candidates across the whole song, the main-drop rule by energy band, the seeded uniform pick,
// and the shared fixture both twins run (app/music_brain/render/entry_lines.py).
const assert = require("assert");
const path = require("path");
const ap = require("../../ui/static/autopilot.js");
const fx = require(path.join(__dirname, "../fixtures/entry_line_cases.json"));
let n = 0;

// ---- fixture parity (the Python twin runs the same file) ----
for (const k of fx.lineCases) {
  const m = k.o.drums;
  const o = Object.assign({}, k.o, { drums: m ? (t) => (m[String(t)] == null ? null : m[String(t)]) : null });
  assert.deepStrictEqual(ap.entryLines(o), k.expect, k.name); n++;
}
for (const k of fx.hashCases) { assert.strictEqual(ap.hashSeed(k.s), k.h, `hash ${k.s}`); n++; }
for (const k of fx.rngCases) {
  const r = ap.seededRng(k.seed);
  assert.deepStrictEqual(k.first.map(() => r()), k.first, `rng ${k.seed}`); n++;
}
for (const k of fx.pickCases) {
  const r = ap.seededRng(k.seed);
  assert.deepStrictEqual(k.expect.map(() => ap.pickEntryLine(k.cands, k.setLevel, r)), k.expect, k.name); n++;
}

// ---- semantics ----
const bar = 240 / 128, lines = [0, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165];
const tt = [], cv = [];
for (let t = 0; t < 190; t++) { tt.push(t); cv.push(t < 15 ? 0.1 : t >= 90 && t < 105 ? 0.95 : 0.6); }
const o = { lines, include: 0, energyTimes: tt, energyCurve: cv, vocals: [], drops: [{ t: 90, energy: 0.95 }], bar, end: 190, roomS: 60 };
// never inside a quiet intro, even when it is the console's own line
const rel = ap.entryLines(Object.assign({ band: "relaxed" }, o));
assert.ok(!rel.lines.includes(0)); assert.strictEqual(rel.rejected.find((r) => r.t === 0).why, "quiet (not a strong downbeat)"); n++;
// relaxed / middle: main drop must stay ahead; high: on / after the drop is fine
assert.ok(rel.lines.every((t) => t < 90)); n++;
assert.deepStrictEqual(ap.entryLines(Object.assign({ band: "middle" }, o)).lines, rel.lines); n++;
const hi = ap.entryLines(Object.assign({ band: "high" }, o));
assert.ok(hi.lines.includes(90) && hi.lines.includes(120)); n++;
// room: the play window (and the next move) must fit before the audible end
assert.ok(hi.lines.every((t) => 190 - t >= 60) && !hi.lines.includes(135)); n++;
assert.strictEqual(ap.entryRoomS({ min: 40, max: 100, xf: 8 }), 60); n++;
// late lines are candidates: the pool spans the whole song, not just its first 45 %
assert.ok(Math.max(...hi.lines) > 0.45 * 190); n++;
// intro recipes may enter on the quiet intro on purpose
assert.ok(ap.introRecipe("Breakdown Transition") && !ap.introRecipe("Bass Swap")); n++;
assert.ok(ap.entryLines(Object.assign({ band: "high", introOk: true }, o)).lines.includes(0)); n++;
// drumless line refused
const dr = ap.entryLines(Object.assign({ band: "high", drums: (t) => (t === 30 ? 0.1 : 1) }, o));
assert.strictEqual(dr.rejected.find((r) => r.t === 30).why, "no drums (not a strong downbeat)"); n++;

// uniform, early and late alike: every good line within +-15 % of its fair share
const cands = [{ t: 7, level: 8 }, { t: 37, level: 8 }, { t: 95, level: 7 }, { t: 150, level: 6 }, { t: 120, level: 1 }];
const r = ap.seededRng(ap.hashSeed("dist")), hits = {};
const N = 8000;
for (let i = 0; i < N; i++) { const p = ap.pickEntryLine(cands, 8, r); hits[p.t] = (hits[p.t] || 0) + 1; }
assert.ok(!hits[120], "a line off the set level is never picked");
for (const t of [7, 37, 95, 150]) assert.ok(Math.abs(hits[t] - N / 4) < 0.15 * N / 4, `share of ${t}: ${hits[t]}`);
n++;
// reproducible: same seed, same picks
const a = ap.seededRng(123), b = ap.seededRng(123);
for (let i = 0; i < 20; i++) assert.strictEqual(ap.pickEntryLine(cands, 8, a).t, ap.pickEntryLine(cands, 8, b).t);
n++;
// none good -> nearest (pickEntryByEnergy), never nothing
assert.strictEqual(ap.pickEntryLine([{ t: 3, level: 1 }, { t: 9, level: 3 }], 10, r).t, 9); n++;
assert.strictEqual(ap.pickEntryLine([], 5, r), null); n++;

console.log(`entry_lines_check: ${n} ok`);
