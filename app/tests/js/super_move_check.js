// node app/tests/js/super_move_check.js: $Up3R-M@SS!V3-M0v3 (super-move.js): the trigger, the build wait, the
// schedule / preload timing, NULL in front for exactly the move's span, clean abort, and the isolation (off = no-op).
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const smv = require("../../ui/static/super-move.js");
const ap = require("../../ui/static/autopilot.js");

const plan = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "..", "music_brain", "supermove", "variants", "v1.json"), "utf8"));
const summary = { name: "v1", title: smv.NAME, n: plan.songs.length,
  songs: plan.songs.map((s) => ({ i: s.i, id: s.id, name: s.name, level: s.level, bpm: s.bpm, core: { start: s.core.start, end: s.core.end }, enter_bars: s.enter_bars })) };
const ID = plan.songs.map((s) => s.id);
const v6 = (bpm, builds = []) => ({ bpm, structure: { version: 6 }, sections: builds.map(([a, b]) => ({ label: "build", start: a, end: b })),
  phrase_boundaries_8bar: Array.from({ length: 80 }, (_, i) => i * 8 * 240 / bpm), energy_times: [], energy_curve: [] });
const analyses = Object.fromEntries(plan.songs.map((s) => [s.id, v6(s.bpm)]));
const seedFor = (want) => { for (let i = 0; ; i++) { const r = smv.chance(`set${i}|v1|${ID[0]}`); if (want ? r < smv.P_FIRE : r >= smv.P_FIRE) return `set${i}`; } };
const base = (o) => Object.assign({ variants: [summary], curId: ID[0], pos: 5, rate: 1, setLevel: 8, setId: seedFor(true), fired: new Set(), analyses }, o);

// 1) the trigger: high set energy AND a first-half song, seeded chance, once per variant, from the matched song
{
  assert.strictEqual(smv.NAME, "$Up3R-M@SS!V3-M0v3");
  assert.strictEqual(smv.firstHalf(7), 3, "7 songs: the first 3 are the first half");
  const r = smv.decide(base());
  assert.ok(r.fire && r.variant === "v1" && r.start === 0, r.why);
  assert.ok(!smv.decide(base({ setLevel: 6 })).fire, "low set energy: no fire");
  assert.ok(!smv.decide(base({ setLevel: null })).fire, "unknown set energy: no fire");
  const k2 = smv.decide(base({ curId: ID[2], pos: 100, setId: (() => { for (let i = 0; ; i++) if (smv.chance(`s${i}|v1|${ID[2]}`) < smv.P_FIRE) return `s${i}`; })() }));
  assert.ok(k2.fire && k2.start === 2, "starts from the matched song's place in the list");
  const second = smv.decide(base({ curId: ID[3], pos: 100, rolled: true }));
  assert.ok(!second.fire && /first half/.test(second.why), "a second-half song never fires");
  assert.ok(!smv.decide(base({ setId: seedFor(false) })).fire, "the seeded roll can say no");
  assert.deepStrictEqual(smv.decide(base()), smv.decide(base()), "same seed, same answer");
  assert.ok(!smv.decide(base({ fired: new Set(["v1"]) })).fire, "once per variant per set");
  assert.ok(!smv.decide(base({ curId: "0000000000000000" })).fire, "a song in no variant");
  assert.ok(!smv.decide(base({ pos: 30 })).fire, "the first lead-in too close (< MIN_LEAD_S)");
  let hits = 0;
  for (let i = 0; i < 2000; i++) if (smv.chance(`x${i}|v1|${ID[0]}`) < smv.P_FIRE) hits++;
  assert.ok(Math.abs(hits / 2000 - smv.P_FIRE) < 0.04, `seeded chance ~ P_FIRE (${hits / 2000})`);
  // manual: any energy, no roll, any song but the last
  assert.ok(smv.decide(base({ setLevel: 2, manual: "v1", setId: seedFor(false), curId: ID[3], pos: 0 })).fire, "the macro ignores energy / roll / half");
  assert.ok(!smv.decide(base({ manual: "v1", curId: ID[6] })).fire, "not from the last song");
}

// 2) builds: never fires inside one, waits for the line after it (the chance kept), v5-only fallback
{
  const an = Object.assign({}, analyses, { [ID[0]]: v6(125, [[0, 15.36]]) });
  const w = smv.decide(base({ analyses: an, pos: 5 }));
  assert.ok(!w.fire && w.wait && w.start === 0 && w.until === smv.lineAfter(an[ID[0]], 15.36), "in a build: wait for the line after it");
  const after = smv.decide(base({ analyses: an, pos: 16, rolled: true }));
  assert.ok(after.fire, "after the build: fires (no second roll)");
  const man = smv.decide(base({ analyses: an, pos: 5, manual: "v1" }));
  assert.ok(man.wait && /build/.test(man.why), "a press during a build waits for the line");
  // the first transition's windows inside a build of either song: blocked, not rolled (not used up)
  const wins = smv.firstWindows(summary, 0);
  const inb = smv.decide(base({ analyses: Object.assign({}, analyses, { [ID[1]]: v6(125, [[wins.in[0] + 2, wins.in[1]]]) }) }));
  assert.ok(!inb.fire && !inb.wait && /inside a build/.test(inb.why) && inb.roll == null, "B's lead-in in a build: no fire, no roll spent");
  // v5 record (no structure): a rising energy_curve over the last 8 bars reads as a build
  const t = Array.from({ length: 40 }, (_, i) => i * 0.5), rising = t.map((x) => 0.2 + 0.03 * x), flat = t.map(() => 0.5);
  const v5 = (curve) => ({ bpm: 125, sections: [], energy_times: t, energy_curve: curve, phrase_boundaries_8bar: [0, 15.36, 30.72, 46.08] });
  assert.ok(smv.buildAt(v5(rising), 19).build, "v5: rising energy is a build");
  assert.strictEqual(smv.buildAt(v5(rising), 19).until, 30.72, "v5: waits for the next phrase line");
  assert.ok(!smv.buildAt(v5(flat), 19).build, "v5: flat energy is not");
  assert.ok(smv.decide(base({ analyses: Object.assign({}, analyses, { [ID[0]]: v5(rising) }), pos: 19 })).wait, "v5 fallback in decide");
}

// 3) schedule: cores back to back on alternating decks, lead-in / exit hosted by the core deck, preload timing
{
  const A = 1000, sch = smv.schedule(plan, A, "a");
  const S = sch.songs;
  assert.deepStrictEqual(S.map((s) => s.deck), ["a", "b", "a", "b", "a", "b", "a"]);
  for (let j = 1; j < S.length; j++) {
    assert.ok(Math.abs(S[j].lead - S[j - 1].end) < 1e-6, "cores back to back");
    assert.strictEqual(S[j].enter.host, S[j - 1].deck, "the lead-in rides the core deck");
    assert.strictEqual(S[j].enter.hp, 120);
    assert.ok(S[j].enter.t0 >= S[j - 1].lead - 1e-6, "no lead-in before the core under it");
    if (j >= 2) {
      assert.ok(S[j].loadBy - S[j].loadFrom >= 20, `song ${j + 1}: >= 20 s to load its deck (${(S[j].loadBy - S[j].loadFrom).toFixed(1)})`);
      assert.ok(S[j].loadFrom >= S[j - 2].end, "its deck is loaded only after that deck's last core ended");
    }
    assert.ok(S[j].enter.t0 - S[j].layerFrom === smv.PRELOAD_S && S[j].layerBy === S[j].enter.t0 - smv.READY_S, "lead-in stem asked PRELOAD_S ahead");
    assert.strictEqual(S[j - 1].exit.host, S[j].deck, "the exit drums ride the next core deck");
  }
  assert.ok(!S[S.length - 1].exit, "the last song has no exit: it plays on into the handback");
  assert.strictEqual(sch.front.on, S[1].enter.t0);
  assert.ok(Math.abs(sch.end - (A + plan.songs[5].out[1])) < 1e-6, "the move ends when the last exit drums end");
  assert.ok(smv.frontAt(sch, sch.front.on, true) && !smv.frontAt(sch, sch.front.on - 0.01, true) && !smv.frontAt(sch, sch.end, true) && !smv.frontAt(sch, sch.front.on + 1, false));
  const lp = smv.layerPlay(S[1].enter, S[1], { ratio: 125 / 124, lag: 0 });
  assert.ok(Math.abs(lp.rate - 1) < 1e-9, "a key-locked set plays at rate 1");
  assert.ok(Math.abs(lp.offset - plan.songs[1].src[0] * 125 / 124) < 1e-9);
}

// 4) parity with the autopilot: HIGH_MIN is the "high" band of autopilotCore.setEnergy; HP_HZ is the plan's sub_hz
{
  for (const pos of [0.05, 0.2, 0.5, 0.9]) for (let lv = 1; lv <= 10; lv++) {
    const r = ap.setEnergy({ setPos: pos, recent: [lv, lv, lv, lv] });
    assert.strictEqual(r.band === "high", r.level >= smv.HIGH_MIN, `set ${r.level}: high band iff >= HIGH_MIN`);
  }
  assert.strictEqual(smv.HP_HZ, plan.rules.sub_hz);
}

// ---- runtime on a fake Host --------------------------------------------------------------------------------------
function world(o = {}) {
  const clock = { t: 0, timers: [], seq: 0 };
  const param = () => ({ value: 0, ev: [], setValueAtTime(v, t) { this.ev.push(["set", v, t]); }, linearRampToValueAtTime(v, t) { this.ev.push(["ramp", v, t]); },
    cancelScheduledValues() {}, setTargetAtTime(v, t) { this.ev.push(["target", v, t]); } });
  const node = () => ({ gain: param(), frequency: param(), connect(x) { return x; }, disconnect() {} });
  const sources = [];
  const audio = { get currentTime() { return clock.t; },
    createGain: node, createBiquadFilter: () => Object.assign(node(), { type: "" }),
    createBufferSource: () => { const s = Object.assign(node(), { playbackRate: { value: 1 }, start(at, off) { this.at = at; this.off = off; }, stop(t) { this.stopT = t; } }); sources.push(s); return s; },
    decodeAudioData: async () => ({ duration: 400 }) };
  const song = (id) => plan.songs.find((s) => s.id === id);
  const deck = (id) => ({ id, playing: false, bpm: null, buffer: null, analysis: null, stems: null, tempoStems: null, _pitchPercent: 0, pos0: 0, t0: 0, _rateRamp: null,
    plays: [], stops: [], inputGain: node(), crossfaderGain: node(), cuePoint: 0,
    _playbackRate() { return 1 + this._pitchPercent / 100; },
    _currentPosition() { return this.playing ? this.pos0 + (clock.t - this.t0) * this._playbackRate() : this.pos0; },
    async useTempoStems(bpm) { this.tempoStems = { bpm }; this.stems = Object.assign({}, this.stems, { ratio: this.bpm / bpm }); return true; },
    setPitchPercent(p) { if (this.playing) { this.pos0 = this._currentPosition(); this.t0 = clock.t; } this._pitchPercent = p; },
    aiSetPitch(p) { this.setPitchPercent(p); return 0; },
    play(pos, spin, when) { this.playing = true; this.pos0 = pos === undefined ? this._currentPosition() : pos; this.t0 = when || clock.t; this.plays.push({ pos: this.pos0, when: this.t0 }); },
    stopNow() { this.pos0 = this._currentPosition(); this.playing = false; this.stops.push(clock.t); },
    stopSourcesAt(t) { this.stopAt = t; }, stemMix() { return true; } });
  const decks = { a: deck("a"), b: deck("b") };
  const state = { trackA: null, trackB: null };
  const els = {};
  const el = (id) => (els[id] = els[id] || { id, value: "", textContent: "", innerHTML: "", on: {}, toggles: [],
    classList: { s: new Set(), toggle(c, on) { if (on) this.s.add(c); else this.s.delete(c); els[id].toggles.push([clock.t, c, !!on]); }, contains(c) { return this.s.has(c); } },
    addEventListener(ev, fn) { this.on[ev] = fn; } });
  const calls = { hold: 0, adopt: [], resume: 0, playStep: [], fetch: [], emit: [], layering: [] };
  const res = (body, ok = true) => ({ ok, status: ok ? 200 : 404, json: async () => body, blob: async () => ({}), arrayBuffer: async () => new ArrayBuffer(8) });
  const failAudio = new Set(o.failAudio || []);
  const api = { fetch: async (url) => {
    calls.fetch.push(url);
    if (url === "/api/supermoves") return res({ move: smv.NAME, variants: o.variants === undefined ? [summary] : o.variants });
    let m;
    if ((m = url.match(/^\/api\/tracks\/(\w+)\/analysis/))) return res(analyses[m[1]]);
    if ((m = url.match(/^\/api\/supermoves\/plan\/v1\?start=(\d+)/))) return res({ plan });
    if ((m = url.match(/^\/api\/tracks\/(\w+)\/stems\?bpm=([\d.]+)/))) return res({ stems: { drums: "u", bass: "u", other: "u", vocals: "u" }, ratio: song(m[1]).bpm / Number(m[2]), bpm: Number(m[2]) });
    if ((m = url.match(/^\/api\/audio\/tracks\/(\w+)/))) return res({}, !failAudio.has(m[1]));
    if (url.startsWith("/api/supermoves/handback")) return res({ pick: { b: "hhhhhhhhhhhhhhhh", b_name: "Handback Song", level: 9 } });
    return res({ stub: true });
  } };
  const apState = { active: true, activeDeck: "a" };
  const host = {
    clock: { now: () => clock.t * 1000, setTimeout: (fn, ms) => { const id = ++clock.seq; clock.timers.push({ id, at: clock.t + Math.max(0, ms) / 1000, fn }); return id; },
      clearTimeout: (id) => { clock.timers = clock.timers.filter((x) => x.id !== id); } },
    audio, decks, state, api, ui: { el, fire() {} },
    bus: { emit: (n, d) => calls.emit.push([n, d]), on() {} }, log: { step() {} },
    loadIntoDeck: async (d, id) => { state[d === "a" ? "trackA" : "trackB"] = id; const x = decks[d]; x.buffer = { duration: 400 }; x.analysis = analyses[id]; x.bpm = song(id).bpm; x.stems = { drums: {}, bass: {}, other: {}, vocals: {}, lag: 0 }; x.tempoStems = null; x._pitchPercent = 0; },
    mod: { autopilotState: apState,
      autopilot: { superMove: { hold: () => calls.hold++, adopt: (x) => { calls.adopt.push(x); apState.activeDeck = x.deck; }, resume: () => calls.resume++ } },
      macroMode: { playStep: async (s, label) => { calls.playStep.push({ s, label }); } },
      djMind: { layering: (s) => calls.layering.push(s) }, beatLayer: { on: true, isEnabled() { return this.on; }, setEnabled(v) { this.on = v; } } },
  };
  const flush = async () => { for (let i = 0; i < 30; i++) await new Promise((r) => setImmediate(r)); };
  async function until(T) {
    for (;;) {
      await flush();
      const due = clock.timers.filter((x) => x.at <= T).sort((a, b) => a.at - b.at || a.id - b.id)[0];
      if (!due) break;
      clock.timers = clock.timers.filter((x) => x !== due);
      clock.t = Math.max(clock.t, due.at);
      due.fn();
    }
    clock.t = Math.max(clock.t, T);
    await flush();
  }
  // Eternity plays on deck A, 5 s in, at its native tempo
  const a = decks.a;
  state.trackA = ID[0]; a.bpm = plan.songs[0].bpm; a.buffer = { duration: 400 }; a.analysis = analyses[ID[0]];
  a.stems = { drums: {}, bass: {}, other: {}, vocals: {}, lag: 0 }; a.playing = true; a.pos0 = 5; a.t0 = 0;
  const rt = smv.create({ host });
  return { rt, clock, calls, decks, els, sources, until, apState, host };
}

(async () => {
  // 5) the whole move: fires, NULL in front for exactly its span, cores on the decks, handback Bass Swap one level lower
  {
    const w = world();
    await w.until(0.1);
    assert.ok(w.rt.armed, "a saved variant arms the hook");
    const took = w.rt.takeOver({ currentId: ID[0], deck: "a", setLevel: 8, setId: seedFor(true) });
    assert.ok(took, "the move takes the booking");
    await w.until(1);
    const sch = w.rt.schedule;
    assert.ok(sch && w.rt.running, "playing");
    assert.strictEqual(w.calls.hold, 1);
    assert.ok(Math.abs(w.decks.a._pitchPercent - (124 / 125 - 1) * 100) < 1e-9, "the playing song key-locked at the plan tempo");
    await w.until(sch.end + 5);
    const t = w.els["nul-super"].toggles.filter(([, c]) => c === smv.FRONT_CLASS);
    assert.deepStrictEqual(t.map(([, , on]) => on), [true, false], "in front once, then back");
    assert.ok(Math.abs(t[0][0] - sch.front.on) < 0.01 && Math.abs(t[1][0] - sch.end) < 0.01, `front from the first transition to the end (${t.map((x) => x[0].toFixed(2))})`);
    assert.deepStrictEqual(w.calls.adopt.map((x) => x.trackId), ID.slice(1), "every song landed in order");
    const plays = [...w.decks.a.plays, ...w.decks.b.plays].sort((x, y) => x.when - y.when);
    assert.strictEqual(plays.length, 6);
    plays.forEach((p, i) => { assert.ok(Math.abs(p.when - sch.songs[i + 1].lead) < 1e-6 && p.pos === plan.songs[i + 1].core.start, `core ${i + 2} on its line`); });
    const layers = w.sources.filter((s) => s.at != null);
    assert.strictEqual(layers.length, 12, "6 lead-ins + 6 exits");
    assert.ok(w.emitCheck === undefined && w.calls.emit.some(([n, d]) => n === "ai-supermove" && d.name === smv.NAME), "NULL-BOT fly-in on the first hit");
    assert.strictEqual(w.calls.playStep.length, 1);
    const hs = w.calls.playStep[0].s;
    assert.ok(hs.recipe === "Bass Swap" && hs.a === ID[6] && hs.b === "hhhhhhhhhhhhhhhh", "handback: Bass Swap from the last song");
    assert.ok(w.calls.fetch.some((u) => /handback\?a=c0309e3905fec60b&level=10/.test(u)), "handback asks one level under the last song");
    assert.strictEqual(w.calls.resume, 1, "the normal set resumes once");
    assert.ok(w.host.mod.beatLayer.on, "beat layer back on");
    assert.strictEqual(w.rt.state, "idle");
    assert.ok(!w.rt.takeOver({ currentId: ID[0], deck: "b", setLevel: 9, setId: seedFor(true) }), "once per variant per set");
  }
  // 6) a song not ready in time: the move ends cleanly into the normal set, no gap, no leftover front / pitch
  {
    const w = world({ failAudio: [ID[3]] });
    await w.until(0.1);
    w.rt.takeOver({ currentId: ID[0], deck: "a", setLevel: 8, setId: seedFor(true) });
    await w.until(1);
    const sch = w.rt.schedule;
    await w.until(sch.end + 5);
    assert.deepStrictEqual(w.calls.adopt.map((x) => x.trackId), ID.slice(1, 3), "landed songs 2 and 3 only");
    const s3 = sch.songs[2];
    assert.ok(w.decks[s3.deck].playing, "song 3 plays on (no gap)");
    assert.ok(!w.decks[s3.deck].stopAt || w.decks[s3.deck].stopAt < s3.lead + 1, "song 3's deck never told to stop at song 4's line");
    const tg = w.els["nul-super"].toggles.filter(([, c]) => c === smv.FRONT_CLASS);
    assert.ok(!tg[tg.length - 1][2] && Math.abs(tg[tg.length - 1][0] - sch.songs[3].loadBy) < 0.01, "NULL back at the early end");
    assert.strictEqual(w.calls.playStep.length, 0, "no handback pick on an early end: the normal set books");
    assert.strictEqual(w.calls.resume, 1);
    assert.strictEqual(w.decks[sch.songs[3].deck]._pitchPercent, 0, "the idle deck is back to neutral tempo");
    assert.strictEqual(w.rt.state, "idle");
  }
  // 7) the autopilot stops mid-move: the move stops too (no resume, nothing restarted)
  {
    const w = world();
    await w.until(0.1);
    w.rt.takeOver({ currentId: ID[0], deck: "a", setLevel: 8, setId: seedFor(true) });
    await w.until(1);
    const sch = w.rt.schedule;
    await w.until(sch.songs[2].lead + 1);
    w.apState.active = false;
    await w.until(sch.end + 5);
    assert.strictEqual(w.calls.resume, 0);
    assert.strictEqual(w.rt.state, "idle");
    assert.ok(!w.els["nul-super"].classList.contains(smv.FRONT_CLASS));
  }
  // 8) isolation: no saved variant = not armed, the autopilot never asks; a non-firing ask changes nothing
  {
    const w = world({ variants: [] });
    await w.until(0.1);
    assert.ok(!w.rt.armed, "nothing saved: the hook is off");
    const w2 = world();
    await w2.until(0.1);
    const before = JSON.stringify(w2.calls);
    // a song in no variant (every song of a normal set): no fetch, no hold, nothing changes
    assert.strictEqual(w2.rt.takeOver({ currentId: "0123456789abcdef", deck: "a", setLevel: 9, setId: seedFor(true) }), false, "not a variant song: no");
    assert.strictEqual(JSON.stringify(w2.calls), before, "a no changes nothing (no fetch, no hold, no log side effect on the set)");
    assert.strictEqual(w2.rt.state, "idle");
    // a variant song at low energy: holds only while its next song's analysis loads, then gives the booking back once
    assert.strictEqual(w2.rt.takeOver({ currentId: ID[0], deck: "a", setLevel: 3, setId: seedFor(true) }), true, "checking");
    await w2.until(0.5);
    assert.strictEqual(w2.rt.state, "idle");
    assert.strictEqual(w2.calls.resume, 1, "low energy: handed straight back");
    assert.strictEqual(w2.calls.hold, 0, "never held the decks");
    assert.strictEqual(w2.rt.takeOver({ currentId: ID[0], deck: "a", setLevel: 3, setId: seedFor(true) }), false, "low energy: no (analysis cached)");
    assert.strictEqual(w2.rt.takeOver({ currentId: ID[5], deck: "a", setLevel: 9, setId: seedFor(true) }) && (await w2.until(1), w2.rt.state), "idle", "second half: no");
    const src = fs.readFileSync(path.join(__dirname, "..", "..", "ui", "static", "autopilot.js"), "utf8");
    assert.ok(/if \(smv && smv\.armed\) \{/.test(src), "the autopilot hook is gated on armed");
  }
  // 9) the macro: a press while a variant song plays starts it by hand; during a build it waits for the line
  {
    const w = world();
    await w.until(0.1);
    w.els["smv-go"].on.click();
    await w.until(1);
    assert.ok(w.rt.running, "pressed: plays from the playing song");
    w.els["smv-go"].on.click();
    await w.until(2);
    assert.strictEqual(w.rt.state, "idle", "pressed again: stopped");
    const wb = world();
    wb.decks.a.analysis = wb.decks.a.analysis;
    Object.assign(analyses[ID[0]], { sections: [{ label: "build", start: 0, end: 15.36 }] });
    await wb.until(0.1);
    wb.els["smv-go"].on.click();
    await wb.until(1);
    assert.strictEqual(wb.rt.state, "waiting", "a press during a build waits");
    await wb.until(16);
    assert.ok(wb.rt.running, "fires on the line after the build");
    analyses[ID[0]].sections = [];
  }
  console.log("super_move_check: ok");
})().catch((e) => { console.error(e); process.exit(1); });
