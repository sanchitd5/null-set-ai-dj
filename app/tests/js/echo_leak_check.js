// node app/tests/js/echo_leak_check.js: an on-demand move (PLAY STEP -> performNow, MERGE -> HOLD ->
// mergeNow) runs executeTransition just before its line, never at the press. Owner, Neverland ->
// Nocturnal Echo Out: A carried the deck echo (wet 0.7, 3/4-beat delay) from the press (stem-preview
// --full: A's 0:00) to the crossfade at 2:52, because the echo case's setFx ran when performNow did.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const ap = require("../../ui/static/autopilot.js");

// 1) the fire delay: lookahead before the line, clamped at now, 0 on a bad clock
assert.strictEqual(ap.fireDelayMs(175.875, 5.25, 150), (175.875 - 5.25) * 1000 - 150, "press 170 s early: fire 150 ms before the line");
assert.strictEqual(ap.fireDelayMs(10, 9.95, 150), 0, "line inside the lookahead: now");
assert.strictEqual(ap.fireDelayMs(10, 12, 150), 0, "line passed: now");
assert.strictEqual(ap.fireDelayMs(NaN, 1, 150), 0);
assert.strictEqual(ap.fireDelayMs(3, 1), 2000, "no lookahead given: on the line");

// 2) the seams: performNow / mergeNow never call executeTransition outside fireOnLine, and
// fireOnLine waits fireDelayMs with the booking's XF_LOOKAHEAD_MS (scheduleTransition does the same)
const src = fs.readFileSync(path.join(__dirname, "../../ui/static/autopilot.js"), "utf8");
const body = (name) => {
  const i = src.indexOf(`function ${name}(`);
  assert.ok(i > 0, `${name} exists`);
  const j = src.indexOf("\n  }\n", i);
  return src.slice(i, j);
};
for (const name of ["performNow", "mergeNow"]) {
  const b = body(name);
  const calls = b.split("executeTransition(").length - 1;
  assert.strictEqual(calls, 1, `${name}: one executeTransition call`);
  const fire = b.indexOf("fireOnLine(o.t0, () => {");
  assert.ok(fire > 0 && fire < b.indexOf("executeTransition("), `${name}: executeTransition runs inside fireOnLine`);
  assert.ok(!/const totalMs = executeTransition\([^)]*\);\s*\n\s*(const ran|later\()/.test(b.slice(0, fire)), `${name}: nothing fires at the press`);
}
assert.ok(/function fireOnLine\(t0, fn\) \{\s*later\(autopilotCore\.fireDelayMs\(t0, audioCtx\.currentTime, XF_LOOKAHEAD_MS\), fn\);/.test(src),
  "fireOnLine: booking lookahead on the audio clock");
// the echo case still arms the deck echo when it runs (it is the timing that moved, not the move)
const echoCase = src.slice(src.indexOf('case "echo":'), src.indexOf('case "echo":') + 700);
assert.ok(/setFx\(out, "echo", 0\.7\)/.test(echoCase));
console.log("echo leak OK");
