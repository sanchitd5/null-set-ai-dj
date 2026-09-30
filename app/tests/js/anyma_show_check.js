// Node check for the SHOW mode core (app/ui/static/anyma-show.js). Run by test_anyma_show.py.
const assert = require("assert");
const C = require("../../ui/static/anyma-show.js");

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
  assert.strictEqual(C.drives(null).intensity, 0.55);   // idle scene reads on a normal display
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

// ---- camera: every scene / shot is finite, drift stays bounded over a long hold
{
  const o = new Float64Array(6);
  for (const s of [...C.SCENES, "nope"]) for (let shot = -1; shot < 6; shot++) for (const t of [0, 5, 60, 600, NaN]) {
    C.cameraPose(s, shot, t, false, o);
    assert.ok(o.every(Number.isFinite), `${s} ${shot} ${t}`);
    const d = Math.hypot(o[0] - o[3], o[1] - o[4], o[2] - o[5]);
    assert.ok(d > 1 && d < 16, `${s} dist ${d}`);
  }
  const a = C.cameraPose("head", 0, 0, false, new Float64Array(6)), b = C.cameraPose("head", 0, 600, false, new Float64Array(6));
  assert.ok(Math.hypot(a[0] - b[0], a[2] - b[2]) < 3, "head camera wanders");
  // the corridor camera stays in the tunnel mouth (frames are 2.4 x 1.6)
  for (let shot = 0; shot < 4; shot++) for (const t of [0, 30, 300]) {
    const c = C.cameraPose("corridor", shot, t, false, o);
    assert.ok(Math.abs(c[0]) < 2.4 && Math.abs(c[1]) < 1.6, `corridor eye ${c[0]} ${c[1]}`);
  }
}

// ---- geometry: four scenes, finite, known kinds, sane budgets
{
  const g = C.buildScenes(0x5eed), g2 = C.buildScenes(0x5eed);
  assert.deepStrictEqual(Object.keys(g).sort(), [...C.SCENES].sort());
  for (const s of C.SCENES) {
    const { points, pointCount, lines, lineCount } = g[s];
    assert.ok(pointCount > 3000 && pointCount < 20000, `${s} points ${pointCount}`);
    assert.strictEqual(points.length, pointCount * C.REC);
    assert.strictEqual(lineCount % 2, 0);
    assert.ok(lineCount < 20000, `${s} line verts ${lineCount}`);
    for (const arr of [points, lines]) for (let i = 0; i < arr.length; i++) assert.ok(Number.isFinite(arr[i]));
    for (let i = 3; i < points.length; i += C.REC) assert.ok([0, 1, 2, 3, 4, 5, 6, 7].includes(points[i]));
    // a segment keeps one rand (a dissolve moves it whole)
    for (let i = 0; i < lines.length; i += 2 * C.REC) assert.strictEqual(lines[i + 4], lines[i + C.REC + 4]);
    assert.deepStrictEqual(points, g2[s].points);                       // deterministic
  }
  // shuffled: the first quarter of the head already has eyes in it
  let eyes = 0; const hp = g.head.points;
  for (let i = 0; i < g.head.pointCount / 4; i++) if (hp[i * C.REC + 3] === 1) eyes++;
  assert.ok(eyes > 60, `eyes in the first quarter ${eyes}`);
  // the figure has both arms
  const kinds = new Set(); for (let i = 3; i < g.figure.points.length; i += C.REC) kinds.add(g.figure.points[i]);
  assert.ok(kinds.has(2) && kinds.has(3));
}

// ---- on-air deck: the louder one, with hysteresis
{
  assert.strictEqual(C.onAirDeck(null, 0, 0), null);
  assert.strictEqual(C.onAirDeck(null, 1, 0), "a");
  assert.strictEqual(C.onAirDeck(null, 0.3, 0.8), "b");
  assert.strictEqual(C.onAirDeck("a", 0.6, 0.7), "a");                 // small lead: no flip
  assert.strictEqual(C.onAirDeck("a", 0.4, 0.9), "b");
  assert.strictEqual(C.onAirDeck("b", 1, 0.01), "a");                 // on-air deck went silent
  assert.strictEqual(C.onAirDeck("a", NaN, 0.5), "b");
  assert.strictEqual(C.onAirDeck("x", 0.5, 0.2), "a");
}

// ---- Anyma drop detector: a long dip / build then a hard jump on a phrase line
const grid120 = (len) => {
  const b = [], d = [], p = [], t = [];
  for (let x = 0; x < len; x += 0.5) b.push(x);
  for (let x = 0; x < len; x += 2) d.push(x);
  for (let x = 0; x < len; x += 16) p.push(x);
  for (let x = 0; x < len; x++) t.push(x);
  return { bpm: 120, beat_times: b, downbeat_times: d, phrase_boundaries_8bar: p, energy_times: t };
};
const trackOf = (len, fn, extra) => C.prepTrack(Object.assign(grid120(len), { energy_curve: grid120(len).energy_times.map(fn) }, extra || {}));
{
  // breakdown 64-128 (32 bars low) -> drop at 128
  const brk = trackOf(320, (t) => (t < 64 ? 0.6 : t < 128 ? 0.15 : t < 192 ? 1 : 0.6));
  const dr = C.anymaDrops(brk, "");
  assert.strictEqual(dr.length, 1, JSON.stringify(dr));
  assert.strictEqual(dr[0].at, 128);
  assert.ok(dr[0].jump > 0.9 && dr[0].low === 16 && dr[0].hint === "");
  assert.ok(/jump .* energy shape only/.test(C.dropEvidence(dr[0])));
  // breakdown then a rising build (tension) -> drop at 128
  const bld = trackOf(320, (t) => (t < 64 ? 0.6 : t < 112 ? 0.15 : t < 128 ? 0.15 + 0.3 * (t - 112) / 16 : t < 192 ? 1 : 0.6));
  assert.deepStrictEqual(C.anymaDrops(bld, "").map((d) => d.at), [128]);
  // flat, flat with noise, a gradual rise: nothing
  assert.deepStrictEqual(C.anymaDrops(trackOf(320, () => 0.6), ""), []);
  assert.deepStrictEqual(C.anymaDrops(trackOf(320, (t) => 0.6 + 0.01 * Math.sin(t * 1.7)), ""), []);
  assert.deepStrictEqual(C.anymaDrops(trackOf(320, (t) => 0.1 + 0.9 * t / 320), ""), []);
  assert.deepStrictEqual(C.anymaDrops(trackOf(320, (t) => 0.1 + 0.9 * t / 320), "anyma"), [], "a rise is no drop, hint or not");
  // the EDM build -> drop above (already loud before the line) is not an Anyma drop
  assert.deepStrictEqual(C.anymaDrops(pt, ""), []);
  // a softer drop (the build ends fairly loud) only counts with a genre / artist hint
  const soft = trackOf(320, (t) => (t < 64 ? 0.6 : t < 112 ? 0.2 : t < 128 ? 0.2 + 0.65 * (t - 112) / 16 : t < 192 ? 1 : 0.6));
  assert.deepStrictEqual(C.anymaDrops(soft, ""), []);
  const sh = C.anymaDrops(soft, "anyma");
  assert.deepStrictEqual(sh.map((d) => d.at), [128]);
  assert.ok(/hint anyma/.test(C.dropEvidence(sh[0])));
  // hints
  assert.strictEqual(C.anymaHint("Anyma & Rebūke - Syren"), "anyma");
  assert.strictEqual(C.anymaHint("REBŪKE - Along Came Polly"), "rebuke");
  assert.strictEqual(C.anymaHint("Tale Of Us - Nova"), "tale of us");
  assert.strictEqual(C.anymaHint("genre: Melodic Techno"), "melodic techno");
  assert.strictEqual(C.anymaHint("Taylor Swift - Style"), "");
  assert.strictEqual(C.anymaHint(null), "");
  // crossing the line: fires once, a seek / backwards never
  assert.strictEqual(C.dropCrossed(dr, 127.98, 128.01).at, 128);
  assert.strictEqual(C.dropCrossed(dr, 128.01, 128.03), null);
  assert.strictEqual(C.dropCrossed(dr, 100, 128.5), null, "seek over the drop");
  assert.strictEqual(C.dropCrossed(dr, 128.5, 127.9), null);
  assert.strictEqual(C.dropCrossed(dr, NaN, 128), null);
}

// ---- DANCE: poses land on beat and bar times
{
  const brk = trackOf(320, (t) => (t < 64 ? 0.6 : t < 128 ? 0.15 : t < 192 ? 1 : 0.6));
  const at = (t, red) => C.dancePose(C.musicState(brk, t, 120), { weight: 0.7, eye: 0.4 }, !!red);
  const down = at(128);                               // a bar downbeat (and a beat)
  assert.ok(Math.abs(down.hit - 1) < 1e-9 && Math.abs(Math.abs(down.sway) - 1) < 1e-9 && Math.abs(down.pose - 1) < 1e-9);
  assert.strictEqual(down.nod, 0, "no nod on beat 1");
  const two = at(128.5);                              // beat 2: the backbeat
  assert.ok(Math.abs(two.nod - 1) < 1e-9 && two.hit > 0.99 && two.pose < 0.1);
  assert.ok(Math.sign(two.sway) === -Math.sign(down.sway), "sway alternates beat to beat");
  assert.strictEqual(at(129).nod, 0, "beat 3: no nod");
  assert.ok(Math.abs(at(129.5).nod - 1) < 1e-9, "beat 4: nod");
  const mid = at(128.25);                             // between beats: arms and sway at rest
  assert.ok(mid.hit < 0.02 && Math.abs(mid.sway) < 1e-9);
  assert.strictEqual(at(128).poseIdx, 0); assert.strictEqual(at(130).poseIdx, 1); assert.strictEqual(at(136).poseIdx, 0);
  assert.ok(at(130).pose > 0.99, "each bar downbeat: a big pose");
  assert.ok(Math.abs(down.amp - 1) < 0.01 && at(100).amp < 0.5, "amplitude follows energy");
  assert.ok(at(128, true).amp < 0.35, "reduced motion: small moves");
  assert.strictEqual(down.weight, 0.7); assert.strictEqual(down.eye, 0.4);
  assert.strictEqual(C.dancePose(C.musicStateNew(), null, false).amp, 0);
  assert.strictEqual(C.POSES.length, 4);
}

// ---- director: an Anyma drop cuts to the figure, which dances 16-32 bars
{
  const run = (track, until, hook) => {
    const d = C.createDirector(3), m = C.musicStateNew();
    for (let t = 100; t <= until; t += 1 / 30) {
      C.musicState(track, t, 120, m);
      if (Math.abs(t - 128) < 1 / 60) C.queueEvent(d, { type: "anyma", at: t });
      C.stepDirector(d, m, t, 1 / 30, false);
      if (hook) hook(d, t);
    }
    return d;
  };
  const brk = trackOf(320, (t) => (t < 64 ? 0.6 : t < 128 ? 0.15 : t < 192 ? 1 : 0.6));
  let seen = "", sceneOk = true, flashes = 0, lastF = 0;
  run(brk, 200, (d, t) => {
    if (d.last === "anyma") seen = d.scene;
    if (t > 129 && t < 191 && (d.scene !== "figure" || d.dance < 0.5)) sceneOk = false;
    if (d.flash > lastF + 0.5) flashes++;
    lastF = d.flash;
  });
  assert.strictEqual(seen, "figure");
  assert.ok(sceneOk, "the figure dances for the whole drop, no scene change");
  assert.ok(flashes <= 2, `flash cap holds (${flashes})`);
  const end = run(brk, 200);
  assert.ok(end.dance < 0.5 && end.danceUntil <= 192.01, "32 bars max");
  // a calm / breakdown phrase after 16 bars ends the dance there
  const calm = C.prepTrack(Object.assign(grid120(320), { energy_curve: grid120(320).energy_times.map((t) => (t < 128 ? 0.15 : t < 160 ? 1 : 0.2)),
    sections: [{ label: "breakdown", start: 64, end: 128 }, { label: "drop", start: 128, end: 160 }, { label: "breakdown", start: 160, end: 320 }] }));
  const c = run(calm, 170);
  assert.ok(Math.abs(c.danceUntil - 160) < 0.1, `stops on the calm line at 16 bars (${c.danceUntil})`);
  // no dance without the event
  const plain = C.createDirector(3), m = C.musicStateNew();
  for (let t = 100; t <= 150; t += 1 / 30) { C.musicState(brk, t, 120, m); C.stepDirector(plain, m, t, 1 / 30, false); }
  assert.strictEqual(plain.dance, 0);
}

// ---- SHOW AUTO: the AI sizes the stage on phrase lines
{
  // 120 BPM: bar 2 s, phrase 16 s. cls / energy per phrase from `shape`.
  const M = (t, cls, energy) => ({ ok: true, beat: 0.5, phraseIdx: Math.floor(t / 16), phrasePhase: (t % 16) / 16,
    cls: cls || "groove", energy: energy == null ? 0.6 : energy });
  let kept = [];
  const sim = (from, to, fn) => {
    const a = C.autoNew(), log = [];
    kept = [];
    let mode = "embed";
    for (let t = from; t <= to + 1e-9; t = Math.round((t + 0.25) * 100) / 100) {
      const inp = Object.assign({ on: true, driving: true, mode, ms: M(t), moment: null }, fn ? fn(t) : {});
      const r = C.autoStep(a, inp, t);
      if (r && r.kept) { kept.push({ t, why: r.why, ev: r.evidence }); continue; }
      if (r) { mode = r.mode; log.push({ t, mode: r.mode, why: r.why, ev: r.evidence, phase: inp.ms.phrasePhase }); }
    }
    return log;
  };
  // set start: first phrase line after the AI starts driving -> full, held 32 bars, calm -> window
  const s1 = sim(5, 200, (t) => ({ ms: M(t, t >= 32 ? "calm" : "groove", 0.2) }));
  assert.deepStrictEqual(s1.map((x) => [x.t, x.mode, x.why]), [[16, "full", "set start"], [80, "embed", "calm section"]]);
  // a drop on a line in window mode: full; a low breakdown later: window
  const s2 = sim(0, 300, (t) => ({ moment: t === 144 ? { kind: "drop" } : null,
    ms: M(t, t >= 240 ? "breakdown" : t >= 64 && t < 144 ? "calm" : "groove", t >= 240 ? 0.2 : 0.7) }));
  assert.deepStrictEqual(s2.map((x) => [x.t, x.mode, x.why]),
    [[0, "full", "set start"], [64, "embed", "calm section"], [144, "full", "drop"], [240, "embed", "low-energy breakdown"]]);
  // a moment mid-phrase waits for the next line; its evidence reaches the log
  const s3 = sim(0, 200, (t) => ({ moment: t === 140 ? { kind: "anyma drop", evidence: "jump 0.9" } : null,
    ms: M(t, t >= 64 && t < 136 ? "calm" : "groove") }));
  assert.deepStrictEqual(s3.slice(2).map((x) => [x.t, x.mode, x.why, x.ev]), [[144, "full", "anyma drop", "jump 0.9"]]);
  // an Anyma drop while already on the stage: no switch, but a log line with the evidence
  const s3b = sim(0, 100, (t) => ({ moment: t === 48 ? { kind: "anyma drop", evidence: "jump 1" } : null }));
  assert.deepStrictEqual(s3b.map((x) => x.mode), ["full"]);
  assert.deepStrictEqual(kept, [{ t: 48, why: "anyma drop (already full)", ev: "jump 1" }]);
  // a high breakdown (energy 0.6) keeps the stage
  const s4 = sim(0, 200, (t) => ({ ms: M(t, "breakdown", 0.6) }));
  assert.deepStrictEqual(s4.map((x) => x.mode), ["full"]);
  // no flapping: a moment on every line and calm every other phrase
  const s5 = sim(0, 2000, (t) => ({ moment: t % 16 === 0 && (t / 16) % 3 === 0 ? { kind: t % 96 === 0 ? "supermove" : "peak move" } : null,
    ms: M(t, Math.floor(t / 16) % 2 ? "calm" : "groove") }));
  assert.ok(s5.length >= 4, `it does switch (${s5.length})`);
  for (let i = 0; i < s5.length; i++) {
    assert.ok(s5[i].phase * 8 < 1, `switch at ${s5[i].t} is on a phrase line`);
    if (i) {
      const gap = s5[i].t - s5[i - 1].t;
      assert.ok(gap >= 32 - 0.05, `16-bar minimum (${s5[i - 1].t} -> ${s5[i].t})`);
      if (s5[i].why !== "supermove") assert.ok(gap >= 64 - 0.05, `one switch per 32 bars (${s5[i - 1].t} -> ${s5[i].t})`);
      assert.notStrictEqual(s5[i].mode, s5[i - 1].mode);
    }
  }
  // a supermove may break the 32-bar rule, never the 16-bar one
  const s6 = sim(0, 200, (t) => ({ moment: t === 96 ? { kind: "supermove" } : null, ms: M(t, t >= 32 && t < 90 ? "calm" : "groove") }));
  assert.deepStrictEqual(s6.map((x) => [x.t, x.mode, x.why]), [[0, "full", "set start"], [64, "embed", "calm section"], [96, "full", "supermove"]]);
  // user input on the decks: window now (mid-phrase), paused 2 minutes, then back to normal
  const s7 = sim(0, 400, (t) => ({ user: t === 20.5, moment: t === 48 || t === 176 ? { kind: "drop" } : null }));
  assert.deepStrictEqual(s7.map((x) => [x.t, x.mode, x.why]),
    [[0, "full", "set start"], [20.5, "embed", "user input, auto paused 2 min"], [176, "full", "drop"]]);
  // Esc: same, with its own reason
  const s8 = sim(0, 100, (t) => ({ esc: t === 30 }));
  assert.deepStrictEqual(s8.map((x) => [x.t, x.mode, x.why]), [[0, "full", "set start"], [30, "embed", "esc, auto paused 2 min"]]);
  // the user picking the stage size: kept, auto paused
  const a9 = C.autoNew();
  assert.strictEqual(C.autoStep(a9, { on: true, driving: true, mode: "full", ms: M(0), manual: true }, 0), null);
  assert.strictEqual(C.autoStep(a9, { on: true, driving: true, mode: "full", ms: M(64, "calm"), moment: null }, 64), null);
  assert.strictEqual(a9.mode, "full");
  // the AI stops driving: window now; SHOW AUTO off or SHOW off: never acts
  const s10 = sim(0, 100, (t) => ({ driving: t < 40 }));
  assert.deepStrictEqual(s10.map((x) => [x.t, x.mode, x.why]), [[0, "full", "set start"], [40, "embed", "AI stopped driving"]]);
  assert.deepStrictEqual(sim(0, 300, (t) => ({ on: false, moment: { kind: "supermove" } })), []);
  assert.deepStrictEqual(sim(0, 300, (t) => ({ driving: false, moment: { kind: "drop" } })), []);

  // ---- anticipation: FULL on the phrase line BEFORE a booked / predicted moment
  const quiet = (t) => ({ ms: M(t, t >= 32 ? "calm" : "groove", 0.2) });
  const upc = (list) => (t) => Object.assign(quiet(t), { upcoming: list(t) });
  // a booked supermove 20 bars ahead (hit at 200, booked at 160): full at 176, one
  // phrase before the hit's phrase, held 16 bars past the hit, window on the next calm line
  const sm = C.bookedMoment("ai-supermove", { at: 200, name: "layer", deck: "b" });
  assert.deepStrictEqual(sm, { key: "sm:b:LAYER", kind: "LAYER", at: 200, deck: "b" });
  const a1 = sim(0, 300, upc((t) => (t >= 160 && t < 202 ? [sm] : [])));
  assert.deepStrictEqual(a1.map((x) => [x.t, x.mode]), [[0, "full"], [64, "embed"], [176, "full"], [240, "embed"]]);
  assert.strictEqual(a1[2].why, "ahead of LAYER at +24.0s (lead 12 bars)");
  assert.ok(a1[2].phase * 8 < 1, "the anticipation switch is on a phrase line");
  // the hit moved later (rescheduled, same key): the hold follows it
  const a1b = sim(0, 300, upc((t) => (t >= 160 && t < 226 ? [Object.assign({}, sm, { at: t < 170 ? 200 : 224 })] : [])));
  assert.deepStrictEqual(a1b.map((x) => [x.t, x.mode]), [[0, "full"], [64, "embed"], [208, "full"], [272, "embed"]]);
  // a cancelled cue: no stuck FULL, back to window after one phrase
  const a2 = sim(0, 300, upc((t) => (t >= 160 && t < 180 ? [sm] : [])));
  assert.deepStrictEqual(a2.map((x) => [x.t, x.mode, x.why]).slice(2),
    [[176, "full", "ahead of LAYER at +24.0s (lead 12 bars)"], [208, "embed", "moment cancelled"]]);
  // a plain crossfade is not a moment; a drop-move transition is
  assert.strictEqual(C.bookedMoment("ai-cue", { at: 200, kind: "transition", why: "long blend", deck: "a" }), null);
  assert.strictEqual(C.bookedMoment("ai-cue", { at: 200, kind: "transition", why: "Double Drop: both drops", deck: "a" }).kind, "DOUBLE DROP");
  assert.strictEqual(C.bookedMoment("ai-cue", { at: 200, kind: "drop", why: "after the merge", deck: "a" }).kind, "MERGE drop");
  assert.strictEqual(C.bookedMoment("vis-moment", { at: 200, tier: "accent", name: "x" }), null);
  assert.strictEqual(C.bookedMoment("vis-moment", { at: 200, tier: "super", name: "hook drop", deck: "a" }).key, "sm:a:HOOK DROP");
  assert.deepStrictEqual(C.eventTrigger("vis-moment", { at: 3, tier: "accent" }, 1, (x) => x), { type: "accent", at: 3 });
  const a3 = sim(0, 300, upc((t) => []));
  assert.deepStrictEqual(a3.map((x) => [x.t, x.mode]), [[0, "full"], [64, "embed"]], "a plain crossfade stays window");
  // an Anyma drop predicted from a synthetic build -> drop curve: full a phrase early
  const brk = trackOf(320, (t) => (t < 64 ? 0.6 : t < 128 ? 0.15 : t < 192 ? 1 : 0.6));
  const sms = C.songMoments(brk, C.anymaDrops(brk, ""));
  assert.deepStrictEqual(sms.map((m) => [m.t, m.kind]), [[128, "anyma drop"]]);
  const a4 = sim(0, 300, upc((t) => sms.filter((m) => m.t > t).map((m) => ({ key: m.key, kind: m.kind, at: m.t, evidence: m.evidence }))));
  assert.deepStrictEqual(a4.map((x) => [x.t, x.mode]), [[0, "full"], [64, "embed"], [112, "full"], [176, "embed"]]);
  assert.ok(/^ahead of anyma drop at \+16\.0s \(lead 8 bars\)$/.test(a4[2].why) && /jump/.test(a4[2].ev), a4[2].why);
  // a section edge into a drop with a big jump is a song moment too (no Anyma dip needed)
  const sec = trackOf(320, (t) => (t < 96 ? 0.4 : 0.95), { sections: [{ label: "verse", start: 0, end: 96 }, { label: "drop", start: 96, end: 320 }] });
  assert.ok(C.songMoments(sec, []).some((m) => m.t === 96 && m.kind === "drop"));
  assert.deepStrictEqual(C.songMoments(trackOf(320, () => 0.6, { sections: [{ label: "drop", start: 96, end: 320 }] }), []), []);
  // the 16-bar minimum still holds with nothing upcoming (s5 above) and a far moment does nothing
  const a5 = sim(0, 300, upc((t) => (t >= 150 ? [Object.assign({}, sm, { at: 290 })] : [])));
  assert.ok(!a5.some((x) => x.t >= 150 && x.t < 256 && x.mode === "full"), JSON.stringify(a5));
}

// ---- the sim never loads the show (paint only), and the console does
{
  const fs = require("fs"), path = require("path");
  const runSet = fs.readFileSync(path.join(__dirname, "../../sim/js/run-set.js"), "utf8");
  assert.ok(/skip = cfg\.skip \|\| \[[^\]]*"anyma-show\.js"/.test(runSet), "sim skips anyma-show.js");
  const html = fs.readFileSync(path.join(__dirname, "../../ui/static/index.html"), "utf8");
  assert.ok(html.includes('src="/anyma-show.js"') && html.includes('id="ap-show-toggle"') && html.includes('id="show-stage"'));
  assert.ok(!/id="ap-show-toggle"[^>]*checked/.test(html), "SHOW is off by default");
}

// ---- layout contract: the stage is baked into the console, no overlay window
{
  const fs = require("fs"), path = require("path");
  const js = fs.readFileSync(path.join(__dirname, "../../ui/static/anyma-show.js"), "utf8");
  const css = fs.readFileSync(path.join(__dirname, "../../ui/static/anyma-show.css"), "utf8");
  assert.ok(!/requestFullscreen|anyma-bar/.test(js), "no browser fullscreen, no floating control bar");
  const base = css.match(/\.anyma-stage \{([^}]*)\}/)[1];
  assert.ok(/position: absolute/.test(base) && /z-index: -1/.test(base) && /pointer-events: none/.test(base), "a background layer");
  assert.ok(!/\.anyma-bar|z-index: 87\d/.test(css), "no overlay z-index");
  // SHOW on alone: no embedded band, no translucent panels; only the manual STAGE (show-full)
  const glue = js.slice(js.indexOf("// ---- DOM / WebGL glue"));
  assert.ok(!/show-embed/.test(css) && !/show-embed|placeBand|"embed"/.test(glue), "no embed layer left");
  assert.ok(/body\.show-full/.test(css), "FULL is still a console layout state");
  assert.ok(/stage\.hidden = m !== "full"/.test(js), "the stage shows only in FULL");
}
{
  const fs = require("fs"), path = require("path");
  const js = fs.readFileSync(path.join(__dirname, "../../ui/static/anyma-show.js"), "utf8");
  assert.ok(!/"pip"/.test(js), "no PIP / floating-window mode left");
  assert.ok(/"IN CONSOLE"/.test(js), "stage button reads IN CONSOLE");
  assert.ok(/debug\(\)\s*\{/.test(js), "anymaShow.debug()");
  // SHOW AUTO never opens the stage: the glue has no auto path into setMode
  const glue = js.slice(js.indexOf("// ---- DOM / WebGL glue"));
  assert.ok(!/autoStep\(|autoSwitch\(/.test(glue), "SHOW AUTO does not size the stage any more");
  assert.deepStrictEqual((glue.match(/setMode\("full"\)|setMode\(mode === "full" \? "elements" : "full"\)/g) || []),
    ['setMode(mode === "full" ? "elements" : "full")'], "only the STAGE button opens FULL");
  assert.ok(/toggle\.checked \? \(mode === "full" \? "full" : "elements"\) : "off"/.test(glue), "SHOW on = elements");
  assert.ok(/sv\.addTick\(tick\)/.test(glue), "elements ride the shared visuals.js loop");
}
// manual play (driving true, no AI) with a booked moment still goes full ahead of it
{
  const a = C.autoNew();
  let hit = null;
  for (let t = 0; t <= 200; t = Math.round((t + 0.25) * 100) / 100) {
    const r = C.autoStep(a, { on: true, driving: true, mode: a.mode, ms: { ok: true, beat: 0.5, phraseIdx: Math.floor(t / 16), phrasePhase: (t % 16) / 16, cls: t < 150 ? "calm" : "groove", energy: 0.6 }, moment: null,
      upcoming: t >= 100 ? [{ key: "sm:a:X", kind: "X", at: 180 }] : [] }, t);
    if (r && r.mode === "full" && t > 100 && hit === null) hit = t;
  }
  assert.ok(hit !== null && hit <= 176, `manual play goes full ahead (${hit})`);
}
console.log("anyma show ok");
