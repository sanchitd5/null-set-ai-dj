// G23: the ONE candidate-acceptance function for the console. Gates (order, hard / soft, parameters, refusal text) are
// data in DJ_CONFIG.PICK_RULES; this file only evaluates them. A mechanical port of dj-core lib/booking/accept.dart and
// app/music_brain/pick_accept.py; the golden vectors fail the tests when any port drifts.
// acceptCandidate(candidate, state, rules?, hooks?) -> {ok, reason}. hooks = {leavesScene(scene, genre, profile),
// eraGap(a, b)}; with no hooks the scene and era_jump gates are skipped (the console has no genre tables).
(function (root) {
  "use strict";
  if (typeof DJ_CONFIG === "undefined" && typeof require === "function") require("./generated/dj_config.js");   // node: browsers load it via index.html

  const norm = (s) => String(s == null ? "" : s).toLowerCase().split(/\s+/).filter(Boolean).join(" ");
  const toSet = (xs) => new Set((xs || []).map(norm));
  const hits = (ids, known) => known.size > 0 && ids.some((i) => i && known.has(i));
  const mins = (s) => `${Math.floor(s / 60 + 0.5)} min`;
  const fmt = (t, v) => t.replace(/\{(\w+)\}/g, (m, k) => (k in v ? v[k] : m));
  const why = (reason, gate) => (reason ? reason : gate.why_default);

  function acceptCandidate(candidate, state, rules, hooks) {
    const r = rules || root.DJ_CONFIG.PICK_RULES;
    const c = candidate || {}, s = state || {};
    const ids = (c.ids || []).map(norm);
    const playing = toSet(s.playing_ids), played = toSet(s.played), vetoed = toSet(s.vetoed);
    const relax = new Set((s.relax || []).map(String));
    const limit = s.max_length_s != null ? Number(s.max_length_s) : Number(r.max_track_secs);
    const dur = c.duration_s != null ? Number(c.duration_s) : null;
    const v = c.lang_verdict;
    const profile = s.punjabi_profile || "off";
    for (const g of r.gates) {
      const id = g.id;
      if (g.hard !== true && relax.has(id)) continue;
      let refuse = false, vars = {};
      switch (id) {
        case "playing": refuse = hits(ids, playing); break;
        case "played": refuse = hits(ids, played); break;
        case "vetoed": refuse = hits(ids, vetoed); break;
        case "not_music": refuse = c.not_music === true; break;
        case "length_unknown": refuse = dur !== null && dur <= 0; break;
        case "too_long":
          refuse = dur !== null && dur > 0 && dur >= limit;
          if (refuse) vars = { dur: mins(dur), limit: mins(limit) };
          break;
        case "language_block": refuse = v === "block"; vars = { why: why(c.lang_reason, g) }; break;
        case "language_unverified": refuse = v === "unverified" && s.language_strict === true; vars = { why: why(c.lang_reason, g) }; break;
        case "language_penalty": refuse = v === "penalty"; vars = { why: why(c.lang_reason, g) }; break;
        case "scene": {
          if (!hooks || !hooks.leavesScene) break;       // no genre tables: skipped
          const scene = s.scene_genre;
          refuse = !!scene && !!hooks.leavesScene(scene, c.genre == null ? null : c.genre, profile);
          vars = { scene: scene || "", genre: c.genre || g.genre_default };
          break;
        }
        case "era_jump": {
          if (!hooks || !hooks.eraGap) break;            // no genre tables: skipped
          const gap = hooks.eraGap(s.cur_era == null ? null : s.cur_era, c.era == null ? null : c.era);
          refuse = gap != null && gap > r.max_era_gap;
          vars = { era: c.era || "", cur_era: s.cur_era || "" };
          break;
        }
        default: throw new Error(`pick_rules: unknown gate ${id}`);
      }
      if (refuse) return { ok: false, reason: fmt(g.reason, vars) };
    }
    return { ok: true, reason: null };
  }

  const api = { acceptCandidate };
  root.PickAccept = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
