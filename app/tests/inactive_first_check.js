// node app/tests/inactive_first_check.js — static scan: every AI-driven tempo
// move ramps (deck.rampPitchPercent / deck.aiSetPitch), never a bare
// deck.setPitchPercent(...) jump. No deck, active or inactive, gets an
// instant/stepped tempo change; a silent deck may glide fast (a few ms) but
// still goes through the ramp path. Exceptions: the method's own definition,
// rampPitchPercent's internal not-playing fallback, and the user's own pitch
// fader / SYNC button (their control, never slowed).
const fs = require("fs");
const path = require("path");

const DIR = path.join(__dirname, "../ui/static");
const FILES = ["deck-controller.js", "stem-moves.js", "ai-actions.js", "automation.js", "autopilot.js", "riff-over-rap.js"];

// file -> line numbers (1-based) allowed to call the bare setter directly.
const ALLOW = {
  "deck-controller.js": [
    790,  // setPitchPercent(pct) { ... }            -- the definition itself
    410,  // rampPitchPercent's own not-playing fallback -- deck isn't producing sound yet
    820,  // aiSetPitch's fallback when tempoGlideSeconds() found no real change (g === 0)
    1467, // SYNC button -- the user's own control
    1528, // pitch fader input handler -- the user's own control
  ],
  "autopilot.js": [
    1816, // setDeckPitch's defensive fallback when d.aiSetPitch doesn't exist (never true in practice)
  ],
};

let bad = 0;
for (const file of FILES) {
  const full = path.join(DIR, file);
  if (!fs.existsSync(full)) continue;
  const lines = fs.readFileSync(full, "utf8").split("\n");
  const allow = new Set(ALLOW[file] || []);
  lines.forEach((line, i) => {
    const n = i + 1;
    if (allow.has(n)) return;
    // A bare instant setter: `<something>.setPitchPercent(` not preceded by "ramp"
    // and not the method definition (`setPitchPercent(pct) {`).
    if (/\bsetPitchPercent\(/.test(line) && !/rampPitchPercent\(/.test(line) && !/^\s*setPitchPercent\(/.test(line)) {
      console.log(`BAD ${file}:${n}: bare setPitchPercent (must ramp) -- ${line.trim()}`);
      bad++;
    }
  });
}
console.log(bad ? `${bad} failures` : "inactive-first: every AI tempo move ramps, ok");
process.exit(bad ? 1 : 0);
