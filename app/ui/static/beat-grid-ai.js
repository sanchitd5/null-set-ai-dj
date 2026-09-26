// AI Music Brain — the autopilot programs and plays the BEAT GRID.
//
// Per song (while the autopilot runs and AI BEATS is on):
//   1. pattern from the song's genre / tempo (house tops, half-time for trap /
//      hip-hop / hyperpop, DnB, breaks, bhangra dhol, dembow);
//   2. the KICK lane muted (the record already has one; single-bass rule,
//      [[EQ & Frequency Management]]) and SUB SAFE on, low level;
//   3. played on the song's first drop for 8 bars (a drop = 8-bar phrase in
//      the song's top energy quartile that jumps over the phrase before it,
//      same idea as dj-mind.js dropLines), once per song, never in the first
//      16 bars or the last 8 bars before the booked transition.
// Restraint is deliberate (user: "shouldn't overdo it"). If the user presses
// RUN on the grid, it is theirs: the AI never starts or stops a user run.
//
// Depends on: window.beatGrid (sampler-deck.js), window.autopilotState
// (autopilot.js), window.decks (deck-controller.js).

(function () {
  const grid = window.beatGrid;
  if (!grid || !grid.runAs) return;

  const TICK_MS = 200;
  const PHRASE_BARS = 8;
  const RUN_BARS = 8;
  const MIN_BARS_ON_TRACK = 16;
  const EXIT_GUARD_BARS = 8;
  const DROP_JUMP = 0.15;
  const LEVEL = 0.3;

  const GENRE_PRESETS = [
    [/drum.?(and|&|n).?bass|\bdnb\b|jungle|liquid/i, "dnb"],
    [/bhangra|punjabi|dhol|desi/i, "bhangra"],
    [/reggaeton|dembow|latin|dancehall|moombah/i, "dembow"],
    [/trap|hip.?hop|\brap\b|hyperpop|dubstep|drill|halftime|r&b|rnb/i, "halftime"],
    [/breaks|breakbeat|2.?step|uk garage|ukg|electro/i, "breaks"],
    [/house|techno|melodic|edm|disco|dance|progressive|trance|afro/i, "house"],
  ];

  function presetFor(genre, bpm) {
    for (const [re, name] of GENRE_PRESETS) if (re.test(genre || "")) return name;
    if (bpm >= 160) return "dnb";
    if (bpm && bpm < 105) return "halftime";
    return "tops";
  }

  function toggleOn() {
    const el = document.getElementById("ap-grid-toggle");
    return !el || el.checked;
  }

  function phraseEnergy(a, t0, t1) {
    const tt = a.energy_times || [], cv = a.energy_curve || [];
    let sum = 0, n = 0;
    for (let i = 0; i < tt.length; i++) if (tt[i] >= t0 && tt[i] < t1) { sum += cv[i]; n++; }
    return n ? sum / n : null;
  }

  // First drop line in [from, to): top-quartile phrase energy that jumps >= DROP_JUMP.
  function firstDrop(a, bar, from, to) {
    const ph = a.phrase_boundaries_8bar || [];
    const L = PHRASE_BARS * bar;
    const es = ph.map((p) => phraseEnergy(a, p, p + L));
    const known = es.filter((e) => e != null).sort((x, y) => x - y);
    if (known.length < 3) return null;
    const q3 = known[Math.floor(0.75 * (known.length - 1))];
    for (let i = 1; i < ph.length; i++) {
      if (ph[i] < from || ph[i] >= to) continue;
      if (es[i] != null && es[i - 1] != null && es[i] >= q3 && es[i] - es[i - 1] >= DROP_JUMP) return ph[i];
    }
    return null;
  }

  let songKey = null;   // `${deck}:${trackId}` the plan belongs to
  let plan = null;      // { preset, start, end, done, running }

  function newPlan(st, d) {
    const a = d.analysis || {};
    const bar = 240 / (d.bpm || 128);
    const from = (st.entryPos || 0) + MIN_BARS_ON_TRACK * bar;
    const to = st.fireAt != null ? st.fireAt - (EXIT_GUARD_BARS + RUN_BARS) * bar : (d.buffer ? d.buffer.duration : 1e9);
    const start = firstDrop(a, bar, from, to);
    return { preset: presetFor(st.genre, d.bpm), start, end: start == null ? null : start + RUN_BARS * bar,
             done: start == null, running: false, bar };
  }

  function stopAi(msg) {
    if (plan && plan.running) {
      grid.stopAs("ai");
      plan.running = false;
      if (msg) grid.status(msg);
    }
  }

  setInterval(() => {
    const st = window.autopilotState;
    if (!st || !st.active || !toggleOn()) { stopAi(); return; }
    if (grid.owner() === "user") return; // the user's grid: hands off
    const d = window.decks && window.decks[st.activeDeck];
    if (!d || !d.playing || !d.analysis) return;
    const key = `${st.activeDeck}:${st.trackId}`;
    if (key !== songKey) {             // new song: stop any AI run, plan again
      stopAi();
      songKey = key;
      plan = newPlan(st, d);
    }
    // the booked exit can arrive after the plan was made: re-check the guard
    if (plan && !plan.done && !plan.running && st.fireAt != null &&
        plan.start > st.fireAt - (EXIT_GUARD_BARS + RUN_BARS) * plan.bar) plan.done = true;
    if (!plan || plan.done) return;
    const pos = d._currentPosition();
    if (!plan.running && pos >= plan.start - 0.3 && pos < plan.start + plan.bar) {
      grid.loadPreset(plan.preset);
      grid.setMute(grid.PAD.KICK, true);
      grid.setSubSafe(true);
      grid.setLevel(LEVEL);
      if (grid.runAs("ai")) {
        plan.running = true;
        grid.status(`AI: ${plan.preset.toUpperCase()} pattern on the drop, ${RUN_BARS} bars (kick muted, sub safe)`);
      } else plan.done = true;
    } else if (plan.running && pos >= plan.end - 0.05) {
      stopAi(`AI: pattern done - back to the record`);
      plan.done = true;
    } else if (!plan.running && pos >= plan.start + plan.bar) {
      plan.done = true; // missed the line (seek / loop): skip this song
    }
  }, TICK_MS);
})();
