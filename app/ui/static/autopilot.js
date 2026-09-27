// AI DJ Autopilot
// Seed track → LLM suggests next → download → analyze → auto-crossfade → repeat.
//
// Deck alternation: even transitions crossfade A→B (xfader -1→+1),
// odd transitions crossfade B→A (xfader +1→-1). Tracks alternate slots.
//
// Requires: window.decks, window.loadIntoDeck, setStatus (app.js + deck-controller.js).

(function () {
  // Every request the autopilot makes has a deadline. Without one, a request
  // lost in a server restart never settled and the set sat in HOLD LOOP
  // forever ("Matching transition..." stuck). Budgets match the work behind
  // each endpoint (LLM queue, Demucs stems, 30-50 MB FLAC audio).
  const _fetch = window.fetch.bind(window);
  function deadlineFor(url) {
    const u = String(url);
    if (u.includes("/api/download")) return 300000;
    if (u.includes("/api/blend/plan") || u.includes("/api/mashup/plan")) return 180000;
    if (u.includes("/api/layer/plan")) return 60000;   // vocal maps already cached by the blend plan
    if (u.includes("/api/bridge/plan")) return 10000;
    if (u.includes("/api/autopilot/suggest")) return 240000; // ~50 s per call, may queue behind a plan
    if (u.includes("/api/audio/")) return 120000;
    if (u.includes("/api/match") || u.includes("/analysis")) return 90000;
    if (u.includes("/api/tracks")) return 20000;
    return 60000;
  }
  function fetch(url, opts = {}) {
    const ctl = new AbortController();
    const ms = deadlineFor(url);
    const timer = setTimeout(() => ctl.abort(), ms);
    return _fetch(url, Object.assign({}, opts, { signal: ctl.signal }))
      .catch((e) => { throw e.name === "AbortError" ? new Error(`timed out after ${ms / 1000}s: ${url}`) : e; })
      .finally(() => clearTimeout(timer));
  }

  // ── state ─────────────────────────────────────────────────────────────────
  let active = false;
  let activeDeck = "a";       // which deck is currently playing
  let currentTrackId = null;
  let occasion = "";
  let history = [];           // display names of played tracks (last 5 kept)
  let mashupTag = "";         // status suffix while a vocal layer is booked
  let entryPos = 0;           // track time where the current song came in
  let currentEnergy = null;   // LLM's 1-10 energy read of the current song
  let playedIds = [];         // track ids played this set (LAYER callbacks: an earlier vocal)
  let setStartedAt = 0;       // Date.now() when the set started (elapsed_seconds for suggest)
  let beatMutedByLayer = false; // a LAYER paused the live beat layer (restore after / on stop)
  function unmuteBeatLayer() {
    if (beatMutedByLayer && window.beatLayer) window.beatLayer.setEnabled(true);
    beatMutedByLayer = false;
  }

  // ── UI refs ───────────────────────────────────────────────────────────────
  const seedInput      = document.getElementById("ap-seed-input");
  const occasionInput  = document.getElementById("ap-occasion-input");
  const startBtn       = document.getElementById("ap-start-btn");
  const stopBtn        = document.getElementById("ap-stop-btn");
  const statusEl       = document.getElementById("ap-status");
  const queueEl        = document.getElementById("ap-queue");
  const xfader         = document.getElementById("crossfader");

  if (!startBtn) return; // panel not present

  // ── helpers ───────────────────────────────────────────────────────────────
  function apStatus(msg) {
    if (statusEl) statusEl.textContent = msg;
    if (typeof setStatus === "function") setStatus(msg);
  }

  function stagingDeck() { return activeDeck === "a" ? "b" : "a"; }

  function deckPosition(id) {
    const d = window.decks && window.decks[id];
    return d ? d._currentPosition() : 0;
  }

  // ── transition engine ─────────────────────────────────────────────────────
  //
  // TECHNICAL SPEC: "Fred again.." style transitions (grounded in ./DJ/ notes)
  //
  // Sources: [[Fred again.. Case Study]], [[Bass Swap]], [[Double Drop]],
  // [[Echo Out]], [[Stems Transition]], [[EQ & Frequency Management]],
  // [[Phrasing & Structure]].
  //
  // 1. PHRASE GRID. Dance music moves in 8-bar (32-beat) phrases. Every move
  //    below is expressed in BARS and lands on the grid: the recipe matcher
  //    already snaps `a_time` / `b_time` to a real 8-bar boundary, so t0 of the
  //    transition IS a phrase boundary. 1 bar = 4 beats = 240000 / bpm ms.
  //    Standard blend = 16 bars, drop-based recipes = 8 bars, hard cut = 0.
  //
  // 2. FREQUENCY OWNERSHIP. Two kick drums / two sub-basses never play at once
  //    (sub-bass < 120 Hz stacks into mud and phase cancellation). The low EQ
  //    knob drives a real Web Audio BiquadFilter lowshelf (deck.lowFilter,
  //    range -26 dB = kill .. +6 dB). Rule: outgoing low is KILLED before the
  //    incoming low opens. Highs and mids blend freely; the crossfader only
  //    moves once the bass has a single owner.
  //
  // 3. EQ ORDER (default): bars 0-4 kill outgoing LOW; bars 4-8 crossfader to
  //    centre; bar 8 (phrase boundary) open incoming LOW; bars 8-16 crossfader
  //    to the incoming side while outgoing HIGH shelf sweeps down (the
  //    "high-pass the old track away" feel, done with the high shelf because
  //    that is the filter the console exposes).
  //
  // 4. BASS SWAP: the Fred again.. staple. Both tracks phrase-aligned, cut A
  //    low over 4 bars, then at the drop snap B low open within one beat so
  //    the new sub arrives as a single event on the downbeat.
  //
  // 5. DOUBLE DROP: both drops land on the same downbeat and play together
  //    for 8 bars at full level (crossfader parked centre). Used only when the
  //    matcher scored the pair as harmonically safe (same/adjacent Camelot,
  //    < 3% BPM delta). Even here bass has one owner: A low is killed at the
  //    drop, B carries the sub, A contributes melody/tops. After 8 bars A is
  //    cut hard.
  //
  // 6. ECHO OUT: arm the ECHO insert on the outgoing deck at the last phrase,
  //    kill its low, then let the delay tail carry the space while B enters
  //    clean. Used for key clashes / big BPM gaps because the tail masks the
  //    harmonic mismatch.
  //
  // 7. LOOP ROLL: lock a 2-bar loop on the outgoing deck at the phrase
  //    boundary (loopBeats = 8, loop button) so the exit point holds steady
  //    for the bass hand-off; release once the incoming drop owns the room.
  //
  // 8. WEB AUDIO / DOM. deck-controller.js wires every knob to the graph:
  //    `.eq-knob[data-deck][data-band]` -> deck.setEQ(band, dB)
  //    `#crossfader` -> equal-power cos curve on deck.crossfaderGain
  //    `.fx-type-btn[data-deck][data-type]` click -> fxUnits[deck].setType()
  //    `.deck-btn[data-action="loop-toggle"]` click -> deck.toggleLoop()
  //    Driving the DOM controls (value + `input` event / click) keeps the UI
  //    lamps in sync and reuses the deck's own BiquadFilter nodes, so this
  //    module needs no direct AudioContext access.
  //
  const LOW_KILL = -26;          // slider minimum, treated as -inf
  const HIGH_SWEEP = -18;        // outgoing high shelf at the end of a blend
  const runTimers = [];          // setTimeout / setInterval ids for this run

  function later(ms, fn) {
    const id = setTimeout(fn, Math.max(0, ms));
    runTimers.push(id);
    return id;
  }

  function clearRun() {
    runTimers.forEach((id) => { clearTimeout(id); clearInterval(id); });
    runTimers.length = 0;
    endAudioClock();
  }

  // ── audio-clock automation ────────────────────────────────────────────────
  // Transitions schedule every EQ / crossfader move on the AudioContext clock,
  // at exact bar offsets from the downbeat where B starts (xT0). JS timers only
  // animate the knobs. Timer jitter used to move the bass swap off the beat
  // (crossfade analysis, fix 6).
  const XF_LOOKAHEAD_MS = 150;   // timers fire this early; audio lands exactly
  const XF_CENTER_BOOST_DB = 1.5; // AI crossfader curve: fills the mid-blend dip (analysis fix 5)
  let xT0 = null;                // audio time of the transition downbeat (null = timer mode)
  let xOffsetMs = 0;             // bar offset of the automation step being scheduled

  function endAudioClock() {
    xT0 = null;
    document.querySelectorAll("[data-ai-audio]").forEach((el) => { delete el.dataset.aiAudio; });
  }

  // Equal-power gains for crossfader value v (-1..1), +XF_CENTER_BOOST_DB at the centre.
  function xfGains(v) {
    const x = (v + 1) / 2;
    const boost = Math.pow(10, (XF_CENTER_BOOST_DB * Math.sin(Math.PI * x)) / 20);
    return [Math.cos(x * 0.5 * Math.PI) * boost, Math.cos((1 - x) * 0.5 * Math.PI) * boost];
  }

  function audioTargetOf(el) {
    if (!el || !window.decks) return null;
    if (el === xfader) return { kind: "xf" };
    if (el.classList && el.classList.contains("eq-knob")) {
      const d = window.decks[el.dataset.deck];
      const f = d && ({ low: d.lowFilter, mid: d.midFilter, high: d.highFilter })[el.dataset.band];
      return f ? { kind: "eq", param: f.gain } : null;
    }
    return null;
  }

  // Schedule from -> to over ms on the audio clock at xT0 + xOffsetMs.
  function scheduleAudio(target, from, to, ms) {
    const when = Math.max(audioCtx.currentTime, xT0 + xOffsetMs / 1000);
    const dur = Math.max(0, ms) / 1000;
    // hold whatever is sounding at `when`, drop later events, then ramp: never
    // throws on back-to-back moves (setValueCurveAtTime does if curves touch)
    const hold = (p) => (p.cancelAndHoldAtTime ? p.cancelAndHoldAtTime(when) : p.cancelScheduledValues(when));
    if (target.kind === "eq") {
      const p = target.param;
      hold(p);
      p.setValueAtTime(from, when);
      if (dur > 0) p.linearRampToValueAtTime(to, when + dur);
      return when;
    }
    // equal-power (+centre boost) curve as short linear segments
    const n = dur > 0 ? Math.max(2, Math.ceil(dur * 30)) : 1;
    for (const [idx, d] of [[0, "a"], [1, "b"]]) {
      const p = window.decks[d].crossfaderGain.gain;
      hold(p);
      p.setValueAtTime(xfGains(n === 1 ? to : from)[idx], when);
      for (let i = 1; i < n; i++) {
        p.linearRampToValueAtTime(xfGains(from + (to - from) * (i / (n - 1)))[idx], when + dur * (i / (n - 1)));
      }
    }
    return when;
  }

  function eqEl(deck, band) {
    return document.querySelector(`.eq-knob[data-deck="${deck}"][data-band="${band}"]`);
  }
  function fxBtn(deck, type) {
    return document.querySelector(`.fx-type-btn[data-deck="${deck}"][data-type="${type}"]`);
  }
  function loopBtn(deck) {
    return document.querySelector(`.deck-btn[data-deck="${deck}"][data-action="loop-toggle"]`);
  }

  function setRange(el, v) {
    if (!el) return;
    el.value = String(v);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  // Linear ramp of a range input over durationMs. fromVal null = current value.
  // Inside a transition (xT0 set) the audio is scheduled on the audio clock and
  // this only animates the control; otherwise it drives the control directly.
  function rampParam(getEl, fromVal, toVal, durationMs) {
    const steps = 20;
    const interval = Math.max(16, durationMs / steps);
    const first = getEl();
    if (!first) return;
    const from = fromVal == null ? parseFloat(first.value) : fromVal;
    const target = xT0 != null ? audioTargetOf(first) : null;
    if (target) {
      first.dataset.aiAudio = "1";
      const when = scheduleAudio(target, from, toVal, durationMs);
      const waitMs = Math.max(0, (when - audioCtx.currentTime) * 1000);
      if (durationMs <= 0) { later(waitMs, () => setRange(first, toVal)); return; }
      later(waitMs, () => {
        let k = 0;
        const t = setInterval(() => {
          if (k > steps) { clearInterval(t); return; }
          setRange(first, from + (toVal - from) * (k / steps));
          k++;
        }, interval);
        runTimers.push(t);
      });
      return;
    }
    let step = 0;
    const t = setInterval(() => {
      const el = getEl();
      if (!el || step > steps) { clearInterval(t); return; }
      const v = from + (toVal - from) * (step / steps);
      setRange(el, v);
      step++;
    }, interval);
    runTimers.push(t);
  }

  function barMs(deck) {
    const d = window.decks && window.decks[deck];
    const bpm = d && d.bpm > 0 ? d.bpm : 128;
    return 240000 / bpm;
  }

  function setLoopLength(deck, beats) {
    const d = window.decks && window.decks[deck];
    if (d && typeof d.setLoopBeats === "function") d.setLoopBeats(beats);
    const v = document.getElementById(`loop-value-${deck}`);
    if (v) v.textContent = String(beats);
  }

  function setLoop(deck, on) {
    const d = window.decks && window.decks[deck];
    const btn = loopBtn(deck);
    if (!d || !btn) return;
    if (!!d.loopOn !== on) btn.click();
  }

  function setFx(deck, type, wet) {
    const btn = fxBtn(deck, type);
    if (btn) btn.click();
    if (wet != null) setRange(document.querySelector(`.fx-wet[data-deck="${deck}"]`), wet);
  }

  // Put a deck back to neutral so it is clean when it becomes the staging deck.
  // Level-match the incoming song to the playing one (trim knob, like a DJ
  // gain-staging on the mixer): vibe gate reports gain_match_db = playing RMS
  // minus candidate RMS. Clamped to the knob's range (-12 dB .. +6 dB).
  function matchGain(outDeck, inDeck, db) {
    const knob = (d) => document.querySelector(`.gain-knob[data-deck="${d}"]`);
    const outK = knob(outDeck), inK = knob(inDeck);
    if (!inK) return;
    const base = outK ? parseFloat(outK.value) || 1 : 1;
    const g = Number.isFinite(db) ? base * Math.pow(10, db / 20) : base;
    setRange(inK, Math.max(0.25, Math.min(2, g)).toFixed(2));
  }

  function resetDeck(deck) {
    setRange(eqEl(deck, "low"), 0);
    setRange(eqEl(deck, "mid"), 0);
    setRange(eqEl(deck, "high"), 0);
    setLoop(deck, false);
    setFx(deck, "none");
  }

  function recipeKind(recipe) {
    const r = String(recipe || "").toLowerCase();
    if (r.includes("double drop")) return "double";
    if (r.includes("bass swap") || r.includes("drop swap")) return "bass";
    if (r.includes("echo")) return "echo";
    if (r.includes("filter")) return "filter";
    if (r.includes("hard cut") || r.includes("quick cut") || r.includes("cut")) return "cut";
    if (r.includes("loop")) return "loop";
    if (r.includes("blend")) return "blend";
    return "default";
  }

  /**
   * Run a recipe-aware, EQ-first transition from `out` to `inn`.
   * Assumes `inn` was cued at b_time and starts playing at t0.
   * `xfDuration` (seconds) is the caller's budget: < 16 means an early bail,
   * so every bar count is halved to keep the set moving.
   * Returns total duration in ms; the outgoing deck may be stopped after that.
   */
  function executeTransition(recipe, out, inn, xfDuration, t0Audio) {
    clearRun();
    xT0 = Number.isFinite(t0Audio) ? t0Audio : audioCtx.currentTime;
    const kind = recipeKind(recipe);
    const scale = xfDuration >= 16 ? 1 : 0.5;
    const bar = barMs(out) * scale;
    const beat = bar / 4;
    const fromXf = out === "a" ? -1 : 1;
    const toXf = -fromXf;
    const xfEl = () => xfader;
    const lowOut = () => eqEl(out, "low");
    const lowIn = () => eqEl(inn, "low");
    const midOut = () => eqEl(out, "mid");
    const highOut = () => eqEl(out, "high");
    // timers fire XF_LOOKAHEAD_MS early; the audio lands on the exact bar
    const at = (bars, fn) => later(Math.max(0, bars * bar - XF_LOOKAHEAD_MS), () => {
      xOffsetMs = bars * bar;
      try { fn(); } finally { xOffsetMs = 0; }
    });
    const setAt = (getEl, v) => rampParam(getEl, v, v, 0); // instant, on the audio clock
    // One owner of the sub at every moment, and never nobody: A keeps its lows
    // until one beat before the swap line, B's lows open on the line
    // ([[Bass Swap]], [[EQ & Frequency Management]]).
    const bassSwapAt = (bars) => {
      at(bars - 0.25, () => rampParam(lowOut, null, LOW_KILL, beat));
      at(bars, () => rampParam(lowIn, LOW_KILL, 0, beat));
    };

    // Tempo gap, both songs have stems: STEM BRIDGE instead of an echo-out (user:
    // "Echo Out is painful"). Strip A, hold its voice, B's pads in beatless, the
    // crossfader sweeps across the beatless stretch, B's beat drops on its own line.
    {
      const sm0 = window.stemMoves, od0 = window.decks && window.decks[out], id0 = window.decks && window.decks[inn];
      if (sm0 && (kind === "echo" || recipe === "Stem Bridge") && od0 && id0 && od0.stemsReady && id0.stems) {
        ["low", "mid", "high"].forEach((b) => { setRange(eqEl(out, b), 0); setRange(eqEl(inn, b), 0); });
        const secs = sm0.stemBridge(out, inn, xT0, id0.startOffset || 0);
        if (secs > 0) {
          const barA = 240 / (od0.bpm || 128) / od0._playbackRate(), barB = 240 / (id0.bpm || 128);
          const sweepMs = 4 * barB * 1000;                    // across the beatless stretch
          later(Math.max(0, (xT0 + 4 * barA - audioCtx.currentTime) * 1000), () => rampParam(xfEl, fromXf, toXf, sweepMs));
          return secs * 1000;
        }
      }
    }

    // Both decks have live stems: the transition is done with stems, not EQ
    // (user: the automixer should lean on stems). Every layer gets one owner:
    // B's synths first, kick + bass swap together on the line, one singer.
    const sm = window.stemMoves;
    const od = window.decks && window.decks[out], idk = window.decks && window.decks[inn];
    if (sm && sm.core.STEM_BLEND_KINDS.has(kind) && od && idk && od.stemsReady && idk.stemsReady) {
      // one song: 32-bar blends when A has the room left, else 16 (bass / double: 8-16)
      const aLeft = od.buffer ? (od.buffer.duration - (od._positionAt ? od._positionAt(xT0) : od._currentPosition())) : 0;
      const long = aLeft / (bar / 1000) >= 34;
      const bars = (kind === "blend" || kind === "filter" || kind === "loop" ? (long ? 32 : 16) : kind === "bass" ? 16 : 8) * scale;
      const barS = bar / 1000;
      ["low", "mid", "high"].forEach((b) => { setRange(eqEl(out, b), 0); setRange(eqEl(inn, b), 0); });
      // The crossfader sweeps A -> B across the whole blend (equal power, on the
      // audio clock): never a jump to the centre or to the far side (user:
      // "putting the crossfader in centre sounds really odd"). The stems still
      // decide which layer plays; the fader just carries the level across.
      if (sm.stemBlend(kind, out, inn, xT0, bars, barS)) {
        rampParam(xfEl, fromXf, toXf, bars * bar);
        return bars * bar;
      }
    }

    // Incoming deck always enters with its sub killed: single bass owner.
    setRange(lowIn(), LOW_KILL);
    setRange(eqEl(inn, "mid"), 0);
    setRange(eqEl(inn, "high"), 0);
    setRange(xfEl(), fromXf);

    let total;
    switch (kind) {
      case "bass": // [[Bass Swap]]: B rises with no lows, one-downbeat bass swap at bar 4
        rampParam(xfEl, fromXf, 0, 4 * bar);
        bassSwapAt(4);
        at(4, () => {
          rampParam(xfEl, 0, toXf, 4 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 4 * bar);
        });
        total = 8;
        break;

      case "double": // both drops together for 8 bars, then cut A
        // [[Double Drop]]: one bass only - B's lows open, A's killed on the same
        // downbeat (no ramp: two subs must never overlap).
        setAt(xfEl, 0);
        setAt(lowOut, LOW_KILL);
        setAt(lowIn, 0);
        at(8, () => setAt(xfEl, toXf));
        total = 8.5;
        break;

      case "echo": // arm ECHO on A; A keeps its lows until the bar-4 swap, tail carries B's entry
        // one continuous 8-bar crossfader sweep (no 2-bar rush); the echo tail
        // carries A out while B takes over
        setFx(out, "echo", 0.7);
        rampParam(xfEl, fromXf, toXf, 8 * bar);
        bassSwapAt(4);
        at(4, () => rampParam(highOut, null, HIGH_SWEEP, 4 * bar));
        total = 8;
        break;

      case "filter": // sweep A's mids down over 4 bars; lows swap on the bar-4 line
        rampParam(midOut, null, -10, 4 * bar);
        rampParam(xfEl, fromXf, 0, 4 * bar);
        bassSwapAt(4);
        at(4, () => {
          rampParam(xfEl, 0, toXf, 4 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 4 * bar);
        });
        total = 8;
        break;

      case "cut": // instant snap on the phrase boundary
        setAt(lowOut, LOW_KILL);
        setAt(lowIn, 0);
        setAt(xfEl, toXf);
        total = 1;
        break;

      case "loop": // 2-bar loop roll on A holds the exit point steady
        setLoopLength(out, 8);
        setLoop(out, true);
        at(2, () => rampParam(xfEl, fromXf, 0, 2 * bar));
        bassSwapAt(4);
        at(4, () => {
          rampParam(xfEl, 0, toXf, 2 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 2 * bar);
        });
        at(6, () => setLoop(out, false));
        total = 8;
        break;

      case "blend": // 16-bar EQ-first blend: B rises under A (no lows) for 8 bars,
        // one-downbeat bass swap on the bar-8 line, then A fades out over 8 bars.
        // (Old version cut A's lows over bars 0-4 and opened B's at bar 8:
        // 13-15 s with nobody on the bass - measured, research/notes/crossfade-analysis.)
        rampParam(xfEl, fromXf, 0, 8 * bar);
        bassSwapAt(8);
        at(8, () => {
          rampParam(xfEl, 0, toXf, 8 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 8 * bar);
        });
        total = 16;
        break;

      default: // 16 bars: same shape as the blend (the old default kept the fader on
        // A for 8 bars with A's lows already cut: a long thin stretch)
        rampParam(xfEl, fromXf, 0, 8 * bar);
        bassSwapAt(8);
        at(8, () => {
          rampParam(xfEl, 0, toXf, 8 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 8 * bar);
        });
        total = 16;
        break;
    }
    stemHandoff(kind, out, inn, (total * bar) / 1000);
    return total * bar;
  }

  // One singer through the blend (user: the outgoing vocal goes onto the
  // incoming stems): B enters as its instrumental, A's vocal rides B's beat on
  // the vocal bus, B's own vocal returns as A's fades (stem-moves.js).
  function stemHandoff(kind, out, inn, totalS) {
    if (!window.stemMoves || kind === "cut" || kind === "double" || totalS < 4) return false;
    const od = window.decks && window.decks[out], id = window.decks && window.decks[inn];
    if (!od || !id) return false;
    const ka = od.analysis && od.analysis.key && od.analysis.key.camelot;
    const kb = id.analysis && id.analysis.key && id.analysis.key.camelot;
    const core = window.djMind && window.djMind.core;
    const keyScore = core && core.camelotScore ? core.camelotScore(ka, kb) : 0;
    const p0 = od._positionAt ? od._positionAt(xT0) : od._currentPosition();
    const outVocal = window.stemMoves.vocalShare(od.analysis && od.analysis.vocal_active_regions, p0, p0 + totalS);
    const fits = window.stemMoves.core.handoffFits({ outStems: od.stemsReady, inStems: id.stemsReady, keyScore, outVocal });
    return fits && window.stemMoves.handoff(out, inn, xT0, totalS,
      `${Math.round(outVocal * 100)}% vocal in the blend, keys ${ka}->${kb}: one singer, A's voice over B's beat`);
  }

  /**
   * LAYER transition ([[3-Deck Layering]], set study item 4): B rides under A
   * as a texture (lows killed, highs trimmed, crossfader just off A) for
   * `hold_bars`, the bass goes to B on the phrase line (A's sub is out one beat
   * before, B's comes in on the line: one sub owner at any time), then A
   * unwinds over `unwind_bars`: highs, then mids, then the fader.
   * `layer.third` (optional) is a cached vocal stem riding B's clean phrase.
   * Bars are A's live (pitch-locked) bars: a 64-bar hold drifts otherwise.
   * Returns total duration in ms.
   */
  function executeLayer(out, inn, layer) {
    xT0 = null; // LAYER keeps timer-driven automation
    clearRun();
    const oa = window.decks && window.decks[out];
    const bpm = oa && oa.bpm > 0 ? oa.bpm * oa._playbackRate() : 128;
    const bar = 240000 / bpm;
    const beat = bar / 4;
    const H = layer.hold_bars, U = layer.unwind_bars;
    const fromXf = out === "a" ? -1 : 1;
    const toXf = -fromXf;
    const xfEl = () => xfader;
    const band = (d, b) => () => eqEl(d, b);
    const at = (bars, fn) => later(bars * bar, fn);

    // 1) texture: B's sub killed, highs trimmed, fader eases to ~-7 dB for B
    setRange(eqEl(inn, "low"), LOW_KILL);
    setRange(eqEl(inn, "mid"), -3);
    setRange(eqEl(inn, "high"), -8);
    setRange(xfEl(), fromXf);
    rampParam(xfEl, fromXf, fromXf * 0.4, 4 * bar);
    // 2) hold H bars; 3) bass hand-off on the phrase line
    later(H * bar - beat, () => rampParam(band(out, "low"), null, LOW_KILL, beat * 0.9));
    at(H, () => {
      rampParam(band(inn, "low"), LOW_KILL, 0, beat);
      rampParam(band(inn, "mid"), null, 0, 2 * bar);
      rampParam(band(inn, "high"), null, 0, 2 * bar);
      rampParam(xfEl, null, 0, 2 * bar);
      // 4) unwind A slowly: highs, then mids, then the fader
      rampParam(band(out, "high"), null, LOW_KILL, (U / 3) * bar);
    });
    at(H + U / 3, () => rampParam(band(out, "mid"), null, LOW_KILL, (U / 3) * bar));
    at(H + (2 * U) / 3, () => rampParam(xfEl, null, toXf, (U / 3) * bar));

    // third element: an earlier / next-next vocal over B's clean phrase
    if (layer.third && window.mashup) {
      later(400, async () => {
        try {
          const t = layer.third;
          if (await window.mashup.play(inn, t.plan, t.host_entry)) {
            mashupTag = ` | ✖ 3rd layer: vocal ${t.name || "callback"}`;
          }
        } catch (e) { console.warn("LAYER third layer failed:", e.message); }
      });
    }
    return (H + U) * bar;
  }

  const ENERGY_DELTA_CLASS = { up: "ap-energy-up", down: "ap-energy-down", maintain: "ap-energy-hold" };
  const ENERGY_DELTA_LABEL = { up: "↑ Energy up", down: "↓ Energy down", maintain: "→ Hold energy" };

  // Text from YouTube titles / the LLM goes into innerHTML: escape it.
  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ── AI playlist panel ─────────────────────────────────────────────────────
  // Shows what is actually lined up: the scheduled NEXT song, songs already
  // downloaded and waiting (READY), and songs still downloading (⬇).
  let scheduledNext = null;   // candidate booked for the coming transition
  let scheduledFireAt = null; // track time of the booked transition on the playing deck
  let pendingSugs = [];       // suggestions whose downloads are in flight
  let aiPicking = false;

  function sugOf(c) {
    return (c && c.suggestion) || { title: c && c.name };
  }

  function showQueue() {
    const rows = [];
    if (scheduledNext) rows.push({ s: sugOf(scheduledNext), tag: "NEXT" });
    if (leadTo && !leadTo.arrived) {
      rows.push({ s: leadTo.cand ? Object.assign({}, sugOf(leadTo.cand), { reason: "your LEAD TO destination" })
                                 : { title: leadTo.text, reason: `steering there in ${leadTo.steps} songs` },
                  tag: leadTo.cand ? `TARGET ${Math.max(0, leadTo.steps - 1 - leadTo.played)}` : "LEAD" });
    }
    ready.forEach((c) => rows.push({ s: sugOf(c), tag: "READY" }));
    const have = new Set(rows.map((r) => `${r.s.artist}|${r.s.title}`));
    pendingSugs.forEach((s) => { if (!have.has(`${s.artist}|${s.title}`)) rows.push({ s, tag: "⬇" }); });
    renderQueue(rows);
  }

  function renderQueue(rows) {
    if (!queueEl) return;
    // BRIDGE PATH banner: where the tempo ladder stands, e.g. "BRIDGE 3/5 → 110 BPM"
    const bl = bridgeLabel();
    const banner = bl
      ? `<div class="ap-bridge" style="font-weight:700;color:#38bdf8;margin:2px 0 6px" title="${esc(bridge.why)}: ${esc(bridge.steps.map(Math.round).join(" → "))}">` +
        `${esc(bl)} <span style="font-weight:400;opacity:.75">toward ${Math.round(bridge.toBpm)} BPM</span></div>`
      : "";
    if (!rows.length) {
      queueEl.innerHTML = `${banner}<div class='ap-empty'>${aiPicking ? "⏳ AI picking the next songs…" : "⏳ Finding next track…"}</div>`;
      return;
    }
    queueEl.innerHTML = banner + rows.map(({ s, tag }) => {
      const eClass = ENERGY_DELTA_CLASS[s.energy_delta] || "";
      const eLabel = ENERGY_DELTA_LABEL[s.energy_delta] || "";
      const genre   = s.genre ? `<span class="ap-genre">${esc(s.genre)}</span>` : "";
      const moment  = s.mix_moment ? `<span class="ap-moment" title="Mix moment">${esc(s.mix_moment)}</span>` : "";
      const energy  = eLabel ? `<span class="ap-energy ${eClass}">${eLabel}</span>` : "";
      const vibe    = s.vibe_link ? `<span class="ap-vibe">"${esc(s.vibe_link)}"</span>` : "";
      const isNext = tag === "NEXT";
      const name = s.artist ? `${esc(s.artist)} — ${esc(s.title)}` : esc(s.title || "?");
      return `
      <div class="ap-item ${isNext ? "ap-next" : ""}" ${isNext ? 'id="ap-next-item"' : ""}>
        <span class="ap-pos">${tag}</span>
        <div class="ap-item-main">
          <span class="ap-name">${name}</span>
          <span class="ap-meta">${esc(s.expected_key || "")}${s.expected_bpm ? "  " + esc(s.expected_bpm) + " BPM" : ""}${genre ? "  " + genre : ""}${isNext ? '  <span id="ap-match-score" style="display:none"></span>' : ""}</span>
          <span class="ap-badges">${energy}${moment}</span>
          ${vibe}
          <span class="ap-why">${esc(s.reason || "")}</span>
        </div>
      </div>`;
    }).join("");
  }

  // Keep a playlist ahead: once the next song is booked, ask the AI what
  // follows IT and pre-download those, so the pool never runs dry and the
  // panel always shows what is coming.
  let toppingUp = false;
  async function topUpPool(afterId) {
    if (toppingUp || !active || ready.length >= 2) return;
    toppingUp = true;
    aiPicking = !ready.length;
    showQueue();
    try {
      const avoid = [scheduledNext && scheduledNext.name, ...ready.map((c) => c.name)].filter(Boolean);
      const sugs = await getSuggestions(afterId, avoid, { lookAhead: true });
      if (!active) return;
      pendingSugs = sugs;
      aiPicking = false;
      showQueue();
      await Promise.all(sugs.map((sg) => downloadSuggestion(sg).then(addReady).catch((e) => {
        console.warn("Prefetch failed:", sg.title, e.message);
      })));
    } catch (e) {
      console.warn("Playlist top-up failed:", e.message);
    } finally {
      pendingSugs = [];
      aiPicking = false;
      toppingUp = false;
      showQueue();
    }
  }

  // ── API calls ─────────────────────────────────────────────────────────────
  async function importUrl(url) {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    return data.tracks || [];
  }

  // Return existing track if artist+title already cached; null otherwise.
  async function findCached(artist, title) {
    try {
      const res = await fetch("/api/tracks");
      if (!res.ok) return null;
      const data = await res.json();
      const tracks = data.tracks || data || [];
      const a = artist.toLowerCase();
      const t = title.toLowerCase();
      return tracks.find(tr => {
        if (tr.not_a_song) return false; // live / event recording or mix in the library
        const dn = (tr.display_name || tr.filename || "").toLowerCase();
        return dn.includes(a) && dn.includes(t);
      }) || null;
    } catch { return null; }
  }

  let energyNotedFor = null; // track whose energy the DJ mind already logged
  const profileById = {};    // LLM current_profile energy (1-10) per track id: PEAK mode
  // Network-level failures (server restarting, connection refused) are retried
  // with backoff and do NOT use up one of prepareTransition's rounds.
  async function getSuggestions(trackId, avoid = [], opts = {}) {
    for (let attempt = 0; ; attempt++) {
      try {
        return await getSuggestionsOnce(trackId, avoid, opts);
      } catch (e) {
        const transient = e instanceof TypeError || /Failed to fetch|NetworkError|timed out|502|503/.test(e.message);
        if (!transient || attempt >= 4 || !active) throw e;
        apStatus(`Server not answering — retrying (${attempt + 1}/4)…`);
        await new Promise((r) => setTimeout(r, 5000));
      }
    }
  }

  async function getSuggestionsOnce(trackId, avoid = [], opts = {}) {
    const setPos = Math.min(history.length / 10, 1.0);
    // `avoid` = titles rejected this round (failed download / vibe gate) so the
    // LLM does not propose them again on retry.
    // DJ mind hint: "dip" after a long peak (study rule 9), "callback" late in
    // the set (rule 7). Null most of the time.
    const energyNote = window.djMind && !opts.lookAhead ? window.djMind.nextEnergyNote(setPos) : null;
    const res = await fetch("/api/autopilot/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: trackId, occasion: occasionWithBridge(opts), ...leadFields(opts), history: history.slice(-30), avoid: avoid.slice(-6), set_position: setPos, set_mode: setMode(), energy_note: energyNote, lookahead: !!opts.lookAhead,
        variety_run: varietyRun().run, variety_genre: varietyRun().genre,
        tempo_target: bridgeTarget(opts.lookAhead), tempo_note: bridgeNote(opts.lookAhead) || null,
        elapsed_seconds: setStartedAt ? (Date.now() - setStartedAt) / 1000 : null }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    // OCCASION FIRST: the AI says the playing song is outside the occasion's
    // music ("punjabi wedding" while Fred again.. plays) -> steer, even across
    // a tempo gap (Echo Out), instead of holding out for a beat-matchable pick.
    if (!opts.lookAhead) {
      if (data.current_genre) currentGenre = data.current_genre;
      steering = data.steering === "move" && steerStep < MAX_STEER_STEPS ? "move" : "stay";
      if (steering === "stay") steerStep = 0;
      // Steering into music at another tempo: ladder there (BRIDGE PATH).
      const far = steering === "move" && !bridge && (data.suggestions || [])
        .map((s) => parseFloat(s.expected_bpm)).find((b) => b > 0 && !locks(playingBpm(), b));
      if (far) startBridge(far, "occasion steering"); // fire-and-forget
    }
    const e = data.current_profile && parseFloat(data.current_profile.energy);
    // Look-ahead describes the booked next song: keep it for when that song plays.
    if (Number.isFinite(e)) profileById[trackId] = e <= 1 ? e * 10 : e;
    if (window.djMind && trackId === currentTrackId) window.djMind.setProfileEnergy(profileById[trackId]);
    // Look-ahead calls describe the NEXT song: they must not overwrite the
    // playing song's energy (it drives the set-mode window).
    if (Number.isFinite(e) && !opts.lookAhead) {
      currentEnergy = e <= 1 ? e * 10 : e;
      if (window.djMind && energyNotedFor !== trackId) { energyNotedFor = trackId; window.djMind.noteEnergy(currentEnergy); }
    }
    return data.suggestions || [];
  }

  async function matchTracks(aId, bId) {
    const res = await fetch("/api/match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_a_id: aId, track_b_id: bId, top_n: 1 }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    const candidate = (data.candidates || [])[0] || null;
    if (candidate && data.vibe) candidate.vibe = data.vibe;
    return candidate;
  }

  // ── pre-download pool ─────────────────────────────────────────────────────
  // Every suggestion starts downloading as soon as the AI proposes it (the
  // server runs 2 downloads in parallel; progress bars in the panel). Songs that
  // finish but are not used right away wait in `ready`, so the next transition
  // can often start with no download at all and the playing song never runs
  // out before the next one exists.
  const ready = [];     // { track_id, name, duration, suggestion }
  const MAX_READY = 4;

  // Length check: the server already rejects < 90 s and >= 9 min; LONG mode
  // needs songs that can actually ride 3-6 min.
  function minSongSecs() { return setMode() === "long" ? 180 : 90; }

  async function trackInfo(trackId) {
    try {
      const a = await fetch(`/api/tracks/${trackId}/analysis`).then((r) => r.json());
      return { duration: (a && a.duration) || 0, bpm: (a && a.bpm) || 0 };
    } catch { return { duration: 0, bpm: 0 }; }
  }

  async function downloadSuggestion(s) {
    const label = `${s.artist} — ${s.title}`;
    const cached = await findCached(s.artist, s.title);
    if (cached) {
      const info = await trackInfo(cached.track_id);
      return { track_id: cached.track_id, name: cached.display_name || label,
               duration: info.duration, bpm: info.bpm, suggestion: s };
    }
    const tracks = window.dlJobs
      ? await window.dlJobs.run(s.search_query, label)
      : await importUrl(s.search_query);
    if (!tracks.length) throw new Error("nothing downloaded");
    const t = tracks[0];
    const info = t.duration && t.bpm ? t : await trackInfo(t.track_id);
    return { track_id: t.track_id, name: t.display_name || label,
             duration: info.duration, bpm: info.bpm, suggestion: s };
  }

  function addReady(c) {
    if (!c || history.includes(c.name) || ready.some((r) => r.track_id === c.track_id)) return;
    ready.push(c);
    while (ready.length > MAX_READY) ready.shift();
    showQueue();
  }

  // Match + gates + load + schedule one downloaded candidate. True = scheduled.
  // Can `cand` be tempo-locked to the playing deck (half/double time counts)?
  // +/-8 % on pitch; +/-15 % when the playing deck has stems: the next song
  // then plays on key-locked tempo stems (multi-BPM stem sets), no pitch shift.
  function stemsOn() { const d = window.decks && window.decks[activeDeck]; return !!(d && d.stems); }
  function lockLimit() { return stemsOn() ? 0.15 : 0.08; }
  function tempoLockableAt(cand, lim) {
    const d = window.decks && window.decks[activeDeck];
    if (!d || !d.bpm || !cand.bpm) return true;
    const aEff = d.bpm * d._playbackRate();
    return [1, 2, 0.5].some((m) => Math.abs(aEff / (cand.bpm * m) - 1) <= lim);
  }
  function tempoLockable(cand) {
    const d = window.decks && window.decks[activeDeck];
    if (!d || !d.bpm || !cand.bpm) return true; // unknown: let the matcher decide
    const aEff = d.bpm * d._playbackRate();
    return [1, 2, 0.5].some((m) => Math.abs(aEff / (cand.bpm * m) - 1) <= lockLimit());
  }
  let allowTempoJump = false; // set on the last round so the set never stalls
  // Tempo-jump budget (user: "genre switch once in a while is fine, or in the
  // middle of high-BPM songs where people are dancing, switch with echo"):
  // beat-matched by default; one tempo jump after JUMP_EVERY songs, or after
  // PEAK_JUMP_EVERY while the floor is at peak energy; never back to back.
  const JUMP_EVERY = 4;
  const PEAK_JUMP_EVERY = 2;
  let songsSinceJump = 0;          // a set starts beat-matched; first jump after JUMP_EVERY songs
  let jumpPending = false;         // the booked transition is a tempo jump
  function tempoJumpBudget() {
    // One song (user): with stems no planned tempo jumps; only the last-round
    // fallback may jump, so the set never stalls.
    if (stemsOn()) return false;
    const peak = currentEnergy != null && currentEnergy >= 8;
    return songsSinceJump >= (peak ? PEAK_JUMP_EVERY : JUMP_EVERY);
  }
  let steering = "stay";      // "move" while steering toward the occasion's music
  // Variety: subgenre of each played song, to spot a style that has plateaued.
  let genreLog = [];
  let currentGenre = "";
  function genreFamily(g) {
    return String(g || "").toLowerCase().split(/[\/,&(]| - /)[0].replace(/[^a-z0-9 ]+/g, " ").trim();
  }
  function varietyRun() {
    const fam = genreFamily(currentGenre || genreLog[genreLog.length - 1]);
    if (!fam) return { run: 0, genre: "" };
    let run = 0;
    for (let i = genreLog.length - 1; i >= 0 && genreFamily(genreLog[i]) === fam; i--) run++;
    return { run, genre: fam };
  }
  let steerStep = 0;          // bridge songs played so far on the current steer (cap 7)
  const HIGH_ENERGY_OCCASION = /\b(wedding|shaadi|sangeet|baraat|mehndi|reception|party|club\s*night|peak|festival|rave|birthday|bachelor(ette)?|new\s*year)\b/i;
  const MAX_STEER_STEPS = 7;
  function occasionWithStep(opts = {}) {
    if (!occasion || steering !== "move") return occasion;
    const step = Math.min(MAX_STEER_STEPS, steerStep + (opts.lookAhead ? 2 : 1));
    return `${occasion} — steering step ${step} of max ${MAX_STEER_STEPS} toward this occasion's music` +
           (step >= MAX_STEER_STEPS - 1 ? " (FINAL: pick the occasion's own anthems now)" : "");
  }

  // Occasion + the BRIDGE step as a hint suffix (only when the user set an
  // occasion: an occasion-less set gets the step through tempo_target alone).
  function occasionWithBridge(opts = {}) {
    const base = occasionWithStep(opts);
    const t = bridgeTarget(opts.lookAhead);
    return base && t ? `${base} — TEMPO BRIDGE: songs natively near ${Math.round(t)} BPM (${bridgeNote(opts.lookAhead)})` : base;
  }

  // ── LEAD TO (user-directed destination) ──────────────────────────────────
  // The user types where the set should end up and in how many songs:
  //   "Artist - Title" -> that exact song is pre-downloaded and played as the
  //                       destination after the steering songs;
  //   an artist / genre -> the set steers into that world, then stays there.
  // Every step stays beat-matched: the destination's tempo becomes a BRIDGE
  // PATH target, and each suggestion round is told how far along it is.
  let leadTo = null; // { text, kind, steps, played, cand, bpm, arrived }
  const leadStatusEl = document.getElementById("ap-lead-status");
  const leadBox = document.getElementById("ap-lead");
  const leadCancel = document.getElementById("ap-lead-cancel");

  function leadStatus(msg) {
    if (leadStatusEl) leadStatusEl.textContent = msg || "";
    if (leadBox) leadBox.classList.toggle("is-leading", !!leadTo);
    if (leadCancel) leadCancel.hidden = !leadTo;
  }

  function leadFields(opts = {}) {
    if (!leadTo) return {};
    return { lead_to: leadTo.text, lead_steps: leadTo.steps,
             lead_step: Math.min(leadTo.steps, leadTo.played + (opts.lookAhead ? 2 : 1)),
             lead_bpm: leadTo.bpm || null };
  }

  function leadHint(base, opts = {}) {
    if (!leadTo) return base;
    const step = Math.min(leadTo.steps, leadTo.played + (opts.lookAhead ? 2 : 1));
    const last = step >= leadTo.steps;
    let hint;
    if (leadTo.kind === "song") {
      hint = `LEAD TO "${leadTo.text}"${leadTo.bpm ? ` (~${Math.round(leadTo.bpm)} BPM)` : ""}: step ${step} of ${leadTo.steps}; ` +
             `each song clearly closer to it in genre, energy and sound; the target song itself plays after the last step, ` +
             `so do NOT suggest it; pick songs that lead naturally into it`;
    } else {
      hint = `LEAD TO "${leadTo.text}": step ${step} of ${leadTo.steps}; each song clearly closer to that world` +
             (last ? " (FINAL step: be fully inside it now)" : "");
    }
    return base ? `${base} — ${hint}` : hint;
  }

  async function startLead(picked) {
    const input = document.getElementById("ap-lead-input");
    const text = picked ? picked.title : (input ? input.value.trim() : "");
    hideLeadResults();
    if (!text) { leadStatus("Type a song ('Artist - Title'), an artist or a genre"); return; }
    if (!active) { leadStatus("Start a set first; LEAD steers a running set"); return; }
    const stepsEl = document.getElementById("ap-lead-steps");
    const steps = Math.max(2, Math.min(6, parseInt(stepsEl ? stepsEl.value : "4", 10) || 4));
    // a picked YouTube result is a song; typed text is a genre / artist to steer toward
    const kind = picked ? "song" : "style";
    leadTo = { text, kind, steps, played: 0, cand: null, bpm: 0, arrived: false };
    leadStatus(kind === "song" ? `fetching the destination "${text}"…` : `steering toward ${text} in ${steps} songs`);
    // steer the NEXT pick, not the one already booked: drop not-yet-booked
    // pool songs that were chosen for the old direction
    ready.length = 0;
    pendingSugs = [];
    showQueue();
    if (kind === "song") {
      try {
        // the exact video the user picked (direct URL: live-title / length checks skip
        // what they deliberately chose, mix / interview checks still apply)
        const tracks = window.dlJobs ? await window.dlJobs.run(picked.url, `LEAD TO: ${text}`)
                                     : await importUrl(picked.url);
        if (!tracks.length) throw new Error("nothing downloaded");
        const t = tracks[0];
        const info = t.duration && t.bpm ? t : await trackInfo(t.track_id);
        const c = { track_id: t.track_id, name: t.display_name || text, duration: info.duration, bpm: info.bpm,
                    suggestion: { title: t.display_name || text, reason: "your LEAD TO destination" } };
        if (!leadTo || leadTo.text !== text) return; // cancelled meanwhile
        leadTo.cand = c;
        leadTo.bpm = c.bpm || 0;
        leadStatus(`→ ${c.name}${c.bpm ? ` (${Math.round(c.bpm)} BPM)` : ""} after ${steps - 1} steering song${steps > 2 ? "s" : ""}`);
        // tempo ladder toward the destination (fewer steps than the lead)
        if (c.bpm && !locks(playingBpm(), c.bpm)) startBridge(c.bpm, `lead to ${c.name}`, steps - 1);
      } catch (e) {
        leadStatus(`couldn't find "${text}" (${e.message}) — steering toward its style instead`);
        if (leadTo) leadTo.kind = "style";
      }
    }
    showQueue();
  }

  function cancelLead(msg) {
    leadTo = null;
    leadStatus(msg || "");
    showQueue();
  }

  // Called after every transition.
  function advanceLead() {
    if (!leadTo) return;
    if (leadTo.arrived) {
      const t = leadTo.text;
      cancelLead(`✓ arrived: ${t}`);
      return;
    }
    leadTo.played++;
    if (leadTo.kind === "style" && leadTo.played >= leadTo.steps) {
      // arrived in that world: keep it as the set's direction from here on
      occasion = occasion ? `${occasion}; now inside ${leadTo.text}` : leadTo.text;
      cancelLead(`✓ now in ${leadTo.text} — the set stays there`);
      return;
    }
    leadStatus(leadTo.kind === "song"
      ? `step ${leadTo.played}/${leadTo.steps - 1} toward ${leadTo.cand ? leadTo.cand.name : leadTo.text}`
      : `step ${leadTo.played}/${leadTo.steps} toward ${leadTo.text}`);
  }

  // The destination song is due: after steps-1 steering songs.
  function leadDue() {
    return !!(leadTo && leadTo.kind === "song" && leadTo.cand && !leadTo.arrived &&
              leadTo.played >= leadTo.steps - 1);
  }

  // ── BRIDGE PATH (set study item 5, [[Genre Bridge Playbook]]) ─────────────
  // A far tempo target (beyond the 8% lock, or the occasion steering into
  // another genre) becomes a BPM ladder of beat-matched songs, <= ~6% per step
  // or a half/double-time link (87 <-> 174), from POST /api/bridge/plan. Each
  // step's BPM is the tempo target of the next suggestion round, and the
  // tempo gate prefers candidates that move up the ladder. The tempo-jump
  // budget (Echo Out) stays the fallback: an infeasible ladder, or the last
  // search round.
  let bridge = null;          // { toBpm, steps[], total, played, link, why }
  let bridgePending = false;
  let forceJump = false;      // last-round fallback: the set never stalls on a ladder
  const BRIDGE_TOL = 0.015;   // a step counts as reached within 1.5%
  function playingBpm() {
    const d = window.decks && window.decks[activeDeck];
    return d && d.bpm > 0 ? d.bpm * d._playbackRate() : 0;
  }
  function pulseNear(bpm, ref) {  // bpm, or its half/double, closest to ref
    return [1, 2, 0.5].map((m) => bpm * m)
      .reduce((b, x) => (Math.abs(Math.log(x / ref)) < Math.abs(Math.log(b / ref)) ? x : b));
  }
  function locks(a, b) { return a > 0 && b > 0 && Math.abs(pulseNear(b, a) / a - 1) <= 0.08; }
  // Index of the ladder step still ahead of the playing tempo (-1: no bridge).
  function bridgeStepIdx(ahead = 0) {
    if (!bridge) return -1;
    const last = bridge.steps.length - 1;
    const cur = playingBpm();
    if (!cur) return Math.min(last, bridge.played + ahead);
    const dir = Math.sign(bridge.steps[last] - bridge.steps[0]) || 1;
    const p = pulseNear(cur, bridge.steps[0]);
    let i = bridge.steps.findIndex((s) => dir * Math.log(s / p) > BRIDGE_TOL);
    if (i < 0) i = last;
    return Math.min(last, i + ahead);
  }
  function bridgeTarget(lookAhead) {
    const i = bridgeStepIdx(lookAhead ? 1 : 0);
    return i < 0 ? null : bridge.steps[i];
  }
  function bridgeLabel() {
    const i = bridgeStepIdx();
    return i < 0 ? "" : `BRIDGE ${i + 1}/${bridge.total} → ${Math.round(bridge.steps[i])} BPM`;
  }
  function bridgeNote(lookAhead) {
    const i = bridgeStepIdx(lookAhead ? 1 : 0);
    return i < 0 ? "" : `bridge step ${i + 1}/${bridge.total} toward ${Math.round(bridge.toBpm)} BPM` +
      (bridge.link !== "direct" ? ` (${bridge.link}-time link at the end)` : "");
  }
  async function startBridge(toBpm, why, stepsCap) {
    const from = playingBpm();
    if (bridge || bridgePending || !(toBpm > 0) || !from || locks(from, toBpm)) return;
    bridgePending = true;
    try {
      // occasion steering keeps its 5-7 song cap; a plain tempo target gets 5
      const maxSteps = stepsCap ? Math.max(1, stepsCap)
        : steering === "move" ? Math.max(2, MAX_STEER_STEPS - steerStep) : 5;
      const res = await fetch("/api/bridge/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ from_bpm: from, to_bpm: toBpm, max_step_pct: 6, max_steps: maxSteps }),
      });
      const lad = await res.json();
      if (!res.ok) throw new Error(lad.detail || res.statusText);
      if (!lad.feasible || lad.step_count < 2) {
        if (!lad.feasible) console.info("BRIDGE infeasible, tempo-jump budget stays:", (lad.reasons || []).join("; "));
        return;
      }
      bridge = { toBpm, steps: lad.steps, total: lad.step_count, played: 0, link: lad.link, why };
      apStatus(`BRIDGE PATH → ${Math.round(toBpm)} BPM (${why}): ${lad.steps.map(Math.round).join(" → ")}, ${lad.step_pct}%/step`);
      showQueue();
    } catch (e) {
      console.warn("Bridge plan failed:", e.message);
    } finally { bridgePending = false; }
  }
  // After a transition: count the step, end the bridge on arrival or at the cap.
  function advanceBridge() {
    if (!bridge) return;
    bridge.played++;
    const d = window.decks && window.decks[activeDeck];
    const native = d && d.bpm > 0 ? d.bpm : 0;          // pitch eases home to the native tempo
    if (locks(native, bridge.toBpm)) {
      apStatus(`BRIDGE done: ${Math.round(native)} BPM locks to ${Math.round(bridge.toBpm)} BPM`);
      bridge = null;
    } else if (bridge.played >= bridge.total + 1) {
      console.info("BRIDGE ran out of steps: tempo-jump budget takes over");
      bridge = null;
    }
  }
  // Tempo gate while bridging: never step back down the ladder (2% slack).
  function bridgeFits(cand) {
    if (!bridge || !cand.bpm) return true;
    const cur = playingBpm();
    if (!cur || locks(cur, bridge.toBpm)) return true;
    const last = bridge.steps[bridge.steps.length - 1];
    const dir = Math.sign(last - bridge.steps[0]) || 1;
    const c = pulseNear(cand.bpm, cur);
    return dir * Math.log(c / cur) >= -0.02;
  }

  async function evaluateCandidate(currentId, cand, gen) {
    if (!active || !cand) return false;
    const nextId = cand.track_id;
    const nextName = cand.name;
    if (!tempoLockable(cand)) {
      // Far tempo: climb there on a BRIDGE PATH instead of one Echo Out; the
      // jump (budget / last round) stays the fallback.
      if (!bridge && !forceJump && !history.includes(nextName)) await startBridge(cand.bpm, `toward ${nextName}`);
      if (!allowTempoJump || (bridge && !forceJump)) {
        apStatus(`Not after this song: ${nextName} (${Math.round(cand.bpm)} BPM can't be beat-matched)` +
                 `${bridge ? ` — ${bridgeLabel()}` : ""} — kept for later`);
        cand.keep = true;
        return false;
      }
    } else if (!forceJump && !bridgeFits(cand)) {
      apStatus(`Not now: ${nextName} (${Math.round(cand.bpm)} BPM) steps back down the ${bridgeLabel()} — kept for later`);
      cand.keep = true;
      return false;
    }
    if (nextId === currentId || history.includes(nextName)) return false;
    if (cand.duration && cand.duration < minSongSecs()) {
      apStatus(`Skipping ${nextName}: ${fmtTime(cand.duration)} is too short for a ${setMode().toUpperCase()} set`);
      return false;
    }

    apStatus(`Matching transition → ${nextName}…`);
    let candidate = await matchTracks(currentId, nextId);
    if (!candidate) return false;

    // Measured vibe gate: reject candidates whose loudness / brightness /
    // onset density / energy sit too far from what is playing right now.
    if (candidate.vibe && candidate.vibe.ok === false) {
      const why = (candidate.vibe.reasons || []).join("; ") || `distance ${candidate.vibe.distance}`;
      console.warn("Autopilot vibe reject:", nextName, why);
      apStatus(`Not after this song: ${nextName} (${why}) — kept for later`);
      cand.keep = true; // pairwise: may fit fine after the next song
      return false;
    }

    // Show match score on the NEXT queue card.
    const scoreEl = document.getElementById("ap-match-score");
    if (scoreEl) {
      const sc = Math.round(candidate.score || 0);
      const good = sc >= 65;
      scoreEl.textContent = `${good ? "⭐" : "⚡"} ${sc}/100${good ? "" : " · early exit"}`;
      scoreEl.style.cssText = `display:inline;font-weight:700;color:${good ? "#4ade80" : "#f97316"};margin-left:6px`;
    }

    // AI plan for this pair (candidate, exit phrase, DJ-mind moves), fetched
    // while the next track loads. Rules-only if it fails or times out.
    const aiPlan = requestMindPlan(currentId, nextId, candidate);

    // Preload next track into staging deck
    apStatus(`Loading ${nextName} into deck ${stagingDeck().toUpperCase()}…`);
    const audioRes = await fetch(`/api/audio/tracks/${nextId}`);
    if (!audioRes.ok) return false;
    const blob = await audioRes.blob();
    if (!active) return false;
    await loadIntoDeck(stagingDeck(), nextId, nextName, blob);
    // tempo gap 2-15 %: render its key-locked tempo stems now, long before the blend
    {
      const oa1 = window.decks && window.decks[activeDeck], sd1 = window.decks && window.decks[stagingDeck()];
      if (oa1 && sd1 && oa1.bpm && sd1.useTempoStems) {
        const waitStems = async () => { for (let i = 0; i < 40 && !sd1.stems; i++) await new Promise((r) => setTimeout(r, 500)); };
        waitStems().then(() => {
          if (!sd1.bpm || !sd1.stems) return;
          const aEff1 = oa1.bpm * oa1._playbackRate();
          const m1 = [1, 2, 0.5].reduce((b, m) => (Math.abs(aEff1 / (sd1.bpm * m) - 1) < Math.abs(aEff1 / (sd1.bpm * b) - 1) ? m : b));
          const g1 = Math.abs(aEff1 / (sd1.bpm * m1) - 1);
          if (g1 > 0.02 && g1 <= 0.15) sd1._tempoStemsJob = sd1.useTempoStems(aEff1 / m1);
        });
      }
    }
    // Over 8 % the blend needs the key-locked stems: book the song only once they're on.
    if (cand.bpm && !tempoLockableAt(cand, 0.08) && tempoLockableAt(cand, 0.15)) {
      const sd2 = window.decks && window.decks[stagingDeck()];
      apStatus(`Key-locking ${nextName} to this tempo (tempo stems)…`);
      const t2 = Date.now();
      while (sd2 && !sd2._tempoStemsJob && Date.now() - t2 < 25000) await new Promise((r) => setTimeout(r, 500));
      const ok2 = sd2 && sd2._tempoStemsJob ? await Promise.race([sd2._tempoStemsJob, new Promise((r) => setTimeout(() => r(false), 90000))]) : false;
      if (!ok2 && !forceJump) {
        apStatus(`Not now: ${nextName} needs key-locked stems that aren't ready — kept for later`);
        cand.keep = true;
        return false;
      }
    }
    matchGain(activeDeck, stagingDeck(), candidate.vibe && candidate.vibe.gain_match_db);
    const plan = await aiPlan;
    if (!active || currentTrackId !== currentId) return false;
    if (plan && plan.candidate) {
      // The AI picked a candidate + exit phrase: both already validated server-side.
      candidate = Object.assign({}, candidate, plan.candidate, { vibe: candidate.vibe });
      apStatus(`AI plan: ${plan.candidate.recipe}, exit ${fmtTime(plan.candidate.a_time)}`);
    }

    const plan0 = await requestBlend(currentId, nextId, candidate);
    const blend = plan0 && plan0.ok !== false ? plan0 : null;
    const minExit = plan0 && plan0.min_exit != null ? plan0.min_exit : null;
    if (!active || currentTrackId !== currentId) return false;

    // LAYER (set study item 4): hold both records, then unwind A. Tempo-locked
    // pairs only; the DJ mind's rules decide and keep the veto over the AI.
    let layer = blend ? await requestLayer(currentId, nextId, candidate, plan, cand) : null;
    if (!active || currentTrackId !== currentId) return false;
    if (layer && !(layer.start >= deckPosition(activeDeck) + 16)) layer = null; // start slipped past

    if (gen !== undefined && gen !== prepGen) return false; // superseded by a restarted search
    const fireAt = scheduleTransition(currentId, nextId, nextName, candidate, blend, minExit, layer);
    scheduledFireAt = fireAt;
    if (!layer) tryMashup(currentId, nextId, nextName, fireAt); // fire-and-forget; B itself enters under a LAYER
    scheduledNext = cand;
    pendingSugs = []; // leftovers show up as READY when their download lands
    showQueue();
    topUpPool(nextId); // fire-and-forget: songs for AFTER the next one
    return true;
  }

  // Exit window (track seconds) for the current song, same maths as scheduleTransition.
  function exitWindow(score) {
    const w = playWindow(score);
    const od = window.decks && window.decks[activeDeck];
    const trackEnd = (od && od.buffer ? od.buffer.duration : Infinity) - w.xf - 2;
    return { lo: Math.min(entryPos + w.min, trackEnd), hi: Math.min(entryPos + w.max, trackEnd) };
  }

  // Beat-to-beat blend plan: vocal-free exit phrase in the playing song,
  // vocal-free entry phrase in the next one, and B's tempo-lock rate.
  // null -> fall back to the matcher's points (still phrase + tempo aligned).
  async function requestBlend(currentId, nextId, candidate) {
    const win = exitWindow(candidate.score || 50);
    const od = window.decks && window.decks[activeDeck];
    const lo = Math.max(win.lo, deckPosition(activeDeck) + 20);
    if (!od || !(win.hi > lo)) return null;
    apStatus("Mapping vocals for a beat-to-beat blend…");
    try {
      const res = await fetch("/api/blend/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          a_id: currentId, b_id: nextId, window_lo: lo, window_hi: win.hi,
          a_bpm_effective: od.bpm * od._playbackRate(),
          bars: setMode() === "quick" || steering === "move" ? 8 : 16,
          a_entry: entryPos, // the playing song's first drop must play before we leave
        }),
      });
      const plan = await res.json();
      if (!res.ok || !plan.ok) {
        console.info("Blend plan unavailable:", plan.detail || (plan.reasons || []).join("; "));
        // tempo gap: no beat blend, but the drop floor still applies to the echo-out exit
        return res.ok && plan.min_exit != null ? { ok: false, min_exit: plan.min_exit } : null;
      }
      // PEAK MOVES: on top of the tempo-locked plan, an entry on B's first long
      // drop for DOUBLE DROP / DROP SWAP. The DJ mind decides whether to use it.
      const peakEl = document.getElementById("ap-peak-toggle");
      if (window.djMind && window.djMind.planPeak && (!peakEl || peakEl.checked)) {
        try {
          const r2 = await fetch("/api/blend/plan", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              a_id: currentId, b_id: nextId, window_lo: lo, window_hi: win.hi,
              a_bpm_effective: od.bpm * od._playbackRate(), bars: 8, entry_mode: "drop",
            }),
          });
          const drop = await r2.json();
          if (r2.ok && drop.ok) plan.drop = drop;
        } catch (e) { /* peak moves are optional: plain blend */ }
      }
      return plan;
    } catch (e) {
      console.warn("Blend plan failed:", e.message);
      return null;
    }
  }

  // LAYER plan for a tempo-locked pair, or null. The DJ mind checks its cap
  // (one LAYER every few songs) before the server call and the full rules
  // (key, groove, vocal clash, steering, peak floor) after it.
  async function requestLayer(currentId, nextId, candidate, aiPlan, cand) {
    const mind = window.djMind;
    if (!mind || !mind.planLayer || !mind.core || !mind.core.layerBars) return null;
    const aiProposed = !!(aiPlan && aiPlan.layer);
    const base = { steering: steering === "move", peak: false, energy: currentEnergy,
                   aiProposed, aiWhy: aiPlan && aiPlan.layer_reason };
    const pre = mind.planLayer(Object.assign({ ok: true, keyScore: 1, vocalClash: 0, groove: true }, base));
    if (!pre.layer) {
      if (aiProposed) console.info("LAYER (AI) vetoed:", pre.why);
      return null;
    }
    const win = exitWindow(candidate.score || 50);
    const od = window.decks && window.decks[activeDeck];
    const lo = Math.max(win.lo, deckPosition(activeDeck) + 20);
    if (!od || !(win.hi > lo)) return null;
    const { maxHold, unwind } = mind.core.layerBars(setMode());
    // third element: next-next songs first, then earlier songs (callbacks)
    const thirdIds = ready.map((c) => c.track_id)
      .concat(playedIds.slice(0, -1).reverse())
      .filter((id, i, all) => id !== currentId && id !== nextId && all.indexOf(id) === i).slice(0, 6);
    const names = {};
    ready.forEach((c) => { names[c.track_id] = c.name; });
    try {
      apStatus("Checking a LAYER (both records together)…");
      const res = await fetch("/api/layer/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          a_id: currentId, b_id: nextId, window_lo: lo, window_hi: win.hi,
          a_bpm_effective: od.bpm * od._playbackRate(), a_entry: entryPos,
          max_hold_bars: maxHold, unwind_bars: unwind, third_ids: thirdIds,
        }),
      });
      const plan = await res.json();
      if (!res.ok) { console.info("Layer plan unavailable:", plan.detail); return null; }
      const dec = mind.planLayer(Object.assign({
        ok: plan.ok, why: (plan.reasons || []).join("; "), keyScore: plan.key_score,
        vocalClash: plan.vocal_clash, groove: plan.groove,
      }, base));
      if (!dec.layer) {
        console.info(`LAYER off for ${cand ? cand.name : nextId}:`, dec.why);
        return null;
      }
      if (plan.third) plan.third.name = names[plan.third.guest_id] || "callback";
      return Object.assign(plan, { source: dec.source, why: dec.why });
    } catch (e) {
      console.warn("Layer plan failed:", e.message);
      return null;
    }
  }

  function setDeckPitch(deckId, pct, range = 8) {
    const d = window.decks && window.decks[deckId];
    if (!d) return;
    const v = Math.max(-range, Math.min(range, pct));
    d.setPitchPercent(v);
    const fader = document.querySelector(`.pitch-fader[data-deck="${deckId}"]`);
    if (fader) fader.value = String(v.toFixed(1));
    const readout = document.getElementById(`pitch-readout-${deckId}`);
    if (readout) readout.textContent = `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;
  }

  // After a tempo-locked handover, drift the new song back to its own tempo
  // over ~32 bars (inaudible steps), so pitch shift never accumulates.
  function easePitchHome(deckId) {
    const d = window.decks && window.decks[deckId];
    // key-locked tempo stems: the set stays at this tempo, the key never moved
    if (!d || !d._pitchPercent || d.tempoStems) return;
    const steps = 32;
    const barMsNow = 240000 / ((d.bpm || 128) * d._playbackRate());
    const start = d._pitchPercent;
    for (let i = 1; i <= steps; i++) {
      later(i * barMsNow, () => {
        if (activeDeck !== deckId) return;
        setDeckPitch(deckId, start * (1 - i / steps));
      });
    }
  }

  async function requestMindPlan(currentId, nextId, candidate) {
    if (!window.djMind || !window.djMind.requestPlan) return null;
    const win = exitWindow(candidate.score || 50);
    if (!(win.hi > win.lo)) return null;
    // Never wait past the point where the transition must be booked.
    const pos = deckPosition(activeDeck);
    apStatus("AI planning the next transition…");
    return window.djMind.requestPlan(currentId, nextId, candidate, Object.assign(win, {
      setPosition: Math.min(history.length / 10, 1.0),
      mashupPossible: mashupsOn() && !!window.mashup,
      deadlineS: Math.max(win.lo, win.hi - 30) - pos - 20,
    }));
  }

  async function tryLibraryLockable(currentId, gen) {
    const d = window.decks && window.decks[activeDeck];
    if (!d || !d.bpm) return false;
    const aEff = d.bpm * d._playbackRate();
    const key = d.analysis && d.analysis.key && d.analysis.key.camelot || "";
    const exclude = [...playedIds, currentId].join(",");
    try {
      const res = await fetch(`/api/library/lockable?bpm=${aEff.toFixed(2)}&key=${encodeURIComponent(key)}&exclude=${encodeURIComponent(exclude)}&max_gap=${lockLimit()}`);
      const lib = (await res.json()).tracks || [];
      for (const t of lib) {
        if (!active || gen !== prepGen) return false;
        if (history.includes(t.name)) continue;
        apStatus(`Library pick that locks to ${Math.round(aEff)} BPM: ${t.name}`);
        const c = { track_id: t.track_id, name: t.name, bpm: t.bpm, duration: t.duration, keep: false, fromLibrary: true };
        if (await tryCandidate(currentId, c, gen)) return true;
      }
    } catch (e) { console.warn("library fallback:", e.message); }
    return false;
  }

  async function tryCandidate(currentId, cand, gen) {
    try { return await evaluateCandidate(currentId, cand, gen); }
    catch (e) {
      console.warn("Autopilot candidate failed:", cand && cand.name, e.message);
      apStatus(`Skipping ${cand && cand.name}: ${e.message}`);
      return false;
    }
  }

  // ── core loop ─────────────────────────────────────────────────────────────
  let prepGen = 0;        // bumps on every (re)started next-song search
  let prepStartedAt = 0;  // ms timestamp of the current search
  async function prepareTransition(currentId) {
    if (!active) return;
    const gen = ++prepGen;
    prepStartedAt = Date.now();
    showQueue();

    // 0) LEAD TO destination is due: book it (beat-matched when the tempo
    // locks; otherwise the tempo-jump fallback, it's where the user asked to go).
    if (leadDue()) {
      apStatus(`LEAD TO: bringing in ${leadTo.cand.name}`);
      allowTempoJump = true;
      const c = Object.assign({}, leadTo.cand, { keep: false });
      if (await tryCandidate(currentId, c, gen)) { leadTo.arrived = true; return; }
      if (!active || gen !== prepGen) return;
      leadStatus(`couldn't book ${leadTo.cand.name} yet — one more steering song`);
      allowTempoJump = false;
    }

    // 1) Songs already pre-downloaded in an earlier round: no waiting.
    // Pairwise rejects go back to the END of the pool (tried once per song).
    allowTempoJump = false;
    forceJump = false;
    const pool = ready.splice(0, ready.length);
    // BRIDGE: songs nearest the ladder's next step first
    const step = bridgeTarget(false);
    if (step) pool.sort((x, y) => (x.bpm ? Math.abs(Math.log(pulseNear(x.bpm, step) / step)) : 9) -
                                  (y.bpm ? Math.abs(Math.log(pulseNear(y.bpm, step) / step)) : 9));
    for (let i = 0; i < pool.length; i++) {
      if (!active || gen !== prepGen) return;
      const c = pool[i];
      apStatus(`Trying earlier suggestion: ${c.name} (${pool.length - i} in pool)`);
      c.keep = false;
      if (await tryCandidate(currentId, c, gen)) {
        pool.slice(i + 1).forEach(addReady); // untried ones stay for later
        return;
      }
      if (c.keep) addReady(c);
    }

    // 2) Fresh AI suggestions, all downloading in parallel. Retry with new
    // suggestions when every candidate fails; rejected titles are fed back as
    // "avoid" so the model proposes different songs.
    const MAX_ROUNDS = 3;
    const rejected = [];
    for (let round = 1; round <= MAX_ROUNDS; round++) {
      // A running BRIDGE PATH holds the budget back; the last round always may jump.
      forceJump = round === MAX_ROUNDS;
      allowTempoJump = forceJump || (!bridge && tempoJumpBudget());
      // Nothing beat-matchable after a strict round: don't burn more AI rounds
      // hunting for a tempo that may barely exist (a 96 BPM dembow seed has
      // almost no house / UK dance peers). Take the best song already waiting
      // (the AI's own first picks) with a tempo-jump transition instead.
      // Before any forced tempo jump: songs already in the library whose REAL
      // tempo locks (the whole set as one song, user). Same vibe gate as the rest.
      if (round === MAX_ROUNDS && await tryLibraryLockable(currentId, gen)) return;
      if (round === MAX_ROUNDS && ready.length) {
        allowTempoJump = true;
        const waiting = ready.splice(0, ready.length);
        for (let i = 0; i < waiting.length; i++) {
          if (!active || gen !== prepGen) return;
          const c = waiting[i];
          apStatus(`No beat-matchable pick — tempo-jump to ${c.name} (Echo Out / breakdown)`);
          c.keep = false;
          if (await tryCandidate(currentId, c, gen)) {
            waiting.slice(i + 1).forEach(addReady);
            return;
          }
          if (c.keep) addReady(c);
        }
        allowTempoJump = false;
      }
      if (!active || gen !== prepGen) return;
      apStatus(round > 1 ? `⏳ Retrying with new suggestions (${round}/${MAX_ROUNDS})…`
                         : "⏳ AI selecting next songs…");
      let suggestions;
      aiPicking = true;
      showQueue();
      try {
        suggestions = await getSuggestions(currentId, rejected);
        aiPicking = false;
      } catch (e) {
        aiPicking = false;
        console.warn("Autopilot suggest failed:", e.message);
        apStatus(`Suggest error: ${e.message}`);
        continue;
      }
      pendingSugs = suggestions;
      showQueue();
      if (!suggestions.length) continue;
      apStatus(`⬇ Pre-downloading ${suggestions.length} songs…`);

      const jobs = suggestions.map((s) => downloadSuggestion(s).catch((e) => {
        rejected.push(`${s.artist} - ${s.title}`);
        console.warn("Download failed:", s.title, e.message);
        return null;
      }));
      // Highest-ranked suggestion first; the rest keep downloading meanwhile.
      for (let i = 0; i < jobs.length; i++) {
        const c = await jobs[i];
        if (!active || gen !== prepGen) return;
        if (!c) continue;
        if (await tryCandidate(currentId, c, gen)) {
          // Leftovers finish in the background and wait for later transitions.
          jobs.slice(i + 1).forEach((p) => p.then(addReady));
          return;
        }
        if (c.keep) addReady(c); // fit problem with THIS song only: try again next time
        rejected.push(`${c.suggestion.artist} - ${c.suggestion.title}`);
      }
    }
    // Never end the set over this: the playing song keeps going (HOLD LOOP near
    // its end) and the search retries. Stopping here turned a 10 s server
    // restart into a dead set.
    apStatus(`No next song yet after ${MAX_ROUNDS} tries — retrying in 20 s (music keeps playing)`);
    setTimeout(() => { if (active && gen === prepGen) prepareTransition(currentId); }, 20000);
  }

  function scheduleTransition(currentId, nextId, nextName, candidate, blend = null, minExit = null, layer = null) {
    // Tempo gap 2-15 %: render B's stems key-locked at A's tempo now, while A plays
    // (multi-BPM stem sets, cached on the server), so the blend keeps B's key.
    {
      const oa0 = window.decks && window.decks[activeDeck], sd0 = window.decks && window.decks[stagingDeck()];
      if (oa0 && sd0 && oa0.bpm > 0 && sd0.bpm > 0 && sd0.useTempoStems) {
        const aEff0 = oa0.bpm * oa0._playbackRate();
        const m0 = [1, 2, 0.5].reduce((b, m) => (Math.abs(aEff0 / (sd0.bpm * m) - 1) < Math.abs(aEff0 / (sd0.bpm * b) - 1) ? m : b));
        const gap0 = Math.abs(aEff0 / (sd0.bpm * m0) - 1);
        if (gap0 > 0.02 && gap0 <= 0.15) {
          sd0.useTempoStems(aEff0 / m0).then((ok) => ok && window.dispatchEvent(new CustomEvent("ai-activity", { detail: {
            kind: "stem-move", deck: stagingDeck(), label: `TEMPO STEMS · ${(aEff0 / m0).toFixed(1)} BPM`,
            why: `${nextName}: stems key-locked ${(gap0 * 100).toFixed(1)} % to this tempo, no pitch shift` } })));
        }
      }
    }
    if (!active) return;
    let bTime = candidate.b_time || 0;
    let vocalShort = false, vocalCut = "";
    let recipe = candidate.recipe || "Blend";
    // Stems on both decks: the set plays as ONE song (user). Every transition is
    // a long stem blend: one owner per layer, one singer, kick + bass swapped on
    // a line. No vocal-driven shortening, no cuts or spinbacks (those were only
    // there to stop two vocals or two beats clashing, which stems already solve).
    const odS = window.decks && window.decks[activeDeck], sdS = window.decks && window.decks[stagingDeck()];
    const stemsBoth = !!(odS && odS.stemsReady && sdS && sdS.stems);
    const aEffS = odS ? odS.bpm * odS._playbackRate() : 0;
    const gapS = sdS && sdS.bpm ? Math.min(...[1, 2, 0.5].map((m) => Math.abs(aEffS / (sdS.bpm * m) - 1))) : 1;
    const oneSong = stemsBoth && gapS <= 0.15;
    const od0bpm = (window.decks && window.decks[activeDeck] && window.decks[activeDeck].bpm) || 128;
    // Beat-to-beat: when the tempos lock, hand beat to beat. Echo-outs and cuts
    // are for tempo gaps; they turned "vocal -> beat" when used between
    // compatible songs.
    if (blend) {
      bTime = blend.entry;
      // A's vocal riding over B's instrumental intro is a classic long blend;
      // only two vocals at once clash, so keep that overlap short (bass swap).
      const bClean = blend.b_vocal_coverage == null || blend.b_vocal_coverage <= 0.15;
      const k = recipeKind(recipe);
      if (!bClean) recipe = "Bass Swap";
      else if (!["bass", "blend", "default"].includes(k)) recipe = "Long Blend";
      blend.clean = bClean;
      // Two vocals must never sing together: the overlap has to END before B's
      // vocal first comes in (user: "vocals are overlapping"). Pick the
      // transition length by how many bars that is.
      const vIn = oneSong ? null : blend.b_vocal_in_bars;   // stems: one singer, no shortening
      if (oneSong) { recipe = "Long Blend"; blend.clean = true; }
      if (vIn != null) {
        if (vIn < 4) { recipe = "Quick Cut"; vocalCut = "cut on the downbeat before B's vocal"; }
        else if (vIn < 8) { recipe = "Bass Swap"; vocalShort = true; vocalCut = `4-bar swap: B sings in ${Math.round(vIn)} bars`; }
        else if (vIn < 16) { recipe = "Bass Swap"; vocalCut = `8-bar swap: B sings in ${Math.round(vIn)} bars`; }
      }
    } else if (oneSong) {
      // tempo gap up to 15 %: key-locked tempo stems make it a real blend
      recipe = "Long Blend";
    } else if (stemsBoth) {
      // tempo can't lock: a stem bridge, never an echo-out (user)
      recipe = "Stem Bridge";
    } else if (!["echo", "filter"].includes(recipeKind(recipe))) {
      // No tempo lock possible: beats cannot be layered, so don't hard-swap
      // (instant swaps between unrelated tempos changed the whole vibe in the
      // live set). Echo the outgoing song away while the new one enters on its
      // phrase - the wiki's tempo-gap move ([[Echo Out]], What Do I Play Next).
      recipe = "Echo Out";
    }
    if (layer) { bTime = layer.entry; recipe = `LAYER ${layer.hold_bars}+${layer.unwind_bars} bars`; }
    jumpPending = !blend && !oneSong;
    const overlapStyle = layer ? "layer"
      : blend ? (candidate.overlap_style || "standard")
              : "standard"; // never "instant" across a tempo gap
    const score  = candidate.score  || 50;

    // Play-time window from the set mode, counted from when this song came in.
    // Prefer the matcher's phrase-aligned exit if it falls inside the window.
    const w = playWindow(score);
    const nowPos = deckPosition(activeDeck);
    const od = window.decks && window.decks[activeDeck];
    const trackEnd = (od && od.buffer ? od.buffer.duration : Infinity) - w.xf - 2;
    const lo = Math.min(entryPos + w.min, trackEnd);
    const hi = Math.min(entryPos + w.max, trackEnd);
    let exitAt = layer ? layer.start : blend ? blend.exit : candidate.a_time;
    if (!blend && !(exitAt >= lo && exitAt <= hi)) exitAt = Math.max(lo, Math.min(hi, exitAt || hi));
    // never leave before the playing song's first drop has played (server floor)
    if (!blend && minExit != null && exitAt < minExit && minExit < trackEnd) exitAt = minExit;
    // PEAK mode (dj-mind.js peakTransition): tempo-locked pairs only, land B's
    // drop on A's drop downbeat - Double Drop or Drop Swap. null -> blend.
    const peakT = !layer && blend && blend.drop && window.djMind && window.djMind.planPeak
      ? window.djMind.planPeak({ drop: blend.drop, lo: Math.max(lo, nowPos + 15), hi,
                                 plannedExit: exitAt, entryPos, inDeck: stagingDeck() })
      : null;
    if (peakT) { recipe = peakT.recipe; bTime = peakT.bTime; exitAt = peakT.exitAt; }
    // Keep the exit on A's phrase grid: push by whole phrases, never by seconds.
    const phraseS = 32 * 60 / od0bpm;
    let effectiveATime = exitAt;
    while (effectiveATime < nowPos + 15) effectiveATime += phraseS;
    // vocalShort: xfDuration < 16 halves every bar count in executeTransition
    // (Bass Swap 8 -> 4 bars) so the overlap ends before B's vocal.
    const xfDuration = oneSong ? Math.max(16, w.xf) : vocalShort ? Math.min(8, w.xf) : peakT ? Math.max(16, w.xf) : w.xf;
    playPlanTag = ` | ${w.label} ${fmtTime(effectiveATime - entryPos)}`;

    let executed = false;
    let filled = false;
    let fireAt = effectiveATime; // B's entry lands exactly on this phrase line

    // Hand the plan to the DJ mind: it may pre-clear the outgoing bass or hold
    // the exit one phrase longer (bounded by the set-mode window + 16 bars).
    const barS = 240 / ((od && od.bpm) || 128);
    if (window.djMind) {
      window.djMind.setPlan({
        fireAt,
        // a vocal-free blend window is exact: the mind must not hold past it
        // vocal-aware plans are exact: a DJ-mind hold would move the overlap into a vocal
        maxFireAt: peakT || layer || (blend && (blend.instrumental || blend.vocals_known)) ? fireAt
          : Math.max(fireAt, Math.min(trackEnd, hi + 16 * barS)),
        style: peakT ? "peak" : overlapStyle,
        peakKind: peakT ? peakT.kind : null, peakWhy: peakT ? peakT.why : null, brake: !!(peakT && peakT.brake),
        preClearBars: Number.isFinite(candidate.pre_clear_bars) ? candidate.pre_clear_bars : 8,
      });
    }

    // Riff over rap (riff-over-rap.js): when the pair fits (3-15 % tempo gap,
    // A has a groove running into its own breakdown, B raps) and A's
    // key-locked stems render in time, the fire line moves to A's groove start
    // and the whole 64-bar move replaces the recipe.
    let riff = null, riffEntry = null;
    if (!layer && !peakT && riffOn() && window.riffOverRap) {
      const notBefore = deckPosition(activeDeck) + 25;
      window.riffOverRap.prepare(currentId, nextId, notBefore).then((r) => {
        if (!r || executed || !active || currentTrackId !== currentId) return;
        const g0 = r.plan.a_groove[0], pos = deckPosition(activeDeck);
        const sdB = window.decks && window.decks[stagingDeck()];
        if (g0 < pos + 6 || g0 > hi + 60 || !(sdB && sdB.stems)) {
          console.info("riff over rap: ready but", g0 < pos + 6 ? "A is past its groove" : g0 > hi + 60 ? "the groove comes too late" : "B's stems aren't loaded");
          return;
        }
        riff = r;
        fireAt = g0;
        recipe = "RIFF OVER RAP";
        if (window.djMind) window.djMind.setPlan({ fireAt, maxFireAt: fireAt, style: "layer", preClearBars: 0 });
      }).catch((e) => console.warn("riff over rap:", e.message));
    }

    const tick = setInterval(() => {
      if (!active) { clearInterval(tick); return; }
      if (window.djMind) fireAt = window.djMind.fireAt(fireAt);
      const pos = deckPosition(activeDeck);
      const left = fireAt - pos;

      // Live drums: 2-bar fill leading into the crossfade (glues the records),
      // rationed by the mind (study rule 8: FX stay the exception).
      const od0 = window.decks && window.decks[activeDeck];
      const barSecs = (60 / ((od0 && od0.bpm) || 128)) * 4;
      if (!filled && !layer && left > 0 && left <= 2 * barSecs && window.beatLayer) {
        filled = true;
        if (!window.djMind || window.djMind.fxAllowed("fill")) window.beatLayer.fill(2);
      }

      if (left > 0.8) {
        const scoreTag = score >= 65 ? `⭐${score}` : `⚡${score} (early exit)`;
        const bl = layer ? ` · ${layer.source} LAYER, bass to B at bar ${layer.hold_bars}${layer.third ? " + 3rd vocal" : ""}`
          : blend ? ` · beat blend ${blend.pitch_percent >= 0 ? "+" : ""}${blend.pitch_percent.toFixed(1)}%${blend.clean ? "" : " (short: both vocal)"}` : "";
        playPlanTag = ` | ${w.label} ${fmtTime(fireAt - entryPos)}${bl}`;
        apStatus(`Next: ${nextName} | ${recipe} | ${scoreTag}${playPlanTag} | in ${left.toFixed(0)}s${mashupTag}`);
        return;
      }
      if (executed) return;
      executed = true;
      clearInterval(tick);
      if (window.djMind) window.djMind.onTransition();

      if (window.mashup) window.mashup.cancel();
      mashupTag = "";
      apStatus(`Blending → ${nextName} (${recipe})…`);

      if (riff) {
        const outgoing = activeDeck, incoming = stagingDeck();
        const oaR = window.decks[outgoing];
        const leadR = Math.max(0.05, (fireAt - deckPosition(outgoing)) / oaR._playbackRate());
        const t0R = audioCtx.currentTime + leadR;
        const ui = {
          xf: (inn, f) => setRange(xfader, (inn === "b" ? 1 : -1) * f),
          eq: (d, band, v) => setRange(eqEl(d, band), v),
          pitch: (d, pct) => setDeckPitch(d, pct),
        };
        const totalMs = window.riffOverRap.run(riff, outgoing, incoming, t0R, ui);
        if (window.djMind && window.djMind.layering) {
          window.djMind.layering(totalMs / 1000, { source: "RIFF",
            why: `riff over rap: A's groove key-locked to ${riff.plan.target_bpm} BPM, B's rap on bar 40` });
        }
        riffEntry = riff.plan.b_entry;
        later(totalMs + 500, afterBlend);
        return;
      }

      // Tempo-lock B to A, then start it sample-accurately so B's entry
      // downbeat lands exactly on A's phrase line.
      const sd = window.decks && window.decks[stagingDeck()];
      const oa = window.decks && window.decks[activeDeck];
      const rateA = oa ? oa._playbackRate() : 1;
      if (sd && oa && oa.bpm > 0 && sd.bpm > 0) {
        // Live A tempo (A may still be easing back from its own tempo lock);
        // half/double time counts as a match.
        const aEff = oa.bpm * rateA;
        const lockRate = [1, 2, 0.5].map((m) => aEff / (sd.bpm * m))
          .reduce((best, r) => (Math.abs(r - 1) < Math.abs(best - 1) ? r : best));
        // Key-locked tempo stems (prefetched below): locks up to 15 % keep B's key.
        const keyLocked = sd.tempoStems && Math.abs(sd.tempoStems.bpm / (sd.bpm * lockRate) - 1) < 0.01;
        if (keyLocked) setDeckPitch(stagingDeck(), (lockRate - 1) * 100, 16);
        else if (Math.abs(lockRate - 1) <= 0.08) setDeckPitch(stagingDeck(), (lockRate - 1) * 100);
      }
      const leadS = Math.max(0.05, (fireAt - deckPosition(activeDeck)) / rateA);
      const t0 = audioCtx.currentTime + leadS;
      if (sd) sd.play(bTime, false, t0);
      // LAYER: B "arrives" at the bass hand-off; its play window counts from there
      const nextEntry = layer ? layer.b_swap : bTime;
      riffEntry = null;
      // Drop Swap + [[Backspin (Spinback)]]: A's build winds down (deck brake,
      // 0.8 s) into the downbeat where B's drop cuts in.
      if (peakT && peakT.brake && oa && typeof oa.brake === "function") {
        later(Math.max(0, leadS - 0.8) * 1000, () => oa.brake());
      }

      // Recipe-aware EQ-first transition, started on the same downbeat.
      const outgoing = activeDeck;
      const incoming = stagingDeck();
      // LAYER: the beat layer (live drums on A) and the mind's phrase moves pause
      // while two records ride, so no third drum line doubles up.
      // Non-layer transitions start XF_LOOKAHEAD_MS early and schedule their
      // automation on the audio clock at exactly t0 (B's first downbeat).
      later(layer ? leadS * 1000 : Math.max(0, leadS * 1000 - XF_LOOKAHEAD_MS), () => {
        let totalMs;
        if (layer) {
          if (window.beatLayer && window.beatLayer.isEnabled()) {
            window.beatLayer.setEnabled(false);
            beatMutedByLayer = true;
          }
          totalMs = executeLayer(outgoing, incoming, layer);
          if (window.djMind && window.djMind.layering) {
            window.djMind.layering(totalMs / 1000, { source: layer.source,
              why: `${layer.why} - ${layer.hold_bars} bars together, bass to B on the line, A unwinds ${layer.unwind_bars} bars` });
          }
        } else {
          totalMs = executeTransition(recipe, outgoing, incoming, xfDuration, t0) + XF_LOOKAHEAD_MS;
          window.dispatchEvent(new CustomEvent("ai-cue", { detail: { at: t0, kind: recipeKind(recipe) === "cut" ? "drop" : "transition",
            deck: incoming, bar: 240 / ((window.decks[incoming] && window.decks[incoming].bpm) || 128), why: `${recipe}: B's first downbeat` } }));
        }
        later(totalMs + 500, afterBlend);
      });

      // After the transition completes, update state and continue
      function afterBlend() {   // declaration: hoisted, the riff branch above calls it
        endAudioClock(); // controls are the user's again
        if (!active) return;

        // Stop the outgoing deck and put it back to neutral for its next load
        const od = window.decks && window.decks[outgoing];
        if (od) od.stopNow();
        if (window.stemMoves) window.stemMoves.reset(od);   // never leave its mix muted
        resetDeck(outgoing);

        history.push(nextName);
        advanceLead();
        playedIds.push(nextId);
        unmuteBeatLayer();
        genreLog.push(currentGenre || "");
        currentGenre = (scheduledNext && scheduledNext.suggestion && scheduledNext.suggestion.genre) || "";
        songsSinceJump = jumpPending ? 0 : songsSinceJump + 1;
        jumpPending = false;
        if (steering === "move") steerStep++;
        scheduledNext = null;
        activeDeck = stagingDeck();
        currentTrackId = nextId;
        entryPos = riffEntry != null ? riffEntry : nextEntry;
        currentEnergy = null;
        if (window.beatLayer) window.beatLayer.follow(activeDeck);
        if (window.djMind) {
          window.djMind.follow(activeDeck);
          window.djMind.setProfileEnergy(profileById[currentTrackId]);
        }
        easePitchHome(activeDeck);
        advanceBridge();

        // Park crossfader fully on the new active deck side
        setRange(xfader, activeDeck === "a" ? -1 : 1);

        prepareTransition(currentTrackId);
      }
    }, 200);
    runTimers.push(tick);
    return fireAt;
  }

  // ── set modes ─────────────────────────────────────────────────────────────
  // LONG   songs ride 3-6 min, long 24 s blends (Fred's layered, patient mode)
  // QUICK  1-2 min, 8 s blends, high energy (weak match bails at 1 min)
  // HYBRID per song: weak match or high energy (>= 7/10) -> quick,
  //        deep / low energy (<= 5/10) -> long, else in between
  let playPlanTag = "";
  function setMode() {
    const el = document.getElementById("ap-mode");
    const v = el ? el.value : "hybrid";
    return ["long", "quick", "hybrid"].includes(v) ? v : "hybrid";
  }

  const WINDOWS = {
    long:   { min: 180, max: 360, xf: 24, label: "LONG" },
    medium: { min: 120, max: 240, xf: 16, label: "MID" },
    quick:  { min: 60,  max: 120, xf: 8,  label: "QUICK" }, // user: "1-2 min"; 45 s felt rushed
    bail:   { min: 30,  max: 60,  xf: 8,  label: "QUICK·bail" },
    // steering toward the occasion's music: short bridge songs (user: 30-60 s each)
    bridge: { min: 30,  max: 60,  xf: 8,  label: "BRIDGE" },
  };

  function playWindow(score) {
    if (steering === "move") return WINDOWS.bridge;
    // A famous song plays in full (user; the USB002 set rides leavemealone for
    // 7 min): exit only in its last ~50 s, i.e. the outro. Stem breakdowns
    // (stem-moves.js) keep it from sounding long.
    const pd = window.decks && window.decks[activeDeck];
    if (pd && pd.fame && pd.fame.famous && pd.buffer) {
      const rem = pd.buffer.duration - (entryPos || 0);
      if (rem > 90) return { min: Math.max(60, rem - 50), max: Math.max(70, rem - 6), xf: 24, label: "FULL·famous" };
    }
    const mode = setMode();
    const weak = score < 65;
    if (mode === "long") return WINDOWS.long;
    if (mode === "quick") return weak ? WINDOWS.bail : WINDOWS.quick;
    if (weak) return WINDOWS.bail;
    if (currentEnergy != null && currentEnergy >= 7) return WINDOWS.quick;
    if (currentEnergy != null && currentEnergy <= 5) return WINDOWS.long;
    return WINDOWS.medium;
  }

  // ── live mashup ("A x B") ─────────────────────────────────────────────────
  // Before the transition, lay the NEXT track's vocal over one instrumental
  // 8/16-bar phrase of the current track (the Fred again.. "x" move: tease the
  // next record's voice over this beat, then bring the record itself in).
  // Restraint: at most one layer per track; skipped unless key and tempo fit.
  function riffOn() {
    const t = document.getElementById("ap-riff-toggle");
    return !t || t.checked;
  }

  function mashupsOn() {
    const t = document.getElementById("ap-mashup-toggle");
    return !t || t.checked;
  }

  function fmtTime(s) {
    const m = Math.floor(s / 60);
    return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  }

  async function tryMashup(hostId, guestId, guestName, fireAt) {
    if (!mashupsOn() || !window.mashup) return;
    const hostDeck = activeDeck;
    const d = window.decks && window.decks[hostDeck];
    if (!d) return;
    const bar = 240 / (d.bpm || 128);
    const room = fireAt - 2 * bar - (deckPosition(hostDeck) + 10);
    // 32 bars = full mashup (the guest's whole vocal phrase over this beat).
    const bars = room >= 32 * bar ? 32 : room >= 16 * bar ? 16 : room >= 8 * bar ? 8 : 0;
    if (!bars) return;
    // Live stems on the host: its own vocal can drop out under the guest's,
    // so the mashup no longer needs an instrumental stretch of this song.
    const hostMutable = !!d.stemsReady;
    try {
      const res = await fetch("/api/mashup/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ host_id: hostId, guest_id: guestId, bars, host_mutable: hostMutable }),
      });
      const plan = await res.json();
      if (!active || activeDeck !== hostDeck || currentTrackId !== hostId) return;
      if (!res.ok || !plan.ok) {
        console.info("Mashup skipped:", guestName, plan.detail || (plan.reasons || []).join("; "));
        return;
      }
      const pos = deckPosition(hostDeck);
      const entry = plan.host_entries.find((e) => e >= pos + 3 && e + plan.host_duration <= fireAt - bar);
      if (entry == null) return;
      if (await window.mashup.play(hostDeck, plan, entry)) {
        mashupTag = ` | ✕ ${guestName} vocal @${fmtTime(entry)} (${plan.bars} bars${plan.mute_host_vocals ? ", host instrumental" : ""})`;
        if (plan.mute_host_vocals && window.stemMoves) {
          // host goes instrumental for exactly the guest's phrase
          const sm = window.stemMoves;
          const onAt = sm.audioAt(d, entry), offAt = sm.audioAt(d, entry + plan.host_duration);
          setTimeout(() => d.stemMix({ vocals: 0 }, onAt, 0.05), Math.max(0, (onAt - audioCtx.currentTime) * 1000 - 200));
          setTimeout(() => d.stemMix(null, offAt, 0.2), Math.max(0, (offAt - audioCtx.currentTime) * 1000 - 200));
          window.dispatchEvent(new CustomEvent("ai-activity", { detail: { kind: "stem-move", deck: hostDeck,
            label: `FULL MASHUP · ${plan.bars} bars`, why: `${guestName} vocal over this song's instrumental` } }));
        }
        // stem remix inside the mashup: host drums + bass out for its last quarter,
        // the guest's vocal over the host's synths, everything back on the line
        if (window.stemMoves && d.stemsReady) window.stemMoves.mashupBreak(d, entry, plan.bars, !!plan.mute_host_vocals);
      }
    } catch (e) {
      console.warn("Mashup failed:", e.message);
    }
  }

  // ── start / stop ──────────────────────────────────────────────────────────
  // Session history: every song that has played audibly for >= 30 s this
  // session, autopilot or not ({id, name}, in play order). The set's
  // suggestions continue from it.
  const SESSION_MIN_S = 30;
  const session = [];
  const heard = {};                      // track id -> audible seconds
  setInterval(() => {
    for (const id of ["a", "b"]) {
      const d = window.decks && window.decks[id];
      const tid = window.state && (id === "a" ? window.state.trackA : window.state.trackB);
      if (!d || !d.playing || !tid) continue;
      const g = (d.crossfaderGain ? d.crossfaderGain.gain.value : 1) * (d.volumeGain ? d.volumeGain.gain.value : 1);
      if (g < 0.3) continue;
      heard[tid] = (heard[tid] || 0) + 1;
      if (heard[tid] === SESSION_MIN_S && !session.some((x) => x.id === tid)) {
        const el = document.getElementById(`title-${id}`);
        session.push({ id: tid, name: el ? el.textContent.trim() : tid });
        if (session.length > 60) session.shift();
      }
    }
  }, 1000);
  window.setSession = session;

  // The audible deck, else a loaded one: {deck, trackId, name, playing}
  function currentDeck() {
    let best = null;
    for (const id of ["a", "b"]) {
      const d = window.decks && window.decks[id];
      const tid = window.state && (id === "a" ? window.state.trackA : window.state.trackB);
      if (!d || !d.buffer || !tid) continue;
      const g = d.playing ? (d.crossfaderGain ? d.crossfaderGain.gain.value : 1) * (d.volumeGain ? d.volumeGain.gain.value : 1) : -1;
      const el = document.getElementById(`title-${id}`);
      const c = { deck: id, trackId: tid, name: el ? el.textContent.trim() : tid, playing: d.playing, level: g };
      if (!best || c.level > best.level) best = c;
    }
    return best;
  }

  // Common reset for any set start.
  function resetSetState() {
    history = [];
    steering = "stay";
    steerStep = 0;
    leadTo = null;
    leadStatus("");
    genreLog = [];
    currentGenre = "";
    songsSinceJump = 0;
    jumpPending = false;
    bridge = null;
    scheduledNext = null;
    pendingSugs = [];
    renderQueue([]);
  }

  // Start the set from the song playing now (or loaded): no download, no
  // restart; the session so far is the set's history.
  async function startFromCurrent() {
    const cur = currentDeck();
    if (!cur) { apStatus("Nothing loaded: load or play a song, or paste a seed URL."); return; }
    occasion = occasionInput ? occasionInput.value.trim() : "";
    resetSetState();
    active = true;
    activeDeck = cur.deck;
    updateButtons();
    currentTrackId = cur.trackId;
    const d = window.decks[cur.deck];
    if (!d.playing) d.play(d._currentPosition() || 0, true);
    if (xfader) { xfader.value = cur.deck === "a" ? "-1" : "1"; xfader.dispatchEvent(new Event("input")); }
    entryPos = Math.max(0, d._currentPosition() - (heard[cur.trackId] || 0));
    currentEnergy = null;
    if (window.beatLayer) window.beatLayer.follow(cur.deck);
    if (window.djMind) { window.djMind.reset(); window.djMind.follow(cur.deck); }
    const past = session.filter((x) => x.id !== cur.trackId);
    history = [...past.map((x) => x.name), cur.name];
    playedIds = [...past.map((x) => x.id), cur.trackId];
    setStartedAt = Date.now() - 1000 * past.reduce((sum, x) => sum + (heard[x.id] || 0), 0);
    apStatus(`▶ Set from ${cur.name}${past.length ? ` (after ${past.length} song${past.length > 1 ? "s" : ""} played)` : ""} — finding next track…`);
    startWatchdog();
    prepareTransition(currentTrackId);
  }

  async function start() {
    const url = seedInput ? seedInput.value.trim() : "";
    if (!url) return startFromCurrent();              // no seed: continue from what's playing
    occasion = occasionInput ? occasionInput.value.trim() : "";
    // High-energy occasions run in QUICK mode unless the user picked a mode.
    const modeEl = document.getElementById("ap-mode");
    if (modeEl && modeEl.value === "hybrid" && HIGH_ENERGY_OCCASION.test(occasion)) {
      modeEl.value = "quick";
      apStatus(`"${occasion}" is a high-energy occasion → QUICK mode`);
    }
    history = [];
    steering = "stay";
    steerStep = 0;
    leadTo = null;
    leadStatus("");
    genreLog = [];
    currentGenre = "";
    songsSinceJump = 0;
    jumpPending = false;
    bridge = null;
    active = true;
    activeDeck = "a";
    updateButtons();
    scheduledNext = null;
    pendingSugs = [];
    renderQueue([]);

    try {
      apStatus("Downloading seed track…");
      const tracks = await importUrl(url);
      if (!tracks.length) throw new Error("No tracks downloaded from seed URL.");

      const seed = tracks[0];
      currentTrackId = seed.track_id;
      const seedName = seed.display_name || seed.filename;

      apStatus(`Loading ${seedName} into deck A…`);
      const audioRes = await fetch(`/api/audio/tracks/${currentTrackId}`);
      if (!audioRes.ok) throw new Error(audioRes.statusText);
      const blob = await audioRes.blob();
      await loadIntoDeck("a", currentTrackId, seedName, blob);

      // Reset crossfader to A side
      if (xfader) { xfader.value = "-1"; xfader.dispatchEvent(new Event("input")); }

      // Play deck A
      const da = window.decks && window.decks.a;
      if (da) da.play(0, true);
      entryPos = 0;
      currentEnergy = null;
      if (window.beatLayer) window.beatLayer.follow("a");
      if (window.djMind) { window.djMind.reset(); window.djMind.follow("a"); }

      // the session so far steers the set too
      const past = session.filter((x) => x.id !== currentTrackId);
      history = [...past.map((x) => x.name), seedName];
      playedIds = [...past.map((x) => x.id), currentTrackId];
      setStartedAt = Date.now();
      apStatus(`▶ Playing: ${seedName} — finding next track in background…`);
      startWatchdog();
      prepareTransition(currentTrackId); // fire-and-forget: seed already playing

    } catch (e) {
      apStatus(`Autopilot error: ${e.message}`);
      active = false;
      updateButtons();
    }
  }

  // Watchdog: if no next song has been booked 150 s into a search, start a
  // fresh search (the old one is abandoned via prepGen). HOLD LOOP keeps the
  // music going meanwhile.
  const WATCHDOG_MS = 150000;
  let watchdog = null;
  function startWatchdog() {
    if (watchdog) clearInterval(watchdog);
    watchdog = setInterval(() => {
      if (!active || scheduledNext || !prepStartedAt) return;
      if (Date.now() - prepStartedAt < WATCHDOG_MS) return;
      console.warn("Autopilot watchdog: next-song search stalled, restarting it");
      apStatus("⚠ Next-song search stalled — restarting it");
      prepareTransition(currentTrackId);
    }, 10000);
  }

  function stop() {
    if (watchdog) { clearInterval(watchdog); watchdog = null; }
    active = false;
    ready.length = 0;
    cancelLead();
    scheduledNext = null;
    pendingSugs = [];
    if (window.mashup) window.mashup.cancel();
    mashupTag = "";
    clearRun();
    unmuteBeatLayer();
    if (window.beatLayer) window.beatLayer.stop();
    if (window.djMind) window.djMind.stop();
    apStatus("Autopilot stopped.");
    updateButtons();
  }

  function updateButtons() {
    if (startBtn) startBtn.disabled = active;
    const scb = document.getElementById("ap-start-current-btn");
    if (scb) scb.disabled = active;
    if (stopBtn)  stopBtn.disabled  = !active;
  }

  // Test hook: run one transition's automation on the empty decks (no audio,
  // no set) to check the audio-clock scheduling from the console.
  window.autopilotDebug = { executeTransition, xfGains };

  // Read-only view for helpers (beat-grid-ai.js).
  window.autopilotState = {
    get active() { return active; },
    get activeDeck() { return activeDeck; },
    get trackId() { return currentTrackId; },
    get genre() { return currentGenre; },
    get entryPos() { return entryPos; },
    get energy() { return currentEnergy; },
    get fireAt() { return scheduledNext ? scheduledFireAt : null; },
    get layering() { return !!(window.djMind && window.djMind.layerActive); },
  };

  const leadGo = document.getElementById("ap-lead-go");
  if (leadGo) leadGo.addEventListener("click", () => {
    // LEAD with the list open: selected row, else the first song result, else steer
    if (leadRows.length) pickLead(leadSel >= 0 ? leadSel : (leadRows.length > 1 ? 1 : 0));
    else startLead();
  });
  if (leadCancel) leadCancel.addEventListener("click", () => cancelLead("lead cancelled — the set carries on"));
  // LEAD TO search: YouTube results as you type (debounced); pick one = that
  // song is the destination; the first row steers toward the typed genre/artist.
  const leadInput = document.getElementById("ap-lead-input");
  const leadResults = document.getElementById("ap-lead-results");
  let leadSearchTimer = null, leadSearchSeq = 0, leadRows = [], leadSel = -1;
  function hideLeadResults() { if (leadResults) { leadResults.hidden = true; leadResults.innerHTML = ""; } leadRows = []; leadSel = -1; }
  function fmtDur(d) { return d ? `${Math.floor(d / 60)}:${String(Math.round(d % 60)).padStart(2, "0")}` : ""; }
  function renderLeadResults(q, results, note) {
    if (!leadResults) return;
    leadRows = [{ style: q }, ...results];
    leadResults.innerHTML = leadRows.map((r, i) => r.style
      ? `<li role="option" data-i="${i}" aria-selected="${i === leadSel}"><span class="r-title r-style">Steer toward “${esc(r.style)}” (genre / artist)</span><span class="r-meta">${esc(note || "")}</span></li>`
      : `<li role="option" data-i="${i}" aria-selected="${i === leadSel}"><span class="r-title">${esc(r.title)}</span><span class="r-meta">${esc(fmtDur(r.duration))}</span><span class="r-meta">${esc(r.channel)}</span></li>`).join("");
    leadResults.hidden = false;
  }
  function pickLead(i) {
    const r = leadRows[i];
    if (!r) return;
    if (r.style) { if (leadInput) leadInput.value = r.style; startLead(); }
    else startLead(r);
  }
  async function searchLead() {
    const q = leadInput ? leadInput.value.trim() : "";
    if (q.length < 2) { hideLeadResults(); return; }
    const seq = ++leadSearchSeq;
    renderLeadResults(q, [], "searching YouTube…");
    try {
      const res = await fetch(`/api/search/youtube?q=${encodeURIComponent(q)}&limit=8`);
      const data = await res.json();
      if (seq !== leadSearchSeq) return; // a newer query is in flight
      if (!res.ok) throw new Error(data.detail || res.statusText);
      renderLeadResults(q, data.results || [], (data.results || []).length ? "" : "no songs found");
    } catch (e) {
      if (seq === leadSearchSeq) renderLeadResults(q, [], `search failed: ${e.message}`);
    }
  }
  if (leadInput) {
    leadInput.addEventListener("input", () => { clearTimeout(leadSearchTimer); leadSearchTimer = setTimeout(searchLead, 450); });
    leadInput.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!leadRows.length) return;
        e.preventDefault();
        leadSel = (leadSel + (e.key === "ArrowDown" ? 1 : -1) + leadRows.length) % leadRows.length;
        renderLeadResults(leadRows[0].style, leadRows.slice(1));
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (leadSel >= 0) pickLead(leadSel);
        else { clearTimeout(leadSearchTimer); searchLead(); }
      } else if (e.key === "Escape") hideLeadResults();
    });
  }
  if (leadResults) leadResults.addEventListener("click", (e) => {
    const li = e.target.closest("li[data-i]");
    if (li) pickLead(Number(li.dataset.i));
  });

  startBtn.addEventListener("click", start);
  const startCurBtn = document.getElementById("ap-start-current-btn");
  if (startCurBtn) startCurBtn.addEventListener("click", () => { if (!active) startFromCurrent(); });
  if (stopBtn) stopBtn.addEventListener("click", stop);
  if (seedInput) seedInput.addEventListener("keydown", (e) => { if (e.key === "Enter") start(); });
})();
