// AI Music Brain - sampler deck: custom one-shots per pad, hand-play
// quantize, and a 16-step BEAT GRID locked to a playing deck's beatgrid.
//
// Depends on globals from deck-controller.js (audioCtx, masterGain, decks),
// performance.js (SAMPLE_PADS, padGains, padSamples, triggerPad, ACTIONS,
// currentKeyFor, keyDisplay, rebuildKeyMap, renderLegend) and beat-layer.js
// (window.beatLayer.grid).
//
// Grounding (./DJ/):
//  - [[Phrasing & Structure]]: steps ride the analysed beatgrid with bar 1 on
//    the record's downbeat, so a programmed pattern lands on the phrase.
//  - [[EQ & Frequency Management]]: SUB SAFE high-passes the grid at 120 Hz
//    so the record keeps sole ownership of the sub-bass.
//  - [[Stems, Live Remixing & Ableton Integration]]: a retriggered chop cuts the previous hit of
//    the same pad (choke), like a hardware sampler.

(function () {
  if (typeof audioCtx === "undefined" || typeof triggerPad !== "function" ||
      typeof padSamples === "undefined") return;

  const SLOTS_KEY = "dj-sampler-slots-v1";
  const SEQ_KEY = "dj-seq-v1";
  const MAX_SAMPLE_BYTES = 25 * 1024 * 1024;   // matches server SAMPLE_MAX_BYTES
  const AUDIO_EXT = /\.(mp3|wav|flac|m4a|ogg|aiff?|opus|webm)$/i;
  const STEPS = 16;
  const LOOKAHEAD_S = 0.12;
  const TICK_MS = 25;
  const LATE_HIT_S = 0.04;  // a hit this close after a grid point counts as on it

  const statusEl = document.getElementById("sampler-status");
  const chipEl = document.getElementById("sampler-chip");
  const quantEl = document.getElementById("sampler-quant");
  function samplerStatus(msg) { if (statusEl) statusEl.textContent = msg; }

  function readJSON(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key) || "null") || fallback; } catch (_) { return fallback; }
  }
  function writeJSON(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* private mode / quota */ }
  }

  // ======================= Clock deck + beatgrid ==========================

  function deckLoudness(d) {
    const xf = d.crossfaderGain ? d.crossfaderGain.gain.value : 1;
    const vol = d.volumeGain ? d.volumeGain.gain.value : 1;
    return xf * vol;
  }

  function isRunning(d) { return !!d && d.playing && !d.reversed && !d._braking; }

  // AUTO: the autopilot's followed deck if it is playing, else the loudest
  // playing deck through the crossfader.
  function clockDeck(pref) {
    const all = window.decks || {};
    if (pref === "a" || pref === "b") return all[pref] || null;
    const bl = window.beatLayer && window.beatLayer.deck;
    if (bl && isRunning(all[bl])) return all[bl];
    const playing = ["a", "b"].map((k) => all[k]).filter(isRunning);
    if (!playing.length) return null;
    return playing.sort((x, y) => deckLoudness(y) - deckLoudness(x))[0];
  }

  // Analysed beatgrid, else a flat grid from the deck BPM anchored at 0 s.
  function gridFor(d) {
    const g = window.beatLayer && window.beatLayer.grid ? window.beatLayer.grid(d) : null;
    if (g) return g;
    const dur = d.buffer ? d.buffer.duration : 0;
    const bpm = d.bpm || 128;
    if (!(dur > 0)) return null;
    if (!d._seqFlat || d._seqFlat.bpm !== bpm || d._seqFlat.dur !== dur) {
      const beatLen = 60 / bpm;
      const beats = [];
      for (let t = 0; t < dur + beatLen; t += beatLen) beats.push(t);
      d._seqFlat = { bpm, dur, g: { beats, beatInBar: beats.map((_, i) => i % 4), flat: true } };
    }
    return d._seqFlat.g;
  }

  function firstBeatAtOrBefore(beats, t) {
    let lo = 0, hi = beats.length - 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (beats[m] <= t) lo = m; else hi = m - 1; }
    return lo;
  }

  // ============================ Quantize ==================================

  // Returns the audioCtx time of the next grid point, or null to play now
  // (quantize off, nothing playing, or the hit is already on the grid).
  window.samplerQuantize = function () {
    const div = quantEl ? parseFloat(quantEl.value) : 0;
    if (!(div > 0)) return null;
    const d = clockDeck(seq.clock);
    if (!isRunning(d)) return null;
    const g = gridFor(d);
    const rate = d._playbackRate();
    if (!g || !(rate > 0.2)) return null;
    const pos = d._currentPosition();
    const { beats, beatInBar } = g;
    const i0 = firstBeatAtOrBefore(beats, pos);
    for (let i = Math.max(0, i0 - 1); i < Math.min(beats.length - 1, i0 + 9); i++) {
      const b0 = beats[i], beatLen = beats[i + 1] - b0;
      if (beatLen <= 0 || beatLen > 2) continue;
      const points = div >= 4 ? (beatInBar[i] === 0 ? [b0] : [])
        : div >= 1 ? [b0]
        : Array.from({ length: Math.round(1 / div) }, (_, k) => b0 + k * div * beatLen);
      for (const t of points) {
        if (t < pos - LATE_HIT_S * rate) continue;
        if (t <= pos) return null;  // just late: already on the grid
        return audioCtx.currentTime + (t - pos) / rate;
      }
    }
    return null;
  };

  // ======================== Custom sample slots ===========================

  const slotMeta = readJSON(SLOTS_KEY, {});   // { "<pad index>": { sampleId, name } }

  function saveSlots() { writeJSON(SLOTS_KEY, slotMeta); }

  function shortName(name) { return name.length > 10 ? name.slice(0, 9) + "…" : name; }

  function paintSlot(i) {
    const slot = padSamples[i];
    const label = slot ? shortName(slot.name) : SAMPLE_PADS[i].name;
    document.querySelectorAll(`[data-pad-label="${i}"]`).forEach((el) => {
      el.textContent = label;
      el.title = slot ? slot.name : "";
    });
    const cell = document.querySelectorAll("#pad-grid .pad-cell")[i];
    if (cell) {
      cell.classList.toggle("has-sample", !!slot);
      const unload = cell.querySelector(".pad-unload");
      if (unload) unload.hidden = !slot;
    }
    const custom = padSamples.filter(Boolean).length;
    if (chipEl) chipEl.textContent = custom ? `${custom} CUSTOM · ${SAMPLE_PADS.length - custom} SYNTH` : "SYNTH KIT";
  }

  async function decode(arrayBuffer) {
    // decodeAudioData detaches its input; hand it a copy.
    return audioCtx.decodeAudioData(arrayBuffer.slice(0));
  }

  async function loadFile(i, file) {
    if (!file) return;
    if (!(file.type.startsWith("audio/") || AUDIO_EXT.test(file.name))) {
      samplerStatus(`${file.name}: not an audio file`);
      return;
    }
    if (file.size > MAX_SAMPLE_BYTES) {
      samplerStatus(`${file.name}: too large (max 25 MB). Trim it to a one-shot or a chop first.`);
      return;
    }
    const name = file.name.replace(/\.[^.]+$/, "");
    samplerStatus(`Loading ${name} into pad ${i + 1}…`);
    let buffer;
    try {
      buffer = await decode(await file.arrayBuffer());
    } catch (_) {
      samplerStatus(`${file.name}: the browser could not decode this audio`);
      return;
    }
    padSamples[i] = { buffer, name, sampleId: null };
    paintSlot(i);
    // Persist server-side so the kit survives a reload; the pad already works
    // locally if this fails.
    try {
      const form = new FormData();
      form.append("file", file, file.name);
      form.append("label", name);
      const res = await fetch("/api/samples", { method: "POST", body: form });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || res.statusText);
      if (padSamples[i] && padSamples[i].buffer === buffer) {
        padSamples[i].sampleId = body.sample_id;
        slotMeta[i] = { sampleId: body.sample_id, name };
        saveSlots();
      }
      samplerStatus(`Pad ${i + 1} = ${name} (${buffer.duration.toFixed(2)} s)`);
    } catch (err) {
      samplerStatus(`Pad ${i + 1} = ${name}, but not saved for next time (${err.message})`);
    }
  }

  function unloadSlot(i) {
    const voice = padVoices[i];
    if (voice) { try { voice.stop(); } catch (_) { /* ended */ } }
    padSamples[i] = null;
    delete slotMeta[i];
    saveSlots();
    paintSlot(i);
    samplerStatus(`Pad ${i + 1} back to the synth ${SAMPLE_PADS[i].name}`);
  }

  async function restoreSlots() {
    const entries = Object.entries(slotMeta);
    if (!entries.length) return;
    let missing = 0;
    await Promise.all(entries.map(async ([k, meta]) => {
      const i = Number(k);
      if (!(i >= 0 && i < SAMPLE_PADS.length) || !meta || typeof meta.sampleId !== "string") {
        delete slotMeta[k];
        return;
      }
      try {
        const res = await fetch(`/api/samples/${encodeURIComponent(meta.sampleId)}`);
        if (!res.ok) throw new Error(res.statusText);
        const buffer = await decode(await res.arrayBuffer());
        padSamples[i] = { buffer, name: String(meta.name || "SAMPLE"), sampleId: meta.sampleId };
        paintSlot(i);
      } catch (_) {
        missing++;
        delete slotMeta[k];
      }
    }));
    saveSlots();
    if (missing) samplerStatus(`${missing} saved pad sample(s) no longer on the server; those pads are back on synth`);
  }

  document.querySelectorAll("#pad-grid .pad-cell").forEach((cell, i) => {
    const row = document.createElement("div");
    row.className = "pad-slot-row";
    row.innerHTML = `
      <button type="button" class="pad-mini pad-load" title="Load an audio file onto this pad (or drop one on it)">LOAD</button>
      <button type="button" class="pad-mini pad-unload" title="Back to the built-in synth voice" hidden>SYNTH</button>
      <input type="file" accept="audio/*" hidden aria-label="Sample file for pad ${i + 1}" />`;
    cell.appendChild(row);
    const input = row.querySelector("input");
    row.querySelector(".pad-load").addEventListener("click", () => input.click());
    row.querySelector(".pad-unload").addEventListener("click", () => unloadSlot(i));
    input.addEventListener("change", () => { loadFile(i, input.files[0]); input.value = ""; });
    cell.addEventListener("dragover", (e) => {
      if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
      e.preventDefault();
      cell.classList.add("drop-target");
    });
    cell.addEventListener("dragleave", () => cell.classList.remove("drop-target"));
    cell.addEventListener("drop", (e) => {
      e.preventDefault();
      cell.classList.remove("drop-target");
      loadFile(i, e.dataTransfer.files[0]);
    });
  });

  // Pad keycaps show the live binding, not the default, after a rebind.
  function refreshKeycaps() {
    if (typeof ACTIONS === "undefined") return;
    SAMPLE_PADS.forEach((_, i) => {
      const action = ACTIONS.find((a) => a.id === `pad-${i}`);
      if (!action) return;
      const label = keyDisplay(currentKeyFor(action));
      document.querySelectorAll(`[data-pad-keycap="${i}"]`).forEach((el) => { el.textContent = label; });
    });
  }

  // ============================ Beat grid =================================

  const PAD = { KICK: 0, SNARE: 1, CLAP: 2, HAT: 3, OPEN_HAT: 4, TOM: 5, ZAP: 6, SWEEP: 7 };
  const steps = (idx) => { const r = Array(STEPS).fill(0); idx.forEach((s) => { r[s] = 1; }); return r; };
  const PRESETS = {
    four:     { [PAD.KICK]: [0, 4, 8, 12], [PAD.CLAP]: [4, 12], [PAD.HAT]: [2, 6, 10, 14] },
    house:    { [PAD.KICK]: [0, 4, 8, 12], [PAD.CLAP]: [4, 12], [PAD.HAT]: [1, 3, 5, 7, 9, 11, 13, 15], [PAD.OPEN_HAT]: [2, 6, 10, 14] },
    breaks:   { [PAD.KICK]: [0, 2, 10], [PAD.SNARE]: [4, 12, 15], [PAD.HAT]: [0, 2, 4, 6, 8, 10, 12, 14] },
    halftime: { [PAD.KICK]: [0, 11], [PAD.SNARE]: [8], [PAD.HAT]: [0, 2, 4, 6, 8, 10, 12, 13, 14, 15] },
    dnb:      { [PAD.KICK]: [0, 10], [PAD.SNARE]: [4, 12], [PAD.HAT]: [2, 6, 10, 14], [PAD.OPEN_HAT]: [14] },
    tops:     { [PAD.HAT]: [1, 3, 5, 7, 9, 11, 13, 15], [PAD.OPEN_HAT]: [2, 6, 10, 14], [PAD.CLAP]: [12] },
  };

  const saved = readJSON(SEQ_KEY, {});
  const validRow = (r) => Array.isArray(r) && r.length === STEPS;
  const seq = {
    steps: SAMPLE_PADS.map((_, p) => (saved.steps && validRow(saved.steps[p]) ? saved.steps[p].map((v) => (v ? 1 : 0)) : Array(STEPS).fill(0))),
    mutes: SAMPLE_PADS.map((_, p) => !!(saved.mutes && saved.mutes[p])),
    swing: Math.min(0.6, Math.max(0, Number(saved.swing) || 0)),
    level: Number.isFinite(saved.level) ? Math.min(1, Math.max(0, saved.level)) : 0.6,
    subSafe: saved.subSafe !== false,
    clock: ["auto", "a", "b"].includes(saved.clock) ? saved.clock : "auto",
    running: false,
  };
  function saveSeq() {
    const { steps: s, mutes, swing, level, subSafe, clock } = seq;
    writeJSON(SEQ_KEY, { steps: s, mutes, swing, level, subSafe, clock });
  }

  // grid bus -> sub-safe high-pass -> master (so REC captures it)
  const seqBus = audioCtx.createGain();
  const subSafe = audioCtx.createBiquadFilter();
  subSafe.type = "highpass";
  subSafe.Q.value = 0.707;
  seqBus.connect(subSafe);
  subSafe.connect(masterGain);
  function applyLevel() { seqBus.gain.setTargetAtTime(seq.level, audioCtx.currentTime, 0.03); }
  function applySubSafe() { subSafe.frequency.setTargetAtTime(seq.subSafe ? 120 : 10, audioCtx.currentTime, 0.02); }
  applyLevel();
  applySubSafe();

  // ---- grid UI ----
  const gridEl = document.getElementById("seq-grid");
  const runBtn = document.getElementById("seq-run");
  const lampEl = document.getElementById("seq-lamp");
  const seqStatusEl = document.getElementById("seq-status");
  const clockEl = document.getElementById("seq-clock");
  const presetEl = document.getElementById("seq-preset");
  const swingEl = document.getElementById("seq-swing");
  const levelEl = document.getElementById("seq-level");
  const subSafeEl = document.getElementById("seq-subsafe");
  const clearEl = document.getElementById("seq-clear");
  let lastSeqStatus = "";
  function seqStatus(msg) {
    if (!seqStatusEl || msg === lastSeqStatus) return;
    lastSeqStatus = msg;
    seqStatusEl.textContent = msg;
  }

  const cellEls = [];   // [pad][step] -> button
  const colEls = Array.from({ length: STEPS }, () => []);
  function buildGrid() {
    if (!gridEl) return;
    gridEl.innerHTML = "";
    SAMPLE_PADS.forEach((pad, p) => {
      const row = document.createElement("div");
      row.className = "seq-row";
      row.setAttribute("role", "row");
      const name = document.createElement("button");
      name.type = "button";
      name.className = "seq-name";
      name.dataset.padLabel = String(p);
      name.textContent = padSamples[p] ? shortName(padSamples[p].name) : pad.name;
      name.title = "Mute / unmute this row";
      name.setAttribute("aria-pressed", String(seq.mutes[p]));
      name.classList.toggle("is-muted", seq.mutes[p]);
      name.addEventListener("click", () => {
        seq.mutes[p] = !seq.mutes[p];
        name.classList.toggle("is-muted", seq.mutes[p]);
        name.setAttribute("aria-pressed", String(seq.mutes[p]));
        row.classList.toggle("is-muted", seq.mutes[p]);
        saveSeq();
      });
      row.appendChild(name);
      row.classList.toggle("is-muted", seq.mutes[p]);
      cellEls[p] = [];
      for (let s = 0; s < STEPS; s++) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "seq-step" + (s % 4 === 0 ? " beat" : "");
        b.setAttribute("role", "gridcell");
        b.setAttribute("aria-label", `${pad.name} step ${s + 1}`);
        b.setAttribute("aria-pressed", String(!!seq.steps[p][s]));
        b.classList.toggle("on", !!seq.steps[p][s]);
        b.addEventListener("click", () => {
          seq.steps[p][s] = seq.steps[p][s] ? 0 : 1;
          paintStep(p, s);
          saveSeq();
        });
        row.appendChild(b);
        cellEls[p][s] = b;
        colEls[s].push(b);
      }
      gridEl.appendChild(row);
    });
  }
  function paintStep(p, s) {
    const b = cellEls[p] && cellEls[p][s];
    if (!b) return;
    b.classList.toggle("on", !!seq.steps[p][s]);
    b.setAttribute("aria-pressed", String(!!seq.steps[p][s]));
  }
  function paintAll() { seq.steps.forEach((r, p) => r.forEach((_, s) => paintStep(p, s))); }

  let litCol = -1;
  function lightColumn(col) {
    if (litCol >= 0) colEls[litCol].forEach((b) => b.classList.remove("is-now"));
    litCol = col;
    if (col >= 0) colEls[col].forEach((b) => b.classList.add("is-now"));
  }

  // ---- scheduler (same lookahead scheme as beat-layer.js) ----
  let timer = null;
  let lastT = null;
  let lastDeck = null;
  const pending = new Set();   // playhead timeouts, cleared on stop

  function tick() {
    const d = clockDeck(seq.clock);
    if (!isRunning(d)) {
      lastT = null;
      lightColumn(-1);
      seqStatus(seq.clock === "auto" ? "Armed: waiting for a playing deck" : `Armed: waiting for deck ${seq.clock.toUpperCase()} to play`);
      return;
    }
    const g = gridFor(d);
    const rate = d._playbackRate();
    if (!g || !(rate > 0.2)) { lastT = null; return; }
    seqStatus(`Running on deck ${d.id.toUpperCase()} · ${(d.bpm * rate).toFixed(1)} BPM${g.flat ? " · no beatgrid yet, flat BPM grid" : ""}`);
    const now = audioCtx.currentTime;
    const pos = d._currentPosition();
    const horizon = pos + LOOKAHEAD_S * rate;
    // First tick, clock-deck change, seek, loop or beatjump: resync, never burst-fire.
    if (lastT === null || d !== lastDeck || pos < lastT - LOOKAHEAD_S * rate - 0.05 || pos - lastT > 1) lastT = pos;
    lastDeck = d;
    const { beats, beatInBar } = g;
    for (let i = firstBeatAtOrBefore(beats, lastT); i < beats.length - 1 && beats[i] <= horizon; i++) {
      const b0 = beats[i], beatLen = beats[i + 1] - b0;
      if (beatLen <= 0 || beatLen > 2) continue;
      for (let s = 0; s < 4; s++) {
        const t = b0 + (beatLen * s) / 4;
        if (t <= lastT || t > horizon) continue;
        const col = beatInBar[i] * 4 + s;
        const swingT = s % 2 ? seq.swing * (beatLen / 4) : 0;   // late off-16ths
        const when = now + (t + swingT - pos) / rate;
        for (let p = 0; p < SAMPLE_PADS.length; p++) {
          if (!seq.steps[p][col] || seq.mutes[p]) continue;
          const v = audioCtx.createGain();
          v.gain.value = padGains[p].gain.value;   // the pad's own VOL knob
          v.connect(seqBus);
          triggerPad(p, when, v);
          const tail = padSamples[p] ? padSamples[p].buffer.duration : 0;
          setTimeout(() => v.disconnect(), (when - now + tail) * 1000 + 1500);
        }
        const id = setTimeout(() => { pending.delete(id); lightColumn(col); }, Math.max(0, (when - now) * 1000));
        pending.add(id);
      }
    }
    lastT = horizon;
  }

  function setRunning(on) {
    seq.running = !!on;
    if (seq.running && audioCtx.state === "suspended") audioCtx.resume();
    if (seq.running && !timer) { lastT = null; timer = setInterval(tick, TICK_MS); tick(); }
    if (!seq.running && timer) {
      clearInterval(timer);
      timer = null;
      pending.forEach(clearTimeout);
      pending.clear();
      lightColumn(-1);
      seqStatus("Stopped. Click steps to program a pattern, then RUN. Click a row name to mute it live.");
    }
    if (runBtn) {
      runBtn.textContent = seq.running ? "■ STOP" : "▶ RUN";
      runBtn.classList.toggle("is-on", seq.running);
      runBtn.setAttribute("aria-pressed", String(seq.running));
    }
    if (lampEl) lampEl.classList.toggle("lamp-ok", seq.running);
    if (typeof questEvent === "function") questEvent("seq-run", seq.running ? 1 : 0);
  }
  function toggleRun() { setRunning(!seq.running); }

  buildGrid();
  if (clockEl) {
    clockEl.value = seq.clock;
    clockEl.addEventListener("change", () => { seq.clock = clockEl.value; lastT = null; saveSeq(); });
  }
  if (presetEl) presetEl.addEventListener("change", () => {
    const preset = PRESETS[presetEl.value];
    if (preset) {
      seq.steps = SAMPLE_PADS.map((_, p) => steps(preset[p] || []));
      paintAll();
      saveSeq();
      seqStatus(`Loaded ${presetEl.options[presetEl.selectedIndex].text}`);
    }
    presetEl.value = "";
  });
  if (swingEl) {
    swingEl.value = String(seq.swing);
    swingEl.addEventListener("input", () => { seq.swing = parseFloat(swingEl.value) || 0; saveSeq(); });
  }
  if (levelEl) {
    levelEl.value = String(seq.level);
    levelEl.addEventListener("input", () => { seq.level = parseFloat(levelEl.value) || 0; applyLevel(); saveSeq(); });
  }
  if (subSafeEl) {
    subSafeEl.checked = seq.subSafe;
    subSafeEl.addEventListener("change", () => { seq.subSafe = subSafeEl.checked; applySubSafe(); saveSeq(); });
  }
  if (clearEl) clearEl.addEventListener("click", () => {
    seq.steps = SAMPLE_PADS.map(() => Array(STEPS).fill(0));
    paintAll();
    saveSeq();
  });
  if (runBtn) runBtn.addEventListener("click", toggleRun);

  window.beatGrid = { run: () => setRunning(true), stop: () => setRunning(false), toggle: toggleRun,
                      isRunning: () => seq.running };

  // ---- keyboard: M runs / stops the grid; pad keycaps follow rebinds ----
  if (typeof ACTIONS !== "undefined" && typeof rebuildKeyMap === "function") {
    ACTIONS.push({ id: "seq-run", group: "Global", label: "Beat grid run / stop", defaultKey: "m", run: toggleRun });
    rebuildKeyMap();
    if (typeof renderLegend === "function") {
      const baseRender = renderLegend;
      // eslint-disable-next-line no-global-assign
      renderLegend = function () { baseRender(); refreshKeycaps(); };
      renderLegend();
    }
  }
  refreshKeycaps();
  restoreSlots();
})();
