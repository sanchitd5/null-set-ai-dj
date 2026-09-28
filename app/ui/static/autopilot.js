// AI DJ Autopilot
// Seed track → LLM suggests next → download → analyze → auto-crossfade → repeat.
//
// Deck alternation: even transitions crossfade A→B (xfader -1→+1),
// odd transitions crossfade B→A (xfader +1→-1). Tracks alternate slots.
//
// Requires: window.decks, window.loadIntoDeck, setStatus (app.js + deck-controller.js).

// Pure transition maths, no DOM / audio (node-checked: app/tests/autopilot_check.js).
var autopilotCore = (function () {
  const MASHUP_KINDS = new Set(["blend", "filter", "loop"]);
  // Stem blend length in real bars. barS = seconds per bar at A's live tempo,
  // roomBars = A's remaining bars, scale = 1 or 0.5 (short window).
  // Mashup kinds: >= 30 s together, rounded UP to 16 so stemBlendPlan's
  // kick/bass swap (bar L/2) sits on an 8-bar phrase line; 32 when A has the
  // room; capped by A's room (16-bar multiple, 8 when 16 doesn't fit).
  // Bass / double-drop stay short and percussive (16 / 8).
  // scale is applied once and wins over the 30 s floor: the short window
  // exists to end the overlap before B's voice lands.
  function stemBlendBars(kind, barS, roomBars, scale) {
    let unscaled;
    if (MASHUP_KINDS.has(kind)) {
      const want = Math.max(roomBars >= 34 ? 32 : 16, Math.ceil(30 / barS / 16) * 16);
      const room = roomBars - 1 >= 16 ? Math.floor((roomBars - 1) / 16) * 16 : 8;
      unscaled = Math.min(want, room);
    } else {
      unscaled = kind === "bass" ? 16 : 8;
    }
    return Math.max(4, unscaled * scale);
  }
  // Crossfader moves for a stem blend of `bars`, as [{bar, from, to, bars}]
  // (fader -1..1 from A's side, `dir` = +1 when B is on the right).
  // With both decks in stem mode the STEMS introduce B, not the fader (user:
  // "at least one appropriate stem should be playing before crossfade
  // starts"): the fader parks at the equal-power centre over 1 bar while B's
  // ONE intro stem rises under A (stemBlendPlan), sits there for the intro
  // phrase (8 bars, 4 when short: stemMoves.core.introBars), and only then
  // crosses: over the last 8 bars, never before the swap line or the end of
  // the intro phrase. The old "centre sounds odd" was full mix + full mix;
  // here B contributes one layer. Double drop: both drops together, park in
  // 1 bar, B's side over the last 2 bars as A's tops leave.
  const FADER_PARK_BARS = 1;
  function introBars(bars) { return bars >= 16 ? 8 : Math.max(2, bars / 2); }
  function stemBlendFader(kind, bars, dir) {
    const from = -dir, to = dir;
    const park = { bar: 0, from, to: 0, bars: Math.min(FADER_PARK_BARS, bars / 4) };
    if (kind === "double") return [park, { bar: bars - 2, from: 0, to, bars: 2 }];
    const fadeAt = Math.max(bars / 2, bars - 8, introBars(bars));
    return [park, { bar: fadeAt, from: 0, to, bars: bars - fadeAt }];
  }
  // Song seconds from `pos` to the next 8-bar phrase line of a grid anchored
  // at `entry` (0 when on the line).
  function phraseWaitS(pos, entry, phraseS) {
    const since = pos - entry;
    if (since < 0) return -since;
    const r = since % phraseS;
    return r < 1e-6 || phraseS - r < 1e-6 ? 0 : phraseS - r;
  }
  // ---- tempo home (user: "also the bpm should come to normal") -------------
  // After a transition the now-playing song ends at its OWN tempo, whatever
  // the lock was. The key-locked tempo stems (native key, locked tempo) are
  // the problem: the pitched mix is the only other way home and swapping to
  // it shifts the key by the lock ratio at that instant. Per gap (%, signed,
  // off native):
  //   none    |gap| < 0.05 %: already home
  //   glide   no tempo stems (pitched mix): one smooth rate glide, key follows
  //   drop    tempo stems, |gap| <= HOME_DROP_PCT (~1/2 semitone): swap to the
  //           pitched mix on a phrase line (inaudible key step), then glide
  //   ladder  tempo stems, bigger gap, renders fit before the song's end:
  //           re-render stems at steps <= LADDER_STEP_PCT toward native and
  //           swap one per phrase line (each a <= 1/2-semitone-free tempo
  //           step, key never moves), last step lands on the native stems
  //   masked  ladder won't fit: drop to the pitched mix where the key step
  //           hides (B's breakdown / no vocal), then a long glide (32-64 bars)
  // bridge: a deliberate tempo ladder (advanceBridge) moves the "native"
  // target to the tempo the bridge expects (bridgeHomePct).
  const HOME_DROP_PCT = 3, LADDER_STEP_PCT = 3, RENDER_S = 35;
  function homePlan(o) {
    // o: {gapPct, tempoStems, songLeftS, phraseS}
    const g = o.gapPct;
    if (Math.abs(g) < 0.05) return { path: "none", steps: [], why: "at native tempo" };
    if (!o.tempoStems) return { path: "glide", steps: [0], why: "pitched mix: one smooth glide home" };
    if (Math.abs(g) <= HOME_DROP_PCT) return { path: "drop", steps: [0], why: `${g.toFixed(1)}% <= ${HOME_DROP_PCT}%: key step under half a semitone` };
    const n = Math.ceil(Math.abs(g) / LADDER_STEP_PCT);
    const steps = [];
    for (let i = 1; i <= n; i++) steps.push(i === n ? 0 : g * (1 - i / n));   // last is 0 = native
    // each step: a render (~RENDER_S, cached renders are instant) + the phrase it plays
    const need = n * Math.max(RENDER_S, o.phraseS || 16) + (o.phraseS || 16);
    if (o.songLeftS > need) return { path: "ladder", steps, why: `${g.toFixed(1)}% in ${n} key-locked steps of <= ${LADDER_STEP_PCT}%` };
    return { path: "masked", steps: [0], why: `${g.toFixed(1)}%: no time for ${n} renders, drop where the key step hides, then a long glide` };
  }
  // Bars for the masked glide: longer for bigger gaps, 32..64.
  function maskedGlideBars(gapPct) { return Math.max(32, Math.min(64, Math.round(Math.abs(gapPct) * 4 / 8) * 8)); }
  // Song time of the first phrase line >= fromPos where the key step hides:
  // inside a breakdown / intro section, else outside every vocal region; null
  // when none before `limit`. sections: [{label, start, end}], vox: [[s, e]].
  function maskedDropAt(fromPos, entry, phraseS, limit, sections, vox) {
    const quiet = (t) => (sections || []).some((s) => ["breakdown", "intro"].includes(s.label) && t >= s.start && t < s.end);
    const sings = (t) => (vox || []).some((r) => t >= r[0] - 0.5 && t < r[1] + 0.5);
    let line = fromPos + phraseWaitS(fromPos, entry, phraseS), firstClear = null;
    for (; line < limit; line += phraseS) {
      if (quiet(line)) return { at: line, why: "B's breakdown" };
      if (firstClear == null && !sings(line)) firstClear = line;
    }
    return firstClear != null ? { at: firstClear, why: "B's vocal is out" } : null;
  }
  // Recipe forced by B's vocal entering `vIn` bars into a beat blend (two
  // voices must never sing together), or null (no constraint). Stems on
  // either deck solve it on the stems: B enters with its voice held (B's
  // stems) or A's voice leaves on the line (A's stems), so the blend keeps a
  // full 8-bar bass swap. Neither deck has stems: a shorter EQ bass swap that
  // ends before B's vocal (never a cut).
  function vocalRecipe(o) {
    const v = o.vIn;
    if (o.oneSong || v == null || !(v < 16)) return null;
    if (o.bStems) return { recipe: "Bass Swap", short: false, why: `B sings in ${Math.round(v)} bars: its voice held on its stems until A's is out` };
    if (o.aStems) return { recipe: "Bass Swap", short: false, why: `B sings in ${Math.round(v)} bars: A's voice leaves on its stems` };
    // never a hard cut (user): no stems and B sings very soon -> the shortest EQ swap
    if (v < 4) return { recipe: "Bass Swap", short: true, why: `no stems, B sings in ${Math.round(v)} bars: 4-bar swap, A's voice out before B's` };
    if (v < 8) return { recipe: "Bass Swap", short: true, why: `4-bar swap: B sings in ${Math.round(v)} bars` };
    return { recipe: "Bass Swap", short: false, why: `8-bar swap: B sings in ${Math.round(v)} bars` };
  }
  // A move learned from studied sets (/api/learned/pick) replaces the recipe only
  // when the console already allows that recipe for this pair (o: the same facts
  // scheduleTransition decides on), and never a move that outranks it (LAYER,
  // PEAK, riff over rap, mashup: user rule "mashup beats every other move").
  // -> {recipe, why} | null
  function learnedRecipe(pick, o) {
    if (!pick || !pick.recipe || o.layer || o.peak || o.riff || o.recipe === "Mashup → Transition" || o.recipe === "Stem Merge") return null;
    const allowed = {
      // any beat-to-beat pair can swap the bass on a line
      "Bass Swap": !!(o.blend || o.oneSong),
      // the long stem intro keeps both records up: only when no vocal rule shortened it
      "Long Blend": !!(o.oneSong && !o.vocalRule && (!o.blend || o.blend.clean)),
      "Mashup → Transition": !!(o.stemsBoth && o.mashupFits),
    };
    if (!allowed[pick.recipe] || pick.recipe === o.recipe) return null;
    return { recipe: pick.recipe, why: `learned ${pick.kind.replace("_", " ")} (seen ${pick.seen}x, ${pick.source})` };
  }
  // A's high-energy sections [[t0, t1]] (song s), same rule as preplan.high_spans:
  // energy >= its 85th percentile AND >= median + 0.3 x range, joined across gaps
  // under 2 bars, padded HIGH_LEAD_BARS before (at the high OR about to reach it:
  // the build into it) and 1 bar after. Quantiles are numpy's exactly (percentile
  // "linear", median = mean of the middle two): golden vectors in
  // app/tests/fixtures/rule_vectors.json check both sides.
  const HIGH_LEAD_BARS = 16;
  // np.percentile(x, 100 * p) on an ascending array, same float operations
  function quantileLinear(sorted, p) {
    const n = sorted.length, vi = (n - 1) * p;
    if (vi >= n - 1) return sorted[n - 1];
    const lo = Math.floor(vi), g = vi - lo, a = sorted[lo], d = sorted[lo + 1] - a;
    return g >= 0.5 ? sorted[lo + 1] - d * (1 - g) : a + d * g;
  }
  // np.median on an ascending array
  function median(sorted) {
    const n = sorted.length, h = n >> 1;
    return n % 2 ? sorted[h] : (sorted[h - 1] + sorted[h]) / 2;
  }
  function highSpans(times, curve, bar) {
    if (!times || !curve || times.length < 4 || times.length !== curve.length) return [];
    const sorted = [...curve].sort((x, y) => x - y);
    const q = (p) => quantileLinear(sorted, p);
    const med = median(sorted), range = sorted[sorted.length - 1] - sorted[0];
    if (!(range > 1e-6)) return [];                       // flat: no high point to protect
    const thr = Math.max(q(0.85), med + 0.3 * range);
    const spans = [];
    for (let i = 0; i < times.length; i++) {
      if (curve[i] < thr || curve[i] <= med) continue;    // must stand above the song's typical level
      const last = spans[spans.length - 1];
      if (last && times[i] - last[1] <= 2 * bar) last[1] = times[i];
      else spans.push([times[i], times[i]]);
    }
    return spans.map(([x, y]) => [Math.max(0, x - HIGH_LEAD_BARS * bar), y + bar]);
  }
  // Exit time moved by whole phrases until [exit, exit + span) is clear of A's highs
  // (user: never transition as A reaches its energy high). Gives up past `limit`.
  function exitPastHigh(exit, spanS, spans, phraseS, limit) {
    let t = exit, moved = 0;
    const hits = (x) => spans.some(([a, b]) => x < b && x + spanS > a);
    while (hits(t) && t + phraseS <= limit && moved < 12) { t += phraseS; moved++; }
    return hits(t) ? { t: exit, clear: false, moved: 0 } : { t, clear: true, moved };
  }
  // The live rule; app/music_brain/energy.next_ok mirrors it (same golden vectors,
  // app/tests/fixtures/rule_vectors.json): at most 2 levels a song (1 relaxed,
  // +1 on the last-round fallback); early in the set (< 30 %) it may not fall more
  // than 1, near the end (> 85 %) not rise more than 1. -> {ok, step, why}
  const ENERGY_MIN_RAW = 0.1;      // raw 0-1: below this the two songs measure the same, whatever the levels say
  const WARMUP_SONGS = 5;          // the set builds over its first songs; open-ended after (no known end)
  // o: {relaxed, force, songs (played so far), rawDelta (raw_b - raw_a)}
  function energyStepOk(cur, nxt, o = {}) {
    const step = nxt - cur, lim = (o.relaxed ? 1 : 2) + (o.force ? 1 : 0);
    if (o.rawDelta != null && Math.abs(o.rawDelta) < ENERGY_MIN_RAW) {
      return { ok: true, step, why: `energy ${cur} -> ${nxt} (measured almost the same)` };
    }
    const arc = o.songs != null ? (o.songs < WARMUP_SONGS ? "build" : "")
      : o.setPos != null && o.setPos < 0.3 ? "build" : o.setPos != null && o.setPos > 0.85 ? "cool" : "";
    if (Math.abs(step) > lim) return { ok: false, step, why: `energy ${step > 0 ? "jump" : "drop"} ${cur} -> ${nxt} (max ${lim} a song)` };
    if (!o.force && arc === "build" && step < -1) return { ok: false, step, why: `energy falls ${cur} -> ${nxt} while the set is building` };
    if (!o.force && arc === "cool" && step > 1) return { ok: false, step, why: `energy rises ${cur} -> ${nxt} while the set is cooling down` };
    return { ok: true, step, why: `energy ${cur} -> ${nxt}` };
  }

  // A background job's final answer (app/ui/bg_jobs.py: the silent ear's preplan /
  // merge audition). `first` is the POST's body; while it reads {status: "pending",
  // job}, poll(job) is asked every `everyMs` until the result lands. Past `budgetMs`,
  // when alive() turns false, or on a failed poll: null, and the caller carries on
  // exactly as it does without the ear. A body without "pending" IS the result.
  async function awaitJob(first, poll, o = {}) {
    const every = o.everyMs || 1500, budget = o.budgetMs == null ? 35000 : o.budgetMs;
    const clock = o.now || Date.now, sleep = o.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
    const alive = o.alive || (() => true);
    const t0 = clock();
    let d = first;
    while (d && d.status === "pending" && d.job) {
      const left = budget - (clock() - t0);
      if (left <= 0 || !alive()) return null;
      await sleep(Math.min(every, left));
      if (!alive()) return null;
      try { d = await poll(d.job); } catch (e) { return null; }
    }
    return d || null;
  }
  const api = { awaitJob, energyStepOk, highSpans, quantileLinear, median, exitPastHigh, learnedRecipe, vocalRecipe, stemBlendBars, stemBlendFader, phraseWaitS, introBars, FADER_PARK_BARS, homePlan, maskedGlideBars, maskedDropAt, HOME_DROP_PCT, LADDER_STEP_PCT };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  return api;
})();

(function () {
  if (typeof window === "undefined") return;   // node: only the pure core above
  // Every request the autopilot makes has a deadline. Without one, a request
  // lost in a server restart never settled and the set sat in HOLD LOOP
  // forever ("Matching transition..." stuck). Budgets match the work behind
  // each endpoint (LLM queue, Demucs stems, 30-50 MB FLAC audio).
  const _fetch = window.fetch.bind(window);
  function deadlineFor(url) {
    const u = String(url);
    if (u.includes("/api/download")) return 300000;
    if (u.includes("/api/blend/plan") || u.includes("/api/mashup/plan")) return 180000;
    if (u.includes("/api/layer/plan")) return 60000;   // vocal maps already cached by the blend plan
    if (u.includes("/api/bridge/plan")) return 10000;
    // background jobs (app/ui/bg_jobs.py): the POST starts one, the GET polls it; both answer at once
    if (u.includes("/api/transition/preplan") || u.includes("/api/merge/audition")) return 20000;
    if (u.includes("/api/autopilot/suggest")) return 240000; // ~50 s per call, may queue behind a plan
    if (u.includes("/api/audio/")) return 120000;
    if (u.includes("/api/match") || u.includes("/analysis")) return 90000;
    if (u.includes("/api/tracks")) return 20000;
    return 60000;
  }
  function fetch(url, opts = {}) {
    const ctl = new AbortController();
    const ms = deadlineFor(url);
    const timer = setTimeout(() => ctl.abort(), ms);
    // the caller's own signal (a flushed merge audition) still aborts the request
    if (opts.signal) {
      if (opts.signal.aborted) ctl.abort();
      else opts.signal.addEventListener("abort", () => ctl.abort(), { once: true });
    }
    return _fetch(url, Object.assign({}, opts, { signal: ctl.signal }))
      .catch((e) => { throw e.name === "AbortError" ? new Error(`timed out after ${ms / 1000}s: ${url}`) : e; })
      .finally(() => clearTimeout(timer));
  }

  // ── state ─────────────────────────────────────────────────────────────────
  let active = false;
  let activeDeck = "a";       // which deck is currently playing
  let currentTrackId = null;
  let occasion = "";
  let history = [];           // display names of played tracks (last 5 kept)
  let mashupTag = "";         // status suffix while a vocal layer is booked
  let entryPos = 0;           // track time where the current song came in
  let currentEnergy = null;   // LLM's 1-10 energy read of the current song
  let playedIds = [];         // track ids played this set (LAYER callbacks: an earlier vocal)
  let setStartedAt = 0;       // Date.now() when the set started (elapsed_seconds for suggest)
  // One id per set in this browser tab: the server scopes its suggestion memory
  // to it, so another tab's set (or this tab's previous set) never counts as
  // "this set", and two tabs never share a cached suggestion.
  const newSetId = () => (window.crypto && crypto.randomUUID ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`);
  let setId = newSetId();
  let beatMutedByLayer = false; // a LAYER paused the live beat layer (restore after / on stop)
  function unmuteBeatLayer() {
    if (beatMutedByLayer && window.beatLayer) window.beatLayer.setEnabled(true);
    beatMutedByLayer = false;
  }

  // ── UI refs ───────────────────────────────────────────────────────────────
  const seedInput      = document.getElementById("ap-seed-input");
  const occasionInput  = document.getElementById("ap-occasion-input");
  const startBtn       = document.getElementById("ap-start-btn");
  const stopBtn        = document.getElementById("ap-stop-btn");
  const statusEl       = document.getElementById("ap-status");
  const queueEl        = document.getElementById("ap-queue");
  const xfader         = document.getElementById("crossfader");

  if (!startBtn) return; // panel not present

  // ── helpers ───────────────────────────────────────────────────────────────
  function apStatus(msg) {
    if (statusEl) statusEl.textContent = msg;
    if (typeof setStatus === "function") setStatus(msg);
  }

  function stagingDeck() { return activeDeck === "a" ? "b" : "a"; }

  function deckPosition(id) {
    const d = window.decks && window.decks[id];
    return d ? d._currentPosition() : 0;
  }
  // A chosen move whose level dip IS the move (stemMoves.core.DIP_ALLOWED):
  // said out loud, never a silently skipped floor check.
  function dipAllowed(kind, what) {
    const D = window.stemMoves && window.stemMoves.core.DIP_ALLOWED;
    console.info(`loudness floor: dip allowed for ${what} - ${(D && D[kind]) || kind}`);
  }
  // "" when the deck's stems are live, else why not (for the recipe log).
  function stemsWhy(d) {
    if (!d) return "no deck";
    if (d.stemsReady) return "";
    if (!d.stems) return "not separated / not loaded";
    if (!d.playing) return "deck not playing";
    if (d._extPos) return "another engine owns the deck";
    return "stems not sounding (ran out or re-arm failed)";
  }

  // ── transition engine ─────────────────────────────────────────────────────
  //
  // TECHNICAL SPEC: "Fred again.." style transitions (grounded in ./DJ/ notes)
  //
  // Sources: [[Fred again.. Case Study]], [[Bass Swap]], [[Double Drop]],
  // [[Echo Out]], [[Stems Transition]], [[EQ & Frequency Management]],
  // [[Phrasing & Structure]].
  //
  // 1. PHRASE GRID. Dance music moves in 8-bar (32-beat) phrases. Every move
  //    below is expressed in BARS and lands on the grid: the recipe matcher
  //    already snaps `a_time` / `b_time` to a real 8-bar boundary, so t0 of the
  //    transition IS a phrase boundary. 1 bar = 4 beats = 240000 / bpm ms.
  //    Standard blend = 16 bars, drop-based recipes = 8 bars. No hard cuts:
  //    a cut recipe runs as a Bass Swap (recipeKind).
  //
  // 2. FREQUENCY OWNERSHIP. Two kick drums / two sub-basses never play at once
  //    (sub-bass < 120 Hz stacks into mud and phase cancellation). The low EQ
  //    knob drives a real Web Audio BiquadFilter lowshelf (deck.lowFilter,
  //    range -26 dB = kill .. +6 dB). Rule: outgoing low is KILLED before the
  //    incoming low opens. Highs and mids blend freely; the crossfader only
  //    moves once the bass has a single owner.
  //
  // 3. EQ ORDER (default): bars 0-4 kill outgoing LOW; bars 4-8 crossfader to
  //    centre; bar 8 (phrase boundary) open incoming LOW; bars 8-16 crossfader
  //    to the incoming side while outgoing HIGH shelf sweeps down (the
  //    "high-pass the old track away" feel, done with the high shelf because
  //    that is the filter the console exposes).
  //
  // 4. BASS SWAP: the Fred again.. staple. Both tracks phrase-aligned, cut A
  //    low over 4 bars, then at the drop snap B low open within one beat so
  //    the new sub arrives as a single event on the downbeat.
  //
  // 5. DOUBLE DROP: both drops land on the same downbeat and play together
  //    for 8 bars at full level (crossfader parked centre). Used only when the
  //    matcher scored the pair as harmonically safe (same/adjacent Camelot,
  //    < 3% BPM delta). Even here bass has one owner: A low is killed at the
  //    drop, B carries the sub, A contributes melody/tops. After 8 bars A is
  //    cut hard.
  //
  // 6. ECHO OUT: arm the ECHO insert on the outgoing deck at the last phrase,
  //    kill its low, then let the delay tail carry the space while B enters
  //    clean. Used for key clashes / big BPM gaps because the tail masks the
  //    harmonic mismatch.
  //
  // 7. LOOP ROLL: lock a 2-bar loop on the outgoing deck at the phrase
  //    boundary (loopBeats = 8, loop button) so the exit point holds steady
  //    for the bass hand-off; release once the incoming drop owns the room.
  //
  // 8. WEB AUDIO / DOM. deck-controller.js wires every knob to the graph:
  //    `.eq-knob[data-deck][data-band]` -> deck.setEQ(band, dB)
  //    `#crossfader` -> equal-power cos curve on deck.crossfaderGain
  //    `.fx-type-btn[data-deck][data-type]` click -> fxUnits[deck].setType()
  //    `.deck-btn[data-action="loop-toggle"]` click -> deck.toggleLoop()
  //    Driving the DOM controls (value + `input` event / click) keeps the UI
  //    lamps in sync and reuses the deck's own BiquadFilter nodes, so this
  //    module needs no direct AudioContext access.
  //
  const LOW_KILL = -26;          // slider minimum, treated as -inf
  const HIGH_SWEEP = -18;        // outgoing high shelf at the end of a blend
  const runTimers = [];          // setTimeout / setInterval ids for this run

  function later(ms, fn) {
    const id = setTimeout(fn, Math.max(0, ms));
    runTimers.push(id);
    return id;
  }

  function clearRun() {
    runTimers.forEach((id) => { clearTimeout(id); clearInterval(id); });
    runTimers.length = 0;
    endAudioClock();
  }

  // ── audio-clock automation ────────────────────────────────────────────────
  // Transitions schedule every EQ / crossfader move on the AudioContext clock,
  // at exact bar offsets from the downbeat where B starts (xT0). JS timers only
  // animate the knobs. Timer jitter used to move the bass swap off the beat
  // (crossfade analysis, fix 6).
  const XF_LOOKAHEAD_MS = 150;   // timers fire this early; audio lands exactly
  const XF_CENTER_BOOST_DB = 1.5; // AI crossfader curve: fills the mid-blend dip (analysis fix 5)
  let xT0 = null;                // audio time of the transition downbeat (null = timer mode)
  let xOffsetMs = 0;             // bar offset of the automation step being scheduled

  function endAudioClock() {
    xT0 = null;
    document.querySelectorAll("[data-ai-audio]").forEach((el) => { delete el.dataset.aiAudio; });
  }

  // Equal-power gains for crossfader value v (-1..1), +XF_CENTER_BOOST_DB at the centre.
  function xfGains(v) {
    const x = (v + 1) / 2;
    const boost = Math.pow(10, (XF_CENTER_BOOST_DB * Math.sin(Math.PI * x)) / 20);
    return [Math.cos(x * 0.5 * Math.PI) * boost, Math.cos((1 - x) * 0.5 * Math.PI) * boost];
  }

  function audioTargetOf(el) {
    if (!el || !window.decks) return null;
    if (el === xfader) return { kind: "xf" };
    if (el.classList && el.classList.contains("eq-knob")) {
      const d = window.decks[el.dataset.deck];
      const f = d && ({ low: d.lowFilter, mid: d.midFilter, high: d.highFilter })[el.dataset.band];
      return f ? { kind: "eq", param: f.gain } : null;
    }
    return null;
  }

  // Schedule from -> to over ms on the audio clock at xT0 + xOffsetMs.
  function scheduleAudio(target, from, to, ms) {
    const when = Math.max(audioCtx.currentTime, xT0 + xOffsetMs / 1000);
    const dur = Math.max(0, ms) / 1000;
    // hold whatever is sounding at `when`, drop later events, then ramp: never
    // throws on back-to-back moves (setValueCurveAtTime does if curves touch)
    const hold = (p) => (p.cancelAndHoldAtTime ? p.cancelAndHoldAtTime(when) : p.cancelScheduledValues(when));
    if (target.kind === "eq") {
      const p = target.param;
      hold(p);
      p.setValueAtTime(from, when);
      if (dur > 0) p.linearRampToValueAtTime(to, when + dur);
      return when;
    }
    // equal-power (+centre boost) curve as short linear segments
    const n = dur > 0 ? Math.max(2, Math.ceil(dur * 30)) : 1;
    for (const [idx, d] of [[0, "a"], [1, "b"]]) {
      const p = window.decks[d].crossfaderGain.gain;
      hold(p);
      p.setValueAtTime(xfGains(n === 1 ? to : from)[idx], when);
      for (let i = 1; i < n; i++) {
        p.linearRampToValueAtTime(xfGains(from + (to - from) * (i / (n - 1)))[idx], when + dur * (i / (n - 1)));
      }
    }
    return when;
  }

  function eqEl(deck, band) {
    return document.querySelector(`.eq-knob[data-deck="${deck}"][data-band="${band}"]`);
  }
  function fxBtn(deck, type) {
    return document.querySelector(`.fx-type-btn[data-deck="${deck}"][data-type="${type}"]`);
  }
  function loopBtn(deck) {
    return document.querySelector(`.deck-btn[data-deck="${deck}"][data-action="loop-toggle"]`);
  }

  function setRange(el, v) {
    if (!el) return;
    el.value = String(v);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  // Linear ramp of a range input over durationMs. fromVal null = current value.
  // Inside a transition (xT0 set) the audio is scheduled on the audio clock and
  // this only animates the control; otherwise it drives the control directly.
  function rampParam(getEl, fromVal, toVal, durationMs) {
    const steps = 20;
    const interval = Math.max(16, durationMs / steps);
    const first = getEl();
    if (!first) return;
    const from = fromVal == null ? parseFloat(first.value) : fromVal;
    const target = xT0 != null ? audioTargetOf(first) : null;
    if (target) {
      first.dataset.aiAudio = "1";
      const when = scheduleAudio(target, from, toVal, durationMs);
      const waitMs = Math.max(0, (when - audioCtx.currentTime) * 1000);
      if (durationMs <= 0) { later(waitMs, () => setRange(first, toVal)); return; }
      later(waitMs, () => {
        let k = 0;
        const t = setInterval(() => {
          if (k > steps) { clearInterval(t); return; }
          setRange(first, from + (toVal - from) * (k / steps));
          k++;
        }, interval);
        runTimers.push(t);
      });
      return;
    }
    let step = 0;
    const t = setInterval(() => {
      const el = getEl();
      if (!el || step > steps) { clearInterval(t); return; }
      const v = from + (toVal - from) * (step / steps);
      setRange(el, v);
      step++;
    }, interval);
    runTimers.push(t);
  }

  function barMs(deck) {
    const d = window.decks && window.decks[deck];
    const bpm = d && d.bpm > 0 ? d.bpm : 128;
    return 240000 / bpm;
  }

  function setLoopLength(deck, beats) {
    const d = window.decks && window.decks[deck];
    if (d && typeof d.setLoopBeats === "function") d.setLoopBeats(beats);
    const v = document.getElementById(`loop-value-${deck}`);
    if (v) v.textContent = String(beats);
  }

  function setLoop(deck, on) {
    const d = window.decks && window.decks[deck];
    const btn = loopBtn(deck);
    if (!d || !btn) return;
    if (!!d.loopOn !== on) btn.click();
  }

  function setFx(deck, type, wet) {
    const btn = fxBtn(deck, type);
    if (btn) btn.click();
    if (wet != null) setRange(document.querySelector(`.fx-wet[data-deck="${deck}"]`), wet);
  }

  // Put a deck back to neutral so it is clean when it becomes the staging deck.
  // Level-match the incoming song to the playing one (trim knob, like a DJ
  // gain-staging on the mixer): vibe gate reports gain_match_db = playing RMS
  // minus candidate RMS. Clamped to the knob's range (-12 dB .. +6 dB).
  function matchGain(outDeck, inDeck, db) {
    const knob = (d) => document.querySelector(`.gain-knob[data-deck="${d}"]`);
    const outK = knob(outDeck), inK = knob(inDeck);
    if (!inK) return;
    const base = outK ? parseFloat(outK.value) || 1 : 1;
    const g = Number.isFinite(db) ? base * Math.pow(10, db / 20) : base;
    setRange(inK, Math.max(0.25, Math.min(2, g)).toFixed(2));
  }

  function resetDeck(deck) {
    setRange(eqEl(deck, "low"), 0);
    setRange(eqEl(deck, "mid"), 0);
    setRange(eqEl(deck, "high"), 0);
    setLoop(deck, false);
    setFx(deck, "none");
    // A key-locked / tempo-matched blend leaves this deck's pitch off native;
    // never carry that into its next track (it must load at its own BPM).
    setRange(document.querySelector(`.pitch-fader[data-deck="${deck}"]`), 0);
  }

  const MASHUP_VOX = 0.7;     // B's voice under A's music but never buried (user)
  // {entry, M, why} when a mashup transition fits A -> B, else null.
  function mashupFits(od, idk) {
    const ve = idk._vocalEntry;
    if (!od.stemsReady || !idk.stems || !ve || ve.entry == null || !od.bpm || !idk.bpm) return null;
    const aEff = od.bpm * od._playbackRate();
    const gap = Math.abs(aEff / idk.bpm - 1);
    if (gap > 0.25) return null;
    if (gap > 0.02 && !(idk.tempoStems && Math.abs(idk.tempoStems.bpm / aEff - 1) < 0.01)) return null;
    const cs = window.djMind && window.djMind.core && window.djMind.core.camelotScore;
    const ka = od.analysis && od.analysis.key && od.analysis.key.camelot, kb = idk.analysis && idk.analysis.key && idk.analysis.key.camelot;
    const keyOk = !cs || !ka || !kb || cs(ka, kb) >= 0.8;
    if (!keyOk && !ve.rap) return null;                                // a sung vocal over clashing chords: no
    const barS = 240 / aEff;
    const aLeft = od.buffer ? (od.buffer.duration - od._currentPosition()) / od._playbackRate() : 0;
    const M = ve.vocal32 >= 0.7 && aLeft >= 44 * barS ? 32 : aLeft >= 26 * barS && ve.vocal16 >= 0.5 ? 16 : 0;
    if (!M) return null;
    return { entry: ve.entry, M, why: `${M}-bar mashup: B's ${ve.rap ? "rap" : "vocal"} over A's instrumental${gap > 0.02 ? `, B key-locked ${(gap * 100).toFixed(0)} %` : ""}, then B's beat on the line` };
  }

  // SONG MERGE needs what a mashup needs except a key/vocal verdict (each combo is
  // judged on its own: stem-moves mergeRank): stems on both, B's entry line, B at
  // A's tempo (key-locked stems when they differ), room for M bars + 8.
  function mergeFits(od, idk) {
    const ve = idk._vocalEntry;
    if (!od.stemsReady || !idk.stems || !ve || ve.entry == null || !od.bpm || !idk.bpm) return null;
    const aEff = od.bpm * od._playbackRate(), gap = Math.abs(aEff / idk.bpm - 1);
    if (gap > 0.25) return null;
    if (gap > 0.02 && !(idk.tempoStems && Math.abs(idk.tempoStems.bpm / aEff - 1) < 0.01)) return null;
    const barS = 240 / aEff;
    const aLeft = od.buffer ? (od.buffer.duration - od._currentPosition()) / od._playbackRate() : 0;
    const M = ve.vocal32 >= 0.7 && aLeft >= 44 * barS ? 32 : aLeft >= 26 * barS ? 16 : 0;
    return M ? { entry: ve.entry, M, rap: !!ve.rap, gap } : null;
  }
  // Mean RMS per stem over [songT, songT + bars) of deck d (null: no decoded stems).
  function stemMeans(d, songT, bars) {
    const sm = window.stemMoves, e = sm && sm.stemEnergyBars ? sm.stemEnergyBars(d, songT, 240 / (d.bpm || 128), bars) : null;
    if (!e) return null;
    const m = {};
    for (const n of ["drums", "bass", "vocals", "other"]) m[n] = e[n].reduce((a, b) => a + b, 0) / (e[n].length || 1);
    return m;
  }
  // Plan the merge for A -> B at A's song time aT: algorithm first, then the
  // silent ear re-ranks the top 3 in the background (offline clips, nothing
  // plays) before the transition fires. Stored on B's deck as _mergePlan.
  function planMerge(aId, bId, od, idk, aT) {
    const mf = mergeFits(od, idk), sm = window.stemMoves;
    if (!mf || !sm || !sm.core.mergeRank) return null;
    const cs = window.djMind && window.djMind.core && window.djMind.core.camelotScore;
    const ka = od.analysis && od.analysis.key && od.analysis.key.camelot, kb = idk.analysis && idk.analysis.key && idk.analysis.key.camelot;
    const ranked = sm.core.mergeRank({ eA: stemMeans(od, aT, mf.M), eB: stemMeans(idk, mf.entry, mf.M),
      keyScore: cs && ka && kb ? cs(ka, kb) : null, bRap: mf.rap });
    if (!ranked.length) return null;
    const plan = { ...mf, ranked, pick: ranked[0], aT, heard: false };
    idk._mergePlan = plan;
    if (idk._mergeCtl) idk._mergeCtl.abort();          // flush the audition for the previous booking
    const ctl = idk._mergeCtl = new AbortController();
    fetch("/api/merge/audition", { method: "POST", headers: { "Content-Type": "application/json" }, signal: ctl.signal,
      body: JSON.stringify({ a_id: aId, b_id: bId, a_time: aT, b_time: mf.entry, combos: ranked.slice(0, 3).map((r) => r.combo) }) })
      .then((r) => (r.ok ? r.json() : null))
      // a background job on the server: poll until heard (budget as the old request's
      // 90 s deadline); a newer booking aborts ctl and ends the polling
      .then((first) => autopilotCore.awaitJob(first, async (job) => {
        const g = await fetch(`/api/merge/audition/${encodeURIComponent(job)}`, { signal: ctl.signal });
        return g.ok ? g.json() : null;
      }, { budgetMs: 90000, everyMs: 2000, alive: () => !ctl.signal.aborted && idk._mergePlan === plan }))
      .then((res) => {
        if (!res || !res.ear || idk._mergePlan !== plan) return;
        plan.ranked = sm.core.mergeWithEar(plan.ranked, res.results);
        plan.pick = plan.ranked[0];
        plan.heard = true;
        console.info("merge (silent ear):", plan.ranked.slice(0, 3).map((r) => `${r.label} ${r.score}${r.ear ? ` ear ${r.ear.score}` : ""}`).join(" | "));
      })
      .catch(() => {});
    return plan;
  }

  function recipeKind(recipe) {
    const r = String(recipe || "").toLowerCase();
    if (r.includes("double drop")) return "double";
    if (r.includes("bass swap") || r.includes("drop swap")) return "bass";
    if (r.includes("echo")) return "echo";
    if (r.includes("filter")) return "filter";
    // no hard cuts (user: "hard cuts are a big no"): a cut recipe that slips through
    // (Hard Cut, Quick Cut) still runs as a bass swap on the audio clock
    if (r.includes("cut")) return "bass";
    if (r.includes("loop")) return "loop";
    if (r.includes("blend")) return "blend";
    return "default";
  }

  /**
   * Run a recipe-aware, EQ-first transition from `out` to `inn`.
   * Assumes `inn` was cued at b_time and starts playing at t0.
   * `xfDuration` (seconds) is the caller's budget: < 16 means an early bail,
   * so every bar count is halved to keep the set moving.
   * Returns total duration in ms; the outgoing deck may be stopped after that.
   */
  function executeTransition(recipe, out, inn, xfDuration, t0Audio) {
    clearRun();
    xT0 = Number.isFinite(t0Audio) ? t0Audio : audioCtx.currentTime;
    const kind = recipeKind(recipe);
    const scale = xfDuration >= 16 ? 1 : 0.5;
    const bar = barMs(out) * scale;
    const beat = bar / 4;
    const fromXf = out === "a" ? -1 : 1;
    const toXf = -fromXf;
    const xfEl = () => xfader;
    const lowOut = () => eqEl(out, "low");
    const lowIn = () => eqEl(inn, "low");
    const midOut = () => eqEl(out, "mid");
    const highOut = () => eqEl(out, "high");
    // timers fire XF_LOOKAHEAD_MS early; the audio lands on the exact bar
    const at = (bars, fn) => later(Math.max(0, bars * bar - XF_LOOKAHEAD_MS), () => {
      xOffsetMs = bars * bar;
      try { fn(); } finally { xOffsetMs = 0; }
    });
    const setAt = (getEl, v) => rampParam(getEl, v, v, 0); // instant, on the audio clock
    // One owner of the sub at every moment, and never nobody: A keeps its lows
    // until one beat before the swap line, B's lows open on the line
    // ([[Bass Swap]], [[EQ & Frequency Management]]).
    let swapBar = 0;                  // the EQ recipe's bass-swap line (vocal handoff times B's beat to it)
    const bassSwapAt = (bars) => {
      swapBar = bars;
      at(bars - 0.25, () => rampParam(lowOut, null, LOW_KILL, beat));
      at(bars, () => rampParam(lowIn, LOW_KILL, 0, beat));
    };

    // SONG MERGE (user): for M bars each stem plays from one deck (e.g. A drums +
    // A bass + B vox + B synth), the combo the algorithm + silent ear picked when
    // B was booked; then B takes every stem on the line, 8-bar crossfade.
    // The crossfader runs stem-moves' mergeFader, the curve the booking checked
    // (rawFader: B-ward units -> this direction's fader values).
    const runMergeFader = (sm, M, barS) => {
      for (const s of sm.core.rawFader(sm.core.mergeFader(M), out)) {
        if (s.bar === 0) { rampParam(xfEl, s.from, s.to, s.bars * barS * 1000); continue; }
        later(Math.max(0, (xT0 + s.bar * barS - audioCtx.currentTime) * 1000), () => rampParam(xfEl, s.from, s.to, s.bars * barS * 1000));
      }
    };
    {
      const smM = window.stemMoves, odM = window.decks && window.decks[out], idM = window.decks && window.decks[inn];
      const mp = idM && idM._mergePlan;
      if (recipe === "Stem Merge" && smM && smM.mergeTransition && mp && odM) {
        ["low", "mid", "high"].forEach((b) => { setRange(eqEl(out, b), 0); setRange(eqEl(inn, b), 0); });
        const secs = smM.mergeTransition(out, inn, xT0, mp.entry, mp.M, mp.pick, undefined, mp.ranked);
        if (secs > 0) {
          runMergeFader(smM, mp.M, 240 / (odM.bpm || 128) / odM._playbackRate());
          return secs * 1000;
        }
      }
    }

    // MASHUP -> TRANSITION (user): A's instrumental under B's vocal phrase, hold
    // vox, A's beat drops out, B's beat takes over on the line, 8-bar crossfade.
    // Needs: stems on both, B's vocal phrase, keys that agree (or B raps), and
    // B on key-locked tempo stems at A's tempo when they differ.
    {
      const sm1 = window.stemMoves, od1 = window.decks && window.decks[out], id1 = window.decks && window.decks[inn];
      const mt = sm1 && od1 && id1 ? mashupFits(od1, id1) : null;
      if (mt && kind !== "double") {
        ["low", "mid", "high"].forEach((b) => { setRange(eqEl(out, b), 0); setRange(eqEl(inn, b), 0); });
        const secs = sm1.mashupTransition(out, inn, xT0, mt.entry, mt.M, MASHUP_VOX, mt.why);
        if (secs > 0) {
          runMergeFader(sm1, mt.M, 240 / (od1.bpm || 128) / od1._playbackRate());   // B's voice fades in with the fader
          return secs * 1000;
        }
      }
    }

    // Tempo gap, both songs have stems: STEM BRIDGE instead of an echo-out (user:
    // "Echo Out is painful"). Strip A, hold its voice, B's pads in beatless, the
    // crossfader sweeps across the beatless stretch, B's beat drops on its own line.
    {
      const sm0 = window.stemMoves, od0 = window.decks && window.decks[out], id0 = window.decks && window.decks[inn];
      if (od0 && !od0.stemsReady && od0.rearmStems) od0.rearmStems("stem bridge");
      if (sm0 && (kind === "echo" || recipe === "Stem Bridge") && od0 && id0 && od0.stemsReady && id0.stems) {
        ["low", "mid", "high"].forEach((b) => { setRange(eqEl(out, b), 0); setRange(eqEl(inn, b), 0); });
        const secs = sm0.stemBridge(out, inn, xT0, id0.startOffset || 0);
        if (secs > 0) {
          // B's intro stem sits alone at the centre, the crossfade proper runs
          // into B's line (stemBridgePlan.fader, seconds, B-ward units)
          for (const m of sm0.bridgeFader() || []) {
            later(Math.max(0, (xT0 + m.t - audioCtx.currentTime) * 1000 - XF_LOOKAHEAD_MS), () => {
              xOffsetMs = m.t * 1000;
              try { rampParam(xfEl, m.from * toXf, m.to * toXf, m.dur * 1000); } finally { xOffsetMs = 0; }
            });
          }
          return secs * 1000;
        }
      }
    }

    // Both decks have live stems: the transition is done with stems, not EQ
    // (user: the automixer should lean on stems). Every layer gets one owner:
    // B's synths first, kick + bass swap together on the line, one singer.
    const sm = window.stemMoves;
    const od = window.decks && window.decks[out], idk = window.decks && window.decks[inn];
    if (od && !od.stemsReady && od.rearmStems) od.rearmStems("stem blend");
    if (sm && sm.core.STEM_BLEND_KINDS.has(kind) && od && idk && od.stemsReady && idk.stemsReady) {
      // Bars here are real bars at A's live tempo (B is locked to it), computed
      // unscaled and scaled exactly once: `bar` above already carries `scale`
      // and barMs() ignores the pitch fader.
      const rateO = od._playbackRate ? od._playbackRate() : 1;
      const barU = 240000 / ((od.bpm || 128) * rateO);
      const barS = barU / 1000;
      const aLeft = od.buffer ? (od.buffer.duration - (od._positionAt ? od._positionAt(xT0) : od._currentPosition())) / rateO : 0;
      // A stem blend is a real mashup (both tracks layered), not a quick swap:
      // >= 30 s together before the crossfade lands (user), phrase-snapped;
      // the short window (scale 0.5) still wins. See autopilotCore.stemBlendBars.
      const bars = autopilotCore.stemBlendBars(kind, barS, aLeft / barS, scale);
      ["low", "mid", "high"].forEach((b) => { setRange(eqEl(out, b), 0); setRange(eqEl(inn, b), 0); });
      // Crossfader shape follows stemBlendPlan, never a jump to the centre or
      // the far side (user: "putting the crossfader in centre sounds really
      // odd"): a gentle rise through the mashup body, both decks riding, then
      // one smooth crossfade over the last 8 bars while A's synths and voice
      // leave. The stems decide which layer plays; the fader carries the level.
      const faderMoves = autopilotCore.stemBlendFader(kind, bars, toXf);
      if (sm.stemBlend(kind, out, inn, xT0, bars, barS, undefined, { fader: faderMoves, dir: toXf })) {
        for (const m of faderMoves) {
          const run = () => rampParam(xfEl, m.from, m.to, m.bars * barU);
          if (m.bar === 0) { run(); continue; }
          later(Math.max(0, m.bar * barU - XF_LOOKAHEAD_MS), () => {
            xOffsetMs = m.bar * barU;
            try { run(); } finally { xOffsetMs = 0; }
          });
        }
        return bars * barU;
      }
      apStatus(`${recipe}: stem blend refused (loudness floor or stems, see console), EQ blend instead`);
    }

    // Incoming deck always enters with its sub killed: single bass owner.
    setRange(lowIn(), LOW_KILL);
    setRange(eqEl(inn, "mid"), 0);
    setRange(eqEl(inn, "high"), 0);
    setRange(xfEl(), fromXf);

    let total;
    switch (kind) {
      case "bass": // [[Bass Swap]]: B rises with no lows, one-downbeat bass swap at bar 4
        rampParam(xfEl, fromXf, 0, 4 * bar);
        bassSwapAt(4);
        at(4, () => {
          rampParam(xfEl, 0, toXf, 4 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 4 * bar);
        });
        total = 8;
        break;

      case "double": // both drops together for 8 bars, then A fades out
        // [[Double Drop]]: one bass only - B's lows open, A's killed on the same
        // downbeat (no ramp: two subs must never overlap). A leaves over the last
        // 1.5 bars, never on one downbeat (user: no hard cuts).
        setAt(xfEl, 0);
        setAt(lowOut, LOW_KILL);
        setAt(lowIn, 0);
        at(7, () => rampParam(xfEl, 0, toXf, 1.5 * bar));
        total = 8.5;
        break;

      case "echo": // arm ECHO on A; A keeps its lows until the bar-4 swap, tail carries B's entry
        // one continuous 8-bar crossfader sweep (no 2-bar rush); the echo tail
        // carries A out while B takes over
        dipAllowed("echo", recipe);
        setFx(out, "echo", 0.7);
        rampParam(xfEl, fromXf, toXf, 8 * bar);
        bassSwapAt(4);
        at(4, () => rampParam(highOut, null, HIGH_SWEEP, 4 * bar));
        total = 8;
        break;

      case "filter": // sweep A's mids down over 4 bars; lows swap on the bar-4 line
        rampParam(midOut, null, -10, 4 * bar);
        rampParam(xfEl, fromXf, 0, 4 * bar);
        bassSwapAt(4);
        at(4, () => {
          rampParam(xfEl, 0, toXf, 4 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 4 * bar);
        });
        total = 8;
        break;

      case "loop": // 2-bar loop roll on A holds the exit point steady
        setLoopLength(out, 8);
        setLoop(out, true);
        at(2, () => rampParam(xfEl, fromXf, 0, 2 * bar));
        bassSwapAt(4);
        at(4, () => {
          rampParam(xfEl, 0, toXf, 2 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 2 * bar);
        });
        at(6, () => setLoop(out, false));
        total = 8;
        break;

      case "blend": // 16-bar EQ-first blend: B rises under A (no lows) for 8 bars,
        // one-downbeat bass swap on the bar-8 line, then A fades out over 8 bars.
        // (Old version cut A's lows over bars 0-4 and opened B's at bar 8:
        // 13-15 s with nobody on the bass - measured, research/notes/crossfade-analysis.)
        rampParam(xfEl, fromXf, 0, 8 * bar);
        bassSwapAt(8);
        at(8, () => {
          rampParam(xfEl, 0, toXf, 8 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 8 * bar);
        });
        total = 16;
        break;

      default: // 16 bars: same shape as the blend (the old default kept the fader on
        // A for 8 bars with A's lows already cut: a long thin stretch)
        rampParam(xfEl, fromXf, 0, 8 * bar);
        bassSwapAt(8);
        at(8, () => {
          rampParam(xfEl, 0, toXf, 8 * bar);
          rampParam(highOut, null, HIGH_SWEEP, 8 * bar);
        });
        total = 16;
        break;
    }
    if (!stemHandoff(kind, out, inn, (total * bar) / 1000, (swapBar * bar) / 1000) &&
        window.stemMoves && window.stemMoves.eqIntro && kind !== "double") {
      window.stemMoves.eqIntro(out, inn, xT0, (total * bar) / 1000, (swapBar * bar) / 1000);
    }
    return total * bar;
  }

  // One singer through the blend (user: the outgoing vocal goes onto the
  // incoming stems): B enters as its instrumental, A's vocal rides B's beat on
  // the vocal bus, B's own vocal returns as A's fades (stem-moves.js).
  function stemHandoff(kind, out, inn, totalS, swapS = 0) {
    if (!window.stemMoves || kind === "double" || totalS < 4) return false;
    const od = window.decks && window.decks[out], id = window.decks && window.decks[inn];
    if (!od || !id) return false;
    const ka = od.analysis && od.analysis.key && od.analysis.key.camelot;
    const kb = id.analysis && id.analysis.key && id.analysis.key.camelot;
    const core = window.djMind && window.djMind.core;
    const keyScore = core && core.camelotScore ? core.camelotScore(ka, kb) : 0;
    const p0 = od._positionAt ? od._positionAt(xT0) : od._currentPosition();
    const outVocal = window.stemMoves.vocalShare(od.analysis && od.analysis.vocal_active_regions, p0, p0 + totalS);
    if (!od.stemsReady && od.rearmStems) od.rearmStems("vocal handoff");
    const fits = window.stemMoves.core.handoffFits({ outStems: od.stemsReady, inStems: id.stemsReady, keyScore, outVocal });
    return fits && window.stemMoves.handoff(out, inn, xT0, totalS,
      `${Math.round(outVocal * 100)}% vocal in the blend, keys ${ka}->${kb}: one singer, A's voice over B's beat`, swapS);
  }

  /**
   * LAYER transition ([[3-Deck Layering]], set study item 4): B rides under A
   * as a texture (lows killed, highs trimmed, crossfader just off A) for
   * `hold_bars`, the bass goes to B on the phrase line (A's sub is out one beat
   * before, B's comes in on the line: one sub owner at any time), then A
   * unwinds over `unwind_bars`: highs, then mids, then the fader.
   * `layer.third` (optional) is a cached vocal stem riding B's clean phrase.
   * Bars are A's live (pitch-locked) bars: a 64-bar hold drifts otherwise.
   * Every move is an AudioParam ramp on the audio clock at an exact bar offset
   * from `t0Audio` (B's first downbeat), like executeTransition: the old
   * timer-driven path stepped setRange 20 times per ramp (a zipper on the
   * crossfader / EQ, one step every ~375 ms on a 4-bar ramp at 128 BPM).
   * Returns total duration in ms.
   */
  function executeLayer(out, inn, layer, t0Audio) {
    clearRun();
    xT0 = Number.isFinite(t0Audio) ? t0Audio : audioCtx.currentTime;
    const oa = window.decks && window.decks[out];
    const bpm = oa && oa.bpm > 0 ? oa.bpm * oa._playbackRate() : 128;
    const bar = 240000 / bpm;
    const beat = bar / 4;
    const H = layer.hold_bars, U = layer.unwind_bars;
    const fromXf = out === "a" ? -1 : 1;
    const toXf = -fromXf;
    const xfEl = () => xfader;
    const band = (d, b) => () => eqEl(d, b);
    // timers fire XF_LOOKAHEAD_MS early; the ramp lands on the exact bar
    const at = (bars, fn) => later(Math.max(0, bars * bar - XF_LOOKAHEAD_MS), () => {
      xOffsetMs = bars * bar;
      try { fn(); } finally { xOffsetMs = 0; }
    });

    // 1) texture: B's sub killed, highs trimmed, fader eases to ~-7 dB for B
    // (instant sets before any ramp books the knob; B is still silent here)
    setRange(eqEl(inn, "low"), LOW_KILL);
    setRange(eqEl(inn, "mid"), -3);
    setRange(eqEl(inn, "high"), -8);
    setRange(xfEl(), fromXf);
    rampParam(xfEl, fromXf, fromXf * 0.4, 4 * bar);
    // 2) hold H bars; 3) bass hand-off on the phrase line
    at(H - 0.25, () => rampParam(band(out, "low"), null, LOW_KILL, beat * 0.9));
    at(H, () => {
      rampParam(band(inn, "low"), LOW_KILL, 0, beat);
      rampParam(band(inn, "mid"), null, 0, 2 * bar);
      rampParam(band(inn, "high"), null, 0, 2 * bar);
      rampParam(xfEl, null, 0, 2 * bar);
      // 4) unwind A slowly: highs, then mids, then the fader
      rampParam(band(out, "high"), null, LOW_KILL, (U / 3) * bar);
    });
    at(H + U / 3, () => rampParam(band(out, "mid"), null, LOW_KILL, (U / 3) * bar));
    at(H + (2 * U) / 3, () => rampParam(xfEl, null, toXf, (U / 3) * bar));

    // third element: an earlier / next-next vocal over B's clean phrase
    if (layer.third && window.mashup) {
      later(400, async () => {
        try {
          const t = layer.third;
          if (await window.mashup.play(inn, t.plan, t.host_entry)) {
            mashupTag = ` | ✖ 3rd layer: vocal ${t.name || "callback"}`;
          }
        } catch (e) { console.warn("LAYER third layer failed:", e.message); }
      });
    }
    return (H + U) * bar;
  }

  const ENERGY_DELTA_CLASS = { up: "ap-energy-up", down: "ap-energy-down", maintain: "ap-energy-hold" };
  const ENERGY_DELTA_LABEL = { up: "↑ Energy up", down: "↓ Energy down", maintain: "→ Hold energy" };

  // Text from YouTube titles / the LLM goes into innerHTML: escape it.
  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ── AI playlist panel ─────────────────────────────────────────────────────
  // Shows what is actually lined up: the scheduled NEXT song, songs already
  // downloaded and waiting (READY), and songs still downloading (⬇).
  let scheduledNext = null;   // candidate booked for the coming transition
  let scheduledFireAt = null; // track time of the booked transition on the playing deck
  let pendingSugs = [];       // suggestions whose downloads are in flight
  let aiPicking = false;

  function sugOf(c) {
    return (c && c.suggestion) || { title: c && c.name };
  }

  function showQueue() {
    const rows = [];
    if (scheduledNext) rows.push({ s: sugOf(scheduledNext), tag: "NEXT" });
    if (leadTo && !leadTo.arrived) {
      rows.push({ s: leadTo.cand ? Object.assign({}, sugOf(leadTo.cand), { reason: "your LEAD TO destination" })
                                 : { title: leadTo.text, reason: `steering there in ${leadTo.steps} songs` },
                  tag: leadTo.cand ? `TARGET ${Math.max(0, leadTo.steps - 1 - leadTo.played)}` : "LEAD" });
    }
    ready.forEach((c) => rows.push({ s: sugOf(c), tag: "READY" }));
    const have = new Set(rows.map((r) => `${r.s.artist}|${r.s.title}`));
    pendingSugs.forEach((s) => { if (!have.has(`${s.artist}|${s.title}`)) rows.push({ s, tag: "⬇" }); });
    renderQueue(rows);
  }

  function renderQueue(rows) {
    if (!queueEl) return;
    // BRIDGE PATH banner: where the tempo ladder stands, e.g. "BRIDGE 3/5 → 110 BPM"
    const bl = bridgeLabel();
    const banner = bl
      ? `<div class="ap-bridge" style="font-weight:700;color:#38bdf8;margin:2px 0 6px" title="${esc(bridge.why)}: ${esc(bridge.steps.map(Math.round).join(" → "))}">` +
        `${esc(bl)} <span style="font-weight:400;opacity:.75">toward ${Math.round(bridge.toBpm)} BPM</span></div>`
      : "";
    if (!rows.length) {
      queueEl.innerHTML = `${banner}<div class='ap-empty'>${aiPicking ? "⏳ AI picking the next songs…" : "⏳ Finding next track…"}</div>`;
      return;
    }
    queueEl.innerHTML = banner + rows.map(({ s, tag }) => {
      const eClass = ENERGY_DELTA_CLASS[s.energy_delta] || "";
      const eLabel = ENERGY_DELTA_LABEL[s.energy_delta] || "";
      const genre   = s.genre ? `<span class="ap-genre">${esc(s.genre)}</span>` : "";
      const moment  = s.mix_moment ? `<span class="ap-moment" title="Mix moment">${esc(s.mix_moment)}</span>` : "";
      const energy  = eLabel ? `<span class="ap-energy ${eClass}">${eLabel}</span>` : "";
      const vibe    = s.vibe_link ? `<span class="ap-vibe">"${esc(s.vibe_link)}"</span>` : "";
      const isNext = tag === "NEXT";
      const name = s.artist ? `${esc(s.artist)} — ${esc(s.title)}` : esc(s.title || "?");
      return `
      <div class="ap-item ${isNext ? "ap-next" : ""}" ${isNext ? 'id="ap-next-item"' : ""}>
        <span class="ap-pos">${tag}</span>
        <div class="ap-item-main">
          <span class="ap-name">${name}</span>
          <span class="ap-meta">${esc(s.expected_key || "")}${s.expected_bpm ? "  " + esc(s.expected_bpm) + " BPM" : ""}${genre ? "  " + genre : ""}${isNext ? '  <span id="ap-match-score" style="display:none"></span>' : ""}</span>
          <span class="ap-badges">${energy}${moment}</span>
          ${vibe}
          <span class="ap-why">${esc(s.reason || "")}</span>
        </div>
      </div>`;
    }).join("");
  }

  // Keep a playlist ahead: once the next song is booked, ask the AI what
  // follows IT and pre-download those, so the pool never runs dry and the
  // panel always shows what is coming.
  let toppingUp = false;
  async function topUpPool(afterId) {
    if (toppingUp || !active || ready.length >= 2) return;
    toppingUp = true;
    aiPicking = !ready.length;
    showQueue();
    try {
      const avoid = [scheduledNext && scheduledNext.name, ...ready.map((c) => c.name)].filter(Boolean);
      const sugs = await getSuggestions(afterId, avoid, { lookAhead: true });
      if (!active) return;
      pendingSugs = sugs;
      aiPicking = false;
      showQueue();
      await Promise.all(sugs.map((sg) => downloadSuggestion(sg).then(addReady).catch((e) => {
        console.warn("Prefetch failed:", sg.title, e.message);
      })));
    } catch (e) {
      console.warn("Playlist top-up failed:", e.message);
    } finally {
      pendingSugs = [];
      aiPicking = false;
      toppingUp = false;
      showQueue();
    }
  }

  // ── API calls ─────────────────────────────────────────────────────────────
  async function importUrl(url) {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    return data.tracks || [];
  }

  // Return existing track if artist+title already cached; null otherwise.
  async function findCached(artist, title) {
    try {
      const res = await fetch("/api/tracks");
      if (!res.ok) return null;
      const data = await res.json();
      const tracks = data.tracks || data || [];
      const a = artist.toLowerCase();
      const t = title.toLowerCase();
      return tracks.find(tr => {
        if (tr.not_a_song) return false; // live / event recording or mix in the library
        const dn = (tr.display_name || tr.filename || "").toLowerCase();
        return dn.includes(a) && dn.includes(t);
      }) || null;
    } catch { return null; }
  }

  let energyNotedFor = null; // track whose energy the DJ mind already logged
  const profileById = {};    // LLM current_profile energy (1-10) per track id: PEAK mode
  // Network-level failures (server restarting, connection refused) are retried
  // with backoff and do NOT use up one of prepareTransition's rounds.
  async function getSuggestions(trackId, avoid = [], opts = {}) {
    for (let attempt = 0; ; attempt++) {
      try {
        return await getSuggestionsOnce(trackId, avoid, opts);
      } catch (e) {
        const transient = e instanceof TypeError || /Failed to fetch|NetworkError|timed out|502|503/.test(e.message);
        if (!transient || attempt >= 4 || !active) throw e;
        apStatus(`Server not answering — retrying (${attempt + 1}/4)…`);
        await new Promise((r) => setTimeout(r, 5000));
      }
    }
  }

  // What plays after the current song, in order (booked, then ready): the model
  // sees the last 3 played + these 3 (autopilot_service.prompt_history).
  function queueNames() {
    return [scheduledNext && scheduledNext.name, ...ready.map((c) => c.name)].filter(Boolean).slice(0, 3);
  }

  async function getSuggestionsOnce(trackId, avoid = [], opts = {}) {
    const setPos = Math.min(history.length / 10, 1.0);
    // `avoid` = titles rejected this round (failed download / vibe gate) so the
    // LLM does not propose them again on retry.
    // DJ mind hint: "dip" after a long peak (study rule 9), "callback" late in
    // the set (rule 7), "reprise" of the set's recurring hook (set study
    // mDtud5fLgFQ section 5, with energy_hook naming it). Null most of the time.
    const hint = window.djMind && !opts.lookAhead ? window.djMind.nextEnergyNote(setPos, history) : null;
    const energyNote = hint ? hint.note : null;
    const energyHook = hint ? hint.hook : null;
    const res = await fetch("/api/autopilot/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ set_id: setId, track_id: trackId, occasion: occasionWithBridge(opts), ...leadFields(opts), history: history.slice(-30), avoid: avoid.slice(-6), queue: queueNames(), set_position: setPos, set_mode: setMode(), relaxed: !!window.djSession.relaxed, energy_note: energyNote, energy_hook: energyHook, lookahead: !!opts.lookAhead,
        variety_run: varietyRun().run, variety_genre: varietyRun().genre,
        tempo_target: bridgeTarget(opts.lookAhead), tempo_note: bridgeNote(opts.lookAhead) || null,
        elapsed_seconds: setStartedAt ? (Date.now() - setStartedAt) / 1000 : null }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    // OCCASION FIRST: the AI says the playing song is outside the occasion's
    // music ("punjabi wedding" while Fred again.. plays) -> steer, even across
    // a tempo gap (Echo Out), instead of holding out for a beat-matchable pick.
    if (!opts.lookAhead) {
      if (data.current_genre) currentGenre = data.current_genre;
      if (data.current_era) currentEra = data.current_era;
      steering = data.steering === "move" && steerStep < MAX_STEER_STEPS ? "move" : "stay";
      if (steering === "stay") steerStep = 0;
      // Steering into music at another tempo: ladder there (BRIDGE PATH).
      const far = steering === "move" && !bridge && (data.suggestions || [])
        .map((s) => parseFloat(s.expected_bpm)).find((b) => b > 0 && !locks(playingBpm(), b));
      if (far) startBridge(far, "occasion steering"); // fire-and-forget
    }
    const e = data.current_profile && parseFloat(data.current_profile.energy);
    // Look-ahead describes the booked next song: keep it for when that song plays.
    if (Number.isFinite(e)) profileById[trackId] = e <= 1 ? e * 10 : e;
    if (window.djMind && trackId === currentTrackId) window.djMind.setProfileEnergy(profileById[trackId]);
    // Look-ahead calls describe the NEXT song: they must not overwrite the
    // playing song's energy (it drives the set-mode window).
    if (Number.isFinite(e) && !opts.lookAhead) {
      currentEnergy = e <= 1 ? e * 10 : e;
      if (window.djMind && energyNotedFor !== trackId) { energyNotedFor = trackId; window.djMind.noteEnergy(currentEnergy); }
    }
    return data.suggestions || [];
  }

  async function matchTracks(aId, bId) {
    const res = await fetch("/api/match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // no_cuts: the matcher never hands the autopilot a Hard Cut / Quick Cut (user rule)
      body: JSON.stringify({ track_a_id: aId, track_b_id: bId, top_n: 1, no_cuts: true }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    const candidate = (data.candidates || [])[0] || null;
    if (candidate && data.vibe) candidate.vibe = data.vibe;
    return candidate;
  }

  // ── pre-download pool ─────────────────────────────────────────────────────
  // Every suggestion starts downloading as soon as the AI proposes it (the
  // server runs 2 downloads in parallel; progress bars in the panel). Songs that
  // finish but are not used right away wait in `ready`, so the next transition
  // can often start with no download at all and the playing song never runs
  // out before the next one exists.
  const ready = [];     // { track_id, name, duration, suggestion }
  const MAX_READY = 4;

  // Length check: the server already rejects < 90 s and >= 9 min; LONG mode
  // needs songs that can actually ride 3-6 min.
  function minSongSecs() { return setMode() === "long" ? 180 : 90; }

  async function trackInfo(trackId) {
    try {
      const a = await fetch(`/api/tracks/${trackId}/analysis`).then((r) => r.json());
      return { duration: (a && a.duration) || 0, bpm: (a && a.bpm) || 0 };
    } catch { return { duration: 0, bpm: 0 }; }
  }

  async function downloadSuggestion(s) {
    const label = `${s.artist} — ${s.title}`;
    const cached = await findCached(s.artist, s.title);
    if (cached) {
      const info = await trackInfo(cached.track_id);
      return { track_id: cached.track_id, name: cached.display_name || label,
               duration: info.duration, bpm: info.bpm, suggestion: s };
    }
    const tracks = window.dlJobs
      ? await window.dlJobs.run(s.search_query, label)
      : await importUrl(s.search_query);
    if (!tracks.length) throw new Error("nothing downloaded");
    const t = tracks[0];
    const info = t.duration && t.bpm ? t : await trackInfo(t.track_id);
    return { track_id: t.track_id, name: t.display_name || label,
             duration: info.duration, bpm: info.bpm, suggestion: s };
  }

  function addReady(c) {
    if (!c || history.includes(c.name) || ready.some((r) => r.track_id === c.track_id)) return;
    ready.push(c);
    while (ready.length > MAX_READY) ready.shift();
    showQueue();
  }

  // Match + gates + load + schedule one downloaded candidate. True = scheduled.
  // Can `cand` be tempo-locked to the playing deck (half/double time counts)?
  // +/-8 % on pitch; +/-15 % when the playing deck has stems: the next song
  // then plays on key-locked tempo stems (multi-BPM stem sets), no pitch shift.
  function stemsOn() { const d = window.decks && window.decks[activeDeck]; return !!(d && d.stems); }
  function lockLimit() { return stemsOn() ? 0.25 : 0.08; }
  function tempoLockableAt(cand, lim) {
    const d = window.decks && window.decks[activeDeck];
    if (!d || !d.bpm || !cand.bpm) return true;
    const aEff = d.bpm * d._playbackRate();
    return [1, 2, 0.5].some((m) => Math.abs(aEff / (cand.bpm * m) - 1) <= lim);
  }
  function tempoLockable(cand) {
    const d = window.decks && window.decks[activeDeck];
    if (!d || !d.bpm || !cand.bpm) return true; // unknown: let the matcher decide
    const aEff = d.bpm * d._playbackRate();
    return [1, 2, 0.5].some((m) => Math.abs(aEff / (cand.bpm * m) - 1) <= lockLimit());
  }
  let allowTempoJump = false; // set on the last round so the set never stalls
  // Tempo-jump budget (user: "genre switch once in a while is fine, or in the
  // middle of high-BPM songs where people are dancing, switch with echo"):
  // beat-matched by default; one tempo jump after JUMP_EVERY songs, or after
  // PEAK_JUMP_EVERY while the floor is at peak energy; never back to back.
  const JUMP_EVERY = 4;
  const PEAK_JUMP_EVERY = 2;
  let songsSinceJump = 0;          // a set starts beat-matched; first jump after JUMP_EVERY songs
  let jumpPending = false;         // the booked transition is a tempo jump
  function tempoJumpBudget() {
    // One song (user): with stems no planned tempo jumps; only the last-round
    // fallback may jump, so the set never stalls.
    if (stemsOn()) return false;
    const peak = currentEnergy != null && currentEnergy >= 8;
    return songsSinceJump >= (peak ? PEAK_JUMP_EVERY : JUMP_EVERY);
  }
  let steering = "stay";      // "move" while steering toward the occasion's music
  // Variety: subgenre of each played song, to spot a style that has plateaued.
  let genreLog = [];
  let currentGenre = "";
  let currentEra = "";     // model's release decade for the playing song ("1990s"): library fallback holds it
  function genreFamily(g) {
    return String(g || "").toLowerCase().split(/[\/,&(]| - /)[0].replace(/[^a-z0-9 ]+/g, " ").trim();
  }
  function varietyRun() {
    const fam = genreFamily(currentGenre || genreLog[genreLog.length - 1]);
    if (!fam) return { run: 0, genre: "" };
    let run = 0;
    for (let i = genreLog.length - 1; i >= 0 && genreFamily(genreLog[i]) === fam; i--) run++;
    return { run, genre: fam };
  }
  let steerStep = 0;          // bridge songs played so far on the current steer (cap 7)
  // Relaxed sessions (user: "in relax sessions it should not go upbeat,
  // maintain relaxed session, no need to sampler mix"): energy never steps up,
  // no sampler / fills / peak or remix moves / riff over rap, LONG set mode.
  const RELAXED_OCCASION = /\b(relax(ed|ing)?|chill(ed|out|ing)?|calm|lounge|dinner|study(ing)?|focus|sleep(y|ing)?|sunday|morning|coffee|caf[eé]|spa|yoga|meditat\w*|ambient|wind(ing)?\s*down|background|mellow|sunset|laid[\s-]*back|easy\s*listening|low[\s-]*key|unwind\w*)\b/i;
  function isRelaxedOccasion(o) { return !!o && RELAXED_OCCASION.test(o) && !HIGH_ENERGY_OCCASION.test(o); }
  window.djSession = window.djSession || { relaxed: false };
  function applySessionMood() {
    const relaxed = isRelaxedOccasion(occasion);
    window.djSession.relaxed = relaxed;
    const modeEl = document.getElementById("ap-mode");
    if (relaxed && modeEl && modeEl.value === "hybrid") modeEl.value = "long";
    if (relaxed) apStatus(`"${occasion}" is a relaxed session → LONG mode, energy held, no sampler / fills / peak moves`);
    return relaxed;
  }
  const HIGH_ENERGY_OCCASION = /\b(wedding|shaadi|sangeet|baraat|mehndi|reception|party|club\s*night|peak|festival|rave|birthday|bachelor(ette)?|new\s*year)\b/i;
  const MAX_STEER_STEPS = 7;
  function occasionWithStep(opts = {}) {
    if (!occasion || steering !== "move") return occasion;
    const step = Math.min(MAX_STEER_STEPS, steerStep + (opts.lookAhead ? 2 : 1));
    return `${occasion} — steering step ${step} of max ${MAX_STEER_STEPS} toward this occasion's music` +
           (step >= MAX_STEER_STEPS - 1 ? " (FINAL: pick the occasion's own anthems now)" : "");
  }

  // Occasion + the BRIDGE step as a hint suffix (only when the user set an
  // occasion: an occasion-less set gets the step through tempo_target alone).
  function occasionWithBridge(opts = {}) {
    const base = occasionWithStep(opts);
    const t = bridgeTarget(opts.lookAhead);
    return base && t ? `${base} — TEMPO BRIDGE: songs natively near ${Math.round(t)} BPM (${bridgeNote(opts.lookAhead)})` : base;
  }

  // ── LEAD TO (user-directed destination) ──────────────────────────────────
  // The user types where the set should end up and in how many songs:
  //   "Artist - Title" -> that exact song is pre-downloaded and played as the
  //                       destination after the steering songs;
  //   an artist / genre -> the set steers into that world, then stays there.
  // Every step stays beat-matched: the destination's tempo becomes a BRIDGE
  // PATH target, and each suggestion round is told how far along it is.
  let leadTo = null; // { text, kind, steps, played, cand, bpm, arrived }
  const leadStatusEl = document.getElementById("ap-lead-status");
  const leadBox = document.getElementById("ap-lead");
  const leadCancel = document.getElementById("ap-lead-cancel");

  function leadStatus(msg) {
    if (leadStatusEl) leadStatusEl.textContent = msg || "";
    if (leadBox) leadBox.classList.toggle("is-leading", !!leadTo);
    if (leadCancel) leadCancel.hidden = !leadTo;
  }

  function leadFields(opts = {}) {
    if (!leadTo) return {};
    return { lead_to: leadTo.text, lead_steps: leadTo.steps,
             lead_step: Math.min(leadTo.steps, leadTo.played + (opts.lookAhead ? 2 : 1)),
             lead_bpm: leadTo.bpm || null };
  }

  function leadHint(base, opts = {}) {
    if (!leadTo) return base;
    const step = Math.min(leadTo.steps, leadTo.played + (opts.lookAhead ? 2 : 1));
    const last = step >= leadTo.steps;
    let hint;
    if (leadTo.kind === "song") {
      hint = `LEAD TO "${leadTo.text}"${leadTo.bpm ? ` (~${Math.round(leadTo.bpm)} BPM)` : ""}: step ${step} of ${leadTo.steps}; ` +
             `each song clearly closer to it in genre, energy and sound; the target song itself plays after the last step, ` +
             `so do NOT suggest it; pick songs that lead naturally into it`;
    } else {
      hint = `LEAD TO "${leadTo.text}": step ${step} of ${leadTo.steps}; each song clearly closer to that world` +
             (last ? " (FINAL step: be fully inside it now)" : "");
    }
    return base ? `${base} — ${hint}` : hint;
  }

  async function startLead(picked) {
    const input = document.getElementById("ap-lead-input");
    const text = picked ? picked.title : (input ? input.value.trim() : "");
    hideLeadResults();
    if (!text) { leadStatus("Type a song ('Artist - Title'), an artist or a genre"); return; }
    if (!active) { leadStatus("Start a set first; LEAD steers a running set"); return; }
    const stepsEl = document.getElementById("ap-lead-steps");
    const steps = Math.max(2, Math.min(6, parseInt(stepsEl ? stepsEl.value : "4", 10) || 4));
    // a picked YouTube result is a song; typed text is a genre / artist to steer toward
    const kind = picked ? "song" : "style";
    leadTo = { text, kind, steps, played: 0, cand: null, bpm: 0, arrived: false };
    leadStatus(kind === "song" ? `fetching the destination "${text}"…` : `steering toward ${text} in ${steps} songs`);
    // steer the NEXT pick, not the one already booked: drop not-yet-booked
    // pool songs that were chosen for the old direction
    ready.length = 0;
    pendingSugs = [];
    showQueue();
    if (kind === "song") {
      try {
        // the exact video the user picked (direct URL: live-title / length checks skip
        // what they deliberately chose, mix / interview checks still apply)
        const tracks = window.dlJobs ? await window.dlJobs.run(picked.url, `LEAD TO: ${text}`)
                                     : await importUrl(picked.url);
        if (!tracks.length) throw new Error("nothing downloaded");
        const t = tracks[0];
        const info = t.duration && t.bpm ? t : await trackInfo(t.track_id);
        const c = { track_id: t.track_id, name: t.display_name || text, duration: info.duration, bpm: info.bpm,
                    suggestion: { title: t.display_name || text, reason: "your LEAD TO destination" } };
        if (!leadTo || leadTo.text !== text) return; // cancelled meanwhile
        leadTo.cand = c;
        leadTo.bpm = c.bpm || 0;
        leadStatus(`→ ${c.name}${c.bpm ? ` (${Math.round(c.bpm)} BPM)` : ""} after ${steps - 1} steering song${steps > 2 ? "s" : ""}`);
        // tempo ladder toward the destination (fewer steps than the lead)
        if (c.bpm && !locks(playingBpm(), c.bpm)) startBridge(c.bpm, `lead to ${c.name}`, steps - 1);
      } catch (e) {
        leadStatus(`couldn't find "${text}" (${e.message}) — steering toward its style instead`);
        if (leadTo) leadTo.kind = "style";
      }
    }
    showQueue();
  }

  function cancelLead(msg) {
    leadTo = null;
    leadStatus(msg || "");
    showQueue();
  }

  // Called after every transition.
  function advanceLead() {
    if (!leadTo) return;
    if (leadTo.arrived) {
      const t = leadTo.text;
      cancelLead(`✓ arrived: ${t}`);
      return;
    }
    leadTo.played++;
    if (leadTo.kind === "style" && leadTo.played >= leadTo.steps) {
      // arrived in that world: keep it as the set's direction from here on
      occasion = occasion ? `${occasion}; now inside ${leadTo.text}` : leadTo.text;
      cancelLead(`✓ now in ${leadTo.text} — the set stays there`);
      return;
    }
    leadStatus(leadTo.kind === "song"
      ? `step ${leadTo.played}/${leadTo.steps - 1} toward ${leadTo.cand ? leadTo.cand.name : leadTo.text}`
      : `step ${leadTo.played}/${leadTo.steps} toward ${leadTo.text}`);
  }

  // The destination song is due: after steps-1 steering songs.
  function leadDue() {
    return !!(leadTo && leadTo.kind === "song" && leadTo.cand && !leadTo.arrived &&
              leadTo.played >= leadTo.steps - 1);
  }

  // ── BRIDGE PATH (set study item 5, [[Genre Bridge Playbook]]) ─────────────
  // A far tempo target (beyond the 8% lock, or the occasion steering into
  // another genre) becomes a BPM ladder of beat-matched songs, <= ~6% per step
  // or a half/double-time link (87 <-> 174), from POST /api/bridge/plan. Each
  // step's BPM is the tempo target of the next suggestion round, and the
  // tempo gate prefers candidates that move up the ladder. The tempo-jump
  // budget (Echo Out) stays the fallback: an infeasible ladder, or the last
  // search round.
  let bridge = null;          // { toBpm, steps[], total, played, link, why }
  let bridgePending = false;
  let forceJump = false;      // last-round fallback: the set never stalls on a ladder
  const BRIDGE_TOL = 0.015;   // a step counts as reached within 1.5%
  function playingBpm() {
    const d = window.decks && window.decks[activeDeck];
    return d && d.bpm > 0 ? d.bpm * d._playbackRate() : 0;
  }
  function pulseNear(bpm, ref) {  // bpm, or its half/double, closest to ref
    return [1, 2, 0.5].map((m) => bpm * m)
      .reduce((b, x) => (Math.abs(Math.log(x / ref)) < Math.abs(Math.log(b / ref)) ? x : b));
  }
  function locks(a, b) { return a > 0 && b > 0 && Math.abs(pulseNear(b, a) / a - 1) <= 0.08; }
  // Index of the ladder step still ahead of the playing tempo (-1: no bridge).
  function bridgeStepIdx(ahead = 0) {
    if (!bridge) return -1;
    const last = bridge.steps.length - 1;
    const cur = playingBpm();
    if (!cur) return Math.min(last, bridge.played + ahead);
    const dir = Math.sign(bridge.steps[last] - bridge.steps[0]) || 1;
    const p = pulseNear(cur, bridge.steps[0]);
    let i = bridge.steps.findIndex((s) => dir * Math.log(s / p) > BRIDGE_TOL);
    if (i < 0) i = last;
    return Math.min(last, i + ahead);
  }
  function bridgeTarget(lookAhead) {
    const i = bridgeStepIdx(lookAhead ? 1 : 0);
    return i < 0 ? null : bridge.steps[i];
  }
  function bridgeLabel() {
    const i = bridgeStepIdx();
    return i < 0 ? "" : `BRIDGE ${i + 1}/${bridge.total} → ${Math.round(bridge.steps[i])} BPM`;
  }
  function bridgeNote(lookAhead) {
    const i = bridgeStepIdx(lookAhead ? 1 : 0);
    return i < 0 ? "" : `bridge step ${i + 1}/${bridge.total} toward ${Math.round(bridge.toBpm)} BPM` +
      (bridge.link !== "direct" ? ` (${bridge.link}-time link at the end)` : "");
  }
  async function startBridge(toBpm, why, stepsCap) {
    const from = playingBpm();
    if (bridge || bridgePending || !(toBpm > 0) || !from || locks(from, toBpm)) return;
    bridgePending = true;
    try {
      // occasion steering keeps its 5-7 song cap; a plain tempo target gets 5
      const maxSteps = stepsCap ? Math.max(1, stepsCap)
        : steering === "move" ? Math.max(2, MAX_STEER_STEPS - steerStep) : 5;
      const res = await fetch("/api/bridge/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ from_bpm: from, to_bpm: toBpm, max_step_pct: 6, max_steps: maxSteps }),
      });
      const lad = await res.json();
      if (!res.ok) throw new Error(lad.detail || res.statusText);
      if (!lad.feasible || lad.step_count < 2) {
        if (!lad.feasible) console.info("BRIDGE infeasible, tempo-jump budget stays:", (lad.reasons || []).join("; "));
        return;
      }
      bridge = { toBpm, steps: lad.steps, total: lad.step_count, played: 0, link: lad.link, why };
      apStatus(`BRIDGE PATH → ${Math.round(toBpm)} BPM (${why}): ${lad.steps.map(Math.round).join(" → ")}, ${lad.step_pct}%/step`);
      showQueue();
    } catch (e) {
      console.warn("Bridge plan failed:", e.message);
    } finally { bridgePending = false; }
  }
  // After a transition: count the step, end the bridge on arrival or at the cap.
  function advanceBridge() {
    if (!bridge) return;
    bridge.played++;
    const d = window.decks && window.decks[activeDeck];
    const native = d && d.bpm > 0 ? d.bpm : 0;          // pitch eases home to the native tempo
    if (locks(native, bridge.toBpm)) {
      apStatus(`BRIDGE done: ${Math.round(native)} BPM locks to ${Math.round(bridge.toBpm)} BPM`);
      bridge = null;
    } else if (bridge.played >= bridge.total + 1) {
      console.info("BRIDGE ran out of steps: tempo-jump budget takes over");
      bridge = null;
    }
  }
  // Tempo gate while bridging: never step back down the ladder (2% slack).
  function bridgeFits(cand) {
    if (!bridge || !cand.bpm) return true;
    const cur = playingBpm();
    if (!cur || locks(cur, bridge.toBpm)) return true;
    const last = bridge.steps[bridge.steps.length - 1];
    const dir = Math.sign(last - bridge.steps[0]) || 1;
    const c = pulseNear(cand.bpm, cur);
    return dir * Math.log(c / cur) >= -0.02;
  }

  async function evaluateCandidate(currentId, cand, gen) {
    if (!active || !cand) return false;
    const nextId = cand.track_id;
    const nextName = cand.name;
    if (!tempoLockable(cand)) {
      // Far tempo: climb there on a BRIDGE PATH instead of one Echo Out; the
      // jump (budget / last round) stays the fallback.
      if (!bridge && !forceJump && !history.includes(nextName)) await startBridge(cand.bpm, `toward ${nextName}`);
      if (!allowTempoJump || (bridge && !forceJump)) {
        apStatus(`Not after this song: ${nextName} (${Math.round(cand.bpm)} BPM can't be beat-matched)` +
                 `${bridge ? ` — ${bridgeLabel()}` : ""} — kept for later`);
        cand.keep = true;
        return false;
      }
    } else if (!forceJump && !bridgeFits(cand)) {
      apStatus(`Not now: ${nextName} (${Math.round(cand.bpm)} BPM) steps back down the ${bridgeLabel()} — kept for later`);
      cand.keep = true;
      return false;
    }
    if (nextId === currentId || history.includes(nextName)) return false;
    if (cand.duration && cand.duration < minSongSecs()) {
      apStatus(`Skipping ${nextName}: ${fmtTime(cand.duration)} is too short for a ${setMode().toUpperCase()} set`);
      return false;
    }

    apStatus(`Matching transition → ${nextName}…`);
    let candidate = await matchTracks(currentId, nextId);
    if (!candidate) return false;

    // Measured vibe gate: reject candidates whose loudness / brightness /
    // onset density / energy sit too far from what is playing right now.
    if (candidate.vibe && candidate.vibe.ok === false) {
      const why = (candidate.vibe.reasons || []).join("; ") || `distance ${candidate.vibe.distance}`;
      console.warn("Autopilot vibe reject:", nextName, why); window.aiStep && window.aiStep("candidate_reject", { track_id: nextId, phase: "selection", decision: "vibe reject", why });
      apStatus(`Not after this song: ${nextName} (${why}) — kept for later`);
      cand.keep = true; // pairwise: may fit fine after the next song
      return false;
    }

    // Measured energy gate (app/music_brain/energy.py, 1-10 vs the library): the next
    // song stays within 2 levels (1 relaxed), the set arc decides the direction.
    // The last-round fallback allows one more level so the set never stalls.
    const ev = candidate.vibe;
    if (ev && Number.isFinite(ev.energy_a) && Number.isFinite(ev.energy_b)) {
      const verdict = autopilotCore.energyStepOk(ev.energy_a, ev.energy_b, {
        relaxed: !!(window.djSession && window.djSession.relaxed), songs: history.length, force: forceJump,
        rawDelta: Number.isFinite(ev.energy_raw_a) && Number.isFinite(ev.energy_raw_b) ? ev.energy_raw_b - ev.energy_raw_a : null });
      if (!verdict.ok) {
        console.warn("Autopilot energy reject:", nextName, verdict.why); window.aiStep && window.aiStep("candidate_reject", { track_id: nextId, phase: "selection", decision: "energy reject", why: verdict.why });
        apStatus(`Not after this song: ${nextName} (${verdict.why}) — kept for later`);
        cand.keep = true;
        return false;
      }
      console.info("energy:", nextName, verdict.why);
    }

    // Show match score on the NEXT queue card.
    const scoreEl = document.getElementById("ap-match-score");
    if (scoreEl) {
      const sc = Math.round(candidate.score || 0);
      const good = sc >= 65;
      scoreEl.textContent = `${good ? "⭐" : "⚡"} ${sc}/100${good ? "" : " · early exit"}`;
      scoreEl.style.cssText = `display:inline;font-weight:700;color:${good ? "#4ade80" : "#f97316"};margin-left:6px`;
    }

    // AI plan for this pair (candidate, exit phrase, DJ-mind moves), fetched
    // while the next track loads. Rules-only if it fails or times out.
    const aiPlan = requestMindPlan(currentId, nextId, candidate);

    // Preload next track into staging deck
    apStatus(`Loading ${nextName} into deck ${stagingDeck().toUpperCase()}…`);
    const audioRes = await fetch(`/api/audio/tracks/${nextId}`);
    if (!audioRes.ok) return false;
    const blob = await audioRes.blob();
    if (!active) return false;
    await loadIntoDeck(stagingDeck(), nextId, nextName, blob);
    // tempo gap 2-15 %: render its key-locked tempo stems now, long before the blend
    {
      const oa1 = window.decks && window.decks[activeDeck], sd1 = window.decks && window.decks[stagingDeck()];
      if (oa1 && sd1 && oa1.bpm && sd1.useTempoStems) {
        const waitStems = async () => { for (let i = 0; i < 40 && !sd1.stems; i++) await new Promise((r) => setTimeout(r, 500)); };
        waitStems().then(() => {
          if (!sd1.bpm || !sd1.stems) return;
          const aEff1 = oa1.bpm * oa1._playbackRate();
          const m1 = [1, 2, 0.5].reduce((b, m) => (Math.abs(aEff1 / (sd1.bpm * m) - 1) < Math.abs(aEff1 / (sd1.bpm * b) - 1) ? m : b));
          const g1 = Math.abs(aEff1 / (sd1.bpm * m1) - 1);
          if (g1 > 0.02 && g1 <= 0.25) sd1._tempoStemsJob = sd1.useTempoStems(aEff1 / m1);
          // where its vocal phrase starts (for a mashup transition)
          fetch(`/api/tracks/${nextId}/vocal_entry`).then((r) => r.json()).then((v) => { sd1._vocalEntry = v; }).catch(() => {});
        });
      }
    }
    // Over 8 % the blend needs the key-locked stems: book the song only once they're on.
    if (cand.bpm && !tempoLockableAt(cand, 0.08) && tempoLockableAt(cand, 0.25)) {
      const sd2 = window.decks && window.decks[stagingDeck()];
      apStatus(`Key-locking ${nextName} to this tempo (tempo stems)…`);
      const t2 = Date.now();
      while (sd2 && !sd2._tempoStemsJob && Date.now() - t2 < 25000) await new Promise((r) => setTimeout(r, 500));
      const ok2 = sd2 && sd2._tempoStemsJob ? await Promise.race([sd2._tempoStemsJob, new Promise((r) => setTimeout(() => r(false), 90000))]) : false;
      if (!ok2 && !forceJump) {
        apStatus(`Not now: ${nextName} needs key-locked stems that aren't ready — kept for later`);
        cand.keep = true;
        return false;
      }
    }
    matchGain(activeDeck, stagingDeck(), candidate.vibe && candidate.vibe.gain_match_db);
    const plan = await aiPlan;
    if (!active || currentTrackId !== currentId) return false;
    if (plan && plan.candidate) {
      // The AI picked a candidate + exit phrase: both already validated server-side.
      candidate = Object.assign({}, candidate, plan.candidate, { vibe: candidate.vibe });
      apStatus(`AI plan: ${plan.candidate.recipe}, exit ${fmtTime(plan.candidate.a_time)}`);
    }

    const plan0 = await requestBlend(currentId, nextId, candidate);
    const blend = plan0 && plan0.ok !== false ? plan0 : null;
    const minExit = plan0 && plan0.min_exit != null ? plan0.min_exit : null;
    if (!active || currentTrackId !== currentId) return false;

    // LAYER (set study item 4): hold both records, then unwind A. Tempo-locked
    // pairs only; the DJ mind's rules decide and keep the veto over the AI.
    let layer = blend ? await requestLayer(currentId, nextId, candidate, plan, cand) : null;
    if (!active || currentTrackId !== currentId) return false;
    if (layer && !(layer.start >= deckPosition(activeDeck) + 16)) layer = null; // start slipped past

    if (gen !== undefined && gen !== prepGen) return false; // superseded by a restarted search
    // The silent ear pre-plans the transition (when B starts inside A, from which
    // of B's lines, how long both play, who owns each stem); the master plays it.
    if (!layer) {
      apStatus(`Ear pre-planning the mix into ${nextName}…`);
      // the plan may take a while (renders + the ear): never hold the booking past
      // PREPLAN_WAIT_MS; the server keeps going and caches a heard plan for next time
      const pp = await Promise.race([requestPreplan(currentId, nextId, candidate),
        new Promise((r) => setTimeout(() => r(null), PREPLAN_WAIT_MS))]);
      if (!active || currentTrackId !== currentId || (gen !== undefined && gen !== prepGen)) return false;
      if (pp) candidate = Object.assign({}, candidate, { preplan: pp });
    }
    const fireAt = scheduleTransition(currentId, nextId, nextName, candidate, blend, minExit, layer);
    scheduledFireAt = fireAt;
    if (!layer) tryMashup(currentId, nextId, nextName, fireAt); // fire-and-forget; B itself enters under a LAYER
    scheduledNext = cand;
    pendingSugs = []; // leftovers show up as READY when their download lands
    showQueue();
    topUpPool(nextId); // fire-and-forget: songs for AFTER the next one
    return true;
  }

  // Exit window (track seconds) for the current song, same maths as scheduleTransition.
  function exitWindow(score) {
    const w = playWindow(score);
    const od = window.decks && window.decks[activeDeck];
    const trackEnd = (od && od.buffer ? od.buffer.duration : Infinity) - w.xf - 2;
    return { lo: Math.min(entryPos + w.min, trackEnd), hi: Math.min(entryPos + w.max, trackEnd) };
  }

  // Beat-to-beat blend plan: vocal-free exit phrase in the playing song,
  // vocal-free entry phrase in the next one, and B's tempo-lock rate.
  // null -> fall back to the matcher's points (still phrase + tempo aligned).
  async function requestBlend(currentId, nextId, candidate) {
    const win = exitWindow(candidate.score || 50);
    const od = window.decks && window.decks[activeDeck];
    const lo = Math.max(win.lo, deckPosition(activeDeck) + 20);
    if (!od || !(win.hi > lo)) return null;
    apStatus("Mapping vocals for a beat-to-beat blend…");
    try {
      const res = await fetch("/api/blend/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          a_id: currentId, b_id: nextId, window_lo: lo, window_hi: win.hi,
          a_bpm_effective: od.bpm * od._playbackRate(),
          bars: setMode() === "quick" || steering === "move" ? 8 : 16,
          a_entry: entryPos, // the playing song's first drop must play before we leave
        }),
      });
      const plan = await res.json();
      if (!res.ok || !plan.ok) {
        console.info("Blend plan unavailable:", plan.detail || (plan.reasons || []).join("; "));
        // tempo gap: no beat blend, but the drop floor still applies to the echo-out exit
        return res.ok && plan.min_exit != null ? { ok: false, min_exit: plan.min_exit } : null;
      }
      // PEAK MOVES: on top of the tempo-locked plan, an entry on B's first long
      // drop for DOUBLE DROP / DROP SWAP. The DJ mind decides whether to use it.
      const peakEl = document.getElementById("ap-peak-toggle");
      if (window.djMind && window.djMind.planPeak && (!peakEl || peakEl.checked)) {
        try {
          const r2 = await fetch("/api/blend/plan", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              a_id: currentId, b_id: nextId, window_lo: lo, window_hi: win.hi,
              a_bpm_effective: od.bpm * od._playbackRate(), bars: 8, entry_mode: "drop",
            }),
          });
          const drop = await r2.json();
          if (r2.ok && drop.ok) plan.drop = drop;
        } catch (e) { /* peak moves are optional: plain blend */ }
      }
      return plan;
    } catch (e) {
      console.warn("Blend plan failed:", e.message);
      return null;
    }
  }

  // LAYER plan for a tempo-locked pair, or null. The DJ mind checks its cap
  // (one LAYER every few songs) before the server call and the full rules
  // (key, groove, vocal clash, steering, peak floor) after it.
  async function requestLayer(currentId, nextId, candidate, aiPlan, cand) {
    const mind = window.djMind;
    if (!mind || !mind.planLayer || !mind.core || !mind.core.layerBars) return null;
    const aiProposed = !!(aiPlan && aiPlan.layer);
    const base = { steering: steering === "move", peak: false, energy: currentEnergy,
                   aiProposed, aiWhy: aiPlan && aiPlan.layer_reason };
    const pre = mind.planLayer(Object.assign({ ok: true, keyScore: 1, vocalClash: 0, groove: true }, base));
    if (!pre.layer) {
      if (aiProposed) console.info("LAYER (AI) vetoed:", pre.why);
      return null;
    }
    const win = exitWindow(candidate.score || 50);
    const od = window.decks && window.decks[activeDeck];
    const lo = Math.max(win.lo, deckPosition(activeDeck) + 20);
    if (!od || !(win.hi > lo)) return null;
    const { maxHold, unwind } = mind.core.layerBars(setMode());
    // third element: next-next songs first, then earlier songs (callbacks)
    const thirdIds = ready.map((c) => c.track_id)
      .concat(playedIds.slice(0, -1).reverse())
      .filter((id, i, all) => id !== currentId && id !== nextId && all.indexOf(id) === i).slice(0, 6);
    const names = {};
    ready.forEach((c) => { names[c.track_id] = c.name; });
    try {
      apStatus("Checking a LAYER (both records together)…");
      const res = await fetch("/api/layer/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          a_id: currentId, b_id: nextId, window_lo: lo, window_hi: win.hi,
          a_bpm_effective: od.bpm * od._playbackRate(), a_entry: entryPos,
          max_hold_bars: maxHold, unwind_bars: unwind, third_ids: thirdIds,
        }),
      });
      const plan = await res.json();
      if (!res.ok) { console.info("Layer plan unavailable:", plan.detail); return null; }
      const dec = mind.planLayer(Object.assign({
        ok: plan.ok, why: (plan.reasons || []).join("; "), keyScore: plan.key_score,
        vocalClash: plan.vocal_clash, groove: plan.groove,
      }, base));
      if (!dec.layer) {
        console.info(`LAYER off for ${cand ? cand.name : nextId}:`, dec.why);
        return null;
      }
      if (plan.third) plan.third.name = names[plan.third.guest_id] || "callback";
      return Object.assign(plan, { source: dec.source, why: dec.why });
    } catch (e) {
      console.warn("Layer plan failed:", e.message);
      return null;
    }
  }

  function setDeckPitch(deckId, pct, range = 8) {
    const d = window.decks && window.decks[deckId];
    if (!d) return;
    const v = Math.max(-range, Math.min(range, pct));
    d.setPitchPercent(v);
    const fader = document.querySelector(`.pitch-fader[data-deck="${deckId}"]`);
    if (fader) fader.value = String(v.toFixed(1));
    const readout = document.getElementById(`pitch-readout-${deckId}`);
    if (readout) readout.textContent = `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;
  }

  // Pitch fader + readout follow the deck's live rate (cosmetic only). Writes
  // .value without an "input" event, so the fader's listener never sets the
  // rate (a real user drag still does, and cancels the glide: deck._applyRate).
  function showPitch(deckId, pct) {
    const fader = document.querySelector(`.pitch-fader[data-deck="${deckId}"]`);
    if (fader) fader.value = String(pct.toFixed(1));
    const readout = document.getElementById(`pitch-readout-${deckId}`);
    if (readout) readout.textContent = `${pct > 0 ? "+" : ""}${pct.toFixed(1)}%`;
  }
  function followPitch(deckId, untilAudioT) {
    const d = window.decks && window.decks[deckId];
    const t = setInterval(() => {                           // 4 Hz: a control, not an animation
      if (!d || !active) { clearInterval(t); return; }
      showPitch(deckId, (d._playbackRate() - 1) * 100 - (d._bendPercent || 0));
      if (audioCtx.currentTime >= untilAudioT || !d._rateRamp) { clearInterval(t); showPitch(deckId, d._pitchPercent); }
    }, 250);
    runTimers.push(t);
  }

  // After a tempo-locked handover the new song goes back to its OWN tempo
  // (user: "also the bpm should come to normal"), starting on its next 8-bar
  // phrase line, so pitch shift never accumulates across the set. The path per
  // gap is autopilotCore.homePlan; the rate itself glides on the audio clock
  // (deck.rampPitchPercent: one AudioParam ramp on every source, position clock
  // rebased), not in JS steps. A bridge (advanceBridge) expects each song at
  // its native tempo too: it counts the song's own BPM once this lands.
  const EASE_BARS = 32;
  let homeGen = 0;
  function easePitchHome(deckId) {
    const d = window.decks && window.decks[deckId];
    if (!d || !d.playing || !d.bpm || typeof d.rampPitchPercent !== "function") return;
    const gen = ++homeGen, song = d.analysis;
    const alive = () => active && activeDeck === deckId && gen === homeGen && d.analysis === song && d.playing;
    const bpm = d.bpm;
    const gap = d._pitchPercent;
    const rateNow = d._playbackRate();
    const barReal = 240 / (bpm * rateNow);
    const phraseSong = 8 * 240 / bpm;                       // one 8-bar phrase, song seconds
    const songLeftS = d.buffer ? (d.buffer.duration - deckPosition(deckId)) / rateNow - 60 : 0;
    const plan = autopilotCore.homePlan({ gapPct: gap, tempoStems: !!d.tempoStems, songLeftS, phraseS: 8 * barReal });
    if (plan.path === "none") return;
    const say = (msg) => { console.info(`tempo home ${deckId.toUpperCase()}: ${msg}`); apStatus(`Tempo home: ${msg}`); };
    say(`${plan.path} - ${plan.why}`);
    // Audio time of the next phrase line at least `minLead` s away (entryPos is
    // on the song's own grid, so its 8-bar lines are the song's phrase lines).
    const nextLine = (minLead = 0.4) => {
      const r = d._playbackRate(), now = audioCtx.currentTime;
      let wait = autopilotCore.phraseWaitS(d._currentPosition(), entryPos, phraseSong) / r;
      if (wait < minLead) wait += phraseSong / r;
      return now + wait;
    };
    const glideHome = (T, bars) => {
      if (!alive()) return;
      const dur = bars * 240 / bpm / Math.max(0.5, d._playbackRate());
      const end = d.rampPitchPercent(0, dur, T);
      followPitch(deckId, end + 0.1);
      later((end - audioCtx.currentTime) * 1000 + 200, () => { if (alive()) say(`native ${Math.round(bpm)} BPM`); });
    };
    // Key-locked stems -> pitched native set on line T (key steps by the lock
    // ratio there), then the glide takes the key back to native with the tempo.
    const dropThenGlide = (T, bars) => {
      if (!alive()) return;
      d.swapTempoStemsAt(null, d._pitchPercent, T);
      glideHome(T + 0.02, bars);
    };
    if (plan.path === "glide") { glideHome(nextLine(), EASE_BARS); return; }
    if (plan.path === "drop") { dropThenGlide(nextLine(), EASE_BARS); return; }
    const masked = () => {
      if (!alive()) return;
      const pos = d._currentPosition(), a = d.analysis || {};
      const limit = d.buffer ? d.buffer.duration - 60 : pos + 120;
      const spot = autopilotCore.maskedDropAt(pos + 0.5, entryPos, phraseSong, limit, a.sections, a.vocal_active_regions);
      const bars = autopilotCore.maskedGlideBars(d._pitchPercent);
      const T = spot ? audioCtx.currentTime + (spot.at - pos) / d._playbackRate() : nextLine();
      say(`drop to the pitched mix ${spot ? `in ${spot.why}` : "on the next line (nowhere quieter)"}, ${bars}-bar glide`);
      later((T - audioCtx.currentTime) * 1000 - 400, () => dropThenGlide(Math.max(T, audioCtx.currentTime + 0.2), bars));
    };
    if (plan.path === "masked") { masked(); return; }
    // ladder: render the next step while the current one plays, swap it in on
    // the first phrase line after it is decoded AND the current step has had
    // its phrase; any failed render -> the masked drop (still ends native).
    (async () => {
      let lastAt = -Infinity;
      for (const pct of plan.steps) {
        const target = bpm * (1 + pct / 100);
        const bufs = pct === 0 ? null : await d.fetchTempoStems(target, 90);
        if (!alive()) return;
        if (pct !== 0 && !bufs) { say("a step render failed, masked drop instead"); masked(); return; }
        const stepPct = bufs ? (bufs.bpm / bpm - 1) * 100 : 0;   // the rendered tempo exactly
        // first line after the render that also gives the current step its phrase
        const phraseReal = 8 * 240 / (bpm * d._playbackRate());
        const Tline = nextLine(Math.max(0.6, lastAt + phraseReal - 0.05 - audioCtx.currentTime));
        await new Promise((r) => later((Tline - audioCtx.currentTime) * 1000 - 400, r));
        if (!alive()) return;
        d.swapTempoStemsAt(bufs, stepPct, Tline);
        lastAt = Tline;
        showPitch(deckId, stepPct);
        console.info(`tempo home ${deckId.toUpperCase()}: step to ${bufs ? bufs.bpm.toFixed(1) : bpm.toFixed(1)} BPM (${stepPct.toFixed(1)}%)${bufs ? ", key-locked" : ", native stems"}`);
      }
      later((lastAt - audioCtx.currentTime) * 1000 + 200, () => { if (alive()) say(`native ${Math.round(bpm)} BPM`); });
    })().catch((e) => { console.warn("tempo home ladder:", e.message); masked(); });
  }

  async function requestMindPlan(currentId, nextId, candidate) {
    if (!window.djMind || !window.djMind.requestPlan) return null;
    const win = exitWindow(candidate.score || 50);
    if (!(win.hi > win.lo)) return null;
    // Never wait past the point where the transition must be booked.
    const pos = deckPosition(activeDeck);
    apStatus("AI planning the next transition…");
    return window.djMind.requestPlan(currentId, nextId, candidate, Object.assign(win, {
      setPosition: Math.min(history.length / 10, 1.0),
      mashupPossible: mashupsOn() && !!window.mashup,
      deadlineS: Math.max(win.lo, win.hi - 30) - pos - 20,
    }));
  }

  async function tryLibraryLockable(currentId, gen) {
    const d = window.decks && window.decks[activeDeck];
    if (!d || !d.bpm) return false;
    const aEff = d.bpm * d._playbackRate();
    const key = d.analysis && d.analysis.key && d.analysis.key.camelot || "";
    const exclude = [...playedIds, currentId].join(",");
    try {
      // genre: the server only offers library songs known to share the playing
      // song's scene (tempo + key alone paired Barbie Girl with Bicep "Glue")
      const res = await fetch(`/api/library/lockable?bpm=${aEff.toFixed(2)}&key=${encodeURIComponent(key)}&exclude=${encodeURIComponent(exclude)}&max_gap=${lockLimit()}&genre=${encodeURIComponent(currentGenre || "")}&era=${encodeURIComponent(currentEra || "")}`);
      const lib = (await res.json()).tracks || [];
      for (const t of lib) {
        if (!active || gen !== prepGen) return false;
        if (history.includes(t.name)) continue;
        apStatus(`Library pick that locks to ${Math.round(aEff)} BPM: ${t.name}`);
        const c = { track_id: t.track_id, name: t.name, bpm: t.bpm, duration: t.duration, keep: false, fromLibrary: true };
        if (await tryCandidate(currentId, c, gen)) return true;
      }
    } catch (e) { console.warn("library fallback:", e.message); }
    return false;
  }

  async function tryCandidate(currentId, cand, gen) {
    try { return await evaluateCandidate(currentId, cand, gen); }
    catch (e) {
      console.warn("Autopilot candidate failed:", cand && cand.name, e.message);
      apStatus(`Skipping ${cand && cand.name}: ${e.message}`);
      return false;
    }
  }

  // ── core loop ─────────────────────────────────────────────────────────────
  let prepGen = 0;        // bumps on every (re)started next-song search
  let prepStartedAt = 0;  // ms timestamp of the current search
  async function prepareTransition(currentId) {
    if (!active) return;
    const gen = ++prepGen;
    prepStartedAt = Date.now();
    showQueue();

    // 0) LEAD TO destination is due: book it (beat-matched when the tempo
    // locks; otherwise the tempo-jump fallback, it's where the user asked to go).
    if (leadDue()) {
      apStatus(`LEAD TO: bringing in ${leadTo.cand.name}`);
      allowTempoJump = true;
      const c = Object.assign({}, leadTo.cand, { keep: false });
      if (await tryCandidate(currentId, c, gen)) { leadTo.arrived = true; return; }
      if (!active || gen !== prepGen) return;
      leadStatus(`couldn't book ${leadTo.cand.name} yet — one more steering song`);
      allowTempoJump = false;
    }

    // 1) Songs already pre-downloaded in an earlier round: no waiting.
    // Pairwise rejects go back to the END of the pool (tried once per song).
    allowTempoJump = false;
    forceJump = false;
    const pool = ready.splice(0, ready.length);
    // BRIDGE: songs nearest the ladder's next step first
    const step = bridgeTarget(false);
    if (step) pool.sort((x, y) => (x.bpm ? Math.abs(Math.log(pulseNear(x.bpm, step) / step)) : 9) -
                                  (y.bpm ? Math.abs(Math.log(pulseNear(y.bpm, step) / step)) : 9));
    for (let i = 0; i < pool.length; i++) {
      if (!active || gen !== prepGen) return;
      const c = pool[i];
      apStatus(`Trying earlier suggestion: ${c.name} (${pool.length - i} in pool)`);
      c.keep = false;
      if (await tryCandidate(currentId, c, gen)) {
        pool.slice(i + 1).forEach(addReady); // untried ones stay for later
        return;
      }
      if (c.keep) addReady(c);
    }

    // 2) Fresh AI suggestions, all downloading in parallel. Retry with new
    // suggestions when every candidate fails; rejected titles are fed back as
    // "avoid" so the model proposes different songs.
    const MAX_ROUNDS = 3;
    const rejected = [];
    for (let round = 1; round <= MAX_ROUNDS; round++) {
      // A running BRIDGE PATH holds the budget back; the last round always may jump.
      forceJump = round === MAX_ROUNDS;
      allowTempoJump = forceJump || (!bridge && tempoJumpBudget());
      // Nothing beat-matchable after a strict round: don't burn more AI rounds
      // hunting for a tempo that may barely exist (a 96 BPM dembow seed has
      // almost no house / UK dance peers). Take the best song already waiting
      // (the AI's own first picks) with a tempo-jump transition instead.
      // Before any forced tempo jump: songs already in the library whose REAL
      // tempo locks (the whole set as one song, user). Same vibe gate as the rest.
      if (round === MAX_ROUNDS && await tryLibraryLockable(currentId, gen)) return;
      if (round === MAX_ROUNDS && ready.length) {
        allowTempoJump = true;
        const waiting = ready.splice(0, ready.length);
        for (let i = 0; i < waiting.length; i++) {
          if (!active || gen !== prepGen) return;
          const c = waiting[i];
          apStatus(`No beat-matchable pick — tempo-jump to ${c.name} (Echo Out / breakdown)`);
          c.keep = false;
          if (await tryCandidate(currentId, c, gen)) {
            waiting.slice(i + 1).forEach(addReady);
            return;
          }
          if (c.keep) addReady(c);
        }
        allowTempoJump = false;
      }
      if (!active || gen !== prepGen) return;
      apStatus(round > 1 ? `⏳ Retrying with new suggestions (${round}/${MAX_ROUNDS})…`
                         : "⏳ AI selecting next songs…");
      let suggestions;
      aiPicking = true;
      showQueue();
      try {
        suggestions = await getSuggestions(currentId, rejected);
        aiPicking = false;
      } catch (e) {
        aiPicking = false;
        console.warn("Autopilot suggest failed:", e.message);
        apStatus(`Suggest error: ${e.message}`);
        continue;
      }
      pendingSugs = suggestions;
      showQueue();
      if (!suggestions.length) continue;
      apStatus(`⬇ Pre-downloading ${suggestions.length} songs…`);

      const jobs = suggestions.map((s) => downloadSuggestion(s).catch((e) => {
        rejected.push(`${s.artist} - ${s.title}`);
        console.warn("Download failed:", s.title, e.message);
        return null;
      }));
      // Highest-ranked suggestion first; the rest keep downloading meanwhile.
      for (let i = 0; i < jobs.length; i++) {
        const c = await jobs[i];
        if (!active || gen !== prepGen) return;
        if (!c) continue;
        if (await tryCandidate(currentId, c, gen)) {
          // Leftovers finish in the background and wait for later transitions.
          jobs.slice(i + 1).forEach((p) => p.then(addReady));
          return;
        }
        if (c.keep) addReady(c); // fit problem with THIS song only: try again next time
        rejected.push(`${c.suggestion.artist} - ${c.suggestion.title}`);
      }
    }
    // Never end the set over this: the playing song keeps going (HOLD LOOP near
    // its end) and the search retries. Stopping here turned a 10 s server
    // restart into a dead set.
    apStatus(`No next song yet after ${MAX_ROUNDS} tries — retrying in 20 s (music keeps playing)`);
    setTimeout(() => { if (active && gen === prepGen) prepareTransition(currentId); }, 20000);
  }

  function scheduleTransition(currentId, nextId, nextName, candidate, blend = null, minExit = null, layer = null) {
    // Tempo gap 2-15 %: render B's stems key-locked at A's tempo now, while A plays
    // (multi-BPM stem sets, cached on the server), so the blend keeps B's key.
    {
      const oa0 = window.decks && window.decks[activeDeck], sd0 = window.decks && window.decks[stagingDeck()];
      if (oa0 && sd0 && oa0.bpm > 0 && sd0.bpm > 0 && sd0.useTempoStems) {
        const aEff0 = oa0.bpm * oa0._playbackRate();
        const m0 = [1, 2, 0.5].reduce((b, m) => (Math.abs(aEff0 / (sd0.bpm * m) - 1) < Math.abs(aEff0 / (sd0.bpm * b) - 1) ? m : b));
        const gap0 = Math.abs(aEff0 / (sd0.bpm * m0) - 1);
        if (gap0 > 0.02 && gap0 <= 0.15) {
          sd0.useTempoStems(aEff0 / m0).then((ok) => ok && window.dispatchEvent(new CustomEvent("ai-activity", { detail: {
            kind: "stem-move", deck: stagingDeck(), label: `TEMPO STEMS · ${(aEff0 / m0).toFixed(1)} BPM`,
            why: `${nextName}: stems key-locked ${(gap0 * 100).toFixed(1)} % to this tempo, no pitch shift` } })));
        }
      }
    }
    if (!active) return;
    let bTime = candidate.b_time || 0;
    let vocalShort = false, vocalCut = "";
    let recipe = candidate.recipe || "Blend";
    // Stems on both decks: the set plays as ONE song (user). Every transition is
    // a long stem blend: one owner per layer, one singer, kick + bass swapped on
    // a line. No vocal-driven shortening, no cuts or spinbacks (those were only
    // there to stop two vocals or two beats clashing, which stems already solve).
    const odS = window.decks && window.decks[activeDeck], sdS = window.decks && window.decks[stagingDeck()];
    let vocalRule = false;
    // A stems-readiness blip (a dead source, a set swap in flight) must not
    // decide the recipe: re-arm A's decoded stems first (Open Eye Signal ->
    // Delilah was a Quick Cut because A reported "no stems").
    if (odS && odS.stems && !odS.stemsReady && odS.rearmStems) odS.rearmStems("transition planning");
    const aStemsWhy = stemsWhy(odS), aStems = !aStemsWhy, bStems = !!(sdS && sdS.stems);
    const stemsBoth = aStems && bStems;
    const aEffS = odS ? odS.bpm * odS._playbackRate() : 0;
    const gapS = sdS && sdS.bpm ? Math.min(...[1, 2, 0.5].map((m) => Math.abs(aEffS / (sdS.bpm * m) - 1))) : 1;
    const oneSong = stemsBoth && gapS <= 0.25;
    const od0bpm = (window.decks && window.decks[activeDeck] && window.decks[activeDeck].bpm) || 128;
    // Beat-to-beat: when the tempos lock, hand beat to beat. Echo-outs are for
    // tempo gaps; they turned "vocal -> beat" when used between compatible songs.
    if (blend) {
      bTime = blend.entry;
      // A's vocal riding over B's instrumental intro is a classic long blend;
      // only two vocals at once clash, so keep that overlap short (bass swap).
      const bClean = blend.b_vocal_coverage == null || blend.b_vocal_coverage <= 0.15;
      const k = recipeKind(recipe);
      if (!bClean) recipe = "Bass Swap";
      else if (!["bass", "blend", "default"].includes(k)) recipe = "Long Blend";
      blend.clean = bClean;
      // Two vocals must never sing together: the overlap has to END before B's
      // vocal first comes in (user: "vocals are overlapping"). Pick the
      // transition length by how many bars that is.
      if (oneSong) { recipe = "Long Blend"; blend.clean = true; }
      // stems on either deck: one singer by muting a vocal stem, never a cut
      const vr = autopilotCore.vocalRecipe({ vIn: blend.b_vocal_in_bars, oneSong, aStems, bStems });
      if (vr) {
        vocalRule = true;
        recipe = vr.recipe; vocalShort = vr.short; vocalCut = vr.why;
      }
    } else if (oneSong) {
      // tempo gap up to 25 %: key-locked tempo stems make it a real blend
      recipe = "Long Blend";
    } else if (stemsBoth) {
      // tempo can't lock: a stem bridge, never an echo-out (user)
      recipe = "Stem Bridge";
    } else if (!["echo", "filter"].includes(recipeKind(recipe))) {
      // No stems and no tempo lock: beats cannot be layered, so don't hard-swap.
      // Echo the outgoing song away while the new one enters on its phrase
      // ([[Echo Out]]). Rare now: every library song is pre-separated.
      recipe = "Echo Out";
    }
    // Never a hard cut (user): a cut the matcher or the AI plan proposed is a Bass Swap.
    if (/\bcut\b/i.test(String(recipe || ""))) {
      // a cut was the matcher's answer to clashing keys: keep the overlap short (4 bars)
      console.info("transition recipe:", `${recipe} -> 4-bar Bass Swap (no hard cuts)`);
      recipe = "Bass Swap";
      vocalShort = true;
    }
    // Mashup -> transition beats every other move when the pair fits (user)
    if (stemsBoth && odS && sdS && mashupFits(odS, sdS)) recipe = "Mashup → Transition";
    if (layer) { bTime = layer.entry; recipe = `LAYER ${layer.hold_bars}+${layer.unwind_bars} bars`; }
    jumpPending = !blend && !oneSong;
    // why an echo / non-stem recipe: on the status line and in the console,
    // so the next time a transition sounds like a cut the reason is visible
    if (!layer && !stemsBoth) {
      const why = `${recipe}: A stems ${aStemsWhy || "live"}, B stems ${bStems ? "loaded" : "not loaded"}` +
        `${vocalCut ? `, ${vocalCut}` : ""}${blend ? "" : `, tempo gap ${(gapS * 100).toFixed(1)}%`}`;
      console.info("transition recipe:", why); window.aiStep && window.aiStep("recipe", { deck: activeDeck, decision: recipe, why });
      apStatus(why);
    } else if (vocalCut) console.info("transition recipe:", `${recipe}: ${vocalCut}`);
    const overlapStyle = layer ? "layer"
      : blend ? (candidate.overlap_style || "standard")
              : "standard"; // never "instant" across a tempo gap
    const score  = candidate.score  || 50;

    // Play-time window from the set mode, counted from when this song came in.
    // Prefer the matcher's phrase-aligned exit if it falls inside the window.
    const w = playWindow(score);
    const nowPos = deckPosition(activeDeck);
    const od = window.decks && window.decks[activeDeck];
    const trackEnd = (od && od.buffer ? od.buffer.duration : Infinity) - w.xf - 2;
    const lo = Math.min(entryPos + w.min, trackEnd);
    const hi = Math.min(entryPos + w.max, trackEnd);
    let exitAt = layer ? layer.start : blend ? blend.exit : candidate.a_time;
    if (!blend && !(exitAt >= lo && exitAt <= hi)) exitAt = Math.max(lo, Math.min(hi, exitAt || hi));
    // never leave before the playing song's first drop has played (server floor)
    if (!blend && minExit != null && exitAt < minExit && minExit < trackEnd) exitAt = minExit;
    // PEAK mode (dj-mind.js peakTransition): tempo-locked pairs only, land B's
    // drop on A's drop downbeat - Double Drop or Drop Swap. null -> blend.
    const peakT = !layer && blend && blend.drop && window.djMind && window.djMind.planPeak
      ? window.djMind.planPeak({ drop: blend.drop, lo: Math.max(lo, nowPos + 15), hi,
                                 plannedExit: exitAt, entryPos, inDeck: stagingDeck() })
      : null;
    if (peakT) { recipe = peakT.recipe; bTime = peakT.bTime; exitAt = peakT.exitAt; }
    // Keep the exit on A's phrase grid: push by whole phrases, never by seconds.
    const phraseS = 32 * 60 / od0bpm;
    let effectiveATime = exitAt;
    while (effectiveATime < nowPos + 15) effectiveATime += phraseS;
    // vocalShort: xfDuration < 16 halves every bar count in executeTransition
    // (Bass Swap 8 -> 4 bars) so the overlap ends before B's vocal.
    const xfDuration = oneSong ? Math.max(16, w.xf) : vocalShort ? Math.min(8, w.xf) : peakT ? Math.max(16, w.xf) : w.xf;
    playPlanTag = ` | ${w.label} ${fmtTime(effectiveATime - entryPos)}`;
    // Pre-planned by the silent ear: B starts where the ear chose (inside A), from
    // the line it chose, the merge it heard. Only when the plan is still ahead.
    const pp = candidate.preplan;
    let preplanned = false;
    if (pp && !layer && !peakT && stemsBoth && odS && sdS && pp.a_in >= nowPos + 15) {
      effectiveATime = pp.a_in;
      bTime = pp.b_start;
      recipe = "Stem Merge";
      preplanned = true;
      sdS._mergePlan = { entry: pp.b_start, M: pp.bars, aT: pp.a_in, heard: !!pp.ear, preplanned: true,
        pick: { combo: pp.combo, label: pp.label, reasons: pp.why || [], ear: pp.ear || null } };
      playPlanTag = ` | ${w.label} ${fmtTime(effectiveATime - entryPos)} · ear plan`;
      console.info("transition recipe:", `Stem Merge (pre-planned): ${pp.direction}, B from ${fmtTime(pp.b_start)} at A ${fmtTime(pp.a_in)}, ` +
        `${pp.bars} bars, ${pp.label}${pp.ear ? `, ear ${pp.ear.score}/10` : ""}`);
    }
    // Never transition out of A while it's at its energy high: push the exit past it
    // by whole phrases (not for PEAK / LAYER / pre-planned: they chose their line).
    if (!peakT && !layer && !preplanned && od && od.analysis) {
      const spans = autopilotCore.highSpans(od.analysis.energy_times, od.analysis.energy_curve, 240 / od0bpm);
      const ex = autopilotCore.exitPastHigh(effectiveATime, 16 * 240 / od0bpm, spans, phraseS, trackEnd);
      if (ex.moved) {
        console.info("transition timing:", `exit moved ${ex.moved} phrase(s) to ${fmtTime(ex.t)}: A is at its energy high`); window.aiStep && window.aiStep("exit_moved", { deck: activeDeck, decision: `exit +${ex.moved} phrase(s)`, why: "A is at its energy high", result: { from: effectiveATime, to: ex.t } });
        effectiveATime = ex.t;
      }
    }
    // Song merge beats the plain mashup (it is its generalization) when a combo fits;
    // never over LAYER / PEAK. The mashup stays the fallback if the merge is refused.
    if (!preplanned && !layer && !peakT && mergesOn() && stemsBoth && odS && sdS) {
      const mp = planMerge(currentId, nextId, odS, sdS, effectiveATime);
      if (mp) {
        recipe = "Stem Merge";
        console.info("transition recipe:", `Stem Merge: ${mp.pick.label} (${mp.pick.reasons.join(", ")}), ${mp.M} bars`);
      }
    }

    let executed = false;
    let filled = false;
    let fireAt = effectiveATime; // B's entry lands exactly on this phrase line

    // Hand the plan to the DJ mind: it may pre-clear the outgoing bass or hold
    // the exit one phrase longer (bounded by the set-mode window + 16 bars).
    const barS = 240 / ((od && od.bpm) || 128);
    if (window.djMind) {
      window.djMind.setPlan({
        fireAt,
        // a vocal-free blend window is exact: the mind must not hold past it
        // vocal-aware plans are exact: a DJ-mind hold would move the overlap into a vocal
        maxFireAt: peakT || layer || preplanned || (blend && (blend.instrumental || blend.vocals_known)) ? fireAt
          : Math.max(fireAt, Math.min(trackEnd, hi + 16 * barS)),
        style: peakT ? "peak" : overlapStyle,
        peakKind: peakT ? peakT.kind : null, peakWhy: peakT ? peakT.why : null, brake: !!(peakT && peakT.brake),
        preClearBars: Number.isFinite(candidate.pre_clear_bars) ? candidate.pre_clear_bars : 8,
      });
    }

    // Learned from studied sets (app/music_brain/set_learner.py): the move that DJ
    // made most on pairs like this one, when the console already allows it here.
    let learned = null;
    if (!layer && !peakT && learnedOn()) {
      const facts = { layer, peak: peakT, blend, oneSong, stemsBoth, vocalRule, recipe,
                      mashupFits: !!(stemsBoth && odS && sdS && mashupFits(odS, sdS)) };
      fetch(`/api/learned/pick?a=${encodeURIComponent(currentId)}&b=${encodeURIComponent(nextId)}&keylock=${!!(sdS && sdS.useTempoStems)}`)
        .then((res) => (res.ok ? res.json() : null))
        .then((r) => {
          const pick = r && r.pick;
          const ch = autopilotCore.learnedRecipe(pick, { ...facts, riff: !!riff, recipe });
          if (!ch || executed || !active || currentTrackId !== currentId) return;
          learned = pick;
          recipe = ch.recipe;
          console.info("transition recipe (learned):", `${ch.recipe}: ${ch.why}`, pick.reasons);
          window.dispatchEvent(new CustomEvent("ai-activity", { detail: {
            kind: "learned", deck: activeDeck, label: `LEARNED · ${ch.recipe}`, why: ch.why } }));
        })
        .catch((e) => console.info("learned pick: none -", e.message));
    }

    // Riff over rap (riff-over-rap.js): when the pair fits (3-15 % tempo gap,
    // A has a groove running into its own breakdown, B raps) and A's
    // key-locked stems render in time, the fire line moves to A's groove start
    // and the whole 64-bar move replaces the recipe.
    let riff = null, riffEntry = null;
    if (!layer && !peakT && riffOn() && window.riffOverRap) {
      const notBefore = deckPosition(activeDeck) + 25;
      window.riffOverRap.prepare(currentId, nextId, notBefore).then((r) => {
        // why no riff: console for detail, the status line for a glance
        const riffNo = (why) => {
          console.info("riff over rap: no -", why);
          if (!executed && active && currentTrackId === currentId) apStatus(`Riff over rap: no - ${why}`);
        };
        if (!r.ok) { riffNo((r.reasons || []).join("; ") || "no plan"); return; }
        if (executed || !active || currentTrackId !== currentId) return;
        const g0 = r.plan.a_groove[0], pos = deckPosition(activeDeck);
        const sdB = window.decks && window.decks[stagingDeck()];
        if (g0 < pos + 6 || g0 > hi + 60 || !(sdB && sdB.stems)) {
          riffNo(g0 < pos + 6 ? "A is past its groove" : g0 > hi + 60 ? "the groove comes too late" : "B's stems aren't loaded");
          return;
        }
        riff = r;
        fireAt = g0;
        recipe = "RIFF OVER RAP";
        if (window.djMind) window.djMind.setPlan({ fireAt, maxFireAt: fireAt, style: "layer", preClearBars: 0 });
      }).catch((e) => console.warn("riff over rap:", e.message));
    }

    const tick = setInterval(() => {
      if (!active) { clearInterval(tick); return; }
      if (window.djMind) fireAt = window.djMind.fireAt(fireAt);
      const pos = deckPosition(activeDeck);
      const left = fireAt - pos;

      // Live drums: 2-bar fill leading into the crossfade (glues the records),
      // rationed by the mind (study rule 8: FX stay the exception).
      const od0 = window.decks && window.decks[activeDeck];
      const barSecs = (60 / ((od0 && od0.bpm) || 128)) * 4;
      if (!filled && !layer && left > 0 && left <= 2 * barSecs && window.beatLayer) {
        filled = true;
        if (!window.djMind || window.djMind.fxAllowed("fill")) window.beatLayer.fill(2);
      }

      if (left > 0.8) {
        const scoreTag = score >= 65 ? `⭐${score}` : `⚡${score} (early exit)`;
        const bl = layer ? ` · ${layer.source} LAYER, bass to B at bar ${layer.hold_bars}${layer.third ? " + 3rd vocal" : ""}`
          : blend ? ` · beat blend ${blend.pitch_percent >= 0 ? "+" : ""}${blend.pitch_percent.toFixed(1)}%${blend.clean ? "" : " (short: both vocal)"}` : "";
        playPlanTag = ` | ${w.label} ${fmtTime(fireAt - entryPos)}${bl}`;
        apStatus(`Next: ${nextName} | ${recipe} | ${scoreTag}${playPlanTag} | in ${left.toFixed(0)}s${mashupTag}`);
        return;
      }
      if (executed) return;
      executed = true;
      clearInterval(tick);
      if (window.djMind) window.djMind.onTransition();

      if (window.mashup) window.mashup.cancel();
      mashupTag = "";
      apStatus(`Blending → ${nextName} (${recipe})…`);

      if (riff) {
        const outgoing = activeDeck, incoming = stagingDeck();
        const oaR = window.decks[outgoing];
        const leadR = Math.max(0.05, (fireAt - deckPosition(outgoing)) / oaR._playbackRate());
        const t0R = audioCtx.currentTime + leadR;
        const ui = {
          xf: (inn, f) => setRange(xfader, (inn === "b" ? 1 : -1) * f),
          eq: (d, band, v) => setRange(eqEl(d, band), v),
          pitch: (d, pct) => setDeckPitch(d, pct),
        };
        dipAllowed("riffRelease", "RIFF OVER RAP");
        const totalMs = window.riffOverRap.run(riff, outgoing, incoming, t0R, ui);
        if (window.djMind && window.djMind.layering) {
          window.djMind.layering(totalMs / 1000, { source: "RIFF",
            why: `riff over rap: A's groove key-locked to ${riff.plan.target_bpm} BPM, B's rap on bar 40` });
        }
        riffEntry = riff.plan.b_entry;
        later(totalMs + 500, afterBlend);
        return;
      }

      // Tempo-lock B to A, then start it sample-accurately so B's entry
      // downbeat lands exactly on A's phrase line.
      const sd = window.decks && window.decks[stagingDeck()];
      const oa = window.decks && window.decks[activeDeck];
      const rateA = oa ? oa._playbackRate() : 1;
      if (sd && oa && oa.bpm > 0 && sd.bpm > 0) {
        // Live A tempo (A may still be easing back from its own tempo lock);
        // half/double time counts as a match.
        const aEff = oa.bpm * rateA;
        const lockRate = [1, 2, 0.5].map((m) => aEff / (sd.bpm * m))
          .reduce((best, r) => (Math.abs(r - 1) < Math.abs(best - 1) ? r : best));
        // Key-locked tempo stems (prefetched below): locks up to 15 % keep B's key.
        const keyLocked = sd.tempoStems && Math.abs(sd.tempoStems.bpm / (sd.bpm * lockRate) - 1) < 0.01;
        if (keyLocked) setDeckPitch(stagingDeck(), (lockRate - 1) * 100, 26);
        else if (Math.abs(lockRate - 1) <= 0.08) setDeckPitch(stagingDeck(), (lockRate - 1) * 100);
      }
      const leadS = Math.max(0.05, (fireAt - deckPosition(activeDeck)) / rateA);
      const t0 = audioCtx.currentTime + leadS;
      if (sd) sd.play(bTime, false, t0);
      // LAYER: B "arrives" at the bass hand-off; its play window counts from there
      const nextEntry = layer ? layer.b_swap : bTime;
      riffEntry = null;
      // Drop Swap + [[Backspin (Spinback)]]: A's build winds down (deck brake,
      // 0.8 s) into the downbeat where B's drop cuts in.
      if (peakT && peakT.brake && oa && typeof oa.brake === "function") {
        dipAllowed("brake", recipe);
        later(Math.max(0, leadS - 0.8) * 1000, () => oa.brake());
      }

      // Recipe-aware EQ-first transition, started on the same downbeat.
      const outgoing = activeDeck;
      const incoming = stagingDeck();
      // LAYER: the beat layer (live drums on A) and the mind's phrase moves pause
      // while two records ride, so no third drum line doubles up.
      // Both paths start XF_LOOKAHEAD_MS early and schedule their automation
      // on the audio clock at exactly t0 (B's first downbeat).
      later(Math.max(0, leadS * 1000 - XF_LOOKAHEAD_MS), () => {
        let totalMs;
        if (layer) {
          if (window.beatLayer && window.beatLayer.isEnabled()) {
            window.beatLayer.setEnabled(false);
            beatMutedByLayer = true;
          }
          totalMs = executeLayer(outgoing, incoming, layer, t0) + XF_LOOKAHEAD_MS;
          // NULL-BOT supermove (mascot.js): the LAYER starts on B's first downbeat
          window.dispatchEvent(new CustomEvent("ai-supermove", { detail: { at: t0, name: "LAYER", deck: incoming } }));
          if (window.djMind && window.djMind.layering) {
            window.djMind.layering(totalMs / 1000, { source: layer.source,
              why: `${layer.why} - ${layer.hold_bars} bars together, bass to B on the line, A unwinds ${layer.unwind_bars} bars` });
          }
        } else {
          totalMs = executeTransition(recipe, outgoing, incoming, xfDuration, t0) + XF_LOOKAHEAD_MS;
          sessionEvent("track", { event: "transition_start", from: history[history.length - 1] || null, to: nextName, recipe,
                                  out: outgoing, in: incoming, seconds: Math.round(totalMs / 100) / 10 });
          window.dispatchEvent(new CustomEvent("ai-cue", { detail: { at: t0, kind: "transition",
            deck: incoming, bar: 240 / ((window.decks[incoming] && window.decks[incoming].bpm) || 128), why: `${recipe}: B's first downbeat` } }));
        }
        later(totalMs + 500, afterBlend);
      });

      // After the transition completes, update state and continue
      function afterBlend() {   // declaration: hoisted, the riff branch above calls it
        endAudioClock(); // controls are the user's again
        if (!active) return;

        // Stop the outgoing deck and put it back to neutral for its next load
        const od = window.decks && window.decks[outgoing];
        if (od) od.stopNow();
        if (window.stemMoves) window.stemMoves.reset(od);   // never leave its mix muted
        resetDeck(outgoing);

        history.push(nextName);
        sessionEvent("track", { event: "transition_end", now_playing: nextName, deck: incoming, set_songs: history.length });
        if (window.liveEar && window.liveEar.flush) window.liveEar.flush("transition done");
        advanceLead();
        playedIds.push(nextId);
        unmuteBeatLayer();
        genreLog.push(currentGenre || "");
        currentGenre = (scheduledNext && scheduledNext.suggestion && scheduledNext.suggestion.genre) || "";
        currentEra = (scheduledNext && scheduledNext.suggestion && scheduledNext.suggestion.era) || currentEra;
        songsSinceJump = jumpPending ? 0 : songsSinceJump + 1;
        jumpPending = false;
        if (steering === "move") steerStep++;
        scheduledNext = null;
        activeDeck = stagingDeck();
        currentTrackId = nextId;
        entryPos = riffEntry != null ? riffEntry : nextEntry;
        currentEnergy = null;
        if (window.beatLayer) window.beatLayer.follow(activeDeck);
        if (window.djMind) {
          window.djMind.follow(activeDeck);
          window.djMind.setProfileEnergy(profileById[currentTrackId]);
        }
        easePitchHome(activeDeck);
        advanceBridge();

        // Park crossfader fully on the new active deck side
        setRange(xfader, activeDeck === "a" ? -1 : 1);

        prepareTransition(currentTrackId);
      }
    }, 200);
    runTimers.push(tick);
    return fireAt;
  }

  // ── set modes ─────────────────────────────────────────────────────────────
  // LONG   songs ride 3-6 min, long 24 s blends (Fred's layered, patient mode)
  // QUICK  1-2 min, 8 s blends, high energy (weak match bails at 1 min)
  // HYBRID per song: weak match or high energy (>= 7/10) -> quick,
  //        deep / low energy (<= 5/10) -> long, else in between
  let playPlanTag = "";
  function setMode() {
    const el = document.getElementById("ap-mode");
    const v = el ? el.value : "hybrid";
    return ["long", "quick", "hybrid"].includes(v) ? v : "hybrid";
  }

  const WINDOWS = {
    long:   { min: 180, max: 360, xf: 24, label: "LONG" },
    medium: { min: 120, max: 240, xf: 16, label: "MID" },
    quick:  { min: 60,  max: 120, xf: 8,  label: "QUICK" }, // user: "1-2 min"; 45 s felt rushed
    bail:   { min: 30,  max: 60,  xf: 8,  label: "QUICK·bail" },
    // steering toward the occasion's music: short bridge songs (user: 30-60 s each)
    bridge: { min: 30,  max: 60,  xf: 8,  label: "BRIDGE" },
  };

  function playWindow(score) {
    if (steering === "move") return WINDOWS.bridge;
    // A famous song plays in full (user; the USB002 set rides leavemealone for
    // 7 min): exit only in its last ~50 s, i.e. the outro. Stem breakdowns
    // (stem-moves.js) keep it from sounding long.
    const pd = window.decks && window.decks[activeDeck];
    if (pd && pd.fame && pd.fame.famous && pd.buffer) {
      const rem = pd.buffer.duration - (entryPos || 0);
      if (rem > 90) return { min: Math.max(60, rem - 50), max: Math.max(70, rem - 6), xf: 24, label: "FULL·famous" };
    }
    const mode = setMode();
    const weak = score < 65;
    if (mode === "long") return WINDOWS.long;
    if (mode === "quick") return weak ? WINDOWS.bail : WINDOWS.quick;
    if (weak) return WINDOWS.bail;
    if (currentEnergy != null && currentEnergy >= 7) return WINDOWS.quick;
    if (currentEnergy != null && currentEnergy <= 5) return WINDOWS.long;
    return WINDOWS.medium;
  }

  // ── live mashup ("A x B") ─────────────────────────────────────────────────
  // Before the transition, lay the NEXT track's vocal over one instrumental
  // 8/16-bar phrase of the current track (the Fred again.. "x" move: tease the
  // next record's voice over this beat, then bring the record itself in).
  // Restraint: at most one layer per track; skipped unless key and tempo fit.
  // One line in this session's event log (app/ui/session_log.py). Fire and forget.
  function sessionEvent(kind, data) {
    try {
      fetch("/api/session/event", { method: "POST", headers: { "Content-Type": "application/json" }, keepalive: true,
        body: JSON.stringify({ kind, data }) }).catch(() => {});
    } catch (e) { /* logging never breaks the set */ }
  }
  window.addEventListener("ear-flush", (e) => sessionEvent("ear_flush", e.detail));
  // every AI move (stem moves, remix, merges, hook drops, learned moves) with the deck
  // position, so a move that "killed the vibe" can be found in the session log
  window.addEventListener("ai-activity", (e) => {
    const d = e.detail || {}, dk = d.deck && window.decks && window.decks[d.deck];
    sessionEvent("move", { move: d.kind || "", label: d.label || "", why: d.why || "", deck: d.deck || null,
      song: history[history.length - 1] || null, pos: dk && dk._currentPosition ? Math.round(dk._currentPosition() * 10) / 10 : null });
  });

  // POST /api/transition/preplan (app/music_brain/preplan.py) for the booked pair:
  // the exit window of this song, now, and A's live tempo. null when stems are
  // missing, B can't sit on A's tempo, the toggle is off, or nothing fits.
  async function requestPreplan(currentId, nextId, candidate) {
    const od = window.decks && window.decks[activeDeck], sd = window.decks && window.decks[stagingDeck()];
    if (!mergesOn() || !od || !sd || !od.stemsReady || !sd.stems || !od.bpm || !sd.bpm) return null;
    const aEff = od.bpm * od._playbackRate(), gap = Math.abs(aEff / sd.bpm - 1);
    if (gap > 0.25 || (gap > 0.02 && !(sd.tempoStems && Math.abs(sd.tempoStems.bpm / aEff - 1) < 0.01))) return null;
    const w = playWindow(candidate.score || 50);
    const trackEnd = (od.buffer ? od.buffer.duration : Infinity) - w.xf - 2;
    const lo = Math.min(entryPos + w.min, trackEnd), hi = Math.min(entryPos + w.max, trackEnd);
    try {
      const r = await fetch("/api/transition/preplan", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ a_id: currentId, b_id: nextId, lo, hi, now: deckPosition(activeDeck), bpm_a: aEff }) });
      // the server answers {status: "pending", job} at once and renders + asks the ear
      // in the background; poll it inside the same PREPLAN_WAIT_MS budget as before
      const d = await autopilotCore.awaitJob(r.ok ? await r.json() : null, async (job) => {
        const g = await fetch(`/api/transition/preplan/${encodeURIComponent(job)}?now=${deckPosition(activeDeck)}`);
        return g.ok ? g.json() : null;
      }, { budgetMs: PREPLAN_WAIT_MS, alive: () => active && currentTrackId === currentId });
      return d && d.ok && d.plan ? d.plan : null;
    } catch (e) { return null; }
  }

  const PREPLAN_WAIT_MS = 35000;

  function mergesOn() {
    if (window.djSession && window.djSession.relaxed) return false;
    const t = document.getElementById("ap-merge-toggle");
    return !t || t.checked;
  }

  // Moves learned from studied sets: on unless the (optional) toggle is off.
  function learnedOn() {
    const t = document.getElementById("ap-learned-toggle");
    return !t || t.checked;
  }

  function riffOn() {
    if (window.djSession && window.djSession.relaxed) return false;
    const t = document.getElementById("ap-riff-toggle");
    return !t || t.checked;
  }

  function mashupsOn() {
    const t = document.getElementById("ap-mashup-toggle");
    return !t || t.checked;
  }

  function fmtTime(s) {
    const m = Math.floor(s / 60);
    return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  }

  async function tryMashup(hostId, guestId, guestName, fireAt) {
    if (!mashupsOn() || !window.mashup) return;
    const hostDeck = activeDeck;
    const d = window.decks && window.decks[hostDeck];
    if (!d) return;
    const bar = 240 / (d.bpm || 128);
    const room = fireAt - 2 * bar - (deckPosition(hostDeck) + 10);
    // 32 bars = full mashup (the guest's whole vocal phrase over this beat).
    const bars = room >= 32 * bar ? 32 : room >= 16 * bar ? 16 : room >= 8 * bar ? 8 : 0;
    if (!bars) return;
    // Live stems on the host: its own vocal can drop out under the guest's,
    // so the mashup no longer needs an instrumental stretch of this song.
    const hostMutable = !!d.stemsReady;
    try {
      const res = await fetch("/api/mashup/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ host_id: hostId, guest_id: guestId, bars, host_mutable: hostMutable }),
      });
      const plan = await res.json();
      if (!active || activeDeck !== hostDeck || currentTrackId !== hostId) return;
      if (!res.ok || !plan.ok) {
        console.info("Mashup skipped:", guestName, plan.detail || (plan.reasons || []).join("; "));
        return;
      }
      const pos = deckPosition(hostDeck);
      const entry = plan.host_entries.find((e) => e >= pos + 3 && e + plan.host_duration <= fireAt - bar);
      if (entry == null) return;
      if (await window.mashup.play(hostDeck, plan, entry)) {
        mashupTag = ` | ✕ ${guestName} vocal @${fmtTime(entry)} (${plan.bars} bars${plan.mute_host_vocals ? ", host instrumental" : ""})`;
        if (plan.mute_host_vocals && window.stemMoves) {
          // host goes instrumental for exactly the guest's phrase
          const sm = window.stemMoves;
          const onAt = sm.audioAt(d, entry), offAt = sm.audioAt(d, entry + plan.host_duration);
          setTimeout(() => d.stemMix({ vocals: 0 }, onAt, 0.05), Math.max(0, (onAt - audioCtx.currentTime) * 1000 - 200));
          setTimeout(() => d.stemMix(null, offAt, 0.2), Math.max(0, (offAt - audioCtx.currentTime) * 1000 - 200));
          window.dispatchEvent(new CustomEvent("ai-activity", { detail: { kind: "stem-move", deck: hostDeck,
            label: `FULL MASHUP · ${plan.bars} bars`, why: `${guestName} vocal over this song's instrumental` } }));
        }
        // stem remix inside the mashup: host drums + bass out for its last quarter,
        // the guest's vocal over the host's synths, everything back on the line
        if (window.stemMoves && d.stemsReady) window.stemMoves.mashupBreak(d, entry, plan.bars, !!plan.mute_host_vocals);
      }
    } catch (e) {
      console.warn("Mashup failed:", e.message);
    }
  }

  // ── start / stop ──────────────────────────────────────────────────────────
  // Session history: every song that has played audibly for >= 30 s this
  // session, autopilot or not ({id, name}, in play order). The set's
  // suggestions continue from it.
  const SESSION_MIN_S = 30;
  const session = [];
  const heard = {};                      // track id -> audible seconds
  setInterval(() => {
    for (const id of ["a", "b"]) {
      const d = window.decks && window.decks[id];
      const tid = window.state && (id === "a" ? window.state.trackA : window.state.trackB);
      if (!d || !d.playing || !tid) continue;
      const g = (d.crossfaderGain ? d.crossfaderGain.gain.value : 1) * (d.volumeGain ? d.volumeGain.gain.value : 1);
      if (g < 0.3) continue;
      heard[tid] = (heard[tid] || 0) + 1;
      if (heard[tid] === SESSION_MIN_S && !session.some((x) => x.id === tid)) {
        const el = document.getElementById(`title-${id}`);
        session.push({ id: tid, name: el ? el.textContent.trim() : tid });
        if (session.length > 60) session.shift();
      }
    }
  }, 1000);
  window.setSession = session;

  // The audible deck, else a loaded one: {deck, trackId, name, playing}
  function currentDeck() {
    let best = null;
    for (const id of ["a", "b"]) {
      const d = window.decks && window.decks[id];
      const tid = window.state && (id === "a" ? window.state.trackA : window.state.trackB);
      if (!d || !d.buffer || !tid) continue;
      const g = d.playing ? (d.crossfaderGain ? d.crossfaderGain.gain.value : 1) * (d.volumeGain ? d.volumeGain.gain.value : 1) : -1;
      const el = document.getElementById(`title-${id}`);
      const c = { deck: id, trackId: tid, name: el ? el.textContent.trim() : tid, playing: d.playing, level: g };
      if (!best || c.level > best.level) best = c;
    }
    return best;
  }

  // Common reset for any set start.
  function resetSetState() {
    setId = newSetId();
    history = [];
    steering = "stay";
    steerStep = 0;
    leadTo = null;
    leadStatus("");
    genreLog = [];
    currentGenre = "";
    currentEra = "";
    songsSinceJump = 0;
    jumpPending = false;
    bridge = null;
    scheduledNext = null;
    pendingSugs = [];
    renderQueue([]);
  }

  // Start the set from the song playing now (or loaded): no download, no
  // restart; the session so far is the set's history.
  async function startFromCurrent() {
    const cur = currentDeck();
    if (!cur) { apStatus("Nothing loaded: load or play a song, or paste a seed URL."); return; }
    occasion = occasionInput ? occasionInput.value.trim() : "";
    applySessionMood();
    resetSetState();
    active = true;
    activeDeck = cur.deck;
    updateButtons();
    currentTrackId = cur.trackId;
    const d = window.decks[cur.deck];
    if (!d.playing) d.play(d._currentPosition() || 0, true);
    if (xfader) { xfader.value = cur.deck === "a" ? "-1" : "1"; xfader.dispatchEvent(new Event("input")); }
    entryPos = Math.max(0, d._currentPosition() - (heard[cur.trackId] || 0));
    currentEnergy = null;
    if (window.beatLayer) window.beatLayer.follow(cur.deck);
    if (window.djMind) { window.djMind.reset(); window.djMind.follow(cur.deck); }
    const past = session.filter((x) => x.id !== cur.trackId);
    history = [...past.map((x) => x.name), cur.name];
    playedIds = [...past.map((x) => x.id), cur.trackId];
    setStartedAt = Date.now() - 1000 * past.reduce((sum, x) => sum + (heard[x.id] || 0), 0);
    apStatus(`▶ Set from ${cur.name}${past.length ? ` (after ${past.length} song${past.length > 1 ? "s" : ""} played)` : ""} — finding next track…`);
    startWatchdog();
    prepareTransition(currentTrackId);
  }

  async function start() {
    const url = seedInput ? seedInput.value.trim() : "";
    if (!url) return startFromCurrent();              // no seed: continue from what's playing
    occasion = occasionInput ? occasionInput.value.trim() : "";
    applySessionMood();
    // High-energy occasions run in QUICK mode unless the user picked a mode.
    const modeEl = document.getElementById("ap-mode");
    if (modeEl && modeEl.value === "hybrid" && HIGH_ENERGY_OCCASION.test(occasion)) {
      modeEl.value = "quick";
      apStatus(`"${occasion}" is a high-energy occasion → QUICK mode`);
    }
    setId = newSetId();
    history = [];
    steering = "stay";
    steerStep = 0;
    leadTo = null;
    leadStatus("");
    genreLog = [];
    currentGenre = "";
    currentEra = "";
    songsSinceJump = 0;
    jumpPending = false;
    bridge = null;
    active = true;
    activeDeck = "a";
    updateButtons();
    scheduledNext = null;
    pendingSugs = [];
    renderQueue([]);

    try {
      apStatus("Downloading seed track…");
      const tracks = await importUrl(url);
      if (!tracks.length) throw new Error("No tracks downloaded from seed URL.");

      const seed = tracks[0];
      currentTrackId = seed.track_id;
      const seedName = seed.display_name || seed.filename;

      apStatus(`Loading ${seedName} into deck A…`);
      const audioRes = await fetch(`/api/audio/tracks/${currentTrackId}`);
      if (!audioRes.ok) throw new Error(audioRes.statusText);
      const blob = await audioRes.blob();
      await loadIntoDeck("a", currentTrackId, seedName, blob);

      // Reset crossfader to A side
      if (xfader) { xfader.value = "-1"; xfader.dispatchEvent(new Event("input")); }

      // Play deck A
      const da = window.decks && window.decks.a;
      if (da) da.play(0, true);
      entryPos = 0;
      currentEnergy = null;
      if (window.beatLayer) window.beatLayer.follow("a");
      if (window.djMind) { window.djMind.reset(); window.djMind.follow("a"); }

      // the session so far steers the set too
      const past = session.filter((x) => x.id !== currentTrackId);
      history = [...past.map((x) => x.name), seedName];
      playedIds = [...past.map((x) => x.id), currentTrackId];
      setStartedAt = Date.now();
      apStatus(`▶ Playing: ${seedName} — finding next track in background…`);
      startWatchdog();
      prepareTransition(currentTrackId); // fire-and-forget: seed already playing

    } catch (e) {
      apStatus(`Autopilot error: ${e.message}`);
      active = false;
      updateButtons();
    }
  }

  // Watchdog: if no next song has been booked 150 s into a search, start a
  // fresh search (the old one is abandoned via prepGen). HOLD LOOP keeps the
  // music going meanwhile.
  const WATCHDOG_MS = 150000;
  let watchdog = null;
  function startWatchdog() {
    if (watchdog) clearInterval(watchdog);
    watchdog = setInterval(() => {
      if (!active || scheduledNext || !prepStartedAt) return;
      if (Date.now() - prepStartedAt < WATCHDOG_MS) return;
      console.warn("Autopilot watchdog: next-song search stalled, restarting it");
      apStatus("⚠ Next-song search stalled — restarting it");
      prepareTransition(currentTrackId);
    }, 10000);
  }

  function stop() {
    if (window.djSession) window.djSession.relaxed = false;   // manual play: sampler etc. back
    if (watchdog) { clearInterval(watchdog); watchdog = null; }
    active = false;
    ready.length = 0;
    cancelLead();
    scheduledNext = null;
    pendingSugs = [];
    if (window.mashup) window.mashup.cancel();
    mashupTag = "";
    clearRun();
    unmuteBeatLayer();
    if (window.beatLayer) window.beatLayer.stop();
    if (window.djMind) window.djMind.stop();
    apStatus("Autopilot stopped.");
    updateButtons();
  }

  function updateButtons() {
    if (startBtn) startBtn.disabled = active;
    const scb = document.getElementById("ap-start-current-btn");
    if (scb) scb.disabled = active;
    if (stopBtn)  stopBtn.disabled  = !active;
  }

  // Test hook: run one transition's automation on the empty decks (no audio,
  // no set) to check the audio-clock scheduling from the console.
  window.autopilotDebug = { executeTransition, xfGains };

  // Read-only view for helpers (beat-grid-ai.js).
  window.autopilotState = {
    get active() { return active; },
    get activeDeck() { return activeDeck; },
    get trackId() { return currentTrackId; },
    get genre() { return currentGenre; },
    get entryPos() { return entryPos; },
    get energy() { return currentEnergy; },
    get fireAt() { return scheduledNext ? scheduledFireAt : null; },
    get layering() { return !!(window.djMind && window.djMind.layerActive); },
  };

  const leadGo = document.getElementById("ap-lead-go");
  if (leadGo) leadGo.addEventListener("click", () => {
    // LEAD with the list open: selected row, else the first song result, else steer
    if (leadRows.length) pickLead(leadSel >= 0 ? leadSel : (leadRows.length > 1 ? 1 : 0));
    else startLead();
  });
  if (leadCancel) leadCancel.addEventListener("click", () => cancelLead("lead cancelled — the set carries on"));
  // LEAD TO search: YouTube results as you type (debounced); pick one = that
  // song is the destination; the first row steers toward the typed genre/artist.
  const leadInput = document.getElementById("ap-lead-input");
  const leadResults = document.getElementById("ap-lead-results");
  let leadSearchTimer = null, leadSearchSeq = 0, leadRows = [], leadSel = -1;
  function hideLeadResults() { if (leadResults) { leadResults.hidden = true; leadResults.innerHTML = ""; } leadRows = []; leadSel = -1; }
  function fmtDur(d) { return d ? `${Math.floor(d / 60)}:${String(Math.round(d % 60)).padStart(2, "0")}` : ""; }
  function renderLeadResults(q, results, note) {
    if (!leadResults) return;
    leadRows = [{ style: q }, ...results];
    leadResults.innerHTML = leadRows.map((r, i) => r.style
      ? `<li role="option" data-i="${i}" aria-selected="${i === leadSel}"><span class="r-title r-style">Steer toward “${esc(r.style)}” (genre / artist)</span><span class="r-meta">${esc(note || "")}</span></li>`
      : `<li role="option" data-i="${i}" aria-selected="${i === leadSel}"><span class="r-title">${esc(r.title)}</span><span class="r-meta">${esc(fmtDur(r.duration))}</span><span class="r-meta">${esc(r.channel)}</span></li>`).join("");
    leadResults.hidden = false;
  }
  function pickLead(i) {
    const r = leadRows[i];
    if (!r) return;
    if (r.style) { if (leadInput) leadInput.value = r.style; startLead(); }
    else startLead(r);
  }
  async function searchLead() {
    const q = leadInput ? leadInput.value.trim() : "";
    if (q.length < 2) { hideLeadResults(); return; }
    const seq = ++leadSearchSeq;
    renderLeadResults(q, [], "searching YouTube…");
    try {
      const res = await fetch(`/api/search/youtube?q=${encodeURIComponent(q)}&limit=8`);
      const data = await res.json();
      if (seq !== leadSearchSeq) return; // a newer query is in flight
      if (!res.ok) throw new Error(data.detail || res.statusText);
      renderLeadResults(q, data.results || [], (data.results || []).length ? "" : "no songs found");
    } catch (e) {
      if (seq === leadSearchSeq) renderLeadResults(q, [], `search failed: ${e.message}`);
    }
  }
  if (leadInput) {
    leadInput.addEventListener("input", () => { clearTimeout(leadSearchTimer); leadSearchTimer = setTimeout(searchLead, 450); });
    leadInput.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!leadRows.length) return;
        e.preventDefault();
        leadSel = (leadSel + (e.key === "ArrowDown" ? 1 : -1) + leadRows.length) % leadRows.length;
        renderLeadResults(leadRows[0].style, leadRows.slice(1));
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (leadSel >= 0) pickLead(leadSel);
        else { clearTimeout(leadSearchTimer); searchLead(); }
      } else if (e.key === "Escape") hideLeadResults();
    });
  }
  if (leadResults) leadResults.addEventListener("click", (e) => {
    const li = e.target.closest("li[data-i]");
    if (li) pickLead(Number(li.dataset.i));
  });

  startBtn.addEventListener("click", start);
  const startCurBtn = document.getElementById("ap-start-current-btn");
  if (startCurBtn) startCurBtn.addEventListener("click", () => { if (!active) startFromCurrent(); });
  if (stopBtn) stopBtn.addEventListener("click", stop);
  if (seedInput) seedInput.addEventListener("keydown", (e) => { if (e.key === "Enter") start(); });
})();
