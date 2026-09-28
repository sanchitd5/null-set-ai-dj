// Node check: a song merge never leaves the master silent (session 2026-09-28_220516,
// 22:11:02, Yotto -> Ben Bohmer, deck b -> deck a: "the master just went silent").
//   1. audibleRms: what a speaker without a sub plays of a stem.
//   2. The incident plan, from the measured stems (merge_silence_fixture.json):
//      the old gate (full-band levelCheck) booked it, the audible band has a 1.9 s
//      hole (B's bare sub kick in A's break); the booking now refuses / repairs it.
//   3. deck-controller stemMix == stem-moves gainsAt for every merge plan (14
//      combos, keys clashing or not, both decks), including two moves on one bar
//      (A's synths over 8 bars + its voice over 2 used to cut the 8-bar fade).
//   4. The crossfader both ways (out of deck a, out of deck b) is the checked curve.
//   5. Every combo, both fixtures, keys clashing or not: what gets booked is audible,
//      one bass and one singer.
// Run by test_keylock.py.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const sm = require("../ui/static/stem-moves.js");
const FX = require("./merge_silence_fixture.json");
const STEMS = ["drums", "bass", "vocals", "other"];

// ---- 1. audibleRms ---------------------------------------------------------------
{
  const sr = 44100, n = sr * 2;
  const sine = (hz, amp = 1) => Float32Array.from({ length: n }, (_, i) => amp * Math.sin((2 * Math.PI * hz * i) / sr));
  const rms = Math.SQRT1_2;
  assert.ok(sm.audibleRms(sine(1000), 0, n, sr) > 0.95 * rms, "1 kHz passes");
  assert.ok(sm.audibleRms(sine(50), 0, n, sr) < 0.02 * rms, "a 50 Hz sub is (almost) not there");
  assert.ok(sm.audibleRms(sine(90), 0, n, sr) < 0.2 * rms, "90 Hz mostly not there");
  assert.strictEqual(sm.audibleRms(new Float32Array(n), 0, n, sr), 0);
  assert.strictEqual(sm.audibleRms(sine(1000), 500, 500, sr), 0, "empty range");
  assert.ok(sm.audibleRms(sine(1000), 0, 300, sr) > 0.5 * rms, "a range shorter than a window still reads");
  console.log("audibleRms ok");
}

// ---- helpers ---------------------------------------------------------------------
const barS = (x) => 240 / x.out.bpm;                   // A's bar, audio seconds (A at its tempo)
const mean = (e, a, b) => Object.fromEntries(STEMS.map((s) => [s, e[s].slice(a, b).reduce((x, y) => x + y, 0) / (b - a)]));
// What mergeTransition runs on a plan: full-band floor, then the audible band (1 s).
function gates(x, plan, M) {
  const f = sm.mergeFader(M);
  const lv = sm.levelCheck({ events: plan.events, fader: f, dir: 1, span: plan.total, eOut: x.out.full, eIn: x.in.full });
  if (!lv.ok) return lv;
  return sm.masterAudibility({ events: plan.events, fader: f, dir: 1, span: plan.total, eOut: x.out.audible, eIn: x.in.audible, minRun: 1 / barS(x) });
}
const audible = (x, plan, M) => sm.masterAudibility({ events: plan.events, fader: sm.mergeFader(M), dir: 1, span: plan.total,
  eOut: x.out.audible, eIn: x.in.audible, minRun: 1 / barS(x) });
function book(x, M, combo, keyClash) {
  const ranked = sm.mergeRank({ eA: mean(x.out.full, 0, M), eB: mean(x.in.full, 0, M), keyScore: keyClash ? 0 : 1 });
  return sm.mergeBooking(M, { combo, label: sm.mergeLabel(combo) }, keyClash, ranked, (p) => gates(x, p, M));
}
function oneBassOneSinger(plan, label) {
  for (let t = 0; t <= plan.total; t += 0.125) {
    const a = sm.gainsAt(plan.events, "out", t), b = sm.gainsAt(plan.events, "in", t);
    assert.ok(!(a.bass > 0.5 && b.bass > 0.5), `two basses at ${t} in ${label}`);
    assert.ok(!(a.vocals > 0.05 && b.vocals > 0.05), `two singers at ${t} in ${label}`);
  }
}

// ---- 2. the incident ---------------------------------------------------------------
{
  const x = FX.silent, M = x.M;
  for (const keyClash of [false, true]) {             // false: the code that played it; true: keys 6A / 3B clash
    const plan = sm.mergeTransitionPlan(M, x.combo, keyClash);
    // the gate before this fix: full-band RMS, owned by B's sub kick -> booked
    const lv = sm.levelCheck({ events: plan.events, fader: sm.mergeFader(M), dir: 1, span: plan.total, eOut: x.out.full, eIn: x.in.full });
    assert.ok(lv.ok && lv.minDb > -5, `the old gate passed it (${lv.minDb.toFixed(1)} dB)`);
    const preFix = lv.ok ? plan : null;               // what the old booking played
    // ... and it is silent in the audible band: A's break (its hats handed to B) under B's sub kick
    const au = audible(x, preFix, M);
    assert.strictEqual(au.ok, false, "reproduces the silence");
    assert.ok(au.at >= 7.9 && au.at <= 8.1 && au.run * barS(x) >= 1.8, `hole at bar ${au.at} for ${(au.run * barS(x)).toFixed(2)} s`);
    // the booking now: repaired onto an audible combo, or refused (the autopilot's next path runs)
    const b = book(x, M, x.combo, keyClash);
    if (b.plan) {
      assert.ok(b.repaired, "not the silent pick");
      assert.ok(audible(x, b.plan, M).ok && gates(x, b.plan, M).ok, "the booked merge is audible");
      oneBassOneSinger(b.plan, sm.mergeLabel(b.pick.combo));
    } else assert.ok(/near silent/.test(b.reason), b.reason);
    if (keyClash) assert.strictEqual(b.plan, null, "keys clash: no tonal mix to repair onto, refused");
    else assert.deepStrictEqual(b.pick.combo, { drums: "b", bass: "b", vocals: "a", other: "a" });
  }
  // A playing on untouched is audible there: the hole is the plan's doing, not A's
  const a8 = Math.sqrt(STEMS.reduce((s, n) => s + x.out.audible[n][8] ** 2, 0));
  const ref = Math.sqrt(STEMS.reduce((s, n) => s + x.out.audible[n][0] ** 2, 0));
  assert.ok(20 * Math.log10(a8 / ref) > -12, "A itself is not silent on bar 8");
  // the fine transition (22:07:42, a -> b) books its pick unchanged
  const f = FX.fine;
  const bf = book(f, f.M, f.combo, true);
  assert.ok(bf.plan && !bf.repaired, bf.reason);
  console.log("incident merge ok");
}

// ---- 3. deck-controller stemMix plays what gainsAt simulates ------------------------
const ctx = { currentTime: 0 };
// AudioParam timeline. What already played is kept (hist): a cancel at audio time
// c only changes the values from c on.
const valueOf = (ev, t) => {
  let prev = ev[0];
  for (const e of ev) {
    if (e.t <= t) { prev = e; continue; }
    if (e.lin) return prev.v + (e.v - prev.v) * ((t - prev.t) / (e.t - prev.t));
    break;
  }
  return prev.v;
};
class Param {
  constructor(v) { this.ev = [{ t: -1, v, lin: false }]; this.hist = []; }
  _keep() { this.hist.push({ until: ctx.currentTime, ev: this.ev.slice() }); }
  cancelScheduledValues(t) {
    this._keep();
    this.ev = this.ev.filter((e) => e.t < t);                  // a running ramp is dropped: snaps back
    if (!this.ev.length) this.ev = [{ t: -1, v: 0, lin: false }];
  }
  cancelAndHoldAtTime(t) {
    this._keep();
    const v = valueOf(this.ev, t);
    this.ev = this.ev.filter((e) => e.t <= t);
    this.ev.push({ t, v, lin: true });                         // a running ramp ends at t, where it was
  }
  setValueAtTime(v, t) { this.ev.push({ t, v, lin: false }); this.ev.sort((a, b) => a.t - b.t); }
  linearRampToValueAtTime(v, t) { this.ev.push({ t, v, lin: true }); this.ev.sort((a, b) => a.t - b.t); }
  at(t) { const h = this.hist.find((x) => t < x.until); return valueOf(h ? h.ev : this.ev, t); }
  get value() { return this.at(ctx.currentTime); }
  set value(v) { this._keep(); this.ev = [{ t: -1, v, lin: false }]; }
}
const node = () => ({ gain: new Param(1), connect() {}, disconnect() {} });
class Src {
  constructor() { this.playbackRate = new Param(1); this.onended = null; }
  connect() {} disconnect() {}
  start(at, off) { this.startedAt = at || ctx.currentTime; this.off = off; }
  stop() {}
}
Object.assign(ctx, { state: "running", createGain: node, createBufferSource: () => new Src() });
const srcText = fs.readFileSync(path.join(__dirname, "../ui/static/deck-controller.js"), "utf8");
const i0 = srcText.indexOf("class Deck {"), i1 = srcText.indexOf("\n}\n", i0);
assert.ok(i0 >= 0 && i1 > i0, "Deck class not found");
const sandbox = {
  audioCtx: ctx, STEM_NAMES: STEMS, SPIN_UP_SECONDS: 0.5, SPIN_DOWN_SECONDS: 0.8, BRAKE_SECONDS: 1,
  window: { dispatchEvent() {} }, CustomEvent: class {}, state: {}, setTimeout: () => 0, clearTimeout() {}, Math,
};
vm.createContext(sandbox);
vm.runInContext(srcText.slice(i0, i1 + 2) + "\nthis.Deck = Deck;", sandbox);
const buf = (d = 400) => ({ duration: d });
function makeDeck(id) {
  const d = Object.create(sandbox.Deck.prototype);
  Object.assign(d, {
    id, buffer: buf(), source: null, playing: false, startedAt: 0, startOffset: 0,
    loopOn: false, loopBeats: 4, bpm: 126, trimStart: 0, trimEnd: 400, reversed: false,
    _pitchPercent: 0, _bendPercent: 0, _braking: false, _spinningUp: false, _holds: {},
    _stemSrc: {}, stems: { drums: buf(), bass: buf(), vocals: buf(), other: buf(), lag: 0 }, stemState: null, tempoStems: null,
    _meterGain: null, mixGain: node(), vocalBusGain: node(), stemGain: {}, stemLive: {}, stemMeter: {}, onPlayStateChange: null,
    _syncWavesurferCursor() {}, _playbackRate: () => 1, _ensureReverseBuffer() {}, _activeBuffer() { return this.buffer; },
  });
  d.vocalBusGain.gain.value = 0;
  for (const n of STEMS) { d.stemGain[n] = node(); d.stemGain[n].gain.value = 0; d.stemLive[n] = node(); d.stemMeter[n] = node(); }
  return d;
}
// what a deck plays of stem n: the full mix carries every stem at 1
const heard = (d, n, t) => d.mixGain.gain.at(t) + d.stemGain[n].gain.at(t);
{
  // two moves on one bar keep each other's ramps
  ctx.currentTime = 10;
  const d = makeDeck("b");
  d.play(200);
  assert.ok(d.stemMix({ drums: 0 }, 10.5, 0.1));
  ctx.currentTime = 19.8;                              // both booked 200 ms early, like stem-moves book()
  d.stemMix({ other: 0 }, 20, 8);
  d.stemMix({ vocals: 0 }, 20, 2);
  assert.ok(Math.abs(d.stemGain.other.gain.at(24) - 0.5) < 1e-6, `A's synths fade over 8 s, got ${d.stemGain.other.gain.at(24)}`);
  assert.ok(d.stemGain.vocals.gain.at(22.5) < 1e-9 && Math.abs(d.stemGain.vocals.gain.at(21) - 0.5) < 1e-6, "its voice over 2 s");
  assert.ok(Math.abs(d.stemGain.bass.gain.at(24) - 1) < 1e-9, "the bass untouched");
  // a move booked 200 ms ahead on a stem mid-ramp: the ramp runs on until the move
  // (it used to snap back to where it started: 200 ms of the old level)
  ctx.currentTime = 25.8;
  d.stemMix({ other: 1 }, 26, 0.05);
  assert.ok(Math.abs(d.stemGain.other.gain.at(25.9) - (1 - 5.9 / 8)) < 1e-6, `held, got ${d.stemGain.other.gain.at(25.9)}`);
  assert.ok(Math.abs(d.stemGain.other.gain.at(26.1) - 1) < 1e-9);
}
{
  // every merge plan, both decks, keys clashing or not: the Deck's gains == gainsAt
  const bar = 2, t0 = 100, lead = 0.2;
  let compared = 0;
  for (const keyClash of [false, true]) for (const combo of sm.mergeCombos()) for (const M of [16, 32]) {
    const plan = sm.mergeTransitionPlan(M, combo, keyClash), label = `${sm.mergeLabel(combo)} M${M}${keyClash ? " keys clash" : ""}`;
    ctx.currentTime = t0 - 5;
    const decks = { out: makeDeck("b"), in: makeDeck("a") };
    decks.out.play(200);
    const calls = plan.events.map((e) => ({ e, at: t0 + e.bar * bar, call: Math.max(t0 - 0.15, t0 + e.bar * bar - lead) }))
      .sort((a, b) => a.call - b.call);
    for (const { e, at, call } of calls) {
      ctx.currentTime = call;
      const d = decks[e.deck];
      if (e.start) { d.play(21, false, at); d.stemMix(e.stems, at - 0.005, 0.005); continue; }
      const [when, r] = sm.onTime(at, Math.max(0.005, e.ramp * bar), call);       // as stem-moves book()
      d.stemMix(e.stems, when, r);
    }
    // sample outside each booking's 200 ms lead (Web Audio drops a cancelled ramp
    // there) and outside mix <-> stems hand-overs (mix and stems are the same signal)
    const busy = calls.map((c) => [c.call, c.at + 0.01]);
    const handing = (d, t) => { const m = d.mixGain.gain.at(t); return m > 1e-9 && m < 1 - 1e-9; };
    for (let b = 0; b <= plan.total; b += 0.125) {
      const t = t0 + b * bar;
      if (busy.some(([s, e]) => t >= s - 1e-9 && t <= e) || handing(decks.out, t) || handing(decks.in, t)) continue;
      for (const side of ["out", "in"]) {
        const g = sm.gainsAt(plan.events, side, b);
        for (const n of STEMS) {
          const h = heard(decks[side], n, t);
          assert.ok(Math.abs(h - g[n]) < 1e-6, `${label}: ${side} ${n} at bar ${b}: deck ${h.toFixed(3)} vs gainsAt ${g[n].toFixed(3)}`);
          compared++;
        }
      }
    }
  }
  assert.ok(compared > 20000, `compared ${compared}`);
  // a move run after its time still ends when planned (the merge's A-hands-over
  // move is a quarter bar before B's line; the transition books 150 ms before it)
  assert.deepStrictEqual(sm.onTime(10, 0.5, 9.8), [10, 0.5]);
  const late = sm.onTime(10, 0.5, 10.2);
  assert.ok(late[0] === 10.2 && Math.abs(late[1] - 0.3) < 1e-9, `ends at 10.5: ${late}`);
  assert.deepStrictEqual(sm.onTime(10, 0.5, 11), [11, 0.005]);
  console.log("stemMix == gainsAt ok");
}

// ---- 4. the crossfader, out of deck a and out of deck b ------------------------------
{
  const M = 16, segs = sm.mergeFader(M);
  for (let b = 0; b <= M + 8; b += 0.25) {
    const x = (sm.faderAt(segs, 1, b) + 1) / 2;
    const fo = Math.cos((x * Math.PI) / 2), fi = Math.sin((x * Math.PI) / 2);    // what the checks assume
    for (const [out, inn] of [["a", "b"], ["b", "a"]]) {
      const v = sm.faderAt(sm.rawFader(segs, out), 1, b);                            // what executeTransition moves
      const g = sm.deckFaderGains(v);
      assert.ok(Math.abs(g[out] - fo) < 1e-9 && Math.abs(g[inn] - fi) < 1e-9, `${out} -> ${inn} at bar ${b}`);
    }
  }
  const aOut = sm.rawFader(segs, "a"), bOut = sm.rawFader(segs, "b");
  assert.deepStrictEqual([aOut[0].from, aOut[1].to], [-1, 1], "out of a: from a's side to b's");
  assert.deepStrictEqual([bOut[0].from, bOut[1].to], [1, -1], "out of b: from b's side to a's");
  console.log("crossfader both ways ok");
}

// ---- 5. every combo: what gets booked is audible, one bass, one singer --------------
{
  const typical = { out: { bpm: 126, full: null, audible: null }, in: { bpm: 126, full: null, audible: null } };
  let booked = 0, refused = 0;
  for (const x of [FX.silent, FX.fine]) for (const keyClash of [false, true]) for (const combo of sm.mergeCombos()) {
    const plan = sm.mergeTransitionPlan(x.M, combo, keyClash);
    oneBassOneSinger(plan, sm.mergeLabel(combo));
    const b = book(x, x.M, combo, keyClash);
    if (!b.plan) { refused++; continue; }
    booked++;
    assert.ok(audible(x, b.plan, x.M).ok, `${sm.mergeLabel(b.pick.combo)} booked silent`);
    assert.ok(gates(x, b.plan, x.M).ok);
    if (keyClash) assert.strictEqual(new Set([b.pick.combo.bass, b.pick.combo.vocals, b.pick.combo.other]).size === 1 || b.pick.combo === combo, true);
  }
  assert.ok(booked > 0 && refused > 0, `booked ${booked}, refused ${refused}`);
  // unmeasured stems (typical shares): nothing to judge in the audible band
  const p = sm.mergeTransitionPlan(16, FX.silent.combo);
  assert.ok(sm.masterAudibility({ events: p.events, fader: sm.mergeFader(16), dir: 1, span: p.total, eOut: typical.out.audible, eIn: null }).ok);
  // A's own silence is not the plan's: A silent on bars 4-6 and nothing moved there
  const quietA = Object.fromEntries(STEMS.map((n) => [n, Array.from({ length: 25 }, (_, i) => (i >= 4 && i < 7 ? 0 : 0.2))]));
  const loudB = Object.fromEntries(STEMS.map((n) => [n, Array(25).fill(0.2)]));
  const hold = [{ bar: 0, deck: "in", start: true, stems: { drums: 0, bass: 0, vocals: 0, other: 0 }, ramp: 0 }];
  assert.ok(sm.masterAudibility({ events: hold, fader: [{ bar: 0, from: -1, to: -1, bars: 0 }], dir: 1, span: 10, eOut: quietA, eIn: loudB, minRun: 0.5 }).ok);
  // ... but taking A's stems away there is
  const strip = [...hold, { bar: 2, deck: "out", stems: { drums: 0, bass: 0, vocals: 0, other: 0 }, ramp: 0.25 }];
  const r = sm.masterAudibility({ events: strip, fader: [{ bar: 0, from: -1, to: -1, bars: 0 }], dir: 1, span: 6, eOut: quietA, eIn: loudB, minRun: 0.5 });
  assert.ok(!r.ok && r.at > 2 && r.at < 3 && r.run < 2, JSON.stringify(r));   // bars ~2.1-4 (A's own gap on 4-6 is not counted)
  console.log("all merge combos ok");
}
