// AI Music Brain — live beat layer ("drum machine synced to the decks").
//
// Fred again.. plays a drum machine locked to the decks through his live sets:
// hats and claps riding on top of the record, snare rolls into drops, fills
// into every transition, drums carrying on through breakdowns. This module
// does the same with the synthesized sampler pads (performance.js), scheduled
// against the followed deck's analysed beatgrid (beat_times / downbeat_times /
// sections from /api/tracks/{id}/analysis).
//
// Rules (grounded in ./DJ/ notes):
//  - [[EQ & Frequency Management]]: never stack a second sub-bass. The KICK
//    pad (45 Hz tail) only plays where the record itself has no drop/verse
//    kick, i.e. in the last bars of a breakdown, driving back into the drop.
//  - [[Phrasing & Structure]]: pattern changes land on bar lines; fills take
//    the last bar before a section change or a transition.
//  - Layer sits under the record: its own bus, default 0.25, scaled by the
//    section's measured energy so it never shouts over quiet parts.
//
// Scheduling: a 25 ms JS timer looks 120 ms ahead in audioCtx time and books
// every 16th-note step inside that window with a sample-accurate start time,
// so hits stay locked even when the main thread is busy.
//
// Depends on globals: audioCtx, masterGain, decks (deck-controller.js),
// triggerPad (performance.js). Public API: window.beatLayer.

(function () {
  if (typeof audioCtx === "undefined" || typeof triggerPad !== "function") return;

  const PAD = { KICK: 0, SNARE: 1, CLAP: 2, HAT: 3, OPEN_HAT: 4, TOM: 5, ZAP: 6, SWEEP: 7 };
  const LOOKAHEAD_S = 0.12;
  const TICK_MS = 25;

  const bus = audioCtx.createGain();
  bus.gain.value = 0.25;
  bus.connect(masterGain);

  let enabled = true;
  let deckId = null;       // deck whose beatgrid we follow
  let timer = null;
  let lastTrackT = null;   // track time up to which steps are already booked
  let fillUntil = -1;      // track time: transition fill active until here
  let boostUntil = -1;                 // track time: peak BEAT BOOST active until here (dj-mind.js)

  // 16-step patterns (one bar of 16ths). 1 = hit.
  const P = {
    offHat:   [0,0,1,0, 0,0,1,0, 0,0,1,0, 0,0,1,0],
    backbeat: [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0],
    four:     [1,0,0,0, 1,0,0,0, 1,0,0,0, 1,0,0,0],
    roll8:    [1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0],
    roll16:   [1,1,1,1, 1,1,1,1, 1,1,1,1, 1,1,1,1],
    tomFill:  [0,0,0,0, 0,0,0,0, 1,0,1,0, 1,1,1,1],
  };

  function deck() { return deckId && window.decks ? window.decks[deckId] : null; }

  // Beat grid for the followed deck: analysed beats, else a flat BPM grid.
  function grid(d) {
    const a = d.analysis;
    if (a && a.beat_times && a.beat_times.length > 8) {
      if (!a._beatLayerBar) {
        const down = (a.downbeat_times || []).map((t) => t.toFixed(3));
        const downSet = new Set(down);
        let k = 0;
        a._beatLayerBar = a.beat_times.map((t) => {
          if (downSet.has(t.toFixed(3))) k = 0; else k = (k + 1) % 4;
          return k;
        });
      }
      return { beats: a.beat_times, beatInBar: a._beatLayerBar };
    }
    return null;
  }

  // The analyzer labels sections on a 1 s energy grid, so a song comes back as
  // dozens of 1-3 s "build"/"breakdown" slivers. Acting on those made the layer
  // snare-roll every few seconds. Merge same-label neighbours and only trust
  // runs of at least MIN_SECTION_BARS; everything else counts as nothing.
  const MIN_SECTION_BARS = 8;
  const ROLL_COOLDOWN_BARS = 32;
  let lastRollAt = -Infinity; // track time of the last build/breakdown roll

  function longSections(d) {
    const a = d.analysis;
    if (!a) return [];
    const bar = (60 / (d.bpm || 128)) * 4;
    if (a._beatLayerLong && a._beatLayerLongBar === bar) return a._beatLayerLong;
    const merged = [];
    for (const s of a.sections || []) {
      const last = merged[merged.length - 1];
      if (last && last.label === s.label && Math.abs(last.end - s.start) < 0.01) {
        last.energy = (last.energy * (last.end - last.start) + s.energy * (s.end - s.start)) / (s.end - last.start);
        last.end = s.end;
      } else merged.push({ ...s });
    }
    a._beatLayerLong = merged.filter((s) => s.end - s.start >= MIN_SECTION_BARS * bar);
    a._beatLayerLongBar = bar;
    return a._beatLayerLong;
  }

  function sectionAt(d, t) {
    for (const s of longSections(d)) if (t >= s.start && t < s.end) return s;
    return null;
  }

  // Decide which pads fire on one 16th step. Returns [{pad, vel}].
  //
  // RESTRAINT: the record is the star. The layer stays silent most of the
  // time and only speaks at moments that earn it:
  //   - fill into a transition (2 bars)
  //   - last bar of a real (>= 8 bar) build: one light roll, max 1 per 32 bars
  //   - last 4 bars of a real breakdown (kick drives back in)
  //   - one 8-bar phrase in four during verses: light hats + claps
  // Intros, outros and drops are left alone (the record already carries them),
  // except one DJ-mind BEAT BOOST phrase in a peak drop: open hats + claps
  // ([[Fred again.. Case Study]], live drums; no snares - "too many snares").
  function hitsFor(d, t, step, barLen, phraseIdx) {
    const hits = [];
    const add = (pad, vel) => hits.push({ pad, vel });
    const sec = sectionAt(d, t);
    const label = sec ? sec.label : "none";
    const rollOk = t - lastRollAt >= ROLL_COOLDOWN_BARS * barLen;
    const barsLeft = sec ? (sec.end - t) / barLen : 99;
    const energy = sec && typeof sec.energy === "number" ? sec.energy : 0.6;

    // Transition fill: one light bar, then a short tom run + sweep.
    if (t < fillUntil) {
      const left = (fillUntil - t) / barLen;
      if (left <= 1) {
        if (P.tomFill[step]) add(PAD.TOM, 0.6);
        if (step === 0) add(PAD.SWEEP, 0.45);
      } else if (P.backbeat[step]) add(PAD.SNARE, 0.3);
      return { hits, energy };
    }

    switch (label) {
      case "verse":
      case "chorus":
        if (phraseIdx % 4 !== 2) break; // 1 phrase in 4 only
        if (P.offHat[step]) add(PAD.HAT, 0.45);
        if (P.backbeat[step]) add(PAD.CLAP, 0.35);
        break;
      case "build":
        if (barsLeft > 1 || !rollOk) break;
        if (P.roll8[step]) add(PAD.SNARE, 0.45);
        if (step === 0) add(PAD.SWEEP, 0.5);
        if (step === 15) lastRollAt = t;
        break;
      case "breakdown":
        // record's own kick is out here, so a kick adds no sub clash
        if (barsLeft <= 4 && P.four[step]) add(PAD.KICK, 0.4);
        if (barsLeft <= 1 && rollOk && P.backbeat[step]) add(PAD.SNARE, 0.35);
        if (barsLeft <= 1 && step === 15) lastRollAt = t;
        break;
      case "drop":
        if (t >= boostUntil) break;
        if (P.offHat[step]) add(PAD.OPEN_HAT, 0.4);
        if (P.backbeat[step]) add(PAD.CLAP, 0.4);
        break;
      default:
        break; // intro / outro: silence
    }
    return { hits, energy };
  }

  function schedule() {
    const d = deck();
    if (!enabled || !d || !d.playing || d.reversed || d._braking) { lastTrackT = null; return; }
    const g = grid(d);
    if (!g) return;
    const rate = d._playbackRate();
    if (!(rate > 0.2)) return;
    const now = audioCtx.currentTime;
    const pos = d._currentPosition();
    const horizon = pos + LOOKAHEAD_S * rate;
    // First tick, seek, loop jump or beatjump: resync, never burst-fire.
    if (lastTrackT === null || pos < lastTrackT - LOOKAHEAD_S * rate - 0.05 || pos - lastTrackT > 1) {
      lastTrackT = pos;
    }
    const { beats, beatInBar } = g;
    // binary search first beat <= lastTrackT
    let lo = 0, hi = beats.length - 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (beats[m] <= lastTrackT) lo = m; else hi = m - 1; }
    for (let i = lo; i < beats.length - 1 && beats[i] <= horizon; i++) {
      const b0 = beats[i], b1 = beats[i + 1];
      const beatLen = b1 - b0;
      if (beatLen <= 0 || beatLen > 2) continue;
      const barLen = beatLen * 4;
      for (let s = 0; s < 4; s++) {
        const t = b0 + (beatLen * s) / 4;
        if (t <= lastTrackT || t > horizon) continue;
        const step = beatInBar[i] * 4 + s;
        const when = now + (t - pos) / rate;
        const phraseIdx = Math.floor(i / 32); // 8 bars = 32 beats
        const { hits, energy } = hitsFor(d, t, step, barLen, phraseIdx);
        const lvl = 0.5 + 0.5 * Math.min(1, Math.max(0, energy));
        for (const h of hits) {
          const v = audioCtx.createGain();
          v.gain.value = h.vel * lvl;
          v.connect(bus);
          triggerPad(h.pad, when, v);
          setTimeout(() => v.disconnect(), (when - now) * 1000 + 1500);
        }
      }
    }
    lastTrackT = horizon;
  }

  function follow(id) {
    deckId = id;
    lastTrackT = null;
    fillUntil = -1;
    boostUntil = -1;
    lastRollAt = -Infinity;
    if (!timer) timer = setInterval(schedule, TICK_MS);
  }

  function stop() {
    deckId = null;
    lastTrackT = null;
    if (timer) { clearInterval(timer); timer = null; }
  }

  // Fill across the next `bars` bars of the followed deck (called by the
  // autopilot just before a crossfade fires so drums glue the two records).
  function fill(bars = 2) {
    const d = deck();
    if (!d) return;
    const barLen = (60 / (d.bpm || 128)) * 4;
    fillUntil = d._currentPosition() + bars * barLen;
  }

  // Heavier pattern inside a drop until track time `t` (one 8-bar phrase).
  function boostUntil_(t) { boostUntil = Number.isFinite(t) ? t : -1; }

  function setEnabled(on) { enabled = !!on; if (!on) lastTrackT = null; }
  function setLevel(v) { bus.gain.setTargetAtTime(Math.max(0, Math.min(1, v)), audioCtx.currentTime, 0.05); }

  window.beatLayer = { follow, stop, fill, setEnabled, setLevel, boostUntil: boostUntil_,
                       isEnabled: () => enabled, get deck() { return deckId; } };

  // UI: LIVE DRUMS toggle + level in the autopilot panel.
  const toggle = document.getElementById("ap-drums-toggle");
  const level = document.getElementById("ap-drums-level");
  if (toggle) toggle.addEventListener("change", () => setEnabled(toggle.checked));
  if (level) level.addEventListener("input", () => setLevel(parseFloat(level.value)));
})();
