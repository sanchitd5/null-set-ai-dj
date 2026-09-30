// node app/tests/js/macro_perform_check.js: a macro step is PERFORMED, not re-decided.
// The forced plan (macro-mode.js defaultPlan / playStep) goes through autopilot.js
// forcedBooking (scheduleTransition's seam): stored recipe, exit, entry and merge hold;
// a gate refusal falls back (never a cut) and says which gate; the re-pick paths in
// scheduleTransition / evaluateCandidate are skipped for a forced plan; PLAY MACRO
// advances step after step; clip files are never a step's audio.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const ap = require("../../ui/static/autopilot.js");
const mm = require("../../ui/static/macro-mode.js");

const A = "38a2671a2840eb7e", B = "b4141543d66ac3f2", C = "c83e697be787dcd7";
const macro = { name: "studied-set-synth", steps: [
  { n: 1, a: A, b: B, a_name: "Bittermind - Resonance", b_name: "Shakedown - At Night", recipe: "Stem Merge", a_time: 61.068, b_time: 45.186,
    merge: { hold_bars: 14, M: 16, pick: { label: "A drums + A bass + B vox + B synth" }, phases: { handover: { bars: 8 } } } },
  { n: 2, a: B, b: C, a_name: "Shakedown - At Night", b_name: "Cassian - Dun Dun", recipe: "Echo Out", a_time: 241.255, b_time: 17.995 },
] };
const phraseS = 32 * 60 / 124;   // 8 bars at 124 BPM

// 1) forcedBooking books EXACTLY the stored move: recipe, exit, entry, and asks the merge for the stored hold + combo
{
  const calls = [];
  const forced = mm.forcedOf(macro.steps[0], { source: "macro", macro: macro.name });
  const planMerge = (aT, bT, prefer) => { calls.push({ aT, bT, prefer }); return { M: prefer.M, phases: { hold: { bars: prefer.M - 2 } } }; };
  const fb = ap.forcedBooking({ forced, nowPos: 30, phraseS, trackEnd: 300, liveATime: 92, liveBTime: 0, beat: true, stemsBoth: true,
    keyScore: 0.9, mashupFits: true, mergeOn: true, planMerge, mergeGate: () => null });
  assert.strictEqual(fb.recipe, "Stem Merge");
  assert.strictEqual(fb.aT, 61.068, "stored exit, not the live pick (92)");
  assert.strictEqual(fb.bT, 45.186, "stored entry");
  assert.strictEqual(fb.refused, null);
  assert.deepStrictEqual(calls, [{ aT: 61.068, bT: 45.186, prefer: { entry: 45.186, M: 16, label: "A drums + A bass + B vox + B synth" } }]);
  assert.strictEqual(fb.line, "macro: performing step 1: Bittermind - Resonance -> Shakedown - At Night Stem Merge (exit 1:01, entry 0:45), hold 14 bars");
  // the forced plan is not changed by the booking
  assert.strictEqual(forced.recipe, "Stem Merge"); assert.strictEqual(forced.a_time, 61.068);
}

// 2) the song moved past the stored exit: the next valid phrase on the stored point's grid
{
  const fx = ap.forcedExit({ aTime: 61.068, nowPos: 70, phraseS, trackEnd: 300 });
  assert.ok(Math.abs(fx - (61.068 + phraseS)) < 1e-9 && fx >= 74, `snapped ${fx}`);
  assert.strictEqual(ap.forcedExit({ aTime: 61.068, nowPos: 299, phraseS, trackEnd: 300 }), null, "A ends first: live timing");
  assert.strictEqual(ap.forcedExit({ aTime: 61.068, nowPos: 10, phraseS, trackEnd: 300 }), 61.068);
}

// 3) gate refusals fall back to the closest allowed move, never a cut, and name the gate
{
  const base = { forced: mm.forcedOf(macro.steps[0]), nowPos: 30, phraseS, trackEnd: 300, liveATime: 92, liveBTime: 0,
    beat: true, stemsBoth: true, keyScore: 0.9, mashupFits: false, mergeOn: true, mergeGate: () => "vocal_clash: no clean hold" };
  const refusedMerge = ap.forcedBooking(Object.assign({}, base, { planMerge: () => null }));
  assert.strictEqual(refusedMerge.recipe, "Bass Swap");
  assert.strictEqual(refusedMerge.refused, "merge: vocal_clash: no clean hold");
  assert.ok(/\[refused Stem Merge: merge: vocal_clash/.test(refusedMerge.line));
  assert.strictEqual(refusedMerge.aT, 61.068, "the fallback still plays at the stored points");
  const noStems = ap.forcedBooking(Object.assign({}, base, { stemsBoth: false, planMerge: () => assert.fail("no merge without stems") }));
  assert.strictEqual(noStems.refused, "stems: stems missing on a deck"); assert.strictEqual(noStems.recipe, "Bass Swap");
  const tempo = ap.forcedBooking(Object.assign({}, base, { beat: false, planMerge: () => null }));
  assert.strictEqual(tempo.recipe, "Echo Out");
  const key = ap.forcedRecipe({ want: "Long Blend", beat: true, stemsBoth: true, keyScore: 0.3 });
  assert.deepStrictEqual(key, { recipe: "Echo Out", refused: "key: camelot 0.3" });
  for (const want of ["Cut", "hard cut", ""]) assert.notStrictEqual(ap.forcedRecipe({ want, beat: true }).recipe.toLowerCase(), "cut");
  assert.deepStrictEqual(ap.forcedRecipe({ want: "Echo Out", beat: false, stemsBoth: false, keyScore: 0 }), { recipe: "Echo Out", refused: null });
  assert.deepStrictEqual(ap.forcedRecipe({ want: "Stem Bridge", beat: false, stemsBoth: true }), { recipe: "Stem Bridge", refused: null });
  assert.strictEqual(ap.forcedRecipe({ want: "Mashup → Transition", beat: true, stemsBoth: true, keyScore: 0.9, mashupFits: false }).refused,
    "mashup: the pair does not fit a mashup");
}

// 3b) Echo Out stays exactly where CLAUDE.md s4 needs it (key clash, tempo gap) or where it was
// planned; any other refusal takes the key-and-tempo-safe blend (Bass Swap), never an Echo Out.
{
  const tr = require("../../ui/static/tempo-rule.js");
  // a planned / stored Echo Out is performed as stored, even when a blend would be allowed
  for (const o of [{ beat: true, stemsBoth: true, keyScore: 1 }, { beat: false, stemsBoth: false, keyScore: 0 }]) {
    assert.deepStrictEqual(ap.forcedRecipe(Object.assign({ want: "Echo Out" }, o)), { recipe: "Echo Out", refused: null });
  }
  const echoStep = { n: 10, a: A, b: B, recipe: "Echo Out", why: "" };
  assert.deepStrictEqual(mm.stepGate({ step: echoStep, aId: A, bId: B, aStems: true, bStems: true, aEff: 128, bBpm: 128,
    keyScore: 1, tempoRule: tr, keySafe: ap.keySafeRecipe }), { ok: true, recipe: "Echo Out", why: "" }, "macro Echo Out step plays Echo Out");
  // fallback order: key clash -> Echo Out, tempo gap -> Echo Out, stems / mashup / merge / vocal -> Bass Swap
  const f = (want, o) => ap.forcedRecipe(Object.assign({ want, beat: true, stemsBoth: true, keyScore: 0.9, mashupFits: true, mergeOk: true }, o));
  assert.strictEqual(f("Mashup → Transition", { keyScore: 0.3, stemsBoth: false }).recipe, "Echo Out", "key clash keeps Echo Out");
  assert.strictEqual(f("Long Blend", { keyScore: 0.3 }).recipe, "Echo Out");
  assert.strictEqual(f("Long Blend", { beat: false }).recipe, "Echo Out", "tempo gap keeps Echo Out");
  assert.strictEqual(f("Mashup → Transition", { stemsBoth: false }).recipe, "Bass Swap", "stems missing: blend, not Echo Out");
  assert.strictEqual(f("Mashup → Transition", { mashupFits: false }).recipe, "Bass Swap", "mashup refusal: blend");
  assert.strictEqual(f("Stem Merge", { mergeOk: false }).recipe, "Bass Swap", "merge hold gate: blend");
  assert.strictEqual(f("Long Blend", { vocalRule: { why: "two voices", recipe: "Bass Swap" } }).recipe, "Bass Swap", "vocal rule: blend");
  assert.strictEqual(f("Mashup → Transition", { stemsBoth: false, keyScore: null }).recipe, "Bass Swap", "unknown key: blend");
  // golden: three owner-liked pairs (rows copied from the real pair atlas, read only)
  const gold = JSON.parse(fs.readFileSync(path.join(__dirname, "../fixtures/owner_liked_pairs.json"), "utf8"));
  const want = { "dd3f0c201c0c0f03>407c498ddd3a6dab": "Long Blend", "407c498ddd3a6dab>c4a392ce13e82bc9": "Stem Merge",
    "c4a392ce13e82bc9>3ef9ad4b3fd01c79": "Echo Out" };
  for (const [k, recipe] of Object.entries(want)) {
    const p = gold.pairs[k];
    assert.ok(p, `fixture has ${k}`);
    const r = ap.forcedRecipe({ want: recipe, beat: p.lock !== "none", stemsBoth: p.stems[0] && p.stems[1], keyScore: p.key,
      mashupFits: false, mergeOk: !!p.merge.ok });
    assert.deepStrictEqual(r, { recipe, refused: null }, `${k} plays ${recipe} as planned`);
  }
  // the atlas's own recipe job (pair_atlas_rules.js: decideRecipe on the row's facts) still gives the stored recipe
  for (const [k, p] of Object.entries(gold.pairs)) {
    const fa = gold.tracks[p.a].bpm, fbpm = gold.tracks[p.b].bpm, r0 = tr.lockRate(fa, fbpm);
    const both = p.stems[0] && p.stems[1], lockGap = Math.abs(r0 - 1);
    const d = ap.decideRecipe({ recipe: "Long Blend", blend: p.blend && p.blend.ok ? Object.assign({}, p.blend) : null, layer: false,
      aStems: p.stems[0], bStems: p.stems[1], aEff: fa, bBpm: fbpm, tempoStemsBpm: both && lockGap <= 0.08 ? fbpm * r0 : null,
      keyScore: p.key, mashupFits: () => !!p.mashup.ok }, tr);
    assert.strictEqual(d.recipe, p.recipe, `${k}: atlas recipe unchanged`);
  }
  // MACRO step Echo Out (Neverland -> Nocturnal, key 0): booked as stored, played through the
  // "echo" case, which is where the VOCAL THROW (fx-moves.js) fires; nothing refused
  const nv = gold.pairs["c4a392ce13e82bc9>3ef9ad4b3fd01c79"];
  const eo = ap.forcedBooking({ forced: mm.forcedOf({ n: 10, a: nv.a, b: nv.b, recipe: "Echo Out", a_time: nv.exit, b_time: nv.entry }),
    nowPos: 0, phraseS, trackEnd: 400, liveATime: 1, liveBTime: 2, beat: true, stemsBoth: true, keyScore: nv.key,
    mashupFits: false, mergeOn: true, mergeGate: () => null });
  assert.strictEqual(eo.recipe, "Echo Out"); assert.strictEqual(eo.refused, null);
  assert.strictEqual(eo.aT, nv.exit, "stored exit kept");
  assert.strictEqual(ap.recipeKind(eo.recipe), "echo");
  const apSrc = fs.readFileSync(path.join(__dirname, "../../ui/static/autopilot.js"), "utf8");
  const echoCase = apSrc.slice(apSrc.indexOf('case "echo":'), apSrc.indexOf('case "echo":') + 700);
  assert.ok(/fxMoves\.vocalThrow\(out, inn/.test(echoCase), "the echo case still offers the vocal throw");
  // No Control -> Neverland: a refused merge still falls to the Bass Swap the owner liked
  const nn = gold.pairs["407c498ddd3a6dab>c4a392ce13e82bc9"];
  assert.strictEqual(ap.forcedRecipe({ want: "Stem Merge", beat: true, stemsBoth: true, keyScore: nn.key, mergeOk: false }).recipe, "Bass Swap");
}

// 4) the seams: a forced plan skips every re-pick path (source scan of the booking)
{
  const src = fs.readFileSync(path.join(__dirname, "../../ui/static/autopilot.js"), "utf8");
  const sched = src.slice(src.indexOf("function scheduleTransition("), src.indexOf("function scheduleTransition(") + 30000);
  for (const guard of ["const peakT = !forced", "if (pp && !forced", "if (!forced && !layer && !peakT && learnedOn())",
    "host.mod.djMind && !forced) fireAt", "if (forced) {", "autopilotCore.forcedBooking("]) {
    assert.ok(sched.includes(guard), `scheduleTransition: ${guard}`);
  }
  for (const guard of ["deferPlan || candidate.forced ? null : requestMindPlan", "blend && !candidate.forced ? await requestLayer",
    "if (!layer && !candidate.forced) tryMashup", "if (!layer && !candidate.forced) {\n      apStatus(`Ear pre-planning"]) {
    assert.ok(src.includes(guard), `evaluateCandidate: ${guard}`);
  }
}

// 5) macro-mode: defaultPlan marks macro / FOLLOW SET / studied-combo candidates FORCED with the stored step
(async () => {
  const logs = [], els = {};
  const el = (id) => els[id] || (els[id] = { id, value: "", dataset: {}, checked: false, textContent: "", hidden: true, innerHTML: "", addEventListener() {}, click() { els.__started = true; } });
  const fetched = [];
  const host = {
    ui: { el, flag: (id, d) => d, status() {} }, clock: { now: () => 0, setTimeout() {} },
    random: { next: () => 0.5, uuid: () => "x" }, log: { step: (k, o) => logs.push([k, o.decision, o.why]) },
    bus: { on() {}, emit() {} }, mod: { autopilotState: { active: false, activeDeck: "a" } }, state: { trackA: A, trackB: null },
    decks: { a: { buffer: {}, isPlaying: true, bpm: 124, analysis: {} }, b: {} },
    api: { fetch: async (url) => { fetched.push(url); return { ok: true, json: async () => (url === "/api/macros" ? { macros: [{ name: macro.name, songs: 3 }] }
      : url.startsWith("/api/macros/") ? { macro, validation: [] } : url.startsWith("/api/atlas/partners") ? { partners: [] } : url === "/api/studied/sets" ? { sets: [] } : {}) }; } },
  };
  const rt = mm.createRuntime({ host });
  await new Promise((r) => setTimeout(r, 0)); await new Promise((r) => setTimeout(r, 0));
  const step1 = macro.steps[0];
  const c = rt.defaultPlan(A, { track_id: B, name: step1.b_name, _macro: { name: macro.name, step: step1 } }, { recipe: "Long Blend", a_time: 92, b_time: 0 });
  assert.strictEqual(c.recipe, "Stem Merge");
  assert.deepStrictEqual(c.forced && [c.forced.n, c.forced.recipe, c.forced.a_time, c.forced.b_time, c.forced.merge.M], [1, "Stem Merge", 61.068, 45.186, 16]);
  const fol = rt.defaultPlan(A, { track_id: B, name: step1.b_name, _follow: { set_id: "x", position: 2 } }, { recipe: "Long Blend", a_time: 92, b_time: 0 });
  assert.ok(fol.forced && fol.forced.source === "follow set" && fol.forced.recipe === "Stem Merge" && fol.forced.b_time === 45.186, "FOLLOW SET pick performs the stored move");
  const plain = rt.defaultPlan(A, { track_id: C, name: "x" }, { recipe: "Long Blend", a_time: 92, b_time: 0 });
  assert.ok(!plain.forced, "an ordinary pool pick is not forced");

  // 6) PLAY MACRO: every booking takes the next step, the cursor advances on landing, then it ends
  await rt.loadMacro(macro.name);
  await rt.playMacro();
  assert.ok(rt.running && els.__started, "PLAY MACRO starts the autopilot");
  assert.strictEqual(els["macro-play"].textContent, "STOP MACRO");
  assert.deepStrictEqual(rt.upcoming().map((x) => x.track_id), [B, C], "the next songs are pre-rendered first");
  const f1 = await rt.firstCandidates(A, { played: [], recent: [] });
  assert.deepStrictEqual([f1.length, f1[0].track_id, f1[0]._macro.step.n], [1, B, 1]);
  rt.landed(A, B, { recipe: "Stem Merge" });
  const f2 = await rt.firstCandidates(B, { played: [A], recent: [] });
  assert.deepStrictEqual([f2[0].track_id, f2[0]._macro.step.n], [C, 2]);
  const forced2 = rt.defaultPlan(B, f2[0], { recipe: "Bass Swap", a_time: 200, b_time: 0 }).forced;
  assert.deepStrictEqual([forced2.recipe, forced2.a_time, forced2.b_time], ["Echo Out", 241.255, 17.995]);
  rt.landed(B, C, { recipe: "Echo Out" });
  await rt.firstCandidates(C, { played: [A, B], recent: [] });
  assert.ok(!rt.running, "the macro is done");
  assert.ok(logs.some(([k, d]) => k === "macro" && d === "PLAY MACRO end"));

  // 7) the panel shows songs, move, points and hold, never a clip; no clip file is ever fetched as audio
  const html = els["macro-steps"].innerHTML;
  assert.ok(/Bittermind - Resonance → Shakedown - At Night/.test(html) && /Stem Merge/.test(html) && /exit 1:01 \/ entry 0:45/.test(html) && /hold 14 bars, handover 8 bars/.test(html), html);
  assert.ok(!/clips|\.wav/.test(html));
  assert.ok(fetched.every((u) => !/sets\/.*clips|\.wav/.test(u)), fetched.join(" "));

  // 8) PLAY STEP with the set stopped: autopilot.performNow gets the stored plan (not aiActions.mix)
  const got = [];
  host.mod.autopilot = { core: ap, performNow: (o) => { got.push(o); return { ok: true, ran: o.forced.recipe, refused: null, line: "l" }; } };
  host.mod.aiActions = { mix: () => assert.fail("PLAY STEP must not run a generic AUTO MIX") };
  host.state = { trackA: A, trackB: B };
  host.decks = { a: { buffer: {}, isPlaying: true, bpm: 124, analysis: { phrase_boundaries_8bar: [0, phraseS, 2 * phraseS, 3 * phraseS] }, _currentPosition: () => 20, _playbackRate: () => 1 },
    b: { buffer: {}, bpm: 124, stems: {}, analysis: {}, seek() {} } };
  host.audio = { currentTime: 100 };
  await rt.playStep(step1, "PLAY STEP");
  assert.strictEqual(got.length, 1);
  assert.deepStrictEqual([got[0].forced.recipe, got[0].forced.a_time, got[0].forced.b_time, got[0].forced.merge.M, got[0].out, got[0].inn], ["Stem Merge", 61.068, 45.186, 16, "a", "b"]);
  console.log("macro perform OK");
})().catch((e) => { console.error(e); process.exit(1); });
