// AI Music Brain - remix mode: live, single-track remixing of the playing deck.
//
// One panel works one record, the way a DJ remixes on the fly
// ([[Stems, Live Remixing & Ableton Integration]]):
//   LOOP     quantized auto-loop of 1/2, 1, 2 or 4 bars on the analysed bar grid,
//            aligned to its own length (a 4-bar loop starts on bar 1 of the
//            4-bar group), so phrase alignment survives ([[Loops & Beat Jumps]]:
//            "always use quantized auto-loops"). The running sources loop in
//            place (no restart, no gap); press the lit size again to exit and
//            the record plays on from inside the loop.
//   CHOP     8 slicer pads over the loop (or the current bar). A pad fires on
//            the next slice line and repeats its slice while held. MIX chops
//            replace the record for the slice (a slicer); VOX chops layer the
//            separated vocal over the running track (vocal chops, 1/8 notes).
//   FILTER   one-knob DJ filter: left = low-pass down to 200 Hz, right =
//            high-pass up to 8 kHz; true bypass at centre.
//   STEMS    live mutes (drums / bass / vocals / other), ACAPELLA, INSTRUMENTAL.
//   FX       the deck's real FX rack (echo / reverb / flanger / crush).
//   HITS     the 8 sampler pads, landed on the remixed deck's next beat.
// REMIX OFF puts the record back: loop released, filter centred, stems and FX
// that remix mode touched returned to the full mix.
//
// Signal path per deck (inserted once, unity gain when idle):
//   inputGain -> liveGate -> lowFilter ... volumeGain -> [dry | hp -> lp] -> fxInput
//   chop voices -> chopBus -> lowFilter      (chops take the deck EQ, filter, FX, fader)
//
// Depends on globals: audioCtx, decks (deck-controller.js), fxUnits,
// updateFxTypeButtons, updateFxPad (fx-rack.js), triggerPad, bindPadPress,
// SAMPLE_PADS, padSamples, syncLoopUI (performance.js). Public API: window.remixMode.
(function (root) {
  "use strict";

  const LOOP_SIZES = [0.5, 1, 2, 4];   // bars
  const SLICES = 8;
  const GUARD_S = 0.05;                // loop end closer than this to the playhead -> take the next region
  const LATE_S = 0.03;                 // a hit this late still counts as "on the line"
  const DEAD_ZONE = 0.03;              // filter knob: centre = true bypass

  const mod = (a, n) => ((a % n) + n) % n;

  // Index of the last element of sorted `arr` <= t, or -1.
  function lastAtOrBefore(arr, t) {
    let lo = 0, hi = arr.length - 1, ans = -1;
    while (lo <= hi) {
      const m = (lo + hi) >> 1;
      if (arr[m] <= t) { ans = m; lo = m + 1; } else hi = m - 1;
    }
    return ans;
  }

  // Bar grid from analysed downbeats (extrapolated at the bar length beyond
  // either end), else a flat grid from the BPM anchored at 0 s.
  function barGrid(downbeats, bpm) {
    const db = Array.isArray(downbeats) && downbeats.length >= 2 ? downbeats : [0];
    const barLen = 240 / (bpm > 0 ? bpm : 128);
    const last = db.length - 1;
    const startOf = (k) => (k < 0 ? db[0] + k * barLen : k > last ? db[last] + (k - last) * barLen : db[k]);
    const indexAt = (t) => {
      if (t < db[0]) return Math.floor((t - db[0]) / barLen);
      if (t >= db[last]) return last + Math.floor((t - db[last]) / barLen);
      return lastAtOrBefore(db, t);
    };
    return { startOf, indexAt, barLen };
  }

  // Pure: [start, end] (track seconds) of a `bars`-long quantized loop holding
  // `pos`. Whole-bar loops align to their own length on the bar grid; sub-bar
  // loops split the current bar evenly. If the loop would end within `guard`
  // of the playhead, the next region is taken (it would already have passed).
  function loopRegion(pos, bars, downbeats, bpm, duration, guard) {
    if (!(bars > 0) || !Number.isFinite(pos)) return null;
    const g = barGrid(downbeats, bpm);
    const gd = guard == null ? GUARD_S : guard;
    let start, end;
    if (bars >= 1) {
      const n = Math.round(bars);
      let k0 = g.indexAt(pos);
      k0 -= mod(k0, n);
      start = g.startOf(k0); end = g.startOf(k0 + n);
      if (end - pos < gd) { start = end; end = g.startOf(k0 + 2 * n); }
    } else {
      const k = g.indexAt(pos);
      const bs = g.startOf(k), sub = (g.startOf(k + 1) - bs) * bars;
      start = bs + Math.floor((pos - bs) / sub) * sub; end = start + sub;
      if (end - pos < gd) { start = end; end += sub; }
    }
    start = Math.max(0, start);
    if (duration > 0) end = Math.min(end, duration);
    return end - start > 0.01 ? [start, end] : null;
  }

  // Pure: the grid point (anchor + n * step) a hit at `pos` should land on:
  // the one just passed if the hit is at most `late` behind it, else the next.
  function nextGridPoint(pos, anchor, step, late) {
    if (!(step > 0)) return pos;
    const f = Math.floor((pos - anchor) / step + 1e-9);
    const prev = anchor + f * step;
    return pos - prev <= (late == null ? LATE_S : late) ? prev : prev + step;
  }

  // Pure: next beat for a hit at `pos` (analysed beats, else a flat BPM grid).
  function nextBeat(pos, beats, bpm, late) {
    const lt = late == null ? LATE_S : late;
    if (Array.isArray(beats) && beats.length > 8 && pos >= beats[0] && pos < beats[beats.length - 1]) {
      const i = lastAtOrBefore(beats, pos);
      return pos - beats[i] <= lt ? beats[i] : beats[i + 1];
    }
    return nextGridPoint(pos, 0, 60 / (bpm > 0 ? bpm : 128), lt);
  }

  // Pure: slice i of n over region [a, b].
  function sliceBounds(region, n, i) {
    const len = (region[1] - region[0]) / n;
    return [region[0] + i * len, region[0] + (i + 1) * len];
  }

  // Pure: filter knob v in [-1, 1] -> {bypass, hp, lp, hpQ, lpQ} (Hz).
  function filterFreqs(v) {
    const x = Math.max(-1, Math.min(1, Number(v) || 0));
    if (Math.abs(x) < DEAD_ZONE) return { bypass: true, hp: 10, lp: 22000, hpQ: 0.707, lpQ: 0.707 };
    if (x < 0) return { bypass: false, hp: 10, lp: 20000 * Math.pow(0.01, -x), hpQ: 0.707, lpQ: 0.707 + 0.8 * -x };
    return { bypass: false, hp: 20 * Math.pow(400, x), lp: 22000, hpQ: 0.707 + 0.8 * x, lpQ: 0.707 };
  }

  const core = { loopRegion, nextGridPoint, nextBeat, sliceBounds, filterFreqs, barGrid, LOOP_SIZES, SLICES };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof root.document === "undefined" || typeof audioCtx === "undefined" || !root.decks) return;

  const panel = document.getElementById("remix-panel");
  if (!panel) return;
  const decks = root.decks;
  const STEMS = ["drums", "bass", "vocals", "other"];
  const FULL = { drums: 1, bass: 1, vocals: 1, other: 1, bus: 0 };

  // ============================ audio inserts ==============================

  const rig = {};
  for (const id of Object.keys(decks)) {
    const d = decks[id];
    const gate = audioCtx.createGain();
    const chopBus = audioCtx.createGain();
    const dry = audioCtx.createGain();
    const wet = audioCtx.createGain();
    const hp = audioCtx.createBiquadFilter();
    const lp = audioCtx.createBiquadFilter();
    hp.type = "highpass"; hp.frequency.value = 10; hp.Q.value = 0.707;
    lp.type = "lowpass"; lp.frequency.value = 22000; lp.Q.value = 0.707;
    wet.gain.value = 0;
    try {
      d.inputGain.disconnect(d.lowFilter);
      d.inputGain.connect(gate); gate.connect(d.lowFilter);
      chopBus.connect(d.lowFilter);
      d.volumeGain.disconnect(d.fxInput);
      d.volumeGain.connect(dry); dry.connect(d.fxInput);
      d.volumeGain.connect(hp); hp.connect(lp); lp.connect(wet); wet.connect(d.fxInput);
    } catch (e) {
      console.warn("remix mode: deck graph not as expected, remix disabled", e);
      return;
    }
    rig[id] = { gate, chopBus, dry, wet, hp, lp, filter: 0, voice: null, loop: null, stemsTouched: false, fxOwned: null };
  }

  // ============================ element refs ===============================

  const $ = (id) => document.getElementById(id);
  const toggleBtn = $("remix-toggle");
  const controls = $("remix-controls");
  const lamp = $("remix-lamp");
  const targetSel = $("remix-target");
  const targetChip = $("remix-target-chip");
  const statusEl = $("remix-status");
  const filterEl = $("remix-filter");
  const filterLabel = $("remix-filter-label");
  const chopSrcSel = $("remix-chop-src");
  const loopBtns = [...panel.querySelectorAll("[data-remix-loop]")];
  const chopGrid = $("remix-chop-grid");
  const stemBtns = [...panel.querySelectorAll("[data-remix-stem]")];
  const fxBtns = [...panel.querySelectorAll("[data-remix-fx]")];
  const hitGrid = $("remix-hit-grid");
  let on = false;

  function status(msg) { if (statusEl) statusEl.textContent = msg; }

  function loudness(d) {
    return (d.crossfaderGain ? d.crossfaderGain.gain.value : 1) * (d.volumeGain ? d.volumeGain.gain.value : 1);
  }

  // AUTO: the loudest playing deck through the crossfader (what the room hears).
  function target() {
    const pref = targetSel ? targetSel.value : "auto";
    if (decks[pref]) return decks[pref];
    const playing = Object.values(decks).filter((d) => d.playing && !d._braking);
    if (!playing.length) return null;
    return playing.sort((x, y) => loudness(y) - loudness(x))[0];
  }

  function usable(d, what) {
    if (!on) return false;
    if (!d || !d.buffer) { status("Load and play a track first."); return false; }
    if (!d.playing || d._braking) { status(`Deck ${d.id.toUpperCase()} is not playing.`); return false; }
    if (d.reversed) { status(`Deck ${d.id.toUpperCase()} is reversed; ${what} needs forward play.`); return false; }
    if (d._spinningUp) { status("Wait for the platter to reach speed."); return false; }
    if (d._extPos) { status(`Another engine is driving deck ${d.id.toUpperCase()} right now.`); return false; }
    return true;
  }

  // Sources that follow the song clock (mix + live stems, not stem holds).
  function clockSources(d) {
    const out = d.source ? [[d.source, 0]] : [];
    const lag = d.stems ? d.stems.lag || 0 : 0;
    for (const [k, s] of Object.entries(d._stemSrc || {})) if (!k.startsWith("hold_")) out.push([s, lag]);
    return out;
  }

  // Change a running source's loop without ever passing through loopStart >= loopEnd.
  function setLoopPoints(s, a, b) {
    if (a >= s.loopEnd) { s.loopEnd = b; s.loopStart = a; } else { s.loopStart = a; s.loopEnd = b; }
    s.loop = true;
  }

  // ================================ LOOP ===================================

  function loopValid(d) {
    const r = rig[d.id];
    return !!(r.loop && d.loopOn && d.source === r.loop.src);
  }

  function setLoop(bars) {
    const d = target();
    if (!usable(d, "a loop")) return;
    const r = rig[d.id];
    if (loopValid(d) && r.loop.bars === bars) { exitLoop(d); return; }
    const pos = d._currentPosition();
    const a = d.analysis || {};
    const region = loopRegion(pos, bars, a.downbeat_times, d.bpm, d.buffer.duration);
    if (!region) { status("No room for that loop here."); return; }
    for (const [s, lag] of clockSources(d)) {
      const k = s._rateMul || 1;
      setLoopPoints(s, (region[0] + lag) * k, (region[1] + lag) * k);
    }
    d.loopOn = true;
    d.loopBeats = bars * 4;
    d._loopSpan = region;
    r.loop = { bars, region, src: d.source, at: audioCtx.currentTime };
    if (typeof root.syncLoopUI === "function") root.syncLoopUI(d.id);
    const ap = root.autopilotState && root.autopilotState.active ? " Autopilot still mixes out on schedule." : "";
    status(`Deck ${d.id.toUpperCase()}: looping ${label(bars)} on the bar grid. Press it again to let the record run.${ap}`);
    paint(true);
  }

  function exitLoop(d) {
    const r = rig[d.id];
    if (d.loopOn && d.source) {
      const pos = d._currentPosition();   // read while the loop still wraps
      for (const [s] of clockSources(d)) s.loop = false;
      d.loopOn = false;
      d._loopSpan = null;
      d.startOffset = pos;
      d.startedAt = audioCtx.currentTime;
      if (typeof root.syncLoopUI === "function") root.syncLoopUI(d.id);
    }
    r.loop = null;
    paint(true);
  }

  const label = (bars) => (bars < 1 ? `1/${Math.round(1 / bars)} bar` : `${bars} bar${bars === 1 ? "" : "s"}`);

  // ================================ CHOP ===================================

  function closeGate(r, at) { const g = r.gate.gain; g.cancelScheduledValues(at); g.setTargetAtTime(0, at, 0.002); }
  function openGate(r, at) { const g = r.gate.gain; g.cancelScheduledValues(at); g.setTargetAtTime(1, at, 0.003); }

  function endVoice(r, v, at, reopen) {
    v.env.gain.cancelScheduledValues(at);
    v.env.gain.setTargetAtTime(0, at, 0.003);
    try { v.src.stop(at + 0.05); } catch (e) { /* already stopped */ }
    if (reopen) openGate(r, at);
  }

  function chopDomain(d) {
    if (d.loopOn && d._loopSpan && d._loopSpan[1] > d._loopSpan[0]) return d._loopSpan;
    const a = d.analysis || {};
    return loopRegion(d._currentPosition(), 1, a.downbeat_times, d.bpm, d.buffer.duration, 0);
  }

  function chopDown(i) {
    const d = target();
    if (!usable(d, "chopping")) return;
    const r = rig[d.id];
    const vox = chopSrcSel && chopSrcSel.value === "vox";
    let buf = d.buffer, k = 1, lag = 0;
    if (vox) {
      if (!d.stemsReady || !d.stems.vocals) { status(`VOX chops need deck ${d.id.toUpperCase()}'s stems (separate it first).`); return; }
      buf = d.stems.vocals; k = d.stems.ratio || 1; lag = d.stems.lag || 0;
    }
    const dom = chopDomain(d);
    if (!dom) return;
    const [s0, s1] = sliceBounds(dom, SLICES, i);
    const len = s1 - s0;
    const pos = d._currentPosition(), rate = d._playbackRate();
    if (!(rate > 0.2)) return;
    const q = nextGridPoint(pos, dom[0], len, LATE_S);
    const when = audioCtx.currentTime + Math.max(0, (q - pos) / rate);
    const src = audioCtx.createBufferSource();
    src.buffer = buf;
    src.loop = true;
    src.loopStart = Math.max(0, (s0 + lag) * k);
    src.loopEnd = Math.min(buf.duration, (s1 + lag) * k);
    src.playbackRate.value = rate * k;
    const env = audioCtx.createGain();
    env.gain.setValueAtTime(0, when);
    env.gain.setTargetAtTime(1, when, 0.002);
    src.connect(env);
    env.connect(r.chopBus);
    src.onended = () => { src.disconnect(); env.disconnect(); };
    src.start(when, src.loopStart);
    const prev = r.voice;
    if (prev) endVoice(r, prev, when, prev.gated && vox);
    if (!vox) closeGate(r, when);
    r.voice = { src, env, pad: i, minEnd: when + len / rate, gated: !vox };
    lightChop(i, true);
  }

  function chopUp(i) {
    for (const r of Object.values(rig)) {
      const v = r.voice;
      if (!v || v.pad !== i) continue;
      endVoice(r, v, Math.max(audioCtx.currentTime, v.minEnd), v.gated);
      r.voice = null;
    }
    lightChop(i, false);
  }

  function stopChops() {
    for (const r of Object.values(rig)) {
      if (r.voice) { endVoice(r, r.voice, audioCtx.currentTime, true); r.voice = null; }
      openGate(r, audioCtx.currentTime);
    }
    if (chopGrid) chopGrid.querySelectorAll(".pad-lit").forEach((p) => p.classList.remove("pad-lit"));
  }

  function lightChop(i, lit) {
    const p = chopGrid && chopGrid.children[i];
    if (p) p.classList.toggle("pad-lit", lit);
  }

  // =============================== FILTER ==================================

  function applyFilter(id, v) {
    const r = rig[id];
    r.filter = v;
    const f = filterFreqs(v), t = audioCtx.currentTime;
    r.hp.frequency.setTargetAtTime(f.hp, t, 0.02);
    r.lp.frequency.setTargetAtTime(f.lp, t, 0.02);
    r.hp.Q.setTargetAtTime(f.hpQ, t, 0.02);
    r.lp.Q.setTargetAtTime(f.lpQ, t, 0.02);
    r.dry.gain.setTargetAtTime(f.bypass ? 1 : 0, t, 0.008);
    r.wet.gain.setTargetAtTime(f.bypass ? 0 : 1, t, 0.008);
  }

  function filterText(v) {
    const f = filterFreqs(v);
    if (f.bypass) return "FILTER OFF";
    const hz = v < 0 ? f.lp : f.hp;
    return `${v < 0 ? "LPF" : "HPF"} ${hz >= 1000 ? (hz / 1000).toFixed(1) + "k" : Math.round(hz)}`;
  }

  // ================================ STEMS ==================================

  const PRESETS = {
    acapella: { drums: 0, bass: 0, vocals: 1, other: 0 },
    instrumental: { drums: 1, bass: 1, vocals: 0, other: 1 },
  };

  function applyStems(d, next) {
    const r = rig[d.id];
    const full = STEMS.every((n) => next[n] > 0.5) && !(next.bus > 0);
    const ok = d.stemMix(full && !d.tempoStems ? null : next, 0, 0.03);
    if (ok) r.stemsTouched = !full;
    return ok;
  }

  function stemAction(key) {
    const d = target();
    if (!usable(d, "stem mutes")) return;
    if (!d.stemsReady) { status(`Deck ${d.id.toUpperCase()} has no stems loaded yet (separate it first).`); return; }
    const cur = { ...FULL, ...(d.stemState || {}) };
    let next;
    if (PRESETS[key]) {
      const p = PRESETS[key];
      const same = STEMS.every((n) => (cur[n] > 0.5) === (p[n] > 0.5));
      next = same ? { ...FULL } : { ...cur, ...p, bus: 0 };
    } else next = { ...cur, [key]: cur[key] > 0.5 ? 0 : 1 };
    applyStems(d, next);
    paint(true);
  }

  // ================================= FX ====================================

  function fxAction(type) {
    const d = target();
    if (!usable(d, "FX")) return;
    const u = root.fxUnits && root.fxUnits[d.id];
    if (!u) return;
    const r = rig[d.id];
    if (u.active && u.type === type) { u.setActive(false); r.fxOwned = null; }
    else { u.setType(type); u.setActive(true); r.fxOwned = type; }
    if (typeof root.updateFxTypeButtons === "function") root.updateFxTypeButtons(d.id);
    if (typeof root.updateFxPad === "function") root.updateFxPad(d.id);
    paint(true);
  }

  // ================================ HITS ===================================

  function hit(i) {
    const d = target();
    if (!usable(d, "quantized hits") || typeof root.triggerPad !== "function") return;
    const rate = d._playbackRate();
    const pos = d._currentPosition();
    const a = d.analysis || {};
    const q = rate > 0.2 ? nextBeat(pos, a.beat_times, d.bpm, LATE_S) : pos;
    root.triggerPad(i, audioCtx.currentTime + Math.max(0, (q - pos) / rate));
  }

  // ================================ MODE ===================================

  function setMode(next) {
    on = next;
    if (controls) controls.disabled = !on;
    if (toggleBtn) {
      toggleBtn.classList.toggle("is-on", on);
      toggleBtn.setAttribute("aria-pressed", String(on));
      toggleBtn.textContent = on ? "REMIX ON" : "REMIX OFF";
    }
    if (lamp) lamp.classList.toggle("lamp-ok", on);
    if (on) {
      refreshHitLabels();
      const d = target();
      status(d ? `Remixing deck ${d.id.toUpperCase()}. Loop a bar, chop it, filter it, strip the stems.` : "Play a track to remix it.");
    } else {
      release();
      status("Remix off. The record plays as it was.");
    }
    paint(true);
  }

  // Put every deck back the way remix mode found it.
  function release() {
    stopChops();
    for (const [id, r] of Object.entries(rig)) {
      const d = decks[id];
      if (loopValid(d)) exitLoop(d);
      r.loop = null;
      if (r.filter !== 0) applyFilter(id, 0);
      if (r.stemsTouched && d.stemsReady) applyStems(d, { ...FULL });
      r.stemsTouched = false;
      const u = root.fxUnits && root.fxUnits[id];
      if (u && r.fxOwned && u.active && u.type === r.fxOwned) {
        u.setActive(false);
        if (typeof root.updateFxTypeButtons === "function") root.updateFxTypeButtons(id);
        if (typeof root.updateFxPad === "function") root.updateFxPad(id);
      }
      r.fxOwned = null;
    }
    if (filterEl) filterEl.value = "0";
    if (filterLabel) filterLabel.textContent = filterText(0);
  }

  // =============================== UI build ================================

  function buildChopPads() {
    if (!chopGrid) return;
    chopGrid.innerHTML = "";
    for (let i = 0; i < SLICES; i++) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pad remix-chop";
      b.innerHTML = `<span class="pad-num">${i + 1}</span><span class="pad-sub">slice</span>`;
      b.addEventListener("pointerdown", (e) => { e.preventDefault(); b.setPointerCapture && b.setPointerCapture(e.pointerId); chopDown(i); });
      const up = () => chopUp(i);
      b.addEventListener("pointerup", up);
      b.addEventListener("pointercancel", up);
      // keyboard: Enter/Space = one slice
      b.addEventListener("click", (e) => { if (e.detail === 0) { chopDown(i); chopUp(i); } });
      chopGrid.appendChild(b);
    }
  }

  function padName(i) {
    const s = typeof padSamples !== "undefined" && padSamples[i];
    if (s && s.name) return s.name.length > 8 ? s.name.slice(0, 7) + "…" : s.name;
    return typeof SAMPLE_PADS !== "undefined" && SAMPLE_PADS[i] ? SAMPLE_PADS[i].name : `PAD ${i + 1}`;
  }

  function buildHitPads() {
    if (!hitGrid) return;
    hitGrid.innerHTML = "";
    const n = typeof SAMPLE_PADS !== "undefined" ? SAMPLE_PADS.length : 8;
    for (let i = 0; i < n; i++) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pad remix-hit";
      b.dataset.pad = String(i);   // triggerPad lights [data-pad] buttons when the hit sounds
      b.innerHTML = `<span class="pad-num"></span><span class="pad-sub">on beat</span>`;
      if (typeof root.bindPadPress === "function") root.bindPadPress(b, () => hit(i));
      else b.addEventListener("click", () => hit(i));
      hitGrid.appendChild(b);
    }
    refreshHitLabels();
  }

  function refreshHitLabels() {
    if (!hitGrid) return;
    [...hitGrid.children].forEach((b, i) => { b.querySelector(".pad-num").textContent = padName(i); });
  }

  // =============================== painting ================================

  // Dirty-flag painting: only touch the DOM when what it shows changed.
  let lastKey = "";
  function paint(force) {
    const d = on ? target() : null;
    const r = d ? rig[d.id] : null;
    const loopBars = d && loopValid(d) ? r.loop.bars : null;
    const st = d && d.stemState ? d.stemState : null;
    const u = d && root.fxUnits ? root.fxUnits[d.id] : null;
    const fx = u && u.active ? u.type : "";
    const laps = loopBars ? Math.floor(((audioCtx.currentTime - r.loop.at) * d._playbackRate()) / (r.loop.region[1] - r.loop.region[0])) : 0;
    const key = [on, d && d.id, loopBars, laps, st && STEMS.map((n) => st[n] > 0.5 ? 1 : 0).join(""), fx, d && d.stemsReady].join("|");
    if (!force && key === lastKey) return;
    lastKey = key;
    panel.classList.toggle("hud-a", !!d && d.id === "a");
    panel.classList.toggle("hud-b", !!d && d.id === "b");
    if (targetChip) targetChip.textContent = d ? `DECK ${d.id.toUpperCase()}${loopBars ? ` · LOOP ${label(loopBars).toUpperCase()} ×${laps + 1}` : ""}` : "NO DECK";
    for (const b of loopBtns) b.classList.toggle("is-on", loopBars === parseFloat(b.dataset.remixLoop));
    for (const b of stemBtns) {
      const k = b.dataset.remixStem;
      let lit;
      if (PRESETS[k]) lit = !!st && STEMS.every((n) => (st[n] > 0.5) === (PRESETS[k][n] > 0.5));
      else lit = !st || st[k] > 0.5;
      b.classList.toggle("is-on", lit && !!d && d.stemsReady);
      b.title = d && !d.stemsReady ? "Needs this deck's stems (separate it first)" : "";
    }
    for (const b of fxBtns) b.classList.toggle("is-on", fx === b.dataset.remixFx);
    if (filterEl && r && document.activeElement !== filterEl && parseFloat(filterEl.value) !== r.filter) {
      filterEl.value = String(r.filter);
      if (filterLabel) filterLabel.textContent = filterText(r.filter);
    }
  }

  // ================================ wiring =================================

  buildChopPads();
  buildHitPads();
  if (toggleBtn) toggleBtn.addEventListener("click", () => setMode(!on));
  for (const b of loopBtns) b.addEventListener("click", () => setLoop(parseFloat(b.dataset.remixLoop)));
  for (const b of stemBtns) b.addEventListener("click", () => stemAction(b.dataset.remixStem));
  for (const b of fxBtns) b.addEventListener("click", () => fxAction(b.dataset.remixFx));
  if (filterEl) {
    filterEl.addEventListener("input", () => {
      const v = parseFloat(filterEl.value);
      if (filterLabel) filterLabel.textContent = filterText(v);
      const d = target();
      if (on && d) applyFilter(d.id, v);
    });
    filterEl.addEventListener("dblclick", () => {
      filterEl.value = "0";
      filterEl.dispatchEvent(new Event("input"));
    });
  }
  if (targetSel) targetSel.addEventListener("change", () => paint(true));
  root.addEventListener("ai-activity", (e) => { if (e.detail && e.detail.kind === "stems") paint(false); });
  setInterval(() => { if (on) paint(false); }, 250);
  setMode(false);

  root.remixMode = {
    get active() { return on; },
    setMode, setLoop, exitLoop: () => { const d = target(); if (d) exitLoop(d); },
    chop: (i) => { chopDown(i); chopUp(i); }, applyFilter, core,
  };
})(typeof window !== "undefined" ? window : globalThis);
