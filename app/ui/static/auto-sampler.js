// AI Music Brain - auto sampler: the AI plays the sampler pads into the mix.
//
// Cues (where a DJ would reach for the sampler):
//   * drops in the playing song (dj-mind dropLines: an energy jump on a phrase line)
//   * lines other engines announce with an "ai-cue" event {at (audio time), kind, deck}:
//       riff over rap (B's rap entry, B's full arrival), strip & rebuild (the drop
//       back), autopilot transitions (B's first downbeat)
// Moves:
//   drop        SWEEP riser 1 bar before, SNARE roll over the last half bar
//               (4 hits, rising), CLAP + OPEN HAT on the line
//   transition  SWEEP 1 bar before, OPEN HAT on the line (the beat layer already fills)
//   line        one OPEN HAT on the line (a new layer arriving)
// Mixer: its own bus, high-passed at 150 Hz (the records own the sub,
// [[EQ & Frequency Management]]), level following the programme loudness so
// the hits sit ~6 dB under the music. Rationed like the DJ mind's FX: one move
// per 16 bars, 4 per song ([[Rules vs Principles]]: FX stay the exception).
//
// Depends on globals: audioCtx, masterGain, decks, triggerPad (performance.js), djMind.
(function (root) {
  "use strict";

  const PAD = { snare: 1, clap: 2, hat: 3, open: 4, sweep: 7 };
  const MIN_GAP_BARS = 16;
  const MAX_PER_SONG = 4;
  const LOOK_BARS = 3;                 // book a detected drop this far ahead

  // Pure: the hits of one move. T = audio time of the line, bar = seconds.
  function planHits(kind, T, bar) {
    if (kind === "drop") {
      const roll = [0, 1, 2, 3].map((k) => ({ pad: "snare", at: T - bar / 2 + (k * bar) / 8, gain: 0.35 + 0.15 * k }));
      return [{ pad: "sweep", at: T - bar, gain: 0.7 }, ...roll,
              { pad: "clap", at: T, gain: 0.9 }, { pad: "open", at: T, gain: 0.7 }];
    }
    if (kind === "transition") return [{ pad: "sweep", at: T - bar, gain: 0.6 }, { pad: "open", at: T, gain: 0.6 }];
    if (kind === "line") return [{ pad: "open", at: T, gain: 0.55 }];
    return [];
  }
  // Pure: may a move go at T? ledger = [{at, song}] of moves made.
  function allowed(ledger, T, bar, song) {
    const last = ledger.length ? ledger[ledger.length - 1].at : -Infinity;
    if (T - last < MIN_GAP_BARS * bar - 0.05) return false;
    return ledger.filter((x) => x.song === song).length < MAX_PER_SONG;
  }
  // OWNER RULE "dhol drop only for punjabi songs, shouldn't experiment": a pad holding a dhol / bhangra
  // sample (its loaded name) fires only when scene-profile.js dholOk passes: the playing song Punjabi, and on
  // a transition cue the incoming one too. -> null (fire) | the refusal
  const DHOL_SAMPLE = /\b(dhol|dholak|dhole|bhangra|chaal)\b/i;
  function dholBlock(sampleName, kind, genreA, genreB, sp) {
    if (!sampleName || !DHOL_SAMPLE.test(String(sampleName).replace(/[_-]+/g, " "))) return null;
    const S = sp || root.sceneProfileCore || (typeof require === "function" ? require("./scene-profile.js") : null);
    if (!S) return "dhol sample: no scene profile to check the song";
    const v = S.dholOk(genreA, kind === "transition" ? (genreB || "") : undefined);
    return v.ok ? null : v.why;
  }
  const core = { planHits, allowed, PAD, MIN_GAP_BARS, MAX_PER_SONG, dholBlock, DHOL_SAMPLE };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof root.document === "undefined" || typeof audioCtx === "undefined" || typeof triggerPad !== "function") return;

  // ---- mixer: bus -> 150 Hz high-pass -> master, level follows the programme --
  const bus = audioCtx.createGain();
  const hp = audioCtx.createBiquadFilter();
  hp.type = "highpass";
  hp.frequency.value = 150;
  bus.gain.value = 0.3;
  bus.connect(hp);
  hp.connect(masterGain);
  const meter = audioCtx.createAnalyser();
  meter.fftSize = 2048;
  masterGain.connect(meter);
  const mbuf = new Float32Array(meter.fftSize);
  let programRms = 0.1;
  function followLevel() {
    meter.getFloatTimeDomainData(mbuf);
    let e = 0;
    for (let i = 0; i < mbuf.length; i++) e += mbuf[i] * mbuf[i];
    const rms = Math.sqrt(e / mbuf.length);
    if (rms > 0.005) programRms = programRms * 0.9 + rms * 0.1;
    // pads peak near 1: sit them ~6 dB under the programme's peaks (~ rms x 2)
    bus.gain.setTargetAtTime(Math.max(0.06, Math.min(0.5, programRms * 1.1)), audioCtx.currentTime, 0.5);
  }

  // relaxed session (autopilot.js window.djSession): no sampler hits at all (user)
  function enabled() {
    if (window.djSession && window.djSession.relaxed) return false;
    const t = document.getElementById("ap-sampler-toggle"); return !t || t.checked;
  }
  const ledger = [];
  const booked = [];                    // {at} lines already covered
  function songOf(d) { return d && d.analysis ? d.analysis.path : ""; }

  // force = on demand (AI ACTIONS button): skips the toggle and the rationing
  function fire(kind, T, bar, deckId, why, force = false) {
    const d = root.decks && root.decks[deckId];
    if ((!force && !enabled()) || T - audioCtx.currentTime < bar * 0.6) return false;
    if (booked.some((b) => Math.abs(b - T) < bar)) return false;           // one move per line
    if (!force && !allowed(ledger, T, bar, songOf(d))) return false;
    const ap = root.autopilotState;
    for (const h of planHits(kind, T, bar)) {
      if (h.at < audioCtx.currentTime + 0.02) continue;
      const slot = typeof padSamples !== "undefined" ? padSamples[PAD[h.pad]] : null;
      const dhol = dholBlock(slot && slot.name, kind, ap && ap.genre, ap && ap.nextGenre);
      if (dhol) { console.info(`auto sampler: ${h.pad} pad skipped (${slot.name}): ${dhol}`); continue; }
      const g = audioCtx.createGain();
      g.gain.value = h.gain;
      g.connect(bus);
      triggerPad(PAD[h.pad], h.at, g);
      setTimeout(() => g.disconnect(), Math.max(0, (h.at - audioCtx.currentTime + 3) * 1000));
    }
    ledger.push({ at: T, song: songOf(d) });
    booked.push(T);
    root.dispatchEvent(new CustomEvent("ai-activity", { detail: { kind: "stem-move", deck: deckId || "",
      label: `AUTO SAMPLER · ${kind}`, why: why || (kind === "drop" ? "riser, snare roll, clap + open hat on the drop" : "open hat on the line") } }));
    return true;
  }

  // Cues from the other engines
  root.addEventListener("ai-cue", (e) => {
    const c = e.detail || {};
    const d = root.decks && root.decks[c.deck];
    const bar = c.bar || 240 / ((d && d.bpm) || 128);
    fire(c.kind, c.at, bar, c.deck, c.why);
  });

  // Drops in the audible song, from its own analysis
  function audible() {
    // another engine is driving the decks (riff over rap, a LAYER): it cues itself
    if (["a", "b"].some((id) => root.decks && root.decks[id] && root.decks[id]._extPos)) return null;
    if (root.djMind && root.djMind.layerActive) return null;
    let best = null, lv = 0;
    for (const id of ["a", "b"]) {
      const d = root.decks && root.decks[id];
      if (!d || !d.playing || d._extPos || d.loopOn) continue;         // riff / hold loop: they cue themselves
      const g = (d.crossfaderGain ? d.crossfaderGain.gain.value : 1) * (d.volumeGain ? d.volumeGain.gain.value : 1);
      if (g > lv) { lv = g; best = id; }
    }
    return lv > 0.3 ? best : null;
  }
  function tick() {
    followLevel();
    if (!enabled()) return;
    const id = audible();
    const d = id && root.decks[id];
    const core2 = root.djMind && root.djMind.core;
    if (!d || !d.analysis || !core2 || !core2.dropLines) return;
    const bar = 240 / (d.bpm || 128), rate = (d._playbackRate && d._playbackRate()) || 1;
    const a = d.analysis;
    if (!a._samplerDrops) a._samplerDrops = core2.trackDropLines
      ? core2.trackDropLines(a, bar) : core2.dropLines(a.phrase_boundaries_8bar, a.energy_times, a.energy_curve, bar);
    const pos = d._currentPosition();
    for (const dl of a._samplerDrops) {
      const ahead = (dl.t - pos) / rate;
      if (ahead > bar * 1.2 && ahead < LOOK_BARS * bar) {
        fire("drop", audioCtx.currentTime + ahead, bar / rate, id, `the drop at ${Math.floor(dl.t / 60)}:${String(Math.floor(dl.t % 60)).padStart(2, "0")} (energy +${(dl.energy - dl.prevEnergy).toFixed(2)})`);
      }
    }
    while (booked.length && booked[0] < audioCtx.currentTime - 60) booked.shift();
  }
  setInterval(tick, 250);
  root.autoSampler = { core, fire, bus };
})(typeof window !== "undefined" ? window : globalThis);
