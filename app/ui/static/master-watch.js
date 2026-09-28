// Master watchdog: always on (not only during a hold loop, like the live ear).
//
// User: "ear should report where glitch was found, it just went silent as well".
// Every 250 ms it reads the master tap (live-ear.js ear-tap, 16 kHz) and checks:
//   silence    master under SILENT_DB for SILENT_S while a deck is on air
//   dropout    master falls >= DROP_DB in one chunk and stays down (not a fade)
//   clipping   samples at full scale
// A glitch is REPORTED with where it happened: set time, the on-air deck(s), song,
// song position, each deck's stem gains / EQ / fader, the move running (last
// stem-move / transition label). Report: browser console (warn), the status line,
// this session's event log (POST /api/session/event, kind "glitch").
// Silence is also RECOVERED: after RECOVER_S the on-air deck returns to its full
// mix (stems back to the mix, EQ lows reopened) and that is logged too.
(function (root) {
  const SILENT_DB = -55;       // master RMS under this = silence (music never sits that low)
  const SILENT_S = 1.0;        // ... for this long while a deck is on air
  const RECOVER_S = 1.5;       // then put the on-air deck back to its full mix
  const DROP_DB = 30;          // a sudden fall this big in one 250 ms step
  const CLIP_RUN = 8;          // clipped samples in one chunk to call it clipping
  const REPORT_GAP_S = 8;      // the same glitch kind is reported at most this often

  const dbOf = (rms) => 20 * Math.log10(Math.max(rms, 1e-9));

  // Pure core: feed one step's measurement, get the glitches it detects.
  // st: persistent state object; m: {t, rms, clips, onAir}. -> [{kind, detail}]
  function step(st, m) {
    const out = [], db = dbOf(m.rms);
    if (m.onAir && db < SILENT_DB) {
      if (st.silentSince == null) st.silentSince = m.t;
      const dur = m.t - st.silentSince;
      if (dur >= SILENT_S && !st.silenceReported) {
        st.silenceReported = true;
        out.push({ kind: "silence", detail: { seconds: +dur.toFixed(2), master_db: +db.toFixed(1) } });
      }
      if (dur >= RECOVER_S && !st.recovered) {
        st.recovered = true;
        out.push({ kind: "recover", detail: { seconds: +dur.toFixed(2) } });
      }
    } else {
      if (st.silentSince != null && st.silenceReported) out.push({ kind: "silence_end", detail: { seconds: +(m.t - st.silentSince).toFixed(2) } });
      st.silentSince = null; st.silenceReported = false; st.recovered = false;
    }
    if (st.lastDb != null && m.onAir && st.lastDb - db >= DROP_DB && db < SILENT_DB + 15) {
      out.push({ kind: "dropout", detail: { from_db: +st.lastDb.toFixed(1), to_db: +db.toFixed(1) } });
    }
    if (m.clips >= CLIP_RUN) out.push({ kind: "clipping", detail: { clipped_samples: m.clips } });
    st.lastDb = db;
    return out;
  }
  // de-duplicate: one report per kind per REPORT_GAP_S (recover / silence_end always pass)
  function allow(st, kind, t) {
    if (kind === "recover" || kind === "silence_end") return true;
    st.lastReport = st.lastReport || {};
    if (st.lastReport[kind] != null && t - st.lastReport[kind] < REPORT_GAP_S) return false;
    st.lastReport[kind] = t;
    return true;
  }
  const core = { step, allow, dbOf, SILENT_DB, SILENT_S, RECOVER_S, DROP_DB, CLIP_RUN, REPORT_GAP_S };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof root.document === "undefined" || typeof audioCtx === "undefined") return;

  // ------------------------------------------------------------ browser --
  const st = {};
  let lastMove = null;               // last stem move / transition the AI started
  root.addEventListener("ai-activity", (e) => { lastMove = { label: e.detail && e.detail.label, deck: e.detail && e.detail.deck, at: audioCtx.currentTime }; });
  root.addEventListener("ai-cue", (e) => { if (e.detail && e.detail.kind === "transition") lastMove = { label: `transition: ${e.detail.why || ""}`, deck: e.detail.deck, at: audioCtx.currentTime }; });

  const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  function onAirDecks() {
    const ds = root.decks || {};
    return Object.values(ds).filter((d) => d && d.playing && d.crossfaderGain && d.crossfaderGain.gain.value > 0.05 &&
      (!d.volumeGain || d.volumeGain.gain.value > 0.05));
  }
  function deckState(d) {
    const eq = {};
    for (const b of ["low", "mid", "high"]) {
      const el = document.querySelector(`.eq-knob[data-deck="${d.id}"][data-band="${b}"]`);
      eq[b] = el ? parseFloat(el.value) || 0 : null;
    }
    const t = document.getElementById(`title-${d.id}`);
    const name = (t && t.textContent.trim()) || (d.analysis && d.analysis.path ? d.analysis.path.split("/").pop() : null);
    return {
      deck: d.id, song: name, pos: fmt(d._currentPosition()), pos_s: +d._currentPosition().toFixed(2),
      stems: d.stemState ? { ...d.stemState } : "full mix", stems_ready: !!d.stemsReady,
      mix_gain: d.mixGain ? +d.mixGain.gain.value.toFixed(3) : null,
      xfader: +d.crossfaderGain.gain.value.toFixed(3), volume: d.volumeGain ? +d.volumeGain.gain.value.toFixed(3) : null, eq,
    };
  }
  function report(kind, detail) {
    const decks = Object.values(root.decks || {}).filter((d) => d && d.playing).map(deckState);
    const move = lastMove && audioCtx.currentTime - lastMove.at < 120
      ? { ...lastMove, seconds_ago: +(audioCtx.currentTime - lastMove.at).toFixed(1) } : null;
    const where = decks.map((x) => `${x.deck.toUpperCase()} ${x.song || "?"} @ ${x.pos}`).join(" | ") || "no deck playing";
    const ev = { kind, ...detail, where, decks, move };
    (kind === "silence_end" ? console.info : console.warn)(`master ${kind}: ${where}${move ? ` (during ${move.label})` : ""}`, ev);
    const s = document.getElementById("ap-status");
    if (s && kind !== "silence_end") s.textContent = `⚠ ${kind} on master: ${where}${move ? ` · during ${move.label}` : ""}`;
    try {
      fetch("/api/session/event", { method: "POST", headers: { "Content-Type": "application/json" }, keepalive: true,
        body: JSON.stringify({ kind: "glitch", data: ev }) }).catch(() => {});
    } catch (e) { /* never break playback */ }
  }
  function recover() {
    for (const d of onAirDecks()) {
      if (root.stemMoves && root.stemMoves.reset) root.stemMoves.reset(d);   // cancels booked moves, full mix
      else if (d.stemMix) d.stemMix(null, 0, 0.05);
      for (const b of ["low", "mid", "high"]) {
        const el = document.querySelector(`.eq-knob[data-deck="${d.id}"][data-band="${b}"]`);
        if (el && parseFloat(el.value) < -20) { el.value = 0; el.dispatchEvent(new Event("input", { bubbles: true })); }
      }
    }
  }

  let armed = false;
  function tick() {
    const ear = root.liveEar;
    if (!ear || !ear.lastSamples) return;
    if (!armed && ear.startTap) { ear.startTap(); armed = true; }
    const { samples } = ear.lastSamples(0.25);
    if (!samples || samples.length < 400) return;
    let sum = 0, clips = 0;
    for (let i = 0; i < samples.length; i++) { const v = samples[i]; sum += v * v; if (v >= 0.999 || v <= -0.999) clips++; }
    const onAir = onAirDecks().length > 0;
    for (const g of step(st, { t: audioCtx.currentTime, rms: Math.sqrt(sum / samples.length), clips, onAir })) {
      if (!allow(st, g.kind, audioCtx.currentTime)) continue;
      if (g.kind === "recover") {
        recover();
        report("recovered", { ...g.detail, action: "on-air deck back to its full mix, EQ lows reopened" });
      } else report(g.kind, g.detail);
    }
  }
  setInterval(tick, 250);
  root.masterWatch = { core, report };
})(typeof window !== "undefined" ? window : globalThis);
