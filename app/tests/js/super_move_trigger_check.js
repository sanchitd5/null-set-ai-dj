// node app/tests/js/super_move_trigger_check.js: $Up3R-M@SS!V3-M0v3 live trigger fixes (owner 2026-10-01: "not triggering
// $Up3R-M@SS!V3-M0v3; it should be a macro button itself"). Replays session 2026-10-01_001355 through the real
// super-move.js + macro-mode.js runtimes on a fake Host (virtual clock):
//   1) press while Eternity plays, the server takes 49 s / 80 s to render the 124 BPM stem sets (the live times):
//      immediate feedback, then the layers start on the next free 8-bar line (not "loading" forever, not deferred)
//   2) a second press stops it and says so
//   3) press on a song outside the variant: Syren is booked through the autopilot (armStep + rebook, nothing loaded
//      behind its back); when Syren lands the move starts on its plan; a song past its lead-in books the next one
//   4) the move is an entry of the MACRO list; PLAY STEP / PLAY MACRO with it selected press it
//   5) every automatic eligibility check is a supermove "check" step (energy, place, build, roll)
//   6) NULL in front: class + caption on for the move's span only
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const smv = require("../../ui/static/super-move.js");
const mm = require("../../ui/static/macro-mode.js");
const ap = require("../../ui/static/autopilot.js");

const plan = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "..", "music_brain", "supermove", "variants", "v1.json"), "utf8"));
const summary = { name: "v1", title: smv.NAME, n: plan.songs.length,
  songs: plan.songs.map((s) => ({ i: s.i, id: s.id, name: s.name, level: s.level, bpm: s.bpm, core: { start: s.core.start, end: s.core.end }, enter_bars: s.enter_bars })) };
const ID = plan.songs.map((s) => s.id);
const FELIX = "18898fa081218894", PATOLA = "aaaaaaaaaaaaaaaa", SOULJA = "bbbbbbbbbbbbbbbb";
const v6 = (bpm, duration, builds = []) => ({ bpm, duration, structure: { version: 6 }, sections: builds.map(([a, b]) => ({ label: "build", start: a, end: b })),
  phrase_boundaries_8bar: Array.from({ length: 80 }, (_, i) => i * 8 * 240 / bpm).filter((x) => x < duration), energy_times: [], energy_curve: [] });
const analyses = Object.fromEntries(plan.songs.map((s) => [s.id, v6(s.bpm, 400)]));
analyses[FELIX] = v6(125, 300);

function world(o = {}) {
  const clock = { t: 0, timers: [], seq: 0 };
  const param = () => ({ value: 0, setValueAtTime() {}, linearRampToValueAtTime() {}, cancelScheduledValues() {}, setTargetAtTime() {} });
  const node = () => ({ gain: param(), frequency: param(), connect(x) { return x; }, disconnect() {} });
  const sources = [];
  const audio = { get currentTime() { return clock.t; }, createGain: node, createBiquadFilter: () => Object.assign(node(), { type: "" }),
    createBufferSource: () => { const s = Object.assign(node(), { playbackRate: { value: 1 }, start(at, off) { this.at = at; this.off = off; }, stop(t) { this.stopT = t; } }); sources.push(s); return s; },
    decodeAudioData: async () => ({ duration: 400 }) };
  const song = (id) => plan.songs.find((s) => s.id === id) || { bpm: 125 };
  const renderAt = o.renderAt || {};        // track id -> clock s its 124 BPM key-locked set is rendered (live times)
  const deck = (id) => ({ id, playing: false, bpm: null, buffer: null, analysis: null, stems: null, tempoStems: null, _pitchPercent: 0, pos0: 0, t0: 0, _rateRamp: null,
    plays: [], inputGain: node(), crossfaderGain: node(), cuePoint: 0,
    _playbackRate() { return 1 + this._pitchPercent / 100; },
    _currentPosition() { return this.playing ? this.pos0 + (clock.t - this.t0) * this._playbackRate() : this.pos0; },
    async useTempoStems(bpm) {       // the server's Rubber Band render: resolves when that set is rendered
      const at = renderAt[state[this.id === "a" ? "trackA" : "trackB"]] || 0;
      while (clock.t < at) await new Promise((r) => host.clock.setTimeout(r, 1000));
      this.tempoStems = { bpm }; this.stems = Object.assign({}, this.stems, { ratio: this.bpm / bpm }); return true;
    },
    setPitchPercent(p) { if (this.playing) { this.pos0 = this._currentPosition(); this.t0 = clock.t; } this._pitchPercent = p; },
    aiSetPitch(p) { this.setPitchPercent(p); return 0; },
    play(pos, spin, when) { this.playing = true; this.pos0 = pos === undefined ? this._currentPosition() : pos; this.t0 = when || clock.t; this.plays.push({ pos: this.pos0, when: this.t0 }); },
    stopNow() { this.pos0 = this._currentPosition(); this.playing = false; },
    stopSourcesAt(t) { this.stopAt = t; }, stemMix() { return true; } });
  const decks = { a: deck("a"), b: deck("b") };
  const state = { trackA: null, trackB: null };
  const els = {};
  const el = (id) => (els[id] = els[id] || { id, value: "", textContent: "", innerHTML: "", dataset: {}, attrs: {}, on: {}, style: { props: {}, setProperty(k, v) { this.props[k] = v; } },
    classList: { s: new Set(), toggle(c, on) { if (on) this.s.add(c); else this.s.delete(c); }, contains(c) { return this.s.has(c); } },
    setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; }, addEventListener(ev, fn) { this.on[ev] = fn; } });
  const calls = { hold: 0, adopt: [], resume: 0, rebook: [], steps: [], status: [], loads: [] };
  const res = (body, ok = true) => ({ ok, status: ok ? 200 : 404, json: async () => body, blob: async () => ({}), arrayBuffer: async () => new ArrayBuffer(8) });
  const api = { fetch: async (url) => {
    let m;
    if (url === "/api/supermoves") return res({ move: smv.NAME, variants: [summary] });
    if (url === "/api/macros") return res({ macros: [{ name: "studied-x", title: "Studied X", songs: 3, kind: "studied" }] });
    if (url.startsWith("/api/macros/")) return res({ macro: { name: "studied-x", title: "Studied X", tracks: [PATOLA, SOULJA],
      steps: [{ n: 1, a: PATOLA, b: SOULJA, a_name: "Proper Patola", b_name: "Kiss Me Thru the Phone", recipe: "Stem Bridge", a_time: 60, b_time: 10 }] }, validation: [] });
    if (url === "/api/liked" || url === "/api/studied/sets") return res({ liked: [], sets: [] });
    if ((m = url.match(/^\/api\/tracks\/(\w+)\/analysis/))) return res(analyses[m[1]]);
    if ((m = url.match(/^\/api\/supermoves\/plan\/v1\?start=(\d+)/))) return res({ plan: Object.assign({}, plan, { songs: fromSong(Number(m[1])) }) });
    if ((m = url.match(/^\/api\/tracks\/(\w+)\/stems\?bpm=([\d.]+)/))) {
      if (clock.t < (renderAt[m[1]] || 0)) return res({ rendering: true });          // not rendered yet: poll again
      return res({ stems: { drums: "u", bass: "u", other: "u", vocals: "u" }, ratio: song(m[1]).bpm / Number(m[2]), bpm: Number(m[2]) });
    }
    if ((m = url.match(/^\/api\/audio\/tracks\/(\w+)/))) return res({});
    if (url.startsWith("/api/supermoves/handback")) return res({ pick: o.handback || null });
    return res({});
  } };
  const apState = { active: true, activeDeck: "a", trackId: null };
  const host = {
    clock: { now: () => clock.t * 1000, setTimeout: (fn, ms) => { const id = ++clock.seq; clock.timers.push({ id, at: clock.t + Math.max(0, ms) / 1000, fn }); return id; },
      clearTimeout: (id) => { clock.timers = clock.timers.filter((x) => x.id !== id); } },
    audio, decks, state, api, random: { next: () => 0.5, uuid: () => "u" },
    ui: { el, fire() {}, flag: (id, d) => d, status: (m) => calls.status.push([clock.t, m]) },
    bus: { emit() {}, on() {} },
    log: { step: (kind, x) => calls.steps.push([clock.t, kind, x.decision, x.why]) },
    loadIntoDeck: async (d, id) => { calls.loads.push([d, id]); state[d === "a" ? "trackA" : "trackB"] = id; const x = decks[d]; x.buffer = { duration: 400 }; x.analysis = analyses[id]; x.bpm = song(id).bpm; x.stems = { drums: {}, bass: {}, other: {}, vocals: {}, lag: 0 }; x.tempoStems = null; x._pitchPercent = 0; },
    mod: { autopilotState: apState,
      autopilot: { core: ap, superMove: { hold: () => calls.hold++, adopt: (x) => { calls.adopt.push(x); apState.activeDeck = x.deck; apState.trackId = x.trackId; }, resume: () => calls.resume++,
        // the real rebook drops the booked transition and runs prepareTransition(currentTrackId): its first act is takeOver
        rebook: (why) => { calls.rebook.push(why); const said = host.mod.superMove.takeOver({ currentId: apState.trackId, deck: apState.activeDeck, setLevel: 8, setId: "set" }); calls.rebookTakeOver = said; return { ok: !o.mixing, why: o.mixing ? "a transition is mixing" : "" }; } } },
      djMind: { layering() {} }, beatLayer: { isEnabled() { return false; }, setEnabled() {} } },
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
  const playOn = (d, id, pos) => { const x = decks[d]; state[d === "a" ? "trackA" : "trackB"] = id; x.bpm = song(id).bpm; x.buffer = { duration: 400 }; x.analysis = analyses[id];
    x.stems = { drums: {}, bass: {}, other: {}, vocals: {}, lag: 0 }; x.playing = true; x.pos0 = pos; x.t0 = clock.t; apState.activeDeck = d; apState.trackId = id; };
  playOn("a", o.playing || ID[0], o.pos == null ? 0.6 : o.pos);
  host.mod.macroMode = mm.createRuntime({ host });
  host.mod.superMove = smv.create({ host });
  return { rt: host.mod.superMove, macro: host.mod.macroMode, clock, calls, decks, els, sources, until, apState, playOn, host };
}
// the plan from song k on (supermove.from_song: the clock starts on k's core, k's own lead-in is gone, the rest shifted)
function fromSong(k) {
  if (!k) return plan.songs;
  const off = plan.songs[k].core_out[0], sh = (x) => x - off;
  return plan.songs.slice(k).map((s, j) => Object.assign({}, s, { i: j, enter_bars: j ? s.enter_bars : 0,
    core_out: s.core_out.map(sh), out: s.out.map(sh),
    parts: s.parts.filter((p) => j || p.role !== "enter").map((p) => Object.assign({}, p, { env: p.env.map(([t, g]) => [sh(t), g]) })) }));
}
const kinds = (w, d) => w.calls.steps.filter((x) => x[1] === "supermove" && (!d || x[2] === d));
const lastStatus = (w) => (w.calls.status[w.calls.status.length - 1] || [0, ""])[1];

(async () => {
  // 1) the session's first press: Eternity 0.6 s in on deck A, Eternity's 124 BPM set rendered 49 s later, Syren's 80 s later
  {
    const w = world({ renderAt: { [ID[0]]: 49, [ID[1]]: 80 } });
    await w.until(0.1);
    w.rt.press();
    await w.until(0.5);
    assert.ok(/rendering the stems at 124 BPM; layers start at 0:59 \(in 59 s\)/.test(lastStatus(w)), `immediate feedback: ${lastStatus(w)}`);
    assert.ok(/layers start at 0:59/.test(w.els["macro-status"].textContent), "on the MACRO panel status too");
    await w.until(28);                                         // the moment the owner pressed again live
    assert.strictEqual(w.rt.state, "loading", "still rendering at 28 s (that was the silence), and the status says why");
    await w.until(82);
    assert.strictEqual(w.rt.state, "playing", "stems in: the move is playing, not deferred");
    const moved = kinds(w, "moves")[0];
    assert.ok(moved && /first lead-in starts on the 8-bar line/.test(moved[3]), "the drop at 0:59 had passed: moved to the next free line");
    const sch = w.rt.schedule, pos = w.decks.a._currentPosition();
    assert.ok(sch.front.on > w.clock.t && sch.front.on - w.clock.t < 20, `layers within one 8-bar line (${(sch.front.on - w.clock.t).toFixed(1)} s)`);
    assert.ok(/layers start in \d+ s/.test(lastStatus(w)), `countdown: ${lastStatus(w)}`);
    const anchorPos = pos + (sch.front.on - w.clock.t) * w.decks.a._playbackRate();
    assert.ok(Math.abs(anchorPos / (8 * 240 / 125) - Math.round(anchorPos / (8 * 240 / 125))) < 0.02, `on an 8-bar line of Eternity (${anchorPos.toFixed(2)} s)`);
    await w.until(sch.front.on + 0.1);
    const lead = w.sources.find((s) => Math.abs(s.at - sch.front.on) < 0.05);
    assert.ok(lead, "Syren's lead-in stem starts on that line (the first layer)");
    const bot = w.els["nul-super"];
    assert.ok(bot.classList.contains(smv.FRONT_CLASS) && bot.attrs[smv.FRONT_CAP] === smv.NAME, "NULL in front, captioned");
    assert.strictEqual(bot.style.props["--front-beat"], "484ms", "dancing on the plan's beat (124 BPM)");
    // 2) the second press stops it, and says so; NULL back to normal
    w.rt.press();
    await w.until(w.clock.t + 1);
    assert.strictEqual(w.rt.state, "idle");
    assert.ok(/stopped by hand/.test(lastStatus(w)), `says so: ${lastStatus(w)}`);
    assert.ok(!bot.classList.contains(smv.FRONT_CLASS) && !(smv.FRONT_CAP in bot.attrs), "NULL back to normal");
  }
  // the old start: rendered in time (cached sets) keeps the planned place, the drop at 0:59
  {
    const w = world();
    await w.until(0.1);
    w.rt.press();
    await w.until(2);
    assert.strictEqual(w.rt.state, "playing");
    assert.ok(Math.abs(w.rt.schedule.front.on - (59.606 - 0.6) / w.decks.a._playbackRate()) < 0.6, "cached stems: layers on the planned drop line");
    assert.strictEqual(kinds(w, "moves").length, 0);
  }
  // 3) a song outside the variant (Felix): the variant's FIRST song is booked, through the autopilot
  {
    const w = world({ playing: FELIX, pos: 30 });
    await w.until(0.1);
    w.rt.press();
    await w.until(0.5);
    assert.deepStrictEqual(w.calls.loads, [], "nothing loaded behind the autopilot's back");
    assert.strictEqual(w.calls.rebook.length, 1, "the autopilot re-books now");
    const cands = await w.macro.firstCandidates(FELIX);
    assert.ok(cands[0] && cands[0].track_id === ID[0] && cands[0]._macro.byUser, "Eternity (its first song) is the armed step");
  }
  // 3a) the session's second press: Eternity too late for its lead-in (no line left before its end) -> Syren booked; Syren
  //     lands on deck B (live it played there under Felix's name: macroMode.playStep loaded it behind the autopilot's back)
  {
    const keep = analyses[ID[0]];
    analyses[ID[0]] = v6(125, 190);
    const w = world({ playing: ID[0], pos: 160 });
    await w.until(0.1);
    w.rt.press();
    await w.until(0.5);
    analyses[ID[0]] = keep;
    assert.deepStrictEqual(w.calls.loads, [], "nothing loaded behind the autopilot's back (Syren under Felix's name live)");
    assert.strictEqual(w.calls.rebook.length, 1, "the autopilot re-books now");
    assert.strictEqual(w.calls.rebookTakeOver, false, "its takeOver on the pressed song says no: the normal booking books Syren");
    const cands = await w.macro.firstCandidates(ID[0]);
    assert.ok(cands[0] && cands[0].track_id === ID[1] && cands[0]._macro && cands[0]._macro.byUser, "Syren is the armed step (measured gates waived)");
    assert.ok(/booking Anyma & Rebūke - Syren .* next .*the move starts when it lands/.test(lastStatus(w)), lastStatus(w));
    // Syren lands on deck B (the autopilot's transition), entered at 20 s: the move starts from it
    w.decks.a.playing = false;
    w.playOn("b", ID[1], 20);
    assert.strictEqual(w.rt.takeOver({ currentId: ID[1], deck: "b", setLevel: 5, setId: "set" }), true);
    await w.until(3);
    assert.strictEqual(w.rt.state, "playing", "the booked song landed: the move plays");
    assert.ok(kinds(w, "starts (the booked song landed)").length === 1, "logged");
    assert.strictEqual(w.rt.schedule.songs[0].id, ID[1], "from Syren's place in the list");
    assert.ok(w.rt.schedule.front.on > w.clock.t, "layers ahead on its plan timeline");
  }
  // 3b) the press on Syren 229 s in (live: "first lead-in -40 s away"): no line left for 31 s before its end -> books Horizon;
  //     with room left it starts on the next free line instead of deferring
  {
    analyses[ID[1]] = v6(125, 300);
    const w = world({ playing: ID[1], pos: 229 });
    await w.until(0.1);
    w.rt.press();
    await w.until(0.5);
    assert.ok(/booking ARTBAT - Horizon/.test(lastStatus(w)), lastStatus(w));
    analyses[ID[1]] = v6(125, 400);
    const w2 = world({ playing: ID[1], pos: 229 });
    await w2.until(0.1);
    w2.rt.press();
    await w2.until(2);
    assert.strictEqual(w2.rt.state, "playing", "past its lead-in with room left: starts on the next free line");
    assert.strictEqual(w2.calls.rebook.length, 0);
  }
  // 3c) a transition is mixing: the booking is refused and it says so; another song landing drops the press
  {
    const w = world({ playing: FELIX, pos: 30, mixing: true });
    await w.until(0.1);
    w.rt.press();
    await w.until(0.5);
    assert.ok(/a transition is mixing, press again after it/.test(lastStatus(w)), lastStatus(w));
    const w2 = world({ playing: FELIX, pos: 30 });
    await w2.until(0.1);
    w2.rt.press();
    await w2.until(0.5);
    w2.playOn("b", "0123456789abcdef", 10);
    assert.strictEqual(w2.rt.takeOver({ currentId: "0123456789abcdef", deck: "b", setLevel: 8, setId: "set" }), false);
    assert.ok(kinds(w2, "dropped").length === 1 && /did not land/.test(lastStatus(w2)), "the press is dropped, and it says so");
    // a second press while booked cancels the move (the song stays booked) and says so
    const w3 = world({ playing: FELIX, pos: 30 });
    await w3.until(0.1);
    w3.rt.press(); await w3.until(0.5);
    w3.rt.press(); await w3.until(1);
    assert.ok(/stopped by hand, .* stays booked/.test(lastStatus(w3)), lastStatus(w3));
    w3.playOn("b", ID[1], 20);
    assert.strictEqual(w3.rt.takeOver({ currentId: ID[1], deck: "b", setLevel: 3, setId: "set" }), true, "checking");
    await w3.until(3);
    assert.strictEqual(w3.rt.state, "idle", "stopped: Syren landing does not start it");
  }
  // 4) the MACRO list: one entry per shipped variant, first; selecting it + PLAY STEP / PLAY MACRO presses it
  {
    const opts = mm.superMoveOptions([summary]);
    assert.deepStrictEqual(opts.map((x) => x.value), ["supermove:v1"]);
    assert.ok(opts[0].label.startsWith(smv.NAME) && /7 songs/.test(opts[0].label));
    assert.deepStrictEqual(mm.superMoveOptions(null), [], "no variant: no entry (the list as before)");
    const w = world();
    await w.until(0.5);
    const html = w.els["macro-select"].innerHTML;
    assert.ok(html.indexOf('value="supermove:v1"') > 0 && html.indexOf('value="supermove:v1"') < html.indexOf("studied-x"), "the move's entry, above the studied sets");
    await w.macro.loadMacro("supermove:v1");
    assert.ok(/Eternity/.test(w.els["macro-steps"].innerHTML), "its songs in the step list");
    await w.macro.ACTIONS["macro-play"]();
    await w.until(2);
    assert.strictEqual(w.rt.state, "playing", "PLAY MACRO with the entry selected starts it now");
    await w.macro.ACTIONS["macro-step"]();
    await w.until(3);
    assert.strictEqual(w.rt.state, "idle", "PLAY STEP again stops it");
    await w.macro.loadMacro("studied-x");
    assert.strictEqual(w.macro.superPick, null, "a normal macro picked: the buttons are the macro's again");
    const html0 = fs.readFileSync(path.join(__dirname, "..", "..", "ui", "static", "index.html"), "utf8");
    assert.ok(!/smv-go|smv-variant/.test(html0), "no separate button / dropdown any more");
  }
  // 5) the automatic trigger: every check is a supermove "check" step; the unmeasured playing song's own level counts
  {
    const w = world();
    await w.until(0.1);
    // a fresh set on Eternity: arc build 6, nothing measured yet -> set 6 without the song, 7+ with its level
    const alone = ap.setEnergy({ setPos: 0.1, recent: [] }).level;
    assert.strictEqual(alone, 6, "the autopilot's own number at the first booking: one band low");
    w.rt.takeOver({ currentId: ID[0], deck: "a", setLevel: alone, setId: "s1", setPos: 0.1, recent: [], curMeasured: false });
    await w.until(0.5);
    const chk = kinds(w, "check");
    assert.ok(chk.length >= 1, "a check step");
    const line = chk[chk.length - 1][3];
    assert.ok(/song 1 of 7 \(first half: yes\), set energy \d+ \(>= 7: yes\), build: none, roll 0\.\d\d/.test(line), line);
    const w2 = world();
    await w2.until(0.1);
    w2.rt.takeOver({ currentId: ID[4], deck: "a", setLevel: 9, setId: "s1" });
    await w2.until(0.5);
    assert.ok(/song 5 of 7 \(first half: no\).*past the first half/.test(kinds(w2, "check").pop()[3]), "a no is logged with its reason");
    const w3 = world();
    await w3.until(0.1);
    const before = w3.calls.steps.length;
    assert.strictEqual(w3.rt.takeOver({ currentId: "0123456789abcdef", deck: "a", setLevel: 9, setId: "s1" }), false);
    assert.strictEqual(w3.calls.steps.length, before, "a song in no variant: no check, nothing logged (off-path unchanged)");
  }
  // 7) "stuck on hold with Proper Patola" (00:50:38): PLAY MACRO loaded the macro's A onto the autopilot's playing deck
  //    (its song's booking and clock stayed, the deadline held 6 s in). With the autopilot on it never loads under it.
  {
    for (const stopped of [false, true]) {
      const w = world();
      await w.until(0.1);
      if (stopped) w.decks.a.playing = false;       // even a paused deck: the autopilot still owns it
      await w.macro.loadMacro("studied-x");
      await w.macro.ACTIONS["macro-play"]();
      await w.until(0.5);
      assert.deepStrictEqual(w.calls.loads, [], `no load onto the autopilot's deck (${stopped ? "paused" : "playing"})`);
      assert.ok(/not an A of studied-x/.test(w.els["macro-status"].textContent), w.els["macro-status"].textContent);
      assert.strictEqual(w.macro.running, false);
    }
    // the move's handback: armed and booked by the autopilot (rebook), not loaded by macro mode behind it
    const w = world({ handback: { b: "cccccccccccccccc", b_name: "Clearest Blue", level: 9 } });
    await w.until(0.1);
    w.rt.press();
    await w.until(2);
    await w.until(w.rt.schedule.end + 5);
    assert.strictEqual(w.rt.state, "idle");
    assert.ok(w.calls.rebook.some((x) => /hands back: Clearest Blue/.test(x)), "handback booked through rebook");
    assert.ok(!w.calls.loads.some(([, id]) => id === "cccccccccccccccc"), "the handback song is not loaded behind the autopilot");
    assert.strictEqual(w.calls.resume, 0, "rebook books it: no second search");
  }
  // the autopilot side (source):the hook passes the unmeasured flag; rebook drops only the booked transition's timer
  {
    const src = fs.readFileSync(path.join(__dirname, "..", "..", "ui", "static", "autopilot.js"), "utf8");
    assert.ok(/curMeasured: Number\.isFinite\(measuredById\[currentId\]\)/.test(src));
    const rb = src.slice(src.indexOf("rebook(why) {"), src.indexOf("rebook(why) {") + 700);
    assert.ok(/if \(mixingPair\) return \{ ok: false/.test(rb) && /clearInterval\(bookedTick\)/.test(rb) && /prepareTransition\(currentTrackId\)/.test(rb), "rebook");
    assert.ok(/runTimers\.push\(tick\);\n    bookedTick = tick;/.test(src), "the booked transition's timer is the one rebook drops");
  }
  console.log("super_move_trigger_check: ok");
})().catch((e) => { console.error(e); process.exit(1); });
