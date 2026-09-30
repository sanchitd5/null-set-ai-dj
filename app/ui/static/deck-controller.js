// AI Music Brain — DJ deck controller: jog wheels (click to play, drag to
// scrub, spin while playing), transport (play/cue), 4 hot cues, loop
// (beat-length presets), rotary 3-band EQ knobs, pitch/volume faders,
// crossfader with VU meters, and waveform trim (WaveSurfer Regions plugin).
//
// Built directly on the Web Audio API -- BiquadFilterNode for the EQ,
// AudioBufferSourceNode.playbackRate for pitch, .loop/.loopStart/.loopEnd
// for looping, AnalyserNode for the VU meters -- the same primitives
// open-source web DJ mixers use internally, without pulling a whole
// separate app/build pipeline into this no-bundler static frontend.
// Depends on globals `waveformA`, `waveformB`, and `state` from app.js
// (loaded first).

const audioCtx = new (window.AudioContext || window.webkitAudioContext)({ latencyHint: "interactive" });

// Semantic events keep recording and automation independent from the widgets
// that happen to originate a control change.
window.djEvents = window.djEvents || new EventTarget();
window.emitDJEvent = window.emitDJEvent || ((type, detail = {}) => {
  window.djEvents.dispatchEvent(new CustomEvent(type, { detail: { ...detail, t: audioCtx.currentTime } }));
});

// Single shared master output stage. Every deck's crossfaderGain lands here
// (instead of going straight to the destination) so one fader controls the
// whole console and so the recorder has one node to tap.
const masterGain = audioCtx.createGain();
masterGain.gain.value = 0.9;
// Brick-wall limiter after the master: a stem move, a level-matched riff or
// two full decks at unity summed past 0 dBFS (a live riff recording hit 0 dBFS
// 184 times). Fast attack, -1 dB ceiling. Everything that "hears the master"
// (VU, recorder, live ear) taps masterOut, i.e. what the crowd hears.
const masterLimiter = audioCtx.createDynamicsCompressor();
masterLimiter.threshold.value = -4;
masterLimiter.knee.value = 0;
masterLimiter.ratio.value = 20;
masterLimiter.attack.value = 0.001;
masterLimiter.release.value = 0.1;
const masterOut = audioCtx.createGain();
// No make-up gain: the Web Audio compressor is not a true brick-wall, so its
// transient overshoot needs the headroom (+2 dB make-up hit 0 dBFS 15k times).
masterOut.gain.value = 10 ** (-0.5 / 20);
masterGain.connect(masterLimiter);
masterLimiter.connect(masterOut);
masterOut.connect(audioCtx.destination);
window.masterOut = masterOut;

// Vocal bus: a deck's vocal stem sent past its own EQ and crossfader, so an
// outgoing vocal can keep singing over the incoming beat. High-passed at
// 120 Hz: one bass owner, always ([[EQ & Frequency Management]]).
const STEM_NAMES = ["drums", "bass", "vocals", "other"];
const SLICE_MAX_S = 40;       // hard cap of one stemSlices window (audio seconds)
const vocalBus = audioCtx.createBiquadFilter();
vocalBus.type = "highpass";
vocalBus.frequency.value = 120;
vocalBus.connect(masterGain);
window.vocalBus = vocalBus;

const BRAKE_SECONDS = 0.8;
// A real turntable never starts or stops instantly: PLAY/PAUSE spins the
// platter up and down. Same mechanism as the BRAKE button (a playbackRate
// ramp), just shorter and applied to the default transport action.
// CUE, hot cues, beatjump and seeking stay instant on purpose -- those are
// precision moves, not platter moves.
const SPIN_UP_SECONDS = 0.35;
const SPIN_DOWN_SECONDS = 0.45;

// Seconds the stems run late against the decoded mix (mp3 encoder delay:
// ~23 ms; lossless: 0). Coarse search on a decimated 3 s window, then a
// sample-exact refine.
function stemLag(mixBuf, stems) {
  const sr = mixBuf.sampleRate, m = mixBuf.getChannelData(0);
  const parts = STEM_NAMES.map((n) => stems[n].getChannelData(0));
  const len = Math.min(m.length, ...parts.map((p) => p.length));
  const sum = (i) => parts.reduce((s, p) => s + (p[i] || 0), 0);
  const at = Math.min(Math.floor(len * 0.3), Math.max(0, len - 4 * sr)), W = Math.floor(3 * sr);
  if (at + W + 4096 >= len) return 0;
  const score = (lag, step) => {
    let dot = 0, a2 = 0, b2 = 0;
    for (let i = at; i < at + W; i += step) { const a = m[i], b = sum(i + lag); dot += a * b; a2 += a * a; b2 += b * b; }
    return a2 > 0 && b2 > 0 ? dot / Math.sqrt(a2 * b2) : -1;
  };
  let best = 0, bestS = -2;
  for (let lag = -4096; lag <= 4096; lag += 16) { const s = score(lag, 8); if (s > bestS) { bestS = s; best = lag; } }
  for (let lag = best - 16; lag <= best + 16; lag++) { const s = score(lag, 1); if (s > bestS) { bestS = s; best = lag; } }
  return bestS > 0.5 ? best / sr : 0;
}

// Same search in the DSP worker (dsp-worker.js): only the 3 s window crosses
// the thread boundary, the main thread stays free. Falls back to stemLag().
let dspWorker = null, dspSeq = 0;
const dspWaiting = new Map();
function dsp() {
  if (dspWorker === null) {
    try {
      dspWorker = new Worker("/dsp-worker.js");
      dspWorker.onmessage = (e) => { const cb = dspWaiting.get(e.data.id); if (cb) { dspWaiting.delete(e.data.id); cb(e.data); } };
      dspWorker.onerror = () => { dspWorker = false; for (const cb of dspWaiting.values()) cb({ error: "worker failed" }); dspWaiting.clear(); };
    } catch (e) { dspWorker = false; }
  }
  return dspWorker || null;
}
function stemLagAsync(mixBuf, stems) {
  const w = dsp();
  if (!w) return Promise.resolve(stemLag(mixBuf, stems));
  const sr = mixBuf.sampleRate, m = mixBuf.getChannelData(0), PAD = 4096;
  const len = Math.min(m.length, ...STEM_NAMES.map((n) => stems[n].length));
  const at = Math.min(Math.floor(len * 0.3), Math.max(0, len - 4 * sr)), W = Math.floor(3 * sr);
  if (at < PAD || at + W + PAD >= len) return Promise.resolve(stemLag(mixBuf, stems));
  const mix = m.slice(at, at + W);
  const parts = STEM_NAMES.map((n) => stems[n].getChannelData(0).slice(at - PAD, at + W + PAD));
  const id = ++dspSeq;
  return new Promise((resolve) => {
    const timer = setTimeout(() => { dspWaiting.delete(id); resolve(stemLag(mixBuf, stems)); }, 5000);
    dspWaiting.set(id, (r) => { clearTimeout(timer); resolve(r.error ? stemLag(mixBuf, stems) : r.lag); });
    w.postMessage({ id, op: "stemLag", sr, mix, stems: parts }, [mix.buffer, ...parts.map((x) => x.buffer)]);
  });
}

class Deck {
  constructor(id, wavesurfer) {
    this.id = id;
    this.wavesurfer = wavesurfer;
    this.buffer = null;
    this.source = null;
    this.startedAt = 0; // audioCtx.currentTime when current playback began
    this.startOffset = 0; // seconds into the buffer that playback began from
    this.playing = false;
    this.cuePoint = 0;
    this.hotCues = { 1: null, 2: null, 3: null, 4: null };
    this.loopOn = false;
    this.loopBeats = 4;
    this.bpm = 128;
    this.trimStart = 0;
    this.trimEnd = null;
    this._pitchPercent = 0;
    this._bendPercent = 0; // temporary pitch-bend nudge, on top of the fader
    this.onPlayStateChange = null; // set by UI wiring
    this.reversed = false;
    this.reverseBuffer = null;
    this.analysis = null;
    this._braking = false;
    this._brakeStartRate = 1;
    this._brakeStartPos = 0;
    this._brakeStartedAt = 0;
    this._brakeTimer = null;
    this._brakeDur = BRAKE_SECONDS;
    this._spinningUp = false;
    this._spinUpStartedAt = 0;
    this._spinUpRate = 1;
    this._spinUpPos = 0;
    this._spinUpTimer = null;
    this.ovPlayedEl = document.querySelector(`#overview-${id} .ov-played`);
    this.ovHeadEl = document.querySelector(`#overview-${id} .ov-head`);
    this.jogPosEl = document.getElementById(`jogpos-${id}`);
    this.bpmEl = document.getElementById(`bpm-${id}`);
    this._lastClock = "";
    this._lastOverviewPct = -1;
    this._lastBpm = "";

    // Pre-EQ gain stage (channel trim), distinct from the volume fader.
    this.inputGain = audioCtx.createGain();

    // Live stems (drums / bass / vocals / other): four sources run sample-locked
    // beside the full mix at gain 0; a stem move crossfades mix -> stems on the
    // audio clock. stems sum to the mix within -15..-21 dB, so the switch is
    // inaudible. The vocal also has a bus send that skips this deck's EQ and
    // crossfader: that's how an outgoing vocal rides over the incoming beat.
    this.mixGain = audioCtx.createGain();
    this.mixGain.connect(this.inputGain);
    this.stems = null;          // {drums, bass, vocals, other: AudioBuffer, lag: s}
    this._stemSrc = {};         // name -> AudioBufferSourceNode
    this.stemGain = {};
    this.stemLive = {};         // name -> gain of the running stem source (0 while that stem is held)
    for (const n of STEM_NAMES) {
      const g = audioCtx.createGain();
      g.gain.value = 0;
      g.connect(this.inputGain);
      this.stemGain[n] = g;
      const live = audioCtx.createGain();
      live.connect(g);
      this.stemLive[n] = live;
    }
    this._holds = {};           // name -> {src, until}
    this._slices = {};          // name -> {srcs, until} (stemSlices)
    this.vocalBusGain = audioCtx.createGain();
    this.vocalBusGain.gain.value = 0;
    this.vocalBusGain.connect(vocalBus);
    // Mini players (stem-moves.js): one pre-fader analyser per stem, so a
    // muted stem still shows what it would play. Not connected to any output.
    this.stemMeter = {};
    for (const n of STEM_NAMES) {
      const m = audioCtx.createAnalyser();
      m.fftSize = 512;
      m.smoothingTimeConstant = 0;
      this.stemMeter[n] = m;
    }
    this._meterGain = null;     // {name: GainNode} while another engine (riff over rap) drives this deck
    this.stemState = null;      // null = full mix, else {drums, bass, vocals, other, bus}

    this.lowFilter = audioCtx.createBiquadFilter();
    this.lowFilter.type = "lowshelf";
    this.lowFilter.frequency.value = 120;

    this.midFilter = audioCtx.createBiquadFilter();
    this.midFilter.type = "peaking";
    this.midFilter.frequency.value = 1000;
    this.midFilter.Q.value = 0.7;

    this.highFilter = audioCtx.createBiquadFilter();
    this.highFilter.type = "highshelf";
    this.highFilter.frequency.value = 5000;

    this.volumeGain = audioCtx.createGain();
    this.crossfaderGain = audioCtx.createGain();
    this.analyser = audioCtx.createAnalyser();
    this.analyser.fftSize = 256;
    this._levelData = new Uint8Array(this.analyser.frequencyBinCount);

    // FX insert point: fxInput fans out to a permanent dry path plus whatever
    // wet chain fx-rack.js patches in; both meet again at fxOutput.
    this.fxInput = audioCtx.createGain();
    this.fxDry = audioCtx.createGain();
    this.fxOutput = audioCtx.createGain();

    this.inputGain.connect(this.lowFilter);
    this.lowFilter.connect(this.midFilter);
    this.midFilter.connect(this.highFilter);
    this.highFilter.connect(this.volumeGain);
    this.volumeGain.connect(this.fxInput);
    this.fxInput.connect(this.fxDry);
    this.fxDry.connect(this.fxOutput);
    this.fxOutput.connect(this.analyser);
    this.fxOutput.connect(this.crossfaderGain);
    this.crossfaderGain.connect(masterGain);

    this.wavesurfer.on("ready", () => this._onWavesurferReady());
  }

  async _onWavesurferReady() {
    // app.js decodes the file once with Web Audio and stashes the real
    // AudioBuffer here; fall back to WaveSurfer's own decoded data if that
    // path was not used (it is not a playable AudioBuffer in every case, but
    // it keeps a legacy load working rather than crashing).
    this.buffer = (window.decodedBuffers && window.decodedBuffers[this.id]) || this.wavesurfer.getDecodedData();
    if (!this.buffer) return;
    // New song: the old one's stems (and any stem move) go.
    this.tempoStems = null;
    this._nativeStems = null;
    this.stemMix(null, 0, 0.005);
    this.setStems(null);
    this._breakdownDone = false;
    this._hookDropDone = false;
    this.hookDrops = null;
    this._remix = null;
    this._learned = null;
    this.trimEnd = this.buffer.duration;
    this.cuePoint = 0;
    this.reverseBuffer = null; // rebuilt lazily for the new track
    if (this.reversed) this._setReversed(false);

    const trackId = this.id === "a" ? state.trackA : state.trackB;
    if (trackId) {
      try {
        const res = await fetch(`/api/tracks/${trackId}/analysis`);
        if (res.ok) {
          const analysis = await res.json();
          this.analysis = analysis;
          if (analysis.bpm > 0) this.bpm = analysis.bpm;
          this._loadVocals(trackId, analysis);
          this.fame = null;
          fetch(`/api/tracks/${trackId}/fame`).then((r) => (r.ok ? r.json() : null))
            .then((f) => { if (this.analysis === analysis) this.fame = f; }).catch(() => {});
          // where to go acapella on this song's emotional line and drop back in
          // (app/music_brain/analysis/hook_drop.py; lyrics + the local model's picks, cached)
          fetch(`/api/tracks/${trackId}/hook-drops?top_n=3`).then((r) => (r.ok ? r.json() : null))
            .then((h) => { if (this.analysis === analysis) this.hookDrops = (h && h.hook_drops) || []; }).catch(() => {});
        }
      } catch (e) { /* keep default bpm */ }
    }
    const bpmEl = document.getElementById(`bpm-${this.id}`);
    if (bpmEl) bpmEl.textContent = this.bpm.toFixed(1);

    const keyEl = document.getElementById(`key-${this.id}`);
    if (keyEl) {
      const camelot = this.analysis && this.analysis.key && this.analysis.key.camelot;
      keyEl.textContent = camelot || "--";
      if (this.analysis && this.analysis.key && this.analysis.key.key_name) {
        keyEl.title = this.analysis.key.key_name;
      }
    }

    this.hotCues = { 1: null, 2: null, 3: null, 4: null };
    refreshHotCueUI(this.id);
    this.renderBeatGrid();
    updateHarmonicReadout();
  }

  // Thin vertical ticks over the Wavesurfer waveform, one per detected beat,
  // with every 4th (downbeat) drawn brighter. Positions are pure percentages
  // so the overlay survives a container resize without a re-fetch.
  renderBeatGrid() {
    const overlay = document.getElementById(`beatgrid-${this.id}`);
    if (!overlay) return;
    overlay.innerHTML = "";
    const beats = this.analysis && this.analysis.beat_times;
    const duration = this.buffer ? this.buffer.duration : 0;
    if (!beats || !beats.length || !duration) return;
    const downbeats = new Set((this.analysis.downbeat_times || []).map((t) => t.toFixed(3)));
    // 8-bar (32-beat) phrase boundaries get a bright ruler line + a bar number,
    // matching the beatgrid ruler in the reference hardware layout. Both sets
    // are real analyzer output, not decoration.
    const phrases = this.analysis.phrase_boundaries_8bar || [];
    const phraseSet = new Set(phrases.map((t) => t.toFixed(3)));
    const phraseIndex = new Map(phrases.map((t, i) => [t.toFixed(3), i * 8 + 1]));
    const frag = document.createDocumentFragment();
    beats.forEach((t) => {
      if (t > duration) return;
      const stamp = t.toFixed(3);
      const tick = document.createElement("div");
      if (phraseSet.has(stamp)) {
        tick.className = "beat-tick phrase";
        const num = document.createElement("span");
        num.className = "bar-num";
        num.textContent = phraseIndex.get(stamp);
        tick.appendChild(num);
      } else {
        tick.className = downbeats.has(stamp) ? "beat-tick downbeat" : "beat-tick";
      }
      tick.style.left = `${(t / duration) * 100}%`;
      frag.appendChild(tick);
    });
    overlay.appendChild(frag);
  }

  // Hot-cue badges drawn on the waveform at each cue's REAL stored position
  // (seconds / duration), plus matching ticks on the mini overview strip.
  renderHotCueMarkers() {
    const overlay = document.getElementById(`cues-${this.id}`);
    const ovCues = document.querySelector(`#overview-${this.id} .ov-cues`);
    if (overlay) overlay.innerHTML = "";
    if (ovCues) ovCues.innerHTML = "";
    const duration = this.buffer ? this.buffer.duration : 0;
    if (!duration) return;
    Object.keys(this.hotCues).forEach((n) => {
      const t = this.hotCues[n];
      if (t === null || t === undefined) return;
      const pct = Math.max(0, Math.min(100, (t / duration) * 100));
      if (overlay) {
        const badge = document.createElement("div");
        badge.className = "cue-badge";
        badge.style.left = `${pct}%`;
        badge.textContent = `CUE ${n} · ${formatClock(t)}`;
        overlay.appendChild(badge);
      }
      if (ovCues) {
        const tick = document.createElement("span");
        tick.className = "ov-cue";
        tick.style.left = `${pct}%`;
        ovCues.appendChild(tick);
      }
    });
  }

  _playbackRate() {
    // mid-glide: the rate the sources are playing at right now
    if (this._rateRamp) return this._rampRateAt(audioCtx.currentTime);
    return Math.max(0.05, 1 + (this._pitchPercent + this._bendPercent) / 100);
  }

  // ---- smooth tempo glide (user: "the bpm should come to normal") -----------
  // One linear playbackRate ramp r0 -> r1 over [t0, t1] on the audio clock, on
  // every source of the deck (mix, stems, holds). The position clock is rebased
  // when the glide is booked (startedAt <= t0), so song time travelled is the
  // integral of the rate: exact during and after the ramp.
  _rampRateAt(T) {
    const rr = this._rateRamp;
    if (T <= rr.t0) return rr.r0;
    if (T >= rr.t1) return rr.r1;
    return rr.r0 + ((rr.r1 - rr.r0) * (T - rr.t0)) / (rr.t1 - rr.t0);
  }

  // Song seconds travelled between startedAt and audio time T.
  _travelled(T) {
    const s = this.startedAt, rr = this._rateRamp;
    if (!(T > s)) return 0;
    if (!rr) return (T - s) * this._playbackRate();
    const d = rr.t1 - rr.t0;
    const pre = Math.max(0, Math.min(T, rr.t0) - s) * rr.r0;
    const x = Math.max(0, Math.min(T, rr.t1) - Math.max(s, rr.t0));
    const mid = d > 0 ? rr.r0 * x + ((rr.r1 - rr.r0) * x * x) / (2 * d) : 0;
    const post = Math.max(0, T - Math.max(s, rr.t1)) * rr.r1;
    return pre + mid + post;
  }

  // Put the deck's rate curve (steady, or the booked glide) on one source's
  // playbackRate param from audio time `from` on. k = the source's rate multiplier.
  _scheduleRate(p, k, from) {
    const rr = this._rateRamp;
    p.cancelScheduledValues(from);
    if (!rr || from >= rr.t1) { p.setValueAtTime((rr ? rr.r1 : this._playbackRate()) * k, from); return; }
    p.setValueAtTime(this._rampRateAt(from) * k, from);
    if (from < rr.t0) p.setValueAtTime(rr.r0 * k, rr.t0);
    p.linearRampToValueAtTime(rr.r1 * k, rr.t1);
  }

  // Glide the tempo to `pct` (pitch fader %) over `seconds`, starting at audio
  // time `at` (now when omitted). Not playing / braking / spinning up: an
  // instant setPitchPercent. A fader or bend move during the glide cancels it
  // at the current rate (_applyRate). Returns the audio time the glide ends.
  rampPitchPercent(pct, seconds, at = 0) {
    const now = audioCtx.currentTime;
    if (!this.playing || this._braking || this._spinningUp || !(seconds > 0) || this._extPos) {
      this.setPitchPercent(pct);
      return now;
    }
    const t0 = Math.max(now, at || 0);
    const r0 = this._playbackRate();
    const shadow = this.slipPosition();
    this.startOffset = this._currentPosition();   // rebase on the old curve first
    this.startedAt = now;
    this._slipRebase(shadow);
    this._pitchPercent = pct;
    this._rateRamp = { t0, t1: t0 + seconds, r0, r1: Math.max(0.05, 1 + (pct + this._bendPercent) / 100) };
    for (const s of this._allSources()) this._scheduleRate(s.playbackRate, s._rateMul || 1, now);
    return t0 + seconds;
  }

  _activeBuffer() {
    return this.reversed ? this.reverseBuffer : this.buffer;
  }

  // Build (once per track) a buffer whose channel data runs backwards. Web
  // Audio has no negative playbackRate, so reverse playback means swapping in
  // this copy and mirroring every position through `duration - t`.
  _ensureReverseBuffer() {
    if (this.reverseBuffer || !this.buffer) return;
    const src = this.buffer;
    const rev = audioCtx.createBuffer(src.numberOfChannels, src.length, src.sampleRate);
    for (let ch = 0; ch < src.numberOfChannels; ch++) {
      const from = src.getChannelData(ch);
      const to = rev.getChannelData(ch);
      const n = from.length;
      for (let i = 0; i < n; i++) to[i] = from[n - 1 - i];
    }
    this.reverseBuffer = rev;
  }

  currentLevel() {
    // 0..1 RMS-ish level for the VU meter, drawn from the analyser.
    if (!this.playing) return 0;
    this.analyser.getByteTimeDomainData(this._levelData);
    let sumSquares = 0;
    for (let i = 0; i < this._levelData.length; i++) {
      const v = (this._levelData[i] - 128) / 128;
      sumSquares += v * v;
    }
    return Math.min(1, Math.sqrt(sumSquares / this._levelData.length) * 3);
  }

  _clampPos(p) {
    const dur = this.buffer ? this.buffer.duration : 0;
    return Math.max(0, Math.min(p, dur));
  }

  _currentPosition() {
    // Another engine (riff over rap) is playing this deck's stems: its clock
    // says where in the song we are, so the platter, time and cursor move on.
    if (this._extPos) return this._extPos(audioCtx.currentTime);
    if (this._braking) {
      // playbackRate is linearly ramping r0 -> 0 across the ramp time, so the
      // distance travelled is the integral of that ramp, not rate * elapsed.
      const dur = this._brakeDur;
      const t = Math.min(dur, audioCtx.currentTime - this._brakeStartedAt);
      const travelled = this._brakeStartRate * (t - (t * t) / (2 * dur));
      return this._clampPos(this._brakeStartPos + (this.reversed ? -travelled : travelled));
    }
    if (this._spinningUp) {
      // Mirror image of the spin-down: rate ramps 0 -> r over SPIN_UP_SECONDS.
      const t = Math.min(SPIN_UP_SECONDS, audioCtx.currentTime - this._spinUpStartedAt);
      const travelled = this._spinUpRate * ((t * t) / (2 * SPIN_UP_SECONDS));
      return this._clampPos(this._spinUpPos + (this.reversed ? -travelled : travelled));
    }
    if (!this.playing) return this.startOffset;
    // before a scheduled start the position holds at the cue point
    const travelled = this._travelled(audioCtx.currentTime);
    const raw = this.startOffset + (this.reversed ? -travelled : travelled);
    const span = this._loopSpan;
    if (span && this.loopOn && raw >= span[1] && span[1] > span[0]) {
      return span[0] + ((raw - span[0]) % (span[1] - span[0]));
    }
    return this._clampPos(raw);
  }

  // Changing playbackRate mid-playback invalidates the startedAt/startOffset
  // accounting, so rebase the clock to "now" before the rate actually changes.
  _applyRate() {
    if (this._spinningUp) return; // let the spin-up ramp finish first
    const rebase = this.playing && !this._braking, shadow = this.slipPosition();   // a slip shadow rebases with the clock
    if (rebase) {
      this.startOffset = this._currentPosition();   // on the glide curve if one runs
      this.startedAt = audioCtx.currentTime;
    }
    // a manual rate change wins over a booked glide (the fader now says where)
    const gliding = !!this._rateRamp;
    this._rateRamp = null;
    if (rebase) this._slipRebase(shadow);
    if (this.source && !this._braking) {
      for (const s of this._allSources()) {
        if (gliding) s.playbackRate.cancelScheduledValues(audioCtx.currentTime);
        s.playbackRate.value = this._playbackRate() * (s._rateMul || 1);
      }
    }
  }

  _allSources() {
    return [this.source, ...Object.values(this._stemSrc)].filter(Boolean);
  }

  // Track position at audio time T (same maths as _currentPosition, any T).
  _positionAt(T) {
    if (!this.playing) return this.startOffset;
    const travelled = this._travelled(T);
    const raw = this.startOffset + (this.reversed ? -travelled : travelled);
    const span = this._loopSpan;
    if (span && this.loopOn && raw >= span[1] && span[1] > span[0]) return span[0] + ((raw - span[0]) % (span[1] - span[0]));
    return this._clampPos(raw);
  }

  // One stem source mirroring the mix source: same rate, same loop window,
  // offset by the stems' measured lag. Starts at audio time `at`, track pos `pos`.
  _startStem(name, at, pos, mixSrc) {
    const st = this.stems;
    if (!st || !st[name] || this.reversed) return;
    const s = audioCtx.createBufferSource();
    s.buffer = st[name];
    // Tempo stems (key-locked at another BPM, st.ratio = stretched / original):
    // song time t lives at t * ratio in them, and they play `ratio` faster than
    // the mix source to cover the same song time, at their own (unchanged) key.
    const k = st.ratio || 1;
    s._rateMul = k;
    s.playbackRate.value = this._playbackRate() * k;
    if (this._rateRamp) this._scheduleRate(s.playbackRate, k, audioCtx.currentTime);   // mid-glide: follow it
    if (mixSrc.loop) {
      s.loop = true;
      s.loopStart = (mixSrc.loopStart + st.lag) * k;
      s.loopEnd = (mixSrc.loopEnd + st.lag) * k;
    }
    s.connect(this.stemLive[name]);
    if (name === "vocals") s.connect(this.vocalBusGain);
    s.connect(this.stemMeter[name]);
    s.start(at, Math.max(0, Math.min((pos + st.lag) * k, st[name].duration - 0.01)));
    s._startAt = at;
    // A stem that runs out (buffer shorter than the mix) while the deck is in
    // stem mode would leave the muted mix over nothing: hand back to the mix.
    s.onended = () => {
      s._dead = true;
      if (this._stemSrc[name] !== s) return;           // replaced / stopped on purpose
      if (this.stemState && this._mixLiveAt(audioCtx.currentTime)) this._fallbackToMix();
      if (this._mixLiveAt(audioCtx.currentTime)) this.rearmStems(`${name} ended`);   // no-op when they ran out
    };
    this._stemSrc[name] = s;
  }

  // Stems decoded: keep them, and if the deck is already playing, attach the
  // stem sources 150 ms ahead on the exact sample the mix will be at (or at
  // audio time `at`, when later: the tempo ladder swaps sets on a phrase line).
  setStems(stems, at = 0) {
    const attach = !!(stems && this.playing && !this._braking && !this._spinningUp && this.source);
    const T = Math.max(audioCtx.currentTime + 0.15, at || 0);
    // In stem mode the old stems play on until the new ones land on the same
    // sample (stopping them now left a 150 ms hole with the mix muted).
    this._stopStems(attach && this.stemState ? T : 0);
    this.stems = stems;
    if (attach) {
      const pos = this._positionAt(T);
      for (const n of STEM_NAMES) this._startStem(n, T, pos, this.source);
      // Key-locked set from the full mix: hand over to the stems. Already in stem
      // mode: the booked moves keep running on the new sources (re-setting every
      // gain here cancelled them and desynced stemState from the audio).
      if (!this.stemState && stems.ratio && Math.abs(stems.ratio - 1) > 0.001) this.stemMix({ drums: 1, bass: 1, vocals: 1, other: 1, bus: 0 }, T, 0.01);
    }
    if (this.stemState && this.playing && !this.stemsLiveAt(attach ? T : audioCtx.currentTime)) this._fallbackToMix();
  }

  // at > 0: stop on the audio clock at `at` (disconnect once ended).
  _stopStems(at = 0) {
    for (const s of Object.values(this._stemSrc)) {
      try { s.stop(at); } catch (e) { /* already stopped */ }
      if (at > audioCtx.currentTime) s.onended = () => { s._dead = true; s.disconnect(); };
      else { s._dead = true; s.disconnect(); }
    }
    this._stemSrc = {};
  }

  // Stop every source of this deck on the audio clock at `t` without dropping
  // the deck state (riff over rap takes over the deck's output from t). Marked
  // so liveness checks know they are silent from t on.
  stopSourcesAt(t) {
    for (const s of this._allSources()) {
      try { s.stop(t); } catch (e) { /* already stopped */ }
      s._stopAt = t;
    }
  }

  // A source is sounding at audio time t: not ended, not stopped by t.
  _srcLiveAt(s, t) {
    return !!s && !s._dead && !(s._stopAt !== undefined && s._stopAt <= t);
  }

  _mixLiveAt(t) { return this._srcLiveAt(this.source, t); }

  // Stems genuinely playing at audio time t: every stem source alive and
  // started by t, or sample-locked with a mix that has not started either
  // (play(pos, false, when): handing over before the first sample is silent
  // on both sides). Existence alone was the old test: dead sources passed it.
  stemsLiveAt(t) {
    if (!this.stems || !this._mixLiveAt(t)) return false;
    const mixStart = this.source._startAt || 0;
    return STEM_NAMES.every((n) => {
      const s = this._stemSrc[n];
      return this._srcLiveAt(s, t) && (s._startAt || 0) <= Math.max(t, mixStart) + 0.005;
    });
  }

  // Back to the full mix now, whatever stemState says (stems gone / dead).
  // Unlike stemMix(null) this ignores tempoStems: a pitched mix beats silence.
  _fallbackToMix() {
    const t = audioCtx.currentTime, end = t + 0.01;
    const to = (param, v) => {
      param.cancelScheduledValues(t);
      param.setValueAtTime(param.value, t);
      param.linearRampToValueAtTime(v, end);
    };
    to(this.mixGain.gain, 1);
    for (const n of STEM_NAMES) to(this.stemGain[n].gain, 0);
    to(this.vocalBusGain.gain, 0);
    this.stemState = null;
    this._emitStem(null);
  }

  // Mini player read-out: {level (RMS, pre-fader), gain (what the crowd hears of it)}.
  meterRead(name, buf) {
    const m = this.stemMeter[name];
    m.getFloatTimeDomainData(buf);
    let e = 0;
    for (let i = 0; i < buf.length; i++) e += buf[i] * buf[i];
    const level = Math.sqrt(e / buf.length);
    const g = this._meterGain ? this._meterGain[name].gain.value
      : this.stemState ? this.stemGain[name].gain.value + (name === "vocals" ? this.vocalBusGain.gain.value : 0)
      : this.playing ? 1 : 0;
    return { level, gain: g };
  }

  // Live now, or a set swap (setStems: new stems land 150 ms ahead) about to
  // land: that window is not "no stems" (a transition planned inside it cut).
  get stemsReady() {
    const now = audioCtx.currentTime;
    return this.stemsLiveAt(now) || this.stemsLiveAt(now + 0.2);   // 0.2 s > setStems' 150 ms lead
  }

  // Decoded stems that are not sounding while the mix plays (a source ended /
  // was replaced / never attached): restart them sample-locked to the mix
  // instead of treating the deck as stemless for the rest of the song. Not
  // while another engine owns the output (riff over rap stopped the mix too,
  // so the mix is not live there). Returns whether the stems are ready.
  rearmStems(why = "") {
    if (this.stemsReady) return true;
    const now = audioCtx.currentTime;
    if (!this.stems || !this.playing || this.reversed || this._braking || this._spinningUp || this._extPos) return false;
    if (!this._mixLiveAt(now)) return false;
    const pos = this._positionAt(now + 0.15), k = this.stems.ratio || 1, lag = this.stems.lag || 0;
    const room = STEM_NAMES.every((n) => this.stems[n] && (pos + lag) * k < this.stems[n].duration - 1);
    if (!room) return false;                               // stems genuinely ran out
    console.info(`deck ${this.id}: stems re-armed${why ? ` (${why})` : ""}`);
    this.setStems(this.stems);
    return this.stemsReady;
  }

  // Stem move on the audio clock. target = {drums, bass, vocals, other, bus}
  // (0..1, omitted = unchanged); null = back to the full mix. Ramps over
  // `ramp` s from `when`. Returns false (mix untouched) unless the stems are
  // genuinely playing at the move's time: a stem move must never mute the
  // full mix over dead stems.
  stemMix(target, when = 0, ramp = 0.03) {
    const t = Math.max(audioCtx.currentTime, when || 0), end = t + Math.max(0.005, ramp);
    // Ramp from the LOGICAL level (the previous stem state), not param.value:
    // a move booked right after one that hasn't played yet would otherwise
    // start from the stale value (a rap meant to land would fade in).
    // Moves are booked ~200 ms ahead: cancelScheduledValues(t) would drop a ramp
    // still running then and snap the stem back to where that ramp started until
    // t (B's synths vanished for 200 ms before the merge's last line). Hold it.
    const set = (param, from, v) => {
      if (param.cancelAndHoldAtTime) param.cancelAndHoldAtTime(t); else param.cancelScheduledValues(t);
      param.setValueAtTime(from, t);
      param.linearRampToValueAtTime(v, end);
    };
    const prev = this.stemState;                         // null = full mix
    const was = (n) => (prev ? prev[n] || 0 : 0);        // audible stem gain before this move
    const live = this.stemsLiveAt(t);
    // key-locked deck: "full" means all stems up, but only while they sound
    if (target === null && this.tempoStems && live) target = { drums: 1, bass: 1, vocals: 1, other: 1, bus: 0 };
    if (target === null) {
      if (!prev) return true;
      set(this.mixGain.gain, 0, 1);
      for (const n of STEM_NAMES) set(this.stemGain[n].gain, was(n), 0);
      set(this.vocalBusGain.gain, was("bus"), 0);
      this.stemState = null;
      this._emitStem(null);
      return true;
    }
    if (!live) return false;
    const cur = prev || { drums: 1, bass: 1, vocals: 1, other: 1, bus: 0 };
    let next = { ...cur, ...target };
    // Never a silent master: an on-air deck nobody else is carrying keeps one stem up.
    const keep = window.stemMoves && window.stemMoves.core && window.stemMoves.core.keepOneStem;
    if (keep && this._onAir()) {
      const r = keep(next, cur, this._othersCarry());
      if (r.kept) console.info(`deck ${this.id}: kept ${r.kept} up (a move would have muted every stem on the master)`);
      next = r.next;
    }
    // from the full mix: stems start where the mix was (1) and the mix hands over
    // instantly-ish, so the switch itself is inaudible. Already in stem mode: only
    // the stems this move changes are re-ramped; the others keep the ramp an
    // earlier move gave them (two moves on one bar, A's synths over 8 bars and its
    // voice over 2, cut the 8-bar fade: the master lost A's synths on the line
    // while B's were still at 0). stem-moves.js gainsAt models the same.
    if (!prev) set(this.mixGain.gain, 1, 0);
    for (const n of STEM_NAMES) if (!prev || next[n] !== was(n)) set(this.stemGain[n].gain, prev ? was(n) : 1, next[n]);
    if (!prev || (next.bus || 0) !== was("bus")) set(this.vocalBusGain.gain, was("bus"), next.bus || 0);
    this.stemState = next;
    this._emitStem(next);
    return true;
  }

  // This deck reaches the master: playing with its crossfader side open.
  _onAir() {
    return !!(this.playing && this.crossfaderGain && this.crossfaderGain.gain.value > 0.05);
  }
  // Another deck is on air AND sounding (full mix, or at least one stem up).
  _othersCarry() {
    const ds = window.decks || {};
    return Object.values(ds).some((d) => d && d !== this && d._onAir && d._onAir() &&
      (!d.stemState || ["drums", "bass", "vocals", "other", "bus"].some((n) => (d.stemState[n] || 0) >= 0.126)));
  }

  // Stem hold: loop `bars` of one stem (song time `from`) from audio time
  // `at` until `until`, while the other stems play on. The live stem is
  // crossfaded out/in over `xf` s at both edges. Needs stem mode.
  holdStem(name, from, bars, at, until, xf = 0.02) {
    const st = this.stems;
    if (!st || !st[name] || !this.stemsReady) return false;
    this.releaseHold(name, at);
    const k = st.ratio || 1, bar = 240 / (this.bpm || 128);
    const s = audioCtx.createBufferSource();
    s.buffer = st[name];
    s.loop = true;
    s.loopStart = (from + st.lag) * k;
    s.loopEnd = (from + bars * bar + st.lag) * k;
    s._rateMul = k;
    s.playbackRate.value = this._playbackRate() * k;
    if (this._rateRamp) this._scheduleRate(s.playbackRate, k, audioCtx.currentTime);
    const g = audioCtx.createGain();
    g.gain.setValueAtTime(0, at);
    g.gain.linearRampToValueAtTime(1, at + xf);
    g.gain.setValueAtTime(1, until - xf);
    g.gain.linearRampToValueAtTime(0, until);
    s.connect(g); g.connect(this.stemGain[name]);
    s.start(at, s.loopStart);
    s.stop(until + 0.05);
    const live = this.stemLive[name].gain;
    live.cancelScheduledValues(at);
    live.setValueAtTime(1, at); live.linearRampToValueAtTime(0, at + xf);
    live.setValueAtTime(0, until - xf); live.linearRampToValueAtTime(1, until);
    this._holds[name] = { src: s, gain: g, until };
    this._stemSrc["hold_" + name] = s;           // rate changes / brakes reach it too
    s.onended = () => { if (this._holds[name] && this._holds[name].src === s) { delete this._holds[name]; delete this._stemSrc["hold_" + name]; } g.disconnect(); };
    return true;
  }

  // Stem slices: pieces of ONE stem played back to back on the audio clock while the other stems play
  // on. slices: [{from, dur, off, gain}] in SONG seconds ({from, dur}: what to play; off: where in the
  // window it sits, default right after the previous piece; gain: 0..1.2, default 1). The window starts
  // at audio time `at`; the live stem is muted across it and comes back on the last edge. Every piece
  // has ~6 ms edges (no clicks). Needs stem mode. Hard cap SLICE_MAX_S: a longer window is refused,
  // nothing is armed. -> {ok, until} or false.
  stemSlices(name, slices, at, xf = 0.008) {
    const st = this.stems;
    if (!st || !st[name] || !this.stemsReady || !Array.isArray(slices) || !slices.length || this._holds[name] || this._slices[name]) return false;
    const now = audioCtx.currentTime, rate = this._playbackRate(), k = st.ratio || 1;
    if (!(at >= now - 0.005) || !(rate > 0)) return false;
    let cur = 0, span = 0;
    const plan = [];
    for (const s of slices) {
      if (!(s.dur > 0) || !(s.from >= 0)) return false;
      const off = Number.isFinite(s.off) ? s.off : cur;
      plan.push({ from: s.from, dur: s.dur, off, gain: Math.max(0, Math.min(1.2, s.gain == null ? 1 : s.gain)) });
      cur = off + s.dur;
      span = Math.max(span, cur);
    }
    if (span / rate > SLICE_MAX_S) return false;
    const until = at + span / rate;
    const srcs = [];
    plan.forEach((p, i) => {
      const t0 = at + p.off / rate, t1 = t0 + p.dur / rate, e = Math.min(xf, (t1 - t0) / 3);
      const s = audioCtx.createBufferSource();
      s.buffer = st[name];
      s._rateMul = k;
      s.playbackRate.value = rate * k;
      if (this._rateRamp) this._scheduleRate(s.playbackRate, k, audioCtx.currentTime);
      const g = audioCtx.createGain();
      g.gain.setValueAtTime(0, t0);
      g.gain.linearRampToValueAtTime(p.gain, t0 + e);
      g.gain.setValueAtTime(p.gain, t1 - e);
      g.gain.linearRampToValueAtTime(0, t1);
      s.connect(g); g.connect(this.stemGain[name]);
      s.start(t0, Math.max(0, Math.min((p.from + st.lag) * k, st[name].duration - 0.01)));
      s.stop(t1 + 0.02);
      const key = `slice_${name}_${i}`;
      this._stemSrc[key] = s;
      s.onended = () => { if (this._stemSrc[key] === s) delete this._stemSrc[key]; g.disconnect(); };
      srcs.push(s);
    });
    const live = this.stemLive[name].gain;
    live.cancelScheduledValues(at);
    live.setValueAtTime(1, at); live.linearRampToValueAtTime(0, at + xf);
    live.setValueAtTime(0, until - xf); live.linearRampToValueAtTime(1, until);
    const rec = { srcs, until };
    this._slices[name] = rec;
    setTimeout(() => { if (this._slices[name] === rec) delete this._slices[name]; }, Math.max(0, (until - now) * 1000) + 60);
    return { ok: true, until };
  }

  // Stop a running / booked slice window: pieces stop, the live stem is back within 30 ms.
  releaseSlices(name, at = 0) {
    const h = this._slices[name];
    if (!h) return;
    const t = Math.max(audioCtx.currentTime, at || 0);
    for (const s of h.srcs) { try { s.stop(t + 0.03); } catch (e) { /* already stopped */ } }
    const live = this.stemLive[name].gain;
    live.cancelScheduledValues(t); live.setValueAtTime(live.value, t); live.linearRampToValueAtTime(1, t + 0.03);
    delete this._slices[name];
  }

  releaseHold(name, at = 0) {
    const h = this._holds[name];
    if (!h) return;
    const t = Math.max(audioCtx.currentTime, at || 0);
    try { h.src.stop(t + 0.03); } catch (e) { /* already stopped */ }
    const live = this.stemLive[name].gain;
    live.cancelScheduledValues(t); live.setValueAtTime(live.value, t); live.linearRampToValueAtTime(1, t + 0.03);
    delete this._holds[name];
    delete this._stemSrc["hold_" + name];
  }

  // Slip loop (artist move S13, Beat Masher / Flux style): loop `beats` from the
  // current position while a shadow playhead keeps the song's own timeline
  // running underneath. slipRelease() lands on the shadow, where the track would
  // have been had the loop never played, so the phrase grid is not lost.
  // `start` (song s, default now) is the grid point the loop begins on; a call more than
  // 0.1 s off it is late and refused. The shadow starts from the real playhead, so the
  // song's timeline is never moved. Refused (false) with no buffer, stopped, reversed or
  // a loop already on.
  slipLoop(beats, start) {
    if (!this.buffer || !this.playing || this.reversed || this.loopOn || this._slip || !(beats > 0)) return false;
    const pos = this._currentPosition(), from = start == null ? pos : start;
    if (Math.abs(from - pos) > 0.1) return false;
    const prevBeats = this.loopBeats;
    this.loopBeats = beats;
    this.loopOn = true;
    this.play(from);
    // the shadow runs from the real playhead at the rate in force (rebased on every rate change / glide)
    this._slip = { pos, at: this.startedAt, rate: this._playbackRate(), prevBeats };
    return true;
  }

  // Where the song would be now (null when no slip loop runs): the rate held since the last rebase, or the
  // glide's own integral (_travelled) while one runs from that rebase.
  slipPosition() {
    const s = this._slip;
    if (!s) return null;
    const T = audioCtx.currentTime;
    const run = this._rateRamp && this.startedAt === s.at ? this._travelled(T) : Math.max(0, T - s.at) * s.rate;
    return this._clampPos(s.pos + run);
  }
  _slipRebase(shadow) {   // after the deck clock was rebased at startedAt
    if (this._slip) Object.assign(this._slip, { pos: shadow, at: this.startedAt, rate: this._playbackRate() });
  }

  // Leave the slip loop onto the shadow playhead. Returns the landing position or null.
  slipRelease() {
    const s = this._slip;
    if (!s) return null;
    if (!this.loopOn) { this._dropSlip(); return null; }   // the loop was already taken off
    const to = this.slipPosition();
    this._slip = null;
    this.loopOn = false;
    this.loopBeats = s.prevBeats;
    if (this.playing) this.play(to);
    return to;
  }

  // Forget the slip shadow without moving (someone else took the loop over).
  _dropSlip() {
    if (!this._slip) return;
    this.loopBeats = this._slip.prevBeats;
    this._slip = null;
  }

  // Layer pieces of ANY buffer (this deck's own stem, or the next track's stem)
  // into this deck's channel on the audio clock, band-limited by a high-pass,
  // under the live signal (nothing is muted). Artist moves S12 (roll at low wet)
  // and S14 (cue-tease stabs). pieces: [{from, dur, at}] in buffer seconds / audio
  // time; `rate` plays the buffer at this deck's heard tempo. Every piece has
  // >= 6 ms edges. -> {ok, until} or false (nothing armed).
  layerPieces(buffer, pieces, opts = {}) {
    const now = audioCtx.currentTime, rate = opts.rate || 1, gain = Math.max(0, Math.min(1, opts.gain == null ? 0.3 : opts.gain));
    if (!buffer || !this.playing || !Array.isArray(pieces) || !pieces.length || !(rate > 0)) return false;
    if (pieces.some((p) => !(p.dur > 0) || !(p.from >= 0) || !(p.at >= now - 0.005))) return false;
    const hp = audioCtx.createBiquadFilter();
    hp.type = "highpass";
    hp.frequency.value = Math.max(120, opts.hpHz || 150);
    hp.connect(this.inputGain);
    const srcs = [];
    let until = now;
    for (const p of pieces) {
      const t1 = p.at + p.dur / rate, e = Math.max(0.006, Math.min(0.02, (t1 - p.at) / 3));
      const s = audioCtx.createBufferSource();
      s.buffer = buffer;
      s.playbackRate.value = rate;
      const g = audioCtx.createGain();
      g.gain.setValueAtTime(0, p.at);
      g.gain.linearRampToValueAtTime(gain, p.at + e);
      g.gain.setValueAtTime(gain, t1 - e);
      g.gain.linearRampToValueAtTime(0, t1);
      s.connect(g); g.connect(hp);
      s.start(p.at, Math.max(0, Math.min(p.from, buffer.duration - 0.01)));
      s.stop(t1 + 0.02);
      srcs.push(s);
      until = Math.max(until, t1);
    }
    const rec = { srcs, hp, until };
    (this._layers = this._layers || []).push(rec);
    setTimeout(() => { this._layers = (this._layers || []).filter((r) => r !== rec); try { hp.disconnect(); } catch (e) { /* gone */ } },
      Math.max(0, (until - now) * 1000) + 100);
    return { ok: true, until };
  }

  // Stop every booked layerPieces window now.
  releaseLayers() {
    const t = audioCtx.currentTime;
    for (const r of this._layers || []) for (const s of r.srcs) { try { s.stop(t); } catch (e) { /* already stopped */ } }
    this._layers = [];
  }

  _emitStem(state) {
    if (typeof window.dispatchEvent === "function") {
      window.dispatchEvent(new CustomEvent("ai-activity", { detail: { kind: "stems", deck: this.id, state } }));
    }
  }

  setPitchPercent(pct) {
    this._pitchPercent = pct;
    this._applyRate();
  }

  // Does this deck reach the master (tempo-rule.js isAudible)?
  onMaster() {
    const rule = window.tempoRule;
    const s = { playing: !!this.playing, side: this.crossfaderGain ? this.crossfaderGain.gain.value : 1,
      volume: this.volumeGain ? this.volumeGain.gain.value : 1 };
    return rule ? rule.isAudible(s) : s.playing;
  }

  // Seconds a glide to `pct` must take under the gradient rule (0: no real
  // change). Never returns 0 for an actual change: a silent deck still
  // glides, just fast (5 ms, inaudible to nobody) instead of a literal jump.
  tempoGlideSeconds(pct) {
    const rule = window.tempoRule;
    if (!rule) return 0;
    if (Math.abs((pct || 0) - this._pitchPercent) < (rule.STILL_PCT || 0.05)) return 0;
    if (!this.onMaster()) return 0.005;
    return rule.glideSeconds(this._pitchPercent, pct, this.bpm, this._playbackRate());
  }

  // Every AI tempo change goes through here: a fast glide on a silent deck,
  // a full gradient on one the room hears. No deck ever gets a literal
  // instant jump. The user's own fader keeps setPitchPercent.
  aiSetPitch(pct) {
    const g = this.tempoGlideSeconds(pct);
    if (g > 0 && typeof this.rampPitchPercent === "function") this.rampPitchPercent(pct, g);
    else this.setPitchPercent(pct);
    return g;
  }

  // Temporary tempo nudge (press-and-hold), layered on top of the fader.
  setBendPercent(pct) {
    this._bendPercent = pct;
    this._applyRate();
  }

  setVolume(v) {
    this.volumeGain.gain.value = v;
  }

  setEQ(band, db) {
    const filter = band === "low" ? this.lowFilter : band === "mid" ? this.midFilter : this.highFilter;
    filter.gain.value = db;
  }

  setCrossfaderGain(g) {
    this.crossfaderGain.gain.value = g;
  }

  // `spin` is true only for the transport PLAY action. Every internal restart
  // (seek, hot-cue jump, loop re-arm, reverse) passes it as false so those stay
  // instant.
  // `when` (audioCtx time, optional): start sample-accurately at that moment
  // instead of now. The autopilot uses it to land the incoming downbeat
  // exactly on the outgoing deck's phrase line (beat-to-beat blends).
  play(fromPosition, spin = false, when = 0) {
    if (!this.buffer) return;
    if (audioCtx.state === "suspended") audioCtx.resume();
    this._cancelBrake();
    this._cancelSpinUp();
    // a glide still running survives internal restarts (loop re-arm, seek,
    // hot cue); the transport's spin-up starts from the fader's rate instead
    const glide = !spin && this.playing && this._rateRamp && this._rateRamp.t1 > audioCtx.currentTime ? this._rateRamp : null;
    this._stopSource();
    const pos = this._clampPos(fromPosition !== undefined ? fromPosition : this._currentPosition());
    if (!glide) this._rateRamp = null;
    const duration = this.buffer.duration;
    if (this.reversed) this._ensureReverseBuffer();
    const buffer = this._activeBuffer();
    // In the reversed buffer, original time t lives at (duration - t).
    const bufPos = this.reversed ? duration - pos : pos;
    const src = audioCtx.createBufferSource();
    src.buffer = buffer;
    src.playbackRate.value = this._playbackRate();
    if (this.loopOn) {
      const secondsPerBeat = 60 / (this.bpm || 128);
      src.loop = true;
      src.loopStart = bufPos;
      src.loopEnd = Math.min(bufPos + this.loopBeats * secondsPerBeat, duration);
    }
    // Forward loops report a wrapped position, so the clock (cursor, phrase
    // grid, autopilot exit timing) stays inside the loop instead of running on.
    this._loopSpan = this.loopOn && !this.reversed ? [pos, Math.min(pos + this.loopBeats * 60 / (this.bpm || 128), duration)] : null;
    src.connect(this.mixGain);
    const startAt = when && when > audioCtx.currentTime ? when : 0;
    src.start(startAt, Math.max(0, Math.min(bufPos, duration - 0.01)));
    src._startAt = startAt || audioCtx.currentTime;
    src.onended = () => { src._dead = true; };
    this.source = src;
    // Stems ride along sample-locked (reversed playback: full mix only).
    if (this.stems && !this.reversed) {
      for (const n of STEM_NAMES) this._startStem(n, src._startAt, pos, src);
      if (this.tempoStems && !this.stemState) this.stemMix({ drums: 1, bass: 1, vocals: 1, other: 1, bus: 0 }, src._startAt, 0.005);
    }
    // stem mode carried over but the stems didn't start (reversed, missing): full mix
    if (this.stemState && !this.stemsLiveAt(src._startAt)) this._fallbackToMix();
    this.startedAt = startAt || audioCtx.currentTime;
    this.startOffset = pos;
    this.playing = true;
    if (glide) {
      // re-anchor the glide on the new clock (it must not begin before startedAt)
      const g0 = Math.max(this.startedAt, glide.t0);
      this._rateRamp = { t0: g0, t1: glide.t1, r0: this._rampRateAt(g0), r1: glide.r1 };
      for (const s of this._allSources()) this._scheduleRate(s.playbackRate, s._rateMul || 1, audioCtx.currentTime);
    }

    if (spin) {
      const now = audioCtx.currentTime;
      const target = this._playbackRate();
      for (const s of this._allSources()) {
        const p = s.playbackRate;
        p.cancelScheduledValues(now);
        p.setValueAtTime(0.0001, now);
        p.linearRampToValueAtTime(target * (s._rateMul || 1), now + SPIN_UP_SECONDS);
      }
      this._spinningUp = true;
      this._spinUpStartedAt = now;
      this._spinUpRate = target;
      this._spinUpPos = pos;
      this._spinUpTimer = setTimeout(() => {
        // Rebase the position clock: during the ramp the platter covered less
        // ground than rate * elapsed, so hand the steady-state maths the real
        // position it reached.
        const reached = this._currentPosition();
        this._spinningUp = false;
        this._spinUpTimer = null;
        this.startOffset = reached;
        this.startedAt = audioCtx.currentTime;
      }, SPIN_UP_SECONDS * 1000);
    }
    this._syncWavesurferCursor();
    if (this.onPlayStateChange) this.onPlayStateChange(true);
  }

  // PLAY/PAUSE pause = a short vinyl spin-down, not an instant cut.
  pause() {
    if (!this.playing || this._braking) return;
    this._rampToStop(SPIN_DOWN_SECONDS);
  }

  // Instant stop, used where a hard cut is correct (CUE, track swap).
  stopNow() {
    if (!this.playing) return;
    this.startOffset = this._currentPosition();
    this._rateRamp = null;
    this._cancelBrake();
    this._cancelSpinUp();
    this._stopSource();
    this.releaseLayers();
    this.playing = false;
    if (this._slip) { this.loopOn = false; this.loopBeats = this._slip.prevBeats; this._slip = null; }
    if (this.onPlayStateChange) this.onPlayStateChange(false);
  }

  _stopSource() {
    if (this.source) {
      try { this.source.stop(); } catch (e) { /* already stopped */ }
      this.source.disconnect();
      this.source = null;
    }
    this._stopStems();
  }

  toggle() {
    if (this.playing && !this._braking) {
      this.pause();
      return false;
    }
    this.play(undefined, true); // transport play -> spin the platter up
    return this.playing;
  }

  cue() {
    this._cancelBrake();
    this._cancelSpinUp();
    this._stopSource();
    this.playing = false;
    this.startOffset = this.cuePoint || this.trimStart;
    this._syncWavesurferCursor();
    if (this.onPlayStateChange) this.onPlayStateChange(false);
  }

  setCueHere() {
    this.cuePoint = this._currentPosition();
  }

  setHotCue(n) {
    if (this.hotCues[n] === null || this.hotCues[n] === undefined) {
      this.hotCues[n] = this._currentPosition();
      return "set";
    }
    this.play(this.hotCues[n]);
    return "jump";
  }

  // Key-locked stems at `bpm` (multi-BPM stem sets, server-rendered and cached).
  // While on, the deck plays its stems instead of its pitched mix: the pitch
  // fader moves tempo, not key. Resolves true when attached (or already on).
  async useTempoStems(bpm) {
    const tid = this.id === "a" ? state.trackA : state.trackB;
    if (!tid || !this.buffer || !this.stems) return false;
    if (this.tempoStems && Math.abs(this.tempoStems.bpm - bpm) < 0.5) return true;
    const bufs = await this.fetchTempoStems(bpm, 180);
    if (!bufs) return false;
    this._nativeStems = this._nativeStems || this.stems;
    this.tempoStems = { bpm: bufs.bpm, ratio: bufs.ratio };
    this.setStems(bufs);
    return true;
  }

  // Decode this song's stems key-locked at `bpm` WITHOUT attaching them
  // (polls while the server renders, up to maxWaitS). Resolves the buffer set
  // ({drums, bass, vocals, other, lag, ratio, bpm}) or null: no track / no
  // native stems / render refused (Rubber Band missing, gap too big) / the
  // deck loaded another song meanwhile / timed out.
  async fetchTempoStems(bpm, maxWaitS = 120) {
    const tid = this.id === "a" ? state.trackA : state.trackB;
    const nat = this._nativeStems || this.stems;
    if (!tid || !this.buffer || !nat || !(bpm > 0)) return null;
    const analysis = this.analysis;
    const t0 = Date.now();
    try {
      while (Date.now() - t0 < maxWaitS * 1000) {
        const res = await fetch(`/api/tracks/${tid}/stems?bpm=${bpm.toFixed(2)}&separate=1`);
        if (!res.ok) return null;
        const v = await res.json();
        if (this.analysis !== analysis) return null;
        if (v.stems) {
          const bufs = {};
          await Promise.all(STEM_NAMES.map(async (n) => {
            bufs[n] = await audioCtx.decodeAudioData(await (await fetch(v.stems[n])).arrayBuffer());
          }));
          if (this.analysis !== analysis) return null;
          bufs.lag = nat.lag || 0;
          bufs.ratio = v.ratio || 1;
          bufs.bpm = v.bpm || bpm;
          return bufs;
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    } catch (e) { console.warn("tempo stems:", e.message); }
    return null;
  }

  // One step of the tempo ladder, on the audio clock at `at` (a phrase line):
  // the deck's tempo becomes `pct` and its stems become `bufs` (key-locked at
  // exactly that tempo, so they play at rate 1: the key never moves), or
  // bufs = null for the native stems at pct 0 (then the full mix is back).
  // The rate step lands in the last 10 ms before the line, while the old set
  // still plays; the new set starts on the line itself.
  swapTempoStemsAt(bufs, pct, at) {
    if (!this.playing) return false;
    const T = Math.max(audioCtx.currentTime + 0.16, at || 0);
    const nat = this._nativeStems || (this.tempoStems ? null : this.stems);
    if (!bufs && !nat) return false;
    // Gradient rule: the heard tempo glides into the step (ending on the line,
    // or as soon after as the max rate allows) instead of a 10 ms jump. The
    // swap itself is then tempo-neutral: only the stems' key snaps back.
    const g = this.tempoGlideSeconds(pct);
    if (g > 0) this.rampPitchPercent(pct, g, Math.max(audioCtx.currentTime, T - g));
    else this.rampPitchPercent(pct, 0.01, T - 0.01);
    if (bufs) {
      this._nativeStems = nat;
      this.tempoStems = { bpm: bufs.bpm, ratio: bufs.ratio };
      this.setStems(bufs, T);
    } else {
      this.tempoStems = null;
      this._nativeStems = null;
      this.setStems(nat, T);
      if (this.stemState) this.stemMix(null, T + 0.01, 0.02);
    }
    return true;
  }

  // Back to the native stems (and the full mix) on the next start/now. Never
  // ends stemless: no cached native set means re-fetching it (the song's
  // stems are on the server already, this deck just dropped its copy).
  dropTempoStems() {
    if (!this.tempoStems) return;
    this.tempoStems = null;
    const nat = this._nativeStems;
    this._nativeStems = null;
    this.setStems(nat || null);
    this.stemMix(null, 0, 0.02);
    if (!nat && this.analysis) {
      const tid = this.id === "a" ? state.trackA : state.trackB;
      console.warn(`deck ${this.id}: no native stems cached after the tempo set, re-fetching`);
      if (tid) this._loadVocals(tid, this.analysis);
    }
  }

  // Live stems + vocal map. The analysis carries no vocal regions unless a
  // stem exists, so the server separates the song (4 stems) in the background
  // while it plays; the deck polls, decodes the stems, measures their lag
  // against the mix and attaches them sample-locked.
  async _loadVocals(trackId, analysis) {
    for (let i = 0; i < 200; i++) {                      // ~10 min of polling (3 s apart)
      if (this.analysis !== analysis) return;            // another song loaded
      try {
        const res = await fetch(`/api/tracks/${trackId}/stems?separate=1`);
        if (!res.ok) return;
        const v = await res.json();
        if (v.stems) {
          if (!(analysis.vocal_active_regions && analysis.vocal_active_regions.length)) {
            const vr = await (await fetch(`/api/tracks/${trackId}/vocals`)).json();
            if (Array.isArray(vr.regions)) analysis.vocal_active_regions = vr.regions;
          }
          if (this.analysis !== analysis) return;
          const bufs = {};
          await Promise.all(STEM_NAMES.map(async (n) => {
            bufs[n] = await audioCtx.decodeAudioData(await (await fetch(v.stems[n])).arrayBuffer());
          }));
          if (this.analysis !== analysis || !this.buffer) return;
          bufs.lag = await stemLagAsync(this.buffer, bufs);
          this.setStems(bufs);
          return;
        }
      } catch (e) { console.warn("stems:", e.message); return; }
      await new Promise((r) => setTimeout(r, 3000));     // stems land on the deck within ~3 s of the server having them
    }
  }

  toggleLoop() {
    const pos = this._currentPosition(); // read while the loop still wraps
    this._dropSlip();                    // the listener takes the loop over
    this.loopOn = !this.loopOn;
    if (this.playing) this.play(pos);
    return this.loopOn;
  }

  setLoopBeats(n) {
    this.loopBeats = n;
  }

  setTrim(start, end) {
    this.trimStart = start;
    this.trimEnd = end;
  }

  scrubBy(deltaSeconds) {
    this.seek(this._currentPosition() + deltaSeconds, { user: true });   // the jog wheel
  }

  // opts.user: the listener clicked / scrubbed / beat-jumped: always honoured.
  // Anything else (the AI) may not move a song that reaches the master (user:
  // "should not change song position with a track contributing to master, wait
  // for the position to reach"): refused unless it is no audible jump (under
  // 80 ms) or a loop's own exit at its end. Returns false when refused.
  seek(position, opts = {}) {
    if (!this.buffer) return false;
    const pos = this._clampPos(position);
    if (opts.user && this._slip) { this._dropSlip(); this.loopOn = false; }   // a scrub ends the AI's slip loop
    if (this.playing && !opts.user && this._onAir()) {
      const cur = this._currentPosition(), end = this._loopSpan && this._loopSpan[1];
      // natural: inaudible, a loop's own end, or a slip release onto the song's own timeline
      const natural = Math.abs(pos - cur) < 0.08 ||
        (opts.loopExit && end != null && Math.abs(pos - end) < 0.15) ||
        (opts.loopExit && opts.slipTo != null && Math.abs(pos - opts.slipTo) < 0.15);
      if (!natural) {
        console.warn(`deck ${this.id}: seek to ${pos.toFixed(2)}s refused (on the master at ${cur.toFixed(2)}s: wait for it)`);
        window.dispatchEvent(new CustomEvent("seek-refused", { detail: { deck: this.id, from: cur, to: pos, why: opts.why || "" } }));
        return false;
      }
    }
    if (this.playing) this.play(pos);
    else {
      this.startOffset = pos;
      this._syncWavesurferCursor();
    }
    return true;
  }

  // Beatjump: move ±n beats at the track's own tempo. A running loop is
  // re-armed at the new position (the loop is length-from-here by design).
  jumpBeats(n, opts = {}) {
    if (!this.buffer) return 0;
    const seconds = n * (60 / (this.bpm || 128));
    return this.seek(this._currentPosition() + seconds, opts) ? seconds : 0;
  }

  setGain(v) {
    this.inputGain.gain.value = v;
  }

  toggleReverse() {
    this._setReversed(!this.reversed);
    return this.reversed;
  }

  _setReversed(on) {
    if (on === this.reversed) return;
    const pos = this._currentPosition();
    if (this._slip) { this._dropSlip(); this.loopOn = false; }   // no slip shadow runs backwards
    const wasPlaying = this.playing;
    if (on) this._ensureReverseBuffer();
    if (wasPlaying) {
      this._stopSource();
      this.playing = false;
    }
    this.reversed = on;
    this.startOffset = pos;
    if (wasPlaying) this.play(pos);
    else this._syncWavesurferCursor();
  }

  // Turntable brake: ramp playbackRate to (near) zero instead of cutting.
  brake() {
    return this._rampToStop(BRAKE_SECONDS);
  }

  _rampToStop(seconds) {
    if (!this.playing || this._braking) return false;
    this._cancelSpinUp();
    const rate = this._playbackRate();
    // Capture the position BEFORE flipping _braking on -- _currentPosition()
    // switches to the deceleration integral as soon as that flag is set.
    this._brakeStartPos = this._currentPosition();
    this._rateRamp = null;                 // the brake's own ramp takes over from `rate`
    this._brakeStartRate = rate;
    this._brakeStartedAt = audioCtx.currentTime;
    this._brakeDur = seconds;
    this._braking = true;
    // AudioParam playbackRate of exactly 0 is legal but some engines stall on
    // it, so ramp to a hair above zero and then hard-stop.
    for (const s of this._allSources()) {
      const p = s.playbackRate;
      p.cancelScheduledValues(audioCtx.currentTime);
      p.setValueAtTime(rate * (s._rateMul || 1), audioCtx.currentTime);
      p.linearRampToValueAtTime(0.0001, audioCtx.currentTime + seconds);
    }
    this._brakeTimer = setTimeout(() => {
      const finalPos = this._currentPosition();
      this._braking = false;
      this._brakeTimer = null;
      this._stopSource();
      this.playing = false;
      this.startOffset = finalPos;
      this._syncWavesurferCursor();
      if (this.onPlayStateChange) this.onPlayStateChange(false);
    }, seconds * 1000);
    return true;
  }

  _cancelSpinUp() {
    if (this._spinUpTimer) clearTimeout(this._spinUpTimer);
    this._spinUpTimer = null;
    this._spinningUp = false;
  }

  _cancelBrake() {
    if (this._brakeTimer) clearTimeout(this._brakeTimer);
    this._brakeTimer = null;
    this._braking = false;
  }

  _syncWavesurferCursor() {
    if (this.wavesurfer && this.buffer) {
      this.wavesurfer.seekTo(this.startOffset / this.buffer.duration);
    }
  }
}

const decks = {
  a: new Deck("a", waveformA),
  b: new Deck("b", waveformB),
};
window.decks = decks;

// --- Trim regions (WaveSurfer Regions plugin) --------------------------------

function setupTrimRegion(deckId, wavesurfer) {
  const RegionsCtor = (window.WaveSurfer && window.WaveSurfer.Regions) || window.WaveSurferRegions || window.RegionsPlugin;
  if (!RegionsCtor) {
    console.warn("WaveSurfer Regions plugin not found on window; trim UI disabled.");
    return;
  }
  const regionsPlugin = wavesurfer.registerPlugin(RegionsCtor.create());
  const readout = document.getElementById(`trim-${deckId}`);

  wavesurfer.on("ready", () => {
    regionsPlugin.clearRegions();
    const duration = wavesurfer.getDuration();
    // Inset very slightly from the true edges so the resize handles never
    // sit flush against the waveform container's own edge (which made them
    // easy to miss with a pointer drag).
    const inset = duration * 0.002;
    regionsPlugin.addRegion({
      id: "trim",
      start: inset,
      end: duration - inset,
      color: deckId === "a" ? "rgba(0, 255, 102, 0.10)" : "rgba(255, 43, 214, 0.10)",
      drag: false,
      resize: true,
    });
  });

  regionsPlugin.on("region-updated", (region) => {
    if (region.id !== "trim") return;
    decks[deckId].setTrim(region.start, region.end);
    readout.textContent = `Trim ${deckId.toUpperCase()}: ${region.start.toFixed(1)}s – ${region.end.toFixed(1)}s`;
  });
}

setupTrimRegion("a", waveformA);
setupTrimRegion("b", waveformB);

// --- Jog wheels: click to play/pause, drag to scrub, spin while playing -----

document.querySelectorAll(".jog-wheel").forEach((wheel) => {
  const deckId = wheel.dataset.deck;
  const deck = decks[deckId];

  let dragging = false;
  let dragStartX = 0;
  let movedWhileDown = false;

  const tonearm = document.querySelector(`.tonearm[data-deck="${deckId}"]`);

  const setSpinSpeed = () => {
    const bpm = deck.bpm || 128;
    const rate = 1 + deck._pitchPercent / 100;
    const secondsPerRev = Math.max(0.4, (60 / bpm) * 8 / rate); // ~2 bars per revolution, tempo-scaled
    wheel.querySelectorAll(".jog-grooves, .jog-indicator").forEach((el) => {
      el.style.animationDuration = `${secondsPerRev}s`;
    });
  };

  const onAirLamp = document.getElementById(`onair-${deckId}`);

  deck.onPlayStateChange = (isPlaying) => {
    wheel.classList.toggle("spinning", isPlaying);
    if (onAirLamp) onAirLamp.classList.toggle("lit", isPlaying);
    if (isPlaying) setSpinSpeed();
    if (tonearm) tonearm.classList.toggle("engaged", isPlaying);
    document.querySelectorAll(`.deck-btn.play-btn[data-deck="${deckId}"]`).forEach((btn) => {
      btn.querySelector(".icon-play").hidden = isPlaying;
      btn.querySelector(".icon-pause").hidden = !isPlaying;
      btn.classList.toggle("active", isPlaying);
    });
  };

  wheel.addEventListener("pointerdown", (e) => {
    dragging = true;
    movedWhileDown = false;
    dragStartX = e.clientX;
    wheel.setPointerCapture(e.pointerId);
  });

  wheel.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const deltaX = e.clientX - dragStartX;
    if (Math.abs(deltaX) > 3) {
      movedWhileDown = true;
      deck.scrubBy(deltaX / 100); // ~100px drag = 1 second scrub
      dragStartX = e.clientX;
    }
  });

  const endDrag = () => {
    if (!dragging) return;
    dragging = false;
    if (!movedWhileDown) {
      const isPlaying = deck.toggle();
      wheel.classList.toggle("spinning", isPlaying);
      if (isPlaying) setSpinSpeed();
    }
  };
  wheel.addEventListener("pointerup", endDrag);
  wheel.addEventListener("pointercancel", endDrag);
});

// --- Rotary EQ knobs: vertical drag rotates the knob + updates the value ----

// Every rotary control on the console -- EQ, TRIM/gain, FX wet pots, sampler
// pad volumes -- goes through this. The gesture is a press-anywhere-on-the-knob
// vertical drag (up = increase), which is what works on a touchscreen where a
// 30px knob is too small to aim a native slider thumb at. The <input range>
// underneath is pointer-events:none and exists only as the value store, so it
// never competes with the drag; arrow keys on the focused knob still work.
function initKnob(wrap) {
  if (wrap.dataset.knobReady === "1") return;
  wrap.dataset.knobReady = "1";
  const input = wrap.querySelector(".knob-input");
  const pointer = wrap.querySelector(".knob-pointer");
  if (!input || !pointer) return;
  const min = parseFloat(input.min);
  const max = parseFloat(input.max);
  const step = parseFloat(input.step) || 0.01;

  if (!wrap.hasAttribute("tabindex")) wrap.setAttribute("tabindex", "0");
  wrap.setAttribute("role", "slider");
  wrap.setAttribute("aria-label", input.getAttribute("aria-label") || "knob");

  const renderPointer = () => {
    const value = parseFloat(input.value);
    const pct = (value - min) / (max - min); // 0..1
    const angle = -135 + pct * 270; // -135deg (min) .. +135deg (max)
    pointer.style.transform = `rotate(${angle}deg)`;
    wrap.setAttribute("aria-valuenow", value.toFixed(2));
  };

  const commit = (value) => {
    input.value = Math.min(max, Math.max(min, value)).toFixed(3);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    renderPointer();
  };

  renderPointer();

  let dragging = false;
  let startY = 0;
  let startVal = parseFloat(input.value);

  wrap.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    dragging = true;
    startY = e.clientY;
    startVal = parseFloat(input.value);
    wrap.classList.add("turning");
    wrap.setPointerCapture(e.pointerId);
  });

  wrap.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const deltaY = startY - e.clientY; // drag up = increase
    // Fine mode: hold Shift for a 4x slower sweep.
    const travel = e.shiftKey ? 480 : 120;
    commit(startVal + (deltaY / travel) * (max - min));
  });

  const endDrag = () => {
    dragging = false;
    wrap.classList.remove("turning");
  };
  wrap.addEventListener("pointerup", endDrag);
  wrap.addEventListener("pointercancel", endDrag);
  wrap.addEventListener("lostpointercapture", endDrag);

  wrap.addEventListener("keydown", (e) => {
    const big = (max - min) / 20;
    if (e.key === "ArrowUp" || e.key === "ArrowRight") commit(parseFloat(input.value) + step);
    else if (e.key === "ArrowDown" || e.key === "ArrowLeft") commit(parseFloat(input.value) - step);
    else if (e.key === "PageUp") commit(parseFloat(input.value) + big);
    else if (e.key === "PageDown") commit(parseFloat(input.value) - big);
    else return;
    e.preventDefault();
  });

  input.addEventListener("input", renderPointer);
}

document.querySelectorAll(".knob-wrap").forEach(initKnob);

// --- Wire up transport / loop / hot cue / fader / crossfader controls -------

document.querySelectorAll(".deck-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const deckId = btn.dataset.deck;
    const deck = decks[deckId];
    const action = btn.dataset.action;
    if (action === "play") {
      deck.toggle(); // onPlayStateChange handles button text + jog spin
    } else if (action === "cue") {
      deck.cue();
    } else if (action === "loop-toggle") {
      const on = deck.toggleLoop();
      btn.classList.toggle("loop-active", on);
      btn.textContent = on ? "ON" : "OFF";
    } else if (action === "reverse") {
      const on = deck.toggleReverse();
      btn.classList.toggle("rev-active", on);
    } else if (action === "brake") {
      deck.brake();
    } else if (action === "sync") {
      const otherId = deckId === "a" ? "b" : "a";
      const other = decks[otherId];
      const otherEffectiveBpm = other.bpm * (1 + other._pitchPercent / 100);
      const targetPitchPercent = Math.max(-8, Math.min(8, (otherEffectiveBpm / deck.bpm - 1) * 100));
      deck.setPitchPercent(targetPitchPercent);
      const fader = document.querySelector(`.pitch-fader[data-deck="${deckId}"]`);
      if (fader) {
        fader.value = targetPitchPercent.toFixed(1);
        fader.dispatchEvent(new Event("input", { bubbles: true }));
      }
      // Lit only when the clamped ±8% pitch range could actually reach the
      // other deck's tempo -- it stays dark when SYNC had to decline.
      const reached = Math.abs(other.bpm * (1 + other._pitchPercent / 100) - deck.bpm * (1 + targetPitchPercent / 100)) < 0.05;
      btn.classList.toggle("synced", reached);
    }
  });
});

const LOOP_LENGTHS = [1, 2, 4, 8, 16, 32];
document.querySelectorAll(".stepper-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const deckId = btn.dataset.deck;
    const deck = decks[deckId];
    const valueEl = document.getElementById(`loop-value-${deckId}`);
    const currentIdx = LOOP_LENGTHS.indexOf(deck.loopBeats);
    const step = parseInt(btn.dataset.step, 10);
    const newIdx = Math.min(LOOP_LENGTHS.length - 1, Math.max(0, currentIdx + step));
    deck.setLoopBeats(LOOP_LENGTHS[newIdx]);
    valueEl.textContent = LOOP_LENGTHS[newIdx];
  });
});

// Hot-cue pads: the pad's own sub-label shows the real stored timestamp, and
// setting/clearing a cue redraws the badges on the waveform + overview strip.
function refreshHotCueUI(deckId) {
  const deck = decks[deckId];
  document.querySelectorAll(`.hot-cue-btn[data-deck="${deckId}"]`).forEach((btn) => {
    const t = deck.hotCues[btn.dataset.cue];
    const isSet = t !== null && t !== undefined;
    btn.classList.toggle("cue-set", isSet);
    const sub = btn.querySelector(".pad-sub");
    if (sub) sub.textContent = isSet ? formatClock(t) : "EMPTY";
  });
  deck.renderHotCueMarkers();
}

document.querySelectorAll(".hot-cue-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const deckId = btn.dataset.deck;
    decks[deckId].setHotCue(btn.dataset.cue);
    refreshHotCueUI(deckId);
  });
});

document.querySelectorAll(".cue-clear-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const deckId = btn.dataset.deck;
    decks[deckId].hotCues[btn.dataset.cue] = null;
    refreshHotCueUI(deckId);
  });
});

document.querySelectorAll(".pitch-fader").forEach((input) => {
  const apply = () => {
    const pct = parseFloat(input.value);
    decks[input.dataset.deck].setPitchPercent(pct);
    const readout = document.getElementById(`pitch-readout-${input.dataset.deck}`);
    if (readout) readout.textContent = `${pct > 0 ? "+" : ""}${pct.toFixed(1)}%`;
  };
  input.addEventListener("input", apply);
  apply();
});

// --- TAP tempo -------------------------------------------------------------
// Overrides the analyzer's BPM for one deck from your own taps. That value is
// the real one the engine uses for loop lengths, beatjump distance, the
// tempo-synced FX delays and the jog-wheel spin rate -- so it is a genuine
// control, not a display gimmick. 4+ taps required before it commits.
const tapState = {};
document.querySelectorAll(".tap-btn").forEach((btn) => {
  const deckId = btn.dataset.deck;
  tapState[deckId] = [];
  btn.addEventListener("click", () => {
    const now = performance.now();
    const taps = tapState[deckId];
    if (taps.length && now - taps[taps.length - 1] > 2000) taps.length = 0; // stale run
    taps.push(now);
    if (taps.length > 8) taps.shift();
    btn.classList.add("tapping");
    setTimeout(() => btn.classList.remove("tapping"), 120);
    if (taps.length < 4) return;
    const spans = [];
    for (let i = 1; i < taps.length; i++) spans.push(taps[i] - taps[i - 1]);
    const avg = spans.reduce((a, b) => a + b, 0) / spans.length;
    const bpm = 60000 / avg;
    if (!isFinite(bpm) || bpm < 40 || bpm > 220) return;
    decks[deckId].bpm = bpm;
    const bpmEl = document.getElementById(`bpm-${deckId}`);
    if (bpmEl) {
      bpmEl.textContent = bpm.toFixed(1);
      bpmEl.title = "BPM set by TAP (overrides the analyzer for this deck)";
    }
  });
});

// --- Waveform zoom ---------------------------------------------------------
// Real WaveSurfer zoom on both lanes at once. Past "fit", autoCenter parks the
// playhead near the middle of the lane and scrolls the waveform underneath it.
let wavePxPerSec = 0;
function applyZoom(factor) {
  [decks.a, decks.b].forEach((deck) => {
    if (!deck.buffer) return;
    try {
      const ws = deck.wavesurfer;
      const base = ws.getWrapper().clientWidth / deck.buffer.duration;
      const current = wavePxPerSec || base;
      const next = Math.max(base, Math.min(400, current * factor));
      wavePxPerSec = next;
      ws.zoom(next);
    } catch (e) { /* nothing loaded yet */ }
  });
}
const zoomIn = document.getElementById("zoom-in");
const zoomOut = document.getElementById("zoom-out");
if (zoomIn) zoomIn.addEventListener("click", () => applyZoom(1.6));
if (zoomOut) zoomOut.addEventListener("click", () => applyZoom(1 / 1.6));

// --- Mini overview scrubbers ----------------------------------------------
document.querySelectorAll(".overview").forEach((strip) => {
  strip.addEventListener("click", (e) => {
    const deck = decks[strip.dataset.deck];
    if (!deck.buffer) return;
    const rect = strip.getBoundingClientRect();
    deck.seek(((e.clientX - rect.left) / rect.width) * deck.buffer.duration, { user: true });
  });
});

// --- Harmonic readout (real Camelot keys from the analyzer) ----------------
function updateHarmonicReadout() {
  const el = document.getElementById("harmonic-readout");
  if (!el) return;
  const keyOf = (d) => (d.analysis && d.analysis.key && d.analysis.key.camelot) || null;
  const ka = keyOf(decks.a);
  const kb = keyOf(decks.b);
  if (!ka && !kb) { el.textContent = "KEY --"; return; }
  el.textContent = `KEY ${ka || "--"} / ${kb || "--"}`;
  el.classList.toggle("hud-ok", Boolean(ka && kb && ka === kb));
}

document.querySelectorAll(".volume-fader").forEach((input) => {
  input.addEventListener("input", () => {
    decks[input.dataset.deck].setVolume(parseFloat(input.value));
  });
});

// --- Gain knobs (pre-EQ trim), master volume, beatjump, pitch bend ----------

// 3-band EQ knobs -> the deck's real BiquadFilter shelves/peak.
document.querySelectorAll(".eq-knob").forEach((input) => {
  // data-ai-audio: the autopilot has scheduled this EQ on the audio clock; the
  // knob only animates (setting .value here would break the scheduled ramp).
  const apply = () => { if (!input.dataset.aiAudio) decks[input.dataset.deck].setEQ(input.dataset.band, parseFloat(input.value)); };
  input.addEventListener("input", apply);
  apply();
});

document.querySelectorAll(".gain-knob").forEach((input) => {
  const apply = () => decks[input.dataset.deck].setGain(parseFloat(input.value));
  input.addEventListener("input", apply);
  apply();
});

const masterFader = document.getElementById("master-fader");
if (masterFader) {
  const applyMaster = () => { masterGain.gain.value = parseFloat(masterFader.value); };
  masterFader.addEventListener("input", applyMaster);
  applyMaster();
}

document.querySelectorAll(".jump-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    decks[btn.dataset.deck].jumpBeats(parseInt(btn.dataset.beats, 10), { user: true });
  });
});

// Pitch bend is momentary: hold the button (or key) for the nudge, release to
// snap back to whatever the sustained pitch fader says.
function startBend(deckId, pct) {
  decks[deckId].setBendPercent(pct);
  document
    .querySelectorAll(`.bend-btn[data-deck="${deckId}"][data-bend="${pct}"]`)
    .forEach((b) => b.classList.add("active"));
}

function endBend(deckId) {
  decks[deckId].setBendPercent(0);
  document.querySelectorAll(`.bend-btn[data-deck="${deckId}"]`).forEach((b) => b.classList.remove("active"));
}

document.querySelectorAll(".bend-btn").forEach((btn) => {
  const deckId = btn.dataset.deck;
  const pct = parseFloat(btn.dataset.bend);
  btn.addEventListener("pointerdown", (e) => {
    btn.setPointerCapture(e.pointerId);
    startBend(deckId, pct);
  });
  const stop = () => endBend(deckId);
  btn.addEventListener("pointerup", stop);
  btn.addEventListener("pointercancel", stop);
  btn.addEventListener("pointerleave", stop);
});

window.addEventListener("resize", () => {
  decks.a.renderBeatGrid();
  decks.b.renderBeatGrid();
});

const crossfaderInput = document.getElementById("crossfader");
function applyCrossfader(v) {
  const x = (v + 1) / 2; // normalize -1..1 to 0..1
  const gainA = Math.cos(x * 0.5 * Math.PI);
  const gainB = Math.cos((1 - x) * 0.5 * Math.PI);
  decks.a.setCrossfaderGain(gainA);
  decks.b.setCrossfaderGain(gainB);
}
crossfaderInput.addEventListener("input", () => {
  if (!crossfaderInput.dataset.aiAudio) applyCrossfader(parseFloat(crossfaderInput.value));
});
applyCrossfader(parseFloat(crossfaderInput.value));

// --- VU meters + track time readout -------------------------------------------

const timeElA = document.getElementById("time-a");
const timeElB = document.getElementById("time-b");

function formatClock(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

// --- 12-segment LED ladders -------------------------------------------------
// Same AnalyserNode data as before; only the rendering changed from a single
// filled bar to discrete segments (green / amber above 8 / red on the last).
const VU_SEGMENTS = 12;

function buildLadder(el, count) {
  if (!el) return [];
  el.innerHTML = "";
  const segs = [];
  for (let i = 0; i < count; i++) {
    const seg = document.createElement("span");
    if (i >= count - 1) seg.classList.add("peak");
    else if (i >= count - 4) seg.classList.add("warn");
    el.appendChild(seg);
    segs.push(seg);
  }
  return segs;
}

const ladderA = buildLadder(document.getElementById("vu-a"), VU_SEGMENTS);
const ladderB = buildLadder(document.getElementById("vu-b"), VU_SEGMENTS);
const masterLadder = buildLadder(document.getElementById("master-vu"), VU_SEGMENTS);

function paintLadder(segs, level) {
  const lit = Math.round(level * segs.length);
  for (let i = 0; i < segs.length; i++) segs[i].classList.toggle("on", i < lit);
}

// Master VU taps the real master stage every deck and the sampler land on.
const masterAnalyser = audioCtx.createAnalyser();
masterAnalyser.fftSize = 256;
const masterLevelData = new Uint8Array(masterAnalyser.frequencyBinCount);
masterOut.connect(masterAnalyser);

function masterLevel() {
  masterAnalyser.getByteTimeDomainData(masterLevelData);
  let sumSquares = 0;
  for (let i = 0; i < masterLevelData.length; i++) {
    const v = (masterLevelData[i] - 128) / 128;
    sumSquares += v * v;
  }
  return Math.min(1, Math.sqrt(sumSquares / masterLevelData.length) * 3);
}

const masterBpmEl = document.getElementById("master-bpm");
const masterBpmBEl = document.getElementById("master-bpm-b");
const tempoDeltaEl = document.getElementById("tempo-delta");
const phaseSegs = Array.from(document.querySelectorAll("#phase-meter span"));
const footDecks = document.getElementById("foot-decks");
const footRate = document.getElementById("foot-rate");
if (footRate) footRate.textContent = `${(audioCtx.sampleRate / 1000).toFixed(1)} kHz`;

function effectiveBpm(deck) {
  return deck.bpm * (1 + (deck._pitchPercent + deck._bendPercent) / 100);
}

// Tempo-match meter: how close the two decks' effective tempos are, in BPM.
// Deliberately labelled TEMPO MATCH, not phase/beat alignment -- this console
// has no beat-phase alignment, so it does not claim one.
function paintTempoMatch() {
  const loaded = decks.a.buffer && decks.b.buffer;
  const delta = loaded ? effectiveBpm(decks.a) - effectiveBpm(decks.b) : null;
  if (tempoDeltaEl) tempoDeltaEl.textContent = delta === null ? "--" : `${delta > 0 ? "+" : ""}${delta.toFixed(2)} BPM`;
  const mid = Math.floor(phaseSegs.length / 2);
  phaseSegs.forEach((seg, i) => {
    seg.classList.remove("lit", "off-grid");
    if (delta === null) return;
    // ±1 BPM maps across the whole ladder; dead centre = matched.
    const offset = Math.max(-mid, Math.min(mid, Math.round(delta * mid)));
    if (i === mid + offset) {
      seg.classList.add("lit");
      if (Math.abs(delta) > 0.05) seg.classList.add("off-grid");
    }
  });
}

function paintOverview(deck) {
  if (!deck.ovPlayedEl || !deck.ovHeadEl || !deck.buffer) return;
  const pct = (deck._currentPosition() / deck.buffer.duration) * 100;
  if (Math.abs(pct - deck._lastOverviewPct) < 0.15) return;
  deck._lastOverviewPct = pct;
  deck.ovPlayedEl.style.transform = `scaleX(${pct / 100})`;
  deck.ovHeadEl.style.transform = `translateX(${pct}%)`;
}

let cursorTick = 0;

function animateFrame() {
  const levelA = decks.a.currentLevel();
  const levelB = decks.b.currentLevel();
  paintLadder(ladderA, levelA);
  paintLadder(ladderB, levelB);
  paintLadder(masterLadder, masterLevel());

  [decks.a, decks.b].forEach((deck) => {
    const timeEl = deck.id === "a" ? timeElA : timeElB;
    if (deck.buffer) {
      const pos = deck._currentPosition();
      const clock = formatClock(pos);
      if (deck._lastClock !== clock) {
        deck._lastClock = clock;
        timeEl.textContent = `${clock} / ${formatClock(deck.buffer.duration)}`;
        if (deck.jogPosEl) deck.jogPosEl.textContent = clock;
      }
      paintOverview(deck);
    }
    const bpm = deck.buffer ? effectiveBpm(deck).toFixed(1) : "";
    if (deck.bpmEl && bpm && deck._lastBpm !== bpm) {
      deck._lastBpm = bpm;
      deck.bpmEl.textContent = bpm;
    }
  });

  // Keep the WaveSurfer cursor tracking the Web Audio playhead (throttled to
  // ~10fps: this is what makes the waveform actually scroll under the playhead
  // when zoomed in, since playback is driven by AudioBufferSourceNode, not by
  // WaveSurfer's own media element).
  if (++cursorTick % 6 === 0) {
    [decks.a, decks.b].forEach((deck) => {
      if (deck.playing && deck.buffer) {
        try { deck.wavesurfer.seekTo(deck._currentPosition() / deck.buffer.duration); } catch (e) { /* not ready */ }
      }
    });
  }

  if (masterBpmEl) masterBpmEl.textContent = decks.a.buffer ? effectiveBpm(decks.a).toFixed(2) : "--.--";
  if (masterBpmBEl) masterBpmBEl.textContent = decks.b.buffer ? effectiveBpm(decks.b).toFixed(2) : "--.--";
  paintTempoMatch();
  if (footDecks) {
    footDecks.textContent =
      `DECK A: ${decks.a.buffer ? effectiveBpm(decks.a).toFixed(1) + " BPM (" + (decks.a._pitchPercent >= 0 ? "+" : "") + decks.a._pitchPercent.toFixed(2) + "%)" : "--"}` +
      ` · DECK B: ${decks.b.buffer ? effectiveBpm(decks.b).toFixed(1) + " BPM (" + (decks.b._pitchPercent >= 0 ? "+" : "") + decks.b._pitchPercent.toFixed(2) + "%)" : "--"}`;
  }

  requestAnimationFrame(animateFrame);
}
animateFrame();
