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
masterGain.connect(audioCtx.destination);

const BRAKE_SECONDS = 0.8;
// A real turntable never starts or stops instantly: PLAY/PAUSE spins the
// platter up and down. Same mechanism as the BRAKE button (a playbackRate
// ramp), just shorter and applied to the default transport action.
// CUE, hot cues, beatjump and seeking stay instant on purpose -- those are
// precision moves, not platter moves.
const SPIN_UP_SECONDS = 0.35;
const SPIN_DOWN_SECONDS = 0.45;

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
    return Math.max(0.05, 1 + (this._pitchPercent + this._bendPercent) / 100);
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
    // max(0): before a scheduled start the position holds at the cue point
    const travelled = Math.max(0, audioCtx.currentTime - this.startedAt) * this._playbackRate();
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
    if (this.playing && !this._braking) {
      this.startOffset = this._currentPosition();
      this.startedAt = audioCtx.currentTime;
    }
    if (this.source && !this._braking) {
      this.source.playbackRate.value = this._playbackRate();
    }
  }

  setPitchPercent(pct) {
    this._pitchPercent = pct;
    this._applyRate();
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
    this._stopSource();
    const pos = this._clampPos(fromPosition !== undefined ? fromPosition : this._currentPosition());
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
    src.connect(this.inputGain);
    const startAt = when && when > audioCtx.currentTime ? when : 0;
    src.start(startAt, Math.max(0, Math.min(bufPos, duration - 0.01)));
    this.source = src;
    this.startedAt = startAt || audioCtx.currentTime;
    this.startOffset = pos;
    this.playing = true;

    if (spin) {
      const now = audioCtx.currentTime;
      const target = this._playbackRate();
      const p = src.playbackRate;
      p.cancelScheduledValues(now);
      p.setValueAtTime(0.0001, now);
      p.linearRampToValueAtTime(target, now + SPIN_UP_SECONDS);
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
    this._cancelBrake();
    this._cancelSpinUp();
    this._stopSource();
    this.playing = false;
    if (this.onPlayStateChange) this.onPlayStateChange(false);
  }

  _stopSource() {
    if (this.source) {
      try { this.source.stop(); } catch (e) { /* already stopped */ }
      this.source.disconnect();
      this.source = null;
    }
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

  toggleLoop() {
    const pos = this._currentPosition(); // read while the loop still wraps
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
    this.seek(this._currentPosition() + deltaSeconds);
  }

  seek(position) {
    if (!this.buffer) return;
    const pos = this._clampPos(position);
    if (this.playing) this.play(pos);
    else {
      this.startOffset = pos;
      this._syncWavesurferCursor();
    }
  }

  // Beatjump: move ±n beats at the track's own tempo. A running loop is
  // re-armed at the new position (the loop is length-from-here by design).
  jumpBeats(n) {
    if (!this.buffer) return 0;
    const seconds = n * (60 / (this.bpm || 128));
    this.seek(this._currentPosition() + seconds);
    return seconds;
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
    this._brakeStartRate = rate;
    this._brakeStartedAt = audioCtx.currentTime;
    this._brakeDur = seconds;
    this._braking = true;
    const p = this.source.playbackRate;
    p.cancelScheduledValues(audioCtx.currentTime);
    p.setValueAtTime(rate, audioCtx.currentTime);
    // AudioParam playbackRate of exactly 0 is legal but some engines stall on
    // it, so ramp to a hair above zero and then hard-stop.
    p.linearRampToValueAtTime(0.0001, audioCtx.currentTime + seconds);
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
      color: deckId === "a" ? "rgba(0, 240, 255, 0.10)" : "rgba(255, 87, 8, 0.10)",
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
    deck.seek(((e.clientX - rect.left) / rect.width) * deck.buffer.duration);
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
  const apply = () => decks[input.dataset.deck].setEQ(input.dataset.band, parseFloat(input.value));
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
    decks[btn.dataset.deck].jumpBeats(parseInt(btn.dataset.beats, 10));
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
crossfaderInput.addEventListener("input", () => applyCrossfader(parseFloat(crossfaderInput.value)));
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
masterGain.connect(masterAnalyser);

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
