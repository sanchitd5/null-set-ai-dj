// AI Music Brain - stem moves on live decks (deck-controller.js stemMix).
//
//  breakdown  The USB002 move (Fred again.. & Bangalter, leavemealone at 74:22,
//             measured with 4-stem Demucs): the vocal never stops while the
//             song is stripped and rebuilt over 40 bars: drums out -> bass out
//             (voice alone) -> bass back, still no kick -> bass out again while
//             drums creep back (build) -> everything slams back on the phrase
//             line. Lets a famous song play in full without sounding long.
//  handoff    Crossfade with ONE singer: the incoming deck enters as its
//             instrumental while the outgoing vocal moves to the vocal bus
//             (past A's EQ/fader) and keeps singing over B's beat; B's own
//             vocal returns as A's fades on the last bar.
//  instrumental  Mute a deck's vocal (hold loops that cross a sung line).
//
// All times on the audio clock. Pure schedules are exported for node checks.
(function (root) {
  "use strict";

  // [bar, stem targets, ramp in beats]; bar counted from the phrase line.
  const BREAKDOWN = {
    40: [
      [0, { drums: 0 }, 1],
      [4, { bass: 0 }, 1],
      [12, { bass: 1 }, 0.25],
      [24, { bass: 0 }, 1],
      [32, { drums: 0.35 }, 16],       // drums creep back over 4 bars
      [39.75, { drums: 0.7 }, 1],
      [40, null, 0.1],                 // full drop on the line
    ],
    24: [
      [0, { drums: 0 }, 1],
      [4, { bass: 0 }, 1],
      [8, { bass: 1 }, 0.25],
      [16, { bass: 0 }, 1],
      [20, { drums: 0.4 }, 12],
      [24, null, 0.1],
    ],
  };
  const BREAKDOWN_MIN_VOCAL = 0.5;     // the voice carries the stripped bars
  const BREAKDOWN_MIN_BARS_IN = 32;    // let the song establish itself first
  const BREAKDOWN_TAIL_BARS = 16;      // room after the drop before the exit

  function vocalShare(regions, a, b) {
    if (!regions || !regions.length || b <= a) return 0;
    let cov = 0;
    for (const [s, e] of regions) cov += Math.max(0, Math.min(e, b) - Math.max(s, a));
    return cov / (b - a);
  }
  // Which breakdown fits here, or null. ctx: {bar, pos, duration, exitAt,
  // barsOnTrack, vocals, famous, used}
  function breakdownFits(ctx) {
    if (!ctx.famous || ctx.used || (ctx.barsOnTrack || 0) < BREAKDOWN_MIN_BARS_IN) return null;
    const limit = Math.min(ctx.duration - 8 * ctx.bar, ctx.exitAt != null ? ctx.exitAt : Infinity);
    for (const bars of [40, 24]) {
      const end = ctx.pos + (bars + BREAKDOWN_TAIL_BARS) * ctx.bar;
      if (end > limit) continue;
      if (vocalShare(ctx.vocals, ctx.pos, ctx.pos + bars * ctx.bar) < BREAKDOWN_MIN_VOCAL) continue;
      return bars;
    }
    return null;
  }
  // Handoff when both decks run stems, the keys sit together and the outgoing
  // song is actually singing in the blend window.
  function handoffFits(ctx) {
    return !!(ctx.outStems && ctx.inStems && ctx.keyScore >= 0.8 && ctx.outVocal >= 0.3);
  }
  // Stem blend: a transition done with stems instead of EQ. Each layer has one
  // owner at a time. bars = 8 or 16. aSings / bSings: vocals in the window.
  // Returns [{bar, deck: "out"|"in", stems, ramp (bars)}], bars from B's entry.
  function stemBlendPlan(kind, bars, aSings, bSings, keyClash = false) {
    const L = bars, swap = L / 2, ev = [];
    if (kind === "double") {
      // both drops together: B owns drums + bass, A keeps its tops (and its voice
      // only if B isn't singing); A leaves on the line after L bars
      ev.push({ bar: 0, deck: "in", stems: null, ramp: 0 });
      ev.push({ bar: 0, deck: "out", stems: { drums: 0, bass: 0, vocals: bSings ? 0 : 1, other: 1 }, ramp: 0 });
      ev.push({ bar: L - 1, deck: "out", stems: { drums: 0, bass: 0, vocals: 0, other: 0 }, ramp: 1 });
      return ev;
    }
    // 0..swap   B's synths/pads fade in under A (no kick, no bass, no voice).
    // Keys clash: one tonal owner, B's synths wait until A's have faded.
    ev.push({ bar: 0, deck: "in", stems: { drums: 0, bass: 0, vocals: 0, other: 0 }, ramp: 0 });
    if (!keyClash) ev.push({ bar: 0.01, deck: "in", stems: { drums: 0, bass: 0, vocals: 0, other: 0.8 }, ramp: swap });
    // swap line  kick + bass change hands together, in one beat
    ev.push({ bar: swap - 0.25, deck: "out", stems: { drums: 0, bass: 0 }, ramp: 0.25 });
    ev.push({ bar: swap, deck: "in", stems: { drums: 1, bass: 1 }, ramp: 0.05 });
    // swap..L   A's synths fade; one singer: A finishes its line, then B's voice
    ev.push({ bar: swap, deck: "out", stems: { other: 0 }, ramp: keyClash ? (L - swap) / 2 : L - swap });
    if (keyClash) ev.push({ bar: swap + (L - swap) / 2, deck: "in", stems: { other: 1 }, ramp: (L - swap) / 2 });
    const aVoxOut = aSings ? L - 2 : swap;
    ev.push({ bar: aVoxOut, deck: "out", stems: { vocals: 0 }, ramp: aSings ? 2 : 1 });
    ev.push({ bar: aSings ? L - 0.5 : swap, deck: "in", stems: keyClash ? { vocals: 1 } : { vocals: 1, other: 1 }, ramp: aSings ? 0.5 : 2 });
    ev.push({ bar: L, deck: "in", stems: null, ramp: 0.05 });
    return ev;
  }
  const STEM_BLEND_KINDS = new Set(["bass", "blend", "filter", "loop", "double"]);

  // Stem bridge: across ANY tempo gap, no echo-out (user: "Echo Out is painful").
  // No two beats ever overlap, so the tempos never meet:
  //   A bar 0   A's drums out            (strip)
  //   A bar 2   A's bass out: voice + synths, beatless
  //   A bar 4   HOLD VOX: A's last vocal bar held; B starts, beatless (pads, no drums,
  //             no bass; its own voice only if the keys agree), crossfader sweeps A->B
  //   B entry   B's bass back 2 B-bars before its line, B's drums ON its line: the
  //             rebuild lands on B's own grid; A has faded out just before
  // Times in seconds from A's bar 0. barA / barB = seconds per bar of each song.
  function stemBridgePlan(barA, barB, keyClash, aSings) {
    const bStart = 4 * barA, bEntry = bStart + 4 * barB;
    const LIFT = 1.25;                 // ~+2 dB on A's voice + synths once its beat is gone (no sag)
    const swapAt = bEntry - 3 * barB;  // tonal layers change hands here, equal power over 2 B-bars
    const ev = [
      { t: 0, deck: "out", stems: { drums: 0, vocals: LIFT, other: LIFT }, ramp: barA },
      { t: 2 * barA, deck: "out", stems: { bass: 0 }, ramp: barA },
      { t: bStart, deck: "in", start: true, stems: { drums: 0, bass: 0, vocals: 0, other: 0 }, ramp: 0 },
      // keys agree: B's pads rise under A from the start of the beatless stretch
      { t: bStart + 0.01, deck: "in", stems: { other: keyClash ? 0 : 0.7, vocals: 0 }, ramp: 2 * barB },
      // the swap: A's tones out while B's bass + tones come in (one crossfade, no gap)
      { t: Math.max(bStart, swapAt), deck: "out", stems: { vocals: 0, other: 0 }, ramp: 2 * barB },
      { t: Math.max(bStart, swapAt), deck: "in", stems: { bass: 1, other: 1 }, ramp: 2 * barB },
      { t: bEntry, deck: "in", stems: null, ramp: 0.03 },   // B's beat, on its own line
    ];
    if (aSings) ev.push({ t: bStart, deck: "out", hold: { stem: "vocals", fromBar: 3, bars: 1 }, until: Math.max(bStart, swapAt) + 2 * barB });
    return { events: ev.sort((x, y) => x.t - y.t), bStart, bEntry, total: bEntry + barB };
  }

  // ---- remix on the go: stem on/offs and holds inside 16 / 32-bar sections ----
  // Each move lives in the last quarter of its section and resolves on the next
  // line with everything back (the drop). Bars are from the section start.
  //   vocal_hold  the vocal's last bar held (looped) over the section's end: "hold on"
  //   acapella    everything but the vocal out, back on the line
  //   drum_break  drums alone, back on the line
  //   bass_out    bass out for the second half, back on the line
  //   synth_hold  the synth/riff bar before the break held under a drumless end
  function remixEvents(kind, len = 16) {
    const q = len * 0.75, e = len;
    switch (kind) {
      case "vocal_hold": return [{ bar: q, hold: { stem: "vocals", fromBar: q - 1, bars: 1, untilBar: e } }];
      case "acapella": return [{ bar: q, stems: { drums: 0, bass: 0, other: 0 }, ramp: 0.25 }, { bar: e, stems: null, ramp: 0.02 }];
      case "drum_break": return [{ bar: q, stems: { bass: 0, vocals: 0, other: 0 }, ramp: 0.25 }, { bar: e, stems: null, ramp: 0.02 }];
      case "bass_out": return [{ bar: len / 2, stems: { bass: 0 }, ramp: 0.5 }, { bar: e, stems: null, ramp: 0.02 }];
      case "synth_hold": return [{ bar: q, stems: { drums: 0, bass: 0 }, ramp: 0.25, hold: { stem: "other", fromBar: q - 1, bars: 1, untilBar: e } },
        { bar: e, stems: null, ramp: 0.02 }];
      default: return [];
    }
  }
  // Which move fits this section. ctx: {vocal (share 0..1), used [kinds this song], count, barsOnTrack, barsLeft, lastAtBar, atBar}
  const REMIX_MAX_PER_SONG = 3, REMIX_GAP_BARS = 32;
  function remixPick(ctx) {
    if ((ctx.count || 0) >= REMIX_MAX_PER_SONG) return null;
    if ((ctx.barsOnTrack || 0) < 32 || (ctx.barsLeft || 0) < 48) return null;
    if (ctx.lastAtBar != null && ctx.atBar - ctx.lastAtBar < REMIX_GAP_BARS) return null;
    const used = new Set(ctx.used || []);
    const menu = ctx.vocal >= 0.5 ? ["vocal_hold", "acapella", "bass_out"]
      : ctx.vocal >= 0.15 ? ["bass_out", "drum_break", "synth_hold"] : ["drum_break", "synth_hold", "bass_out"];
    return menu.find((k) => !used.has(k)) || null;
  }
  const core = { BREAKDOWN, breakdownFits, handoffFits, vocalShare, stemBlendPlan, STEM_BLEND_KINDS, remixEvents, remixPick, stemBridgePlan };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof root.document === "undefined" || typeof audioCtx === "undefined") return;

  // ------------------------------------------------------------ browser --
  const timers = { a: [], b: [] };
  function note(deckId, label, why) {
    root.dispatchEvent(new CustomEvent("ai-activity", { detail: { kind: "stem-move", deck: deckId, label, why } }));
  }
  // Audio time at which deck `d` reaches track time `t` (no loop wrap).
  function audioAt(d, t) {
    const rate = (d._playbackRate && d._playbackRate()) || 1;
    return audioCtx.currentTime + Math.max(0, (t - d._currentPosition()) / rate);
  }
  function cancel(deckId) {
    (timers[deckId] || []).forEach(clearTimeout);
    timers[deckId] = [];
  }
  // Schedule gain moves; setTimeout only books them ~200 ms early, the ramps
  // themselves land on the audio clock.
  function book(d, at, target, ramp) {
    const lead = Math.max(0, (at - audioCtx.currentTime) * 1000 - 200);
    timers[d.id].push(setTimeout(() => { if (d.playing) d.stemMix(target, at, ramp); }, lead));
  }

  function breakdown(d, startTrackT, bars, why) {
    const plan = BREAKDOWN[bars];
    if (!plan || !d.stemsReady) return false;
    cancel(d.id);
    const bar = 240 / (d.bpm || 128), rate = (d._playbackRate && d._playbackRate()) || 1;
    const t0 = audioAt(d, startTrackT), beat = bar / 4 / rate;
    for (const [b, target, rampBeats] of plan) book(d, t0 + (b * bar) / rate, target, rampBeats * beat);
    root.dispatchEvent(new CustomEvent("ai-cue", { detail: { at: t0 + (plan[plan.length - 1][0] * bar) / rate, kind: "drop",
      deck: d.id, bar: bar / rate, why: "everything slams back after the strip & rebuild" } }));
    note(d.id, `STRIP & REBUILD · ${bars} bars`, why || "drums out, bass out, voice alone, rebuild, drop on the line");
    return true;
  }

  // out/inn: deck ids. t0: audio time the blend starts; totalS: its length (s).
  function handoff(outId, innId, t0, totalS, why) {
    const out = root.decks[outId], inn = root.decks[innId];
    if (!out || !inn || !out.stemsReady || !inn.stemsReady) return false;
    cancel(outId); cancel(innId);
    const bar = 240 / (out.bpm || 128) / ((out._playbackRate && out._playbackRate()) || 1);
    // B enters as its instrumental; A's vocal leaves A's strip for the bus.
    inn.stemMix({ vocals: 0 }, t0, 0.02);
    out.stemMix({ vocals: 0, bus: 1 }, t0, bar / 4);
    // Last bar: A's voice fades, B's own vocal comes back in.
    book(out, t0 + totalS - bar, { bus: 0 }, bar);
    book(inn, t0 + totalS - bar / 2, { vocals: 1 }, bar / 2);
    book(inn, t0 + totalS + bar, null, 0.05);
    note(outId, `VOCAL HANDOFF ${outId.toUpperCase()} → ${innId.toUpperCase()}`, why || "one singer: A's vocal rides B's beat, B's vocal enters as A's fades");
    return true;
  }

  function instrumental(d, on, atTrackT, why) {
    if (!d || !d.stemsReady) return false;
    const at = atTrackT == null ? 0 : audioAt(d, atTrackT);
    const ok = d.stemMix(on ? { vocals: 0 } : null, at, 0.25);
    if (ok) note(d.id, on ? "INSTRUMENTAL" : "VOCAL BACK", why || (on ? "loop runs as the instrumental: no chopped singer" : "full mix"));
    return ok;
  }

  // Deck stopped / reloaded: never leave its mix muted.
  function reset(d) { if (d) { cancel(d.id); d.stemMix(null, 0, 0.005); } }

  // Run a stem blend from audio time t0 (B's first downbeat). barS = seconds per bar.
  function stemBlend(kind, outId, innId, t0, bars, barS, why) {
    const out = root.decks[outId], inn = root.decks[innId];
    if (!out || !inn || !out.stemsReady || !inn.stemsReady || !STEM_BLEND_KINDS.has(kind)) return false;
    cancel(outId); cancel(innId);
    const pA = out._positionAt ? out._positionAt(t0) : out._currentPosition();
    const aSings = vocalShare(out.analysis && out.analysis.vocal_active_regions, pA, pA + bars * barS) >= 0.3;
    const pB = (inn.cuePoint || 0);
    const bSings = vocalShare(inn.analysis && inn.analysis.vocal_active_regions, pB, pB + bars * barS) >= 0.3;
    const ka = out.analysis && out.analysis.key && out.analysis.key.camelot, kb = inn.analysis && inn.analysis.key && inn.analysis.key.camelot;
    const cs = root.djMind && root.djMind.core && root.djMind.core.camelotScore;
    const keyClash = !!(cs && ka && kb && cs(ka, kb) < 0.8);
    for (const e of stemBlendPlan(kind, bars, aSings, bSings, keyClash)) {
      const d = e.deck === "out" ? out : inn, at = t0 + e.bar * barS;
      if (e.bar === 0 && e.deck === "in") {
        // B must be silent-in-stems from its very first sample
        setTimeout(() => d.stemMix(e.stems, at - 0.005, 0.005), Math.max(0, (at - audioCtx.currentTime) * 1000 - 400));
      } else book(d, at, e.stems, Math.max(0.005, e.ramp * barS));
    }
    note(outId, `STEM ${kind === "double" ? "DOUBLE DROP" : "BLEND"} ${outId.toUpperCase()} → ${innId.toUpperCase()}`,
      why || `${bars} bars: synths first, kick + bass swap on bar ${kind === "double" ? 0 : bars / 2}, one singer${aSings ? " (A finishes its line)" : ""}`);
    return true;
  }

  // Run one remix move on deck d for the section starting at track time `lineT`.
  const REMIX_LABEL = { vocal_hold: "HOLD ON (vocal)", acapella: "ACAPELLA", drum_break: "DRUM BREAK",
                        bass_out: "BASS OUT", synth_hold: "SYNTH HOLD" };
  function remix(d, lineT, kind, len = 16, why) {
    if (!d || !d.stemsReady) return false;
    const rate = (d._playbackRate && d._playbackRate()) || 1, barS = 240 / (d.bpm || 128);
    const at = (b) => audioAt(d, lineT + b * barS);
    // stem mode from the section start (inaudible switch), full mix again after
    book(d, at(0), {}, 0.01);
    for (const e of remixEvents(kind, len)) {
      if (e.stems !== undefined) book(d, at(e.bar), e.stems, Math.max(0.005, (e.ramp * barS) / rate));
      if (e.hold) {
        const h = e.hold, T0 = at(e.bar), T1 = at(h.untilBar);
        timers[d.id].push(setTimeout(() => { if (d.playing) d.holdStem(h.stem, lineT + h.fromBar * barS, h.bars, T0, T1); },
          Math.max(0, (T0 - audioCtx.currentTime) * 1000 - 250)));
      }
    }
    book(d, at(len) + 0.03, null, 0.02);
    note(d.id, `REMIX · ${REMIX_LABEL[kind] || kind}`, why || `bars ${len * 0.75}-${len} of this ${len}-bar section, back on the line`);
    return true;
  }

  // Remix inside a vocal-clip mashup on host deck d: drums + bass out for the
  // last quarter (the guest vocal over the host's synths), back on the line.
  function mashupBreak(d, entryT, bars, hostMuted) {
    if (!d || !d.stemsReady || bars < 8) return false;
    const barS = 240 / (d.bpm || 128), rate = (d._playbackRate && d._playbackRate()) || 1;
    const q = entryT + bars * 0.75 * barS, e = entryT + bars * barS;
    const keep = hostMuted ? { vocals: 0 } : {};
    book(d, audioAt(d, q), { ...keep, drums: 0, bass: 0 }, barS / 4 / rate);
    book(d, audioAt(d, e) - 0.01, { ...keep, drums: 1, bass: 1 }, 0.01);
    note(d.id, "REMIX · MASHUP BREAK", `drums + bass out for the last ${bars / 4} bars of the mashup, back on the line`);
    return true;
  }

  // Run a stem bridge: A's bar 0 at audio time t0; B starts at its track time
  // bFrom (4 B-bars before its entry line) at native tempo. Returns seconds.
  function stemBridge(outId, innId, t0, bEntryTrack, why) {
    const out = root.decks[outId], inn = root.decks[innId];
    if (!out || !inn || !out.stemsReady || !inn.stems) return 0;
    cancel(outId); cancel(innId);
    const rA = (out._playbackRate && out._playbackRate()) || 1;
    const barA = 240 / (out.bpm || 128) / rA, barB = 240 / (inn.bpm || 128);
    const ka = out.analysis && out.analysis.key && out.analysis.key.camelot, kb = inn.analysis && inn.analysis.key && inn.analysis.key.camelot;
    const cs = root.djMind && root.djMind.core && root.djMind.core.camelotScore;
    const keyClash = !!(cs && ka && kb && cs(ka, kb) < 0.8);
    const pA = out._positionAt ? out._positionAt(t0) : out._currentPosition();
    const aSings = vocalShare(out.analysis && out.analysis.vocal_active_regions, pA, pA + 4 * barA * rA) >= 0.3;
    const plan = stemBridgePlan(barA, barB, keyClash, aSings);
    const bFrom = Math.max(0, bEntryTrack - 4 * barB);
    for (const e of plan.events) {
      const d = e.deck === "out" ? out : inn, at = t0 + e.t;
      if (e.start) {
        timers[innId].push(setTimeout(() => {
          inn.setPitchPercent(0);                              // B at its own tempo: no beat ever overlaps
          inn.play(bFrom, false, at);
          setTimeout(() => inn.stemMix(e.stems, at - 0.005, 0.005), 150);
        }, Math.max(0, (at - audioCtx.currentTime) * 1000 - 600)));
      } else if (e.hold) {
        timers[outId].push(setTimeout(() => { if (out.playing) out.holdStem(e.hold.stem, pA + e.hold.fromBar * barA * rA, e.hold.bars, at, t0 + e.until); },
          Math.max(0, (at - audioCtx.currentTime) * 1000 - 250)));
      } else book(d, at, e.stems, Math.max(0.005, e.ramp));
    }
    root.dispatchEvent(new CustomEvent("ai-cue", { detail: { at: t0 + plan.bEntry, kind: "drop", deck: innId, bar: barB,
      why: "B's beat lands after the stem bridge" } }));
    note(outId, `STEM BRIDGE ${outId.toUpperCase()} → ${innId.toUpperCase()}`, why ||
      `any tempo: strip A, ${aSings ? "hold its voice, " : ""}B's pads in beatless, B's beat drops on its own line`);
    return plan.total;
  }

  root.stemMoves = { core, breakdown, handoff, instrumental, reset, audioAt, vocalShare, stemBlend, remix, REMIX_LABEL, mashupBreak, stemBridge };

  // ------------------------------------------------------ stem rail UI --
  // Per deck, under the loop rail: separation status + one toggle per stem.
  // Lit = playing, dim = muted, gold flash = the AI moved it.
  const LABEL = { drums: "DRUMS", bass: "BASS", vocals: "VOX", other: "SYNTH" };
  let userClick = false;
  function buildRail(id) {
    const panel = document.getElementById(`deck-${id}`);
    const loopRail = panel && panel.querySelector(".rail");
    if (!loopRail || panel.querySelector(".stem-rail")) return;
    const rail = document.createElement("div");
    rail.className = "rail stem-rail";
    rail.innerHTML = `<span class="hud-label">STEMS</span><span class="stem-status" id="stem-status-${id}">—</span>` +
      ["drums", "bass", "vocals", "other"].map((n) =>
        `<button class="hw-btn stem-btn" data-deck="${id}" data-stem="${n}" disabled title="Mute / unmute the ${LABEL[n].toLowerCase()} stem">` +
        `<span class="stem-name">${LABEL[n]}</span><canvas class="stem-scope" data-deck="${id}" data-stem="${n}" width="120" height="28"></canvas></button>`).join("") +
      `<button class="hw-btn stem-btn stem-all" data-deck="${id}" data-stem="all" disabled title="Back to the full mix">MIX</button>`;
    loopRail.after(rail);
    rail.addEventListener("click", (e) => {
      const b = e.target.closest(".stem-btn");
      const d = root.decks && root.decks[id];
      if (!b || !d || !e.isTrusted) return;
      userClick = true;
      try {
        if (b.dataset.stem === "all") d.stemMix(null);
        else {
          const cur = d.stemState ? d.stemState[b.dataset.stem] : 1;
          d.stemMix({ [b.dataset.stem]: cur > 0.5 ? 0 : 1 }, 0, 0.03);
        }
      } finally { userClick = false; }
    });
  }
  function paint(id, flash) {
    const d = root.decks && root.decks[id];
    const st = document.getElementById(`stem-status-${id}`);
    if (!d || !st) return;
    const ready = d.stemsReady, has = !!d.stems;
    st.textContent = d._meterGain ? "RIFF · KEY-LOCKED" : ready ? (d.stemState ? "LIVE STEMS" : "READY · full mix") : has ? "LOADED" : d.buffer ? "SEPARATING…" : "—";
    st.className = `stem-status${ready || d._meterGain ? " stem-ready" : d.buffer && !has ? " stem-wait" : ""}`;
    document.querySelectorAll(`.stem-btn[data-deck="${id}"]`).forEach((b) => {
      b.disabled = !ready;
      const n = b.dataset.stem;
      const v = n === "all" ? (d.stemState ? 0 : 1) : d.stemState ? d.stemState[n] : 1;
      b.classList.toggle("stem-on", v > 0.5);
      b.classList.toggle("stem-part", v > 0.05 && v <= 0.5);
      if (n === "vocals") b.classList.toggle("stem-bus", !!(d.stemState && d.stemState.bus > 0.5));
      if (flash && n !== "all") { b.classList.remove("stem-ai"); void b.offsetWidth; b.classList.add("stem-ai"); }
    });
  }
  ["a", "b"].forEach(buildRail);

  // ---- mini players: a scrolling level trace per stem ------------------
  // Pre-fader level (what the stem is playing), drawn bright in proportion to
  // what the crowd hears of it; a muted stem keeps scrolling, dim.
  const COLS = 60, hist = {}, scopes = [];
  const buf = new Float32Array(512);
  document.querySelectorAll(".stem-scope").forEach((c) => {
    const key = `${c.dataset.deck}:${c.dataset.stem}`;
    hist[key] = new Float32Array(COLS);
    scopes.push({ c, ctx: c.getContext("2d"), key, deck: c.dataset.deck, stem: c.dataset.stem });
  });
  function accent(deck) {
    return getComputedStyle(document.getElementById(`deck-${deck}`)).getPropertyValue("--accent").trim() || "#0f6";
  }
  const colour = { a: null, b: null };
  let last = 0, frame = 0;
  function drawScopes(t) {
    requestAnimationFrame(drawScopes);
    if (t - last < 33) return;                   // ~30 fps
    last = t;
    if (!colour.a) { colour.a = accent("a"); colour.b = accent("b"); }
    frame++;
    for (const s of scopes) {
      const d = root.decks && root.decks[s.deck];
      const live = d && d.playing !== undefined && (d.stemsReady || d._meterGain);
      const h = hist[s.key];
      h.copyWithin(0, 1);
      let g = 0;
      if (live) {
        const r = d.meterRead(s.stem, buf);
        h[COLS - 1] = Math.min(1, r.level * 4);
        g = Math.max(0, Math.min(1, r.gain));
      } else h[COLS - 1] = 0;
      const { ctx, c } = s, W = c.width, H = c.height, bw = W / COLS;
      ctx.clearRect(0, 0, W, H);
      ctx.fillStyle = colour[s.deck];
      ctx.globalAlpha = live ? 0.18 + 0.82 * g : 0.08;
      for (let i = 0; i < COLS; i++) {
        const v = h[i] * (H - 2);
        ctx.fillRect(i * bw, (H - v) / 2, Math.max(1, bw - 1), Math.max(1, v));
      }
      ctx.globalAlpha = 1;
    }
  }
  requestAnimationFrame(drawScopes);
  root.addEventListener("ai-activity", (e) => {
    const d = e.detail || {};
    if (d.kind === "stems") paint(d.deck, !userClick);
  });
  setInterval(() => { paint("a"); paint("b"); }, 1000);
})(typeof window !== "undefined" ? window : globalThis);
