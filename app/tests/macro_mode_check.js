// node app/tests/macro_mode_check.js: pair atlas COMBOS and MACROS in the console (macro-mode.js).
const assert = require("assert");
const mm = require("../ui/static/macro-mode.js");
const tempoRule = require("../ui/static/tempo-rule.js");
const ap = require("../ui/static/autopilot.js");

const SEVEN = "6e3ee890fbf3d9bc", LANE8 = "9f7060c991aeb80b", RISE = "0123456789abcdef", OTHER = "fedcba9876543210";
const row = (b, name, works, combo, extra = {}) => Object.assign({ a: SEVEN, b, a_name: "Seven Lions - Days To Come (feat. Fiora)", b_name: name,
  b_bpm: 112.5, works, combo, plan: combo === "merge" ? { recipe: "Stem Merge", a_time: 150.1, b_time: 8.6, merge: { hold_bars: 16 } } : null }, extra);
const partners = [
  row(LANE8, "Lane 8, Jyll - Stay Still, A Little While (slowed/pitched down)", 86, "merge"),
  row(OTHER, "Some Artist - Other Song", 70, "mashup"),
  row(RISE, "Skrillex - Rise ft. Krewella", 58, null),
];

// 1) combos: the owner's live test pair beats a random candidate; the loaded song goes first
{
  const c = mm.comboCandidates({ aId: SEVEN, partners, played: [], recent: [] });
  assert.deepStrictEqual(c.list.map((x) => x.track_id), [LANE8, OTHER], "combos in works order, no non-combo");
  assert.ok(!c.list.some((x) => x.track_id === RISE), "Rise (no combo) is not a combo");
  const loaded = mm.comboCandidates({ aId: SEVEN, loadedId: OTHER, partners, played: [], recent: [] });
  assert.strictEqual(loaded.list[0].track_id, OTHER, "the other deck's combo song is tried first");
  assert.strictEqual(loaded.list[0].loaded, true);
  const played = mm.comboCandidates({ aId: SEVEN, partners, played: [LANE8], recent: [] });
  assert.ok(played.skipped.some((s) => s.b === LANE8 && /already played/.test(s.why)));
  const spaced = mm.comboCandidates({ aId: SEVEN, partners, played: [], recent: ["Lane 8 - Brightest Lights"] });
  assert.ok(spaced.skipped.some((s) => s.b === LANE8 && /artist spacing/.test(s.why)), "artist spacing");
  const bad = mm.comboCandidates({ aId: SEVEN, partners: [row(LANE8, "Lane 8 - X", 90, "merge", { played_bad: 2, played_good: 0 })], played: [], recent: [] });
  assert.strictEqual(bad.list.length, 0, "bad played evidence: no combo");
}

// 1b) studied combos (a famous studied set played the pair) rank above rule-only combos,
//     need no works score, still yield to a song the user loaded and to played / spacing gates
const STUD = "aaaaaaaaaaaaaaaa";
const studied = { count: 1, sets: ["oRb_81stwy8"], djs: ["Anyma"], techniques: { stem_intro: 1 }, move: "stem_intro", recipe: "Stems Transition" };
{
  const rows = partners.concat([row(STUD, "Anyma - Eternity", 40, "studied", { studied })]);
  const c = mm.comboCandidates({ aId: SEVEN, partners: rows, played: [], recent: [] });
  assert.deepStrictEqual(c.list.map((x) => x.track_id), [STUD, LANE8, OTHER], "studied first, whatever its works");
  assert.strictEqual(c.list[0].label, "STUDIED COMBO (Anyma set)");
  assert.strictEqual(c.list[0].studied.sets[0], "oRb_81stwy8");
  const loaded = mm.comboCandidates({ aId: SEVEN, loadedId: LANE8, partners: rows, played: [], recent: [] });
  assert.deepStrictEqual(loaded.list.map((x) => x.track_id), [LANE8, STUD, OTHER], "the user's loaded song still goes first");
  const gated = mm.comboCandidates({ aId: SEVEN, partners: rows, played: [STUD], recent: [] });
  assert.ok(gated.skipped.some((s) => s.b === STUD && /already played/.test(s.why)), "studied is still gated");
  const merged = mm.comboCandidates({ aId: SEVEN, partners: [row(STUD, "Anyma - Eternity", 90, "merge", { studied })], played: [], recent: [] });
  assert.deepStrictEqual([merged.list[0].combo, merged.list[0].label], ["merge", "STUDIED COMBO (Anyma set)"], "a studied merge keeps its move");
  assert.strictEqual(mm.studiedLabel({ djs: [] }), "STUDIED COMBO");
  assert.deepStrictEqual(mm.macroOrder([{ name: "combo-x" }, { name: "studied-set-a" }, { name: "b" }, { name: "studied-a-3" }], 3).map((m) => m.name),
    ["studied-set-a", "studied-a-3", "combo-x"], "studied macros are kept in the tab first");
  assert.strictEqual(mm.streakLabel(mm.streakAfter(mm.streakAfter(null, "merge"), "studied", "STUDIED COMBO (Anyma set)")),
    "COMBO x2: MERGE -> STUDIED COMBO (Anyma set)");
}

// 1c) FOLLOW SET: playing song at position k -> k+1, then the neighbours (nearest, ahead first), then the rest
{
  const id = (n) => String(n).repeat(16).slice(0, 16);
  const song = (p, title, tid, status = "library") => ({ position: p, title, track_id: tid, status });
  const set = { set_id: "oRb_81stwy8", dj: "Anyma", songs: [
    song(1, "A1 - One", id(1)), song(2, "A2 - Two", id(2)), song(3, "A3 - Three", id(3)), song(4, "ID - ID", null, "id"),
    song(5, "A5 - Five", id(5)), song(6, "Cherry - Puer", null, "download"), song(7, "A7 - Seven", id(7)), song(8, "A8 - Eight", id(8)),
    song(9, "A9 - Nine", id(9)), song(10, "Far - Missing", null, "download")] };
  const f = mm.followCandidates({ sets: [set], follow: "", aId: id(3), played: [], recent: [] });
  assert.strictEqual(f.set.set_id, "oRb_81stwy8", "auto: the playing song's set");
  assert.strictEqual(f.pos, 3);
  assert.deepStrictEqual(f.list.map((c) => c.position), [5, 2, 1, 6, 7, 8, 9], "next (ID skipped), neighbours nearest first, rest");
  assert.deepStrictEqual(f.list.find((c) => c.position === 6).download, { artist: "Cherry", title: "Puer", search_query: "ytmsearch:Cherry - Puer" }, "a near missing song downloads");
  assert.ok(f.skipped.some((s) => s.position === 10 && /downloads when near/.test(s.why)), "a far missing song waits");
  const gated = mm.followCandidates({ sets: [set], follow: "", aId: id(3), played: [id(5)], recent: ["A2 - Something"] });
  assert.deepStrictEqual(gated.list.map((c) => c.position).slice(0, 3), [1, 6, 7], "played (5) and artist spacing (2) fail their gates: skipped");
  assert.ok(gated.skipped.some((s) => s.position === 5 && /already played/.test(s.why)));
  assert.ok(gated.skipped.some((s) => s.position === 2 && /artist spacing/.test(s.why)));
  assert.strictEqual(mm.followCandidates({ sets: [set], follow: "none", aId: id(3) }).list.length, 0, "off");
  assert.strictEqual(mm.followCandidates({ sets: [set], follow: "", aId: "0".repeat(16) }).set, null, "not in a studied set: no auto follow");
  const chosen = mm.followCandidates({ sets: [set], follow: "oRb_81stwy8", aId: "0".repeat(16), max: 2 });
  assert.deepStrictEqual(chosen.list.map((c) => c.position), [1, 2], "a chosen set starts from its first song");
}

// 2) macro preference: ~80 % of valid steps over many seeded draws; an invalid step is never taken
function mulberry(seed) { return () => { seed = (seed + 0x6D2B79F5) >>> 0; let t = seed; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
{
  const macro = { name: "friday", steps: [{ n: 1, a: SEVEN, b: LANE8, b_name: "Lane 8 - Stay Still", recipe: "Stem Merge" }] };
  const rng = mulberry(7);
  let taken = 0;
  const N = 5000;
  for (let i = 0; i < N; i++) {
    const c = mm.macroCandidate({ macros: [macro], aId: SEVEN, played: [], valid: () => ({ ok: true }) });
    const p = mm.macroPrefer(c, rng, 0.8);
    if (p.take) { taken++; assert.strictEqual(p.line, "macro: preferred friday step 1"); } else assert.match(p.line, /20% explore/);
  }
  const share = taken / N;
  assert.ok(Math.abs(share - 0.8) < 0.02, `preferred share ${share}`);
  for (let i = 0; i < 500; i++) {
    const c = mm.macroCandidate({ macros: [macro], aId: SEVEN, played: [], valid: () => ({ ok: false, gate: "stems missing" }) });
    const p = mm.macroPrefer(c, rng, 1);
    assert.strictEqual(p.take, false);
    assert.strictEqual(p.line, "macro: skipped (invalid: stems missing)");
  }
  const again = mm.macroCandidate({ macros: [macro], aId: SEVEN, played: [LANE8] });
  assert.strictEqual(again.found, null, "a played step is not valid");
  assert.strictEqual(mm.macroPrefer(mm.macroCandidate({ macros: [macro], aId: LANE8 }), rng).line, null, "song not in a macro: silent");
}

// 3) streaks, default plan, fire point
{
  let s = mm.streakAfter(null, "merge");
  s = mm.streakAfter(s, "riff");
  s = mm.streakAfter(s, "mashup");
  assert.strictEqual(mm.streakLabel(s), "COMBO x3: MERGE -> RIFF x RAP -> MASHUP");
  assert.strictEqual(mm.streakLabel(mm.streakAfter(s, null)), "", "a non-combo ends the streak");
  const r = mm.applyPlan({ recipe: "Long Blend", a_time: 100, b_time: 0 }, partners[0].plan);
  assert.deepStrictEqual([r.cand.recipe, r.cand.a_time, r.cand.b_time, r.used], ["Stem Merge", 150.1, 8.6, true]);
  assert.strictEqual(mm.applyPlan({ recipe: "Long Blend" }, null).used, false);
  const phrases = [10, 27.1, 44.2, 61.3];
  assert.strictEqual(mm.fireAt({ nowPos: 20, aTime: 44.2, phrases, bar: 2.14 }), 44.2, "early: the stored point");
  assert.strictEqual(mm.fireAt({ nowPos: 50, aTime: 44.2, phrases, bar: 2.14 }), 61.3, "late: the next phrase line");
}

// 4) the step gates (same rules as the autopilot) and the run-now entries
{
  const st = { n: 2, a: SEVEN, b: LANE8, b_name: "Lane 8", recipe: "Stem Merge", a_time: 150 };
  const base = { step: st, aId: SEVEN, bId: LANE8, aStems: true, bStems: true, aEff: 112, bBpm: 112.5, keyScore: 1, tempoRule, keySafe: ap.keySafeRecipe };
  assert.deepStrictEqual(mm.stepGate(base), { ok: true, recipe: "Stem Merge", why: "" });
  assert.strictEqual(mm.stepGate(Object.assign({}, base, { aId: OTHER })).ok, false, "wrong A: refused");
  assert.strictEqual(mm.stepGate(Object.assign({}, base, { bStems: false })).recipe, "Bass Swap", "no stems: stem move falls back");
  const far = mm.stepGate(Object.assign({}, base, { bBpm: 140 }));
  assert.strictEqual(far.recipe, "Stem Bridge", "tempo gap: beatless fallback");
  const clash = mm.stepGate(Object.assign({}, base, { bStems: false, keyScore: 0 }));
  assert.strictEqual(clash.recipe, "Echo Out", "keys clash: Echo Out");
  assert.strictEqual(mm.runNowCheck("macro-step", { playing: false }).ok, false, "nothing playing: refused");
  assert.strictEqual(mm.runNowCheck("macro-step", { playing: true }).ok, false, "no macro: refused");
  assert.strictEqual(mm.runNowCheck("macro-step", Object.assign({ playing: true }, base)).ok, true);
  assert.strictEqual(mm.runNowCheck("macro-transition", { playing: true, pairStep: null }).ok, false);
  assert.strictEqual(mm.runNowCheck("plan-picks", { picks: [SEVEN] }).ok, false);
  assert.strictEqual(mm.runNowCheck("plan-picks", { picks: [SEVEN, LANE8] }).ok, true);
  const m = { name: "friday", steps: [st] };
  const e = mm.editStep(m, 2, { recipe: "Bass Swap", a_time: 140 });
  assert.deepStrictEqual([e.steps[0].recipe, e.steps[0].a_time, e.parent, m.steps[0].recipe], ["Bass Swap", 140, "friday", "Stem Merge"]);
  assert.throws(() => mm.setToMacro("x", [{ a: SEVEN, b: LANE8 }, { a: OTHER, b: RISE }]), /does not start/);
  assert.strictEqual(mm.setToMacro("x", [{ a: SEVEN, b: LANE8 }, { a: LANE8, b: RISE }]).steps.length, 2);
}

// 5) the runtime over a fake Host: combos before the pool, seeded macro draws, logs
(async () => {
  const logs = [], els = {};
  const el = (id) => els[id] || (els[id] = { id, value: "", checked: id === "ap-atlas-combo", textContent: "", hidden: true, innerHTML: "", addEventListener() {} });
  const macro = { name: "friday", steps: [{ n: 1, a: SEVEN, b: OTHER, b_name: "Some Artist - Other Song", recipe: "Mashup → Transition", a_time: 120, b_time: 30 }] };
  const rng = mulberry(3);
  const host = {
    ui: { el, flag: (id, d) => (els[id] ? !!els[id].checked : d), status() {} },
    clock: { now: () => 0, setTimeout() {} },
    random: { next: rng, uuid: () => "x" },
    log: { step: (k, o) => logs.push([k, o.decision, o.why]) },
    bus: { on() {} }, mod: {}, state: {}, decks: {},
    api: { fetch: async (url) => ({ ok: true, json: async () => (url.startsWith("/api/atlas/partners") ? { partners }
      : url === "/api/macros" ? { macros: [{ name: "friday", songs: 2 }] } : url.startsWith("/api/macros/") ? { macro, validation: [] } : {}) }) },
  };
  const rt = mm.createRuntime({ host });
  const first = await rt.firstCandidates(SEVEN, { played: [], recent: [], loadedId: null });
  assert.deepStrictEqual(first.map((c) => c.track_id), [LANE8, OTHER], "no macro known yet: combos first, Lane 8 before a random pick");
  assert.ok(logs.some(([k, d, w]) => k === "combo" && d === "picked" && /Lane 8/.test(w)));
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  let macroFirst = 0;
  for (let i = 0; i < 400; i++) {
    const f = await rt.firstCandidates(SEVEN, { played: [], recent: [] });
    if (f[0]._macro) { macroFirst++; assert.strictEqual(f[0].track_id, OTHER); }
    else assert.strictEqual(f[0].track_id, LANE8);
  }
  assert.ok(Math.abs(macroFirst / 400 - 0.8) < 0.07, `runtime macro share ${macroFirst / 400}`);
  assert.ok(logs.some(([k, , w]) => k === "macro" && w === "macro: preferred friday step 1"));
  assert.ok(logs.some(([k, , w]) => k === "macro" && /macro: skipped \(20% explore\)/.test(w)));
  const planned = rt.defaultPlan(SEVEN, { track_id: LANE8 }, { recipe: "Long Blend", a_time: 90, b_time: 0 });
  assert.strictEqual(planned.recipe, "Stem Merge", "the atlas plan is the default");
  assert.ok(logs.some(([k, d]) => k === "atlas" && d === "plan"));
  rt.landed(SEVEN, LANE8, { recipe: "Stem Merge" });
  assert.strictEqual(els["combo-streak"].textContent, "COMBO x1: MERGE");
  assert.strictEqual(rt.stats.maxStreak, 1);
  partners.push(row(STUD, "Anyma - Eternity", 40, "studied", { studied }));     // same array the partners cache holds
  rt.landed(SEVEN, STUD, { recipe: "Stems Transition" });
  assert.strictEqual(els["combo-streak"].textContent, "COMBO x2: MERGE -> STUDIED COMBO (Anyma set)", "VIBE line names the studied set");
  // FOLLOW SET in the runtime: the set's next song is tried before the atlas combos, and logged
  {
    const logs2 = [], els2 = {};
    const el2 = (id) => els2[id] || (els2[id] = { id, value: "", dataset: {}, checked: id === "ap-atlas-combo", textContent: "", hidden: true, innerHTML: "", addEventListener() {} });
    const sets = [{ set_id: "oRb_81stwy8", dj: "Anyma", songs: [{ position: 1, title: "Seven Lions - Days To Come", track_id: SEVEN, status: "library" },
      { position: 2, title: "Some Artist - Other Song", track_id: OTHER, status: "library" }, { position: 3, title: "Cherry - Puer", track_id: null, status: "download" }] }];
    const host2 = {
      ui: { el: el2, flag: (id, d) => (els2[id] ? !!els2[id].checked : d), status() {} }, clock: { now: () => 0, setTimeout() {} },
      random: { next: mulberry(5), uuid: () => "x" }, log: { step: (k, o) => logs2.push([k, o.decision, o.why]) }, bus: { on() {} }, mod: {}, state: {}, decks: {},
      api: { fetch: async (url) => ({ ok: true, json: async () => (url.startsWith("/api/atlas/partners") ? { partners } : url === "/api/studied/sets" ? { sets } : url === "/api/macros" ? { macros: [] } : {}) }) },
    };
    const rt2 = mm.createRuntime({ host: host2 });
    const f2 = await rt2.firstCandidates(SEVEN, { played: [], recent: [] });
    assert.deepStrictEqual(f2.map((c) => c.track_id), [OTHER, null, STUD, LANE8], "set order first (next, then the near missing song), then combos");
    assert.deepStrictEqual(f2[0]._follow, { set_id: "oRb_81stwy8", dj: "Anyma", position: 2 });
    assert.strictEqual(f2[1]._download.search_query, "ytmsearch:Cherry - Puer");
    assert.ok(logs2.some(([k, , w]) => k === "studied" && w === "studied: suggest Some Artist - Other Song (set oRb_81stwy8, pos 2)"));
    els2["ap-follow-set"].value = "none";
    const off = await rt2.firstCandidates(SEVEN, { played: [], recent: [] });
    assert.deepStrictEqual(off.map((c) => c.track_id), [STUD, LANE8, OTHER], "FOLLOW SET off: combos only (studied first)");
  }
  console.log("macro mode OK");
})().catch((e) => { console.error(e); process.exit(1); });
