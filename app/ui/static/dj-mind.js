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

  // state -> { action, why, rule }. One move per phrase; order is priority.
  function decide(s) {
    const exitNear = s.barsToExit != null;
    if (exitNear) {
      const holdCap = s.setMode === "quick" ? 1 : 2;
      if (s.section === "build" && s.barsToExit <= HOLD_BARS + PRECLEAR_SLACK_BARS &&
          s.holdsUsed < holdCap && s.holdRoomBars >= HOLD_BARS) {
        return { action: "hold", rule: "4.1", why: "build running into the exit - let it resolve first" };
      }
      if (s.overlapStyle === "instant") {
        if (!s.instantShown && s.barsToExit <= PHRASE_BARS + PRECLEAR_SLACK_BARS) {
          return { action: "instant", rule: "1", why: "records meet on a drop - bass swaps on one downbeat" };
        }
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
    return { action: "ride", rule: "", why: s.section ? `riding the ${s.section}` : "riding the record" };
  }

  // Rule 9 (dip after a long peak) outranks rule 7 (late callback).
  function energyNote(energies, setPos, callbackDone) {
    const e = (energies || []).filter(Number.isFinite);
    if (e.length >= 2 && e.slice(-2).every((v) => v >= PEAK_ENERGY)) return "dip";
    if (!callbackDone && setPos >= CALLBACK_SET_POS) return "callback";
    return null;
  }

  const core = { decide, mergeSections, sectionAt, phraseAt, vocalShare, subdropBars, energyNote,
                 PRECLEAR_DB, LOW_KILL };
  root.djMindCore = core;
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof document === "undefined") return;

  // ------------------------------------------------------------------ runtime
  const TICK_MS = 250;
  const LABEL = { hold: "HOLD", preclear: "PRE-CLEAR", instant: "INSTANT SWAP",
                  subdrop: "SUB DROP", layer: "LAYER", ride: "RIDE" };

  let deckId = null, timer = null, timers = [];
  let lastPhrase = null, trackIdx = 0, subdropTrackIdx = -9;
  let lastMoveAt = -Infinity;                   // performance.now() seconds
  let holdsUsed = 0, preCleared = false, instantShown = false;
  let plan = null;                              // {fireAt, maxFireAt, style, preClearBars}
  let lastFillTransition = -9, transitions = 0;
  const energies = []; let callbackDone = false;
  const log = [];

  const nowS = () => performance.now() / 1000;
  const deck = () => (deckId && window.decks ? window.decks[deckId] : null);
  const barSecsOf = (d) => 240 / ((d && d.bpm) || 128);

  function eqLow(id) { return document.querySelector(`.eq-knob[data-deck="${id}"][data-band="low"]`); }
  function setKnob(el, v) {
    if (!el) return;
    el.value = String(v);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
  function later(ms, fn) { const t = setTimeout(fn, Math.max(0, ms)); timers.push(t); }
  function ramp(el, to, ms) {
    if (!el) return;
    const from = parseFloat(el.value) || 0, steps = 16;
    for (let i = 1; i <= steps; i++) later((ms * i) / steps, () => setKnob(el, from + (to - from) * (i / steps)));
  }
  function cancelMoves() { timers.forEach(clearTimeout); timers = []; }

  function render(dec) {
    const nowEl = document.getElementById("ap-mind-now");
    const logEl = document.getElementById("ap-mind-log");
    const panel = document.getElementById("ap-mind");
    if (!nowEl) return;
    nowEl.innerHTML = `<span class="ap-mind-act ap-mind-${dec.action}">${LABEL[dec.action]}</span>` +
      `<span class="ap-mind-why">${dec.why}${dec.rule ? ` · rule ${dec.rule}` : ""}</span>`;
    if (panel && dec.action !== "ride") {
      panel.classList.remove("ap-mind-flash"); void panel.offsetWidth; panel.classList.add("ap-mind-flash");
    }
    if (logEl) {
      logEl.innerHTML = log.slice(-4).reverse()
        .map((l) => `<li><b>${LABEL[l.action]}</b> ${l.clock} ${l.why}</li>`).join("");
    }
  }

  function state(d, pos) {
    const a = d.analysis || {};
    const bar = barSecsOf(d);
    const rate = (d._playbackRate && d._playbackRate()) || 1;
    const long = mergeSections(a.sections, bar);
    const sec = sectionAt(long, pos);
    const mode = document.getElementById("ap-mode");
    return {
      barSecs: bar,
      section: sec ? sec.label : null,
      sectionEnergy: sec ? sec.energy : 0.5,
      sectionBarsLeft: sec ? (sec.end - pos) / bar : 0,
      vocalAhead: vocalShare(a.vocal_active_regions, pos, pos + PHRASE_BARS * bar),
      barsOnTrack: (pos - (d._mindEntry || 0)) / bar,
      barsToExit: plan ? (plan.fireAt - pos) / bar : null,
      holdRoomBars: plan ? (plan.maxFireAt - plan.fireAt) / bar : 0,
      overlapStyle: plan ? plan.style : null,
      preClearBars: plan ? plan.preClearBars : 8,
      preCleared, instantShown, holdsUsed,
      mashupActive: !!(window.mashup && window.mashup.active),
      subdropThisTrack: subdropTrackIdx === trackIdx,
      subdropLastTrack: subdropTrackIdx === trackIdx - 1,
      secsSinceMove: nowS() - lastMoveAt,
      setMode: mode ? mode.value : "hybrid",
      rate,
    };
  }

  function apply(dec, st) {
    const bar = st.barSecs, rate = st.rate;
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
      const el = eqLow(deckId), id = deckId;
      ramp(el, LOW_KILL, (bar / 4) * 1000 / rate);                 // one beat down
      later((dec.bars * bar * 1000) / rate, () => {                  // snap back on the line
        if (deckId === id) setKnob(el, 0);
      });
    }
  }

  function tick() {
    const d = deck();
    if (!d || !d.playing) return;
    const pos = d._currentPosition();
    const bar = barSecsOf(d);
    const phrase = phraseAt(d.analysis && d.analysis.downbeat_times, pos, bar);
    if (phrase === lastPhrase) return;
    const first = lastPhrase === null;
    lastPhrase = phrase;
    if (first) { d._mindEntry = pos; }
    const st = state(d, pos);
    const dec = decide(st);
    apply(dec, st);
    if (dec.action !== "ride" && (dec.action !== "layer" || log.length === 0 || log[log.length - 1].action !== "layer")) {
      const m = Math.floor(pos / 60), s = Math.floor(pos % 60);
      log.push({ action: dec.action, why: dec.why, clock: `${m}:${String(s).padStart(2, "0")}` });
      if (log.length > 20) log.shift();
    }
    render(dec);
  }

  // -- public API (called by autopilot.js) -----------------------------------
  function follow(id) {
    deckId = id; lastPhrase = null;
    trackIdx++; holdsUsed = 0; preCleared = false; instantShown = false; plan = null;
    if (!timer) timer = setInterval(tick, TICK_MS);
  }
  function stop() {
    cancelMoves();
    if (timer) { clearInterval(timer); timer = null; }
    deckId = null; plan = null;
    render({ action: "ride", rule: "", why: "idle" });
  }
  function setPlan(p) { plan = p; preCleared = false; instantShown = false; holdsUsed = 0; }
  function fireAt(fallback) { return plan ? plan.fireAt : fallback; }
  function onTransition() { cancelMoves(); transitions++; plan = null; }
  // Rule 8: a pre-crossfade drum fill every other transition at most, never on
  // an instant swap (the swap itself is the event).
  function fxAllowed(kind) {
    if (kind !== "fill") return true;
    if (plan && plan.style === "instant") return false;
    if (transitions - lastFillTransition < 2) return false;
    lastFillTransition = transitions;
    return true;
  }
  function noteEnergy(e) { if (Number.isFinite(e)) energies.push(e); }
  function nextEnergyNote(setPos) {
    const n = energyNote(energies, setPos, callbackDone);
    if (n === "callback") callbackDone = true;
    return n;
  }
  function reset() { stop(); energies.length = 0; callbackDone = false; log.length = 0;
                     trackIdx = 0; subdropTrackIdx = -9; lastMoveAt = -Infinity;
                     transitions = 0; lastFillTransition = -9; }

  window.djMind = { follow, stop, reset, setPlan, fireAt, onTransition, fxAllowed,
                    noteEnergy, nextEnergyNote, core };
})(typeof window !== "undefined" ? window : globalThis);
