// Node check for the two artist moves added on artist-moves-more (app/ui/static/artist-moves.js):
// pad_lead (Lane 8 "pads first", learned stem_intro order) and chant_gate (S20 gated vocal).
// Pure planners on synthetic data, then the index.html wiring (button + toggle per move).
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "ui", "static");
const am = require(path.join(STATIC, "artist-moves.js"));

// ---- pad_lead
const BPM = 120, beatS = 0.5, bar = 2;
const env = (v, t0 = 0, hop = 0.125, n = 2000) => ({ t0, hop, v: Array(n).fill(v) });
const padBase = () => ({ pos: 10, exitT: 60, bEntry: 40, aBpm: BPM, aRate: 1, bBpm: BPM, bPlaying: false,
  keyScore: 0.9, bOtherEnv: env(0.1), aOtherRms: 0.05, inTransition: false, mashupActive: false, relaxed: false, onDemand: false, style: "standard", recipe: "Long Blend", sinceLast: 5 });
let p = am.planPadLead(padBase());
assert.ok(p.ok, JSON.stringify(p));
assert.strictEqual(p.kind, "pad_lead");
assert.strictEqual(p.window_beats, 32, "8-bar window when there is room");
assert.ok(Math.abs(p.start - (60 - 32 * beatS)) < 1e-9);
assert.strictEqual(p.release, 60);
assert.ok(Math.abs(p.piece.b_from - (40 - 32 * beatS)) < 1e-9, "B's pads from the 8 bars before its entry");
assert.ok(p.gain > 0 && p.gain <= 0.7 && p.hp_hz >= 120, "pads stay under A and above the sub line");

// refusals, each with its gate
const refuse = (patch, gate) => { const r = am.planPadLead(Object.assign(padBase(), patch)); assert.strictEqual(r.ok, false); assert.strictEqual(r.gate, gate, JSON.stringify(r)); };
refuse({ inTransition: true }, "transition");
refuse({ mashupActive: true }, "vocal_layer");
refuse({ relaxed: true }, "relaxed");
refuse({ bPlaying: true }, "b_rolling");
refuse({ exitT: null }, "no_plan");
refuse({ keyScore: null }, "unmeasured");
refuse({ keyScore: 0.6 }, "key");                   // G2: two melodies need >= 0.8
refuse({ bBpm: 132 }, "tempo");                     // 10 % stretch > 8 % keylock cap
refuse({ bOtherEnv: null }, "no_stems");
refuse({ bOtherEnv: env(0.001) }, "no_pads");       // silent pads never enter alone
refuse({ pos: 57 }, "late");
refuse({ style: "instant" }, "style");
refuse({ style: "peak" }, "style");
refuse({ recipe: "Echo Out" }, "recipe");
refuse({ recipe: "Quick Cut" }, "recipe");
refuse({ sinceLast: 1 }, "spacing");            // at most one pad lead every PAD_EVERY transitions
assert.ok(am.planPadLead(Object.assign(padBase(), { sinceLast: am.PAD_EVERY })).ok);
// relaxed session still refused on demand? no: onDemand skips the choice gate
assert.ok(am.planPadLead(Object.assign(padBase(), { relaxed: true, onDemand: true })).ok);
// 4-bar fallback when the 8-bar window starts too soon
p = am.planPadLead(Object.assign(padBase(), { pos: 50 }));
assert.ok(p.ok && p.window_beats === 16, JSON.stringify(p));
// unmeasured A pads -> named fallback gain
p = am.planPadLead(Object.assign(padBase(), { aOtherRms: null }));
assert.ok(p.ok && p.fallbacks.some((f) => /^gain=/.test(f)));

// ---- chant_gate
const beats = Array.from({ length: 400 }, (_, i) => i * beatS);
const chantBase = () => ({ pos: 50, lineT: 64, bpm: BPM, rate: 1, beats, vocals: [[55, 70]], drops: [], energyNow: 0.3, energyNext: 0.6,
  count: 0, quiet: true, vocalLive: true, vocalBusy: false, inTransition: false, holdActive: false, mashupActive: false, relaxed: false, onDemand: false });
p = am.planChantGate(chantBase());
assert.ok(p.ok, JSON.stringify(p));
assert.strictEqual(p.kind, "chant_gate");
assert.strictEqual(p.release, 64, "opens again exactly on the phrase line");
assert.strictEqual(p.window_beats, 8);
assert.ok(Math.abs(p.start - 60) < 1e-9);
assert.strictEqual(p.steps.length, 16, "straight 16ths: 32 slots, every other one open");
for (const s of p.steps) assert.ok(Math.abs(s.t1 - s.t0 - 0.125) < 1e-9 && s.t0 >= p.start && s.t1 <= p.release);
assert.ok(p.floor > 0 && p.floor < 0.5);
const refuseC = (patch, gate) => { const r = am.planChantGate(Object.assign(chantBase(), patch)); assert.strictEqual(r.ok, false); assert.strictEqual(r.gate, gate, JSON.stringify(r)); };
refuseC({ inTransition: true }, "transition");
refuseC({ holdActive: true }, "transition");
refuseC({ mashupActive: true }, "vocal_layer");
refuseC({ vocalLive: false }, "no_stems");
refuseC({ vocalBusy: true }, "busy");
refuseC({ relaxed: true }, "relaxed");
refuseC({ quiet: false }, "phrase_busy");
refuseC({ count: 1 }, "cap");                        // once per song
refuseC({ energyNow: null }, "unmeasured");
refuseC({ energyNext: 0.2 }, "no_build");            // a gate is a build move
refuseC({ vocals: null }, "unmeasured");
refuseC({ vocals: [[10, 20]] }, "no_vocal");         // nothing sung, nothing to gate
refuseC({ pos: 63.5 }, "late");
// owner rule "never vocal mix a drop line"
refuseC({ drops: null }, "unmeasured");
refuseC({ drops: [[58, 66]] }, "drop_line");                  // the window sits inside a drop
refuseC({ drops: [[64, 80]] }, "drop_line");                  // A's line 55-70 runs on into the drop at 64
assert.ok(am.planChantGate(Object.assign(chantBase(), { drops: [[64, 80]], vocals: [[55, 63.9]] })).ok, "a line that ends before the drop may be gated");
assert.strictEqual(am.planChantGate(Object.assign(chantBase(), { drops: [[58, 66]], onDemand: true })).gate, "drop_line", "on demand too");
// on demand skips the choice gates (cap, build, quiet) but keeps safety ones
assert.ok(am.planChantGate(Object.assign(chantBase(), { onDemand: true, count: 5, quiet: false, energyNext: 0.1 })).ok);
assert.strictEqual(am.planChantGate(Object.assign(chantBase(), { onDemand: true, vocalBusy: true })).gate, "busy");
// 1-bar fallback when 2 bars are no longer ahead
p = am.planChantGate(Object.assign(chantBase(), { pos: 61 }));
assert.ok(p.ok && p.window_beats === 4, JSON.stringify(p));

// ---- dhol_drop
const dholBase = () => ({ pos: 50, exitT: 64, bEntry: 20, aBpm: BPM, aRate: 1, bBpm: BPM, bPlaying: false, bDrumEnv: env(0.1),
  aDrumRms: 0.1, recipe: "Quick Cut", style: "standard", sinceLast: 5, inTransition: false, mashupActive: false, relaxed: false, onDemand: false,
  sceneLevel: "full", aPunjabi: true, bPunjabi: true });
p = am.planDholDrop(dholBase());
assert.ok(p.ok, JSON.stringify(p));
assert.strictEqual(p.window_beats, 8, "2-bar bed when there is room");
assert.ok(Math.abs(p.start - 60) < 1e-9 && p.release === 64, "ends on the cut");
assert.ok(Math.abs(p.piece.b_from - 16) < 1e-9, "B's drums from the 2 bars before its entry");
assert.ok(p.hp_hz >= 120 && p.gain > 0 && p.gain <= 0.7);
const refuseD = (patch, gate) => { const r = am.planDholDrop(Object.assign(dholBase(), patch)); assert.strictEqual(r.ok, false); assert.strictEqual(r.gate, gate, JSON.stringify(r)); };
refuseD({ inTransition: true }, "transition");
refuseD({ mashupActive: true }, "vocal_layer");
refuseD({ relaxed: true }, "relaxed");
refuseD({ recipe: "Long Blend" }, "recipe");        // only ahead of a cut
refuseD({ sinceLast: 1 }, "spacing");               // at most every other cut
refuseD({ exitT: null }, "no_plan");
refuseD({ bPlaying: true }, "b_rolling");
refuseD({ bBpm: 110 }, "tempo");                    // 9 % > the 8 % keylock cap
refuseD({ bDrumEnv: null }, "no_stems");
refuseD({ bDrumEnv: env(0.001) }, "no_drums");
refuseD({ bEntry: 1 }, "no_room");
refuseD({ pos: 63.8 }, "late");
// owner rule: Punjabi songs only, never an experiment (handover, off, unknown genre, "on" over non-Punjabi songs, on demand)
refuseD({ sceneLevel: "handover", bPunjabi: false }, "scene");
refuseD({ sceneLevel: "handover" }, "scene");
refuseD({ sceneLevel: null }, "scene");
refuseD({ aPunjabi: false }, "scene");                          // PUNJABI "on" gives level full, the songs still decide
refuseD({ bPunjabi: false }, "scene");
refuseD({ aPunjabi: undefined, bPunjabi: undefined }, "scene"); // unknown genre = not Punjabi
refuseD({ aPunjabi: false, onDemand: true }, "scene");
assert.ok(am.planDholDrop(Object.assign(dholBase(), { recipe: "Long Blend", style: "instant" })).ok, "an instant swap is a cut");
assert.ok(am.planDholDrop(Object.assign(dholBase(), { recipe: "Long Blend", onDemand: true })).ok, "on demand skips the recipe gate");
p = am.planDholDrop(Object.assign(dholBase(), { pos: 59.8 }));
assert.ok(p.ok && p.window_beats === 4, "1-bar fallback");

// ---- chop_duck (S18)
p = am.planChopDuck({ kind: "vocal_chop", drumsRms: 0.2, chopRms: 0.2, drops: [] });
assert.ok(p.ok && p.duck_db === -10, JSON.stringify(p));          // chops level with the drums: the deepest duck
p = am.planChopDuck({ kind: "vocal_chop", drumsRms: 0.1, chopRms: 0.2, drops: [] });
assert.ok(p.ok && p.duck_db === -6, JSON.stringify(p));           // chops 6 dB over: the lightest duck
assert.ok(Math.abs(p.gain - Math.pow(10, -6 / 20)) < 1e-9);
p = am.planChopDuck({ kind: "vocal_chop", drumsRms: 0.1, chopRms: 0.13, drops: [] });
assert.ok(p.ok && p.duck_db <= -6 && p.duck_db >= -10);
const refuseK = (c, gate) => { const r = am.planChopDuck(c); assert.strictEqual(r.ok, false); assert.strictEqual(r.gate, gate, JSON.stringify(r)); };
refuseK({ kind: "vocal_loop", drumsRms: 0.2, chopRms: 0.2, drops: [] }, "not_chop");
refuseK({ kind: "vocal_chop", drumsRms: 0.2, chopRms: 0.2, relaxed: true, drops: [] }, "relaxed");
refuseK({ kind: "vocal_chop", drumsRms: null, chopRms: 0.2, drops: [] }, "unmeasured");
refuseK({ kind: "vocal_chop", drumsRms: 0.001, chopRms: 0.2, drops: [] }, "no_drums");
refuseK({ kind: "vocal_chop", drumsRms: 0.02, chopRms: 0.2, drops: [] }, "no_need");   // 20 dB over the drums already
refuseK({ kind: "vocal_chop", drumsRms: 0.2, chopRms: 0.2, start: 60, end: 64, drops: [[62, 78]] }, "drop_line");
refuseK({ kind: "vocal_chop", drumsRms: 0.2, chopRms: 0.2, start: 60, end: 64 }, "unmeasured");
assert.ok(am.planChopDuck({ kind: "vocal_chop", drumsRms: 0.2, chopRms: 0.2, start: 60, end: 64, drops: [[64, 78]] }).ok, "ends on the drop line");

// ---- wiring: every new move has a button (ACTION_IDS) and a toggle in index.html
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const src = fs.readFileSync(path.join(STATIC, "artist-moves.js"), "utf8");
for (const [btn, kind] of [["artist-pad", "pad_lead"], ["artist-chant", "chant_gate"], ["artist-dhol", "dhol_drop"], ["artist-duck", "chop_duck"]]) {
  assert.ok(html.includes(`data-ai-action="${btn}"`), btn);
  assert.ok(html.includes(`id="ap-artist-${kind}"`), kind);
  assert.ok(new RegExp(`"${btn}": "${kind}"`).test(src), `ACTION_IDS ${btn}`);
  assert.ok(am.KINDS.includes(kind) && am.PLANNERS[kind] && am.LABEL[kind] && am.SPEC[kind]);
}
// ---- runtime through a fake Host port (the way artist_moves_check.js mounts it)
const vm = require("vm");
function fakeHost(decks, over = {}) {
  const t = { now: 0, q: [] };
  const events = [], steps = [], spent = [];
  const host = {
    audio: { get currentTime() { return t.now; } },
    clock: { now: () => t.now * 1000, perfNow: () => t.now * 1000, setTimeout: (fn, ms) => { t.q.push({ at: t.now + ms / 1000, fn }); return t.q.length; }, clearTimeout() {} },
    decks, session: { relaxed: false },
    mod: Object.assign({ djMind: { core: { camelotScore: () => 0.9 } }, sceneProfile: require(path.join(STATIC, "scene-profile.js")),
      fxBudget: { spend: (k, c, ctx) => { spent.push(k); return { ok: true, why: "ok" }; } } }, over.mod || {}),
    bus: { emit: (type, detail) => events.push({ type, detail }) },
    log: { step: (kind, o) => steps.push({ kind, o }) },
    ui: { flag: () => true, status: () => {}, queryAll: () => [] },
  };
  const advance = (to) => { for (;;) { t.q.sort((a, b) => a.at - b.at); const x = t.q[0]; if (!x || x.at > to) break; t.q.shift(); t.now = x.at; x.fn(); } t.now = to; };
  return { host, events, steps, spent, t, advance };
}
const SR = 1000;
const buf = (level, dur = 400) => { const ch = new Float32Array(dur * SR).fill(level); return { getChannelData: () => ch, sampleRate: SR, duration: dur }; };
const gainParam = () => { const calls = []; return { calls, cancelScheduledValues: (t) => calls.push(["cancel", t]), setValueAtTime: (v, t) => calls.push(["set", v, t]),
  linearRampToValueAtTime: (v, t) => calls.push(["ramp", v, t]) }; };
function fakeDeck(id, over = {}) {
  const d = { id, playing: true, bpm: 120, loopOn: false, reversed: false, _slices: {}, _holds: {}, stemsReady: true,
    analysis: { beat_times: beats, downbeat_times: beats.filter((_, i) => i % 4 === 0), vocal_active_regions: [[55, 70]], key: { camelot: "8A" }, phrase_boundaries_8bar: [0, 16, 32, 48, 64, 80],
      energy_times: Array.from({ length: 100 }, (_, i) => i * 2), energy_curve: Array.from({ length: 100 }, (_, i) => (i >= 32 ? 0.8 : 0.4)) },
    stems: { other: buf(0.2), vocals: buf(0.2), drums: buf(0.2), lag: 0, ratio: 1 }, stemGain: { vocals: { gain: gainParam() } },
    stemLive: { vocals: { gain: gainParam() } }, stemsLiveAt: () => true, mixCalls: [], stemMix(tg, at) { this.mixCalls.push([tg, at]); return true; },
    pos: 50, _currentPosition() { return this.pos; }, _playbackRate: () => 1, crossfaderGain: { gain: { value: 1 } },
    layers: [], layerPieces(b, pieces, opts) { this.layers.push({ pieces, opts }); return { ok: true, until: pieces[0].at + 1 }; } };
  return Object.assign(d, over);
}
function mount(H) {
  let api = null;
  const sb = { Engine: { mount: (n, create) => { if (n === "artistMoves") api = create({ host: H.host }); } }, console: { info() {}, warn() {} }, Math, Object, Array, Number, JSON, String, RegExp, Float32Array, require };
  sb.window = sb;
  vm.createContext(sb);
  vm.runInContext(fs.readFileSync(path.join(STATIC, "drop-line.js"), "utf8"), sb);   // index.html order
  vm.runInContext(fs.readFileSync(path.join(STATIC, "learned-moves.js"), "utf8"), sb);
  vm.runInContext(fs.readFileSync(path.join(STATIC, "artist-moves.js"), "utf8"), sb);
  return api;
}
const o = (pos, over = {}) => Object.assign({ pos, bar: 2, entryT: 0, lineT: 64, exitT: 64, bEntry: 40, quiet: true, holdActive: false,
  mashupActive: false, fxOk: true, style: "standard", recipe: "Long Blend" }, over);
{
  // pad lead: booked once per entry, two layered pieces of B's other stem (half then full gain), high-passed
  const a = fakeDeck("a"), b = fakeDeck("b", { playing: false });
  const H = fakeHost({ a, b });
  const api = mount(H);
  a.pos = 47;
  api.tick(a, o(47, { lineT: 999 }));                // lineT far away: only the pad lead is in range
  assert.strictEqual(a.layers.length, 2, JSON.stringify(H.steps));
  assert.ok(a.layers.every((l) => l.opts.hpHz >= 120), "B's pads never own the sub");
  assert.ok(Math.abs(a.layers[1].opts.gain - 2 * a.layers[0].opts.gain) < 1e-9, "half gain, then full");
  const ev = H.events.find((e) => e.type === "ai-activity" && e.detail.move === "pad_lead");
  assert.ok(ev && ev.detail.kind === "artist_move" && ev.detail.why && ev.detail.label === "artist_move: pad_lead");
  api.tick(a, o(48, { lineT: 999 }));
  assert.strictEqual(H.events.filter((e) => e.type === "ai-activity" && e.detail.move === "pad_lead").length, 1, "one plan per entry");
  // conservative rate: the next two entries are refused with the spacing gate
  api.tick(a, o(100, { exitT: 110, lineT: 999 }));
  assert.ok(H.steps.some((s) => s.kind === "artist_move_refused" && s.o.decision === "pad_lead" && /^spacing/.test(s.o.why)));
  // a cut is not a blend the pads can lead into
  const a2 = fakeDeck("a"), b2 = fakeDeck("b", { playing: false });
  const H2 = fakeHost({ a: a2, b: b2 });
  mount(H2).tick(a2, o(47, { lineT: 999, recipe: "Quick Cut" }));
  assert.strictEqual(a2.layers.length, 0);
  assert.ok(H2.steps.some((s) => s.o.decision === "pad_lead" && /^recipe/.test(s.o.why)));
  // key gate from dj-mind's camelotScore
  const a3 = fakeDeck("a"), b3 = fakeDeck("b", { playing: false });
  const H3 = fakeHost({ a: a3, b: b3 }, { mod: { djMind: { core: { camelotScore: () => 0.6 } } } });
  mount(H3).tick(a3, o(47, { lineT: 999 }));
  assert.ok(a3.layers.length === 0 && H3.steps.some((s) => s.o.decision === "pad_lead" && /^key/.test(s.o.why)));
}
{
  // chant gate: stem mode at the window, 16 gate steps on A's live vocal gain, full mix back after the line
  const a = fakeDeck("a"), b = fakeDeck("b", { playing: false });
  const H = fakeHost({ a, b });
  const api = mount(H);
  a.pos = 58.5;
  assert.strictEqual(api.tick(a, o(58.5, { exitT: 200 })), null);
  assert.deepStrictEqual(H.spent, ["vocal"], "one vocal unit of the FX budget");
  H.advance(10);
  const lg = a.stemLive.vocals.gain.calls;
  assert.ok(lg.filter((c) => c[0] === "ramp" && c[1] === am.CHANT_FLOOR).length === 16, "16 closed steps");
  assert.ok(a.mixCalls.some((m) => m[0] && Object.keys(m[0]).length === 0), "stem mode entered");
  assert.ok(a.mixCalls.some((m) => m[0] === null), "full mix restored after the line");
  const last = lg[lg.length - 1];
  assert.ok(last[0] === "set" && last[1] === 1, "open on the line");
  // once per song
  api.tick(a, o(75.5, { lineT: 80, exitT: 200 }));
  assert.ok(H.steps.some((s) => s.o.decision === "chant_gate" && /^cap/.test(s.o.why)));
  // stop() opens an armed gate at once
  const a4 = fakeDeck("a"), b4 = fakeDeck("b", { playing: false });
  const H4 = fakeHost({ a: a4, b: b4 });
  const api4 = mount(H4);
  api4.tick(a4, o(59.5, { exitT: 200 }));
  api4.stop(a4);
  const c4 = a4.stemLive.vocals.gain.calls;
  assert.ok(c4.length >= 2 && c4[c4.length - 1][0] === "set" && c4[c4.length - 1][1] === 1);
}
{
  // FX budget refusal: nothing armed, the refusal names the gate
  const a = fakeDeck("a"), b = fakeDeck("b", { playing: false });
  const H = fakeHost({ a, b }, { mod: { fxBudget: { spend: () => ({ ok: false, why: "2 vocal moves this song" }) } } });
  const api = mount(H);
  api.tick(a, o(59.5, { exitT: 200 }));
  H.advance(10);
  assert.strictEqual(a.stemLive.vocals.gain.calls.length, 0);
  assert.ok(H.steps.some((s) => s.o.decision === "chant_gate" && /^fx_budget/.test(s.o.why)));
  // on demand (AI ACTIONS button) skips the budget, as the other artist buttons skip choice gates
  a.pos = 58.5;
  H.host.mod.djMind.fireAt = () => null;
  const r = api.runNow("chant_gate");
  assert.ok(r.ok, r.why);
}
{
  // dhol drop-in runtime: one layered piece of B's drums, and the cue tease / roll stand down on that entry
  const a = fakeDeck("a"), b = fakeDeck("b", { playing: false });
  const H = fakeHost({ a, b });
  const api = mount(H);
  const oc = (pos) => o(pos, { lineT: 999, exitT: 64, bEntry: 20, recipe: "Quick Cut", scene: "full", genreA: "Punjabi Pop", genreB: "Bhangra" });
  api.tick(a, oc(50));                                 // 7 bars out: the tease waits for the drop-in
  assert.strictEqual(H.events.filter((e) => e.detail && e.detail.move === "cue_tease").length, 0);
  api.tick(a, oc(58.5));
  const dh = H.events.filter((e) => e.type === "ai-activity" && e.detail.move === "dhol_drop");
  assert.strictEqual(dh.length, 1, JSON.stringify(H.steps));
  api.tick(a, oc(60)); api.tick(a, oc(62));
  assert.strictEqual(H.events.filter((e) => e.detail && (e.detail.move === "cue_tease" || e.detail.move === "roll")).length, 0, "no stacking on the cut");
  // a blend entry: the drop-in refuses by recipe, the tease is free again
  api.tick(a, o(105, { lineT: 999, exitT: 110, bEntry: 20, recipe: "Long Blend", scene: "full", genreA: "Punjabi Pop", genreB: "Bhangra" }));
  assert.ok(H.steps.some((s) => s.o.decision === "dhol_drop" && /^recipe/.test(s.o.why)));
  // a cut into a pop song under the handover level: refused by scene, nothing layered
  const a5 = fakeDeck("a"), b5 = fakeDeck("b", { playing: false });
  const H5 = fakeHost({ a: a5, b: b5 });
  mount(H5).tick(a5, o(58.5, { lineT: 999, exitT: 64, bEntry: 20, recipe: "Quick Cut", scene: "handover", genreA: "Punjabi Pop", genreB: "Pop" }));
  assert.ok(!H5.events.some((e) => e.detail && e.detail.move === "dhol_drop"), "no drop-in outside two Punjabi songs");
  assert.ok(H5.steps.some((s) => s.o.decision === "dhol_drop" && /^scene/.test(s.o.why)));
  // the chop duck stays off a drop: the runtime reads the section map
  const a6 = fakeDeck("a"); a6.analysis.sections = [{ label: "drop", start: 62, end: 78 }];
  const H6 = fakeHost({ a: a6, b: fakeDeck("b", { playing: false }) });
  const r6 = mount(H6).chopDuck(a6, { kind: "vocal_chop", start: 60, end: 64, beats: 8, slices: [{ from: 50, dur: 0.2 }] }, 1, 5);
  assert.strictEqual(r6, null);
  assert.ok(H6.steps.some((s) => s.o.decision === "chop_duck" && /^drop_line/.test(s.o.why)));
}
{
  // chop duck runtime: the learned chop hook ducks A's drums from the window start, logs an artist_move
  const a = fakeDeck("a"), b = fakeDeck("b", { playing: false });
  const H = fakeHost({ a, b });
  const api = mount(H);
  const plan = { kind: "vocal_chop", start: 60, end: 64, beats: 8, cap_beats: 32, slices: [{ from: 50, dur: 0.2 }, { from: 52, dur: 0.2 }] };
  const r = api.chopDuck(a, plan, 1, 5);
  assert.ok(r && r.ok && r.duck_db === -10, JSON.stringify(r));    // fake stems: vocals level with drums
  const m = a.mixCalls.find((x) => x[0] && x[0].drums != null);
  assert.ok(m && Math.abs(m[0].drums - r.gain) < 1e-9 && m[1] === 1, "drums ducked at the window start");
  assert.ok(H.events.some((e) => e.type === "ai-activity" && e.detail.move === "chop_duck" && e.detail.kind === "artist_move"));
  assert.strictEqual(api.chopDuck(a, Object.assign({}, plan, { kind: "vocal_loop" }), 1, 5), null, "only under chops");
  // learned-moves.js runSlices calls the hook for vocal_chop
  const lm = fs.readFileSync(path.join(STATIC, "learned-moves.js"), "utf8");
  assert.ok(lm.includes('if (plan.kind === "vocal_chop" && am && typeof am.chopDuck === "function") am.chopDuck(d, plan, at, until);'));
}
console.log("artist_moves_more_check ok");
