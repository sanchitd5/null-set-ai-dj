// AI Music Brain - AI ACTIONS: every automatic move, on demand.
//
// Each button acts on the audible deck ("host") and the other loaded deck
// ("next"), lands on the host's next 8-bar phrase line, and uses the same
// engines and rules as the autopilot (stem-moves.js, riff-over-rap.js,
// auto-sampler.js, mashup-layer.js). A button says why when it can't go.
//
//   AUTO MIX        stem blend host -> next (EQ recipe engine if no stems)
//   AUTO MASHUP     next's vocal phrase over host (host's vocal muted via stems)
//   AUTO SAMPLE     sampler move into the next line (riser + roll + clap / hat)
//   STRIP & REBUILD the 40/24-bar stem breakdown on the host
//   VOCAL SWAP      host drops to its instrumental / back to the full mix
//   RIFF × RAP      armed: runs when the host reaches its groove (if the pair fits)
//
// Depends on globals: audioCtx, decks, stemMoves, riffOverRap, autoSampler, mashup.
(function (root) {
  "use strict";

  // Pure: the next 8-bar line at least `leadS` of audio ahead. Returns track time.
  function nextLine(phrases, pos, rate, leadS, barS) {
    for (const p of phrases || []) if ((p - pos) / rate >= leadS) return p;
    const last = (phrases || []).length ? phrases[phrases.length - 1] : pos;
    const L = 8 * barS;
    let t = last;
    while ((t - pos) / rate < leadS) t += L;
    return t;
  }
  const core = { nextLine };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof root.document === "undefined" || typeof audioCtx === "undefined") return;

  const say = (label, why, ok = true) => root.dispatchEvent(new CustomEvent("ai-activity",
    { detail: { kind: "stem-move", deck: "", label: `${ok ? "" : "✗ "}${label}`, why } }));

  function host() {
    let best = null, lv = -1;
    for (const id of ["a", "b"]) {
      const d = root.decks[id];
      if (!d || !d.playing) continue;
      const g = (d.crossfaderGain ? d.crossfaderGain.gain.value : 1) * (d.volumeGain ? d.volumeGain.gain.value : 1);
      if (g > lv) { lv = g; best = id; }
    }
    return best;
  }
  const other = (id) => (id === "a" ? "b" : "a");
  function ctx(needNext) {
    const h = host();
    if (!h) return { err: "nothing is playing" };
    const d = root.decks[h], n = root.decks[other(h)];
    if (needNext && !(n && n.buffer && n.analysis)) return { err: `load a song on deck ${other(h).toUpperCase()} first` };
    const bar = 240 / (d.bpm || 128), rate = (d._playbackRate && d._playbackRate()) || 1;
    const pos = d._currentPosition();
    const line = nextLine(d.analysis && d.analysis.phrase_boundaries_8bar, pos, rate, 2 * bar / rate, bar);
    return { h, d, n, nId: other(h), bar, rate, pos, line, T: audioCtx.currentTime + (line - pos) / rate };
  }
  const setRange = (el, v) => { if (el) { el.value = String(v); el.dispatchEvent(new Event("input", { bubbles: true })); } };
  const eq = (deck, band) => document.querySelector(`.eq-knob[data-deck="${deck}"][data-band="${band}"]`);

  const ACTIONS = {
    async mix() {
      const c = ctx(true); if (c.err) return say("AUTO MIX", c.err, false);
      const n = c.n, sm = root.stemMoves;
      // tempo-lock next to host; key-locked tempo stems when the gap is big
      const target = c.d.bpm * c.rate;
      if (n.useTempoStems && Math.abs(target / n.bpm - 1) > 0.02) await n.useTempoStems(target);
      n.setPitchPercent((target / n.bpm - 1) * 100);
      const entry = (n.analysis.phrase_boundaries_8bar || [0])[0] || 0;
      const barS = c.bar / c.rate;
      n.play(entry, false, c.T);
      const xf = document.getElementById("crossfader");
      ["low", "mid", "high"].forEach((b) => { setRange(eq(c.h, b), 0); setRange(eq(c.nId, b), 0); });
      if (sm && c.d.stemsReady && n.stems) {
        await new Promise((r) => setTimeout(r, 120));        // next's stem sources are booked with its play()
        if (sm.stemBlend("blend", c.h, c.nId, c.T, 16, barS, "on demand: 16-bar stem blend")) {
          // crossfader sweeps across the blend: no jump to the centre
          const from = c.h === "a" ? -1 : 1, to = -from, steps = 32;
          for (let i = 1; i <= steps; i++) setTimeout(() => setRange(xf, from + (to - from) * (i / steps)),
            (c.T - audioCtx.currentTime) * 1000 + (16 * barS * 1000 * i) / steps);
          setTimeout(() => c.d.stopNow(), (c.T - audioCtx.currentTime + 16 * barS) * 1000 + 300);
          return;
        }
      }
      // no stems: classic EQ blend (next rises with no lows, bass swap on bar 8)
      setRange(eq(c.nId, "low"), -26);
      const steps = 32;
      for (let i = 1; i <= steps; i++) setTimeout(() => setRange(xf, (c.nId === "b" ? 1 : -1) * (i / steps)),
        (c.T - audioCtx.currentTime) * 1000 + (16 * barS * 1000 * i) / steps);
      setTimeout(() => { setRange(eq(c.h, "low"), -26); setRange(eq(c.nId, "low"), 0); }, (c.T - audioCtx.currentTime + 8 * barS) * 1000);
      setTimeout(() => c.d.stopNow(), (c.T - audioCtx.currentTime + 16 * barS) * 1000 + 300);
      say("AUTO MIX", "no stems on both decks: 16-bar EQ blend, bass swap on bar 8");
    },

    async mashup() {
      const c = ctx(true); if (c.err) return say("AUTO MASHUP", c.err, false);
      if (!root.mashup) return say("AUTO MASHUP", "mashup engine not loaded", false);
      const res = await fetch("/api/mashup/plan", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ host_id: trackId(c.h), guest_id: trackId(c.nId), bars: 16, host_mutable: !!c.d.stemsReady }) });
      const plan = await res.json();
      if (!res.ok || !plan.ok) return say("AUTO MASHUP", plan.detail || (plan.reasons || []).join("; ") || "no plan", false);
      const pos = c.d._currentPosition();
      const entry = plan.host_entries.find((e) => (e - pos) / c.rate >= 3) ?? null;
      if (entry == null) return say("AUTO MASHUP", "no fitting phrase left in this song", false);
      if (!(await root.mashup.play(c.h, plan, entry))) return say("AUTO MASHUP", "the phrase passed while loading", false);
      if (plan.mute_host_vocals && root.stemMoves) {
        const sm = root.stemMoves, on = sm.audioAt(c.d, entry), off = sm.audioAt(c.d, entry + plan.host_duration);
        setTimeout(() => c.d.stemMix({ vocals: 0 }, on, 0.05), Math.max(0, (on - audioCtx.currentTime) * 1000 - 200));
        setTimeout(() => c.d.stemMix(null, off, 0.2), Math.max(0, (off - audioCtx.currentTime) * 1000 - 200));
      }
      if (root.stemMoves && c.d.stemsReady) root.stemMoves.mashupBreak(c.d, entry, plan.bars, !!plan.mute_host_vocals);
      say("AUTO MASHUP", `${plan.bars} bars of deck ${c.nId.toUpperCase()}'s vocal at ${fmt(entry)}${plan.mute_host_vocals ? ", host instrumental" : ""}`);
    },

    sample() {
      const c = ctx(false); if (c.err) return say("AUTO SAMPLE", c.err, false);
      if (!root.autoSampler) return say("AUTO SAMPLE", "sampler not loaded", false);
      root.autoSampler.fire("drop", c.T, c.bar / c.rate, c.h, `on demand, into ${fmt(c.line)}`, true);
    },

    strip() {
      const c = ctx(false); if (c.err) return say("STRIP & REBUILD", c.err, false);
      if (!c.d.stemsReady) return say("STRIP & REBUILD", "this deck's stems aren't ready", false);
      const room = (c.d.buffer.duration - c.line) / c.bar;
      const bars = room >= 48 ? 40 : room >= 30 ? 24 : 0;
      if (!bars) return say("STRIP & REBUILD", "not enough song left", false);
      root.stemMoves.breakdown(c.d, c.line, bars, "on demand");
    },

    remix() {
      const c = ctx(false); if (c.err) return say("STEM REMIX", c.err, false);
      if (!c.d.stemsReady) return say("STEM REMIX", "this deck's stems aren't ready", false);
      const sm = root.stemMoves, used = (c.d._remix && c.d._remix.used) || [];
      const vocal = sm.vocalShare(c.d.analysis && c.d.analysis.vocal_active_regions, c.line, c.line + 16 * c.bar);
      const kind = sm.core.remixPick({ vocal, used, count: 0, barsOnTrack: 99, barsLeft: 99 }) || "bass_out";
      if (sm.remix(c.d, c.line, kind, 16, "on demand")) {
        const r = c.d._remix || (c.d._remix = { used: [], count: 0, lastAtBar: null });
        r.used.push(kind);
      }
    },

    hold() {
      const c = ctx(false); if (c.err) return say("HOLD VOX", c.err, false);
      if (!c.d.stemsReady) return say("HOLD VOX", "this deck's stems aren't ready", false);
      const sm = root.stemMoves, at = sm.audioAt(c.d, c.line), until = sm.audioAt(c.d, c.line + 4 * c.bar);
      c.d.stemMix({}, at - 0.01, 0.005);
      setTimeout(() => c.d.holdStem("vocals", c.line - c.bar, 1, at, until), Math.max(0, (at - audioCtx.currentTime) * 1000 - 250));
      setTimeout(() => c.d.stemMix(null, until + 0.03, 0.02), Math.max(0, (until - audioCtx.currentTime) * 1000 - 200));
      say("HOLD VOX", `the vocal's last bar held for 4 bars at ${fmt(c.line)}`);
    },

    vocals() {
      const c = ctx(false); if (c.err) return say("VOCAL SWAP", c.err, false);
      const inst = c.d.stemState && c.d.stemState.vocals < 0.5;
      if (!root.stemMoves.instrumental(c.d, !inst, c.line)) say("VOCAL SWAP", "this deck's stems aren't ready", false);
    },

    async riff() {
      const c = ctx(true); if (c.err) return say("RIFF × RAP", c.err, false);
      if (!root.riffOverRap) return say("RIFF × RAP", "engine not loaded", false);
      say("RIFF × RAP", "planning: key-locking this song's stems to the next song's tempo…");
      const prep = await root.riffOverRap.prepare(trackId(c.h), trackId(c.nId), c.pos + 4);
      if (!prep.ok) return say("RIFF × RAP", `this pair doesn't fit: ${(prep.reasons || []).join("; ")}`, false);
      const g0 = prep.plan.a_groove[0], pos = c.d._currentPosition();
      if (g0 < pos + 1) return say("RIFF × RAP", `this song is past its groove (${fmt(g0)})`, false);
      const t0 = audioCtx.currentTime + (g0 - pos) / c.rate;
      const xf = document.getElementById("crossfader");
      root.riffOverRap.run(prep, c.h, c.nId, t0, {
        xf: (inn, f) => setRange(xf, (inn === "b" ? 1 : -1) * f),
        eq: (d, band, v) => setRange(eq(d, band), v),
        pitch: (d, pct) => root.decks[d].setPitchPercent(pct),
      });
      say("RIFF × RAP", `armed: starts at ${fmt(g0)} (in ${Math.round((g0 - pos) / c.rate)} s)`);
    },
  };
  function trackId(id) { return root.state ? (id === "a" ? root.state.trackA : root.state.trackB) : null; }
  function fmt(t) { return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`; }

  document.querySelectorAll("[data-ai-action]").forEach((b) => b.addEventListener("click", async () => {
    b.classList.add("ai-action-busy");
    try { await ACTIONS[b.dataset.aiAction](); } catch (e) { say(b.textContent.trim(), e.message, false); }
    finally { setTimeout(() => b.classList.remove("ai-action-busy"), 600); }
  }));
  root.aiActions = { core, ...ACTIONS };
})(typeof window !== "undefined" ? window : globalThis);
