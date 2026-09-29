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
// Reaches the world only through the Host port (engine.js): host.decks, host.mod (stemMoves, riffOverRap,
// autoSampler, mashup), host.ui, host.clock, host.api, host.bus.
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

  function create({ host }) {
  const { setTimeout } = host.clock;
  const audioCtx = host.audio;
  const ui = host.ui;
  const fetch = (url, opts) => host.api.fetch(url, opts);
  const root = { get decks() { return host.decks; }, get state() { return host.state; }, get stemMoves() { return host.mod.stemMoves; },
    get mashup() { return host.mod.mashup; }, get autoSampler() { return host.mod.autoSampler; }, get riffOverRap() { return host.mod.riffOverRap; } };

  // every on-demand action: the step log (step-log.js collects ai-activity) and the status line
  const say = (label, why, ok = true) => {
    host.bus.emit("ai-activity", { kind: "stem-move", deck: "", label: `${ok ? "" : "✗ "}${label}`, why });
    ui.status(`${label}: ${ok ? "" : "refused, "}${why}`);
  };
  // learned in-song moves, one kind now (dj-mind learnedNow -> learned-moves demand: only the rate gates skipped)
  const learned = (kind, label) => () => {
    const c = ctx(false); if (c.err) return say(label, c.err, false);
    const mind = host.mod.djMind;
    if (!mind || !mind.learnedNow) return say(label, "SET MIND not loaded", false);
    const r = mind.learnedNow(c.h, kind);
    return r.refused ? say(label, r.refused, false) : say(label, r.why);
  };

  function hostDeck() {
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
    const h = hostDeck();
    if (!h) return { err: "nothing is playing" };
    const d = root.decks[h], n = root.decks[other(h)];
    if (needNext && !(n && n.buffer && n.analysis)) return { err: `load a song on deck ${other(h).toUpperCase()} first` };
    const bar = 240 / (d.bpm || 128), rate = (d._playbackRate && d._playbackRate()) || 1;
    const pos = d._currentPosition();
    const line = nextLine(d.analysis && d.analysis.phrase_boundaries_8bar, pos, rate, 2 * bar / rate, bar);
    return { h, d, n, nId: other(h), bar, rate, pos, line, T: audioCtx.currentTime + (line - pos) / rate };
  }
  const setRange = (el, v) => { if (el) { el.value = String(v); ui.fire(el, "input", true); } };
  const eq = (deck, band) => ui.query(`.eq-knob[data-deck="${deck}"][data-band="${band}"]`);

  const ACTIONS = {
    async mix() {
      const c = ctx(true); if (c.err) return say("AUTO MIX", c.err, false);
      const n = c.n, sm = root.stemMoves;
      // tempo-lock next to host; key-locked tempo stems when the gap is big
      const target = c.d.bpm * c.rate;
      if (n.useTempoStems && Math.abs(target / n.bpm - 1) > 0.02) await n.useTempoStems(target);
      n.rampPitchPercent((target / n.bpm - 1) * 100, 0.005);
      const entry = (n.analysis.phrase_boundaries_8bar || [0])[0] || 0;
      const barS = c.bar / c.rate;
      n.play(entry, false, c.T);
      const xf = ui.el("crossfader");
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
      const kind = sm.core.remixPick({ vocal, used, count: 0, barsOnTrack: 99, barsLeft: 99, energy: sm.remixEnergy(c.d, c.line, c.bar) }) || "bass_out";
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
      const xf = ui.el("crossfader");
      root.riffOverRap.run(prep, c.h, c.nId, t0, {
        xf: (inn, f) => setRange(xf, (inn === "b" ? 1 : -1) * f),
        eq: (d, band, v) => setRange(eq(d, band), v),
        pitch: (d, pct) => root.decks[d].rampPitchPercent(pct, 0.005),
      });
      say("RIFF × RAP", `armed: starts at ${fmt(g0)} (in ${Math.round((g0 - pos) / c.rate)} s)`);
    },

    // merge -> hold -> transition on the loaded pair from the host's next line (autopilot planMerge /
    // holdPlan, the set's own gates; the refusal names the gate). Not while the set runs.
    merge_hold() {
      const c = ctx(true); if (c.err) return say("MERGE → HOLD", c.err, false);
      const ap = host.mod.autopilot;
      if (!ap || !ap.mergeNow) return say("MERGE → HOLD", "autopilot not loaded", false);
      const r = ap.mergeNow({ out: c.h, inn: c.nId, aId: trackId(c.h), bId: trackId(c.nId), aT: c.line, t0: c.T });
      return r.ok ? say("MERGE → HOLD", `at ${fmt(c.line)}: ${r.why}`) : say("MERGE → HOLD", r.why, false);
    },
    learned_vocal_loop: learned("vocal_loop", "VOX LOOP"),
    learned_vocal_resequence: learned("vocal_resequence", "RE-CUT"),
    learned_vocal_chop: learned("vocal_chop", "CHOPS"),
    learned_loop_extend: learned("loop_extend", "EXTEND"),
  };
  function trackId(id) { return root.state ? (id === "a" ? root.state.trackA : root.state.trackB) : null; }
  function fmt(t) { return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`; }

  // Buttons for the actions above that the page does not carry yet (the HTML stays untouched).
  const NEW_BUTTONS = [
    ["merge_hold", "MERGE → HOLD", "TRANSITIONS", "Merge the other deck in on the next line, hold both songs together, then hand over (the set's merge gates)"],
    ["learned_vocal_loop", "VOX LOOP", "LEARNED", "Learned move now: one vocal line looped on the beat (learned rate limits skipped, safety gates kept)"],
    ["learned_vocal_resequence", "RE-CUT", "LEARNED", "Learned move now: the vocal re-cut in a new order on the bar grid"],
    ["learned_vocal_chop", "CHOPS", "LEARNED", "Learned move now: short vocal chops on the 1/8 grid"],
    ["learned_loop_extend", "EXTEND", "LEARNED", "Learned move now: a break / intro extended by looping (needs SET MIND on this deck)"],
  ];
  const bar = ui.el("ai-actions");
  if (bar && ui.create) {
    for (const [id, label, group, title] of NEW_BUTTONS) {
      if (ui.query(`[data-ai-action="${id}"]`)) continue;
      const b = ui.create("button");
      b.className = "hw-btn ai-action";
      b.dataset.aiAction = id; b.dataset.group = group; b.title = title; b.textContent = label;
      if (bar.appendChild) bar.appendChild(b);
    }
  }

  // Contract for other modules: any [data-ai-action="<id>"] button runs ACTIONS[id] or a handler added with
  // register(id, fn); an id with neither is emitted as "ai-action" {id} on the host bus (window).
  function register(id, fn) {
    if (typeof fn !== "function") throw new Error(`aiActions.register(${id}): fn must be a function`);
    ACTIONS[id] = fn;
  }
  // (the Host bus: a window CustomEvent in the browser; engine modules never construct events themselves)
  function dispatch(id) { host.bus.emit("ai-action", { id }); }
  function bind(b) {
    if (!b || !b.dataset || b.dataset.aiBound) return;
    b.dataset.aiBound = "1";
    b.addEventListener("click", async () => {
      const id = b.dataset.aiAction;
      b.classList.add("ai-action-busy");
      try { if (typeof ACTIONS[id] === "function") await ACTIONS[id](); else dispatch(id); }
      catch (e) { say(b.textContent.trim(), e.message, false); }
      finally { setTimeout(() => b.classList.remove("ai-action-busy"), 600); }
    });
  }
  ui.queryAll("[data-ai-action]").forEach(bind);
  return { core, ...ACTIONS, register, bind };
  }
  if (root.Engine) root.Engine.mount("aiActions", create);
})(typeof window !== "undefined" ? window : globalThis);
