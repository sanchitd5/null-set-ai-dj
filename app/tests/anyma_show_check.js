// Node check for the SHOW mode core (app/ui/static/anyma-show.js). Run by test_anyma_show.py.
const assert = require("assert");
const C = require("../ui/static/anyma-show.js");

// ---- a synthetic 120 BPM track: beat 0.5 s, bar 2 s, 8-bar phrase 16 s
const LEN = 256;
const beats = [], downs = [], phrases = [], energy = [], etimes = [];
for (let t = 0; t < LEN; t += 0.5) beats.push(t);
for (let t = 0; t < LEN; t += 2) downs.push(t);
for (let t = 0; t < LEN; t += 16) phrases.push(t);
const SECTIONS = [
  { label: "intro", start: 0, end: 32 }, { label: "build", start: 32, end: 64 }, { label: "drop", start: 64, end: 128 },
  { label: "breakdown", start: 128, end: 160 }, { label: "verse", start: 160, end: 256 }];
for (let t = 0; t < LEN; t++) { etimes.push(t); energy.push(t < 32 ? 0.2 : t < 64 ? 0.2 + (t - 32) / 50 : t < 128 ? 1 : t < 160 ? 0.3 : 0.6); }
const an = { bpm: 120, beat_times: beats, downbeat_times: downs, phrase_boundaries_8bar: phrases, sections: SECTIONS,
  energy_curve: energy, energy_times: etimes, vocal_active_regions: [[160, 200]] };
const pt = C.prepTrack(an);

// ---- music state
{
  const ms = C.musicState(pt, 65.25, 120);
  assert.strictEqual(ms.ok, true);
  assert.strictEqual(ms.cls, "drop");
  assert.strictEqual(ms.beatIdx, 130);
  assert.ok(Math.abs(ms.beatPhase - 0.5) < 1e-9);
  assert.strictEqual(ms.barIdx, 32);
  assert.strictEqual(ms.phraseIdx, 4);
  assert.strictEqual(ms.vocal, false);
  assert.ok(ms.energy > 0.99);
  const v = C.musicState(pt, 170, 120, ms);            // reuses `out`
  assert.strictEqual(v, ms);
  assert.strictEqual(v.vocal, true);
  assert.strictEqual(v.cls, "groove");
  assert.strictEqual(C.musicState(pt, 205, 120).vocal, false);
  const b = C.musicState(pt, 60, 120);
  assert.strictEqual(b.cls, "build");
  assert.ok(b.slope > 0.2, `build slope ${b.slope}`);
  assert.strictEqual(C.musicState(null, 10).ok, false);
  assert.strictEqual(C.musicState(pt, NaN).ok, false);
  // no grid at all: derived from bpm, never throws
  const bare = C.musicState(C.prepTrack({}), 9, 120);
  assert.strictEqual(bare.barIdx, 4);
  assert.strictEqual(bare.energy, 0.5);
  // vocal regions as objects
  assert.strictEqual(C.musicState(C.prepTrack({ vocal_active_regions: [{ start: 1, end: 3 }] }), 2, 120).vocal, true);
}

// ---- section class without labels: energy and slope decide
assert.strictEqual(C.sectionClass("", 0.9, 0), "drop");
assert.strictEqual(C.sectionClass("", 0.5, 0.2), "build");
assert.strictEqual(C.sectionClass("", 0.2, 0), "breakdown");
assert.strictEqual(C.sectionClass("", 0.5, 0), "groove");
assert.strictEqual(C.sectionClass("Chorus", 0.1, 0), "drop");
assert.strictEqual(C.sectionClass("outro", 0.9, 0), "calm");

// ---- scene choice
assert.strictEqual(C.pickScene("groove", true, "figure", false), "head");     // the face speaks
assert.strictEqual(C.pickScene("drop", true, "monolith", false), "figure");   // a drop beats the vocal
assert.strictEqual(C.pickScene("drop", false, "figure", true), "head");       // forced: always a new scene
assert.strictEqual(C.pickScene("build", false, "monolith", false), "monolith"); // second choice: keep it
assert.strictEqual(C.pickScene("breakdown", false, "figure", false), "monolith");
assert.strictEqual(C.pickScene("nonsense", false, "corridor", false), "figure");
for (const cls of Object.keys(C.PREF)) for (const s of C.SCENES) for (const v of [true, false])
  assert.ok(C.SCENES.includes(C.pickScene(cls, v, s, true)) && C.pickScene(cls, v, s, true) !== s);

// ---- trigger mapping from console events
{
  const toNow = (at) => at + 100;                       // audio clock 100 s behind
  assert.deepStrictEqual(C.eventTrigger("ai-supermove", { at: 5, name: "LAYER" }, 50, toNow), { type: "supermove", at: 105 });
  assert.deepStrictEqual(C.eventTrigger("ai-cue", { at: 7, kind: "drop" }, 50, toNow), { type: "drop", at: 107 });
  assert.deepStrictEqual(C.eventTrigger("ai-cue", { at: 7, kind: "transition" }, 50, toNow), { type: "transition", at: 107 });
  assert.deepStrictEqual(C.eventTrigger("ai-cue", { at: 7, kind: "line" }, 50, toNow), { type: "accent", at: 107 });
  assert.deepStrictEqual(C.eventTrigger("ai-activity", { kind: "learned_move" }, 50, toNow), { type: "accent", at: 50 });
  assert.deepStrictEqual(C.eventTrigger("ai-activity", { kind: "artist_move" }, 50, toNow), { type: "accent", at: 50 });
  assert.strictEqual(C.eventTrigger("ai-activity", { kind: "decision" }, 50, toNow), null);
  assert.strictEqual(C.eventTrigger("ai-cue", { kind: "nope" }, 50, toNow), null);
  assert.deepStrictEqual(C.eventTrigger("ai-cue", { kind: "drop" }, 50, toNow), { type: "drop", at: 50 });  // no time: now
  assert.deepStrictEqual(C.eventTrigger("ai-supermove", null, 50, null), { type: "supermove", at: 50 });
  assert.strictEqual(C.eventTrigger("click", {}, 50, toNow), null);
}

// ---- the director over the whole synthetic song (60 fps, perf clock = song clock)
function play(from, to, opts = {}) {
  const d = opts.d || C.createDirector(7), ms = C.musicStateNew(), log = [];
  const dt = 1 / 60;
  for (let t = from; t < to; t += dt) {
    for (const ev of opts.events || []) if (!ev.q && t >= ev.post) { C.queueEvent(d, ev.ev); ev.q = true; }
    C.musicState(pt, t, 120, ms);
    const scene = d.scene;
    C.stepDirector(d, ms, t, dt, !!opts.reduced);
    if (d.last) log.push({ t: +t.toFixed(3), what: d.last, scene: d.scene, from: scene, flash: d.flash });
  }
  return { d, log };
}
{
  const { d, log } = play(0, 250);
  const drops = log.filter((e) => e.what === "drop");
  assert.strictEqual(drops.length, 1, JSON.stringify(log));
  assert.ok(Math.abs(drops[0].t - 64) < 0.05, `drop at ${drops[0].t}`);
  assert.strictEqual(drops[0].scene, "figure");                       // hard cut to the giant figure
  assert.ok(drops[0].flash > 0.9);
  // 2 bars of fast cuts after the drop, one every 2 beats, camera only
  const burst = log.filter((e) => e.what === "cut");
  assert.deepStrictEqual(burst.map((e) => Math.round(e.t)), [65, 66, 67], JSON.stringify(burst));
  assert.ok(burst.every((e) => e.scene === "figure" && e.flash < 0.9));
  // scene changes land on phrase boundaries only, and hold >= 16 bars
  const diss = log.filter((e) => e.what === "dissolve");
  for (const e of diss) assert.ok(Math.abs(e.t / 16 - Math.round(e.t / 16)) < 0.01, `off-phrase change at ${e.t}`);
  const changes = log.filter((e) => e.scene !== e.from).map((e) => e.t);
  for (let i = 1; i < changes.length; i++) assert.ok(changes[i] - changes[i - 1] >= 32 - 0.05, `held ${changes[i] - changes[i - 1]} s`);
  assert.ok(diss.some((e) => Math.abs(e.t - 128) < 0.05 && e.scene === "monolith"), "breakdown -> monolith");
  assert.ok(diss.some((e) => Math.abs(e.t - 160) < 0.05 && e.scene === "head"), "vocal -> head");
  assert.ok(diss.some((e) => Math.abs(e.t - 208) < 0.05 && e.scene === "figure"), "vocal over -> figure on the next phrase");
  assert.strictEqual(d.scene, "figure");
  assert.ok(d.flash < 0.01 && d.red === 0);
}
// the dissolve runs one bar and then settles
{
  // start >= 16 bars before the 128 s phrase, so the hold lets it change there
  const { d } = play(90, 128.5);
  assert.ok(d.prev === "figure" || d.prev === "corridor", `prev ${d.prev}`);
  assert.ok(d.mix > 0.2 && d.mix < 0.3, `mix ${d.mix}`);
  const { d: d2 } = play(90, 131);
  assert.strictEqual(d2.prev, null);
  assert.strictEqual(d2.mix, 1);
}

// ---- supermove: booked ahead, fires on time; cut + flash + red + new scene
{
  const events = [{ post: 170, ev: { type: "supermove", at: 172 } }];
  const { d, log } = play(165, 175, { events });
  const sm = log.filter((e) => e.what === "supermove");
  assert.strictEqual(sm.length, 1);
  assert.ok(Math.abs(sm[0].t - 172) < 0.02, `fired at ${sm[0].t}`);
  assert.notStrictEqual(sm[0].scene, sm[0].from);
  assert.ok(sm[0].flash > 0.9);
  assert.ok(d.red > 0.01);
  assert.ok(log.filter((e) => e.what === "cut").length >= 2);          // the burst follows
}
// the flash cap: one per second, however many moments ask
{
  const d = C.createDirector(1), ms = C.musicState(pt, 70, 120);
  let flashes = 0, last = 0;
  for (let i = 0; i < 400; i++) {
    const now = 1000 + i / 60;
    if (i % 20 === 0) C.queueEvent(d, { type: "supermove", at: now });
    C.stepDirector(d, ms, now, 1 / 60, false);
    if (d.flash > last + 0.5) flashes++;
    last = d.flash;
  }
  assert.ok(flashes <= Math.ceil(400 / 60 / C.FLASH_GAP_S) + 1, `flashes ${flashes}`);
}
// a cue-booked drop and the section edge right after it are one drop
{
  const events = [{ post: 60, ev: { type: "drop", at: 63.2 } }];
  const { log } = play(58, 70, { events });
  assert.strictEqual(log.filter((e) => e.what === "drop").length, 1, JSON.stringify(log));
}
// reduced motion: no flash, no burst, the drop dissolves instead of cutting
{
  const { d, log } = play(60, 70, { reduced: true });
  const drop = log.find((e) => e.what === "drop");
  assert.ok(drop && drop.scene === "figure");
  assert.strictEqual(drop.flash, 0);
  assert.strictEqual(log.filter((e) => e.what === "cut").length, 0);
  assert.ok(d.glitch === 0);
}
// track transition: a long dissolve (8 bars, 4..16 s)
{
  const d = C.createDirector(3), ms = C.musicState(pt, 170, 120);
  C.stepDirector(d, ms, 10, 1 / 60, false);
  C.queueEvent(d, { type: "transition", at: 10.5 });
  for (let t = 10; t < 11; t += 1 / 60) C.stepDirector(d, C.musicState(pt, 170 + t - 10, 120), t, 1 / 60, false);
  assert.ok(d.prev && d.mixDur === 16 && d.mix < 0.1, `mixDur ${d.mixDur}`);
}
// accents never change the scene
{
  const d = C.createDirector(3), ms = C.musicState(pt, 100, 120);
  C.stepDirector(d, ms, 5, 1 / 60, false);
  const s = d.scene;
  C.queueEvent(d, { type: "accent", at: 5.1 });
  C.stepDirector(d, C.musicState(pt, 100.2, 120), 5.2, 0.2, false);
  assert.strictEqual(d.last, "accent");
  assert.strictEqual(d.scene, s);
  assert.ok(d.accent > 0.5);
}
// a seek never fires a drop or a phrase change
{
  const d = C.createDirector(9);
  C.stepDirector(d, C.musicState(pt, 20, 120), 1, 1 / 60, false);
  C.stepDirector(d, C.musicState(pt, 90, 120), 1.02, 1 / 60, false);   // jumped into the drop
  assert.strictEqual(d.last, "");
  C.stepDirector(d, C.musicState(pt, 90.02, 120), 1.04, 1 / 60, false);
  assert.strictEqual(d.last, "");
}
// stale events (tab was hidden) are dropped; bad input never throws
{
  const d = C.createDirector(0);
  C.queueEvent(d, { type: "drop", at: 1 });
  C.queueEvent(d, { type: "drop", at: NaN });
  C.queueEvent(d, null);
  assert.strictEqual(d.queue.length, 1);
  C.stepDirector(d, null, 10, 1 / 60, false);
  assert.strictEqual(d.last, "");
  assert.strictEqual(d.queue.length, 0);
  C.stepDirector(d, { ok: false }, NaN, NaN, false);
  for (let i = 0; i < 40; i++) C.queueEvent(d, { type: "accent", at: 99 });
  assert.strictEqual(d.queue.length, 16);
}

// ---- stems and drives
{
  const st = C.stemsNew();
  for (let i = 0; i < 120; i++) C.stemsStep(st, { drums: 0.5, bass: 0.2, vocals: 0, other: 0.1 }, 1 / 60);
  assert.ok(st.level.drums > 0.95 && st.level.bass > 0.9 && st.level.vocals === 0);
  for (let i = 0; i < 30; i++) C.stemsStep(st, { drums: 0.05, bass: 0.2, vocals: 0, other: 0.1 }, 1 / 60);
  assert.ok(st.level.drums < 0.3, `drums release ${st.level.drums}`);
  const ms = C.musicState(pt, 170, 120);
  const dr = C.drives(ms, st);
  assert.ok(dr.weight > 0.9 && dr.eye === 0 && dr.pulse > 0 && dr.pulse <= 1);
  const fb = C.drives(ms, C.stemsNew());                 // no stems: grid + energy + vocal regions
  assert.strictEqual(fb.eye, 0.8);
  assert.ok(Math.abs(fb.pulse - (0.3 + 0.7 * ms.energy)) < 1e-9);  // on the beat
  assert.strictEqual(C.drives(null).intensity, 0.35);
  C.stemsStep(st, null, 1 / 60);
  assert.strictEqual(st.live, false);
  C.stemsStep(st, { drums: NaN, bass: -1 }, NaN);
  for (const n of ["drums", "bass", "vocals", "other"]) assert.ok(Number.isFinite(st.level[n]));
}

// ---- adaptive quality
{
  const q = C.qualityNew(), T = 1000 / 60;
  let steps = 0;
  for (let i = 0; i < C.DOWN_FRAMES - 1; i++) steps += C.qualityStep(q, 8, T, T);
  assert.strictEqual(q.level, 0);                          // not on a blip
  steps += C.qualityStep(q, 8, T, T);
  assert.strictEqual(q.level, 1);
  for (let i = 0; i < 200; i++) C.qualityStep(q, 8, T, T);
  assert.ok(q.level >= 2);
  const l = q.level;
  for (let i = 0; i < 500; i++) C.qualityStep(q, 1, T, T);
  assert.strictEqual(q.level, l);                          // up is slow
  for (let i = 0; i < 20000 && q.level === l; i++) C.qualityStep(q, 1, T, T);
  assert.strictEqual(q.level, l - 1);
  // a long frame gap (jank) also steps down; a huge gap (tab switch) is ignored
  const g = C.qualityNew();
  for (let i = 0; i < 100; i++) C.qualityStep(g, 1, 40, T);
  assert.ok(g.level >= 1);
  const h = C.qualityNew();
  for (let i = 0; i < 100; i++) C.qualityStep(h, 1, 5000, T);
  assert.strictEqual(h.level, 0);
  assert.strictEqual(C.qualityStep(h, NaN, T, T), false);
  // never past the lowest level
  const z = C.qualityNew();
  for (let i = 0; i < 5000; i++) C.qualityStep(z, 50, 100, T);
  assert.strictEqual(z.level, C.QUALITY.length - 1);
  // step-down doubles the wait to step back up (no flapping)
  const f = C.qualityNew();
  for (let i = 0; i < 60; i++) C.qualityStep(f, 8, T, T);
  assert.strictEqual(f.upFrames, C.UP_FRAMES * 2);
}

console.log("anyma show ok");
