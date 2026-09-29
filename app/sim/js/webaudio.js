// A recording Web Audio API for the headless console. It makes no sound: every AudioParam keeps
// its automation timeline (so `.value` reads are right at the virtual audio clock) and every
// node keeps its connections, so afterwards graph.js can work out what a listener would have
// heard: per moment, per deck, per stem, per band.
//
// Time: audioCtx.currentTime is the virtual clock (clock.now), the same clock the timers use.
"use strict";

let NODE_ID = 0;

class FakeAudioParam {
  constructor(ctx, name, value = 0, owner = null) {
    this._ctx = ctx; this.name = name; this._base = value; this.owner = owner;
    this._ev = [];                       // sorted automation events
    this.defaultValue = value;
    this.minValue = -3.4e38; this.maxValue = 3.4e38;
  }
  _push(e) {
    this._ctx._log("param", { node: this.owner && this.owner._id, param: this.name, ...e });
    const i = this._ev.findIndex((x) => x.time > e.time);
    if (i < 0) this._ev.push(e); else this._ev.splice(i, 0, e);
    return this;
  }
  get value() { return this.valueAt(this._ctx.currentTime); }
  set value(v) { this._base = +v; this._ev = this._ev.filter((e) => e.time <= this._ctx.currentTime); this._push({ type: "set", time: this._ctx.currentTime, value: +v }); }
  setValueAtTime(v, t) { return this._push({ type: "set", time: +t, value: +v }); }
  linearRampToValueAtTime(v, t) { return this._push({ type: "lin", time: +t, value: +v }); }
  exponentialRampToValueAtTime(v, t) { return this._push({ type: "exp", time: +t, value: Math.max(1e-6, +v) }); }
  setTargetAtTime(v, t, tc) { return this._push({ type: "target", time: +t, value: +v, tc: Math.max(1e-6, +tc) }); }
  setValueCurveAtTime(curve, t, d) { return this._push({ type: "curve", time: +t, curve: Array.from(curve), dur: +d, value: curve[curve.length - 1] }); }
  cancelScheduledValues(t) { this._ev = this._ev.filter((e) => e.time < t); this._ctx._log("param", { node: this.owner && this.owner._id, param: this.name, type: "cancel", time: +t }); return this; }
  cancelAndHoldAtTime(t) { const v = this.valueAt(+t); this._ev = this._ev.filter((e) => e.time < t); this._push({ type: "set", time: +t, value: v }); return this; }
  // value at audio time t, from the event timeline (W3C automation rules, simplified)
  valueAt(t) {
    let v = this._base, tPrev = -Infinity;
    for (let i = 0; i < this._ev.length; i++) {
      const e = this._ev[i];
      if (e.time > t && !(e.type === "lin" || e.type === "exp")) break;
      if (e.type === "set") { if (e.time > t) break; v = e.value; tPrev = e.time; }
      else if (e.type === "lin" || e.type === "exp") {
        const t0 = tPrev === -Infinity ? 0 : tPrev;
        if (e.time <= t) { v = e.value; tPrev = e.time; }
        else {
          if (t <= t0) break;
          const f = (t - t0) / Math.max(1e-9, e.time - t0);
          v = e.type === "lin" ? v + (e.value - v) * f : v * Math.pow(e.value / (v || 1e-6), f);
          break;
        }
      } else if (e.type === "target") {
        if (e.time > t) break;
        const nxt = this._ev[i + 1];
        const tEnd = nxt ? Math.min(t, nxt.time) : t;
        v = e.value + (v - e.value) * Math.exp(-(tEnd - e.time) / e.tc);
        tPrev = e.time;
      } else if (e.type === "curve") {
        if (e.time > t) break;
        const f = Math.min(1, (t - e.time) / Math.max(1e-9, e.dur));
        const idx = f * (e.curve.length - 1), lo = Math.floor(idx);
        v = e.curve[lo] + ((e.curve[Math.min(e.curve.length - 1, lo + 1)] - e.curve[lo]) * (idx - lo));
        tPrev = e.time;
      }
    }
    return v;
  }
}

class FakeNode {
  constructor(ctx, kind) { this.context = ctx; this._kind = kind; this._id = ++NODE_ID; this._out = []; this.numberOfInputs = 1; this.numberOfOutputs = 1; ctx._nodes.push(this); }
  connect(dest) { if (dest && !this._out.includes(dest)) this._out.push(dest); this.context._log("connect", { from: this._id, to: dest && dest._id }); return dest; }
  disconnect(dest) { if (dest) this._out = this._out.filter((d) => d !== dest); else this._out = []; this.context._log("disconnect", { from: this._id, to: dest && dest._id }); }
  addEventListener() {} removeEventListener() {}
  _param(name, v) { return new FakeAudioParam(this.context, name, v, this); }
}
class FakeGain extends FakeNode { constructor(c) { super(c, "gain"); this.gain = this._param("gain", 1); } }
class FakeBiquad extends FakeNode {
  constructor(c) { super(c, "biquad"); this.type = "lowpass"; this.frequency = this._param("frequency", 350); this.Q = this._param("Q", 1); this.gain = this._param("gain", 0); this.detune = this._param("detune", 0); }
  getFrequencyResponse() {}
}
class FakeDelay extends FakeNode { constructor(c) { super(c, "delay"); this.delayTime = this._param("delayTime", 0); } }
class FakePanner extends FakeNode { constructor(c) { super(c, "panner"); this.pan = this._param("pan", 0); } }
class FakeCompressor extends FakeNode {
  constructor(c) { super(c, "compressor"); this.threshold = this._param("threshold", -24); this.knee = this._param("knee", 30); this.ratio = this._param("ratio", 12); this.attack = this._param("attack", 0.003); this.release = this._param("release", 0.25); this.reduction = 0; }
}
class FakeWaveShaper extends FakeNode { constructor(c) { super(c, "shaper"); this.curve = null; this.oversample = "none"; } }
class FakeAnalyser extends FakeNode {
  constructor(c) { super(c, "analyser"); this.fftSize = 2048; this.smoothingTimeConstant = 0.8; this.minDecibels = -100; this.maxDecibels = -30; }
  get frequencyBinCount() { return this.fftSize / 2; }
  getByteTimeDomainData(a) { a.fill(128); } getByteFrequencyData(a) { a.fill(0); }
  getFloatTimeDomainData(a) { a.fill(0); } getFloatFrequencyData(a) { a.fill(-100); }
}
class FakeOscillator extends FakeNode {
  constructor(c) { super(c, "osc"); this.type = "sine"; this.frequency = this._param("frequency", 440); this.detune = this._param("detune", 0); this.onended = null; this._start = null; this._stop = null; }
  start(when = 0) { this._start = Math.max(when, this.context.currentTime); this.context._log("osc.start", { node: this._id, when }); }
  stop(when = 0) { this._stop = Math.max(when, this.context.currentTime); const c = this.context; c._later(this._stop, () => this.onended && this.onended()); }
}
class FakeMediaStreamDest extends FakeNode { constructor(c) { super(c, "streamdest"); this.stream = { getTracks: () => [], getAudioTracks: () => [] }; } }
class FakeConstantSource extends FakeNode { constructor(c) { super(c, "const"); this.offset = this._param("offset", 1); } start() {} stop() {} }

class FakeAudioBuffer {
  constructor(o) {
    this.sampleRate = o.sampleRate || 4000; this.numberOfChannels = o.numberOfChannels || 1;
    this.length = o.length || 0; this.duration = this.length / this.sampleRate;
    this._ch = o.channels || Array.from({ length: this.numberOfChannels }, () => new Float32Array(this.length));
    this.tag = o.tag || null;                 // {hash, stem, bpm, ratio} from the file's SIMT chunk
    this._id = ++NODE_ID;
  }
  getChannelData(i) { return this._ch[Math.min(i, this._ch.length - 1)]; }
  copyToChannel(src, ch, off = 0) { this._ch[ch].set(src, off); }
  copyFromChannel(dst, ch, off = 0) { dst.set(this._ch[ch].subarray(off, off + dst.length)); }
}

class FakeBufferSource extends FakeNode {
  constructor(c) {
    super(c, "source");
    this.buffer = null; this.loop = false; this.loopStart = 0; this.loopEnd = 0;
    this.playbackRate = this._param("playbackRate", 1); this.detune = this._param("detune", 0);
    this.onended = null; this._sStart = null; this._sOff = 0; this._sStop = null; this._sEnded = false;
  }
  start(when = 0, offset = 0, duration) {
    const c = this.context;
    this._sStart = Math.max(when || 0, c.currentTime); this._sOff = offset || 0; this._dur = duration;
    c._log("source.start", { node: this._id, when: this._sStart, offset: this._sOff, buffer: this.buffer && this.buffer._id, tag: this.buffer && this.buffer.tag, loop: this.loop });
    if (!this.loop && this.buffer) this._natCheck(this._sStart);
  }
  // natural end: when the playhead (with whatever rate automation the deck has booked by then)
  // reaches the buffer's end. Re-checked at the estimated end, because the rate may have moved.
  _natCheck(from) {
    const c = this.context;
    const rate = Math.max(0.05, this.playbackRate.valueAt(Math.max(from, c.currentTime)) || 1);
    const pos = this.positionAt(Math.max(from, c.currentTime));
    const left = this.buffer.duration - (pos === null ? this._sOff : pos);
    const at = Math.max(from, c.currentTime) + Math.max(0, left) / rate;
    this._sNat = at;
    c._later(at, () => {
      if (this._sEnded || (this._sStop !== null && this._sStop <= at + 1e-9)) return;
      const p = this.positionAt(c.currentTime);
      if (p !== null && p >= this.buffer.duration - 1e-3) { this._sEnded = true; if (this.onended) { try { this.onended({ target: this }); } catch (e) { c._error(e); } } }
      else this._natCheck(c.currentTime);
    });
  }
  stop(when = 0) {
    const c = this.context, t = Math.max(when || 0, c.currentTime);
    if (this._sStop === null || t < this._sStop) this._sStop = t;
    c._log("source.stop", { node: this._id, when: t });
    c._later(t, () => this._end(t));
  }
  _end(t) {                                         // an explicit stop() reached its time
    if (this._sEnded) return;
    if (this._sStop === null || t < this._sStop - 1e-9) return;
    this._sEnded = true;
    if (this.onended) { try { this.onended({ target: this }); } catch (e) { this.context._error(e); } }
  }
  // song position (buffer seconds) at audio time t, integrating the rate automation (incremental:
  // asking for later times continues from the last answer)
  positionAt(t) {
    if (this._sStart === null || t < this._sStart) return null;
    let c = this._pc;
    if (!c || t < c.t - 1e-9) c = this._pc = { t: this._sStart, pos: this._sOff };
    const step = 0.05;
    while (c.t < t - 1e-9) { const dt = Math.min(step, t - c.t); c.pos += dt * Math.max(0, this.playbackRate.valueAt(c.t + dt / 2)); c.t += dt; }
    let pos = c.pos;
    if (this.loop && this.loopEnd > this.loopStart && pos >= this.loopEnd) pos = this.loopStart + ((pos - this.loopStart) % (this.loopEnd - this.loopStart));
    return pos;
  }
  aliveAt(t) {
    if (this._sStart === null || t < this._sStart) return false;
    if (this._sStop !== null && t >= this._sStop) return false;
    if (!this.loop && this.buffer) { const p = this.positionAt(t); if (p !== null && p >= this.buffer.duration) return false; }
    return true;
  }
}

class FakeAudioContext {
  constructor(clock, opts = {}) {
    this._clock = clock; this._nodes = []; this._cmds = []; this._errors = [];
    this.sampleRate = opts.sampleRate || 4000; this.state = "running"; this.baseLatency = 0.01; this.outputLatency = 0.02;
    this.destination = new FakeNode(this, "destination"); this.destination._isDest = true;
    this.listener = {}; this.audioWorklet = { addModule: () => Promise.resolve() };
    this.decode = null;               // set by env.js: (ArrayBuffer) -> FakeAudioBuffer
    this.onstatechange = null;
  }
  get currentTime() { return this._clock.now; }
  _log(kind, o) { if (this._cmds.length < 2_000_000) this._cmds.push({ t: +this._clock.now.toFixed(4), kind, ...o }); }
  _later(at, fn) { const d = Math.max(0, at - this._clock.now); this._clock.setTimeout(fn, d * 1000); }
  _error(e) { this._errors.push(String(e && e.stack || e)); }
  resume() { return Promise.resolve(); } suspend() { return Promise.resolve(); } close() { return Promise.resolve(); }
  createGain() { return new FakeGain(this); }
  createBiquadFilter() { return new FakeBiquad(this); }
  createBufferSource() { return new FakeBufferSource(this); }
  createAnalyser() { return new FakeAnalyser(this); }
  createDelay() { return new FakeDelay(this); }
  createStereoPanner() { return new FakePanner(this); }
  createOscillator() { return new FakeOscillator(this); }
  createWaveShaper() { return new FakeWaveShaper(this); }
  createDynamicsCompressor() { return new FakeCompressor(this); }
  createConstantSource() { return new FakeConstantSource(this); }
  createMediaStreamDestination() { return new FakeMediaStreamDest(this); }
  createMediaStreamSource() { return new FakeNode(this, "streamsrc"); }
  createMediaElementSource() { return new FakeNode(this, "elsrc"); }
  createScriptProcessor() { return new FakeNode(this, "script"); }
  createChannelSplitter() { return new FakeNode(this, "split"); }
  createChannelMerger() { return new FakeNode(this, "merge"); }
  createBuffer(nch, len, sr) { return new FakeAudioBuffer({ numberOfChannels: nch, length: len, sampleRate: sr }); }
  decodeAudioData(ab, ok, err) {
    const p = Promise.resolve().then(() => this.decode(ab));
    if (ok || err) p.then(ok, err);
    return p;
  }
}
class FakeAudioWorkletNode extends FakeNode { constructor(ctx, name) { super(ctx, "worklet:" + name); this.port = { postMessage() {}, onmessage: null }; this.parameters = new Map(); } }

module.exports = { FakeAudioContext, FakeAudioBuffer, FakeAudioParam, FakeNode, FakeBufferSource, FakeAudioWorkletNode };
