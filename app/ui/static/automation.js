// Live Transition Maker. Audio changes are scheduled against AudioContext time;
// requestAnimationFrame mirrors those changes in controls without becoming the
// timing source.
(function () {
  const arm = document.getElementById("live-transition-arm");
  const cancel = document.getElementById("live-transition-cancel");
  const status = document.getElementById("live-transition-status");
  let selected = null;
  let run = null;

  const say = (text) => { if (status) status.textContent = text; };
  const beatsToSeconds = (deck, beats) => beats * 60 / (deck.bpm || 128);
  function setRange(selector, value) {
    const input = document.querySelector(selector);
    if (!input) return;
    input.value = value;
    const evt = new Event("input", { bubbles: true });
    evt.isAutomation = true;
    input.dispatchEvent(evt);
  }
  function curve(param, from, to, at, duration) {
    param.cancelScheduledValues(at);
    param.setValueAtTime(from, at);
    param.linearRampToValueAtTime(to, at + duration);
  }
  // data-ai-audio: tells deck-controller.js's own input listener the control's
  // underlying AudioParam is already clock-scheduled, so it must only animate
  // the knob, not also set .value directly — a direct .value= set cancels any
  // running setValueCurveAtTime/ramp automation (Web Audio spec), which is
  // what made every automated crossfade/EQ move step and stutter: the mirrored
  // slider's own synthetic "input" event was fighting the smooth curve 60x/s.
  // Scoped: each mark is a refcounted hold released once (idempotent). The flag
  // goes only when the last hold on the element is released, and never when it
  // was already set by someone else (autopilot.js flags its own transition).
  const holds = new Map();   // element -> {n, foreign}
  function markAiAudio(selector) {
    const el = document.querySelector(selector);
    if (!el) return { el: null, release() {} };
    let h = holds.get(el);
    if (!h) {
      h = { n: 0, foreign: el.dataset.aiAudio !== undefined };
      holds.set(el, h);
      el.dataset.aiAudio = "1";
    }
    h.n++;
    let done = false;
    const hold = {
      el,
      release() {
        if (done) return;
        done = true;
        if (--h.n > 0) return;
        holds.delete(el);
        if (!h.foreign) delete el.dataset.aiAudio;
      },
    };
    if (run) run.holds.push(hold);
    return hold;
  }
  function mirror(selector, from, to, start, duration, token) {
    const hold = markAiAudio(selector);
    const frame = () => {
      if (!run || run.token !== token) { hold.release(); return; }
      const p = Math.min(1, Math.max(0, (audioCtx.currentTime - start) / duration));
      setRange(selector, from + (to - from) * p);
      if (p < 1) requestAnimationFrame(frame);
      else hold.release();
    };
    requestAnimationFrame(frame);
  }
  function crossfade(from, to, at, duration, token) {
    const points = 32;
    const gainA = new Float32Array(points);
    const gainB = new Float32Array(points);
    for (let i = 0; i < points; i++) {
      const x = (from + (to - from) * i / (points - 1) + 1) / 2;
      gainA[i] = Math.cos(x * Math.PI / 2);
      gainB[i] = Math.cos((1 - x) * Math.PI / 2);
    }
    try {
      decks.a.crossfaderGain.gain.cancelScheduledValues(at);
      decks.b.crossfaderGain.gain.cancelScheduledValues(at);
    } catch (e) {}
    decks.a.crossfaderGain.gain.setValueCurveAtTime(gainA, at, duration);
    decks.b.crossfaderGain.gain.setValueCurveAtTime(gainB, at, duration);
    mirror("#crossfader", from, to, at, duration, token);
  }
  function preflight(candidate) {
    if (!decks.a.buffer || !decks.b.buffer) return "load both decks first";
    if (!candidate) return "select an AI suggestion first";
    if (!Number.isFinite(candidate.a_time) || !Number.isFinite(candidate.b_time)) return "this suggestion has no usable phrase points";
    return null;
  }
  function finish(token, outcome = "completed") {
    if (!run || run.token !== token) return;
    run.timers.forEach(clearTimeout);
    // only this run's flags: a blanket clear also wiped autopilot's, re-opening the stutter
    run.holds.forEach((h) => h.release());
    try {
      const now = audioCtx.currentTime;
      decks.a.crossfaderGain.gain.cancelScheduledValues(now);
      decks.b.crossfaderGain.gain.cancelScheduledValues(now);
      decks.a.lowFilter.gain.cancelScheduledValues(now);
      decks.b.lowFilter.gain.cancelScheduledValues(now);
      decks.a.volumeGain.gain.cancelScheduledValues(now);
    } catch (e) {}
    window.emitDJEvent("automation-finish", { recipe: run.selected.recipe, outcome });
    run = null;
    cancel.disabled = true;
    arm.disabled = !selected;
    say(`LIVE MAKER: ${outcome === "completed" ? "transition complete" : "DJ takeover — remaining automation cancelled"}`);
  }
  function execute() {
    const issue = preflight(selected);
    if (issue) { say(`LIVE MAKER: ${issue}`); return; }
    const token = crypto.randomUUID();
    const preRoll = beatsToSeconds(decks.a, 16);
    const duration = beatsToSeconds(decks.a, 32);
    const start = audioCtx.currentTime + 0.15;
    const swap = start + duration / 2;
    run = { token, selected, start, finish: start + duration, timers: [], holds: [] };
    arm.disabled = true;
    cancel.disabled = false;

    decks.a.seek(Math.max(0, selected.a_time - preRoll));
    decks.b.seek(Math.max(0, selected.b_time - preRoll));
    const pitch = Math.max(-8, Math.min(8, ((decks.a.bpm || 128) / (decks.b.bpm || 128) - 1) * 100));
    decks.b.setPitchPercent(pitch);
    decks.a.play(undefined, false);
    decks.b.play(undefined, false);
    setRange('.volume-fader[data-deck="a"]', 1);
    setRange('.volume-fader[data-deck="b"]', 1);
    setRange("#crossfader", -1);
    crossfade(-1, 1, start, duration, token);

    if (selected.recipe === "Bass Swap" || selected.recipe === "Drop Swap") {
      curve(decks.a.lowFilter.gain, 0, -26, swap, 0.01);
      curve(decks.b.lowFilter.gain, -26, 0, swap, 0.01);
      const knobA = markAiAudio('.eq-knob[data-deck="a"][data-band="low"]');
      const knobB = markAiAudio('.eq-knob[data-deck="b"][data-band="low"]');
      run.timers.push(setTimeout(() => {
        setRange('.eq-knob[data-deck="a"][data-band="low"]', -26);
        setRange('.eq-knob[data-deck="b"][data-band="low"]', 0);
        knobA.release();
        knobB.release();
      }, Math.max(0, (swap - audioCtx.currentTime) * 1000)));
    } else if (selected.recipe === "Filter Transition") {
      curve(decks.a.lowFilter.gain, 0, -26, start, duration);
      curve(decks.b.lowFilter.gain, -26, 0, start, duration);
    } else if (selected.recipe === "Echo Out") {
      const fx = window.fxUnits && window.fxUnits.a;
      if (fx) { fx.setType("echo"); if (!fx.active) fx.toggle(); fx.setWet(0.7); }
      curve(decks.a.volumeGain.gain, 1, 0.0001, swap, 0.02);
      run.timers.push(setTimeout(() => setRange('.volume-fader[data-deck="a"]', 0), Math.max(0, (swap - audioCtx.currentTime) * 1000)));
    } else if (selected.recipe === "Quick Cut" || selected.recipe === "Hard Cut") {
      crossfade(-1, 1, swap, 0.012, token);
    }
    window.emitDJEvent("automation-start", { recipe: selected.recipe, candidate: selected, start, finish: run.finish });
    say(`LIVE MAKER: ${selected.recipe} running — move a control to take over.`);
    run.timers.push(setTimeout(() => finish(token), Math.max(0, (run.finish - audioCtx.currentTime) * 1000 + 50)));
  }

  // A manual control change is intentional DJ takeover, not an error.
  document.addEventListener("input", (event) => {
    if (event.isAutomation) return;
    if (run && (event.isTrusted || event.isTakeover || event.isManual)) finish(run.token, "taken-over");
  }, true);
  arm.addEventListener("click", execute);
  cancel.addEventListener("click", () => { if (run) finish(run.token, "cancelled"); });
  window.liveTransition = {
    select(candidate) {
      selected = candidate;
      arm.disabled = false;
      say(`LIVE MAKER: ${candidate.recipe} ready — phrase-locked 8-bar run.`);
    },
    cancel() { if (run) finish(run.token, "cancelled"); },
  };
})();
