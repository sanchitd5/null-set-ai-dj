// AI Music Brain - the ONE drop-line rule for every vocal-layering path.
//
// OWNER RULE "never vocal mix a drop line": no vocal from either deck (acapella over, mashup vocal, riff over
// rap, stem merge / hold carrying the other deck's vocal, learned vocal loop / chop / re-cut, hook drop, artist
// vocal moves) may play over a song's drop, or over its drop vocal line (the sung line that runs into the drop).
//
// Pure (node-tested: app/tests/js/drop_line_check.js). Python twin: app/music_brain/render/drop_line.py, same fixture
// (app/tests/fixtures/drop_line_cases.json) drives both so they cannot drift.
(function (root) {
  "use strict";

  const fin = (x) => typeof x === "number" && Number.isFinite(x);
  // dj-mind.js dropLines (= blend.py drop_lines), resolved at call time: dj-mind loads after this file
  function defaultDropLines() {
    let c = root.djMindCore;
    if (!c && typeof require === "function") { try { c = require("./dj-mind.js"); } catch (e) { c = null; } }
    return c && typeof c.dropLines === "function" ? c.dropLines : null;
  }

  // drop spans [[t0, t1]] overlapping [t0, t1) -> the first one, else null
  function dropHit(drops, t0, t1) {
    for (const x of drops || []) if (Array.isArray(x) && Math.min(t1, x[1]) - Math.max(t0, x[0]) > 1e-6) return x;
    return null;
  }

  // a vocal region that is live inside [t0, at) and runs on past `at` into a drop starting at `at`
  function dropVocalLine(drops, vocals, t0, at) {
    const d = (drops || []).find((x) => Array.isArray(x) && Math.abs(x[0] - at) < 1e-3);
    if (!d) return null;
    return (vocals || []).find((r) => Array.isArray(r) && r[1] > at + 1e-3 && r[0] < at && r[1] > t0) || null;
  }

  // A vocal layered over [t0, t1) of this song: null when clear, else {gate, reason}.
  // NARROW (owner: "more quality mashups and transitions, not less"): only the drop window (DROP_WINDOW_BARS
  // from the drop line) and the sung line running into it count, and only while the layered vocal really sounds
  // there: sings(lo, hi) -> seconds the layered vocal is singing inside [lo, hi) of THIS song's clock (null: the
  // move is the vocal itself, e.g. a chop or a loop, so it always sounds). At least SING_MIN_S of it refuses.
  // drops not an array = the song has no drop map: nothing can be ruled out, so it refuses ("unmeasured").
  const SING_MIN_S = 1.0;
  function dropLineBusy(drops, vocals, t0, t1, sings = null) {
    if (!Array.isArray(drops)) return { gate: "unmeasured", reason: "no drop map: cannot rule out a drop line" };
    const sounds = (lo, hi) => hi - lo > 1e-6 && (typeof sings !== "function" || sings(lo, hi) >= Math.min(SING_MIN_S, hi - lo) - 1e-6);
    for (const x of drops) {
      if (Array.isArray(x) && sounds(Math.max(t0, x[0]), Math.min(t1, x[1])))
        return { gate: "drop_line", reason: `the vocal sings over the drop at ${x[0].toFixed(1)} s: never vocal mix a drop line` };
    }
    const vl = dropVocalLine(drops, vocals, t0, t1);
    if (vl && sounds(Math.max(t0, vl[0]), t1))
      return { gate: "drop_line", reason: `the sung line ${vl[0].toFixed(1)}-${vl[1].toFixed(1)} s runs into the drop: never vocal mix a drop line` };
    return null;
  }
  // sings() for a vocal from another song laid over this one: regions on that song's clock, mapped linearly
  // (this song's s -> that song's s: at0 + (u - t0) * ratio, ratio = that song's seconds per this song's second)
  function mappedSings(regions, t0, at0, ratio) {
    return (lo, hi) => {
      const a = at0 + (lo - t0) * ratio, b = at0 + (hi - t0) * ratio;
      let s = 0;
      for (const r of regions || []) if (Array.isArray(r)) s += Math.max(0, Math.min(b, r[1]) - Math.max(a, r[0]));
      return ratio > 0 ? s / ratio : 0;
    };
  }

  // Drop windows of one analysed song: the first DROP_WINDOW_BARS after each of dj-mind's energy drop lines, and of
  // each section-map "drop" label at least MIN_SECTION_DROP_BARS long. The analyzer's labels flicker (1-3 s "drop"
  // slivers all over a song, blend.py drop_lines): a sliver is not a drop, or every vocal move would be refused.
  // dropLinesFn = dj-mind core dropLines (phrases, times, curve, bar) -> [{t}], or null.
  const MIN_SECTION_DROP_BARS = 2, DROP_WINDOW_BARS = 2;
  function dropSpans(analysis, bpm, dropLinesFn) {
    const a = analysis || {}, out = [];
    const bar = 240 / (bpm || a.bpm || 128), W = DROP_WINDOW_BARS * bar;
    for (const x of a.sections || []) {
      if (x && /drop/i.test(String(x.label || "")) && fin(x.start) && fin(x.end) && x.end - x.start >= MIN_SECTION_DROP_BARS * bar - 1e-6) out.push([x.start, Math.min(x.end, x.start + W)]);
    }
    if (dropLinesFn === undefined) dropLinesFn = defaultDropLines();
    if (typeof dropLinesFn === "function") {
      for (const x of dropLinesFn(a.phrase_boundaries_8bar, a.energy_times, a.energy_curve, bar) || []) if (x && fin(x.t)) out.push([x.t, x.t + W]);
    }
    return out;
  }

  // dropLineBusy over one deck's song (deck-like: {analysis, bpm}); sings / dropLinesFn as above.
  // With no analysis the drop map is missing: refused as "unmeasured".
  function deckBusy(d, t0, t1, sings = null, dropLinesFn) {
    const a = d && d.analysis;
    if (!a) return dropLineBusy(null, null, t0, t1);
    return dropLineBusy(dropSpans(a, d.bpm || a.bpm, dropLinesFn), a.vocal_active_regions, t0, t1, sings);
  }

  const core = { MIN_SECTION_DROP_BARS, DROP_WINDOW_BARS, SING_MIN_S, dropHit, dropVocalLine, dropLineBusy, mappedSings, dropSpans, deckBusy };
  root.dropLineCore = core;
  if (typeof module !== "undefined" && module.exports) module.exports = core;
})(typeof window !== "undefined" ? window : globalThis);
