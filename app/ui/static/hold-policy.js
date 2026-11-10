// G17 long merges + holds: hold lengths, the recipe mix rule layer, stem level matching, hold safety and set telemetry.
// Mirror of dj-core lib/console/hold_policy.dart and app/music_brain/hold_policy.py (same numbers, same functions);
// every number is a dj-config key. Pure functions, no DOM; also require()-able in node.
if (typeof DJ_CONFIG === "undefined" && typeof require === "function") require("./generated/dj_config.js");   // node: browsers load it via index.html
(function (root) {
  "use strict";

  const MERGE_HOLD_MIN_BARS = DJ_CONFIG.MERGE_HOLD_MIN_BARS, MERGE_HOLD_MAX_BARS = DJ_CONFIG.MERGE_HOLD_MAX_BARS;
  const MERGE_HOLD_TARGET_S = DJ_CONFIG.MERGE_HOLD_TARGET_S, HOLD_ALIGN_BARS = DJ_CONFIG.HOLD_ALIGN_BARS;
  const INSONG_HOLD_MIN_BARS = DJ_CONFIG.INSONG_HOLD_MIN_BARS, INSONG_HOLD_MAX_BARS = DJ_CONFIG.INSONG_HOLD_MAX_BARS;
  const INSONG_HOLDS_ON = DJ_CONFIG.INSONG_HOLDS_ON, STEM_MERGE_SHARE_MIN = DJ_CONFIG.STEM_MERGE_SHARE_MIN;
  const ACCENT_MAX_SHARE = DJ_CONFIG.ACCENT_MAX_SHARE, ACCENT_RECIPES = DJ_CONFIG.ACCENT_RECIPES;
  const STEM_MATCH_MAX_BOOST_DB = DJ_CONFIG.STEM_MATCH_MAX_BOOST_DB, HOLD_END_ENERGY_DROP = DJ_CONFIG.HOLD_END_ENERGY_DROP;
  const STEM_MERGE = "Stem Merge";
  const STEMS = ["drums", "bass", "vocals", "other"];
  const SHARE = { drums: 0.45, bass: 0.45, vocals: 0.35, other: 0.35 };
  const SILENT_BIN = 0.002, DROPOUT_FLOOR = 0.5;
  const MERGE_BARS_RANGE = [16, 128], INSONG_BARS_RANGE = [8, 64];

  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);

  // The per-device override (phone Settings "Merges & holds", sent as `hold_settings` with plan / suggest requests).
  // Keys: merge_hold_bars (0 = auto, 16-128), insong_hold_bars (0 = auto, 8-64), stem_merge_share, accent_cap (0-1),
  // insong_holds (bool). Anything malformed falls back to the dj-config default; bounds are never widened.
  function settings(j) {
    const o = j && typeof j === "object" ? j : {};
    const bars = (v, r) => (typeof v === "number" && Number.isFinite(v) && v > 0 ? clamp(Math.round(v), r[0], r[1]) : 0);
    const share = (v, d) => (typeof v === "number" && Number.isFinite(v) ? clamp(v, 0, 1) : d);
    const s = {
      mergeHoldBars: bars(o.merge_hold_bars, MERGE_BARS_RANGE), inSongHoldBars: bars(o.insong_hold_bars, INSONG_BARS_RANGE),
      stemMergeShare: share(o.stem_merge_share, STEM_MERGE_SHARE_MIN), accentCap: share(o.accent_cap, ACCENT_MAX_SHARE),
      inSongHolds: typeof o.insong_holds === "boolean" ? o.insong_holds : INSONG_HOLDS_ON,
    };
    s.mergeMin = s.mergeHoldBars || MERGE_HOLD_MIN_BARS; s.mergeMax = s.mergeHoldBars || MERGE_HOLD_MAX_BARS;
    s.inSongMin = s.inSongHoldBars || INSONG_HOLD_MIN_BARS; s.inSongMax = s.inSongHoldBars || INSONG_HOLD_MAX_BARS;
    return s;
  }

  // Merge-hold bars: target seconds / bar seconds snapped down to the 16-bar grid, clamped to the settings' min..max, capped by roomBars
  // on the same grid; with less than one grid step of room, whole 8-bar phrases (0 = no room).
  function mergeHoldBars(barS, roomBars, s) {
    s = s || settings();
    if (!(barS > 0) || !(roomBars >= 8)) return 0;
    const want = Math.floor(MERGE_HOLD_TARGET_S / barS);
    const bars = clamp(Math.floor(want / HOLD_ALIGN_BARS) * HOLD_ALIGN_BARS, s.mergeMin, s.mergeMax);
    const cap = Math.floor(roomBars / HOLD_ALIGN_BARS) * HOLD_ALIGN_BARS;
    if (bars <= cap) return bars;
    return cap >= HOLD_ALIGN_BARS ? cap : Math.floor(roomBars / 8) * 8;
  }

  // In-song hold (merge_hold / remix_synth_hold / filter_loop) for a section: longest of min..max on the 8-bar grid, 0 when off or too short.
  function inSongHoldBars(sectionBars, s) {
    s = s || settings();
    if (!s.inSongHolds) return 0;
    const fit = Math.floor(sectionBars / 8) * 8;
    return fit < s.inSongMin ? 0 : Math.min(s.inSongMax, fit);
  }

  const isAccent = (r) => !!r && ACCENT_RECIPES.includes(r);

  // Rule layer over the LLM / matcher pick. history = recipes so far (oldest first), mergeFits = Stem Merge passes key / BPM / vocal checks.
  // -> { recipe, why } (why null when the pick stands).
  function enforceRecipeMix(proposed, history, mergeFits, s, accentFallback) {
    s = s || settings();
    const n = history.length + 1;
    const merges = history.filter((r) => r === STEM_MERGE).length, accents = history.filter(isAccent).length;
    const lastAccent = history.length > 0 && isAccent(history[history.length - 1]);
    if (proposed !== STEM_MERGE && mergeFits && merges / n < s.stemMergeShare) {
      return { recipe: STEM_MERGE, why: `merge share ${Math.round(100 * merges / n)} % under ${Math.round(100 * s.stemMergeShare)} %` };
    }
    if (isAccent(proposed)) {
      const over = lastAccent || accents + 1 > Math.ceil(s.accentCap * n - 1e-9);
      if (over) {
        const why = lastAccent ? "accent twice in a row" : `accent cap ${Math.round(100 * s.accentCap)} %`;
        return { recipe: mergeFits ? STEM_MERGE : (accentFallback || "Basic Blend"), why };
      }
    }
    return { recipe: proposed, why: null };
  }

  const meanOf = (arr, from, to) => {
    if (!Array.isArray(arr) || !arr.length) return 0;
    const v = arr.slice(Math.min(from, arr.length), Math.min(to, arr.length));
    return v.length ? v.reduce((a, b) => a + b, 0) / v.length : 0;
  };

  // Level-match the held stems before the merge vet. combo: stem -> "a" | "b". The loudest stem relative to its typical mix share sets the
  // target; quieter stems are lifted toward it, at most STEM_MATCH_MAX_BOOST_DB. Silent stems are left alone. -> { eA, eB, trim }
  function levelMatch(eA, eB, combo, bars, maxBoostDb) {
    const cap = 10 ** ((maxBoostDb == null ? STEM_MATCH_MAX_BOOST_DB : maxBoostDb) / 20);
    const rel = {};
    for (const n of STEMS) {
      const m = meanOf((combo[n] === "a" ? eA : eB)[n], 0, bars);
      if (m > 0) rel[n] = m / SHARE[n];
    }
    const target = Math.max(0, ...Object.values(rel));
    const trim = {};
    for (const n of STEMS) trim[n] = rel[n] ? clamp(target / rel[n], 1, cap) : 1;
    // a dropout bin (silent while the stem's section is loud) is matched to a fraction of the stem's own level; a stem silent over the whole window stays silent
    const lift = (k, arr) => arr.map((v) => (v <= SILENT_BIN ? (rel[k] || 0) * SHARE[k] * trim[k] * DROPOUT_FLOOR : v * trim[k]));
    const scaled = (src, deck) => {
      const out = {};
      for (const k of Object.keys(src)) out[k] = STEMS.includes(k) && combo[k] === deck && Array.isArray(src[k]) ? lift(k, src[k]) : src[k];
      return out;
    };
    return { eA: scaled(eA, "a"), eB: scaled(eB, "b"), trim };
  }

  // Should a running long hold end at the next phrase boundary? -> reason | null
  function holdEndReason(o) {
    if (o.userAction) return "user action";
    if (o.vocalClash) return "vocal clash";
    if (o.energyAtStart > 0 && o.energyNow < o.energyAtStart * HOLD_END_ENERGY_DROP) return "energy drop";
    return null;
  }
  const holdEndBar = (bar, phraseBars) => { const p = phraseBars || 8; return Math.floor((bar + p - 1e-9) / p) * p; };

  // Watches one running long hold (bars counted from the line). check({bar, vocalClash, energy, userAction}) -> the phrase-grid bar the
  // hold must end at, or null while it runs on; once decided it stays decided. A boundary at or past the hold's own end is no early end.
  function holdGuard(fromBar, toBar, phraseBars) {
    let energyAtStart = null;
    const g = {
      endBar: null, reason: null,
      check(o) {
        if (g.endBar != null) return g.endBar;
        if (o.bar < fromBar || o.bar >= toBar) return null;
        if (energyAtStart == null) energyAtStart = o.energy;
        const why = holdEndReason({ vocalClash: o.vocalClash, energyNow: o.energy, energyAtStart, userAction: o.userAction });
        if (!why) return null;
        const end = holdEndBar(o.bar, phraseBars);
        if (end >= toBar) return null;
        g.reason = why;
        return (g.endBar = end);
      },
    };
    return g;
  }

  // Per-set telemetry from the set log: [{ recipe, holdBars, holdS, early }] -> merge share, hold count, hold seconds.
  function holdTelemetry(log) {
    const n = log.length, merges = log.filter((t) => t.recipe === STEM_MERGE).length, holds = log.filter((t) => t.holdBars > 0);
    const r3 = (x) => Math.round(x * 1000) / 1000;
    return {
      transitions: n, merge_share: n ? r3(merges / n) : 0, hold_count: holds.length,
      hold_seconds: Math.round(holds.reduce((a, t) => a + (t.holdS || 0), 0) * 10) / 10,
      accent_share: n ? r3(log.filter((t) => isAccent(t.recipe)).length / n) : 0, early_ends: holds.filter((t) => t.early).length,
    };
  }

  const core = { MERGE_HOLD_MIN_BARS, MERGE_HOLD_MAX_BARS, MERGE_HOLD_TARGET_S, HOLD_ALIGN_BARS, INSONG_HOLD_MIN_BARS, INSONG_HOLD_MAX_BARS,
    INSONG_HOLDS_ON, STEM_MERGE_SHARE_MIN, ACCENT_MAX_SHARE, ACCENT_RECIPES, STEM_MATCH_MAX_BOOST_DB, HOLD_END_ENERGY_DROP,
    settings, mergeHoldBars, inSongHoldBars, isAccent, enforceRecipeMix, levelMatch, holdEndReason, holdEndBar, holdGuard, holdTelemetry };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.HoldPolicy = core;
})(typeof window !== "undefined" ? window : globalThis);
