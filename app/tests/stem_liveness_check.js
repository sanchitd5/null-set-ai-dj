// Node check: a stem move never mutes a deck's full mix over stems that are not
// sounding (app/ui/static/deck-controller.js stemMix / stemsLiveAt), and the
// Live Transition Maker only clears the data-ai-audio flags it set
// (app/ui/static/automation.js). Run by test_keylock.py.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

// ---- minimal Web Audio fakes ------------------------------------------------
const ctx = { currentTime: 10 };
class Param {
  constructor(v) { this.ev = [{ t: -1, v, lin: false }]; }
  cancelScheduledValues(t) { this.ev = this.ev.filter((e) => e.t < t); if (!this.ev.length) this.ev = [{ t: -1, v: 0, lin: false }]; }
  setValueAtTime(v, t) { this.ev.push({ t, v, lin: false }); this.ev.sort((a, b) => a.t - b.t); }
  linearRampToValueAtTime(v, t) { this.ev.push({ t, v, lin: true }); this.ev.sort((a, b) => a.t - b.t); }
  setValueCurveAtTime(c, t, d) { this.ev.push({ t: t + d, v: c[c.length - 1], lin: true }); }
  at(t) {
    let prev = this.ev[0];
    for (const e of this.ev) {
      if (e.t <= t) { prev = e; continue; }
      if (e.lin) return prev.v + (e.v - prev.v) * ((t - prev.t) / (e.t - prev.t));
      break;
    }
    return prev.v;
  }
  get value() { return this.at(ctx.currentTime); }
  set value(v) { this.ev = [{ t: -1, v, lin: false }]; }
}
const node = () => ({ gain: new Param(1), connect() {}, disconnect() {} });
class Src {
  constructor() { this.playbackRate = new Param(1); this.onended = null; this.stopped = null; }
  connect() {} disconnect() {}
  start(at, off) { this.startedAt = at || ctx.currentTime; this.off = off; }
  stop(t = 0) { this.stopped = t || ctx.currentTime; }
  end() { if (this.onended) this.onended(); }   // the engine firing 'ended'
}
Object.assign(ctx, {
  state: "running",
  createGain: node,
  createBufferSource: () => new Src(),
});

// ---- load the Deck class out of the browser script --------------------------
const srcText = fs.readFileSync(path.join(__dirname, "../ui/static/deck-controller.js"), "utf8");
const i0 = srcText.indexOf("class Deck {");
const i1 = srcText.indexOf("\n}\n", i0);
assert.ok(i0 >= 0 && i1 > i0, "Deck class not found");
const events = [];
const sandbox = {
  audioCtx: ctx,
  STEM_NAMES: ["drums", "bass", "vocals", "other"],
  SPIN_UP_SECONDS: 0.5, SPIN_DOWN_SECONDS: 0.8, BRAKE_SECONDS: 1,
  window: { dispatchEvent: (e) => events.push(e) },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
  state: {},
  setTimeout: () => 0, clearTimeout() {},
  Math,
};
vm.createContext(sandbox);
vm.runInContext(srcText.slice(i0, i1 + 2) + "\nthis.Deck = Deck;", sandbox);
const { Deck } = sandbox;

const buf = (d = 200) => ({ duration: d });
function makeDeck() {
  const d = Object.create(Deck.prototype);
  Object.assign(d, {
    id: "a", buffer: buf(), source: null, playing: false, startedAt: 0, startOffset: 0,
    loopOn: false, loopBeats: 4, bpm: 128, trimStart: 0, trimEnd: 200, reversed: false,
    _pitchPercent: 0, _bendPercent: 0, _braking: false, _spinningUp: false, _holds: {},
    _stemSrc: {}, stems: null, stemState: null, tempoStems: null, _meterGain: null,
    mixGain: node(), vocalBusGain: node(), stemGain: {}, stemLive: {}, stemMeter: {},
    onPlayStateChange: null,
  });
  d.vocalBusGain.gain.value = 0;
  for (const n of sandbox.STEM_NAMES) {
    d.stemGain[n] = node(); d.stemGain[n].gain.value = 0;
    d.stemLive[n] = node(); d.stemMeter[n] = node();
  }
  // bits of play() that touch the page
  d._syncWavesurferCursor = () => {};
  d._playbackRate = () => 1;
  d._ensureReverseBuffer = () => {};
  d._activeBuffer = () => d.buffer;
  return d;
}
const stemBufs = () => ({ drums: buf(), bass: buf(), vocals: buf(), other: buf(), lag: 0 });
const DRUMS_ONLY = { vocals: 0, bass: 0, other: 0 };
const mixAt = (d, t) => d.mixGain.gain.at(t);

// 1. sanity: stems playing -> the move goes through, mix hands over
{
  ctx.currentTime = 10;
  const d = makeDeck();
  d.stems = stemBufs();
  d.play(30);
  assert.strictEqual(d.stemsReady, true);
  assert.strictEqual(d.stemMix(DRUMS_ONLY, 10.5, 0.03), true);
  assert.ok(mixAt(d, 11) < 1e-9, "mix hands over to live stems");
  assert.ok(Math.abs(d.stemGain.drums.gain.at(11) - 1) < 1e-9);
}

// 2. riff over rap: sources stopped on the clock at t0 -> refused after t0, mix stays up
{
  ctx.currentTime = 10;
  const d = makeDeck();
  d.stems = stemBufs();
  d.play(30);
  d.stopSourcesAt(12);
  assert.strictEqual(d.stemsLiveAt(11), true, "still sounding before t0");
  ctx.currentTime = 12.5;
  assert.strictEqual(d.playing, true, "the deck still 'plays' (its clock follows the riff)");
  assert.strictEqual(d.stemsReady, false, "stopped stems are not ready");
  assert.strictEqual(d.stemMix(DRUMS_ONLY, 13, 0.03), false, "refuses over stopped stems");
  assert.strictEqual(mixAt(d, 14), 1, "mix untouched");
  assert.strictEqual(d.stemState, null);
  // booked before t0 for a time after it: refused too
  ctx.currentTime = 10;
  const e = makeDeck();
  e.stems = stemBufs();
  e.play(30);
  e.stopSourcesAt(12);
  assert.strictEqual(e.stemMix(DRUMS_ONLY, 12.5, 0.03), false);
  assert.strictEqual(mixAt(e, 13), 1);
}

// 3. a stem that ran out (ended) -> refused; in stem mode the deck falls back to the mix
{
  ctx.currentTime = 10;
  const d = makeDeck();
  d.stems = stemBufs();
  d.play(30);
  d._stemSrc.vocals.end();
  assert.strictEqual(d.stemsReady, false);
  assert.strictEqual(d.stemMix(DRUMS_ONLY, 10.2, 0.03), false, "refuses with an ended stem");
  assert.strictEqual(mixAt(d, 11), 1);

  const e = makeDeck();
  e.stems = stemBufs();
  e.play(30);
  assert.strictEqual(e.stemMix(DRUMS_ONLY, 10, 0.03), true);
  ctx.currentTime = 20;
  assert.ok(mixAt(e, 20) < 1e-9);
  e._stemSrc.drums.end();                    // shorter stem buffer runs out mid stem move
  assert.strictEqual(e.stemState, null, "stem mode dropped");
  assert.ok(Math.abs(mixAt(e, 20.02) - 1) < 1e-9, "mix back up, no silence");
  assert.ok(e.stemGain.drums.gain.at(20.02) < 1e-9);
}

// 4. stems attached mid-play start 150 ms ahead: a move before they land is refused
{
  ctx.currentTime = 10;
  const d = makeDeck();
  d.play(30);
  d.setStems(stemBufs());
  assert.strictEqual(d.stemMix(DRUMS_ONLY, 10, 0.03), false, "not started yet");
  assert.strictEqual(mixAt(d, 10.2), 1);
  assert.strictEqual(d.stemMix(DRUMS_ONLY, 10.15, 0.03), true, "fine once they sound");
}

// 5. play(pos, false, when): stems and mix start together, a move booked just
//    before that first sample is fine (both silent before it)
{
  ctx.currentTime = 10;
  const d = makeDeck();
  d.stems = stemBufs();
  d.play(30, false, 11);
  assert.strictEqual(d.stemMix(DRUMS_ONLY, 10.99, 0.005), true);
}

// 6. stopped deck: nothing lingers that passes for stems
{
  ctx.currentTime = 10;
  const d = makeDeck();
  d.stems = stemBufs();
  d.play(30);
  d.stopNow();
  assert.strictEqual(d.stemsReady, false);
  assert.strictEqual(d.stemMix(DRUMS_ONLY, 10.1), false);
  assert.strictEqual(mixAt(d, 11), 1);
}

// 7. key-locked deck, stems dead: "back to full" means the mix, not dead stems
{
  ctx.currentTime = 10;
  const d = makeDeck();
  d.stems = stemBufs();
  d.tempoStems = { bpm: 125, ratio: 1.02 };
  d.play(30);
  assert.ok(mixAt(d, 10.1) < 1e-9, "key-locked: stems carry the deck");
  d.stopSourcesAt(10.5);
  ctx.currentTime = 11;
  assert.strictEqual(d.stemMix(null, 0, 0.02), true);
  assert.ok(Math.abs(mixAt(d, 11.05) - 1) < 1e-9, "mix restored");
}

// 8. stem set swapped in stem mode: old stems play on to the new ones' first sample
{
  ctx.currentTime = 10;
  const d = makeDeck();
  d.stems = stemBufs();
  d.play(30);
  d.stemMix(DRUMS_ONLY, 10, 0.03);
  const old = d._stemSrc.drums;
  d.stemMix({ bass: 1 }, 12, 0.03);            // a booked move
  d.setStems({ ...stemBufs(), ratio: 1.03 });
  assert.ok(Math.abs(old.stopped - 10.15) < 1e-9, `old stem stops at the hand-over, got ${old.stopped}`);
  assert.ok(d._stemSrc.drums !== old && Math.abs(d._stemSrc.drums.startedAt - 10.15) < 1e-9);
  assert.ok(Math.abs(d.stemGain.bass.gain.at(12.1) - 1) < 1e-9, "booked move survives the swap");
  assert.ok(mixAt(d, 12.1) < 1e-9);
}

// 9. reversed play on a key-locked deck: no stems -> full mix, never silence
{
  ctx.currentTime = 10;
  const d = makeDeck();
  d.stems = stemBufs();
  d.tempoStems = { bpm: 125, ratio: 1.02 };
  d.play(30);
  d.reversed = true;
  d.play(30);
  assert.strictEqual(d.stemState, null);
  assert.ok(Math.abs(mixAt(d, 10.1) - 1) < 1e-9, "reversed: mix up");
}
console.log("stem liveness ok");

// ---- automation.js: flag scoping --------------------------------------------
{
  const els = {};
  const el = (sel) => (els[sel] = els[sel] || { sel, dataset: {}, value: 0, disabled: false, textContent: "",
    dispatchEvent() {}, addEventListener(type, fn) { this["on" + type] = fn; } });
  const raf = [];
  const timers = [];
  const deckFake = () => ({ bpm: 128, buffer: buf(), seek() {}, play() {}, setPitchPercent() {},
    crossfaderGain: node(), lowFilter: node(), volumeGain: node() });
  const docListeners = {};
  const sb = {
    document: {
      getElementById: (id) => el("#" + id),
      querySelector: (sel) => el(sel),
      querySelectorAll: () => [],
      addEventListener: (t, fn) => { docListeners[t] = fn; },
    },
    Event: class { constructor(type) { this.type = type; } },
    requestAnimationFrame: (fn) => raf.push(fn),
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout() {},
    audioCtx: ctx,
    decks: { a: deckFake(), b: deckFake() },
    crypto: { randomUUID: (() => { let n = 0; return () => "tok" + ++n; })() },
    Float32Array, Math, Number,
  };
  sb.window = sb;
  sb.emitDJEvent = () => {};
  vm.createContext(sb);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../ui/static/automation.js"), "utf8"), sb);
  const xf = el("#crossfader");
  const flush = () => { const f = raf.splice(0); f.forEach((fn) => fn()); };

  // Quick Cut: two mirror loops on #crossfader with the same token
  ctx.currentTime = 10;
  sb.liveTransition.select({ recipe: "Quick Cut", a_time: 60, b_time: 30 });
  els["#live-transition-arm"].onclick();
  flush();
  assert.strictEqual(xf.dataset.aiAudio, "1");
  ctx.currentTime = 10.15 + 7.5 + 0.02;          // past the short cut, the long sweep runs on
  flush();
  assert.strictEqual(xf.dataset.aiAudio, "1", "the short loop must not clear the flag under the long one");
  ctx.currentTime = 30;
  flush();
  assert.strictEqual(xf.dataset.aiAudio, undefined, "cleared when the last loop ends");

  // autopilot's own flag on another control survives finish(); on a shared one too
  const eq = el('.eq-knob[data-deck="b"][data-band="mid"]');
  eq.dataset.aiAudio = "1";                        // set by autopilot.js
  xf.dataset.aiAudio = "1";                        // autopilot is also driving the fader
  ctx.currentTime = 40;
  sb.liveTransition.select({ recipe: "Blend", a_time: 60, b_time: 30 });
  els["#live-transition-arm"].onclick();
  flush();
  sb.liveTransition.cancel();
  flush();
  assert.strictEqual(eq.dataset.aiAudio, "1", "finish() left autopilot's flag alone");
  assert.strictEqual(xf.dataset.aiAudio, "1", "a flag that was already set is not ours to clear");
  console.log("automation flags ok");
}
