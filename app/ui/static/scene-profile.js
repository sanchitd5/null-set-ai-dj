// Punjabi scene profile, console half. The values are owned by app/music_brain/scene_profile.py
// (evidence and GUESS marks there: research/notes/punjabi-original-sets.md section 7);
// this is a parity-tested copy (app/tests/test_scene_profile.py runs it in node).
//
// core  pure and node-testable (app/tests/scene_profile_check.js)
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
  // status text: the mode, and when auto is active which level fired
  function statusLabel(mode, lvl) {
    const m = normalizeMode(mode);
    if (m === "off") return "";
    if (!lvl) return m === "auto" ? "" : "PUNJABI profile on";
    return `Punjabi profile active (${m === "on" ? "on" : "auto"}, ${lvl})`;
  }

  const core = { MODES, DEFAULT_MODE, PUNJABI_PROFILE, FULL, HANDOVER, normalizeMode, isPunjabi, isNeighbour, level,
    fallbackRecipe, playWindow, foldBpm, statusLabel };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (root.Engine) root.Engine.mount("sceneProfile", () => core);
})(typeof window !== "undefined" ? window : globalThis);
