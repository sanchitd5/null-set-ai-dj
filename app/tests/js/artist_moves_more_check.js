// Node check for the two artist moves added on artist-moves-more (app/ui/static/artist-moves.js):
// pad_lead (Lane 8 "pads first", learned stem_intro order).
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

const beats = Array.from({ length: 400 }, (_, i) => i * beatS);
// ---- wiring: every new move has a button (ACTION_IDS) and a toggle in index.html
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const src = fs.readFileSync(path.join(STATIC, "artist-moves.js"), "utf8");
for (const [btn, kind] of [["artist-pad", "pad_lead"]]) {
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
    mod: Object.assign({ djMind: { core: { camelotScore: () => 0.9 } },
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
console.log("artist_moves_more_check ok");
