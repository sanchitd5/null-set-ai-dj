// Node checks for the late-entry rule (owner spec, "Neverland -> Nocturnal"): strong-downbeat
// candidates across the whole song, the main-drop rule by energy band, the seeded uniform pick,
// and the shared fixture both twins run (app/music_brain/render/entry_lines.py).
const assert = require("assert");
const path = require("path");
const ap = require("../../ui/static/autopilot.js");
const fx = require(path.join(__dirname, "../fixtures/entry_line_cases.json"));
let n = 0;

// ---- fixture parity (the Python twin runs the same file) ----
// a case's drums / low: a {t: level} map, or measured bar arrays (drumBars / lowBars) on its grid
const cb = (o) => {
  const lvl = (m, arr) => arr ? (t) => ap.lowLevelAt(arr, o.anchor, o.bar, t) : m ? (t) => (m[String(t)] == null ? null : m[String(t)]) : null;
  return Object.assign({}, o, { drums: lvl(o.drums, o.drumBars), low: lvl(o.low, o.lowBars) });
};
for (const k of fx.lineCases) { assert.deepStrictEqual(ap.entryLines(cb(k.o)), k.expect, k.name); n++; }
// the owner's pair on its measured levels: Nocturnal never enters in its quiet intro (3.78), it
// enters on 18.79 (the beat lands on 33.81 as A leaves), with stems and without
for (const nm of ["Nocturnal with stems (drum bars)", "Nocturnal without stems (mix low band)"]) {
  const k = fx.lineCases.find((x) => x.name === nm), r = ap.entryLines(cb(k.o));
  assert.ok(!r.lines.some((t) => Math.abs(t - 3.785) < 0.01), nm);
  assert.ok(r.lines.some((t) => Math.abs(t - 18.785) < 0.01), nm);
  assert.ok(/no beat/.test(r.rejected.find((x) => Math.abs(x.t - 3.785) < 0.01).why)); n++;
}
// without stems the drum-free intro is caught by the mix's low band, not the energy curve
{
  const k = fx.lineCases.find((x) => x.name === "Nocturnal without stems (mix low band)");
  assert.ok(ap.lowLevelAt(k.o.lowBars, k.o.anchor, k.o.bar, 3.785) < ap.ENTRY_LOW_MIN);
  assert.ok(ap.lowLevelAt(k.o.lowBars, k.o.anchor, k.o.bar, 33.808) >= ap.ENTRY_LOW_MIN); n++;
}
// the low band on a synthetic float32 signal (the Python twin rebuilds it)
{
  const k = fx.lowCases[0], len = k.sr * k.secs, s1 = new Float32Array(len), s2 = new Float32Array(len);
  for (let i = 0; i < len; i++) {
    const b = Math.floor(i / (k.sr * 0.5)), amp = [0.05, 0.05, 0.8, 1, 0.8, 1][b % 6];
    s1[i] = amp * Math.sin(2 * Math.PI * 60 * i / k.sr) + 0.3 * Math.sin(2 * Math.PI * 3000 * i / k.sr); s2[i] = 0.5 * s1[i];
  }
  const got = ap.lowBandBars([s1, s2], k.sr, k.anchor, k.bar);
  assert.strictEqual(got.length, k.expect.length);
  got.forEach((x, i) => assert.ok(Math.abs(x - k.expect[i]) < 1e-9));
  assert.ok(got[0] < 0.1 * got[3], "quiet bars low, loud bars high; the 3 kHz tone is filtered out"); n++;
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
for (let t = 0; t < 190; t++) { tt.push(t); cv.push(t < 30 ? 0.1 : t >= 90 && t < 105 ? 0.95 : 0.6); }
// no audio decoded: the energy curve (two quiet intro phrases, the beat from 30)
const o = { lines, include: 0, energyTimes: tt, energyCurve: cv, vocals: [], drops: [{ t: 90, energy: 0.95 }], bar, end: 190, roomS: 60 };
// never inside a quiet intro, even when it is the console's own line; its last phrase (the beat
// lands on the handover) is kept
const rel = ap.entryLines(Object.assign({ band: "relaxed" }, o));
assert.ok(!rel.lines.includes(0) && rel.lines.includes(15)); assert.ok(/no beat/.test(rel.rejected.find((r) => r.t === 0).why)); n++;
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
// with stems: the drum stem decides (a drumless break mid-song is refused, a loud energy curve does
// not rescue it); drums win over the low band, the low band over the energy curve
const dr = ap.entryLines(Object.assign({ band: "high", drums: (t) => (t === 60 || t === 75 ? 0.1 : 1) }, o));
assert.ok(/no beat/.test(dr.rejected.find((r) => r.t === 60).why) && dr.lines.includes(75)); n++;
assert.ok(ap.entryLines(Object.assign({ band: "high", drums: () => 1, low: () => 0 }, o)).lines.includes(0)); n++;
// without stems: the mix low band decides
const lo = ap.entryLines(Object.assign({ band: "high", low: (t) => (t < 30 ? 0.3 : 1.1) }, o));
assert.ok(!lo.lines.includes(0) && lo.lines.includes(15) && lo.lines.includes(30)); n++;

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
