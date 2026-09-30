// $Up3R-M@SS!V3-M0v3: the owner's saved stem-mashup variants (app/music_brain/supermove/variants/*.json, shipped in git) played
// live on the console's real decks, then handed back to the normal set.
//
// TRIGGER (owner): automatic and random, when the set energy is high (autopilot setEnergy level >= HIGH_MIN,
// its "high" band) AND the playing song is in the FIRST HALF of a saved variant (floor(n / 2): 7 songs -> the
// first 3). Seeded chance P_FIRE per eligible booking point (seed: set id | variant | song), at most once per
// variant per set. Never while the playing song is in an energy build-up (v6 "build" section; a v5-only song:
// energy_curve slope over the last 8 bars >= BUILD_SLOPE), never with the first transition's lead-in inside a
// build of either song: a build waits for its end (the chance is not used up). The MACRO button / Shift+S
// plays a variant by hand at any time (a press during a build waits for the first phrase line after it).
//
// DECKS: the console has 2 decks (A, B), each a full mix + 4 stem sources (key-locked tempo sets). A plan
// needs 3 songs at once (the previous song's exit drums, the core, the next song's "other" lead-in). Mapping:
// every CORE plays on a real deck (A / B alternating, all 4 stems key-locked at the plan tempo); the lead-in and
// the exit are extra stem sources, high-passed at 120 Hz exactly as the plan's parts, fed into the core deck's
// channel (its EQ, fader, crossfader). One deck is audible at a time, so one sub owner, one vocal.
// The next song is loaded onto the idle deck while the core before it plays (a whole core, ~31 s) and its
// "other" stem is decoded PRELOAD_S before its lead-in; not ready READY_S before its line: the move ends
// cleanly (the playing song simply plays on) and the normal set takes over. Never the pre-rendered mp3.
//
// NULL-BOT IN FRONT: from the first transition's lead-in to the move's end, #nul-super (the NULL-BOT
// SUPERMOVE overlay, mascot.js) carries FRONT_CLASS: visible, on the top layer (z-index 1100, over every
// console layer), in its dock pose (null-bot.css). Change the meaning in one place: FRONT_CLASS + its CSS.
//
// ISOLATION: nothing here runs unless a variant is saved (or a move / manual press is pending); the autopilot
// asks takeOver() at each booking point and goes on exactly as before when it says false.
// Pure core (node-testable: app/tests/js/super_move_check.js) + a runtime mounted on the Host port.
(function (root) {
  "use strict";

  const NAME = "$Up3R-M@SS!V3-M0v3";
  const HIGH_MIN = 7;          // = autopilot SET_HIGH_MIN: setEnergy level of the "high" band
  const P_FIRE = 0.35;         // seeded chance per eligible booking point (GUESS: a peak moment, not every set)
  const MIN_LEAD_S = 40;       // the first lead-in at least this far ahead of the takeover (key-lock + decode)
  const PRELOAD_S = 45;        // a song's lead-in stem is fetched this long before its lead-in starts
  const READY_S = 4;           // ... and it (and its deck) must be ready this long before its line
  const BUILD_SLOPE = 0.018;   // v5 fallback: energy_curve slope per second over 8 bars (90th pct of non-build windows)
  const HP_HZ = 120;           // the plan's sub_hz: lead-in and exit stems never carry sub
  const FRONT_CLASS = "nul-front";
  const STEMS = ["drums", "bass", "other", "vocals"];

  const firstHalf = (n) => Math.floor(n / 2);
  function hash32(s) {
    let h = 0x811c9dc5;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
    return h >>> 0;
  }
  // Seeded chance in [0, 1): the same seed always rolls the same number (mulberry32 over FNV-1a).
  function chance(seed) {
    let t = (hash32(String(seed)) + 0x6d2b79f5) >>> 0;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }

  const isV6 = (an) => !!(an && an.structure);
  function slopeOver(an, a, b) {
    const t = (an && an.energy_times) || [], e = (an && an.energy_curve) || [];
    const xs = [], ys = [];
    for (let i = 0; i < t.length && i < e.length; i++) if (t[i] >= a && t[i] <= b) { xs.push(t[i]); ys.push(e[i]); }
    if (xs.length < 4) return null;
    const mx = xs.reduce((s, x) => s + x, 0) / xs.length, my = ys.reduce((s, y) => s + y, 0) / ys.length;
    let num = 0, den = 0;
    for (let i = 0; i < xs.length; i++) { num += (xs[i] - mx) * (ys[i] - my); den += (xs[i] - mx) ** 2; }
    return den > 0 ? num / den : null;
  }
  const barOf = (an) => 240 / ((an && an.bpm) || 124);
  function lineAfter(an, t) {
    const ls = (an && an.phrase_boundaries_8bar) || [];
    const l = ls.find((x) => x >= t - 1e-6);
    return l != null ? l : t;
  }
  // Is `pos` (song seconds) inside an energy build-up? -> {build, until (song s), why}
  function buildAt(an, pos) {
    if (!an) return { build: false, why: "no analysis" };
    if (isV6(an)) {
      const s = (an.sections || []).find((x) => x.label === "build" && pos >= x.start && pos < x.end);
      return s ? { build: true, until: s.end, why: `v6 build ${s.start.toFixed(1)}-${s.end.toFixed(1)} s` } : { build: false, why: "v6: no build here" };
    }
    const w = 8 * barOf(an), k = slopeOver(an, pos - w, pos);
    if (k != null && k >= BUILD_SLOPE) return { build: true, until: lineAfter(an, pos + 0.01), why: `v5 energy rising ${k.toFixed(3)}/s over 8 bars` };
    return { build: false, why: "v5: energy not rising" };
  }
  // A build overlapping [a, b] (song seconds)? -> why | null
  function buildOverlap(an, a, b) {
    if (!an) return null;
    if (isV6(an)) {
      const s = (an.sections || []).find((x) => x.label === "build" && x.start < b && x.end > a);
      return s ? `v6 build ${s.start.toFixed(1)}-${s.end.toFixed(1)} s` : null;
    }
    const k = slopeOver(an, a, b);
    return k != null && k >= BUILD_SLOPE ? `v5 energy rising ${k.toFixed(3)}/s` : null;
  }
  // The first transition from song k: the lead-in rides the end of k's core ("out", k's seconds) and is the
  // stretch of k+1 right before its core ("in", k+1's seconds). v: a variant summary (songs with core, bpm).
  function firstWindows(v, k) {
    const s = v.songs[k], n = v.songs[k + 1], e = n.enter_bars || 0;
    return { out: [s.core.end - e * 240 / s.bpm, s.core.end], in: [n.core.start - e * 240 / n.bpm, n.core.start] };
  }

  // The trigger. o: {variants, curId, pos (song s), rate, setLevel, setId, fired (Set of variant names),
  // analyses {id: analysis}, manual (variant name) | null, rolled (skip the roll: already won)}.
  // -> {fire, wait, variant, start, until (song s, wait only), roll, why}
  function decide(o) {
    let why = "no saved variant has the playing song";
    for (const v of o.variants || []) {
      if (o.manual ? v.name !== o.manual : o.fired && o.fired.has(v.name)) continue;
      const k = (v.songs || []).findIndex((s) => s.id === o.curId);
      if (k < 0) continue;
      if (k > v.songs.length - 2) { why = `${v.name}: the playing song is its last`; continue; }
      if (!o.manual && k >= firstHalf(v.songs.length)) { why = `${v.name}: song ${k + 1} of ${v.songs.length} is past the first half`; continue; }
      if (!o.manual && !(o.setLevel >= HIGH_MIN)) { why = `set energy ${o.setLevel == null ? "unknown" : o.setLevel} < ${HIGH_MIN}`; continue; }
      const w = firstWindows(v, k), an = o.analyses || {};
      const ov = buildOverlap(an[o.curId], w.out[0], w.out[1]) || buildOverlap(an[v.songs[k + 1].id], w.in[0], w.in[1]);
      if (ov) { why = `${v.name}: first transition inside a build (${ov})`; continue; }
      const lead = (w.out[0] - o.pos) / (o.rate || 1);
      if (lead < MIN_LEAD_S) { why = `${v.name}: first lead-in ${lead.toFixed(0)} s away (< ${MIN_LEAD_S})`; continue; }
      let roll = null;
      if (!o.manual && !o.rolled) {
        roll = chance(`${o.setId}|${v.name}|${o.curId}`);
        if (roll >= P_FIRE) { why = `${v.name}: roll ${roll.toFixed(2)} >= ${P_FIRE}`; continue; }
      }
      const b = buildAt(an[o.curId], o.pos);
      if (b.build) return { fire: false, wait: true, variant: v.name, start: k, until: lineAfter(an[o.curId], b.until), roll, why: `wait: ${b.why}` };
      return { fire: true, wait: false, variant: v.name, start: k, roll, why: `${v.name} from song ${k + 1}: ${o.manual ? "pressed" : `set ${o.setLevel}${roll == null ? "" : `, roll ${roll.toFixed(2)}`}`}` };
    }
    return { fire: false, wait: false, why };
  }

  // Every timed thing of a plan (already from_song: song 0 is the playing one) on the audio clock. A = the audio
  // time song 0's core starts on (its deck reaches core.start). d0 = song 0's deck; song j is on d0 when j is even.
  function schedule(plan, A, d0) {
    const other = d0 === "a" ? "b" : "a", deckOf = (j) => (j % 2 === 0 ? d0 : other);
    const songs = plan.songs, n = songs.length, L0 = songs[0].core_out[0], target = plan.bpm;
    const at = (x) => A + (x - L0);
    const env = (p) => p.env.map(([x, g]) => [at(x), g]);
    const out = songs.map((s, j) => {
      const enter = j > 0 ? s.parts.find((p) => p.role === "enter") : null;
      const exit = j < n - 1 ? s.parts.find((p) => p.role === "exit") : null;
      const vm = (s.parts.find((p) => p.stem === "vocals" && p.role === "core") || {}).mute || [];
      const lead = at(s.core_out[0]);
      const e = enter ? env(enter) : null;
      return {
        j, id: s.id, name: s.name, level: s.level, bpm: s.bpm, target, deck: deckOf(j), lead, end: at(s.core_out[1]),
        coreStart: s.core.start,
        enter: e ? { env: e, t0: e[0][0], src0: s.src[0] + (enter.env[0][0] - s.out[0]) * target / s.bpm, stem: "other", hp: enter.hp_hz || HP_HZ, host: deckOf(j - 1) } : null,
        exit: exit ? { env: env(exit), t0: at(exit.env[0][0]), src0: s.core.end + (exit.env[0][0] - s.core_out[1]) * target / s.bpm, stem: exit.stem, hp: exit.hp_hz || HP_HZ, host: deckOf(j + 1) } : null,
        mutes: vm.map(([a, b]) => [at(a), at(b)]),
        // deck j is free once song j-2's core ended (its exit rides deck j-1): load it then, ready READY_S before its line
        loadFrom: j === 0 ? null : j === 1 ? -Infinity : at(songs[j - 2].core_out[1]) + 0.3,
        loadBy: lead - READY_S,
        layerFrom: e ? e[0][0] - PRELOAD_S : null,
        layerBy: e ? e[0][0] - READY_S : null,
      };
    });
    const endT = n >= 2 ? at(songs[n - 2].out[1]) : at(songs[0].core_out[1]);
    return { songs: out, front: { on: out[1] && out[1].enter ? out[1].enter.t0 : out[1] ? out[1].lead : A, off: endT }, hit: out[1] ? out[1].lead : A, end: endT };
  }
  // Is the null bot to be in front at audio time t? (a schedule, a run state) -> bool
  const frontAt = (sch, t, running) => !!(running && sch && t >= sch.front.on && t < sch.front.off);

  // Source playback of a lead-in / exit piece: start offset (buffer s) and rate for a stem set {ratio, lag}.
  // A key-locked set holds song time t at t * ratio and plays at (target / native) * ratio (= 1 when locked).
  function layerPlay(piece, song, bufs) {
    const ratio = (bufs && bufs.ratio) || 1, lag = (bufs && bufs.lag) || 0;
    return { offset: Math.max(0, (piece.src0 + lag) * ratio), rate: (song.target / song.bpm) * ratio };
  }

  const core = { NAME, HIGH_MIN, P_FIRE, MIN_LEAD_S, PRELOAD_S, READY_S, BUILD_SLOPE, HP_HZ, FRONT_CLASS,
    firstHalf, chance, buildAt, buildOverlap, firstWindows, lineAfter, decide, schedule, frontAt, layerPlay, slopeOver };
  if (typeof module !== "undefined" && module.exports) module.exports = Object.assign({ create }, core);   // create: node checks only

  // ---- runtime ------------------------------------------------------------------------------------------------------
  function create({ host }) {
    const { setTimeout, clearTimeout } = host.clock;
    const ui = host.ui;
    let variants = [];              // summaries from GET /api/supermoves (the first saved is the default)
    const analyses = {};            // track id -> analysis (first-half songs and their next, fetched once)
    const fired = new Set();        // variants played this set
    const released = new Set();     // songs the move let go of (the normal set books them)
    let setKey = null;              // the set the fired / released memory belongs to
    let run = null;                 // the move in progress (or waiting / loading)
    let pendingManual = null;       // {variant} pressed with no variant song playing yet

    const log = (decision, why) => { try { host.log.step("supermove", { phase: "move", decision, why: `${NAME}: ${why}` }); } catch (e) { /* no log */ } console.info(`${NAME}: ${decision}: ${why}`); };
    const say = (msg) => { const el = ui.el("smv-status"); if (el) el.textContent = msg; };
    const audioNow = () => (host.audio ? host.audio.currentTime : 0);
    const ap = () => host.mod.autopilot && host.mod.autopilot.superMove;
    const apActive = () => !!(host.mod.autopilotState && host.mod.autopilotState.active);
    async function getJSON(url) {
      const r = await host.api.fetch(url);
      if (!r.ok) throw new Error(`${url}: ${r.status}`);
      return r.json();
    }

    async function refresh() {
      try { variants = ((await getJSON("/api/supermoves")).variants || []).filter((v) => v.songs && v.songs.length >= 2); }
      catch (e) { variants = []; }
      render();
      for (const v of variants) {
        for (const s of v.songs.slice(0, firstHalf(v.songs.length) + 1)) {
          if (analyses[s.id]) continue;
          try { analyses[s.id] = await getJSON(`/api/tracks/${encodeURIComponent(s.id)}/analysis`); } catch (e) { /* decide treats it as unknown */ }
        }
      }
    }
    function render() {
      const sel = ui.el("smv-variant");
      if (sel) sel.innerHTML = variants.length
        ? variants.map((v, i) => `<option value="${v.name}">${NAME} ${i + 1}: ${v.n} songs${v.title && v.title !== NAME ? ` (${String(v.title).replace(/[<>&"]/g, "")})` : ""}</option>`).join("")
        : `<option value="">no saved ${NAME}</option>`;
    }

    // ---- the autopilot's booking point (autopilot.js prepareTransition) --------------------------------------------
    // Sync; true = the move holds this booking (it fires, waits for a build to end, or is already playing).
    function takeOver(o) {
      if (o.setId !== setKey) { setKey = o.setId; fired.clear(); released.clear(); }
      if (run) return true;
      if (released.has(o.currentId)) return false;
      const d = host.decks && host.decks[o.deck];
      if (!d || !d.playing) return false;
      const manual = pendingManual && variants.find((v) => v.name === pendingManual.variant && v.songs.some((s) => s.id === o.currentId)) ? pendingManual.variant : null;
      const r = decide({ variants, curId: o.currentId, pos: d._currentPosition(), rate: d._playbackRate(), setLevel: o.setLevel,
        setId: o.setId, fired, analyses, manual });
      if (manual) pendingManual = null;
      if (!r.fire && !r.wait) {
        if (manual) log("pressed, not played", r.why);
        return false;
      }
      if (r.wait) { waitBuild(r, o, manual); return true; }
      log("fires", r.why);
      start(r.variant, r.start, o.deck, o.currentId);
      return true;
    }
    // A build is playing: hold the booking and look again on the first line after it (the chance is kept).
    function waitBuild(r, o, manual) {
      const d = host.decks[o.deck], gen = {};
      run = { state: "waiting", gen, timers: [], sources: [], variant: r.variant };
      const dt = Math.max(0.5, (r.until - d._currentPosition()) / d._playbackRate());
      log("waits", `${r.why}; looking again on the line after it (${dt.toFixed(1)} s)`);
      say(`${NAME}: waiting for the build to end`);
      run.timers.push(setTimeout(() => {
        if (!run || run.gen !== gen) return;
        run = null;
        if (!apActive()) return;
        const again = decide({ variants, curId: o.currentId, pos: d._currentPosition(), rate: d._playbackRate(), setLevel: o.setLevel,
          setId: o.setId, fired, analyses, manual: manual || r.variant, rolled: true });
        if (again.fire) { log("fires", `${again.why} (after the build)`); start(again.variant, again.start, o.deck, o.currentId); return; }
        if (again.wait) { waitBuild(again, o, manual || r.variant); return; }
        release(o.currentId, `not after the build: ${again.why}`);
      }, dt * 1000));
    }
    // Let the normal set book this song (the hook says false for it from now on).
    function release(currentId, why) {
      released.add(currentId);
      log("hands back", why);
      say("");
      const a = ap();
      if (a && apActive()) a.resume();
    }

    // ---- loading ---------------------------------------------------------------------------------------------------
    async function tempoSet(id, target, gen, needs = STEMS) {
      const t0 = host.clock.now();
      while (run && run.gen === gen && host.clock.now() - t0 < 150000) {
        const v = await getJSON(`/api/tracks/${encodeURIComponent(id)}/stems?bpm=${target.toFixed(2)}&separate=1`);
        if (v.stems) {
          const bufs = { ratio: v.ratio || 1, bpm: v.bpm || target, lag: 0 };
          for (const n of needs) bufs[n] = await host.audio.decodeAudioData(await (await host.api.fetch(v.stems[n])).arrayBuffer());
          return bufs;
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
      return null;
    }
    const wait = async (ok, ms, gen) => { const t0 = host.clock.now(); while (!ok() && host.clock.now() - t0 < ms && run && run.gen === gen) await new Promise((r) => setTimeout(r, 250)); return ok(); };
    // Song on a deck, native stems in, key-locked at the plan tempo (Rubber Band set, as the autopilot does), not playing.
    async function loadSong(deckId, s, gen) {
      const r = await host.api.fetch(`/api/audio/tracks/${encodeURIComponent(s.id)}`);
      if (!r.ok) throw new Error(`cannot load ${s.name}`);
      await host.loadIntoDeck(deckId, s.id, s.name, await r.blob());
      const d = host.decks[deckId];
      if (!(await wait(() => d.buffer && d.analysis, 20000, gen))) throw new Error(`${s.name}: no analysis`);
      await keyLock(d, s.target, gen);
      if (d.cuePoint != null) d.cuePoint = s.coreStart;
      return d;
    }
    async function keyLock(d, target, gen) {
      if (!(await wait(() => !!d.stems, 30000, gen))) throw new Error("no stems");
      const pct = (target / d.bpm - 1) * 100;
      if (Math.abs(pct) > 8) throw new Error(`${pct.toFixed(1)} % is past the 8 % key-lock cap`);
      if (Math.abs(target / d.bpm - 1) > 0.005 && !(d.tempoStems && Math.abs(d.tempoStems.bpm - target) < 0.05)) {
        if (!(await d.useTempoStems(target))) throw new Error("no key-locked stems");
      }
      if (d.playing) d.aiSetPitch(pct); else d.setPitchPercent(pct);
      await wait(() => !d._rateRamp || d._rateRamp.t1 <= audioNow(), 60000, gen);
    }

    // ---- playing ---------------------------------------------------------------------------------------------------
    // A lead-in / exit stem piece into a deck's channel on the audio clock, envelope exactly as the plan's part.
    function layer(piece, song, bufs) {
      const actx = host.audio, buf = bufs && bufs[piece.stem], d = host.decks[piece.host];
      if (!buf || !d || !d.inputGain) return false;
      const p = layerPlay(piece, song, bufs), t0 = piece.env[0][0], t1 = piece.env[piece.env.length - 1][0];
      const src = actx.createBufferSource(), g = actx.createGain(), hp = actx.createBiquadFilter();
      src.buffer = buf; src.playbackRate.value = p.rate;
      hp.type = "highpass"; hp.frequency.value = Math.max(HP_HZ, piece.hp);
      g.gain.setValueAtTime(0, Math.max(audioNow(), t0 - 0.01));
      for (const [t, v] of piece.env) g.gain.linearRampToValueAtTime(v, Math.max(audioNow(), t));
      src.connect(g); g.connect(hp); hp.connect(d.inputGain);
      src.start(Math.max(audioNow(), t0), Math.min(p.offset, buf.duration - 0.01));
      src.stop(t1 + 0.05);
      const rec = { src, g, hp, t1 };
      run.sources.push(rec);
      src.onended = () => { try { hp.disconnect(); } catch (e) { /* gone */ } };
      return true;
    }
    function front(on) {
      const el = ui.el("nul-super");
      if (el && el.classList) el.classList.toggle(FRONT_CLASS, !!on);
    }
    const later = (t, fn) => {
      const gen = run.gen;
      run.timers.push(setTimeout(() => {
        if (!run || run.gen !== gen) return;
        if (!apActive()) return abort("the autopilot stopped");
        try { const p = fn(); if (p && p.catch) p.catch((e) => abort(`error: ${e.message}`)); } catch (e) { abort(`error: ${e.message}`); }
      }, Math.max(0, (t - audioNow()) * 1000)));
    };

    async function start(variant, k, deck0, currentId) {
      const gen = {};
      run = { state: "loading", gen, timers: [], sources: [], variant, deck0, currentId, cur: null, pending: null, beat: null, touched: new Set() };
      fired.add(variant);
      const a = ap();
      if (a) a.hold();
      say(`${NAME}: loading`);
      try {
        const plan = (await getJSON(`/api/supermoves/plan/${encodeURIComponent(variant)}?start=${k}`)).plan;
        if (!run || run.gen !== gen) return;
        const target = plan.bpm, d0 = host.decks[deck0];
        // pre-stretch: every song's key-locked set is asked for now (the server renders and caches it)
        for (const s of plan.songs.slice(1)) host.api.fetch(`/api/tracks/${encodeURIComponent(s.id)}/stems?bpm=${target.toFixed(2)}&separate=1`).catch(() => {});
        const firstBufs = tempoSet(plan.songs[1].id, target, gen, ["other"]);
        await keyLock(d0, target, gen);
        if (!run || run.gen !== gen) return;
        const A = audioNow() + (plan.songs[0].core.start - d0._currentPosition()) / d0._playbackRate();
        const sch = schedule(plan, A, deck0);
        run.sch = sch; run.plan = plan; run.cur = 0;
        const s1 = sch.songs[1];
        if (s1.enter && s1.enter.t0 - audioNow() < READY_S) throw new Error("the first lead-in is already due");
        run.state = "playing";
        play(sch, firstBufs, gen);
      } catch (e) { abort(`could not start: ${e.message}`); }
    }

    function play(sch, firstBufs, gen) {
      const songs = sch.songs, n = songs.length;
      const layerBufs = { 1: firstBufs };
      // the mind's phrase moves and the beat layer stay off the move's decks (restored at the end)
      const total = sch.end - audioNow();
      if (host.mod.djMind && host.mod.djMind.layering) host.mod.djMind.layering(total, { source: "RULE", why: `${NAME}: ${n} songs layered by stems` });
      if (host.mod.beatLayer && host.mod.beatLayer.isEnabled && host.mod.beatLayer.isEnabled()) { host.mod.beatLayer.setEnabled(false); run.beat = true; }
      later(sch.front.on, () => { front(true); say(`${NAME}: playing`); });
      later(sch.hit - 0.3, () => host.bus.emit("ai-supermove", { at: sch.hit, name: NAME, deck: songs[1].deck }));
      for (let j = 1; j < n; j++) {
        const s = songs[j], prev = songs[j - 1];
        if (j >= 2 && s.enter) later(Math.max(audioNow(), s.layerFrom), () => { layerBufs[j] = tempoSet(s.id, s.target, gen, ["other"]); });
        later(Math.max(audioNow(), s.loadFrom), () => {
          run.touched.add(s.deck);
          loadSong(s.deck, s, gen).then(() => { s.ready = true; }, (e) => { s.failed = e.message; });
        });
        if (s.enter) later(s.layerBy, async () => {
          const bufs = await Promise.race([layerBufs[j] || Promise.resolve(null), new Promise((r) => setTimeout(() => r(null), 1000))]);
          if (!run || run.gen !== gen) return;
          if (!bufs) return end(`${s.name}'s lead-in stem was not ready in time`, false);
          layer(s.enter, s, bufs);
        });
        later(s.loadBy, () => {
          if (!s.ready) return end(`${s.name} was not on deck ${s.deck.toUpperCase()} in time${s.failed ? ` (${s.failed})` : ""}`, false);
          const d = host.decks[s.deck], pd = host.decks[prev.deck];
          d.crossfaderGain.gain.setValueAtTime(1, s.lead);
          d.play(s.coreStart, false, s.lead);
          for (const [a, b] of s.mutes) { later(a - 0.2, () => d.stemMix({ vocals: 0 }, a, 0.02)); later(b - 0.2, () => d.stemMix({ vocals: 1 }, b, 0.02)); }
          if (prev.exit && pd.stems) layer(prev.exit, prev, pd.stems);
          if (pd.stopSourcesAt) pd.stopSourcesAt(s.lead);
          run.pending = { j, d, pd };
          later(s.lead + 0.05, () => {
            run.pending = null;
            pd.stopNow();
            const x = ui.el("crossfader");
            if (x) { x.value = s.deck === "a" ? "-1" : "1"; ui.fire(x, "input", true); }
            run.cur = j;
            const a = ap();
            if (a) a.adopt({ deck: s.deck, trackId: s.id, name: s.name, entry: s.coreStart });
          });
        });
      }
      later(sch.end, () => end("done", true));
    }

    // The move's end: stop what it owns, leave the playing song as it is, hand the set back.
    function cleanup(fadeNow) {
      const t = audioNow();
      for (const id of run.timers) clearTimeout(id);
      for (const r of run.sources) {
        if (!fadeNow || r.t1 <= t) continue;
        try { r.g.gain.cancelScheduledValues(t); r.g.gain.setTargetAtTime(0, t, 0.05); r.src.stop(t + 0.4); } catch (e) { /* ended */ }
      }
      if (run.pending) {      // a handover booked but not reached: the old deck plays on, the new one never starts
        const { d, pd } = run.pending;
        try { d.stopNow(); } catch (e) { /* not started */ }
        if (apActive()) { try { pd.play(); } catch (e) { /* stopped */ } }
      }
      // a deck the move loaded that is not playing: back to neutral tempo for the normal set's next load
      for (const id of run.touched || []) {
        const d = host.decks && host.decks[id];
        if (d && !d.playing && d.setPitchPercent) d.setPitchPercent(0);
      }
      front(false);
      if (host.mod.djMind && host.mod.djMind.layering) host.mod.djMind.layering(0, { source: "RULE", why: `${NAME} over` });
      if (run.beat && host.mod.beatLayer) host.mod.beatLayer.setEnabled(true);
      say("");
    }
    async function end(why, handback) {
      if (!run) return;
      const r = run;
      cleanup(!handback);
      run = null;
      if (!apActive()) return log("stops", `${why} (the autopilot is off)`);
      const songs = r.sch ? r.sch.songs : [];
      const last = songs[r.cur || 0];
      log(handback ? "ends" : "ends early", why);
      const a = ap();
      if (handback && last) {
        try {
          const played = songs.map((s) => s.id).join(",");
          const pick = (await getJSON(`/api/supermoves/handback?a=${last.id}&level=${last.level == null ? "" : last.level}&played=${played}`)).pick;
          const mm = host.mod.macroMode;
          if (pick && mm && mm.playStep) {
            log("hands back", `Bass Swap into ${pick.b_name} (energy ${pick.level}, one under ${last.level})`);
            await mm.playStep({ n: 1, a: last.id, b: pick.b, a_name: last.name, b_name: pick.b_name, recipe: "Bass Swap" }, `${NAME} HANDBACK`);
          } else log("hands back", "no Bass Swap partner one level lower: the normal set picks");
        } catch (e) { log("hands back", `handback pick failed (${e.message}): the normal set picks`); }
      }
      if (a) a.resume();
    }
    function abort(why) {
      if (!run) return;
      if (run.state === "waiting" || run.state === "loading" && !run.sch) {
        const cur = run.currentId;
        cleanup(true);
        run = null;
        return cur ? release(cur, why) : undefined;
      }
      end(why, false);
    }

    // ---- the macro (MACRO panel button, Shift+S, ai-action "supermove") -------------------------------------------
    function press() {
      if (run) return abort("stopped by hand");
      const sel = ui.el("smv-variant"), name = (sel && sel.value) || (variants[0] && variants[0].name);
      const v = variants.find((x) => x.name === name);
      if (!v) return say(`no saved ${NAME}`);
      const st = host.mod.autopilotState;
      if (!st || !st.active) return say(`${NAME}: start the autopilot first (the move hands the set back to it)`);
      const deck = st.activeDeck, d = host.decks[deck], tid = deck === "a" ? host.state.trackA : host.state.trackB;
      const r = decide({ variants: [v], curId: tid, pos: d ? d._currentPosition() : 0, rate: d ? d._playbackRate() : 1, fired, analyses, manual: v.name });
      if (r.fire || r.wait) {
        pendingManual = null;
        log("pressed", r.why);
        if (r.wait) return waitBuild(r, { currentId: tid, deck, setId: setKey }, v.name);
        return start(v.name, r.start, deck, tid);
      }
      // too late in (or not) one of its songs: its next song (its first when none plays) is booked next as a Bass Swap
      // through every gate; the move starts when that song plays
      const k = v.songs.findIndex((s) => s.id === tid), nx = v.songs[k >= 0 && k < v.songs.length - 2 ? k + 1 : 0];
      pendingManual = { variant: v.name };
      log("pressed", `${r.why}: booking ${nx.name} next, the move starts when it plays`);
      say(`${NAME}: starts on ${nx.name}`);
      const mm = host.mod.macroMode;
      if (mm && mm.playStep) mm.playStep({ n: 1, a: tid, b: nx.id, a_name: "", b_name: nx.name, recipe: "Bass Swap" }, NAME);
    }
    const on = (id, ev, fn) => { const el = ui.el(id); if (el) el.addEventListener(ev, fn); };
    on("smv-go", "click", press);
    if (root.aiActions && typeof root.aiActions.register === "function") root.aiActions.register("supermove", press);
    if (host.bus && host.bus.on) host.bus.on("keydown", (e) => {
      if (e && e.key === "S" && e.shiftKey && !(e.target && /input|select|textarea/i.test(e.target.tagName || ""))) press();
    });
    refresh();

    return { core, takeOver, press, refresh, abort: (why) => abort(why || "aborted"),
      get armed() { return variants.length > 0 || !!run || !!pendingManual; },
      get running() { return !!run && run.state === "playing"; },
      get state() { return run ? run.state : "idle"; },
      get schedule() { return run ? run.sch || null : null; },
      get variants() { return variants; } };
  }
  if (root.Engine) root.Engine.mount("superMove", create);
})(typeof window !== "undefined" ? window : globalThis);
