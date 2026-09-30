// $Up3R-M@SS!V3-M0v3: the owner's saved stem-mashup variants (app/music_brain/supermove/variants/*.json, shipped in git) played
// live on the console's real decks, then handed back to the normal set.
//
// TRIGGER (owner): automatic and random, when the set energy is high (autopilot setEnergy level >= HIGH_MIN,
// its "high" band) AND the playing song is in the FIRST HALF of a saved variant (floor(n / 2): 7 songs -> the
// first 3). Seeded chance P_FIRE per eligible booking point (seed: set id | variant | song), at most once per
// variant per set. Never while the playing song is in an energy build-up (v6 "build" section; a v5-only song:
// energy_curve slope over the last 8 bars >= BUILD_SLOPE), never with the first transition's lead-in inside a
// build of either song: a build waits for its end (the chance is not used up). A planned first lead-in that is
// too close (< MIN_LEAD_S) or in a build moves to the next free 8-bar line (nextAnchor). Every automatic check is
// logged (supermove "check": song place, set energy, build, roll). The MACRO list entry per variant (macro-mode.js)
// / Shift+S plays it by hand at any time (a press during a build waits for the first phrase line after it).
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
// console layer), in the centre of the screen, dancing, captioned NAME (FRONT_CAP; null-bot.css), clicks passing
// through it. Change the meaning in one place: FRONT_CLASS + its CSS.
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
  const FRONT_CAP = "data-front-cap";   // #nul-super attribute: the caption null-bot.css shows while it is in front
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
  // Where the first lead-in starts when its planned place (the end of song k's core) is too close or already past:
  // the first 8-bar phrase line at or after `from` (song s; the analysis' sections sit on this grid) where a lead-in
  // of `len` s fits before the song's end and overlaps no build. -> song s | null (no analysis / no line left)
  function nextAnchor(an, from, len) {
    const ls = (an && an.phrase_boundaries_8bar) || [];
    const end = an && Number.isFinite(an.duration) ? an.duration : Infinity;
    for (const l of ls) if (l >= from - 1e-6 && l + len <= end && !buildOverlap(an, l, l + len)) return l;
    return null;
  }

  // The trigger. o: {variants, curId, pos (song s), rate, setLevel, setId, fired (Set of variant names),
  // analyses {id: analysis}, manual (variant name) | null, rolled (skip the roll: already won)}.
  // -> {fire, wait, variant, start, anchor (song s: the first lead-in starts there), moved (why it is not the planned
  //    place) | null, until (song s, wait only), roll, why, checks [{variant, song, of, setLevel, roll, build, anchor, why}]}
  // The planned lead-in too close (< MIN_LEAD_S) or in a build moves to the next free 8-bar line (it never defers).
  function decide(o) {
    let why = "no saved variant has the playing song";
    const checks = [];
    for (const v of o.variants || []) {
      if (o.manual ? v.name !== o.manual : o.fired && o.fired.has(v.name)) continue;
      const k = (v.songs || []).findIndex((s) => s.id === o.curId);
      if (k < 0) continue;
      const n = v.songs.length, an = o.analyses || {}, rate = o.rate || 1;
      const c = { variant: v.name, song: k + 1, of: n, setLevel: o.setLevel == null ? null : o.setLevel, roll: null, build: null, anchor: null, why: null };
      checks.push(c);
      const no = (w) => { why = c.why = w; };
      if (k > n - 2) { no(`${v.name}: the playing song is its last`); continue; }
      if (!o.manual && !o.rolled) c.roll = chance(`${o.setId}|${v.name}|${o.curId}`);
      const b = buildAt(an[o.curId], o.pos);
      c.build = b.build ? b.why : "none";
      if (!o.manual && k >= firstHalf(n)) { no(`${v.name}: song ${k + 1} of ${n} is past the first half`); continue; }
      if (!o.manual && !(o.setLevel >= HIGH_MIN)) { no(`set energy ${o.setLevel == null ? "unknown" : o.setLevel} < ${HIGH_MIN}`); continue; }
      const w = firstWindows(v, k), len = w.out[1] - w.out[0];
      const inOv = o.manual ? null : buildOverlap(an[v.songs[k + 1].id], w.in[0], w.in[1]);
      if (inOv) { no(`${v.name}: the next song's lead-in stretch is inside a build (${inOv})`); continue; }
      const lead = (w.out[0] - o.pos) / rate, outOv = buildOverlap(an[o.curId], w.out[0], w.out[1]);
      let anchor = w.out[0], moved = null;
      if (lead < MIN_LEAD_S || outOv) {
        moved = lead < MIN_LEAD_S ? `planned first lead-in ${lead.toFixed(0)} s away (< ${MIN_LEAD_S})` : `planned first lead-in inside a build (${outOv})`;
        anchor = nextAnchor(an[o.curId], o.pos + MIN_LEAD_S * rate, len);
        if (anchor == null) { no(`${v.name}: ${moved}, no 8-bar line left for a ${len.toFixed(0)} s lead-in`); continue; }
      }
      c.anchor = anchor;
      if (c.roll != null && c.roll >= P_FIRE) { no(`${v.name}: roll ${c.roll.toFixed(2)} >= ${P_FIRE}`); continue; }
      const res = { variant: v.name, start: k, anchor, moved, roll: c.roll, checks };
      const at = moved ? `; first lead-in on the 8-bar line at ${anchor.toFixed(1)} s (${moved})` : "";
      if (b.build) { c.why = `waits: ${b.why}`; return Object.assign(res, { fire: false, wait: true, until: lineAfter(an[o.curId], b.until), why: `wait: ${b.why}` }); }
      c.why = "fires";
      return Object.assign(res, { fire: true, wait: false,
        why: `${v.name} from song ${k + 1}: ${o.manual ? "pressed" : `set ${o.setLevel}${c.roll == null ? "" : `, roll ${c.roll.toFixed(2)}`}`}${at}` });
    }
    return { fire: false, wait: false, why, checks };
  }
  // One eligibility check as the owner reads it in the step log (supermove "check").
  function checkLine(c) {
    const yes = (b) => (b ? "yes" : "no");
    const lv = c.setLevel == null ? "unknown" : c.setLevel;
    return `${c.variant} song ${c.song} of ${c.of} (first half: ${yes(c.song - 1 < firstHalf(c.of))}), set energy ${lv} (>= ${HIGH_MIN}: ${yes(c.setLevel >= HIGH_MIN)}), ` +
      `build: ${c.build || "?"}, roll ${c.roll == null ? "-" : `${c.roll.toFixed(2)} (< ${P_FIRE}: ${yes(c.roll < P_FIRE)})`}` +
      `${c.anchor == null ? "" : `, first lead-in at ${c.anchor.toFixed(1)} s`} -> ${c.why}`;
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

  const core = { NAME, HIGH_MIN, P_FIRE, MIN_LEAD_S, PRELOAD_S, READY_S, BUILD_SLOPE, HP_HZ, FRONT_CLASS, FRONT_CAP,
    firstHalf, chance, buildAt, buildOverlap, firstWindows, lineAfter, nextAnchor, decide, checkLine, schedule, frontAt, layerPlay, slopeOver };
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
    let pendingManual = null;       // {variant, b, name, from}: a press booked song b next (from the song `from`)

    const log = (decision, why) => { try { host.log.step("supermove", { phase: "move", decision, why: `${NAME}: ${why}` }); } catch (e) { /* no log */ } console.info(`${NAME}: ${decision}: ${why}`); };
    // the press's feedback: the console status line and the MACRO panel's status (macro-mode.js writes the same one)
    const say = (msg) => {
      const el = ui.el("macro-status");
      if (el) el.textContent = msg;
      if (msg && ui.status) ui.status(msg);
    };
    const fmt = (t) => (Number.isFinite(t) ? `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}` : "?");
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
    }
    // Analyses a booking point needs (the song after the playing one in each variant holding it), fetched only when a
    // variant song plays: a set with no variant song never asks for anything. -> the ids still missing
    function missingFor(curId) {
      const ids = [];
      for (const v of variants) {
        const k = v.songs.findIndex((s) => s.id === curId);
        if (k >= 0 && k < v.songs.length - 1 && !(v.songs[k + 1].id in analyses)) ids.push(v.songs[k + 1].id);
      }
      return [...new Set(ids)];
    }
    // the variants are entries of the MACRO list itself (macro-mode.js superMoveOptions): redraw it once they are in
    function render() {
      const mm = host.mod.macroMode;
      if (mm && mm.renderList) mm.renderList();
    }

    // ---- the autopilot's booking point (autopilot.js prepareTransition) --------------------------------------------
    // Sync; true = the move holds this booking (it fires, waits for a build to end, or is already playing).
    function takeOver(o) {
      if (o.setId !== setKey) { setKey = o.setId; fired.clear(); released.clear(); }
      if (run) return true;
      if (pendingManual) {
        // the press booked a song: the booking from the song it was pressed on is the normal set's (it books that song)
        if (o.currentId === pendingManual.from) return false;
        if (o.currentId !== pendingManual.b) {
          log("dropped", `${pendingManual.name} was booked but another song landed: the press is dropped`);
          say(`${NAME}: ${pendingManual.name} did not land (a gate refused it): not started`);
          pendingManual = null;
        }
      }
      if (released.has(o.currentId)) return false;
      const d = host.decks && host.decks[o.deck];
      if (!d || !d.playing) return false;
      if (!(o.currentId in analyses) && d.analysis && variants.some((v) => v.songs.some((s) => s.id === o.currentId))) analyses[o.currentId] = d.analysis;
      const need = missingFor(o.currentId);
      if (need.length) {      // a variant song plays: hold this booking while its next song's analysis loads, then ask again
        const gen = {};
        run = { state: "checking", gen, timers: [], sources: [], currentId: o.currentId };
        Promise.all(need.map((id) => getJSON(`/api/tracks/${encodeURIComponent(id)}/analysis`).then((a) => { analyses[id] = a; }, () => { analyses[id] = null; })))
          .then(() => {
            if (!run || run.gen !== gen) return;
            run = null;
            if (!apActive()) return;
            if (!takeOver(o)) { const a = ap(); if (a) a.resume(); }
          });
        return true;
      }
      const manual = pendingManual && pendingManual.b === o.currentId ? pendingManual.variant : null;
      const r = decide({ variants, curId: o.currentId, pos: d._currentPosition(), rate: d._playbackRate(), setLevel: levelWith(o),
        setId: o.setId, fired, analyses, manual });
      if (!manual) for (const c of r.checks) log("check", checkLine(c));      // every automatic check, fired or not
      if (manual) pendingManual = null;
      if (!r.fire && !r.wait) {
        if (manual) { log("pressed, not played", r.why); say(`${NAME}: not started (${r.why})`); }
        return false;
      }
      if (r.wait) { waitBuild(r, o, manual); return true; }
      log(manual ? "starts (the booked song landed)" : "fires", r.why);
      start(r.variant, r.start, o.deck, o.currentId, r.anchor, !!manual);
      return true;
    }
    // The set energy of the check. At a booking point the playing song has usually not been measured yet (the autopilot
    // measures A while it evaluates a B), so its level is missing from the rolling window: a fresh set sat one band low
    // ("set 6") on the very song the move starts from. Here the variant's own level of the song stands in for it.
    function levelWith(o) {
      if (o.curMeasured !== false || !Array.isArray(o.recent)) return o.setLevel;
      const core = host.mod.autopilot && host.mod.autopilot.core;
      let lv = null;
      for (const v of variants) { const s = v.songs.find((x) => x.id === o.currentId); if (s && Number.isFinite(s.level)) { lv = s.level; break; } }
      if (lv == null || !core || !core.setEnergy) return o.setLevel;
      return core.setEnergy({ setPos: o.setPos, recent: [...o.recent, lv] }).level;
    }
    // A build is playing: hold the booking and look again on the first line after it (the chance is kept).
    function waitBuild(r, o, manual) {
      const d = host.decks[o.deck], gen = {};
      run = { state: "waiting", gen, timers: [], sources: [], variant: r.variant, manual: !!manual, currentId: o.currentId };
      const dt = Math.max(0.5, (r.until - d._currentPosition()) / d._playbackRate());
      log("waits", `${r.why}; looking again on the line after it (${dt.toFixed(1)} s)`);
      say(`${NAME}: waiting for the build to end (${dt.toFixed(0)} s), then it starts`);
      run.timers.push(setTimeout(() => {
        if (!run || run.gen !== gen) return;
        run = null;
        if (!apActive()) return;
        const again = decide({ variants, curId: o.currentId, pos: d._currentPosition(), rate: d._playbackRate(), setLevel: o.setLevel,
          setId: o.setId, fired, analyses, manual: manual || r.variant, rolled: true });
        if (again.fire) { log("fires", `${again.why} (after the build)`); start(again.variant, again.start, o.deck, o.currentId, again.anchor, !!manual); return; }
        if (again.wait) { waitBuild(again, o, manual || r.variant); return; }
        release(o.currentId, `not after the build: ${again.why}`);
      }, dt * 1000));
    }
    // Let the normal set book this song (the hook says false for it from now on).
    function release(currentId, why) {
      released.add(currentId);
      log("hands back", why);
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
    // NULL in the centre, dancing on the plan's beat, the move's name under it (null-bot.css .nul-front); off: back to normal
    function front(on, bpm) {
      const el = ui.el("nul-super");
      if (!el) return;
      if (el.classList) el.classList.toggle(FRONT_CLASS, !!on);
      if (on) {
        if (el.setAttribute) el.setAttribute(FRONT_CAP, NAME);
        if (bpm > 0 && el.style && el.style.setProperty) el.style.setProperty("--front-beat", `${Math.round(60000 / bpm)}ms`);
      } else if (el.removeAttribute) el.removeAttribute(FRONT_CAP);
    }
    const later = (t, fn) => {
      const gen = run.gen;
      run.timers.push(setTimeout(() => {
        if (!run || run.gen !== gen) return;
        if (!apActive()) return abort("the autopilot stopped");
        try { const p = fn(); if (p && p.catch) p.catch((e) => abort(`error: ${e.message}`)); } catch (e) { abort(`error: ${e.message}`); }
      }, Math.max(0, (t - audioNow()) * 1000)));
    };

    // anchor: song s of the playing song where the first lead-in starts (decide; null = its planned place, the end of
    // its core). The key-locked stem sets are rendered by the server first (Eternity at 124 BPM took 49 s live on
    // 2026-10-01): the lead-in is placed only once they are in, on its anchor when that is still ahead, else on the
    // next free 8-bar line (nextAnchor), never deferred.
    async function start(variant, k, deck0, currentId, anchor = null, manual = false) {
      const gen = {};
      run = { state: "loading", gen, timers: [], sources: [], variant, deck0, currentId, manual, cur: null, pending: null, beat: null, touched: new Set() };
      fired.add(variant);
      const a = ap();
      if (a) a.hold();
      say(`${NAME} ${variant}: loading its plan`);
      try {
        const plan = (await getJSON(`/api/supermoves/plan/${encodeURIComponent(variant)}?start=${k}`)).plan;
        if (!run || run.gen !== gen) return;
        const target = plan.bpm, d0 = host.decks[deck0], s0 = plan.songs[0], s1 = plan.songs[1];
        const want = anchor == null ? s0.core.start : anchor;
        const inS = (want - d0._currentPosition()) / d0._playbackRate();
        const prep = `${NAME} ${variant} from ${s0.name}: rendering the stems at ${target} BPM; ` +
          `layers start at ${fmt(want)} (in ${Math.max(0, inS).toFixed(0)} s) if they are in by then, else on the next 8-bar line`;
        say(prep);
        log("prepares", prep);
        // the first song pair first, then the rest (the server renders and caches each key-locked set)
        const firstBufs = tempoSet(s1.id, target, gen, ["other"]);
        const bufs1 = (await Promise.all([keyLock(d0, target, gen), firstBufs]))[1];
        if (!run || run.gen !== gen) return;
        if (!bufs1) throw new Error(`${s1.name}'s stems at ${target} BPM did not arrive`);
        for (const s of plan.songs.slice(2)) host.api.fetch(`/api/tracks/${encodeURIComponent(s.id)}/stems?bpm=${target.toFixed(2)}&separate=1`).catch(() => {});
        const pos = d0._currentPosition(), rate = d0._playbackRate();
        const len = (s0.core_out[1] - s0.core_out[0]) * rate;       // the first lead-in, in the playing song's seconds
        let at = want;
        if ((at - pos) / rate < READY_S + 2) {
          at = nextAnchor(d0.analysis || analyses[currentId], pos + (READY_S + 2) * rate, len);
          if (at == null) throw new Error("no 8-bar line left in the playing song for the first lead-in");
          log("moves", `the stems were in at ${fmt(pos)}, past ${fmt(want)}: the first lead-in starts on the 8-bar line at ${fmt(at)}`);
        }
        const A = audioNow() + (at - pos) / rate;
        const sch = schedule(plan, A, deck0);
        run.sch = sch; run.plan = plan; run.cur = 0;
        const e1 = sch.songs[1];
        if (e1.enter && e1.enter.t0 - audioNow() < READY_S) throw new Error("the first lead-in is already due");
        run.state = "playing";
        const msg = `${NAME} ${variant}: layers start at ${fmt(at)} of ${s0.name} (in ${((A - audioNow())).toFixed(0)} s): ${s1.name} comes in under it`;
        say(msg);
        log("starts", msg);
        play(sch, Promise.resolve(bufs1), gen);
      } catch (e) { abort(`could not start: ${e.message}`); }
    }

    function play(sch, firstBufs, gen) {
      const songs = sch.songs, n = songs.length;
      const layerBufs = { 1: firstBufs };
      // the mind's phrase moves and the beat layer stay off the move's decks (restored at the end)
      const total = sch.end - audioNow();
      if (host.mod.djMind && host.mod.djMind.layering) host.mod.djMind.layering(total, { source: "RULE", why: `${NAME}: ${n} songs layered by stems` });
      if (host.mod.beatLayer && host.mod.beatLayer.isEnabled && host.mod.beatLayer.isEnabled()) { host.mod.beatLayer.setEnabled(false); run.beat = true; }
      // the countdown to the first layer on the status line, then NULL in front for the whole move
      const countdown = () => {
        const left = sch.front.on - audioNow();
        if (left < 1) return;
        say(`${NAME}: layers start in ${left.toFixed(0)} s (${songs[1].name} under ${songs[0].name})`);
        later(audioNow() + Math.min(5, left - 0.5), countdown);
      };
      countdown();
      later(sch.front.on, () => { front(true, sch.songs[0].target); say(`${NAME}: playing, ${n} songs layered by stems (press again to stop)`); });
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
      say(`${NAME}: ${handback ? "done, the set carries on" : `ended early (${why})`}`);
      const a = ap();
      if (handback && last) {
        try {
          const played = songs.map((s) => s.id).join(",");
          const pick = (await getJSON(`/api/supermoves/handback?a=${last.id}&level=${last.level == null ? "" : last.level}&played=${played}`)).pick;
          const mm = host.mod.macroMode;
          const step = pick && { n: 1, a: last.id, b: pick.b, a_name: last.name, b_name: pick.b_name, recipe: "Bass Swap" };
          if (pick) log("hands back", `Bass Swap into ${pick.b_name} (energy ${pick.level}, one under ${last.level})`);
          if (pick && mm && mm.armStep && a && a.rebook) {
            // armed and booked by the autopilot itself (its load path, its bookkeeping): no load behind its back
            mm.armStep(step, `${NAME} HANDBACK`);
            if (a.rebook(`${NAME} hands back: ${pick.b_name}`).ok) return;
            if (mm.disarm) mm.disarm();
          } else if (pick && mm && mm.playStep) await mm.playStep(step, `${NAME} HANDBACK`);
          else if (!pick) log("hands back", "no Bass Swap partner one level lower: the normal set picks");
        } catch (e) { log("hands back", `handback pick failed (${e.message}): the normal set picks`); }
      }
      if (a) a.resume();
    }
    function abort(why) {
      if (!run) return;
      say(`${NAME}: ${why}`);
      if (run.state === "waiting" || run.state === "checking" || run.state === "loading" && !run.sch) {
        const cur = run.currentId;
        cleanup(true);
        run = null;
        return cur ? release(cur, why) : undefined;
      }
      end(why, false);
    }

    // ---- the macro: its entries in the MACRO list (macro-mode.js), Shift+S, ai-action "supermove" -------------------
    // Press: starts the move now from the playing song when it is one of the variant's (layers on the next free line
    // once the stems are rendered), else books the variant's first song (its next one when the playing song is too late
    // in the variant) as the next transition and starts when that song lands. Again: stops it, and says so.
    function press(name) {
      if (run && (run.manual || run.state === "loading" || run.state === "playing")) {
        abort("stopped by hand");
        return say(`${NAME}: stopped by hand (press again to start it)`);
      }
      if (pendingManual) {
        const p = pendingManual;
        pendingManual = null;
        log("stopped", `by hand: ${p.name} stays booked, the move will not start on it`);
        return say(`${NAME}: stopped by hand, ${p.name} stays booked as a normal transition`);
      }
      if (run) { for (const id of run.timers) clearTimeout(id); run = null; }   // an automatic check / build wait: the press takes over
      const v = variants.find((x) => x.name === name) || variants[0];
      if (!v) return say(`no saved ${NAME}`);
      const st = host.mod.autopilotState;
      if (!st || !st.active) return say(`${NAME}: start the autopilot first (the move hands the set back to it)`);
      const deck = st.activeDeck, d = host.decks[deck];
      const tid = deck === "a" ? host.state.trackA : host.state.trackB;
      if (d && d.analysis && !(tid in analyses)) analyses[tid] = d.analysis;
      const r = decide({ variants: [v], curId: tid, pos: d ? d._currentPosition() : 0, rate: d ? d._playbackRate() : 1, fired, analyses, manual: v.name });
      if (r.fire || r.wait) {
        log("pressed", r.why);
        if (r.wait) return waitBuild(r, { currentId: tid, deck, setId: setKey }, v.name);
        return start(v.name, r.start, deck, tid, r.anchor, true);
      }
      const k = v.songs.findIndex((s) => s.id === tid), nx = v.songs[k >= 0 && k < v.songs.length - 2 ? k + 1 : 0];
      book(v, nx, st.trackId || tid, r.why);
    }
    // The booked song through the autopilot's own booking (macroMode.armStep + autopilot superMove.rebook): the
    // booking made so far is dropped and the next one takes this step (normal gates, measured gates waived as for a
    // step armed by the owner). Never loaded onto a deck behind the autopilot's back: that is how Syren played on
    // deck B under Felix's name on 2026-10-01 and the move never saw it land.
    function book(v, nx, from, why0) {
      const mm = host.mod.macroMode, a = ap();
      if (!mm || !mm.armStep || !a || !a.rebook) return say(`${NAME}: cannot book ${nx.name} (macro mode / autopilot not loaded)`);
      mm.armStep({ n: 1, a: from, b: nx.id, a_name: "", b_name: nx.name, recipe: "Bass Swap" }, NAME);
      pendingManual = { variant: v.name, b: nx.id, name: nx.name, from };    // before rebook: its takeOver reads it
      const rb = a.rebook(`${NAME}: ${nx.name} next`);
      if (!rb.ok) {
        pendingManual = null;
        if (mm.disarm) mm.disarm();
        log("pressed, not booked", `${why0}: ${nx.name} cannot be booked now (${rb.why})`);
        return say(`${NAME}: ${rb.why}, press again after it`);
      }
      const msg = `booking ${nx.name} next (Bass Swap, normal gates); the move starts when it lands, first layers on the next free 8-bar line`;
      log("pressed", `${why0 ? `${why0}: ` : ""}${msg}`);
      say(`${NAME}: ${msg}`);
    }
    if (root.aiActions && typeof root.aiActions.register === "function") root.aiActions.register("supermove", () => press());
    if (host.bus && host.bus.on) host.bus.on("keydown", (e) => {
      if (e && e.key === "S" && e.shiftKey && !(e.target && /input|select|textarea/i.test(e.target.tagName || ""))) press(host.mod.macroMode && host.mod.macroMode.superPick);
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
