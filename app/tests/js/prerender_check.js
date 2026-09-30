// Node check for the pre-render readiness planner (app/ui/static/autopilot.js autopilotCore).
const assert = require("assert");
const c = require("../../ui/static/autopilot.js");

// -- tempi to render ahead: A now and A native, folded to B, only inside the key-lock cap and past 2 %
assert.deepStrictEqual(c.prerenderTargets({ aEff: 124, aNative: 124, bBpm: 120, lim: 0.08 }), [124]);
assert.deepStrictEqual(c.prerenderTargets({ aEff: 126.2, aNative: 123, bBpm: 120, lim: 0.08 }), [126, 123]);   // A gliding home: both
assert.deepStrictEqual(c.prerenderTargets({ aEff: 126.2, aNative: 121, bBpm: 120, lim: 0.08 }), [126]);      // native inside 2 %: pitch locks it
assert.deepStrictEqual(c.prerenderTargets({ aEff: 120.5, aNative: 120.9, bBpm: 120, lim: 0.08 }), []);       // pitch fader locks it: nothing to render
assert.deepStrictEqual(c.prerenderTargets({ aEff: 140, aNative: 140, bBpm: 120, lim: 0.08 }), []);          // past the cap
assert.deepStrictEqual(c.prerenderTargets({ aEff: 248, aNative: 248, bBpm: 120, lim: 0.08 }), [124]);        // double time folds
assert.deepStrictEqual(c.prerenderTargets({ aEff: 124.1, aNative: 123.9, bBpm: 120, lim: 0.08 }), [124]);    // 0.5 BPM steps, unique
assert.deepStrictEqual(c.prerenderTargets({ aEff: 124, aNative: 124, bBpm: 0, lim: 0.08 }), []);

// -- what the booking waits for
const base = { aStems: true, bStems: true, aEff: 124, bBpm: 124, tempoStemsBpm: null, lim: 0.08, keyScore: 0.9 };
assert.deepStrictEqual(c.readinessNeeds(base), { needs: [], skip: null });
assert.deepStrictEqual(c.readinessNeeds({ ...base, bStems: false }).needs, ["stems"]);
assert.deepStrictEqual(c.readinessNeeds({ ...base, bBpm: 120 }).needs, ["tempo stems"]);                      // 3.3 % gap, none rendered
assert.deepStrictEqual(c.readinessNeeds({ ...base, bBpm: 120, bStems: false }).needs, ["stems", "tempo stems"]);
assert.deepStrictEqual(c.readinessNeeds({ ...base, bBpm: 120, tempoStemsBpm: 124 }).needs, []);               // matches A's tempo
assert.deepStrictEqual(c.readinessNeeds({ ...base, bBpm: 120, tempoStemsBpm: 118 }).needs, ["tempo stems"]); // rendered for another tempo
assert.ok(c.readinessNeeds({ ...base, aStems: false, bStems: false }).skip);                                  // A has none: pointless to wait
assert.ok(c.readinessNeeds({ ...base, bStems: false, keyScore: 0.3 }).skip.includes("keys clash"));
assert.ok(c.readinessNeeds({ ...base, bStems: false, bBpm: 100 }).skip.includes("over the cap"));
assert.strictEqual(c.readinessNeeds({ ...base, bStems: false, keyScore: null }).needs[0], "stems");           // unknown key: wait, the merge gate decides

// -- A still easing home: wait for it (the tempo stems are for A's HOME tempo), never chase the moving tempo
assert.deepStrictEqual(c.readinessNeeds({ ...base, aSettled: false }).needs, ["A's tempo home"]);
assert.deepStrictEqual(c.readinessNeeds({ ...base, aSettled: true }).needs, []);
assert.deepStrictEqual(c.readinessNeeds({ ...base, bBpm: 120, aSettled: false }).needs, ["tempo stems", "A's tempo home"]);
assert.deepStrictEqual(c.aTempoAtEntry({ bpm: 124, rate: 1.03, pitchPct: 3 }), { bpm: 124, settled: false });       // easing: its own tempo
assert.deepStrictEqual(c.aTempoAtEntry({ bpm: 124, rate: 1.0004, pitchPct: 0.04 }), { bpm: 124 * 1.0004, settled: true });
assert.deepStrictEqual(c.aTempoAtEntry({ bpm: 124, rate: 0.97, pitchPct: -3 }), { bpm: 124, settled: false });
assert.deepStrictEqual(c.aTempoAtEntry({ bpm: 124, rate: 1 }), { bpm: 124, settled: true });

// -- bounded wait: to the earliest exit minus the lead the plan needs, never past the cap
assert.strictEqual(c.deferBudgetS({ nowPos: 20, exitLo: 200 }), 100);                                         // 135 s room, capped
assert.strictEqual(c.deferBudgetS({ nowPos: 20, exitLo: 100 }), 35);                                          // 80 - 45
assert.strictEqual(c.deferBudgetS({ nowPos: 90, exitLo: 100 }), 0);                                           // no room: decide now
assert.strictEqual(c.deferBudgetS({ nowPos: 20, exitLo: 200, minLeadS: 60, maxS: 30 }), 30);
assert.deepStrictEqual(c.deferDecision({ needs: [], waitedS: 0, budgetS: 50 }), { wait: false, why: "ready" });
assert.deepStrictEqual(c.deferDecision({ needs: ["stems"], waitedS: 10, budgetS: 50 }), { wait: true, why: "waiting for stems" });
assert.deepStrictEqual(c.deferDecision({ needs: ["stems", "tempo stems"], waitedS: 10, budgetS: 50 }).why, "waiting for stems and tempo stems");
assert.deepStrictEqual(c.deferDecision({ needs: ["tempo stems"], waitedS: 50, budgetS: 50 }), { wait: false, why: "gave up after 50 s waiting for tempo stems" });
assert.strictEqual(c.deferDecision({ needs: ["stems"], waitedS: 0, budgetS: 0 }).wait, false);                // zero budget: never waits

// -- readiness bonus: bounded, stable, taste wins beyond one place
const ready = new Set(["c", "d"]);
const isR = (x) => ready.has(x);
assert.deepStrictEqual(c.orderByReadiness(["a", "b", "c", "d"], isR), ["a", "c", "d", "b"]);                  // c and d each pass ONE not-ready song
assert.deepStrictEqual(c.orderByReadiness(["a", "b", "c", "d"], isR, 2), ["c", "d", "a", "b"]);              // wider bonus, still bounded
assert.deepStrictEqual(c.orderByReadiness(["a", "b", "c", "d", "e"], (x) => x === "e"), ["a", "b", "c", "e", "d"]);   // one place, not to the head
assert.deepStrictEqual(c.orderByReadiness(["a", "b", "c"], () => true), ["a", "b", "c"]);                     // all ready: untouched
assert.deepStrictEqual(c.orderByReadiness(["a", "b", "c"], () => false), ["a", "b", "c"]);                    // none ready: untouched
assert.deepStrictEqual(c.orderByReadiness(["a", "b"], () => true, 0), ["a", "b"]);
assert.deepStrictEqual(c.orderByReadiness([], isR), []);
const src = ["a", "b", "c"]; c.orderByReadiness(src, isR); assert.deepStrictEqual(src, ["a", "b", "c"]);       // input not mutated

console.log("prerender core ok");
