// AI Music Brain — performance layer: synthesized sampler pads, mix recording,
// and the keyboard shortcut map (spec 1.5 "Sampler", "Recording",
// "Keyboard shortcuts").
//
// Depends on globals from deck-controller.js (`audioCtx`, `masterGain`,
// `decks`, `initKnob`, `startBend`, `endBend`) and fx-rack.js (`toggleFx`).

// ============================ Sampler pads ==================================
//
// There is no sample library in this repo, so every pad is synthesized on the
// fly from oscillators and generated white noise — a real, distinct hit per
// pad, not a stub. Pads run into masterGain so they are heard through the
// master fader and captured by the recorder.

let noiseBuffer = null;
function getNoiseBuffer() {
  if (noiseBuffer) return noiseBuffer;
  const len = audioCtx.sampleRate * 2;
  noiseBuffer = audioCtx.createBuffer(1, len, audioCtx.sampleRate);
  const data = noiseBuffer.getChannelData(0);
  for (let i = 0; i < len; i++) data[i] = Math.random() * 2 - 1;
  return noiseBuffer;
}

function noiseVoice(dest, { duration, type, freq, q = 1, gain = 1, decay, when }) {
  const src = audioCtx.createBufferSource();
  src.buffer = getNoiseBuffer();
  const filter = audioCtx.createBiquadFilter();
  filter.type = type;
  filter.frequency.value = freq;
  filter.Q.value = q;
  const env = audioCtx.createGain();
  const t = Math.max(audioCtx.currentTime, when || 0);
  env.gain.setValueAtTime(0.0001, t);
  env.gain.exponentialRampToValueAtTime(gain, t + 0.003);
  env.gain.exponentialRampToValueAtTime(0.0001, t + (decay || duration));
  src.connect(filter);
  filter.connect(env);
  env.connect(dest);
  src.start(t);
  src.stop(t + duration + 0.05);
}

function toneVoice(dest, { wave = "sine", from, to, duration, gain = 1, glideTime, when }) {
  const osc = audioCtx.createOscillator();
  osc.type = wave;
  const t = Math.max(audioCtx.currentTime, when || 0);
  osc.frequency.setValueAtTime(from, t);
  osc.frequency.exponentialRampToValueAtTime(to, t + (glideTime || duration));
  const env = audioCtx.createGain();
  env.gain.setValueAtTime(0.0001, t);
  env.gain.exponentialRampToValueAtTime(gain, t + 0.004);
  env.gain.exponentialRampToValueAtTime(0.0001, t + duration);
  osc.connect(env);
  env.connect(dest);
  osc.start(t);
  osc.stop(t + duration + 0.05);
}

const SAMPLE_PADS = [
  {
    name: "KICK", key: "t",
    play: (d, w) => { toneVoice(d, { from: 150, to: 45, duration: 0.45, gain: 1.0, glideTime: 0.12, when: w }); noiseVoice(d, { duration: 0.05, type: "lowpass", freq: 300, gain: 0.35, decay: 0.03, when: w }); },
  },
  {
    name: "SNARE", key: "y",
    play: (d, w) => { noiseVoice(d, { duration: 0.22, type: "highpass", freq: 1400, gain: 0.8, decay: 0.18, when: w }); toneVoice(d, { wave: "triangle", from: 220, to: 150, duration: 0.14, gain: 0.5, when: w }); },
  },
  {
    name: "CLAP", key: "u",
    play: (d, w) => {
      [0, 0.014, 0.03].forEach((offset) => {
        noiseVoice(d, { duration: 0.18, type: "bandpass", freq: 1500, q: 1.1, gain: 1.4, decay: 0.13, when: Math.max(audioCtx.currentTime, w || 0) + offset });
      });
    },
  },
  {
    name: "HAT", key: "g",
    play: (d, w) => noiseVoice(d, { duration: 0.07, type: "highpass", freq: 8000, gain: 0.5, decay: 0.045, when: w }),
  },
  {
    name: "OPEN HAT", key: "h",
    play: (d, w) => noiseVoice(d, { duration: 0.42, type: "highpass", freq: 7000, gain: 0.45, decay: 0.35, when: w }),
  },
  {
    name: "TOM", key: "v",
    play: (d, w) => toneVoice(d, { wave: "sine", from: 260, to: 90, duration: 0.42, gain: 0.85, glideTime: 0.3, when: w }),
  },
  {
    name: "ZAP", key: "b",
    play: (d, w) => toneVoice(d, { wave: "sawtooth", from: 900, to: 70, duration: 0.3, gain: 0.5, when: w }),
  },
  {
    name: "SWEEP", key: "n",
    play: (d, w) => {
      const src = audioCtx.createBufferSource();
      src.buffer = getNoiseBuffer();
      const filter = audioCtx.createBiquadFilter();
      filter.type = "bandpass";
      filter.Q.value = 3;
      const t = Math.max(audioCtx.currentTime, w || 0);
      filter.frequency.setValueAtTime(300, t);
      filter.frequency.exponentialRampToValueAtTime(7000, t + 0.85);
      const env = audioCtx.createGain();
      env.gain.setValueAtTime(0.0001, t);
      env.gain.exponentialRampToValueAtTime(0.9, t + 0.4);
      env.gain.exponentialRampToValueAtTime(0.0001, t + 0.9);
      src.connect(filter);
      filter.connect(env);
      env.connect(d);
      src.start(t);
      src.stop(t + 0.95);
    },
  },
];

const padGains = SAMPLE_PADS.map(() => {
  const g = audioCtx.createGain();
  g.gain.value = 0.8;
  g.connect(masterGain);
  return g;
});

// User-loaded one-shots (sampler-deck.js fills these). A slot with a buffer
// plays it instead of the synth voice; `padVoices` lets a retrigger choke the
// previous hit of the same slot, the way a hardware sampler cuts a vocal chop.
const padSamples = SAMPLE_PADS.map(() => null);   // { buffer, name, sampleId } | null
const padVoices = SAMPLE_PADS.map(() => null);

function playPadSample(index, dest, when) {
  const slot = padSamples[index];
  const t = Math.max(audioCtx.currentTime, when || 0);
  const prev = padVoices[index];
  if (prev) { try { prev.stop(t); } catch (_) { /* already ended */ } }
  const src = audioCtx.createBufferSource();
  src.buffer = slot.buffer;
  src.connect(dest);
  src.onended = () => { if (padVoices[index] === src) padVoices[index] = null; src.disconnect(); };
  src.start(t);
  padVoices[index] = src;
}

// `when` (audioCtx time) lets the beat layer schedule sample-accurate hits;
// `dest` routes into the beat layer's own bus instead of the pad fader.
// `opts.synth` forces the built-in voice even when a sample is loaded.
// Hand-played hits (no `when`) snap to the sampler's QUANTIZE grid if set.
function triggerPad(index, when, dest, opts = {}) {
  if (audioCtx.state === "suspended") audioCtx.resume();
  const pad = SAMPLE_PADS[index];
  if (!pad) return;
  if (when == null && typeof window.samplerQuantize === "function") when = window.samplerQuantize();
  const out = dest || padGains[index];
  if (padSamples[index] && !opts.synth) playPadSample(index, out, when);
  else pad.play(out, when);
  // Light the pad when the hit actually sounds, not when it was booked.
  const delay = Math.max(0, ((when || 0) - audioCtx.currentTime) * 1000);
  setTimeout(() => {
    document.querySelectorAll(`[data-pad="${index}"]`).forEach((el) => {
      if (el.tagName !== "BUTTON") return;
      el.classList.add("hit", "pad-hit");
      setTimeout(() => el.classList.remove("hit", "pad-hit"), 130);
    });
  }, delay);
}

// Pads fire on pointerdown: a click fires on release, which is a finger
// drummer's worth of latency. Keyboard activation (Enter/Space on a focused
// pad) still arrives as a click with detail 0.
function bindPadPress(btn, fire) {
  btn.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    fire();
  });
  btn.addEventListener("click", (e) => { if (e.detail === 0) fire(); });
}

const padGrid = document.getElementById("pad-grid");
if (padGrid) {
  SAMPLE_PADS.forEach((pad, i) => {
    const cell = document.createElement("div");
    cell.className = "pad-cell";
    cell.innerHTML = `
      <button class="sample-pad" data-pad="${i}">
        <span class="pad-name" data-pad-label="${i}">${pad.name}</span>
        <span class="pad-key">KEY <span data-pad-keycap="${i}">${pad.key.toUpperCase()}</span></span>
      </button>
      <div class="pad-vol-row">
        <span class="hud-label">VOL</span>
        <div class="knob-wrap pot">
          <div class="knob-pointer"></div>
          <input type="range" class="knob-input pad-vol" data-pad="${i}" min="0" max="1.5" value="0.8" step="0.01" aria-label="${pad.name} volume" />
        </div>
      </div>
    `;
    padGrid.appendChild(cell);
    bindPadPress(cell.querySelector(".sample-pad"), () => triggerPad(i));
    const vol = cell.querySelector(".pad-vol");
    vol.addEventListener("input", () => { padGains[i].gain.value = parseFloat(vol.value); });
    initKnob(cell.querySelector(".knob-wrap"));
  });
}

// ============================ Mix recording =================================
//
// Tap the shared master stage with a MediaStreamDestination and hand the bytes
// to MediaRecorder. On stop the blob becomes a local object URL on a plain
// <a download> — no backend round-trip needed.

const recDest = audioCtx.createMediaStreamDestination();
masterOut.connect(recDest);

const recBtn = document.getElementById("rec-btn");
const recStatus = document.getElementById("rec-status");
const recDownload = document.getElementById("rec-download");
const recLogDownload = document.getElementById("rec-log-download");
// Header REC indicator — driven only by the real MediaRecorder state below.
const recHud = document.getElementById("rec-hud");
const recClock = document.getElementById("rec-clock");
let mediaRecorder = null;
let recChunks = [];
let recStartedAt = 0;
let recStartedAudioTime = 0;
let recTimer = null;
let questEvents = [];

function questEvent(param, val = "") {
  if (!mediaRecorder || mediaRecorder.state !== "recording") return;
  questEvents.push({ t: Number((audioCtx.currentTime).toFixed(3)), param, val });
}

// Capture semantic automation events plus real user control changes. This is
// intentionally append-only so a set can be replayed or audited afterwards.
if (window.djEvents) {
  window.djEvents.addEventListener("automation-start", (e) => questEvent("automation-start", e.detail.recipe || ""));
  window.djEvents.addEventListener("automation-finish", (e) => questEvent("automation-finish", `${e.detail.recipe || ""}:${e.detail.outcome || ""}`));
  window.djEvents.addEventListener("transition-pin", (e) => questEvent(`transition-pin:${e.detail.deck || ""}`, e.detail.value));
}
document.addEventListener("input", (event) => {
  const el = event.target;
  if (!(el instanceof HTMLInputElement) || !el.dataset.deck && el.id !== "crossfader" && el.id !== "master-fader") return;
  questEvent(el.id || `${el.dataset.deck}:${el.dataset.band || el.className}`, Number(el.value));
}, true);

function pickMimeType() {
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
  return candidates.find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
}

function startRecording() {
  if (audioCtx.state === "suspended") audioCtx.resume();
  const mimeType = pickMimeType();
  recChunks = [];
  questEvents = [];
  mediaRecorder = new MediaRecorder(recDest.stream, mimeType ? { mimeType } : undefined);
  mediaRecorder.ondataavailable = (e) => { if (e.data && e.data.size) recChunks.push(e.data); };
  mediaRecorder.onstop = () => {
    const blob = new Blob(recChunks, { type: mediaRecorder.mimeType || "audio/webm" });
    const url = URL.createObjectURL(blob);
    recDownload.href = url;
    recDownload.download = `dj-mix-${new Date().toISOString().replace(/[:.]/g, "-")}.webm`;
    recDownload.hidden = false;
    const log = {
      $schema: "https://pulse.dj/schemas/djset-v1.json",
      metadata: {
        created_at: new Date().toISOString(),
        duration_seconds: Number((audioCtx.currentTime - (recStartedAudioTime || audioCtx.currentTime)).toFixed(3)),
        track_a_id: state.trackA,
        track_b_id: state.trackB,
      },
      control_event_stream: questEvents,
    };
    const logUrl = URL.createObjectURL(new Blob([JSON.stringify(log, null, 2)], { type: "application/json" }));
    if (recLogDownload) {
      recLogDownload.href = logUrl;
      recLogDownload.download = `dj-set-${new Date().toISOString().replace(/[:.]/g, "-")}.djset.json`;
      recLogDownload.hidden = false;
    }
    // Saving locally remains the primary workflow. Archive validation/export is
    // best-effort so a temporary backend outage never loses the download.
    fetch("/api/set-logs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(log),
    }).then((res) => {
      if (!res.ok) throw new Error(res.statusText);
      return res.json();
    }).then(() => {
      recStatus.textContent = `Saved ${(blob.size / 1024).toFixed(0)} KB + set log`;
    }).catch(() => {
      recStatus.textContent = `Saved ${(blob.size / 1024).toFixed(0)} KB (set log downloaded locally)`;
    });
    recStatus.textContent = `Saved ${(blob.size / 1024).toFixed(0)} KB`;
  };
  mediaRecorder.start(250);
  questEvent("recording-start", `${state.trackA || ""}:${state.trackB || ""}`);
  recStartedAt = Date.now();
  recStartedAudioTime = audioCtx.currentTime;
  recBtn.classList.add("recording");
  recBtn.textContent = "■ STOP REC";
  if (recHud) recHud.classList.add("is-recording");
  recTimer = setInterval(() => {
    const s = Math.floor((Date.now() - recStartedAt) / 1000);
    const clock = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    recStatus.textContent = `● Recording ${clock}`;
    if (recClock) recClock.textContent = clock.padStart(5, "0");
  }, 250);
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== "inactive") mediaRecorder.stop();
  clearInterval(recTimer);
  recBtn.classList.remove("recording");
  recBtn.textContent = "● REC MIX";
  if (recHud) recHud.classList.remove("is-recording");
}

function toggleRecording() {
  if (mediaRecorder && mediaRecorder.state === "recording") stopRecording();
  else startRecording();
}

if (recBtn) {
  if (!window.MediaRecorder) {
    recBtn.disabled = true;
    recStatus.textContent = "MediaRecorder unsupported in this browser";
  } else {
    recBtn.addEventListener("click", toggleRecording);
  }
}

// ========================= Keyboard shortcuts ===============================
//
// Two ways to drive two decks from one keyboard:
//  1. Mirrored halves (legacy) — left hand keys always mean deck A, right
//     hand keys always mean deck B (q=play A, p=play B, etc).
//  2. Combo keys (new) — the SAME key drives whichever deck isn't held down
//     by Shift: Space/Enter/\ act on deck A normally, deck B while Shift is
//     held. Lets you play, cue and toggle FX on either deck one-handed
//     without relearning a second key per action.
// Both schemes are live at once, and every binding here is just a *default*:
// click any row in the legend (toggle it with "?" or backtick) to rebind it
// to any key you want. Overrides persist in this browser (localStorage) and
// "RESET ALL" restores the defaults below.

const ACTIONS = [
  // --- Deck A (left hand) ---
  { id: "play-a", group: "Deck A", label: "Play / Pause A", defaultKey: "q", run: () => decks.a.toggle() },
  { id: "cue-a", group: "Deck A", label: "Cue A", defaultKey: "a", run: () => decks.a.cue() },
  { id: "loop-a", group: "Deck A", label: "Loop A on/off", defaultKey: "z", run: () => uiToggleLoop("a") },
  { id: "jump-back-a", group: "Deck A", label: "Beatjump A −4 (Shift = −16)", defaultKey: "s", run: (e) => decks.a.jumpBeats(e.shiftKey ? -16 : -4) },
  { id: "jump-fwd-a", group: "Deck A", label: "Beatjump A +4 (Shift = +16)", defaultKey: "d", run: (e) => decks.a.jumpBeats(e.shiftKey ? 16 : 4) },
  { id: "reverse-a", group: "Deck A", label: "Reverse A", defaultKey: "x", run: () => uiToggleReverse("a") },
  { id: "brake-a", group: "Deck A", label: "Brake A", defaultKey: "c", run: () => decks.a.brake() },
  { id: "fx-a", group: "Deck A", label: "FX A on/off", defaultKey: "f", run: () => toggleFx("a") },
  { id: "hotcue-a1", group: "Deck A", label: "Hot cue A1", defaultKey: "1", run: () => uiHotCue("a", 1) },
  { id: "hotcue-a2", group: "Deck A", label: "Hot cue A2", defaultKey: "2", run: () => uiHotCue("a", 2) },
  { id: "hotcue-a3", group: "Deck A", label: "Hot cue A3", defaultKey: "3", run: () => uiHotCue("a", 3) },
  { id: "hotcue-a4", group: "Deck A", label: "Hot cue A4", defaultKey: "4", run: () => uiHotCue("a", 4) },
  { id: "bend-down-a", group: "Deck A", label: "Pitch bend A −2% (hold)", defaultKey: "w", hold: ["a", -2] },
  { id: "bend-up-a", group: "Deck A", label: "Pitch bend A +2% (hold)", defaultKey: "e", hold: ["a", 2] },

  // --- Deck B (right hand) ---
  { id: "play-b", group: "Deck B", label: "Play / Pause B", defaultKey: "p", run: () => decks.b.toggle() },
  { id: "cue-b", group: "Deck B", label: "Cue B", defaultKey: ";", run: () => decks.b.cue() },
  { id: "loop-b", group: "Deck B", label: "Loop B on/off", defaultKey: "/", run: () => uiToggleLoop("b") },
  { id: "jump-back-b", group: "Deck B", label: "Beatjump B −4 (Shift = −16)", defaultKey: "k", run: (e) => decks.b.jumpBeats(e.shiftKey ? -16 : -4) },
  { id: "jump-fwd-b", group: "Deck B", label: "Beatjump B +4 (Shift = +16)", defaultKey: "l", run: (e) => decks.b.jumpBeats(e.shiftKey ? 16 : 4) },
  { id: "reverse-b", group: "Deck B", label: "Reverse B", defaultKey: ".", run: () => uiToggleReverse("b") },
  { id: "brake-b", group: "Deck B", label: "Brake B", defaultKey: ",", run: () => decks.b.brake() },
  { id: "fx-b", group: "Deck B", label: "FX B on/off", defaultKey: "j", run: () => toggleFx("b") },
  { id: "hotcue-b1", group: "Deck B", label: "Hot cue B1", defaultKey: "6", run: () => uiHotCue("b", 1) },
  { id: "hotcue-b2", group: "Deck B", label: "Hot cue B2", defaultKey: "7", run: () => uiHotCue("b", 2) },
  { id: "hotcue-b3", group: "Deck B", label: "Hot cue B3", defaultKey: "8", run: () => uiHotCue("b", 3) },
  { id: "hotcue-b4", group: "Deck B", label: "Hot cue B4", defaultKey: "9", run: () => uiHotCue("b", 4) },
  { id: "bend-down-b", group: "Deck B", label: "Pitch bend B −2% (hold)", defaultKey: "i", hold: ["b", -2] },
  { id: "bend-up-b", group: "Deck B", label: "Pitch bend B +2% (hold)", defaultKey: "o", hold: ["b", 2] },

  // --- Combo keys: one key, Shift picks the deck ---
  { id: "combo-play", group: "Combo (Shift = Deck B)", label: "Play / Pause", defaultKey: " ", run: (e) => decks[e.shiftKey ? "b" : "a"].toggle() },
  { id: "combo-cue", group: "Combo (Shift = Deck B)", label: "Cue", defaultKey: "enter", run: (e) => decks[e.shiftKey ? "b" : "a"].cue() },
  { id: "combo-fx", group: "Combo (Shift = Deck B)", label: "FX on/off (live effect)", defaultKey: "\\", run: (e) => toggleFx(e.shiftKey ? "b" : "a") },
  { id: "combo-loop", group: "Combo (Shift = Deck B)", label: "Loop on/off", defaultKey: "'", run: (e) => uiToggleLoop(e.shiftKey ? "b" : "a") },

  // --- Global ---
  { id: "record", group: "Global", label: "Record mix on/off", defaultKey: "r", run: () => toggleRecording() },
  { id: "legend", group: "Global", label: "Show / hide this legend", defaultKey: "`", run: () => toggleLegend() },
];

SAMPLE_PADS.forEach((pad, i) => {
  ACTIONS.push({ id: `pad-${i}`, group: "Sampler", label: `Sampler: ${pad.name}`, defaultKey: pad.key, run: () => triggerPad(i) });
});

// --- Editable bindings: overrides persist per-browser in localStorage ------

const OVERRIDES_KEY = "dj-console-key-overrides";

function loadOverrides() {
  try {
    return JSON.parse(localStorage.getItem(OVERRIDES_KEY) || "{}");
  } catch {
    return {};
  }
}

let keyOverrides = loadOverrides();

function saveOverrides() {
  try {
    localStorage.setItem(OVERRIDES_KEY, JSON.stringify(keyOverrides));
  } catch {
    // localStorage unavailable (private mode, quota) — bindings just won't persist.
  }
}

function currentKeyFor(action) {
  return (keyOverrides[action.id] ?? action.defaultKey).toLowerCase();
}

let KEY_MAP = {};
function rebuildKeyMap() {
  KEY_MAP = {};
  ACTIONS.forEach((action) => { KEY_MAP[currentKeyFor(action)] = action; });
}
rebuildKeyMap();

// Route keyboard actions through the same UI updates the buttons use, so the
// on-screen state (loop lamp, hot-cue lamp, REV lamp) never goes stale.
function uiToggleLoop(deckId) {
  const btn = document.querySelector(`.deck-btn[data-deck="${deckId}"][data-action="loop-toggle"]`);
  if (btn) btn.click();
}

function uiToggleReverse(deckId) {
  const btn = document.querySelector(`.deck-btn[data-deck="${deckId}"][data-action="reverse"]`);
  if (btn) btn.click();
}

function uiHotCue(deckId, n) {
  const btn = document.querySelector(`.hot-cue-btn[data-deck="${deckId}"][data-cue="${n}"]`);
  if (btn) btn.click();
}

const heldBendKeys = new Set();

const TYPING_INPUT_TYPES = new Set(["text", "search", "url", "email", "password", "number", "tel", "date", "time"]);

function isTypingTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable) return true;
  // Sliders, knobs and the file pickers keep focus after you use them; they
  // aren't text fields, so they must not swallow the performance shortcuts.
  if (tag === "INPUT") return TYPING_INPUT_TYPES.has((el.type || "text").toLowerCase());
  return false;
}

window.addEventListener("keydown", (e) => {
  if (rebindTarget) { captureRebind(e); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (isTypingTarget(e.target)) return;
  const entry = KEY_MAP[e.key.toLowerCase()] || KEY_MAP[e.key];
  if (!entry) return;
  e.preventDefault();
  if (entry.hold) {
    if (heldBendKeys.has(e.key.toLowerCase())) return; // ignore auto-repeat
    heldBendKeys.add(e.key.toLowerCase());
    startBend(entry.hold[0], entry.hold[1]);
    return;
  }
  if (e.repeat) return;
  entry.run(e);
});

window.addEventListener("keyup", (e) => {
  const key = e.key.toLowerCase();
  const entry = KEY_MAP[key];
  if (entry && entry.hold && heldBendKeys.has(key)) {
    heldBendKeys.delete(key);
    endBend(entry.hold[0]);
  }
});

// ======================= Performance pad modes ==============================
//
// The 4x2 pad grid on each deck is a view onto controls that already exist:
//   HOT CUE  -> the 4 real hot cues + a CLR pad each (static markup)
//   ROLL     -> the real deck loop, armed at a fixed beat length per pad
//   SLICER   -> not implemented (no slicing DSP) — the tab is disabled, not faked
//   SAMPLER  -> the same 8 synthesized one-shots as the sampler bank below
// No pad invents functionality the audio engine does not have.

const ROLL_LENGTHS = [0.25, 0.5, 1, 2, 4, 8, 16, 32];

function buildRollBank(bank) {
  const deckId = bank.dataset.deck;
  const deck = decks[deckId];
  bank.innerHTML = "";
  ROLL_LENGTHS.forEach((beats) => {
    const btn = document.createElement("button");
    btn.className = "pad roll-pad";
    btn.dataset.beats = String(beats);
    btn.innerHTML = `<span class="pad-num">${beats < 1 ? `1/${1 / beats}` : beats}</span><span class="pad-sub">beat${beats === 1 ? "" : "s"}</span>`;
    btn.addEventListener("click", () => {
        const alreadyOn = deck.loopOn && deck.loopBeats === beats;
      deck.setLoopBeats(beats);
      if (alreadyOn) {
        if (deck.loopOn) deck.toggleLoop();
      } else if (!deck.loopOn) {
        deck.toggleLoop();
      } else {
        // Loop already running at a different length: re-arm it from here.
        if (deck.playing) deck.play(deck._currentPosition());
      }
      syncLoopUI(deckId);
    });
    bank.appendChild(btn);
  });
}

// Keeps the loop stepper, the loop ON/OFF lamp and the ROLL pads showing the
// same real deck state no matter which control changed it.
function syncLoopUI(deckId) {
  const deck = decks[deckId];
  const valueEl = document.getElementById(`loop-value-${deckId}`);
  if (valueEl) valueEl.textContent = deck.loopBeats < 1 ? `1/${1 / deck.loopBeats}` : deck.loopBeats;
  const toggle = document.querySelector(`.hw-btn[data-deck="${deckId}"][data-action="loop-toggle"]`);
  if (toggle) {
    toggle.classList.toggle("loop-active", deck.loopOn);
    toggle.textContent = deck.loopOn ? "ON" : "OFF";
  }
  document.querySelectorAll(`.pad-bank[data-deck="${deckId}"][data-mode="roll"] .roll-pad`).forEach((p) => {
    p.classList.toggle("pad-lit", deck.loopOn && parseFloat(p.dataset.beats) === deck.loopBeats);
  });
}

function buildSamplerBank(bank) {
  bank.innerHTML = "";
  SAMPLE_PADS.forEach((pad, i) => {
    const btn = document.createElement("button");
    btn.className = "pad";
    btn.dataset.pad = String(i);
    btn.innerHTML = `<span class="pad-num" data-pad-label="${i}">${pad.name}</span><span class="pad-sub" data-pad-keycap="${i}">${pad.key.toUpperCase()}</span>`;
    bindPadPress(btn, () => triggerPad(i));
    bank.appendChild(btn);
  });
}

document.querySelectorAll('.pad-bank[data-mode="roll"]').forEach(buildRollBank);
document.querySelectorAll('.pad-bank[data-mode="sampler"]').forEach(buildSamplerBank);
["a", "b"].forEach(syncLoopUI);

// The loop stepper and the ON/OFF button live in deck-controller.js; re-sync
// the pad lamps after either of them is used.
document.querySelectorAll(".stepper-btn, [data-action='loop-toggle']").forEach((btn) => {
  btn.addEventListener("click", () => syncLoopUI(btn.dataset.deck));
});

document.querySelectorAll(".pad-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    if (tab.disabled) return;
    const deckId = tab.dataset.deck;
    document.querySelectorAll(`.pad-tab[data-deck="${deckId}"]`).forEach((t) => t.classList.toggle("is-active", t === tab));
    document.querySelectorAll(`.pad-bank[data-deck="${deckId}"]`).forEach((bank) => {
      bank.hidden = bank.dataset.mode !== tab.dataset.mode;
    });
  });
});

// ---------------------------- Help overlay ---------------------------------
//
// The legend doubles as the rebind UI: click any row's key chip to arm it,
// then press the new key. Escape cancels. A collision (the new key is
// already bound elsewhere) is allowed but flagged in the status line, since
// last-bound-wins in rebuildKeyMap().

const legendEl = document.getElementById("shortcut-legend");
const helpToggle = document.getElementById("help-toggle");
const helpOverlay = document.getElementById("shortcut-overlay");
const helpClose = document.getElementById("help-close");

const GROUP_ORDER = ["Deck A", "Deck B", "Combo (Shift = Deck B)", "Sampler", "Global"];

function keyDisplay(key) {
  if (key === " ") return "Space";
  if (key === "enter") return "Enter";
  return key.toUpperCase();
}

function renderLegend() {
  if (!legendEl) return;
  legendEl.innerHTML = GROUP_ORDER.map((group) => {
    const actions = ACTIONS.filter((a) => a.group === group);
    if (!actions.length) return "";
    return `
      <div class="legend-group">
        <h4>${group}</h4>
        ${actions.map((action) => `
          <div class="legend-row${keyOverrides[action.id] ? " is-rebound" : ""}">
            <button type="button" class="kbd rebind-btn" data-action-id="${action.id}" title="Click, then press a new key to rebind">${keyDisplay(currentKeyFor(action))}</button>
            <span>${action.label}</span>
          </div>
        `).join("")}
        ${group === "Deck A" || group === "Deck B" ? '<div class="legend-row legend-note">Hold Shift with the beatjump keys for ±16 beats.</div>' : ""}
      </div>
    `;
  }).join("") + `
    <div class="legend-group legend-actions">
      <button type="button" class="hw-btn" id="help-reset">RESET ALL TO DEFAULTS</button>
      <p class="legend-note">Click any key chip above, then press the new key. Bindings save in this browser.</p>
    </div>
  `;
  document.querySelectorAll(".rebind-btn").forEach((btn) => {
    btn.addEventListener("click", () => armRebind(btn.dataset.actionId, btn));
  });
  const resetBtn = document.getElementById("help-reset");
  if (resetBtn) resetBtn.addEventListener("click", () => {
    keyOverrides = {};
    saveOverrides();
    rebuildKeyMap();
    renderLegend();
    setStatusIfAvailable("Shortcuts reset to defaults.");
  });
}

// `status` (from app.js) may not exist on every page that loads this script;
// fail silently rather than throw.
function setStatusIfAvailable(text) {
  const el = document.getElementById("status");
  if (el) el.textContent = text;
}

let rebindTarget = null; // { actionId, btn } while armed and waiting for a keypress

function armRebind(actionId, btn) {
  if (rebindTarget) rebindTarget.btn.classList.remove("is-armed");
  rebindTarget = { actionId, btn };
  btn.classList.add("is-armed");
  btn.textContent = "…";
}

function captureRebind(e) {
  e.preventDefault();
  const { actionId, btn } = rebindTarget;
  btn.classList.remove("is-armed");
  rebindTarget = null;
  if (e.key === "Escape") { renderLegend(); return; }

  const newKey = e.key.toLowerCase();
  const action = ACTIONS.find((a) => a.id === actionId);
  const collision = ACTIONS.find((a) => a.id !== actionId && currentKeyFor(a) === newKey);

  keyOverrides[actionId] = newKey;
  saveOverrides();
  rebuildKeyMap();
  renderLegend();
  setStatusIfAvailable(
    collision
      ? `"${action.label}" is now ${keyDisplay(newKey)} — note it also fires "${collision.label}".`
      : `"${action.label}" is now ${keyDisplay(newKey)}.`
  );
}

function setLegendOpen(open) {
  if (!helpOverlay) return;
  helpOverlay.hidden = !open;
  if (helpToggle) helpToggle.setAttribute("aria-expanded", String(open));
  if (!open && rebindTarget) { rebindTarget.btn.classList.remove("is-armed"); rebindTarget = null; renderLegend(); }
}

function toggleLegend() {
  if (!helpOverlay) return;
  setLegendOpen(helpOverlay.hidden);
}

renderLegend();
if (helpToggle) helpToggle.addEventListener("click", toggleLegend);
if (helpClose) helpClose.addEventListener("click", () => setLegendOpen(false));
if (helpOverlay) {
  helpOverlay.addEventListener("click", (e) => { if (e.target === helpOverlay) setLegendOpen(false); });
}
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && helpOverlay && !helpOverlay.hidden && !rebindTarget) setLegendOpen(false);
});
