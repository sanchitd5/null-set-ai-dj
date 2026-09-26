// AI DJ Autopilot
// Seed track → LLM suggests next → download → analyze → auto-crossfade → repeat.
//
// Deck alternation: even transitions crossfade A→B (xfader -1→+1),
// odd transitions crossfade B→A (xfader +1→-1). Tracks alternate slots.
//
// Requires: window.decks, window.loadIntoDeck, setStatus (app.js + deck-controller.js).

(function () {
  // ── state ─────────────────────────────────────────────────────────────────
  let active = false;
  let activeDeck = "a";       // which deck is currently playing
  let currentTrackId = null;
  let occasion = "";
  let history = [];           // display names of played tracks (last 5 kept)

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
  function rampParam(getEl, fromVal, toVal, durationMs) {
    const steps = 20;
    const interval = Math.max(16, durationMs / steps);
    const first = getEl();
    if (!first) return;
    const from = fromVal == null ? parseFloat(first.value) : fromVal;
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
  function executeTransition(recipe, out, inn, xfDuration) {
    clearRun();
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
    const at = (bars, fn) => later(bars * bar, fn);

    // Incoming deck always enters with its sub killed: single bass owner.
    setRange(lowIn(), LOW_KILL);
    setRange(eqEl(inn, "mid"), 0);
    setRange(eqEl(inn, "high"), 0);
    setRange(xfEl(), fromXf);

    let total;
    switch (kind) {
      case "bass": // 8 bars: cut A low, snap B low at the drop (bar 4)
        rampParam(lowOut, null, LOW_KILL, 4 * bar);
        rampParam(xfEl, fromXf, 0, 4 * bar);
        at(4, () => {
          rampParam(lowIn, LOW_KILL, 0, beat);
          rampParam(xfEl, 0, toXf, 4 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 4 * bar);
        });
        total = 8;
        break;

      case "double": // both drops together for 8 bars, then cut A
        setRange(xfEl(), 0);
        rampParam(lowOut, null, LOW_KILL, beat);
        rampParam(lowIn, LOW_KILL, 0, beat);
        at(8, () => setRange(xfEl(), toXf));
        total = 8.5;
        break;

      case "echo": // arm ECHO on A, kill its low, tail carries B's entry
        setFx(out, "echo", 0.7);
        rampParam(lowOut, null, LOW_KILL, 2 * bar);
        rampParam(xfEl, fromXf, 0, 4 * bar);
        at(4, () => {
          rampParam(lowIn, LOW_KILL, 0, bar);
          rampParam(xfEl, 0, toXf, 2 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 2 * bar);
        });
        total = 8;
        break;

      case "filter": // sweep A low/mid down over 4 bars, swap at centre
        rampParam(lowOut, null, LOW_KILL, 4 * bar);
        rampParam(midOut, null, -10, 4 * bar);
        rampParam(xfEl, fromXf, 0, 4 * bar);
        at(4, () => {
          rampParam(lowIn, LOW_KILL, 0, 2 * bar);
          rampParam(xfEl, 0, toXf, 4 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 4 * bar);
        });
        total = 8;
        break;

      case "cut": // instant snap on the phrase boundary
        setRange(lowOut(), LOW_KILL);
        setRange(lowIn(), 0);
        setRange(xfEl(), toXf);
        total = 1;
        break;

      case "loop": // 2-bar loop roll on A holds the exit point steady
        setLoopLength(out, 8);
        setLoop(out, true);
        at(2, () => {
          rampParam(lowOut, null, LOW_KILL, 2 * bar);
          rampParam(xfEl, fromXf, 0, 2 * bar);
        });
        at(4, () => {
          rampParam(lowIn, LOW_KILL, 0, beat);
          rampParam(xfEl, 0, toXf, 2 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 2 * bar);
        });
        at(6, () => setLoop(out, false));
        total = 8;
        break;

      case "blend": // 16-bar EQ-first blend
        rampParam(lowOut, null, LOW_KILL, 4 * bar);
        at(4, () => rampParam(xfEl, fromXf, 0, 4 * bar));
        at(8, () => {
          rampParam(lowIn, LOW_KILL, 0, 2 * bar);
          rampParam(xfEl, 0, toXf, 8 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 8 * bar);
        });
        total = 16;
        break;

      default: // 8-bar bass swap then 8-bar crossfader sweep
        rampParam(lowOut, null, LOW_KILL, 4 * bar);
        at(4, () => rampParam(lowIn, LOW_KILL, 0, 4 * bar));
        at(8, () => {
          rampParam(xfEl, fromXf, toXf, 8 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 8 * bar);
        });
        total = 16;
        break;
    }
    return total * bar;
  }

  const ENERGY_DELTA_CLASS = { up: "ap-energy-up", down: "ap-energy-down", maintain: "ap-energy-hold" };
  const ENERGY_DELTA_LABEL = { up: "↑ Energy up", down: "↓ Energy down", maintain: "→ Hold energy" };

  function renderQueue(items) {
    if (!queueEl) return;
    if (!items.length) {
      queueEl.innerHTML = "<div class='ap-empty'>⏳ Finding next track…</div>";
      return;
    }
    queueEl.innerHTML = items.map((s, i) => {
      const eClass = ENERGY_DELTA_CLASS[s.energy_delta] || "";
      const eLabel = ENERGY_DELTA_LABEL[s.energy_delta] || "";
      const genre   = s.genre ? `<span class="ap-genre">${s.genre}</span>` : "";
      const moment  = s.mix_moment ? `<span class="ap-moment" title="Mix moment">${s.mix_moment}</span>` : "";
      const energy  = eLabel ? `<span class="ap-energy ${eClass}">${eLabel}</span>` : "";
      const vibe    = s.vibe_link ? `<span class="ap-vibe">"${s.vibe_link}"</span>` : "";
      return `
      <div class="ap-item ${i === 0 ? "ap-next" : ""}" id="${i === 0 ? "ap-next-item" : ""}">
        <span class="ap-pos">${i === 0 ? "NEXT" : `+${i + 1}`}</span>
        <div class="ap-item-main">
          <span class="ap-name">${s.artist || "?"} — ${s.title || "?"}</span>
          <span class="ap-meta">${s.expected_key || ""}${s.expected_bpm ? "  " + s.expected_bpm + " BPM" : ""}${genre ? "  " + genre : ""}${i === 0 ? '  <span id="ap-match-score" style="display:none"></span>' : ""}</span>
          <span class="ap-badges">${energy}${moment}</span>
          ${vibe}
          <span class="ap-why">${s.reason || ""}</span>
        </div>
      </div>`;
    }).join("");
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
        const dn = (tr.display_name || tr.filename || "").toLowerCase();
        return dn.includes(a) && dn.includes(t);
      }) || null;
    } catch { return null; }
  }

  async function getSuggestions(trackId) {
    const setPos = Math.min(history.length / 10, 1.0);
    const res = await fetch("/api/autopilot/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: trackId, occasion, history: history.slice(-6), set_position: setPos }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
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

  // ── core loop ─────────────────────────────────────────────────────────────
  async function prepareTransition(currentId) {
    if (!active) return;
    apStatus("⏳ Loading next track — AI selecting…");
    renderQueue([]);

    let suggestions;
    try {
      suggestions = await getSuggestions(currentId);
    } catch (e) {
      apStatus(`Suggest error: ${e.message}`);
      active = false;
      updateButtons();
      return;
    }
    renderQueue(suggestions);

    for (const s of suggestions) {
      if (!active) return;
      try {
        const label = `${s.artist} — ${s.title}`;

        // Check cache before downloading — skip yt-dlp if already on server.
        let nextId, nextName;
        const cached = await findCached(s.artist, s.title);
        if (cached) {
          apStatus(`Using cached: ${label}`);
          nextId   = cached.track_id;
          nextName = cached.display_name || cached.filename || label;
        } else {
          apStatus(`Downloading: ${label}…`);
          const tracks = await importUrl(s.search_query);
          if (!tracks.length) continue;
          nextId   = tracks[0].track_id;
          nextName = tracks[0].display_name || label;
        }

        apStatus(`Matching transition…`);
        const candidate = await matchTracks(currentId, nextId);
        if (!candidate) continue;

        // Measured vibe gate: reject candidates whose loudness / brightness /
        // onset density / energy sit too far from what is playing right now.
        if (candidate.vibe && candidate.vibe.ok === false) {
          const why = (candidate.vibe.reasons || []).join("; ") || `distance ${candidate.vibe.distance}`;
          console.warn("Autopilot vibe reject:", s.title, why);
          apStatus(`Skipping ${nextName}: ${why}`);
          continue;
        }

        // Show match score on the NEXT queue card.
        const scoreEl = document.getElementById("ap-match-score");
        if (scoreEl) {
          const sc = Math.round(candidate.score || 0);
          const good = sc >= 65;
          scoreEl.textContent = `${good ? "⭐" : "⚡"} ${sc}/100${good ? "" : " · early exit"}`;
          scoreEl.style.cssText = `display:inline;font-weight:700;color:${good ? "#4ade80" : "#f97316"};margin-left:6px`;
        }

        // Preload next track into staging deck
        apStatus(`Loading ${nextName} into deck ${stagingDeck().toUpperCase()}…`);
        const audioRes = await fetch(`/api/audio/tracks/${nextId}`);
        if (!audioRes.ok) continue;
        const blob = await audioRes.blob();
        await loadIntoDeck(stagingDeck(), nextId, nextName, blob);

        scheduleTransition(currentId, nextId, nextName, candidate);
        return;
      } catch (e) {
        console.warn("Autopilot suggestion failed:", s.title, e.message);
      }
    }
    apStatus("All suggestions failed — autopilot stopped.");
    active = false;
    updateButtons();
  }

  function scheduleTransition(currentId, nextId, nextName, candidate) {
    if (!active) return;
    const bTime = candidate.b_time || 0;
    const recipe = candidate.recipe || "Blend";
    const score  = candidate.score  || 50;

    // Cap play time: good match (score ≥ 65) → max 120s; poor match → 60s.
    // This keeps sets moving and bails early on weak transitions.
    const MAX_PLAY_SECS = score >= 65 ? 120 : 60;
    const nowPos  = deckPosition(activeDeck);
    const hardCap = nowPos + MAX_PLAY_SECS;

    // Use the recipe's suggested exit point, but never past the hard cap.
    // Ensure at least 15s of play before any crossfade fires.
    const aTime = Math.min(candidate.a_time, hardCap);
    const MIN_PLAY_SECS = 15;
    const effectiveATime = Math.max(aTime, nowPos + MIN_PLAY_SECS);

    // Shorter crossfade for early-bail situations so it doesn't drag.
    const xfDuration = aTime < hardCap ? 16 : 8;

    let executed = false;
    const fireAt = effectiveATime - 1; // start crossfade 1s early

    const tick = setInterval(() => {
      if (!active) { clearInterval(tick); return; }
      const pos = deckPosition(activeDeck);
      const left = fireAt - pos;

      if (left > 0) {
        const scoreTag = score >= 65 ? `⭐${score}` : `⚡${score} (early exit)`;
        apStatus(`Next: ${nextName} | ${recipe} | ${scoreTag} | in ${left.toFixed(0)}s`);
        return;
      }
      if (executed) return;
      executed = true;
      clearInterval(tick);

      apStatus(`Crossfading → ${nextName} (${recipe})…`);

      // Start the staging deck at b_time
      const sd = window.decks && window.decks[stagingDeck()];
      if (sd) sd.play(bTime);

      // Recipe-aware EQ-first transition toward the staging deck
      const outgoing = activeDeck;
      const totalMs = executeTransition(recipe, outgoing, stagingDeck(), xfDuration);

      // After the transition completes, update state and continue
      later(totalMs + 500, () => {
        if (!active) return;

        // Stop the outgoing deck and put it back to neutral for its next load
        const od = window.decks && window.decks[outgoing];
        if (od) od.stopNow();
        resetDeck(outgoing);

        history.push(nextName);
        activeDeck = stagingDeck();
        currentTrackId = nextId;

        // Park crossfader fully on the new active deck side
        setRange(xfader, activeDeck === "a" ? -1 : 1);

        prepareTransition(currentTrackId);
      });
    }, 500);
    runTimers.push(tick);
  }

  // ── start / stop ──────────────────────────────────────────────────────────
  async function start() {
    const url = seedInput ? seedInput.value.trim() : "";
    if (!url) { apStatus("Paste a seed URL first."); return; }
    occasion = occasionInput ? occasionInput.value.trim() : "";
    history = [];
    active = true;
    activeDeck = "a";
    updateButtons();
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

      history = [seedName];
      apStatus(`▶ Playing: ${seedName} — finding next track in background…`);
      prepareTransition(currentTrackId); // fire-and-forget: seed already playing

    } catch (e) {
      apStatus(`Autopilot error: ${e.message}`);
      active = false;
      updateButtons();
    }
  }

  function stop() {
    active = false;
    clearRun();
    apStatus("Autopilot stopped.");
    updateButtons();
  }

  function updateButtons() {
    if (startBtn) startBtn.disabled = active;
    if (stopBtn)  stopBtn.disabled  = !active;
  }

  startBtn.addEventListener("click", start);
  if (stopBtn) stopBtn.addEventListener("click", stop);
  if (seedInput) seedInput.addEventListener("keydown", (e) => { if (e.key === "Enter") start(); });
})();
