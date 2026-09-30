// AI Music Brain - FX BUDGET: restraint as a technique (research/notes/artist-signature-techniques.md S21).
// John Summit: "it's kind of a pet peeve of mine when there's too many effects added to songs because I think
// you should let the songs just talk" (DJ LIFE 2023, SOURCED). So every wet / loop / vocal move the console adds
// on top of the music asks this budget first, and a refused move does nothing (it is logged, never queued).
//
// API (batches A, B, C call this; do not re-implement it in their files):
//
//   const fb = host.mod.fxBudget;                 // runtime, created through the Host port (engine.js)
//   fb.canSpend(kind, cost, ctx) -> {ok, why, level, song_used, window_used, fallbacks}   (asks, spends nothing)
//   fb.spend(kind, cost, ctx)    -> same shape; records the move only when ok, logs the refusal otherwise
//   fb.report()                  -> {spent: {kind: n}, refused: {kind: n}, events}
//
//   kind   "wet"   echo / reverb / delay throw, filter sweep, riser, pre-crossfade fill, roll
//          "loop"  loop roll, slip loop, beat jump, loop tail
//          "vocal" vocal loop / chop / echo, acapella drop
//   cost   budget units, 1 by default; a move that stacks two effects costs 2
//   ctx    {now: s on the audio clock (the runtime fills it), phraseS: s per 8-bar phrase from the beat grid,
//           song: id of the song the move plays on, transition: id of the transition it rides on (null in-song)}
//
// Rules (all in `core`, pure, node-tested in app/tests/js/fx_budget_check.js):
//   1. one FX per transition: a second move on the same transition id is refused (S21 "forbid stacking")
//   2. per song: at most MAX_PER_SONG units on one song
//   3. per K phrases, decaying: spent units decay with a half-life of HALF_LIFE_PHRASES phrases of the playing
//      song (measured phraseS); a move is refused while level + cost > MAX_LEVEL. Steady state: MAX_LEVEL moves,
//      then one move per half-life, i.e. about 2 per 4 phrases.
//   4. per 30 minutes: at most MAX_PER_30MIN units (the spec's "N per 30 minutes", sim fx_density_per_30min)
// Constants are GUESSES (no artist gives a number) and are listed in `fallbacks` of every answer, plus the
// phrase length when the caller did not measure one. The switch is ap-fxbudget-toggle (on by default: S21 is
// SOURCED); off, every move is allowed and still counted.
(function (root) {
  "use strict";

  const KINDS = ["wet", "loop", "vocal"];
  const MAX_LEVEL = 2;               // GUESS: units in flight over the decaying window
  const HALF_LIFE_PHRASES = 2;       // GUESS: 2 per 4 phrases sustained
  const MAX_PER_SONG = 3;            // GUESS: stem remix and the auto sampler already cap themselves near 3-4
  const MAX_PER_30MIN = 10;          // GUESS: under one move per song at 3-4 min songs
  const WINDOW_30MIN_S = 1800;
  const FALLBACK_PHRASE_S = 15;      // 8 bars at 128 BPM, used only when ctx.phraseS is not measured
  const CONSTANTS = [`MAX_LEVEL=${MAX_LEVEL}`, `HALF_LIFE_PHRASES=${HALF_LIFE_PHRASES}`,
    `MAX_PER_SONG=${MAX_PER_SONG}`, `MAX_PER_30MIN=${MAX_PER_30MIN}`];

  function newState() { return { events: [], spent: {}, refused: {} }; }

  // decayed units still in flight at `now` (half-life in seconds)
  function level(events, now, halfLifeS) {
    let s = 0;
    for (const e of events) {
      const age = now - e.t;
      if (age >= 0) s += e.cost * Math.pow(0.5, age / halfLifeS);
    }
    return s;
  }

  // -> {ok, why, level, song_used, window_used, fallbacks}. Pure: reads state, never writes it.
  function decide(state, kind, cost, ctx) {
    ctx = ctx || {};
    const fallbacks = CONSTANTS.slice();
    const out = (ok, why, extra) => Object.assign({ ok, why, level: 0, song_used: 0, window_used: 0, fallbacks }, extra || {});
    if (!KINDS.includes(kind)) return out(false, `unknown fx kind "${kind}" (wet / loop / vocal)`);
    const c = cost == null ? 1 : Number(cost);
    if (!Number.isFinite(c) || c <= 0) return out(false, `bad cost ${cost}`);
    const now = Number(ctx.now);
    if (!Number.isFinite(now)) return out(false, "no clock (ctx.now)");
    let phraseS = Number(ctx.phraseS);
    if (!(phraseS > 0)) { phraseS = FALLBACK_PHRASE_S; fallbacks.push(`phraseS=${FALLBACK_PHRASE_S}`); }
    const ev = state.events;
    const lvl = level(ev, now, HALF_LIFE_PHRASES * phraseS);
    const songUsed = ctx.song == null ? 0 : ev.filter((e) => e.song === ctx.song).reduce((a, e) => a + e.cost, 0);
    const winUsed = ev.filter((e) => now - e.t >= 0 && now - e.t < WINDOW_30MIN_S).reduce((a, e) => a + e.cost, 0);
    const m = { level: +lvl.toFixed(3), song_used: songUsed, window_used: winUsed };
    if (ctx.transition != null) {
      const on = ev.find((e) => e.transition === ctx.transition);
      if (on) return out(false, `one FX per transition (${on.kind} already rides transition ${ctx.transition})`, m);
    }
    if (songUsed + c > MAX_PER_SONG) return out(false, `song budget spent (${songUsed}/${MAX_PER_SONG})`, m);
    if (winUsed + c > MAX_PER_30MIN) return out(false, `30-minute budget spent (${winUsed}/${MAX_PER_30MIN})`, m);
    if (lvl + c > MAX_LEVEL + 1e-9) return out(false, `too many FX lately (level ${lvl.toFixed(2)} + ${c} > ${MAX_LEVEL})`, m);
    return out(true, `fx ${kind} ok (level ${lvl.toFixed(2)}, song ${songUsed}/${MAX_PER_SONG})`, m);
  }

  // Records the answer in the state (events on ok, a refusal count otherwise). Returns the answer.
  function record(state, kind, cost, ctx, answer) {
    if (answer.ok) {
      state.events.push({ t: Number(ctx.now), kind, cost: cost == null ? 1 : Number(cost),
        song: ctx.song == null ? null : ctx.song, transition: ctx.transition == null ? null : ctx.transition });
      state.spent[kind] = (state.spent[kind] || 0) + 1;
    } else {
      state.refused[kind] = (state.refused[kind] || 0) + 1;
    }
    return answer;
  }

  const core = { KINDS, MAX_LEVEL, HALF_LIFE_PHRASES, MAX_PER_SONG, MAX_PER_30MIN, WINDOW_30MIN_S, FALLBACK_PHRASE_S,
    newState, level, decide, record };

  function create({ host }) {
    const state = newState();
    const on = () => host.ui.flag("ap-fxbudget-toggle", true);
    const fill = (ctx) => Object.assign({ now: host.clock.perfNow() / 1000 }, ctx || {});
    function canSpend(kind, cost, ctx) {
      const c = fill(ctx);
      const a = decide(state, kind, cost, c);
      return on() || !KINDS.includes(kind) ? a : Object.assign({}, a, { ok: true, why: `budget off (${a.why})` });
    }
    function spend(kind, cost, ctx) {
      const c = fill(ctx);
      const a = record(state, kind, cost, c, canSpend(kind, cost, c));
      // one line per answer: app/sim/runlog.py counts these (fx_budget_spent / fx_budget_refused)
      console.info(a.ok ? `fx budget: spent ${kind}: ${a.why}` : `fx budget: refused ${kind}: ${a.why}`);
      return a;
    }
    function report() { return { spent: Object.assign({}, state.spent), refused: Object.assign({}, state.refused), events: state.events.slice() }; }
    const api = { core, canSpend, spend, report };
    host.mod.fxBudget = api;
    return api;
  }

  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (root.Engine) root.Engine.mount("fxBudget", create);
})(typeof window !== "undefined" ? window : globalThis);
