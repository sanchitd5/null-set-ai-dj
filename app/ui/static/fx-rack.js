// AI Music Brain — per-deck FX rack (spec 1.3).
//
// Each deck exposes an insert point built in deck-controller.js:
//
//   volumeGain -> fxInput -+-> fxDry ------------------+-> fxOutput -> crossfader
//                          \-> [effect chain] -> wet --/
//
// Every effect here is real Web Audio, not a placeholder: a filter sweep
// (BiquadFilterNode + LFO), echo (DelayNode with a feedback GainNode), an
// algorithmic comb-filter reverb (the live counterpart of dsp_rack.py's
// apply_reverb), flanger/phaser (short LFO-modulated delay / cascaded
// all-pass), a quantizing bit-crusher (WaveShaperNode stair-step curve), and
// a ping-pong delay (two cross-feeding delays hard-panned L/R).
//
// Depends on globals `audioCtx`, `decks` from deck-controller.js.

const FX_TYPES = {
  none: "None",
  filter: "Filter",
  echo: "Echo",
  reverb: "Reverb",
  flanger: "Flanger",
  phaser: "Phaser",
  bitcrusher: "Bit-crusher",
  pingpong: "Ping-Pong Delay",
};

function makeLFO(freq, depth, target, base) {
  const osc = audioCtx.createOscillator();
  osc.frequency.value = freq;
  const depthGain = audioCtx.createGain();
  depthGain.gain.value = depth;
  osc.connect(depthGain);
  target.value = base;
  depthGain.connect(target);
  osc.start();
  return [osc, depthGain];
}

function bitCrushCurve(levels) {
  const n = 2048;
  const curve = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const x = (i / (n - 1)) * 2 - 1;
    curve[i] = Math.round(x * levels) / levels; // stair-step quantization
  }
  return curve;
}

// Returns { input, output, nodes } — `nodes` are stopped/disconnected on swap.
function createEffect(type, bpm) {
  const input = audioCtx.createGain();
  const output = audioCtx.createGain();
  const nodes = [input, output];
  const beat = 60 / (bpm || 128);

  if (type === "filter") {
    const filter = audioCtx.createBiquadFilter();
    filter.type = "lowpass";
    filter.Q.value = 8;
    const [osc, depth] = makeLFO(0.25, 2600, filter.frequency, 3000);
    input.connect(filter);
    filter.connect(output);
    nodes.push(filter, osc, depth);
  } else if (type === "echo") {
    const delay = audioCtx.createDelay(2);
    delay.delayTime.value = Math.min(1.9, beat * 0.75); // tempo-synced 3/4 beat
    const feedback = audioCtx.createGain();
    feedback.gain.value = 0.45;
    const tone = audioCtx.createBiquadFilter();
    tone.type = "lowpass";
    tone.frequency.value = 4500;
    input.connect(delay);
    delay.connect(tone);
    tone.connect(feedback);
    feedback.connect(delay);
    delay.connect(output);
    nodes.push(delay, feedback, tone);
  } else if (type === "reverb") {
    // Comb-filter bank + a damping lowpass — same technique as the offline
    // Python reverb, running live.
    const combTimes = [0.0297, 0.0371, 0.0411, 0.0437];
    const damp = audioCtx.createBiquadFilter();
    damp.type = "lowpass";
    damp.frequency.value = 3200;
    combTimes.forEach((t) => {
      const d = audioCtx.createDelay(1);
      d.delayTime.value = t;
      const fb = audioCtx.createGain();
      fb.gain.value = 0.78;
      const lvl = audioCtx.createGain();
      lvl.gain.value = 0.25;
      input.connect(d);
      d.connect(fb);
      fb.connect(d);
      d.connect(lvl);
      lvl.connect(damp);
      nodes.push(d, fb, lvl);
    });
    damp.connect(output);
    nodes.push(damp);
  } else if (type === "flanger") {
    const delay = audioCtx.createDelay(0.05);
    const [osc, depth] = makeLFO(0.3, 0.0025, delay.delayTime, 0.005);
    const feedback = audioCtx.createGain();
    feedback.gain.value = 0.55;
    input.connect(delay);
    delay.connect(feedback);
    feedback.connect(delay);
    delay.connect(output);
    input.connect(output); // the comb notch needs the dry signal alongside
    nodes.push(delay, osc, depth, feedback);
  } else if (type === "phaser") {
    const stages = [];
    for (let i = 0; i < 4; i++) {
      const ap = audioCtx.createBiquadFilter();
      ap.type = "allpass";
      ap.Q.value = 1.2;
      stages.push(ap);
      nodes.push(ap);
    }
    const [osc, depth] = makeLFO(0.35, 700, stages[0].frequency, 900);
    stages.slice(1).forEach((ap, i) => {
      depth.connect(ap.frequency);
      ap.frequency.value = 900 + (i + 1) * 350;
    });
    input.connect(stages[0]);
    stages.reduce((prev, cur) => (prev.connect(cur), cur));
    stages[stages.length - 1].connect(output);
    input.connect(output);
    nodes.push(osc, depth);
  } else if (type === "bitcrusher") {
    const shaper = audioCtx.createWaveShaper();
    shaper.curve = bitCrushCurve(8); // ~4-bit crunch
    shaper.oversample = "none";
    const tone = audioCtx.createBiquadFilter();
    tone.type = "lowpass";
    tone.frequency.value = 6000;
    input.connect(shaper);
    shaper.connect(tone);
    tone.connect(output);
    nodes.push(shaper, tone);
  } else if (type === "pingpong") {
    const delayL = audioCtx.createDelay(2);
    const delayR = audioCtx.createDelay(2);
    delayL.delayTime.value = Math.min(1.9, beat * 0.5);
    delayR.delayTime.value = Math.min(1.9, beat * 0.5);
    const panL = audioCtx.createStereoPanner();
    panL.pan.value = -1;
    const panR = audioCtx.createStereoPanner();
    panR.pan.value = 1;
    const feedback = audioCtx.createGain();
    feedback.gain.value = 0.5;
    input.connect(delayL);
    delayL.connect(panL);
    delayL.connect(delayR);
    delayR.connect(panR);
    delayR.connect(feedback);
    feedback.connect(delayL);
    panL.connect(output);
    panR.connect(output);
    nodes.push(delayL, delayR, panL, panR, feedback);
  } else {
    input.connect(output); // "none" — a straight wire
  }

  return { input, output, nodes };
}

const WET_BAND_TYPES = ["reverb", "echo", "pingpong"];

class FXUnit {
  constructor(deck) {
    this.deck = deck;
    this.type = "none";
    this.wet = 0.6;
    this.active = false;
    this.effect = null;
    this.wetGain = audioCtx.createGain();
    this.wetGain.gain.value = 0;
    this.wetGain.connect(deck.fxOutput);
    this._applyMix();
  }

  _teardown() {
    if (!this.effect) return;
    this.deck.fxInput.disconnect(this.effect.input);
    this.effect.nodes.forEach((n) => {
      if (typeof n.stop === "function") {
        try { n.stop(); } catch (e) { /* already stopped */ }
      }
      try { n.disconnect(); } catch (e) { /* already gone */ }
    });
    this.effect = null;
  }

  _build() {
    this._teardown();
    if (this.type === "none") return;
    this.effect = createEffect(this.type, this.deck.bpm);
    this.deck.fxInput.connect(this.effect.input);
    const band = this._wetBand();
    if (!band) { this.effect.output.connect(this.wetGain); return; }
    // [S3] Angello "reverb only on the mids": the wet return is band-limited
    // (high-pass + low-pass), so a tail never carries sub-bass or hats.
    const hp = audioCtx.createBiquadFilter(), lp = audioCtx.createBiquadFilter();
    hp.type = "highpass"; hp.frequency.value = band.low;
    lp.type = "lowpass"; lp.frequency.value = band.high;
    this.effect.output.connect(hp); hp.connect(lp); lp.connect(this.wetGain);
    this.effect.nodes.push(hp, lp);
    this.band = band;
  }

  // wetBand: reverb / echo / ping-pong sends are band-limited unless the
  // console's ap-fx-wet_band (or the artist-moves master) is unchecked.
  // Edges from the deck's measured brightness (fx-moves.js bandEdges).
  _wetBand() {
    this.band = null;
    if (!WET_BAND_TYPES.includes(this.type)) return null;
    const box = (id) => { const el = typeof document !== "undefined" && document.getElementById(id); return el ? el.checked : true; };
    if (!box("ap-fx-toggle") || !box("ap-fx-wet_band")) return null;
    const fm = window.fxMoves, core = window.fxMovesCore;
    const band = fm && fm.bandFor ? fm.bandFor(this.deck) : core ? core.bandEdges(null) : { low: 300, high: 4000, fallbacks: [] };
    const fb = band.fallbacks && band.fallbacks.length ? ` (constants: ${band.fallbacks.join(", ")})` : "";
    console.info(`artist move wet_band: ${this.type} send on deck ${this.deck.id} band-limited ${band.low}-${band.high} Hz${fb}`);
    return band;
  }

  _applyMix() {
    const on = this.active && this.type !== "none";
    // Equal-ish wet/dry: fully dry when bypassed, so switching effects while
    // the deck plays never drops the signal.
    this.wetGain.gain.value = on ? this.wet : 0;
    this.deck.fxDry.gain.value = on ? 1 - this.wet * 0.85 : 1;
  }

  setType(type) {
    this.type = type;
    this._build();
    this._applyMix();
  }

  setWet(v) {
    this.wet = v;
    this._applyMix();
  }

  setActive(on) {
    this.active = on;
    if (on && !this.effect) this._build();
    this._applyMix();
    return this.active;
  }

  toggle() {
    return this.setActive(!this.active);
  }
}

const fxUnits = { a: new FXUnit(decks.a), b: new FXUnit(decks.b) };
window.fxUnits = fxUnits;

// The FX bar's effect selector is a row of hardware-style buttons rather than
// a <select>; each one sets the real FXUnit type on that deck. (A <select
// class="fx-select"> is still honoured if one is present, so either markup
// drives the same code.)
document.querySelectorAll(".fx-select").forEach((sel) => {
  sel.addEventListener("change", () => {
    const deckId = sel.dataset.deck;
    fxUnits[deckId].setType(sel.value);
    updateFxTypeButtons(deckId);
    updateFxPad(deckId);
  });
});

function updateFxTypeButtons(deckId) {
  const type = fxUnits[deckId].type;
  document.querySelectorAll(`.fx-type-btn[data-deck="${deckId}"]`).forEach((btn) => {
    btn.classList.toggle("is-on", btn.dataset.type === type);
  });
}

document.querySelectorAll(".fx-type-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const deckId = btn.dataset.deck;
    const unit = fxUnits[deckId];
    unit.setType(btn.dataset.type);
    // Picking an effect arms the unit; picking OFF disarms it, so the buttons
    // read the way a hardware FX section does.
    if (btn.dataset.type === "none") unit.setActive(false);
    else if (!unit.active) unit.setActive(true);
    const sel = document.querySelector(`.fx-select[data-deck="${deckId}"]`);
    if (sel) sel.value = btn.dataset.type;
    updateFxTypeButtons(deckId);
    updateFxPad(deckId);
  });
});

document.querySelectorAll(".fx-wet").forEach((input) => {
  const apply = () => {
    const deckId = input.dataset.deck;
    const wet = parseFloat(input.value);
    fxUnits[deckId].setWet(wet);
    const label = document.getElementById(`fx-wet-label-${deckId}`);
    if (label) label.textContent = `D/W ${Math.round(wet * 100)}%`;
  };
  input.addEventListener("input", apply);
  apply();
});

// The FX pad is a TOGGLE (click on / click off), not momentary — it matches
// the loop button's behaviour on this console and keeps a hands-free effect
// running while you work the EQ.
function updateFxPad(deckId) {
  const unit = fxUnits[deckId];
  document.querySelectorAll(`.fx-pad[data-deck="${deckId}"]`).forEach((pad) => {
    const on = unit.active && unit.type !== "none";
    pad.classList.toggle("fx-on", on);
    if (on) pad.textContent = `${FX_TYPES[unit.type]} ON`;
    // Armed with nothing selected: say so rather than looking like a dead pad.
    else pad.textContent = unit.active ? "PICK FX" : "FX OFF";
  });
}

function toggleFx(deckId) {
  fxUnits[deckId].toggle();
  updateFxTypeButtons(deckId);
  updateFxPad(deckId);
}

document.querySelectorAll(".fx-pad").forEach((pad) => {
  pad.addEventListener("click", () => toggleFx(pad.dataset.deck));
});
