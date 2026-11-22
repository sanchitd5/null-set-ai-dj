// Prints the stem plans the web executor builds (stem-moves.js) over a grid of inputs, as [{label, events, fader, span}] JSON.
// app/tests/py/test_stem_ownership_plans.py hands every one to dj-core's stem_overlaps (the stem-only ownership rule lives there only).
const sm = require("../../ui/static/stem-moves.js");
const ap = require("../../ui/static/autopilot.js");
const out = [];
for (const kind of ["bass", "blend", "filter", "loop", "double"]) {
  for (const bars of [8, 16, 24, 32, 48]) {
    for (const aSings of [false, true]) for (const bSings of [false, true]) for (const keyClash of [false, true]) {
      for (const intro of [undefined, "drums", "other"]) for (const keepA of [false, true]) {
        const plan = sm.stemBlendPlan(kind, bars, aSings, bSings, keyClash, { intro, keepA });
        out.push({ label: `blend ${kind} ${bars} a${+aSings} b${+bSings} k${+keyClash} ${intro || "auto"} keepA${+keepA}`,
          events: plan, fader: ap.stemBlendFader(kind, bars, 1), span: bars });
      }
    }
  }
}
for (const combo of sm.mergeCombos()) {
  for (const M of [16, 24, 32]) for (const keyClash of [false, true]) {
    const p = sm.mergeTransitionPlan(M, combo, keyClash);
    out.push({ label: `merge ${JSON.stringify(combo)} M${M} k${+keyClash}`, events: p.events, fader: sm.mergeFader(M), span: p.total });
  }
}
process.stdout.write(JSON.stringify(out));
