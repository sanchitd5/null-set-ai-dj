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
  let transitionTimer = null;

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

  function animateCrossfader(fromVal, toVal, durationSec) {
    if (!xfader) return;
    const steps = Math.max(30, Math.round(durationSec * 15));
    const stepMs = (durationSec * 1000) / steps;
    let step = 0;
    clearInterval(transitionTimer);
    transitionTimer = setInterval(() => {
      step++;
      const v = fromVal + (toVal - fromVal) * (step / steps);
      xfader.value = v.toFixed(4);
      xfader.dispatchEvent(new Event("input"));
      if (step >= steps) { clearInterval(transitionTimer); transitionTimer = null; }
    }, stepMs);
  }

  function renderQueue(items) {
    if (!queueEl) return;
    if (!items.length) { queueEl.innerHTML = "<div class='ap-empty'>Generating set…</div>"; return; }
    queueEl.innerHTML = items.map((s, i) => `
      <div class="ap-item ${i === 0 ? "ap-next" : ""}">
        <span class="ap-pos">${i === 0 ? "NEXT" : `+${i + 1}`}</span>
        <span class="ap-name">${s.artist || "?"} — ${s.title || "?"}</span>
        <span class="ap-meta">${s.expected_key || ""}  ${s.expected_bpm ? s.expected_bpm + " BPM" : ""}</span>
        <span class="ap-why">${s.reason || ""}</span>
      </div>`).join("");
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

  async function getSuggestions(trackId) {
    const res = await fetch("/api/autopilot/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: trackId, occasion, history: history.slice(-5) }),
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
    return (data.candidates || [])[0] || null;
  }

  // ── core loop ─────────────────────────────────────────────────────────────
  async function prepareTransition(currentId) {
    if (!active) return;
    apStatus("AI choosing next track…");

    const suggestions = await getSuggestions(currentId);
    renderQueue(suggestions);

    for (const s of suggestions) {
      if (!active) return;
      try {
        const label = `${s.artist} — ${s.title}`;
        apStatus(`Downloading: ${label}…`);
        const tracks = await importUrl(s.search_query);
        if (!tracks.length) continue;

        const nextId = tracks[0].track_id;
        const nextName = tracks[0].display_name || label;

        apStatus(`Matching transition…`);
        const candidate = await matchTracks(currentId, nextId);
        if (!candidate) continue;

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
    const aTime = candidate.a_time;
    const bTime = candidate.b_time || 0;
    const recipe = candidate.recipe || "Blend";

    let executed = false;
    const xfDuration = 16; // seconds — roughly 8 bars at 120 BPM
    const fireAt = aTime - 1; // start crossfade 1s early

    const tick = setInterval(() => {
      if (!active) { clearInterval(tick); return; }
      const pos = deckPosition(activeDeck);
      const left = fireAt - pos;

      if (left > 0) {
        apStatus(`Next: ${nextName} | ${recipe} in ${left.toFixed(0)}s`);
        return;
      }
      if (executed) return;
      executed = true;
      clearInterval(tick);

      apStatus(`Crossfading → ${nextName} (${recipe})…`);

      // Start the staging deck at b_time
      const sd = window.decks && window.decks[stagingDeck()];
      if (sd) sd.play(bTime);

      // Animate crossfader toward staging deck
      const fromXf = activeDeck === "a" ? -1 : 1;
      const toXf   = activeDeck === "a" ?  1 : -1;
      animateCrossfader(fromXf, toXf, xfDuration);

      // After crossfade completes, update state and continue
      setTimeout(() => {
        if (!active) return;

        // Stop the outgoing deck
        const od = window.decks && window.decks[activeDeck];
        if (od) od.stopNow();

        history.push(nextName);
        activeDeck = stagingDeck();
        currentTrackId = nextId;

        // Reset crossfader fully to new active deck side before next transition
        if (xfader) {
          xfader.value = activeDeck === "a" ? "-1" : "1";
          xfader.dispatchEvent(new Event("input"));
        }

        prepareTransition(currentTrackId);
      }, (xfDuration + 1) * 1000);
    }, 500);
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
      apStatus(`Playing: ${seedName}`);
      prepareTransition(currentTrackId);

    } catch (e) {
      apStatus(`Autopilot error: ${e.message}`);
      active = false;
      updateButtons();
    }
  }

  function stop() {
    active = false;
    clearInterval(transitionTimer);
    transitionTimer = null;
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
