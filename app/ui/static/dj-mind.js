// AI Music Brain - DJ mind ("a mind like Fred again.. plays, not his set")
//
// A small decision layer that sits on top of the autopilot. Once per 8-bar
// phrase of the playing deck it reads the musical state and picks ONE move.
// The rules are the general ones pulled from the set study
// (research/notes/set-study-gfF8jzBVWvM.md, section 2); nothing here knows any
// particular song or tracklist.
//
//   hold      (study section 4.1, "segments are musical phrases, not clock
//              lengths") - a build is running into the planned exit: push the
//              exit back one phrase so the build resolves on the old record.
//   preclear  (rule 3) - pull the outgoing sub down ~12 dB some 8-16 bars
//              before the audible transition, so the bass is already moving
//              when the new record arrives.
//   instant   (rule 1) - both records meet on a drop: skip the pre-clear, the
//              bass changes owner on one downbeat inside executeTransition.
//   subdrop   (rule 4) - vocal-forward stretch: kill the sub for 10-20 s, then
//              a sharp full-band return on the phrase line.
//   layer     (rule 2) - the mashup layer is riding the next record's vocal.
//   ride      - everything else. Most phrases are this: the record is the star.
//
// Restraint (rule 8 + the user's "too many snares" complaint): at most one
// sub drop per track, never on two tracks in a row, and none within 4 minutes
// of the previous mind move. Drum fills before a crossfade are rationed too
// (fxAllowed). Energy dip (rule 9) and callback (rule 7) are hints passed to
// the LLM track picker through energyNote(), not audio moves.
//
// Second study (research/notes/set-study-mDtud5fLgFQ.md, Latin house/pop,
// one DJ, ~69 min):
//   reprise   (mDtud5fLgFQ sections 4.3, 5 and 6 "Callback device") - a hook
//              the set has already returned to ("Quiereme" at 19:10, 20:50,
//              59:03, same band-energy shape each time) is a recurring motif,
//              not a bookend. Once a song has 2+ plays in the history it may
//              come back, as another version, up to 3 plays in all, spaced
//              MOTIF_GAP_SONGS apart. A set that threads a motif uses it
//              INSTEAD of the rule 7 open/close callback. Picker hint only.
//   The same study corroborates rule 3 (mDtud5fLgFQ section 4.1: the sub is
//   already falling before nearly every marker) and rule 4 (section 4.2: sub
//   held down under the ambient 28:32 / 36:02 records); no change needed.
//
// Rules from CLAUDE.md honoured here: moves start on 8-bar phrase lines; the
// sub drop only ever REMOVES low end, so two tracks never share sub-bass.
//
// Depends on (at runtime only): window.decks (deck-controller.js) and the
// .eq-knob inputs. The pure core (decide & helpers) runs under node for tests.

(function (root) {
  "use strict";

  // -- tunables (evidence from the study is small-n: treat as starting points) --
  const PHRASE_BARS = 8;
  const MIN_SECTION_BARS = 8;          // same sliver filter as beat-layer.js
  const PRECLEAR_DB = -12;             // study: sub dips 10-20 dB before the swap
  const PRECLEAR_RAMP_BARS = 4;
  const PRECLEAR_SLACK_BARS = 4;       // phrase lines rarely hit the exit exactly
  const SUBDROP_MIN_S = 10, SUBDROP_MAX_S = 20;  // rule 4: 10-20 s stretch
  const SUBDROP_COOLDOWN_S = 240;      // set-wide gap between mind moves
  const SUBDROP_VOCAL_SHARE = 0.6;     // vocal must cover most of the next phrase
  const SUBDROP_MAX_ENERGY = 0.75;     // not on a full-energy drop
  const MIN_BARS_ON_TRACK = 16;        // let a record settle before touching it
  const EXIT_GUARD_BARS = 24;          // no sub drop close to a transition
  const HOLD_BARS = 8;
  const LOW_KILL = -26;
  const PEAK_ENERGY = 8;               // LLM 1-10 read; rule 9 dip hint
  const CALLBACK_SET_POS = 0.8;        // rule 7: late in the set
  // Repeated-hook motif (research/notes/set-study-mDtud5fLgFQ.md section 5).
  const MOTIF_MIN_PLAYS = 2;           // a title heard twice is the set's hook, not a one-off
  const MOTIF_MAX_PLAYS = 3;           // "Quiereme x3": the most any hook returned in that set
  // "Quiereme x2" (20:50) -> "x3" (59:03): 7 songs between. Same number as
  // REMIX_REPLAY_GAP in app/ui/autopilot_service.py, so the picker's repeat
  // filter lets the reprise through instead of dropping it.
  const MOTIF_GAP_SONGS = 8;
  // Remix moves (the song is re-edited live, not played as is). Restraint
  // (rule 8, "too many snares"): a per-song cap, never two phrases in a row,
  // each kind once per song, none in the first 16 bars or last 24 before exit.
  const REMIX_MAX_PER_SONG = 4;        // AI plans may go up to this
  const REMIX_RULE_MAX = 3;            // rules alone stop earlier
  const REMIX_GAP_PHRASES = 2;         // remix phrases at least 2 apart
  const BEAT_LAYER_MIN_SCORE = 65;
  const FILTER_MID_DB = -18;           // filter build: mids swept this far out
  const SEAM_VOCAL_GAP_S = 0.25;      // a loop seam this close inside a vocal region cuts it
  const HOLD_SEAM_PASS = 0.6;         // silent pre-check: worst seam score a hold loop may have
  const HOLD_LOOP_LEAD_S = 24;         // safety loop when the end is this close
  const REMIX_MOVES = ["loop_extend", "beat_jump", "stutter", "filter_build", "echo_freeze", "beat_layer", "peak_roll"];
  const DROP_LEADINS = ["stutter", "filter_build", "echo_freeze"];  // tension -> release

  // PEAK mode: bold moves on high-energy songs ("heavy beat mixing to surprise
  // everyone"), rare enough to hit hard. Grounding:
  //   ./DJ/05 [[Double Drop]] (one bass, key-safe, "not more than 4-5 a set"),
  //   [[Drop Swap]] (cut on the drop downbeat; never into a weaker drop),
  //   [[Backspin (Spinback)]] ("2-3 times in a 2-hour set"), [[Loop Roll]] /
  //   [[Stutter Transition]] (roll released on beat 1), [[Build-to-Drop Transition]];
  //   ./DJ/13 [[Skrillex Case Study]] (1-beat silence before impact, slam cut),
  //   [[Martin Garrix Case Study]] (fader cut before the drop, the vocal carries),
  //   [[Fred again.. Case Study]] (live drums over the record);
  //   ./DJ/06 [[Energy Management & Dynamics]] (contrast: constant peak numbs);
  //   ./DJ/09 Dubstep / Trap / DnB / EDM playbooks (drops are the event).
  // Restraint: <= 1 BIG moment per song, never the same kind on two songs in a
  // row, none in the first 16 bars, <= 2 in any 3 songs, 4 min apart.
  const PEAK_PROFILE_ENERGY = 8;       // LLM current_profile energy (1-10)
  const PEAK_QUARTILE = 0.75;          // quick mode: section energy in the top quartile
  const BIG_MOMENTS = ["double_drop", "fakeout", "drop_swap"];
  const BIG_MIN_BARS = 16;
  const BIG_WINDOW_SONGS = 3, BIG_MAX_PER_WINDOW = 2;
  const BIG_COOLDOWN_S = 240;
  const PEAK_KEY_MIN = 0.8;            // Double Drop: same / adjacent / relative key
  const DOUBLE_DROP_BARS = 8;
  const DOUBLE_DROP_MAX_VOCAL = 0.3;   // one record carries the vocal, not both
  const BACKSPIN_MAX = 2;              // per set, never on two drop swaps running
  const BEAT_BOOST_BARS = 8;
  const BEAT_BOOST_COOLDOWN_S = 300;
  const FAKEOUT_VOCAL_SHARE = 0.5;     // vocal in the last bar -> 1 bar vocal-only, else 1 beat silence
  const PEAK_MOVES = ["fakeout", "peak_roll", "beat_boost"];   // in-song; the LLM may plan these
  // Drop line: labels flicker (1-3 s slivers), so a drop is found by energy:
  // the phrase is in the song's top quartile and jumps >= DROP_JUMP over the
  // phrase before. Same rule as drop_lines() in app/music_brain/blend.py.
  const DROP_JUMP = 0.2;

  // ---------------------------------------------------------------- pure core
  function mergeSections(sections, barSecs) {
    const merged = [];
    for (const s of sections || []) {
      const last = merged[merged.length - 1];
      if (last && last.label === s.label && Math.abs(last.end - s.start) < 0.01) {
        const la = last.end - last.start, sa = s.end - s.start;
        last.energy = (last.energy * la + (s.energy || 0) * sa) / Math.max(1e-6, la + sa);
        last.end = s.end;
      } else {
        merged.push({ label: s.label, start: s.start, end: s.end, energy: s.energy || 0 });
      }
    }
    return merged.filter((s) => s.end - s.start >= MIN_SECTION_BARS * barSecs);
  }

  function sectionAt(longSecs, t) {
    for (const s of longSecs) if (t >= s.start && t < s.end) return s;
    return null;
  }

  // Phrase index of `pos`: every 8 analysed downbeats, else a flat BPM grid.
  function phraseAt(downbeats, pos, barSecs) {
    if (downbeats && downbeats.length > PHRASE_BARS) {
      let lo = 0, hi = downbeats.length;       // count of downbeats <= pos
      while (lo < hi) { const m = (lo + hi) >> 1; if (downbeats[m] <= pos) lo = m + 1; else hi = m; }
      return lo === 0 ? -1 : Math.floor((lo - 1) / PHRASE_BARS);
    }
    return Math.floor(pos / (barSecs * PHRASE_BARS));
  }

  function vocalShare(regions, t0, t1) {
    if (!(t1 > t0)) return 0;
    let covered = 0;
    for (const r of regions || []) {
      const a = Math.max(t0, r[0]), b = Math.min(t1, r[1]);
      if (b > a) covered += b - a;
    }
    return Math.min(1, covered / (t1 - t0));
  }

  // Bars the sub drop lasts: whole bars, within 10-20 s, 8 when it fits.
  function subdropBars(barSecs) {
    if (PHRASE_BARS * barSecs <= SUBDROP_MAX_S) return PHRASE_BARS;
    const half = PHRASE_BARS / 2;
    return half * barSecs >= SUBDROP_MIN_S * 0.8 ? half : 0;
  }

  // Start and end (track seconds) of phrase `idx`: analysed downbeats, else grid.
  function phraseBounds(downbeats, idx, barSecs) {
    const len = PHRASE_BARS * barSecs;
    if (downbeats && downbeats.length > PHRASE_BARS && idx >= 0) {
      const a = downbeats[idx * PHRASE_BARS];
      if (a != null) {
        const b = downbeats[(idx + 1) * PHRASE_BARS];
        return [a, b != null ? b : a + len];
      }
    }
    return [idx * len, (idx + 1) * len];
  }

  // [label owning most of [t0, t1), overlap-weighted energy]. Same as
  // phrase_label() in mind_plan.py: analysed sections are often shorter than
  // a phrase, so the phrase takes the label that covers most of it.
  function phraseLabel(sections, t0, t1) {
    const cover = {};
    let eSum = 0, wSum = 0;
    for (const x of sections || []) {
      const ov = Math.min(t1, x.end) - Math.max(t0, x.start);
      if (ov > 0) { cover[x.label] = (cover[x.label] || 0) + ov; eSum += (x.energy || 0) * ov; wSum += ov; }
    }
    const keys = Object.keys(cover);
    if (!keys.length) return [null, null];
    return [keys.reduce((a, b) => (cover[b] > cover[a] ? b : a)), eSum / wSum];
  }

  const RELEASE_LABELS = ["drop", "chorus"];
  function isPreDrop(label, next) {
    if (RELEASE_LABELS.includes(label) || next == null) return false;
    return RELEASE_LABELS.includes(next) || (label === "build" && next !== "build");
  }

  // Why a remix move may NOT run now (null = allowed). Same caps as
  // app/ui/mind_plan.py validate_plan; the AI plan and the rules both pass here.
  function remixBlock(kind, s, bars) {
    const used = s.remixUsed || [];
    const sect = s.phraseSection != null ? s.phraseSection : s.section;
    if ((s.remixCount || 0) >= REMIX_MAX_PER_SONG) return `remix cap ${REMIX_MAX_PER_SONG} reached`;
    if (used.includes(kind)) return "already used on this song";
    if (kind === "beat_jump" && s.onAir) return "the deck is on the master: no jumps";
    if (s.barsOnTrack < MIN_BARS_ON_TRACK) return `first ${MIN_BARS_ON_TRACK} bars`;
    if (s.barsToExit != null && s.barsToExit < EXIT_GUARD_BARS) return `last ${EXIT_GUARD_BARS} bars before exit`;
    if ((s.phraseIdx || 0) - (s.lastRemixPhrase == null ? -99 : s.lastRemixPhrase) < REMIX_GAP_PHRASES) {
      return "remix on the previous phrase";
    }
    if (s.mashupActive) return "vocal layer running";
    if (DROP_LEADINS.includes(kind) && !s.preDrop) return "no drop after this phrase";
    if (kind === "loop_extend" && (sect === "build" || sect === "outro")) return "no loop on a build/outro";
    if (kind === "beat_jump") {
      if (RELEASE_LABELS.includes(sect) || s.preDrop) return "never jump a drop or its lead-in";
      if ((s.skipHitsDrop || {})[bars]) return "jump would skip a drop";
      if (s.barsToExit != null && s.barsToExit - PHRASE_BARS - bars < EXIT_GUARD_BARS) return "jump lands in the exit guard";
    }
    if (kind === "beat_layer") return "beat layer is booked by the mashup layer";
    return null;
  }

  const REMIX_WHY = {
    loop_extend: (b) => `looping ${b} bars of this groove once more, release on the line`,
    beat_jump: (b) => `weak stretch - jumping ${b} bars ahead to the good part`,
    stutter: () => "loop roll 4 > 2 > 1 > 1/2 beat over the last 2 bars into the drop",
    filter_build: () => "sweeping lows and mids out, snap open on the drop",
    echo_freeze: () => "echo tail on the last beat, cut straight into the drop",
    peak_roll: () => "filter riser + loop roll 1 > 1/2 > 1/4 beat, released on the drop",
  };
  const REMIX_RULE = { loop_extend: "Loops & Beat Jumps", beat_jump: "Loops & Beat Jumps",
                       stutter: "tension/release", filter_build: "Filter Transition",
                       echo_freeze: "Echo Out", peak_roll: "Loop Roll" };

  function remixMove(kind, bars, why, source) {
    return { action: kind, rule: REMIX_RULE[kind] || "", bars, source,
             why: why || REMIX_WHY[kind](bars) };
  }

  // -- PEAK mode ------------------------------------------------------------
  // Camelot compatibility, the SAME table as the matcher (recipe_matcher.camelot_distance_score) and CLAUDE.md
  // section 4: 1 same key, 0.9 one hour, 0.85 relative major/minor, 0.8 two hours up (energy boost), 0.75
  // diagonal (one hour, letter changed), 0.6 two hours down (energy drop), 0.3 two hours with the letter
  // changed, 0 for three hours or more (a clash). Direction matters: a is the song leaving, b the one arriving.
  function camelotScore(a, b) {
    const pa = /^(\d{1,2})([AB])$/i.exec(String(a || "").trim());
    const pb = /^(\d{1,2})([AB])$/i.exec(String(b || "").trim());
    if (!pa || !pb) return 0;
    const up = ((+pb[1] - +pa[1]) % 12 + 12) % 12;              // clockwise hours a -> b
    const d = Math.min(up, 12 - up);
    if (pa[2].toUpperCase() !== pb[2].toUpperCase()) return d === 0 ? 0.85 : d === 1 ? 0.75 : d === 2 ? 0.3 : 0;
    return d === 0 ? 1 : d === 1 ? 0.9 : up === 2 ? 0.8 : up === 10 ? 0.6 : 0;
  }
  // Energy at the top quartile of the song's merged (8+ bar) sections.
  function energyQ3(longSecs) {
    const e = (longSecs || []).map((x) => x.energy).filter(Number.isFinite).sort((a, b) => a - b);
    return e.length ? e[Math.floor(PEAK_QUARTILE * (e.length - 1))] : null;
  }
  // [{t, energy, prevEnergy}] drop lines on the 8-bar phrase grid.
  function dropLines(phrases, times, curve, bar) {
    const ph = phrases || [], tt = times || [], cv = curve || [], L = PHRASE_BARS * bar;
    const es = ph.map((p) => {
      let sum = 0, n = 0;
      for (let i = 0; i < tt.length; i++) if (tt[i] >= p && tt[i] < p + L) { sum += cv[i]; n++; }
      return n ? sum / n : null;
    });
    const known = es.filter((e) => e != null).sort((a, b) => a - b);
    if (known.length < 3) return [];
    const q3 = known[Math.floor(PEAK_QUARTILE * (known.length - 1))];
    const out = [];
    for (let i = 1; i < ph.length; i++) {
      const e = es[i], pe = es[i - 1];
      if (e != null && pe != null && e >= q3 && e - pe >= DROP_JUMP - 1e-9) out.push({ t: ph[i], energy: e, prevEnergy: pe });
    }
    return out;
  }
  // The playing song is at peak: LLM energy >= 8, or quick mode in a top-
  // quartile section, or a drop now / on the next phrase line.
  function isPeak(s) {
    if (Number.isFinite(s.profileEnergy) && s.profileEnergy >= PEAK_PROFILE_ENERGY) return true;
    if (s.setMode === "quick" && s.energyQ3 != null && s.sectionEnergy >= s.energyQ3) return true;
    return !!(s.inDrop || s.nextDrop || s.section === "drop" || s.nextSection === "drop");
  }
  // Why a BIG moment (double drop / fake-out / drop swap) may NOT happen on song
  // `idx` now. `log` = [{track, kind, at}] of earlier big moments (set-wide).
  function bigMomentBlock(kind, idx, log, barsOnTrack, now) {
    const l = log || [];
    if (barsOnTrack < BIG_MIN_BARS) return `first ${BIG_MIN_BARS} bars`;
    if (l.some((b) => b.track === idx)) return "one big moment per song";
    if (l.some((b) => b.track === idx - 1 && b.kind === kind)) return "same big move as the last song";
    if (l.filter((b) => b.track > idx - BIG_WINDOW_SONGS).length >= BIG_MAX_PER_WINDOW) {
      return `${BIG_MAX_PER_WINDOW} big moments in the last ${BIG_WINDOW_SONGS} songs`;
    }
    if (l.length && now - l[l.length - 1].at < BIG_COOLDOWN_S) return "big-moment cooldown";
    return null;
  }
  // Why an in-song peak move may NOT run on this phrase (null = allowed).
  function peakBlock(kind, s, bars) {
    if (!s.peakOn) return "peak moves off";
    if (!s.peak) return "song not at peak";
    if (s.mashupActive) return "vocal layer running";
    if (s.barsOnTrack < MIN_BARS_ON_TRACK) return `first ${MIN_BARS_ON_TRACK} bars`;
    if (s.barsToExit != null && s.barsToExit <= EXIT_GUARD_BARS) return `last ${EXIT_GUARD_BARS} bars before exit`;
    const nextDrop = s.nextDrop || (s.nextPhraseSection === "drop" && s.nextSection === "drop");
    const inDrop = s.inDrop || (s.phraseSection === "drop" && s.section === "drop");
    if (kind === "fakeout") {
      if (!nextDrop) return "no drop on the next downbeat";
      if (inDrop) return "already inside the drop";
      return s.bigBlock || null;
    }
    if (kind === "peak_roll") {
      if (!nextDrop || inDrop) return "roll only on the run-up into a drop";
      return remixBlock("peak_roll", s, bars);
    }
    if (kind === "beat_boost") {
      if (!inDrop) return "boost only inside a drop";
      if (!s.drumsOn) return "live drums off";
      if (s.boostThisTrack || s.secsSinceBoost < BEAT_BOOST_COOLDOWN_S) return "beat boost cooldown";
      return null;
    }
    return "unknown move";
  }
  const PEAK_RULE = { fakeout: "Skrillex silence", peak_roll: "Loop Roll", beat_boost: "Fred again.. drums",
                      double_drop: "Double Drop", drop_swap: "Drop Swap" };
  function peakMove(kind, s, why, source) {
    const vocalBar = (s.lastBarVocal || 0) >= FAKEOUT_VOCAL_SHARE;
    const d = { action: kind, rule: PEAK_RULE[kind], source, peak: true, why: why || "" };
    if (kind === "fakeout") {
      d.bars = vocalBar ? 1 : 0.25;                       // 1 bar vocal-only, or 1 beat of nothing
      d.why = d.why || (vocalBar ? "1 bar with only the vocal, then the drop slams back"
                                 : "1 beat of silence, then the drop slams back");
      if (vocalBar) d.rule = "Garrix fader cut";
    } else if (kind === "peak_roll") d.why = d.why || REMIX_WHY.peak_roll();
    else if (kind === "beat_boost") {
      d.bars = BEAT_BOOST_BARS;
      d.why = d.why || `open hats + claps over ${BEAT_BOOST_BARS} bars of the drop`;
    }
    return d;
  }
  function peakRule(s) {
    if (!s.peakOn || !s.peak) return null;
    if (!peakBlock("fakeout", s)) return peakMove("fakeout", s, "", "RULE");
    if ((s.remixCount || 0) < REMIX_RULE_MAX && !peakBlock("peak_roll", s)) return peakMove("peak_roll", s, "", "RULE");
    if (!peakBlock("beat_boost", s)) return peakMove("beat_boost", s, "", "RULE");
    return null;
  }
  // Peak TRANSITION: land B's drop on A's drop downbeat. Needs a tempo-locked
  // blend plan with entry_mode "drop" (never around the echo-out fallback).
  // Returns {kind, recipe, exitAt, bTime, brake, why} or null (= just blend).
  // p: {peakOn, peak, drop (blend plan, entry_mode "drop"), aDrops (dropLines of A),
  //     aVocal, lo, hi, plannedExit, entryPos, bar, keyScore, bDropEnergy,
  //     log, trackIdx, now, brakesUsed, lastSwapBraked}
  function peakTransition(p) {
    const drop = p.drop;
    if (!p.peakOn || !p.peak || !drop || !drop.ok || drop.entry_mode !== "drop" || !drop.drop) return null;
    const bar = p.bar;
    const drops = (p.aDrops || []).filter((x) => x.t >= p.lo && x.t <= p.hi);
    drops.sort((u, v) => Math.abs(u.t - p.plannedExit) - Math.abs(v.t - p.plannedExit));
    const block = (kind, t) => bigMomentBlock(kind, p.trackIdx, p.log, (t - p.entryPos) / bar, p.now);
    for (const d of drops) {
      const aVoc = vocalShare(p.aVocal, d.t, d.t + DOUBLE_DROP_BARS * bar);
      const bVoc = drop.b_vocal_coverage;
      const oneVocal = aVoc <= DOUBLE_DROP_MAX_VOCAL || (bVoc != null && bVoc <= DOUBLE_DROP_MAX_VOCAL);
      if (p.keyScore >= PEAK_KEY_MIN && oneVocal && !block("double_drop", d.t)) {
        return { kind: "double_drop", recipe: "Double Drop", exitAt: d.t, bTime: drop.entry, brake: false,
                 why: `both drops on one downbeat, ${DOUBLE_DROP_BARS} bars, only B's bass - then A cuts` };
      }
    }
    for (const d of drops) {
      // Drop Swap "When NOT": into a drop weaker than the one it replaces.
      if (!(p.bDropEnergy >= d.energy - 0.05)) continue;
      if (block("drop_swap", d.t)) continue;
      const brake = (p.brakesUsed || 0) < BACKSPIN_MAX && !p.lastSwapBraked;
      // runs as a 4+4-bar Bass Swap in executeTransition: B's drop lands on the downbeat
      return { kind: "drop_swap", recipe: "Drop Swap", exitAt: d.t, bTime: drop.entry, brake,
               why: brake ? "A's build winds down (brake), B's drop hits on the downbeat"
                          : "A's build, then B's drop cuts in on the downbeat" };
    }
    return null;
  }

  // Live veto of one AI-planned move: the rules stay in charge.
  function aiVeto(m, s) {
    const exitNear = s.barsToExit != null;
    const holdCap = s.setMode === "quick" ? 1 : 2;
    if (m.move === "hold") {
      if (!exitNear || s.barsToExit > HOLD_BARS + PRECLEAR_SLACK_BARS) return "exit not near";
      if (s.holdsUsed >= holdCap || s.holdRoomBars < HOLD_BARS) return "no hold room left";
      return null;
    }
    if (m.move === "preclear") {
      if (s.overlapStyle === "layer") return "LAYER: A keeps the bass until the hand-off";
      if (!exitNear || s.overlapStyle === "instant" || s.overlapStyle === "peak") return "no pre-clear on an instant swap";
      if (s.preCleared || !(s.barsToExit > 2 && s.barsToExit <= 16 + PRECLEAR_SLACK_BARS)) return "outside the pre-clear window";
      return null;
    }
    if (m.move === "subdrop") {
      const bars = subdropBars(s.barSecs);
      if (!bars || !(s.section === "verse" || s.section === "breakdown")) return "not a verse/breakdown";
      if (s.sectionEnergy > SUBDROP_MAX_ENERGY || s.sectionBarsLeft < bars) return "section too hot or too short";
      if (s.barsOnTrack < MIN_BARS_ON_TRACK || (exitNear && s.barsToExit <= EXIT_GUARD_BARS)) return "too close to start/exit";
      if (s.subdropThisTrack || s.subdropLastTrack || s.secsSinceMove < SUBDROP_COOLDOWN_S) return "sub drop cooldown";
      return null;
    }
    if (m.move === "beat_layer") {
      // Rides on the mashup layer (next record's vocal over this beat, lows killed).
      return s.mashupActive ? null : "no layer booked on this phrase";
    }
    if (PEAK_MOVES.includes(m.move)) return peakBlock(m.move, s, m.bars);
    if (REMIX_MOVES.includes(m.move)) return remixBlock(m.move, s, m.bars);
    return "unknown move";
  }

  // -- LAYER transition (set study section 8 item 4, [[3-Deck Layering]]) -----
  // 2-3 songs held together for minutes, then unwound: the study's most common
  // pattern (14 of 30 segments). Restraint: at most one LAYER every
  // LAYER_EVERY transitions; the rules veto whatever the AI proposes.
  const LAYER_EVERY = 3;
  const LAYER_MIN_KEY = 0.8;
  const LAYER_MAX_CLASH = 0.03;
  // Hold / unwind bars by set mode: LONG the long end, QUICK 16 bars at most.
  function layerBars(setMode) {
    if (setMode === "long") return { maxHold: 64, unwind: 16 };
    if (setMode === "quick") return { maxHold: 16, unwind: 8 };
    return { maxHold: 32, unwind: 8 };
  }
  // Why a LAYER is off for this pair, or null. c: {ok (server plan), keyScore,
  // vocalClash, groove, sinceLayer (transitions since the last one), steering,
  // peak, energy (0-10), aiProposed}.
  function layerVeto(c) {
    if (!c || !c.ok) return (c && c.why) || "no layer window";
    if (c.steering) return "steering: bridge songs are short";
    if (c.peak) return "a peak move owns this transition";
    if (!(c.keyScore >= LAYER_MIN_KEY)) return "keys too far";
    if (c.groove === false) return "no steady groove";
    if (!(c.vocalClash <= LAYER_MAX_CLASH)) return "two vocals would overlap";
    if (c.sinceLayer < LAYER_EVERY) return `one layer every ${LAYER_EVERY} songs`;
    return null;
  }
  // -> { layer: bool, source: "AI" | "RULE", why }.
  function layerDecision(c) {
    const veto = layerVeto(c);
    if (veto) return { layer: false, source: c && c.aiProposed ? "AI" : "RULE", why: veto };
    if (c.aiProposed) return { layer: true, source: "AI", why: c.aiWhy || "AI: both grooves ride together" };
    // Rule pick: a peak floor wants swaps and drops, not a slow layer.
    if (Number.isFinite(c.energy) && c.energy >= 8) return { layer: false, source: "RULE", why: "peak floor: swap, don't layer" };
    return { layer: true, source: "RULE", why: "locked tempo, keys fit, steady grooves" };
  }

  // state -> { action, why, rule, source }. One move per phrase; order is priority.
  // Relaxed session (user: "in relax sessions it should not go upbeat"): only
  // the smooth moves survive. Peak transitions, sub drops and remix moves all
  // lift or jolt the energy, so they turn into a ride.
  const RELAXED_MOVES = new Set(["ride", "hold", "preclear", "layer", "instant"]);
  function decide(s) {
    const dec = decideAny(s);
    if (s.relaxed && dec && !RELAXED_MOVES.has(dec.action)) {
      return { action: "ride", rule: "R", source: dec.source || "RULE",
               why: `relaxed session: no ${dec.action} - the record rides` };
    }
    return dec;
  }

  function decideAny(s) {
    const exitNear = s.barsToExit != null;
    // A LAYER transition is running: both records ride, no phrase moves.
    if (s.layerActive) return { action: "layer", rule: "L", source: s.layerSource, why: s.layerWhy || "two records layered" };
    // Recipe-forced: an instant-swap pair meets on a drop, whatever the plan says.
    if (exitNear && s.overlapStyle === "instant" && !s.instantShown &&
        s.barsToExit <= PHRASE_BARS + PRECLEAR_SLACK_BARS) {
      return { action: "instant", rule: "1", why: "records meet on a drop - bass swaps on one downbeat" };
    }
    // Peak transition (double drop / drop swap) booked by peakTransition().
    if (exitNear && s.overlapStyle === "peak" && !s.instantShown &&
        s.barsToExit <= PHRASE_BARS + PRECLEAR_SLACK_BARS) {
      const k = s.peakKind === "drop_swap" ? "drop_swap" : "double_drop";
      return { action: k, rule: PEAK_RULE[k], source: "RULE", peak: true, why: s.peakWhy || "" };
    }
    // The AI's planned move for this phrase, if the live state still allows it.
    let vetoed = "";
    if (s.aiMove) {
      const block = aiVeto(s.aiMove, s);
      if (!block) {
        const m = s.aiMove;
        if (m.move === "beat_layer") return { action: "layer", rule: "2", source: "AI", why: m.reason || "layering the next record" };
        if (PEAK_MOVES.includes(m.move)) return peakMove(m.move, s, m.reason, "AI");
        if (REMIX_MOVES.includes(m.move)) return remixMove(m.move, m.bars, m.reason, "AI");
        const bars = m.move === "subdrop" ? subdropBars(s.barSecs) : undefined;
        return { action: m.move, rule: "", bars, source: "AI",
                 why: m.reason || (m.move === "hold" ? "letting the build resolve first" : "planned") };
      }
      vetoed = ` (AI ${s.aiMove.move} vetoed: ${block})`;
    }
    const dec = decideRules(s, exitNear);
    if (vetoed) dec.why += vetoed;
    return dec;
  }

  function decideRules(s, exitNear) {
    if (exitNear) {
      const holdCap = s.setMode === "quick" ? 1 : 2;
      if (s.section === "build" && s.barsToExit <= HOLD_BARS + PRECLEAR_SLACK_BARS &&
          s.holdsUsed < holdCap && s.holdRoomBars >= HOLD_BARS) {
        return { action: "hold", rule: "4.1", why: "build running into the exit - let it resolve first" };
      }
      if (s.overlapStyle === "instant" || s.overlapStyle === "peak" || s.overlapStyle === "layer") {
        // handled in decide(); an instant / peak pair never pre-clears, and a
        // LAYER keeps A's bass until the hand-off inside the layer
      } else if (!s.preCleared && s.barsToExit > 2 &&
                 s.barsToExit <= s.preClearBars + PRECLEAR_SLACK_BARS) {
        return { action: "preclear", rule: "3",
                 why: `easing the outgoing sub ${-PRECLEAR_DB} dB, ~${Math.round(s.barsToExit)} bars before the swap` };
      }
    }
    if (s.mashupActive) {
      return { action: "layer", rule: "2", why: "next record's vocal riding this beat" };
    }
    const bars = subdropBars(s.barSecs);
    if (bars && s.setMode !== "quick" &&
        (s.section === "verse" || s.section === "breakdown") &&
        s.vocalAhead >= SUBDROP_VOCAL_SHARE && s.sectionEnergy <= SUBDROP_MAX_ENERGY &&
        s.barsOnTrack >= MIN_BARS_ON_TRACK && s.sectionBarsLeft >= bars &&
        (!exitNear || s.barsToExit > EXIT_GUARD_BARS) &&
        !s.subdropThisTrack && !s.subdropLastTrack &&
        s.secsSinceMove >= SUBDROP_COOLDOWN_S) {
      return { action: "subdrop", rule: "4", bars,
               why: `vocal up front - sub out for ${bars} bars, then back in hard` };
    }
    const pk = peakRule(s);
    if (pk) return pk;
    // Remix rules (no AI plan, or the plan had nothing here). Rules stop at
    // REMIX_RULE_MAX; only an AI plan uses the full per-song cap.
    if ((s.remixCount || 0) < REMIX_RULE_MAX) {
      if (s.preDrop) {
        for (const k of DROP_LEADINS) if (!remixBlock(k, s)) return remixMove(k, undefined, "", "RULE");
      }
      const sect = s.phraseSection != null ? s.phraseSection : s.section;
      const energy = s.phraseEnergy != null ? s.phraseEnergy : s.sectionEnergy;
      // weak stretch that runs on past this phrase: skip one phrase of it
      if (s.setMode !== "long" && ["intro", "verse", "breakdown"].includes(sect) &&
          energy < 0.45 && s.nextPhraseSection === sect && !remixBlock("beat_jump", s, 8)) {
        return remixMove("beat_jump", 8, "", "RULE");
      }
      if (s.setMode !== "quick" && sect === "drop" && energy >= 0.7 &&
          s.nextPhraseSection === "drop" && !remixBlock("loop_extend", s, 4)) {
        const lb = s.setMode === "long" ? 8 : 4;
        return remixMove("loop_extend", lb, "", "RULE");
      }
    }
    return { action: "ride", rule: "", why: s.section ? `riding the ${s.section}` : "riding the record" };
  }

  // -- safety net: the playing song is about to end with nothing scheduled --
  function needsHoldLoop(remainingS, planned, looping) {
    return !planned && !looping && remainingS <= HOLD_LOOP_LEAD_S;
  }
  // Last clean 8-bar phrase that ends before the outro (grid-snapped) and has
  // already started. null when there is none.
  function holdLoopAnchor(downbeats, longSecs, pos, duration, barSecs) {
    const outro = (longSecs || []).find((x) => x.label === "outro" && x.start <= pos + PHRASE_BARS * barSecs);
    const pick = (limit) => {
      let best = null;
      for (let i = 0; ; i++) {
        const [a, b] = phraseBounds(downbeats, i, barSecs);
        if (a > pos || b > duration + 0.01) break;
        if (b <= limit + 0.01) best = a;
      }
      return best;
    };
    const before = outro ? pick(outro.start) : null;
    return before != null ? before : pick(duration);
  }
  // The hold loop should sound like the song going on (an extended edit), not
  // a stuck record: loop the last 32 bars before the outro (16 / 8 when the
  // song hasn't played that much since it came in). `anchor` = start of the
  // last clean phrase; returns {start, bars} with start on a downbeat.
  // With vocal regions, every seam (loop start, loop end, and the jump-out
  // point at the anchor's end) must sit in a vocal gap: a seam through a sung
  // line chops the singer at every wrap. Candidates: 32/16/8 bars, ending at
  // the anchor's end or up to 3 phrases earlier; longest clean span wins.
  // All spans worth trying, in preference order: vocal-clean ones first
  // (longest, latest), then the plain spans. Each: {start, bars, end, vocalClean}.
  // The loop engages at the anchor's end (jump-out), so the first seam is
  // jumpOut -> start and every later one end -> start; both get checked.
  function holdLoopCandidates(anchor, entry, barSecs, downbeats, vocals) {
    const snap = (t) => (downbeats && downbeats.length
      ? downbeats.reduce((b, x) => (Math.abs(x - t) < Math.abs(b - t) ? x : b), downbeats[0]) : t);
    const phrase = PHRASE_BARS * barSecs, lo = Math.max(0, entry || 0) - 0.01;
    const cuts = (t) => (vocals || []).some(([s, e]) => s < t - SEAM_VOCAL_GAP_S && e > t + SEAM_VOCAL_GAP_S);
    const jumpOut = anchor + phrase, out = [], seen = new Set();
    const add = (start, bars, clean) => {
      const key = `${start.toFixed(2)}:${bars}`;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ start, bars, end: start + bars * barSecs, jumpOut, vocalClean: clean });
    };
    if (vocals && vocals.length && !cuts(jumpOut)) {
      for (const bars of [32, 16, PHRASE_BARS]) {
        for (let back = 0; back <= 3; back++) {
          const end = back ? snap(jumpOut - back * phrase) : jumpOut;
          const start = snap(end - bars * barSecs);
          if (start >= lo && !cuts(start) && !cuts(end)) add(start, bars, true);
        }
      }
    }
    for (const bars of [32, 16, PHRASE_BARS]) {
      const start = bars === PHRASE_BARS ? anchor : snap(anchor - (bars - PHRASE_BARS) * barSecs);
      if (bars === PHRASE_BARS || start >= lo) add(start, bars, !(vocals && vocals.length) ? null : false);
    }
    return out;
  }
  function holdLoopSpan(anchor, entry, barSecs, downbeats, vocals) {
    const c = holdLoopCandidates(anchor, entry, barSecs, downbeats, vocals)[0];
    return c ? { start: c.start, bars: c.bars } : { start: anchor, bars: PHRASE_BARS };
  }
  // Pick from silently-scored candidates: the first (most preferred) whose
  // worst seam passes, else the best-sounding one. seam(c) -> 0..1 or null.
  function pickHoldLoop(cands, seam, pass) {
    let best = null, bestS = -1;
    for (const c of cands) {
      const s = seam(c);
      if (s == null) return { pick: c, score: null };          // can't score: keep preference order
      if (s >= pass) return { pick: c, score: s };
      if (s > bestS) { best = c; bestS = s; }
    }
    return { pick: best || cands[0], score: best ? bestS : null };
  }

  // Song identity for the motif count: the tracklist's own operational
  // definition in research/notes/set-study-mDtud5fLgFQ.md section 5 is "the
  // same title again" ("Quiereme", "Quiereme x2", "Quiereme x3"), so a remix
  // or edit of a played song is the same hook. Mirrors _bare_title() in
  // app/ui/autopilot_service.py, plus the "xN" pass counter. Unicode letters
  // are kept so non-Latin titles still get an identity.
  function hookKey(name) {
    let t = String(name || "").toLowerCase();
    const dash = t.indexOf(" - ");
    if (dash >= 0) t = t.slice(dash + 3);                 // "Artist - Title" -> title
    t = t.replace(/[([][^)\]]*[)\]]/g, " ");              // (Remix) / [Official Video]
    t = t.split(/\s+[-–—|]\s+/)[0];
    t = t.replace(/\s+(feat\.?|ft\.?|featuring)\s+.*$/, "");
    t = t.replace(/\s+x\s?\d+\s*$/, "");                  // "Quiereme x2" -> "quiereme"
    return t.replace(/[^\p{L}\p{N} ]+/gu, " ").split(/\s+/).filter(Boolean).join(" ");
  }

  // The set's recurring hook: the most-played identity with >= MOTIF_MIN_PLAYS
  // plays (ties: the one introduced first). null when nothing has recurred.
  // -> {key, title (latest display name), plays, lastIdx}
  function motifHook(history) {
    const seen = new Map();
    (history || []).forEach((name, i) => {
      const key = hookKey(name);
      if (!key) return;
      const m = seen.get(key) || { key, title: name, plays: 0, firstIdx: i, lastIdx: i };
      m.plays += 1; m.lastIdx = i; m.title = name;
      seen.set(key, m);
    });
    let best = null;
    for (const m of seen.values()) {
      if (m.plays < MOTIF_MIN_PLAYS) continue;
      if (!best || m.plays > best.plays || (m.plays === best.plays && m.firstIdx < best.firstIdx)) best = m;
    }
    return best && { key: best.key, title: best.title, plays: best.plays, lastIdx: best.lastIdx };
  }

  // A motif may return (research/notes/set-study-mDtud5fLgFQ.md section 5):
  // under MOTIF_MAX_PLAYS, >= MOTIF_GAP_SONGS songs since its last play
  // (history ends with the playing song), not already asked for at this count.
  function repriseDue(m, history, asked) {
    if (!m || m.plays >= MOTIF_MAX_PLAYS) return false;
    if ((history || []).length - 1 - m.lastIdx < MOTIF_GAP_SONGS) return false;
    return asked !== `${m.key}:${m.plays}`;
  }

  // Rule 9 (dip after a long peak) outranks the mDtud5fLgFQ reprise, which
  // outranks rule 7 (late callback). A set with a motif already has its
  // repetition device (research/notes/set-study-mDtud5fLgFQ.md section 6,
  // "Callback device": one hook threaded through the set INSTEAD of an
  // open/close frame), so the opener bookend is not asked for on top of it.
  // opts: {history: display names played, playing song last; repriseAsked}
  function energyNote(energies, setPos, callbackDone, opts = {}) {
    const e = (energies || []).filter(Number.isFinite);
    if (e.length >= 2 && e.slice(-2).every((v) => v >= PEAK_ENERGY)) return "dip";
    const m = motifHook(opts.history);
    if (repriseDue(m, opts.history, opts.repriseAsked)) return "reprise";
    if (!callbackDone && !m && setPos >= CALLBACK_SET_POS) return "callback";
    return null;
  }

  // The loop start a deck on the master can use without jumping back: `start`, or the
  // first loop line (start + k loops) at or ahead of the playhead.
  function nextLoopStart(start, pos, len, onAir) {
    if (!onAir || !(len > 0) || start >= pos - 0.08) return start;
    return start + Math.ceil((pos - 0.08 - start) / len) * len;
  }
  // How long the client waits for the LLM plan. Measured plans take 4-13 s, so a
  // 3 s floor threw 13 of 19 away after the model time was already spent.
  const PLAN_MIN_WAIT_MS = 12000, PLAN_MAX_WAIT_MS = 65000;   // server LLM timeout is 60 s
  const planWaitMs = (deadlineS) => Math.max(PLAN_MIN_WAIT_MS,
    Math.min(PLAN_MAX_WAIT_MS, (Number.isFinite(deadlineS) ? deadlineS : Infinity) * 1000));
  const core = { planWaitMs, PLAN_MIN_WAIT_MS, nextLoopStart, decide, mergeSections, sectionAt, phraseAt, vocalShare, subdropBars, energyNote,
                 hookKey, motifHook, MOTIF_MAX_PLAYS, MOTIF_GAP_SONGS,
                 phraseBounds, phraseLabel, isPreDrop, remixBlock, aiVeto, needsHoldLoop, holdLoopAnchor, holdLoopSpan, holdLoopCandidates, pickHoldLoop,
                 PRECLEAR_DB, LOW_KILL, REMIX_MOVES,
                 camelotScore, energyQ3, isPeak, dropLines, DROP_JUMP, bigMomentBlock, peakBlock, peakTransition,
                 PEAK_MOVES, BIG_MOMENTS, BIG_COOLDOWN_S, BEAT_BOOST_BARS, BEAT_BOOST_COOLDOWN_S, BACKSPIN_MAX,
                 layerBars, layerVeto, layerDecision, LAYER_EVERY };
  root.djMindCore = core;
  if (typeof module !== "undefined" && module.exports) module.exports = core;

  // ---- runtime: reaches the world only through the Host port (engine.js) -------------------------------
  function create({ host, ai }) {
  const { setTimeout, clearTimeout, setInterval, clearInterval } = host.clock;
  const audioCtx = host.audio;
  const ui = host.ui;

  // ------------------------------------------------------------------ runtime
  const TICK_MS = 250;
  const LABEL = { hold: "HOLD", preclear: "PRE-CLEAR", instant: "INSTANT SWAP",
                  subdrop: "SUB DROP", layer: "LAYER", ride: "RIDE",
                  loop_extend: "LOOP EXTEND", beat_jump: "BEAT JUMP", stutter: "STUTTER",
                  filter_build: "FILTER BUILD", echo_freeze: "ECHO FREEZE",
                  holdloop: "HOLD LOOP", peak_roll: "ROLL INTO DROP",
                  double_drop: "DOUBLE DROP", drop_swap: "DROP SWAP", fakeout: "FAKE-OUT",
                  beat_boost: "BEAT BOOST", learned_move: "LEARNED MOVE", artist_move: "ARTIST MOVE" };

  let deckId = null, timer = null, timers = [];
  let lastPhrase = null, trackIdx = 0, subdropTrackIdx = -9;
  let lastMoveAt = -Infinity;                   // host.clock.perfNow() seconds
  let holdsUsed = 0, preCleared = false, instantShown = false;
  let plan = null;                              // {fireAt, maxFireAt, style, preClearBars}
  let aiMoves = [], aiFor = null;               // validated AI moves for the playing song

  let remixUsed = [], lastRemixPhrase = null, busyUntil = 0;
  let holdLoop = null;                          // {start, bars, passes} safety loop
  let inTransition = false;                     // onTransition() .. follow(): no moves
  let phraseQuiet = false;                      // this phrase line decided a ride (an artist slip loop may run)
  let lastFillTransition = -9, transitions = 0;
  let lastLayerTransition = -9;                 // LAYER ledger (one every LAYER_EVERY)
  let layerRun = null;                          // {until (nowS), source, why} while a LAYER plays
  const energies = []; let callbackDone = false;
  let repriseAsked = null;                      // "hookKey:plays" of the last reprise hint (mDtud5fLgFQ)
  const log = [];
  // PEAK ledger (set-wide): big moments, backspins, beat boosts, LLM energy
  const bigLog = [];                            // [{track, kind, at}]
  let brakesUsed = 0, lastSwapBraked = false;
  let boostTrackIdx = -9, lastBoostAt = -Infinity;
  let profileE = null;                          // LLM current_profile energy of the playing song

  const nowS = () => host.clock.perfNow() / 1000;
  const deck = () => (deckId && host.decks ? host.decks[deckId] : null);
  const barSecsOf = (d) => 240 / ((d && d.bpm) || 128);

  function eqLow(id) { return ui.query(`.eq-knob[data-deck="${id}"][data-band="low"]`); }
  function setKnob(el, v) {
    if (!el) return;
    el.value = String(v);
    ui.fire(el, "input", true);
  }
  function later(ms, fn) { const t = setTimeout(fn, Math.max(0, ms)); timers.push(t); }
  function ramp(el, to, ms) {
    if (!el) return;
    const from = parseFloat(el.value) || 0, steps = 16;
    for (let i = 1; i <= steps; i++) later((ms * i) / steps, () => setKnob(el, from + (to - from) * (i / steps)));
  }
  function cancelMoves() {
    timers.forEach(clearTimeout); timers = [];
    const lm = host.mod.learnedMoves, d = deck();
    if (lm && d) lm.stop(d);                      // a booked learned slice window / stem mode goes with the moves
    const am = host.mod.artistMoves;
    if (am && d) am.stop(d);                      // booked artist stabs / a slip loop (released onto its shadow)
  }

  function render(dec) {
    const nowEl = ui.el("ap-mind-now");
    const logEl = ui.el("ap-mind-log");
    const panel = ui.el("ap-mind");
    if (!nowEl) return;
    const tag = dec.source || (dec.action === "holdloop" ? "SAFETY" : dec.action !== "ride" ? "RULE" : "");
    const peakTag = (p) => (p ? `<span class="ap-mind-tag ap-mind-tag-peak">PEAK</span>` : "");
    nowEl.innerHTML = peakTag(dec.peak) + (tag ? `<span class="ap-mind-tag ap-mind-tag-${tag.toLowerCase()}">${tag}</span>` : "") +
      `<span class="ap-mind-act ap-mind-${dec.action}">${LABEL[dec.action]}</span>` +
      `<span class="ap-mind-why">${esc(dec.why)}${dec.rule ? ` · ${esc(dec.rule)}` : ""}</span>`;
    if (panel && dec.action !== "ride") {
      panel.classList.remove("ap-mind-flash"); void panel.offsetWidth; panel.classList.add("ap-mind-flash");
    }
    if (logEl) {
      logEl.innerHTML = log.slice(-4).reverse()
        .map((l) => `<li>${peakTag(l.peak)}${l.tag ? `<span class="ap-mind-tag ap-mind-tag-${l.tag.toLowerCase()}">${l.tag}</span>` : ""}` +
                    `<b>${LABEL[l.action]}</b> ${l.clock} ${esc(l.why)}</li>`).join("");
    }
  }

  // -- deck loop / fx helpers (reuse deck-controller + fx-rack, no new DSP) --
  function loopUi(id, d) {
    const btn = ui.query(`.deck-btn[data-deck="${id}"][data-action="loop-toggle"]`);
    if (btn) btn.classList.toggle("loop-active", !!d.loopOn);
    const v = ui.el(`loop-value-${id}`);
    if (v) v.textContent = String(d.loopBeats);
  }
  // Loop `beats` from track time `start` (grid point), exact: loopOn + seek.
  // A deck on the master never jumps back to reach `start` (user): the loop
  // engages on the first loop line AT or AHEAD of the playhead (start + k loops),
  // when playback gets there. Returns the loop start used.
  function loopAt(d, id, start, beats) {
    const len = beats * 60 / (d.bpm || 128);
    const s = nextLoopStart(start, d._currentPosition(), len, !!(d.playing && d._onAir && d._onAir()));
    const ana = d.analysis;
    const engage = () => {
      if (deckId !== id || d.analysis !== ana || !d.playing) return;   // song changed meanwhile
      const was = d.loopOn, prevBeats = d.loopBeats;
      if (!was) d._loopSlip = { pos: s, at: audioCtx.currentTime };  // where the song's own timeline left off
      if (typeof d.setLoopBeats === "function") d.setLoopBeats(beats);
      d.loopOn = true;
      if (!d.seek(s)) {
        // refused (the engage fired late): a loop already running keeps going as it
        // was; with none running nothing was started, so just stay out of loop mode
        if (was) { if (typeof d.setLoopBeats === "function") d.setLoopBeats(prevBeats); }
        else { d.loopOn = false; d._loopSlip = null; }
      }
      loopUi(id, d);
    };
    const wait = (s - d._currentPosition()) / ((d._playbackRate && d._playbackRate()) || 1);
    if (wait > 0.08) setTimeout(engage, Math.max(0, wait * 1000 - 20)); else engage();
    return s;
  }
  // Leave a loop. `to` = where the song continues: its own loop end, or (stutter /
  // roll) where the song would be had the loop never played (slip: the song's own
  // timeline, not a jump). A deck on the master refuses anything else; then the
  // loop simply ends where playback is. Never leaves a native loop running.
  function loopRelease(d, id, to) {
    if (!d.loopOn) return;
    const here = d._currentPosition();                     // read while the loop still wraps
    const rate = (d._playbackRate && d._playbackRate()) || 1;
    const slip = d._loopSlip ? d._loopSlip.pos + (audioCtx.currentTime - d._loopSlip.at) * rate : null;
    d.loopOn = false;
    d._loopSlip = null;
    if (!d.seek(to, { loopExit: true, slipTo: slip }) && d.playing) d.play(here);
    loopUi(id, d);
  }
  function fxEcho(id, on) {
    const u = host.mod.fxUnits && host.mod.fxUnits[id];
    if (!u) return;
    if (on) { u.setType("echo"); u.setWet(0.6); u.setActive(true); } else u.setActive(false);
    if (typeof updateFxTypeButtons === "function") updateFxTypeButtons(id);
    if (typeof updateFxPad === "function") updateFxPad(id);
  }
  function eqBand(id, band) {
    return ui.query(`.eq-knob[data-deck="${id}"][data-band="${band}"]`);
  }

  function aDropLines(d, bar) {
    const a = d.analysis || {};
    if (a._mindDrops && a._mindDropsBar === bar) return a._mindDrops;
    a._mindDrops = dropLines(a.phrase_boundaries_8bar, a.energy_times, a.energy_curve, bar);
    a._mindDropsBar = bar;
    return a._mindDrops;
  }
  function isDropAt(d, t, bar) { return aDropLines(d, bar).some((x) => Math.abs(x.t - t) <= bar); }
  function state(d, pos) {
    const a = d.analysis || {};
    const bar = barSecsOf(d);
    const rate = (d._playbackRate && d._playbackRate()) || 1;
    const long = mergeSections(a.sections, bar);
    const sec = sectionAt(long, pos);
    const mode = ui.el("ap-mode");
    const idx = phraseAt(a.downbeat_times, pos, bar);
    const [p0, p1] = phraseBounds(a.downbeat_times, idx, bar);
    const plen = p1 - p0;
    const [pLab, pEnergy] = phraseLabel(a.sections, p0, p1);
    const [nLab] = phraseLabel(a.sections, p1, p1 + plen);
    // A beat jump of n bars skips [p1, p1+n) and lands on p1+n. Blocked when a
    // skipped phrase OR the landing phrase holds a drop (labels flicker, so the
    // acoustic drop detector counts too), or when a skipped phrase is at or
    // above the song's median energy - that is a hook / popular section, not a
    // "weak stretch" (user: "AI skips drops and popular sections").
    const medE = (() => {
      const e = long.map((x) => x.energy).filter(Number.isFinite).sort((x, y) => x - y);
      return e.length ? e[Math.floor(0.5 * (e.length - 1))] : null;
    })();
    const dropIn = (n) => {
      for (let k = 0; k * PHRASE_BARS <= n; k++) {
        const t0 = p1 + k * plen, [lab, en] = phraseLabel(a.sections, t0, t0 + plen);
        if (RELEASE_LABELS.includes(lab) || isDropAt(d, t0, bar)) return true;
        const skipped = k * PHRASE_BARS < n;
        if (skipped && medE != null && Number.isFinite(en) && en >= medE) return true;
      }
      return false;
    };
    const aiMove = aiFor === trackIdx
      ? aiMoves.find((m) => !m.done && Math.abs(m.at - p0) <= bar) || null : null;
    const st = {
      barSecs: bar, phraseIdx: idx, phraseStart: p0, phraseEnd: p1,
      section: sec ? sec.label : null,
      nextSection: (sectionAt(long, p1 + 0.01) || {}).label || null,
      inDrop: isDropAt(d, p0, bar), nextDrop: isDropAt(d, p1, bar),
      sectionEnergy: sec ? sec.energy : 0.5,
      sectionBarsLeft: sec ? (sec.end - pos) / bar : 0,
      vocalAhead: vocalShare(a.vocal_active_regions, pos, pos + PHRASE_BARS * bar),
      barsOnTrack: (pos - (d._mindEntry || 0)) / bar,
      barsToExit: plan ? (plan.fireAt - pos) / bar : null,
      holdRoomBars: plan ? (plan.maxFireAt - plan.fireAt) / bar : 0,
      overlapStyle: plan ? plan.style : null,
      preClearBars: plan ? plan.preClearBars : 8,
      preCleared, instantShown, holdsUsed,
      mashupActive: !!(host.mod.mashup && host.mod.mashup.active),
      subdropThisTrack: subdropTrackIdx === trackIdx,
      subdropLastTrack: subdropTrackIdx === trackIdx - 1,
      secsSinceMove: nowS() - lastMoveAt,
      setMode: mode ? mode.value : "hybrid",
      phraseSection: pLab, phraseEnergy: pEnergy, nextPhraseSection: nLab,
      preDrop: isPreDrop(pLab, nLab),
      skipHitsDrop: { 8: dropIn(8), 16: dropIn(16) },
      remixUsed, remixCount: remixUsed.length, lastRemixPhrase,
      onAir: !!(d._onAir && d._onAir()),
      relaxed: !!(host.session && host.session.relaxed),
      aiMove,
      rate,
      // PEAK mode
      peakOn: peakOn(), profileEnergy: profileE, energyQ3: energyQ3(long),
      bigBlock: plan && plan.style === "peak" ? "the transition is this song's big moment"
        : bigMomentBlock("fakeout", trackIdx, bigLog, (pos - (d._mindEntry || 0)) / bar, nowS()),
      drumsOn: !!(host.mod.beatLayer && host.mod.beatLayer.boostUntil && host.mod.beatLayer.isEnabled()),
      boostThisTrack: boostTrackIdx === trackIdx, secsSinceBoost: nowS() - lastBoostAt,
      lastBarVocal: vocalShare(a.vocal_active_regions, p1 - bar, p1),
      peakKind: plan ? plan.peakKind : null, peakWhy: plan ? plan.peakWhy : null,
    };
    st.peak = isPeak(st);
    return st;
  }

  function apply(dec, st, d) {
    const bar = st.barSecs, rate = st.rate, id = deckId;
    const ms = (sec) => Math.max(0, (sec * 1000) / rate);
    const pos = d._currentPosition();
    const at = (trackT, fn) => later(ms(trackT - pos), () => { if (deckId === id) fn(); });
    if (st.aiMove && st.aiMove.move === dec.action) st.aiMove.done = true;
    if (REMIX_MOVES.includes(dec.action)) {
      remixUsed.push(dec.action);
      lastRemixPhrase = st.phraseIdx;
      lastMoveAt = nowS();
    }
    const beat = bar / 4, end = st.phraseEnd;
    if (dec.action === "hold") {
      holdsUsed++;
      plan.fireAt = Math.min(plan.maxFireAt, plan.fireAt + HOLD_BARS * bar);
    } else if (dec.action === "preclear") {
      preCleared = true;
      ramp(eqLow(deckId), PRECLEAR_DB, (PRECLEAR_RAMP_BARS * bar * 1000) / rate);
    } else if (dec.action === "instant") {
      instantShown = true;
    } else if (dec.action === "subdrop") {
      subdropTrackIdx = trackIdx;
      lastMoveAt = nowS();
      const el = eqLow(deckId);
      ramp(el, LOW_KILL, (bar / 4) * 1000 / rate);                 // one beat down
      later((dec.bars * bar * 1000) / rate, () => {                  // snap back on the line
        if (deckId === id) setKnob(el, 0);
      });
    } else if (dec.action === "loop_extend") {
      // Loop the phrase's last `bars` once more, release on the phrase line.
      const n = dec.bars === 8 ? 8 : 4, L = end - n * bar;
      at(L, () => loopAt(d, id, L, n * 4));
      later(ms(L - pos) + ms(2 * n * bar) - 30, () => { if (deckId === id) loopRelease(d, id, end - 0.03 * rate); });
      busyUntil = nowS() + (ms(end - pos) + ms(n * bar)) / 1000;
    } else if (dec.action === "beat_jump") {
      // Skip ahead from the phrase line: the jump keeps the grid phase.
      const n = dec.bars === 16 ? 16 : 8;
      at(end - 0.02, () => { if (typeof d.jumpBeats === "function") d.jumpBeats(n * 4); });
    } else if (dec.action === "stutter") {
      // Last 2 bars restart from L with a shrinking loop: 4 beats x1, 2 x1,
      // 1 x1, 1/2 x2 = 8 beats of wall time, then release onto the drop downbeat.
      const L = end - 2 * bar;
      let t = ms(L - pos);
      for (const [lenBeats, playBeats] of [[4, 4], [2, 2], [1, 1], [0.5, 1]]) {
        later(t, () => { if (deckId === id) loopAt(d, id, L, lenBeats); });
        t += ms(playBeats * beat);
      }
      later(t - 10, () => { if (deckId === id) loopRelease(d, id, end); });
      busyUntil = nowS() + t / 1000 + 1;
    } else if (dec.action === "filter_build") {
      // Sweep lows + mids out across the phrase, snap both open on the drop.
      const lo = eqBand(id, "low"), mid = eqBand(id, "mid");
      const dur = ms(end - pos - beat);
      ramp(lo, LOW_KILL, dur);
      ramp(mid, FILTER_MID_DB, dur);
      at(end, () => { setKnob(lo, 0); setKnob(mid, 0); });
    } else if (dec.action === "echo_freeze") {
      // Echo throw on the last beat (Echo Out, beat 4): the FX sits post-EQ, so
      // the beat feeds the delay, the tail rings into the drop, then the unit
      // is disarmed a bar in so the drop plays clean.
      at(end - beat, () => fxEcho(id, true));
      at(end + bar, () => fxEcho(id, false));
    } else if (dec.action === "double_drop" || dec.action === "drop_swap") {
      instantShown = true;                       // the transition itself runs in autopilot.js
    } else if (dec.action === "fakeout") {
      // [[Skrillex Case Study]]: 1 beat of nothing before impact (the ear's
      // reflex makes the drop feel louder). [[Martin Garrix Case Study]]: cut
      // for the last bar and let the vocal carry (lows + highs out, mids stay).
      // Either way the drop downbeat slams back at full level.
      bigLog.push({ track: trackIdx, kind: "fakeout", at: nowS() });
      lastMoveAt = nowS();
      if (dec.bars >= 1) {
        const lo = eqBand(id, "low"), hi = eqBand(id, "high");
        at(end - bar, () => { setKnob(lo, LOW_KILL); setKnob(hi, LOW_KILL); });
        at(end, () => { setKnob(lo, 0); setKnob(hi, 0); });
      } else {
        const vol = ui.query(`.volume-fader[data-deck="${id}"]`);
        let was = null;
        at(end - beat, () => { was = vol ? vol.value : null; setKnob(vol, 0); });
        at(end, () => { if (was != null) setKnob(vol, was); });
      }
    } else if (dec.action === "peak_roll") {
      // [[Loop Roll]] / [[Build-to-Drop Transition]]: last 2 bars of the build,
      // loop 1 beat x4, 1/2 x2, 1/4 x2 (8 beats of wall time) under a filter
      // riser (lows + mids swept out), released exactly on the drop downbeat.
      const L = end - 2 * bar;
      const lo = eqBand(id, "low"), mid = eqBand(id, "mid");
      let t = ms(L - pos);
      later(t, () => { if (deckId === id) { ramp(lo, LOW_KILL, ms(2 * bar - beat)); ramp(mid, FILTER_MID_DB, ms(2 * bar - beat)); } });
      for (const [lenBeats, playBeats] of [[1, 4], [0.5, 2], [0.25, 2]]) {
        later(t, () => { if (deckId === id) loopAt(d, id, L, lenBeats); });
        t += ms(playBeats * beat);
      }
      later(t - 10, () => { if (deckId === id) { loopRelease(d, id, end); setKnob(lo, 0); setKnob(mid, 0); } });
      busyUntil = nowS() + t / 1000 + 1;
    } else if (dec.action === "beat_boost") {
      // [[Fred again.. Case Study]]: live drums over the record, here one
      // 8-bar phrase of open hats + claps inside a peak drop (beat-layer.js).
      boostTrackIdx = trackIdx; lastBoostAt = nowS();
      if (host.mod.beatLayer && host.mod.beatLayer.boostUntil) host.mod.beatLayer.boostUntil(end);
    }
  }

  // Safety net: the song is ending and nothing is scheduled -> at the end of
  // the last clean phrase before the outro, jump back up to 32 bars and loop
  // that whole span, so the song sounds extended rather than stuck. Never
  // shrinks: a short loop getting shorter is what a stuck record sounds like.
  function holdLoopTick(d, pos) {
    if (holdLoop) return;
    const a = d.analysis || {};
    const bar = barSecsOf(d);
    const rate = (d._playbackRate && d._playbackRate()) || 1;
    const dur = d.buffer ? d.buffer.duration : Infinity;
    if (!needsHoldLoop(dur - pos, !!plan, !!d.loopOn)) return;
    const long = mergeSections(a.sections, bar);
    const anchor = holdLoopAnchor(a.downbeat_times, long, pos, dur, bar);
    if (anchor == null) return;
    const endT = anchor + PHRASE_BARS * bar, id = deckId;
    // Silent pre-check (live-ear.js): every candidate is scored on the deck's
    // buffer before anything is heard; the crowd only hears the winner.
    const cands = holdLoopCandidates(anchor, d._mindEntry, bar, a.downbeat_times, a.vocal_active_regions);
    const ear = host.mod.liveEar && host.mod.liveEar.precheck;
    const scored = ear ? ear(d, cands.flatMap((c) => [c, { start: c.start, bars: (c.jumpOut - c.start) / bar, _jump: c }]), bar) : [];
    const seamOf = (c) => {
      if (!ear) return null;
      const wrap = scored.find((s) => s.start === c.start && s.bars === c.bars && !s._jump);
      const jump = scored.find((s) => s._jump === c);
      const w = wrap && wrap.seam ? wrap.seam.score : null, j = jump && jump.seam ? jump.seam.score : null;
      return w == null ? null : Math.min(w, j == null ? w : j);
    };
    const choice = pickHoldLoop(cands, seamOf, HOLD_SEAM_PASS);
    const span = choice.pick || { start: anchor, bars: PHRASE_BARS };
    holdLoop = { start: span.start, bars: span.bars, seam: choice.score,
                 tried: cands.map((c) => ({ start: c.start, bars: c.bars, seam: seamOf(c), vocalClean: c.vocalClean })) };
    emitAi("precheck", { deck: deckId, span, score: choice.score, tried: holdLoop.tried.length });
    if (host.mod.liveEar && host.mod.liveEar.silentEar) {
      host.mod.liveEar.silentEar(d, { start: span.start, bars: span.bars, seam: { score: choice.score } }, bar)
        .then((r) => { if (r && holdLoop) { holdLoop.omni = r; emitAi("silent-ear", { deck: id, result: r }); } });
    }
    const go = () => {
      if (deckId !== id || plan) { holdLoop = null; return; }
      loopAt(d, id, span.start, span.bars * 4);
      holdLoop.looping = true;
      holdLoop.since = nowS();
      if (host.mod.learnedMoves) host.mod.learnedMoves.noteFiller(d, span);   // logged as the learned loop_extend when that kind is on
      // The span crosses a sung line: loop the instrumental (live stems), so it
      // plays like an extended break instead of a chopped singer.
      if (span.vocalClean === false && host.mod.stemMoves && host.mod.stemMoves.instrumental(d, true)) {
        holdLoop.instrumental = true;
      }
    };
    // Loop back on the phrase line: at the anchor's end if still ahead, else
    // (already in the outro) at the next phrase line, so the jump back keeps
    // the grid. Right away only if the track ends before that line.
    let lineT = endT;
    if (endT <= pos) {
      const [, nextLine] = phraseBounds(a.downbeat_times, phraseAt(a.downbeat_times, pos, bar), bar);
      lineT = nextLine < dur - 0.5 ? nextLine : pos;
    }
    if (lineT > pos) later(((lineT - pos) * 1000) / rate - 10, go);
    else go();
    const checked = choice.score == null ? "" : ` · silent check ${Math.round(choice.score * 100)}% of ${cands.length}`;
    const vox = span.vocalClean === true ? " · clear of vocals" : span.vocalClean === false ? " · crosses vocals" : "";
    say({ action: "holdloop", rule: "safety", why: `HOLD LOOP: ${span.bars} bars, waiting for next song${checked}${vox}` }, pos);
  }

  // ---- live ear hooks (live-ear.js): read the hold loop, reshape it -------
  function holdLoopInfo() {
    const d = deck();
    if (!holdLoop || !holdLoop.looping || !d || !d.loopOn || plan) return null;
    const bar = barSecsOf(d);
    return {
      deck: deckId, start: holdLoop.start, bars: holdLoop.bars, bar,
      rate: (d._playbackRate && d._playbackRate()) || 1,
      secs: nowS() - (holdLoop.since || nowS()),
      washed: !!holdLoop.washed, moved: !!holdLoop.moved,
      // moving the loop a phrase back is a jump back: never on a deck the room hears
      canMove: holdLoop.start - PHRASE_BARS * bar >= Math.max(0, d._mindEntry || 0) - 0.01 && !(d._onAir && d._onAir()),
    };
  }
  // One ear decision. Loop changes land on the next wrap (a grid line), EQ
  // moves ramp over bars; a wash only cuts, so the low end never doubles.
  function holdLoopAct(action, why, source) {
    const d = deck(), info = holdLoopInfo();
    if (!d || !info || action === "keep") return false;
    const id = deckId, { bar, rate } = info;
    const pos = d._currentPosition();
    const wrapIn = ((info.start + info.bars * bar - pos) * 1000) / rate - 10;
    // Snap to the analysed downbeat: bars from BPM alone drift off the grid.
    const db = (d.analysis && d.analysis.downbeat_times) || [];
    const snap = (t) => (db.length ? db.reduce((b, x) => (Math.abs(x - t) < Math.abs(b - t) ? x : b), db[0]) : t);
    const reloop = (start, bars) => later(wrapIn, () => {
      if (deckId !== id || plan || !holdLoop || !d.loopOn) return;
      holdLoop.bars = bars;
      holdLoop.start = loopAt(d, id, snap(start), bars * 4);   // the start actually used
    });
    const voc = (d.analysis && d.analysis.vocal_active_regions) || [];
    const cutsVocal = (t) => voc.some(([s, e]) => s < t - SEAM_VOCAL_GAP_S && e > t + SEAM_VOCAL_GAP_S);
    const moveTo = snap(info.start - PHRASE_BARS * bar);
    // Silent check before the crowd hears it: the moved loop must not score
    // worse than the one playing.
    const pre = host.mod.liveEar && host.mod.liveEar.precheck;
    const moveSeam = pre ? (pre(d, [{ start: moveTo, bars: info.bars }], bar)[0].seam || {}).score : null;
    const moveOk = moveSeam == null || holdLoop.seam == null || moveSeam >= holdLoop.seam - 0.05;
    if (action === "move_loop" && info.canMove && !cutsVocal(moveTo) && !cutsVocal(moveTo + info.bars * bar) && moveOk) {
      if (moveSeam != null) holdLoop.seam = moveSeam;
      reloop(moveTo, info.bars); holdLoop.moved = true;
    } else if (action === "filter_wash" && !info.washed) {
      ramp(eqBand(id, "low"), LOW_KILL, (4 * bar * 1000) / rate);
      ramp(eqBand(id, "mid"), -8, (4 * bar * 1000) / rate);
      holdLoop.washed = true;
    } else if (action === "restore" && info.washed) {
      ramp(eqBand(id, "low"), 0, (bar * 1000) / rate);
      ramp(eqBand(id, "mid"), 0, (bar * 1000) / rate);
      holdLoop.washed = false;
    } else return false;
    holdLoop.touched = true;
    say({ action: "holdloop", source: source || "EAR", why: `${action.replace("_", " ")}: ${why || ""}`.trim() }, pos);
    return true;
  }

  // Famous song + live stems: once per song, strip it to the voice and rebuild
  // it (stem-moves.js, the USB002 move), on a phrase line, with the vocal
  // carrying the bars and room left before the planned exit.
  function stemBreakdownTick(d, pos, bar) {
    const sm = host.mod.stemMoves;
    if (!sm || !d.stemsReady || d._breakdownDone) return false;
    const [phraseStart] = phraseBounds(d.analysis && d.analysis.downbeat_times, phraseAt(d.analysis && d.analysis.downbeat_times, pos, bar), bar);
    if (Math.abs(pos - phraseStart) > bar / 2) return false;          // only right on the line
    const bars = sm.core.breakdownFits({
      bar, pos: phraseStart, duration: d.buffer ? d.buffer.duration : 0,
      exitAt: plan ? plan.fireAt : null, barsOnTrack: (pos - (d._mindEntry || 0)) / bar,
      vocals: d.analysis && d.analysis.vocal_active_regions, famous: !!(d.fame && d.fame.famous), used: false,
    });
    if (!bars) return false;
    // Start on the NEXT line if this one is already past by more than a beat.
    const start = pos - phraseStart > bar / 4 ? phraseStart + PHRASE_BARS * bar : phraseStart;
    if (!sm.breakdown(d, start, bars, `${d.fame.views ? Math.round(d.fame.views / 1e6) + "M views: " : ""}famous, play it in full`)) return false;
    d._breakdownDone = true;
    busyUntil = nowS() + ((start - pos) + bars * bar) / ((d._playbackRate && d._playbackRate()) || 1);
    say({ action: "stem_breakdown", source: "STEMS", why: `STRIP & REBUILD ${bars} bars: drums out, bass out, voice alone, rebuild, drop` }, pos);
    return true;
  }

  // Hook drop: once per song, the beat leaves under the song's emotional lyric
  // line (hook_drop.plan: lyrics, the local model's pick, learned hold length)
  // and slams back on the phrase line after it. Needs live stems; not in a
  // relaxed session; never within 4 bars of the planned exit.
  function hookDropTick(d, pos, bar) {
    const sm = host.mod.stemMoves;
    if (!sm || !sm.hookDrop || !d.stemsReady || d._hookDropDone || !(d.hookDrops && d.hookDrops.length)) return false;
    if (host.session && host.session.relaxed) return false;
    const it = sm.core.hookDropDue(d.hookDrops, pos, bar, plan ? plan.fireAt : null);
    if (!it || !sm.hookDrop(d, it, (it.why || []).join("; "))) return false;
    d._hookDropDone = true;
    busyUntil = nowS() + (it.drop_at - pos + bar) / ((d._playbackRate && d._playbackRate()) || 1);
    say({ action: "hook_drop", source: it.ai ? "AI" : "LYRICS",
          why: `HOOK DROP on "${it.text}": beat out ${(it.drop_at - it.cut_at).toFixed(1)}s, drop on the line` }, pos);
    return true;
  }

  // Remix on the go: on 16-bar lines (every 2nd phrase from the song's entry),
  // a stem move in that section (stem-moves.js remixPick / remixEvents).
  function stemRemixTick(d, pos, bar, phrase) {
    const sm = host.mod.stemMoves;
    if (!sm || !d.stemsReady || !toggleOn("ap-remix-toggle")) return false;
    const entryPhrase = phraseAt(d.analysis && d.analysis.downbeat_times, d._mindEntry || 0, bar);
    if ((phrase - entryPhrase) % 2 !== 0) return false;                  // 16-bar lines only
    const [lineT] = phraseBounds(d.analysis && d.analysis.downbeat_times, phrase, bar);
    if (Math.abs(pos - lineT) > bar / 2) return false;
    const exitT = plan ? plan.fireAt : d.buffer.duration;
    const r = d._remix || (d._remix = { used: [], count: 0, lastAtBar: null });
    const atBar = (lineT - (d._mindEntry || 0)) / bar;
    const kind = sm.core.remixPick({
      vocal: sm.vocalShare(d.analysis && d.analysis.vocal_active_regions, lineT, lineT + 16 * bar),
      used: r.used, count: r.count, barsOnTrack: atBar, barsLeft: (exitT - lineT) / bar, lastAtBar: r.lastAtBar, atBar,
      energy: sm.remixEnergy(d, lineT, bar),
      othersCarry: !!(d._othersCarry && d._othersCarry()),     // a drum break only while the other deck plays too
    });
    if (!kind || !sm.remix(d, lineT, kind, 16)) return false;
    r.used.push(kind); r.count++; r.lastAtBar = atBar;
    busyUntil = nowS() + (16 * bar) / ((d._playbackRate && d._playbackRate()) || 1);
    say({ action: "stem_remix", source: "STEMS", why: `REMIX: ${sm.REMIX_LABEL[kind]} in this 16-bar section` }, pos);
    return true;
  }

  // Learned moves (learned-moves.js: vocal loop / re-cut / chops, loop extend): once per 8-bar line, when the
  // module finds one that passes its gates. Never here during a transition (tick() returned above).
  function learnedLoop(d, id, p, pos, rate) {
    const beat = barSecsOf(d) / 4, L = p.loop.beats, s = p.loop.start;
    const ms = (sec) => Math.max(0, (sec * 1000) / rate);
    later(ms(s - pos), () => { if (deckId === id) loopAt(d, id, s, L); });
    later(ms(s - pos) + ms(L * beat * (1 + p.loop.passes)) - 30, () => { if (deckId === id) loopRelease(d, id, p.loop.release - 0.03 * rate); });
  }
  function learnedMoveTick(d, pos, bar, phrase) {
    const lm = host.mod.learnedMoves;
    if (!lm || !lm.tick) return false;
    const [lineT] = phraseBounds(d.analysis && d.analysis.downbeat_times, phrase, bar);
    if (Math.abs(pos - lineT) > bar / 2) return false;                  // only right on the line
    const id = deckId, rate = (d._playbackRate && d._playbackRate()) || 1;
    const res = lm.tick(d, { pos, bar, phrase, lineT, entryT: d._mindEntry || 0, exitT: plan ? plan.fireAt : (d.buffer ? d.buffer.duration : 0),
      st: state(d, pos), loop: { extend: (p) => learnedLoop(d, id, p, pos, rate) } });
    if (!res) return false;
    busyUntil = nowS() + res.busyS;
    const m = Math.floor(pos / 60), sc = Math.floor(pos % 60);
    log.push({ action: "learned_move", why: res.why, tag: "LEARNED", peak: false, clock: `${m}:${String(sc).padStart(2, "0")}` });
    if (log.length > 20) log.shift();
    render({ action: "learned_move", rule: "", source: "LEARNED", why: res.why });
    return true;
  }

  // Artist moves (artist-moves.js: slip loop, cue tease, roll): between phrase lines, after the line's own
  // decision. A slip loop only in a phrase the mind rides (nothing else booked on the deck); it owns the
  // deck until its release on the next line. Never during a transition / hold loop / layer (tick() returned).
  function artistTick(d, pos, bar, phrase) {
    const am = host.mod.artistMoves;
    if (!am || !am.tick) return false;
    const [, lineT] = phraseBounds(d.analysis && d.analysis.downbeat_times, phrase, bar);
    const res = am.tick(d, { pos, bar, entryT: d._mindEntry || 0, lineT, quiet: phraseQuiet,
      exitT: plan ? plan.fireAt : null, bEntry: plan && Number.isFinite(plan.bEntry) ? plan.bEntry : null,
      holdActive: !!holdLoop, mashupActive: !!(host.mod.mashup && host.mod.mashup.active), fxOk: fxAllowed("roll") });
    if (!res) return false;
    busyUntil = nowS() + res.busyS;
    const m = Math.floor(pos / 60), sc = Math.floor(pos % 60);
    log.push({ action: "artist_move", why: res.why, tag: "ARTIST", peak: false, clock: `${m}:${String(sc).padStart(2, "0")}` });
    if (log.length > 20) log.shift();
    render({ action: "artist_move", rule: "", source: "ARTIST", why: res.why });
    return true;
  }

  function say(dec, pos) {
    const m = Math.floor(pos / 60), s = Math.floor(pos % 60);
    const tag = dec.source || (dec.action === "holdloop" ? "SAFETY" : dec.action !== "ride" ? "RULE" : "");
    log.push({ action: dec.action, why: dec.why, tag, peak: !!dec.peak, clock: `${m}:${String(s).padStart(2, "0")}` });
    if (log.length > 20) log.shift();
    render(dec);
    emitAi("decision", { deck: deckId, pos, action: dec.action, why: dec.why, tag });
  }
  // AI activity bus for the overlays (ai-overlay.js).
  function emitAi(kind, detail) {
    host.bus.emit("ai-activity", { kind, ...detail });
  }
  // What the overlays draw on the waveforms: exit window, hold loop, vocals.
  function overlayState() {
    const d = deck();
    return {
      deck: deckId, planFireAt: plan ? plan.fireAt : null, planStyle: plan ? plan.style : null,
      holdLoop: holdLoop ? { start: holdLoop.start, bars: holdLoop.bars, looping: !!holdLoop.looping,
                             seam: holdLoop.seam, tried: holdLoop.tried, omni: holdLoop.omni } : null,
      bar: d ? barSecsOf(d) : null,
    };
  }

  function tick() {
    const d = deck();
    if (!d || !d.playing) return;
    // The transition owns both decks from onTransition() until follow() hands the
    // mind the new song: with the plan gone the exit guards are gone too, and a
    // hook drop / breakdown here cancelled the merge's booked moves on A, a remix
    // put A's full mix back mid-merge, a strip left A silent under B's fader.
    if (inTransition) return;
    const pos = d._currentPosition();
    if (layerRun) {
      // LAYER running: A unwinds on its own EQ plan; no hold loop, no moves.
      if (nowS() < layerRun.until) return;
      layerRun = null;
    }
    holdLoopTick(d, pos);
    if (holdLoop || nowS() < busyUntil) return;   // a loop is running: no new phrase moves
    const bar = barSecsOf(d);
    // checked every tick, not once per phrase: the cut sits on a lyric line, not a phrase line
    if (lastPhrase !== null && mindOn() && hookDropTick(d, pos, bar)) return;
    const phrase = phraseAt(d.analysis && d.analysis.downbeat_times, pos, bar);
    if (phrase === lastPhrase) { if (mindOn()) artistTick(d, pos, bar, phrase); return; }
    const first = lastPhrase === null;
    lastPhrase = phrase;
    phraseQuiet = false;                          // set true below only when this line's decision is a ride
    if (first) { d._mindEntry = pos; }
    if (!first && mindOn() && stemBreakdownTick(d, pos, bar)) return;
    if (!first && mindOn() && stemRemixTick(d, pos, bar, phrase)) return;
    if (!first && mindOn() && learnedMoveTick(d, pos, bar, phrase)) return;
    const st = state(d, pos);
    if (!aiOn()) st.aiMove = null;
    const dec = mindOn() ? decide(st)
      : { action: "ride", rule: "", why: "SET MIND off - playing the record" };
    apply(dec, st, d);
    phraseQuiet = dec.action === "ride";
    if (dec.action !== "ride" && (dec.action !== "layer" || log.length === 0 || log[log.length - 1].action !== "layer")) {
      say(dec, pos);
    } else render(dec);
  }

  // -- AI plan (one LLM call per song pair; rules re-check every move) -------
  const toggleOn = (id) => { const el = ui.el(id); return !el || el.checked; };
  const mindOn = () => toggleOn("ap-mind-toggle");
  const aiOn = () => mindOn() && toggleOn("ap-ai-toggle");
  const peakOn = () => mindOn() && toggleOn("ap-peak-toggle");
  const esc = (t) => String(t == null ? "" : t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const clock = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;

  function renderPlan(p) {
    const el = ui.el("ap-mind-plan");
    if (!el) return;
    if (!p) { el.innerHTML = ""; el.hidden = true; return; }
    const c = p.candidate || {};
    const moves = (p.moves || []).map((m) =>
      `<li><span class="ap-mind-tag ap-mind-tag-ai">AI</span><b>${LABEL[m.move] || esc(m.move)}</b> ${clock(m.at)}` +
      `${m.bars ? ` · ${m.bars} bars` : ""} <span class="ap-mind-why">${esc(m.reason)}</span></li>`).join("");
    const dropped = (p.dropped || []).length
      ? `<span class="ap-mind-dim" title="${esc(p.dropped.join("\n"))}"> · ${p.dropped.length} rejected by rules</span>` : "";
    el.hidden = false;
    el.innerHTML = `<div><span class="ap-mind-tag ap-mind-tag-ai">AI PLAN</span>` +
      `<b>${esc(c.recipe || "?")}</b> · exit ${p.exit != null ? clock(p.exit) : "?"}` +
      ` <span class="ap-mind-why">${esc((p.reasons && (p.reasons.candidate || p.reasons.exit)) || "")}</span>` +
      `<span class="ap-mind-dim"> · ${esc(p.model || "")} ${p.latency_seconds != null ? p.latency_seconds + "s" : ""}</span>${dropped}</div>` +
      (moves ? `<ol class="ap-mind-planned">${moves}</ol>` : "");
  }

  // One LLM plan per song pair. Resolves to the validated plan, or null when
  // AI ASSIST is off, the call fails, or it misses `deadlineS` (rules only).
  async function requestPlan(currentId, nextId, candidate, win) {
    const d = deck();
    if (!aiOn() || !d || !currentId || !nextId || !win) return null;
    const forTrack = trackIdx, id = deckId;
    const ctrl = new AbortController();
    const waitMs = planWaitMs(win.deadlineS);
    const t = setTimeout(() => ctrl.abort(), waitMs);
    const mode = ui.el("ap-mode");
    renderPlan({ candidate: { recipe: "planning…" }, exit: null, moves: [], reasons: {} });
    try {
      const res = await ai.plan({
          track_a_id: currentId, track_b_id: nextId,
          now: d._currentPosition(), entry: d._mindEntry || 0,
          window_lo: win.lo, window_hi: win.hi,
          set_mode: mode ? mode.value : "hybrid",
          set_position: win.setPosition || 0,
          recent_moves: log.slice(-4).map((l) => l.action),
          remix_used: remixUsed.slice(),
          mashup_possible: !!win.mashupPossible,
          subdrop_last_track: subdropTrackIdx === trackIdx - 1,
          peak_moves: peakOn(),
          big_moment_ok: !(plan && plan.style === "peak") &&
            !bigMomentBlock("fakeout", trackIdx, bigLog, BIG_MIN_BARS, nowS() + 60),
      }, { signal: ctrl.signal });
      const p = await res.json();
      if (!res.ok) throw new Error(p.detail || res.statusText);
      if (trackIdx !== forTrack || deckId !== id) return null;   // song changed meanwhile
      // Moves apply to the CURRENT song (the one playing now).
      aiMoves = (p.moves || []).map((m) => Object.assign({}, m));
      aiFor = trackIdx;
      renderPlan(p);
      return p;
    } catch (e) {
      console.warn("DJ mind plan failed, rules only:", e.name === "AbortError" ? "timeout" : e.message);
      if (trackIdx === forTrack) {
        renderPlan(null);
        say({ action: "ride", rule: "", why: "AI plan unavailable - rules only" }, d._currentPosition());
      }
      return null;
    } finally { clearTimeout(t); }
  }

  // -- public API (called by autopilot.js) -----------------------------------
  function follow(id) {
    deckId = id; lastPhrase = null; inTransition = false;
    trackIdx++; holdsUsed = 0; preCleared = false; instantShown = false; plan = null; profileE = null;
    remixUsed = []; lastRemixPhrase = null; busyUntil = 0; holdLoop = null; layerRun = null;
    if (!timer) timer = setInterval(tick, TICK_MS);
  }
  // LAYER: may this pair layer now? ctx as layerVeto(); adds the set-wide ledger.
  function planLayer(ctx) {
    return layerDecision(Object.assign({}, ctx, { sinceLayer: transitions - lastLayerTransition }));
  }
  // The LAYER booked by planLayer() starts now (call after onTransition()).
  function layering(secs, info = {}) {
    lastLayerTransition = transitions;
    layerRun = { until: nowS() + Math.max(0, secs), source: info.source || "RULE", why: info.why || "" };
    cancelMoves(); holdLoop = null;
    const d = deck();
    say({ action: "layer", rule: "L", source: info.source || "RULE",
          why: info.why || "two records layered" }, d ? d._currentPosition() : 0);
  }
  function stop() {
    cancelMoves();
    if (timer) { clearInterval(timer); timer = null; }
    deckId = null; plan = null; aiMoves = []; aiFor = null; holdLoop = null; layerRun = null; inTransition = false;
    renderPlan(null);
    render({ action: "ride", rule: "", why: "idle" });
  }
  function setPlan(p) {
    plan = p; preCleared = false; instantShown = false; holdsUsed = 0;
    const d = deck();
    if (holdLoop && d && plan) {
      if (!holdLoop.looping) { holdLoop = null; return; }   // never engaged: normal exit
      // Release the safety loop at its next wrap (a phrase line) and fire the
      // transition right there: position runs on past `end` only after release.
      const bar = barSecsOf(d), id = deckId;
      const rate = (d._playbackRate && d._playbackRate()) || 1;
      const end = holdLoop.start + holdLoop.bars * bar;
      const pos = d._currentPosition();
      // An ear wash left mids down: bring them back for the blend (the
      // transition owns the low EQ from here).
      if (holdLoop.washed) ramp(eqBand(id, "mid"), 0, (bar * 1000) / rate);
      // Instrumental hold: the vocal returns on the release line (a handoff
      // transition may take it straight onto the bus instead).
      if (holdLoop.instrumental && host.mod.stemMoves) host.mod.stemMoves.instrumental(d, false, end);
      later(((end - pos) * 1000) / rate - 10, () => { if (deckId === id) loopRelease(d, id, end); });
      plan.fireAt = plan.maxFireAt = end + 0.1;
      say({ action: "holdloop", rule: "safety", why: "next song ready - releasing the loop into the transition" }, pos);
    }
  }
  function fireAt(fallback) { return plan ? plan.fireAt : fallback; }
  function onTransition() {
    if (plan && plan.style === "peak") {
      bigLog.push({ track: trackIdx, kind: plan.peakKind, at: nowS() });
      if (plan.peakKind === "drop_swap") { if (plan.brake) brakesUsed++; lastSwapBraked = !!plan.brake; }
    }
    cancelMoves(); transitions++; plan = null; aiMoves = []; aiFor = null; holdLoop = null;
    inTransition = true;
    renderPlan(null);
  }
  // Rule 8: a pre-crossfade drum fill every other transition at most, never on
  // an instant swap (the swap itself is the event).
  function fxAllowed(kind) {
    if (host.session && host.session.relaxed) return false;   // relaxed: no fills, no FX
    if (kind !== "fill") return true;
    if (plan && (plan.style === "instant" || plan.style === "peak")) return false;
    if (transitions - lastFillTransition < 2) return false;
    lastFillTransition = transitions;
    return true;
  }
  function noteEnergy(e) { if (Number.isFinite(e)) energies.push(e); }
  // -> {note, hook}: hook is the motif's display name when note is "reprise"
  // (research/notes/set-study-mDtud5fLgFQ.md section 5), else null.
  function nextEnergyNote(setPos, history) {
    const n = energyNote(energies, setPos, callbackDone, { history, repriseAsked });
    if (n === "callback") callbackDone = true;
    let hook = null;
    if (n === "reprise") {
      const m = motifHook(history);
      repriseAsked = `${m.key}:${m.plays}`;     // ask once per play count
      hook = m.title;
    }
    return { note: n, hook };
  }
  // Peak transition for the booked pair (autopilot.js scheduleTransition hook).
  // ctx: {drop (blend plan, entry_mode "drop"), lo, hi, plannedExit, entryPos, inDeck}
  function planPeak(ctx) {
    if (host.session && host.session.relaxed) return null;       // relaxed: never a peak swap
    const d = deck();
    if (!d || !ctx || !ctx.drop) return null;
    const a = d.analysis || {}, bar = barSecsOf(d);
    const inn = host.decks && host.decks[ctx.inDeck];
    const b = (inn && inn.analysis) || {};
    const bE = aDropLines(inn || {}, barSecsOf(inn)).find((x) => Math.abs(x.t - ctx.drop.entry) <= barSecsOf(inn));
    const mode = ui.el("ap-mode");
    return peakTransition({
      peakOn: peakOn(),
      // song-level only: a transition changes the vibe, so "has a drop" is not enough
      peak: (Number.isFinite(profileE) && profileE >= PEAK_PROFILE_ENERGY) || (mode && mode.value === "quick"),
      drop: ctx.drop, aDrops: aDropLines(d, bar),
      aVocal: a.vocal_active_regions, lo: ctx.lo, hi: ctx.hi, plannedExit: ctx.plannedExit,
      entryPos: ctx.entryPos || 0, bar,
      keyScore: camelotScore(a.key && a.key.camelot, b.key && b.key.camelot),
      bDropEnergy: bE ? bE.energy : (ctx.drop.entry_energy != null ? ctx.drop.entry_energy : null),
      log: bigLog, trackIdx, now: nowS(), brakesUsed, lastSwapBraked,
    });
  }
  function setProfileEnergy(e) { profileE = Number.isFinite(e) ? (e <= 1 ? e * 10 : e) : null; }
  function reset() { stop(); energies.length = 0; callbackDone = false; repriseAsked = null; log.length = 0;
                     bigLog.length = 0; brakesUsed = 0; lastSwapBraked = false;
                     boostTrackIdx = -9; lastBoostAt = -Infinity; profileE = null;
                     trackIdx = 0; subdropTrackIdx = -9; lastMoveAt = -Infinity;
                     transitions = 0; lastFillTransition = -9; lastLayerTransition = -9; layerRun = null; }

  return { follow, stop, reset, setPlan, fireAt, onTransition, fxAllowed,
                    noteEnergy, nextEnergyNote, requestPlan, planPeak, setProfileEnergy,
                    planLayer, layering, get layerActive() { return !!layerRun; }, core,
                    holdLoopInfo, holdLoopAct, overlayState,
                    get transitioning() { return inTransition; },
                    busy: () => inTransition || !!holdLoop || !!layerRun };   // artist-moves.js run-now safety gate
  }
  if (root.Engine) root.Engine.mount("djMind", create);
})(typeof window !== "undefined" ? window : globalThis);
