// AI Music Brain — minimal interactive workbench frontend.
// Uploads two tracks, renders dual waveforms, requests AI transition
// suggestions, and plays back rendered previews (spec 1.6 / 3.8). Also
// supports manually clicking a rough point on each waveform and picking
// any of the 28 recipes yourself (spec 5: pick points -> pick effect ->
// render), rather than only accepting the AI's own suggestions.

const state = { trackA: null, trackB: null, aTime: null, bTime: null };
window.state = state;

// Stacked A-over-B lanes sharing one viewport. autoCenter/autoScroll mean that
// once you zoom past "fit the whole track", the playhead parks near the centre
// of its lane and the waveform scrolls underneath it — the fixed-centre-needle
// feel, without giving up WaveSurfer's click-to-seek and Regions trim handles.
const waveformA = WaveSurfer.create({
  container: "#waveform-a",
  waveColor: "#00803a",
  progressColor: "#00ff66",
  cursorColor: "#b8ffd3",
  cursorWidth: 2,
  height: 84,
  autoCenter: true,
  autoScroll: true,
});
window.waveformA = waveformA;

const waveformB = WaveSurfer.create({
  container: "#waveform-b",
  waveColor: "#8a1a76",
  progressColor: "#ff2bd6",
  cursorColor: "#ffc2f1",
  cursorWidth: 2,
  height: 84,
  autoCenter: true,
  autoScroll: true,
});
window.waveformB = waveformB;

const statusEl = document.getElementById("status");
const renderBtn = document.getElementById("render-btn");
const recipeSelect = document.getElementById("recipe-select");
const manualPreviewBtn = document.getElementById("manual-preview-btn");
const clearPointsBtn = document.getElementById("clear-points-btn");
const stemsABtn = document.getElementById("stems-a-btn");
const stemsBBtn = document.getElementById("stems-b-btn");
const aiHint = document.getElementById("ai-hint");
const pointAEl = document.getElementById("point-a");
const pointBEl = document.getElementById("point-b");
const candidatesEl = document.getElementById("candidates");
const previewSection = document.getElementById("preview-player");
const previewAudio = document.getElementById("preview-audio");
const previewExplanation = document.getElementById("preview-explanation");
const renderSection = document.getElementById("render-result");
const renderAudio = document.getElementById("render-audio");
const renderExplanation = document.getElementById("render-explanation");
const renderDownload = document.getElementById("render-download");

function setStatus(text) {
  statusEl.textContent = text;
}

function formatTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = (seconds % 60).toFixed(1);
  return `${m}:${s.padStart(4, "0")}`;
}

function updatePointReadouts() {
  pointAEl.innerHTML = state.aTime === null
    ? "Exit point: <em>not set — AI will pick one</em>"
    : `Exit point: ${formatTime(state.aTime)}`;
  pointBEl.innerHTML = state.bTime === null
    ? "Entry point: <em>not set — AI will pick one</em>"
    : `Entry point: ${formatTime(state.bTime)}`;
}

function bothTracksLoaded() {
  return Boolean(state.trackA && state.trackB);
}

function refreshControlAvailability() {
  const ready = bothTracksLoaded();
  recipeSelect.disabled = !ready;
  manualPreviewBtn.disabled = !ready;
  clearPointsBtn.disabled = !ready;
  stemsABtn.disabled = !state.trackA;
  stemsBBtn.disabled = !state.trackB;
}

async function loadRecipeOptions() {
  const res = await fetch("/api/recipes");
  const data = await res.json();
  data.recipes
    .sort((a, b) => a.name.localeCompare(b.name))
    .forEach((recipe) => {
      const opt = document.createElement("option");
      opt.value = recipe.name;
      opt.textContent = recipe.name;
      recipeSelect.appendChild(opt);
    });
}
loadRecipeOptions();

// Buffers decoded here and handed to the deck engine (see below).
window.decodedBuffers = { a: null, b: null };

// The single load path for a deck. Both the file picker (fresh upload) and the
// track browser (an already-uploaded track streamed back from the server) end
// up here, so a deck populates identically either way: waveform, analysis,
// BPM, key, beat grid, hot-cue markers and trim region.
//
// We decode the audio ourselves with Web Audio and hand WaveSurfer the channel
// data + duration, instead of letting it wait on an <audio> element. The deck
// engine plays through AudioBufferSourceNode, so that media element was never
// used for playback -- decoding once here is both faster and avoids the
// browser deferring media loads (e.g. in a backgrounded tab).
async function loadIntoDeck(deckLetter, trackId, displayName, blob) {
  const key = deckLetter === "a" ? "trackA" : "trackB";
  state[key] = trackId;

  const titleEl = document.getElementById(`title-${deckLetter}`);
  if (titleEl) titleEl.textContent = displayName;

  const waveform = deckLetter === "a" ? waveformA : waveformB;
  const objectUrl = URL.createObjectURL(blob);

  try {
    const audioBuffer = await audioCtx.decodeAudioData(await blob.arrayBuffer());
    window.decodedBuffers[deckLetter] = audioBuffer;
    const channels = [];
    for (let c = 0; c < audioBuffer.numberOfChannels; c++) channels.push(audioBuffer.getChannelData(c));
    waveform.load(objectUrl, channels, audioBuffer.duration);
  } catch (e) {
    // Fall back to WaveSurfer's own decoding if Web Audio can't read the file.
    window.decodedBuffers[deckLetter] = null;
    waveform.load(objectUrl);
  }

  refreshControlAvailability();
  document.dispatchEvent(new CustomEvent("deck-track-loaded", { detail: { deck: deckLetter, trackId } }));
}
window.loadIntoDeck = loadIntoDeck;

document.addEventListener("deck-track-loaded", () => { maybeAutoSuggest(); });

async function uploadTrack(file, waveform, which) {
  setStatus(`Uploading ${which}...`);
  const deckLetter = which === "Track A" ? "a" : "b";
  const form = new FormData();
  form.append("file", file);
  const res = await fetch("/api/tracks", { method: "POST", body: form });
  const data = await res.json();

  await loadIntoDeck(deckLetter, data.track_id, file.name, file);
  setStatus(`${which} uploaded (${data.track_id}).`);
}

document.getElementById("file-a").addEventListener("change", (e) => {
  if (e.target.files[0]) uploadTrack(e.target.files[0], waveformA, "Track A");
});

document.getElementById("file-b").addEventListener("change", (e) => {
  if (e.target.files[0]) uploadTrack(e.target.files[0], waveformB, "Track B");
});

let isShiftActive = false;
window.addEventListener("keydown", (e) => { if (e.key === "Shift") isShiftActive = true; }, true);
window.addEventListener("keyup", (e) => { if (e.key === "Shift") isShiftActive = false; }, true);

// Primary clicks seek like normal DJ software. Shift-click pins an AI point.
function bindWaveformInteraction(waveform, deckId, pointKey, label) {
  let lastShiftKey = false;
  const container = document.getElementById(`waveform-${deckId}`);
  if (container) {
    container.addEventListener("pointerdown", (e) => {
      lastShiftKey = Boolean(e.shiftKey);
    }, true);
    container.addEventListener("click", (e) => {
      lastShiftKey = Boolean(e.shiftKey);
    }, true);
  }

  waveform.on("interaction", (newTime, event) => {
    const isShift = (event && event.shiftKey) || lastShiftKey || isShiftActive;
    lastShiftKey = false;
    if (isShift) {
      state[pointKey] = newTime;
      updatePointReadouts();
      setStatus(`${label} point set at ${formatTime(newTime)} (snaps to the nearest phrase boundary).`);
      window.emitDJEvent && window.emitDJEvent("transition-pin", { deck: deckId, value: newTime });
      return;
    }
    const deck = window.decks && window.decks[deckId];
    if (deck) deck.seek(newTime);
    setStatus(`Deck ${deckId.toUpperCase()} sought to ${formatTime(newTime)}. Shift-click to pin a transition point.`);
  });
}
bindWaveformInteraction(waveformA, "a", "aTime", "Exit");
bindWaveformInteraction(waveformB, "b", "bTime", "Entry");

clearPointsBtn.addEventListener("click", () => {
  state.aTime = null;
  state.bTime = null;
  updatePointReadouts();
  setStatus("Cleared manual points — the AI will pick its own again.");
});

// Suggestions appear the moment both decks are loaded — no button press.
// Re-triggered automatically whenever a deck's track changes.
let lastSuggestedPair = null;

async function maybeAutoSuggest() {
  if (!bothTracksLoaded()) {
    candidatesEl.innerHTML = "";
    renderBtn.disabled = true;
    if (aiHint) aiHint.textContent = "Load both decks — suggestions appear automatically";
    return;
  }
  const pairKey = `${state.trackA}:${state.trackB}`;
  if (pairKey === lastSuggestedPair) return;
  lastSuggestedPair = pairKey;
  await suggestTransitions();
}

async function suggestTransitions() {
  if (aiHint) aiHint.textContent = "Scoring all 28 recipes against this pair...";
  candidatesEl.innerHTML = "";
  const res = await fetch("/api/match", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ track_a_id: state.trackA, track_b_id: state.trackB, top_n: 3 }),
  });
  const data = await res.json();
  if (aiHint) aiHint.textContent = `${data.candidates.length} suggestions — hover to preview the points, click to audition`;
  renderBtn.disabled = false;

  data.candidates.forEach((candidate) => {
    const card = document.createElement("div");
    card.className = "candidate-card";
    card.innerHTML = `
      <h3>${candidate.recipe}</h3>
      <div class="score">${candidate.score.toFixed(0)}% match</div>
      <p>${candidate.explanation}</p>
    `;
    card.addEventListener("mouseenter", () => showGhostMarkers(candidate.a_time, candidate.b_time));
    card.addEventListener("mouseleave", clearGhostMarkers);
    card.addEventListener("click", () => {
      window.liveTransition && window.liveTransition.select(candidate);
      renderTransition({ recipe: candidate.recipe, useManualPoints: false });
    });
    candidatesEl.appendChild(card);
  });
}

// Ghost markers: a translucent line on each waveform showing exactly where a
// hovered suggestion would enter/exit, before you commit to auditioning it.
function showGhostMarkers(aTime, bTime) {
  clearGhostMarkers();
  placeGhostMarker("cues-a", waveformA, aTime);
  placeGhostMarker("cues-b", waveformB, bTime);
}

function placeGhostMarker(containerId, waveform, time) {
  const container = document.getElementById(containerId);
  const duration = waveform.getDuration();
  if (!container || !duration) return;
  const marker = document.createElement("div");
  marker.className = "ghost-marker";
  marker.style.left = `${Math.min(Math.max((time / duration) * 100, 0), 100)}%`;
  container.appendChild(marker);
}

function clearGhostMarkers() {
  document.querySelectorAll(".ghost-marker").forEach((el) => el.remove());
}

manualPreviewBtn.addEventListener("click", () => {
  renderTransition({ recipe: recipeSelect.value || null, useManualPoints: true });
});

async function renderTransition({ recipe, useManualPoints }) {
  const label = recipe || "the AI's top pick";
  setStatus(`Rendering ${label}${useManualPoints ? " at your points" : ""}...`);

  const body = { track_a_id: state.trackA, track_b_id: state.trackB };
  if (recipe) body.recipe = recipe;
  if (useManualPoints) {
    if (state.aTime !== null) body.a_time = state.aTime;
    if (state.bTime !== null) body.b_time = state.bTime;
  }

  const res = await fetch("/api/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    setStatus(`Error: ${err.detail}`);
    return;
  }

  const data = await res.json();
  previewAudio.src = data.audio_url;
  previewExplanation.textContent = `${data.explanation} (rendered at A=${formatTime(data.a_time)}, B=${formatTime(data.b_time)})`;
  previewSection.hidden = false;
  previewAudio.play();
  setStatus(`Previewing ${data.recipe} (rendered in ${(data.render_time_seconds * 1000).toFixed(0)}ms).`);
  lastRenderRequestBody = body; // RENDER FULL MIX reuses exactly what was just auditioned
}

// --- RENDER FULL MIX: an offline export action (secondary to the live
// preview above), not a performance control — separate button, separate
// status area, ends in a downloadable file. ---
let lastRenderRequestBody = null;

renderBtn.addEventListener("click", async () => {
  const body = lastRenderRequestBody || { track_a_id: state.trackA, track_b_id: state.trackB };
  setStatus("Rendering the full mix (this can take longer than a preview)...");
  renderBtn.disabled = true;
  const originalLabel = renderBtn.textContent;
  renderBtn.textContent = "RENDERING…";

  try {
    const res = await fetch("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      setStatus(`Render error: ${err.detail}`);
      return;
    }
    const data = await res.json();
    renderAudio.src = data.audio_url;
    renderExplanation.textContent = `${data.explanation} — ${formatTime(data.duration_seconds)} total (rendered in ${data.render_time_seconds.toFixed(1)}s).`;
    renderDownload.href = data.audio_url;
    renderSection.hidden = false;
    setStatus(`Full mix ready: ${data.recipe}.`);
  } finally {
    renderBtn.disabled = false;
    renderBtn.textContent = originalLabel;
  }
});

// --- Stems: separates a deck's track into Demucs stems so stem-based
// recipes (Acapella Overlay, Stems Transition, ...) become reachable. ---
async function separateDeckStems(deckLetter, btn) {
  const trackId = deckLetter === "a" ? state.trackA : state.trackB;
  if (!trackId) return;
  const originalLabel = btn.textContent;
  btn.disabled = true;
  btn.textContent = `STEMS ${deckLetter.toUpperCase()}…`;
  setStatus(`Separating Deck ${deckLetter.toUpperCase()} into stems (first run downloads the Demucs model)...`);
  try {
    const res = await fetch(`/api/tracks/${trackId}/separate?stems=4`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      setStatus(`Stem separation error: ${err.detail}`);
      btn.textContent = originalLabel;
      return;
    }
    btn.textContent = `STEMS ${deckLetter.toUpperCase()} ✓`;
    btn.classList.add("stems-ready");
    setStatus(`Deck ${deckLetter.toUpperCase()} stems ready — stem-based recipes are now usable.`);
  } finally {
    btn.disabled = false;
  }
}

stemsABtn.addEventListener("click", () => separateDeckStems("a", stemsABtn));
stemsBBtn.addEventListener("click", () => separateDeckStems("b", stemsBBtn));
