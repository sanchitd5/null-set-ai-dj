// Punjabi scene profile, console half. The values are owned by app/music_brain/analysis/scene_profile.py
// (evidence and GUESS marks there: research/notes/punjabi-original-sets.md section 7);
// this is a parity-tested copy (app/tests/py/test_scene_profile.py runs it in node).
//
// core  pure and node-testable (app/tests/js/scene_profile_check.js)
// mode  the PUNJABI setting: "off" = today's behaviour, "on" = every transition,
//       "auto" = the songs decide (both Punjabi -> "full", exactly one side -> "handover")
(function (root) {
  "use strict";

  const MODES = ["auto", "on", "off"];
  const DEFAULT_MODE = "auto";
  const PUNJABI_PROFILE = Object.freeze({
    name: "punjabi",
    scene_terms: ["punjabi", "bhangra", "desi", "punjabi hip hop", "punjabi pop"],   // note s7 same_scene_terms
    neighbour_terms: ["bollywood"],                                                  // note s7 neighbour
    max_era_gap: 4,                  // note s7 (GUESS)
    play_seconds: [45, 90],          // note s7 target_play_seconds (GUESS rule, SOURCED lengths)
    fallback_recipe: "Quick Cut",    // note s7: a refused tonal blend -> Quick Cut on beat 1 (GUESS)
    cuts_skip_key_bypass: true,      // note s7: cuts skip the key gate (GUESS)
    bpm_octave_fold: true,           // note s7: 176 == 88 (GUESS)
    repeat_anthems_after_min: 45,    // note s7 (GUESS); not wired
    learned_clash_min_obs: 3,        // full level: a learned tonal blend on a key clash needs 3 clashing sightings in Punjabi sets (GUESS)
    learned_tempo_cap: 0.08,         // ... and a learned move never past the keylock cap (tempo-rule.js KEYLOCK_RANGE_PCT)
  });
  const FULL = "full", HANDOVER = "handover";

  const normalizeMode = (m) => { const v = String(m == null ? "" : m).trim().toLowerCase(); return MODES.includes(v) ? v : DEFAULT_MODE; };
  function hasTerm(label, terms) {
    const g = " " + String(label == null ? "" : label).toLowerCase().replace(/[^a-z0-9&]+/g, " ").split(" ").filter(Boolean).join(" ") + " ";
    return terms.some((t) => g.includes(` ${t} `));
  }
  const isPunjabi = (label) => hasTerm(label, PUNJABI_PROFILE.scene_terms);
  const isNeighbour = (label) => hasTerm(label, PUNJABI_PROFILE.neighbour_terms);

  // A -> B: "full" | "handover" | null (no profile)
  function level(mode, genreA, genreB) {
    const m = normalizeMode(mode);
    if (m === "off") return null;
    if (m === "on") return FULL;
    const a = isPunjabi(genreA), b = isPunjabi(genreB);
    if (a && b) return FULL;
    return a !== b ? HANDOVER : null;
  }
  // the fallback when decideRecipe would echo out: a refused tonal blend (key rewrite) or no tempo lock
  function fallbackRecipe(recipe, lvl, why) {
    if (!lvl || recipe !== "Echo Out" || !why) return recipe;
    return PUNJABI_PROFILE.fallback_recipe;
  }
  // full profile: short snippets (in on the hook, out before verse 2); the steering bridge still wins
  function playWindow(lvl, steering) {
    if (lvl !== FULL || steering === "move") return null;
    const [min, max] = PUNJABI_PROFILE.play_seconds;
    return { min, max, xf: 8, label: "PUNJABI" };
  }
  // B's tempo moved by x2 / x0.5 to sit nearest A (the profile scores 88 vs 176 as one feel)
  function foldBpm(a, b) {
    if (!(a > 0) || !(b > 0)) return b;
    return [1, 2, 0.5].map((m) => b * m).reduce((best, x) => (Math.abs(a / x - 1) < Math.abs(a / best - 1) ? x : best));
  }
  // Learned moves (scene_profile.py learned_scene / learned_clash_ok / learned_tempo_ok).
  // Only the full level reads the Punjabi-tagged sets' sightings.
  const learnedScene = (lvl) => (lvl === FULL ? PUNJABI_PROFILE.name : null);
  // a tonal learned blend on a key clash: full level and enough clashing sightings in the scene's sets
  const learnedClashOk = (lvl, clashObs) => lvl === FULL && Math.trunc(Number(clashObs) || 0) >= PUNJABI_PROFILE.learned_clash_min_obs;
  // full level: a learned move plays only inside the keylock cap (octave-folded gap), else the fallback
  function learnedTempoOk(lvl, foldedGap) {
    if (lvl !== FULL || foldedGap == null) return true;
    return Number(foldedGap) <= PUNJABI_PROFILE.learned_tempo_cap + 1e-9;
  }
  // status text: the mode, and when auto is active which level fired
  function statusLabel(mode, lvl) {
    const m = normalizeMode(mode);
    if (m === "off") return "";
    if (!lvl) return m === "auto" ? "" : "PUNJABI profile on";
    return `Punjabi profile active (${m === "on" ? "on" : "auto"}, ${lvl})`;
  }

  // OWNER RULE "dhol drop only for punjabi songs, shouldn't experiment": the ONE gate for every dhol / bhangra
  // sample or pattern (beat-grid-ai.js presets, artist-moves.js dhol_drop, the sampler's AI use). The playing
  // song must resolve to the Punjabi scene, and in a transition (genreB given) the incoming one too. Whatever
  // the PUNJABI mode says: "on" forces the profile's mixing, never a dhol on a song that is not Punjabi.
  // -> {ok, why}
  function dholOk(genreA, genreB) {
    if (!isPunjabi(genreA)) return { ok: false, why: `dhol only on Punjabi songs: the playing song is ${genreA ? `"${genreA}"` : "unlabelled"}` };
    if (genreB !== undefined && !isPunjabi(genreB)) return { ok: false, why: `dhol only between Punjabi songs: the incoming song is ${genreB ? `"${genreB}"` : "unlabelled"}` };
    return { ok: true, why: "Punjabi scene" };
  }

  const core = { MODES, DEFAULT_MODE, PUNJABI_PROFILE, FULL, HANDOVER, normalizeMode, isPunjabi, isNeighbour, level,
    fallbackRecipe, playWindow, foldBpm, statusLabel, learnedScene, learnedClashOk, learnedTempoOk, dholOk };
  root.sceneProfileCore = core;   // beat-grid-ai.js / auto-sampler.js read the dhol gate before the Engine mounts
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (root.Engine) root.Engine.mount("sceneProfile", () => core);
})(typeof window !== "undefined" ? window : globalThis);
