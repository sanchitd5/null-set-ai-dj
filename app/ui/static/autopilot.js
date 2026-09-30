// AI DJ Autopilot
// Seed track → LLM suggests next → download → analyze → auto-crossfade → repeat.
//
// Deck alternation: even transitions crossfade A→B (xfader -1→+1),
// odd transitions crossfade B→A (xfader +1→-1). Tracks alternate slots.
//
// Requires: window.decks, window.loadIntoDeck, setStatus (app.js + deck-controller.js).

// Pure transition maths, no DOM / audio (node-checked: app/tests/js/autopilot_check.js).
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
  // Clashing keys (Camelot < 0.8, CLAUDE.md s4): a tonal blend layers two harmonic
  // records for 16-32 bars. Only Echo Out / Breakdown / Stem Bridge route around it;
  // a Long Blend or Bass Swap becomes an Echo Out. keyScore null (unknown key): unchanged.
  const KEY_SAFE_MIN = 0.6;      // below the KB's worst legal move (-2 hours, 0.6): only 2 hours with the letter flipped (0.3) and clashes (0) are rewritten
  function keySafeRecipe(recipe, keyScore) {
    if (keyScore == null || keyScore >= KEY_SAFE_MIN) return recipe;
    return /long blend|bass swap|drop swap/i.test(String(recipe || "")) ? "Echo Out" : recipe;
  }
  // A move learned from studied sets (/api/learned/pick) replaces the recipe only
  // when the console already allows that recipe for this pair (o: the same facts
  // scheduleTransition decides on), and never a move that outranks it (LAYER,
  // PEAK, riff over rap, mashup: user rule "mashup beats every other move").
  // sp: scene-profile.js core, o.profile: {level} (the Punjabi profile, null = none). Under the
  // full level (techniques.py learned_pick, same rule): a tonal learned blend on a key clash is
  // allowed when the Punjabi sets show it on clashes often enough (sp.learnedClashOk), and a
  // pair past the keylock cap never stretches: the move becomes the profile's Quick Cut.
  // -> {recipe, why} | null
  function learnedRecipe(pick, o, sp) {
    if (!pick || !pick.recipe || o.layer || o.peak || o.riff || o.recipe === "Mashup → Transition" || o.recipe === "Stem Merge") return null;
    const lvl = (sp && o.profile && o.profile.level) || null, full = !!sp && lvl === sp.FULL;
    const clashOk = full && sp.learnedClashOk(lvl, pick.scene_clash);
    const keyOk = o.keyScore == null || o.keyScore >= KEY_SAFE_MIN || clashOk;   // clashing keys: no tonal learned blend
    const cut = full && sp.PUNJABI_PROFILE.fallback_recipe;
    const want = full && !sp.learnedTempoOk(lvl, pick.tempo_gap) ? cut : pick.recipe;
    const allowed = {
      // any beat-to-beat pair can swap the bass on a line
      "Bass Swap": !!(keyOk && (o.blend || o.oneSong)),
      // the long stem intro keeps both records up: only when no vocal rule shortened it
      "Long Blend": !!(keyOk && o.oneSong && !o.vocalRule && (!o.blend || o.blend.clean)),
      "Mashup → Transition": !!(o.stemsBoth && o.mashupFits),
    };
    // the degraded learned move: the scene's cut on the downbeat (full level only)
    if (cut) allowed[cut] = keyOk || !/long blend|bass swap/i.test(String(pick.planned || pick.recipe));
    if (!allowed[want] || want === o.recipe) return null;
    const notes = full ? [pick.clash, pick.degraded || (want !== pick.recipe ? "tempo gap past the keylock cap" : null)].filter(Boolean) : [];
    return { recipe: want, why: `learned ${pick.kind.replace("_", " ")} (seen ${pick.seen}x, ${pick.source})` + (notes.length ? `; ${notes.join("; ")}` : "") };
  }
  // A FORCED plan (macro-mode.js: a macro step, a studied combo, a FOLLOW SET pick) is
  // performed as stored: scheduleTransition books its recipe and points and never re-picks.
  // The safety gates only refuse it; a refusal falls back to the closest allowed move.
  // Exit: the stored point on A while it is still ahead (lead s), else the next phrase line
  // on the stored point's grid. null: A ends first (the caller keeps its live timing).
  // o: {aTime, nowPos, phraseS, trackEnd, lead}
  function forcedExit(o) {
    const lead = o.lead == null ? 4 : o.lead;
    if (!Number.isFinite(o.aTime)) return null;
    let t = o.aTime;
    if (t < o.nowPos + lead && o.phraseS > 0) t += Math.ceil((o.nowPos + lead - t) / o.phraseS) * o.phraseS;
    return Number.isFinite(o.trackEnd) && t >= o.trackEnd ? null : t;
  }
  // The stored recipe through the live gates. o: {want, beat (tempo-locked), stemsBoth,
  // keyScore, mashupFits, mergeOk, mergeGate, vocalRule} -> {recipe, refused: null | "gate: why"}
  function forcedRecipe(o) {
    const want = String(o.want || "");
    const keyOk = o.keyScore == null || o.keyScore >= KEY_SAFE_MIN;
    const fb = o.beat && keyOk ? "Bass Swap" : "Echo Out";             // never a cut
    const no = (gate, recipe = fb) => ({ recipe, refused: gate });
    if (!want || /^(hard )?cut$/i.test(want)) return no("cut: never a hard cut");
    if (/merge|mashup|stem|riff/i.test(want) && !o.stemsBoth) return no("stems: stems missing on a deck");
    if (!/echo|bridge|filter/i.test(want) && !o.beat) return no("tempo: the pair is not beat-matchable", "Echo Out");
    if (keySafeRecipe(want, o.keyScore) !== want) return no(`key: camelot ${o.keyScore}`, "Echo Out");
    if (/stem merge/i.test(want) && !o.mergeOk) return no(`merge: ${o.mergeGate || "no clean hold"}`);
    if (/mashup/i.test(want) && !o.mashupFits) return no("mashup: the pair does not fit a mashup");
    if (/long blend|drop swap/i.test(want) && o.vocalRule) return no(`vocal: ${o.vocalRule.why || "two voices"}`, o.vocalRule.recipe || "Bass Swap");
    return { recipe: want, refused: null };
  }
  // The whole forced booking (scheduleTransition's seam, node-checked). o: {forced, nowPos,
  // phraseS, trackEnd, liveATime, liveBTime, beat, stemsBoth, keyScore, mashupFits, vocalRule,
  // mergeOn, planMerge(aT, bT, prefer) -> plan | null, mergeGate() -> "gate: why" | null}
  // -> {recipe, aT, bT, merge, refused, line}
  function forcedBooking(o) {
    const f = o.forced;
    const fx = forcedExit({ aTime: f.a_time, nowPos: o.nowPos, phraseS: o.phraseS, trackEnd: o.trackEnd });
    const aT = fx != null ? fx : o.liveATime;
    const bT = Number.isFinite(f.b_time) ? f.b_time : o.liveBTime;
    const fm = f.merge || {};
    let merge = null;
    if (/stem merge/i.test(f.recipe) && o.beat && o.mergeOn && o.stemsBoth && o.planMerge) {
      merge = o.planMerge(aT, bT, { entry: bT, M: fm.M || null, label: (fm.pick && fm.pick.label) || null });
    }
    const fr = forcedRecipe({ want: f.recipe, beat: o.beat, stemsBoth: o.stemsBoth, keyScore: o.keyScore, mashupFits: o.mashupFits,
      mergeOk: !!merge, mergeGate: !o.mergeOn ? "MERGE is off" : o.mergeGate ? o.mergeGate() : null, vocalRule: o.vocalRule });
    const line = forcedLine(f, fr.recipe, aT, bT) + (merge && merge.phases ? `, hold ${merge.phases.hold.bars} bars` : "") +
      (fr.refused ? ` [refused ${f.recipe}: ${fr.refused}]` : "");
    return { recipe: fr.recipe, aT, bT, merge: fr.recipe === "Stem Merge" ? merge : null, refused: fr.refused, line };
  }
  // `macro: performing step <n>: <A> -> <B> <move> (exit <t>, entry <t>)`
  function forcedLine(f, recipe, aT, bT) {
    const m = (t) => (Number.isFinite(t) ? `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}` : "?");
    const who = f.n != null ? `macro: performing step ${f.n}` : `${f.source || "studied"}: performing`;
    return `${who}: ${f.a_name || f.a || "A"} -> ${f.b_name || f.b || "B"} ${recipe} (exit ${m(aT)}, entry ${m(bT)})`;
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
  // S22 breakdown ownership (research/notes/artist-signature-techniques.md): A's breakdowns
  // [[t0, t1]], same rule as app/music_brain/preplan.breakdown_spans (golden vectors in
  // app/tests/fixtures/rule_vectors.json): energy <= its 30th percentile AND 0.15 x range under
  // its median, between the first and last frame at or above the median (not intro / outro),
  // joined across gaps up to 2 bars, at least 8 bars long. Thresholds GUESSED from counts.
  const BD_PCT = 30, BD_BELOW_MEDIAN = 0.15, BD_JOIN_BARS = 2, BD_MIN_BARS = 8;
  function breakdownSpans(times, curve, bar) {
    if (!times || !curve || times.length < 4 || times.length !== curve.length) return [];
    const sorted = [...curve].sort((x, y) => x - y);
    const range = sorted[sorted.length - 1] - sorted[0];
    if (!(range > 1e-6)) return [];                                  // flat
    const med = median(sorted), thr = Math.min(quantileLinear(sorted, BD_PCT / 100), med - BD_BELOW_MEDIAN * range);
    let first = null, last = null;
    for (let i = 0; i < times.length; i++) {
      if (curve[i] >= med) { if (first === null) first = times[i]; last = times[i]; }
    }
    const spans = [];
    for (let i = 0; i < times.length; i++) {
      const t = times[i];
      if (curve[i] > thr || t <= first || t >= last) continue;
      const prev = spans[spans.length - 1];
      if (prev && t - prev[1] <= BD_JOIN_BARS * bar) prev[1] = t;
      else spans.push([t, t]);
    }
    const hop = times[1] - times[0];
    return spans.filter(([x, y]) => y + hop - x >= BD_MIN_BARS * bar).map(([x, y]) => [x, y + hop]);
  }
  // Exit kept out of A's breakdowns by whole phrases: first the latest phrase before the
  // breakdown (the section before it, no earlier than `lo`), else the first one past its end
  // (the drop, no later than `limit`). Neither fits -> unchanged, clear false (logged, not refused).
  function exitOutOfBreakdown(exit, spans, phraseS, lo, limit) {
    const inBd = (x) => spans.some(([a, b]) => x >= a && x < b);
    if (!inBd(exit) || !(phraseS > 0)) return { t: exit, clear: !inBd(exit), moved: 0 };
    for (let k = 1; k <= 12; k++) {
      const t = exit - k * phraseS;
      if (t < lo) break;
      if (!inBd(t)) return { t, clear: true, moved: -k };
    }
    for (let k = 1; k <= 12; k++) {
      const t = exit + k * phraseS;
      if (t > limit) break;
      if (!inBd(t)) return { t, clear: true, moved: k };
    }
    return { t: exit, clear: false, moved: 0 };
  }
  // The live rule; app/music_brain/energy.next_ok mirrors it (same golden vectors,
  // app/tests/fixtures/rule_vectors.json): at most 2 levels a song (1 relaxed,
  // +1 on the last-round fallback); early in the set (< 30 %) it may not fall more
  // than 1, near the end (> 85 %) not rise more than 1. -> {ok, step, why}
  const ENERGY_MIN_RAW = 0.1;      // raw 0-1: below this the two songs measure the same, whatever the levels say
  const WARMUP_SONGS = 5;          // the set builds over its first songs; open-ended after (no known end)
  const LOW_ENERGY_SET_MAX = 4;    // cur at or below this: the set is already playing low (sufi, relaxing) -- warmup's "build" assumption doesn't apply, don't force it up
  // Cumulative fall (mirrors energy.py PEAK_WINDOW / MAX_BELOW_PEAK): per-step falls are capped
  // but 9 > 7 > 4 > 2 still drained a set. DJ/06 Energy Management & Dynamics holds a valley to ~3
  // below the peak, and only as a deliberate reset, so landing 3+ under the last 6 songs' peak is refused.
  const PEAK_WINDOW = 6, MAX_BELOW_PEAK = 2;
  // o: {relaxed, force, songs (played so far), rawDelta (raw_b - raw_a), recent (measured levels played, playing last), reset (a dip was asked for)}
  function energyStepOk(cur, nxt, o = {}) {
    const step = nxt - cur, lim = (o.relaxed ? 1 : 2) + (o.force && step > 0 ? 1 : 0); // force widens rises only, never falls
    if (o.rawDelta != null && Math.abs(o.rawDelta) < ENERGY_MIN_RAW) {
      return { ok: true, step, why: `energy ${cur} -> ${nxt} (measured almost the same)` };
    }
    let arc = o.songs != null ? (o.songs < WARMUP_SONGS ? "build" : "")
      : o.setPos != null && o.setPos < 0.3 ? "build" : o.setPos != null && o.setPos > 0.85 ? "cool" : "";
    // A set already playing low energy (sufi, relaxing) isn't "building" just
    // because it's early: WARMUP_SONGS assumes an opening ramp, which doesn't
    // hold when the songs themselves are already calm. Let it stay calm or
    // drop further instead of forcing it toward "build" (but still respect a
    // genuine "cool" arc near the end -- don't spike energy up there either).
    if (arc === "build" && Number.isFinite(cur) && cur <= LOW_ENERGY_SET_MAX) arc = "";
    if (Math.abs(step) > lim) return { ok: false, step, why: `energy ${step > 0 ? "jump" : "drop"} ${cur} -> ${nxt} (max ${lim} a song)` };
    if (!o.force && arc === "build" && step < -1) return { ok: false, step, why: `energy falls ${cur} -> ${nxt} while the set is building` };
    if (!o.force && arc === "cool" && step > 1) return { ok: false, step, why: `energy rises ${cur} -> ${nxt} while the set is cooling down` };
    if (step < 0 && o.recent && o.recent.length && !o.reset && !o.relaxed && arc !== "cool") {
      const peak = Math.max(cur, ...o.recent.slice(-PEAK_WINDOW).filter((v) => v != null && Number.isFinite(v)));
      if (peak > LOW_ENERGY_SET_MAX && peak - nxt > MAX_BELOW_PEAK) return { ok: false, step, why: `energy ${cur} -> ${nxt} drains the set: ${peak - nxt} below its recent peak ${peak}` };
    }
    return { ok: true, step, why: `energy ${cur} -> ${nxt}` };
  }
  // "Let the song finish" gate (research/notes/dj-hidden-practices.md item 10); the same rule as
  // app/music_brain/energy.at_target (golden vectors): out of the warm-up (or a low set), the last
  // 3 measured levels within 1 of each other, the playing song within 1 of the recent peak. GUESS numbers.
  const TARGET_SONGS = 3, TARGET_TOL = 1;
  function energyAtTarget(recent, songs) {
    const lv = (recent || []).filter((v) => v != null);
    if (lv.length < TARGET_SONGS) return { ok: false, why: `only ${lv.length} measured songs` };
    const cur = lv[lv.length - 1];
    if (songs < WARMUP_SONGS && cur > LOW_ENERGY_SET_MAX) return { ok: false, why: "the set is still building" };
    const last = lv.slice(-TARGET_SONGS), lo = Math.min(...last), hi = Math.max(...last);
    if (hi - lo > TARGET_TOL) return { ok: false, why: `energy still moving (${lo}-${hi})` };
    const peak = Math.max(...lv.slice(-PEAK_WINDOW));
    if (peak - cur > TARGET_TOL) return { ok: false, why: `energy ${cur} under the recent peak ${peak}` };
    return { ok: true, why: `energy holding at ${cur}` };
  }

  // HYBRID window class by measured energy. Very low energy (<= 3) rides MID, not LONG:
  // an E2 song in a fading set ran 283 s with no exit (015929).
  function hybridWindowKey(energy) {
    if (energy == null) return "medium";
    if (energy >= 7) return "quick";
    if (energy <= 3) return "medium";
    return energy <= 5 ? "long" : "medium";
  }

  // A background job's final answer (app/ui/services/bg_jobs.py: the silent ear's preplan /
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
  // Song search that keeps coming back empty (60 empty answers in one session, every 20 s):
  // wait 20, 40, 80, 120 s (cap) between searches, and from the 2nd empty answer in a row take a
  // library song instead of asking the model again.
  const EMPTY_LIBRARY_AFTER = 2;
  const emptyRetryMs = (streak) => Math.min(120000, 20000 * 2 ** Math.max(0, Math.min(streak, 10) - 1));
  const useLibraryFallback = (streak) => streak >= EMPTY_LIBRARY_AFTER;
  // Deadline rule (HOLD LOOP in Punjabi set 2026-09-30_102327: 17 ok suggest calls, every pick dropped,
  // the set looped at the exit). Once the playing song is DEADLINE_LEAD_S from the start of its exit
  // window, or a whole search for this song already failed, the next search tries the ready pool and a
  // library song that tempo-locks BEFORE asking the model again: a ready fallback beats waiting. 45 s =
  // one model answer (12-17 s) + analysis / stems of a library song + a phrase of margin (GUESS).
  // Failed searches back off like empty answers (20, 40, 80, 120 s), so a model whose picks all get
  // dropped is not asked back-to-back every 15 s.
  const DEADLINE_LEAD_S = 45;
  function searchPlan({ pos, exitLo, failedSearches = 0, emptyStreak = 0 } = {}) {
    const deadline = Number.isFinite(pos) && Number.isFinite(exitLo) && pos >= exitLo - DEADLINE_LEAD_S;
    return {
      deadline,
      fallbackFirst: deadline || failedSearches >= 1 || useLibraryFallback(emptyStreak),
      retryMs: emptyRetryMs(Math.max(emptyStreak, failedSearches)),
    };
  }
  // Atlas backup B (preplanned as soon as A plays; GET /api/atlas/backup rows). The atlas's partners
  // are library songs with a scored pair: they cannot be invented the way the model's picks were.
  // Tier order: studied combo, atlas combo, then the best partner; songs heard in an earlier set after
  // the fresh ones; inside a tier a partner whose atlas energy level passes energyStepOk before an
  // unmeasured one, then by works. A partner that fails a cheap live gate (played, bad played evidence,
  // artist spacing, a remembered pair reject, the energy step) is skipped WITH its reason: 102327 died
  // on "energy drop 8 -> 2..5" and said nothing. Booking still runs every live gate (tryCandidate).
  // o: {aId, played: ids, recent: names, energyA, songs, relaxed, recentLevels, spacing(name), rejected(bId)}
  function rankAtlasBackups(rows, o = {}) {
    const played = new Set(o.played || []), recent = new Set(o.recent || []);
    const list = [], skipped = [];
    for (const r of rows || []) {
      if (!r || !r.b || r.b === o.aId) continue;
      const name = r.b_name || r.b;
      let why = played.has(r.b) || recent.has(name) ? "already played this set"
        : (r.played_bad || 0) > (r.played_good || 0) ? `bad played evidence (-${r.played_bad})` : null;
      if (!why && o.spacing) why = o.spacing(name) || null;
      if (!why && o.rejected) { const k = o.rejected(r.b); if (k) why = k.why || String(k); }
      let fit = 0;
      if (!why && Number.isFinite(o.energyA) && Number.isFinite(r.b_level)) {
        const v = energyStepOk(o.energyA, r.b_level, { songs: o.songs, relaxed: o.relaxed, recent: o.recentLevels });
        if (v.ok) fit = 1; else why = v.why;
      }
      if (why) { skipped.push({ b: r.b, name, why }); continue; }
      list.push({ track_id: r.b, name, bpm: r.b_bpm, duration: r.b_duration, works: r.works || 0, plan: r.plan || null,
                  level: Number.isFinite(r.b_level) ? r.b_level : null, fit, earlier: !!r.earlier_set,
                  tier: r.studied ? "studied combo" : r.combo ? "atlas combo" : "atlas partner" });
    }
    const rank = { "studied combo": 0, "atlas combo": 1, "atlas partner": 2 };
    list.sort((x, y) => (x.earlier - y.earlier) || (rank[x.tier] - rank[y.tier]) || (y.fit - x.fit)
      || (y.works - x.works) || (x.track_id < y.track_id ? -1 : x.track_id > y.track_id ? 1 : 0));
    return { list, skipped };
  }
  // The backup is re-ranked when A changed, a song was played since, or A's energy became known / changed.
  function backupStale(b, ctx) {
    return !b || b.for !== ctx.aId || b.played !== (ctx.played || []).length || b.energyA !== ctx.energyA;
  }
  // Stems are warmed early only for a plan whose recipe plays stems.
  const STEM_RECIPE = /stem|merge|acapella|mashup/i;
  const backupNeedsStems = (plan) => !!(plan && plan.recipe && STEM_RECIPE.test(plan.recipe));

  // (A, B) pairs that already failed a pairwise gate (vibe / energy / plan-fit) are not matched again
  // while A still plays. A reject made under the relaxed last-round limits holds in every round; one
  // made under the strict limits does not hold in the forced round (it may pass there).
  const pairKey = (aId, bId) => `${aId}>${bId}`;
  function rememberPairReject(store, aId, bId, why, forced) { store.set(pairKey(aId, bId), { why, forced: !!forced }); }
  function pairRejected(store, aId, bId, forced) {
    const r = store.get(pairKey(aId, bId));
    return r && (!forced || r.forced) ? r : null;
  }
  // ---- scheduleTransition seams (pure; the virtual set in app/sim drives them) ----
  // Beat-to-beat blends run on the pitched mix (8 %) or on key-locked tempo stems
  // (keyLim); half / double time counts. aEff: A's heard tempo.
  function tempoLockableAt(aEff, bBpm, lim) {
    return [1, 2, 0.5].some((m) => Math.abs(aEff / (bBpm * m) - 1) <= lim);
  }
  // Recipe kind: which family of moves a recipe name runs as.
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
  // The recipe scheduleTransition books from the matcher's pick and the live facts.
  // o: {recipe, blend, layer (bool), aStems, bStems, aEff, bBpm, tempoStemsBpm,
  //     keyScore (Camelot 0-1 or null), mashupFits: () => bool}
  // -> {recipe, blend (null when dropped), dropLayer, blendClean, vocalShort, vocalCut,
  //     vocalRule, oneSong, stemsBoth, lockS, keyRewrite {from,to}|null, cutRewrite}
  function decideRecipe(o, tempoRule) {
    let recipe = o.recipe || "Blend", blend = o.blend || null;
    let vocalShort = false, vocalCut = "", vocalRule = false, blendClean = null;
    const aStems = !!o.aStems, bStems = !!o.bStems, stemsBoth = aStems && bStems;
    // Tempo gate (tempo-rule.js): a beat-to-beat recipe only when B locks to A's
    // heard tempo right now (pitched mix or key-locked stems), else beatless.
    const lockS = tempoRule.planFit({ aEff: o.aEff, bBpm: o.bBpm, stemsBoth, tempoStemsBpm: o.tempoStemsBpm });
    const oneSong = lockS.oneSong;
    let dropLayer = false;
    if (!lockS.beat) { blend = null; dropLayer = true; }
    // Beat-to-beat: when the tempos lock, hand beat to beat. Echo-outs are for
    // tempo gaps; they turned "vocal -> beat" when used between compatible songs.
    if (blend) {
      // A's vocal riding over B's instrumental intro is a classic long blend;
      // only two vocals at once clash, so keep that overlap short (bass swap).
      const bClean = blend.b_vocal_coverage == null || blend.b_vocal_coverage <= 0.15;
      const k = recipeKind(recipe);
      if (!bClean) recipe = "Bass Swap";
      else if (!["bass", "blend", "default"].includes(k)) recipe = "Long Blend";
      blendClean = bClean;
      // Two vocals must never sing together: the overlap has to END before B's
      // vocal first comes in. Pick the transition length by how many bars that is.
      if (oneSong) { recipe = "Long Blend"; blendClean = true; }
      // stems on either deck: one singer by muting a vocal stem, never a cut
      const vr = vocalRecipe({ vIn: blend.b_vocal_in_bars, oneSong, aStems, bStems });
      if (vr) { vocalRule = true; recipe = vr.recipe; vocalShort = vr.short; vocalCut = vr.why; }
    } else if (oneSong) {
      // tempos lock (key-locked tempo stems attached, or inside the pitch range)
      recipe = "Long Blend";
    } else if (stemsBoth) {
      // tempo can't lock: a stem bridge, never an echo-out (user)
      recipe = "Stem Bridge";
    } else if (!["echo", "filter"].includes(recipeKind(recipe))) {
      // No stems and no tempo lock: beats cannot be layered, so don't hard-swap.
      // Echo the outgoing song away while the new one enters on its phrase ([[Echo Out]]).
      recipe = "Echo Out";
    }
    // Clashing keys never get a tonal blend: Echo Out (CLAUDE.md s4). The matcher
    // ranked key-safe recipes for these pairs; the rewrites above turned them into
    // Long Blend / Bass Swap without reading the key.
    let keyRewrite = null;
    if (!o.layer || dropLayer) {
      const safe = keySafeRecipe(recipe, o.keyScore);
      if (safe !== recipe) { keyRewrite = { from: recipe, to: safe }; recipe = safe; blend = null; vocalShort = false; }
    }
    // Punjabi scene profile (scene-profile.js, o.profile = {level, fallback}): the Echo Out a
    // key rewrite or a missing tempo lock would book is a Quick Cut on the line, and a Quick Cut
    // is kept (the scene's main move; note s7, GUESS). No profile: nothing below changes.
    let profileCut = false;
    if (o.profile && recipe === "Echo Out" && (keyRewrite || !lockS.beat)) { recipe = o.profile.fallback || "Quick Cut"; profileCut = true; }
    const keepCut = !!o.profile && recipe === "Quick Cut";
    // Never a hard cut (user): a cut the matcher or the AI plan proposed is a 4-bar Bass Swap.
    let cutRewrite = false;
    if (!keepCut && /\bcut\b/i.test(String(recipe || ""))) { recipe = "Bass Swap"; vocalShort = true; cutRewrite = true; }
    // Mashup -> transition beats every other move when the pair fits (user)
    if (lockS.beat && stemsBoth && o.mashupFits && o.mashupFits()) recipe = "Mashup → Transition";
    const out = { recipe, blend, dropLayer, blendClean, vocalShort, vocalCut, vocalRule, oneSong, stemsBoth, lockS, keyRewrite, cutRewrite };
    if (o.profile) { out.profileCut = profileCut; out.quickCut = recipe === "Quick Cut"; }
    return out;
  }
  // Would the plan LLM call change what plays? With stems on both decks and a beat lock,
  // decideRecipe forces the recipe, the blend plan owns the exit, LAYER is rule-gated,
  // and the stem remix/breakdown ticks own moves on a stem deck. PEAK moves (fakeout,
  // beat_boost) only come from the plan, so they keep it. -> reason string, or null (ask).
  function planSkipReason(o) {
    if (!o || !o.stemsBoth || !o.lockBeat) return null;
    if (o.peakOn) return null;
    return "stems on both decks and tempo locked: recipe, exit and moves are rule-decided";
  }
  // Play-time windows by set mode, counted from when a song came in.
  //   long   songs ride 3-6 min, long 24 s blends;  quick  40-100 s, 8 s blends
  //   hybrid per song: weak match or high energy -> quick, low energy -> long
  const WINDOWS = {
    long:   { min: 180, max: 360, xf: 24, label: "LONG" },
    medium: { min: 120, max: 240, xf: 16, label: "MID" },
    quick:  { min: 40,  max: 100, xf: 8,  label: "QUICK" }, // user: 40-100 s (widened from 60-120)
    bail:   { min: 30,  max: 60,  xf: 8,  label: "QUICK·bail" },
    // steering toward the occasion's music: short bridge songs (user: 30-60 s each)
    bridge: { min: 30,  max: 60,  xf: 8,  label: "BRIDGE" },
  };
  // o: {steering ("move"), famous, rem (famous / finish song: seconds left from its entry),
  //     mode, score, energy, finish}
  const FINISH_MAX_S = 360;        // = WINDOWS.long.max; GUESS
  function playWindowFor(o) {
    if (o.steering === "move") return WINDOWS.bridge;
    // Punjabi scene profile, full level: 45-90 s snippets (scene-profile.js playWindow; note s7, GUESS)
    if (o.profileWindow) return o.profileWindow;
    // A famous song plays in full (user; the USB002 set rides leavemealone for
    // 7 min): exit only in its last ~50 s, i.e. the outro.
    if (o.famous && o.rem > 90) return { min: Math.max(60, o.rem - 50), max: Math.max(70, o.rem - 6), xf: 24, label: "FULL·famous" };
    const weak = (o.score == null ? 50 : o.score) < 65;
    // Let the song finish (dj-hidden-practices item 10): the set holds its energy target
    // (o.finish, energyAtTarget), so this song plays to its outro like a famous one. Never in
    // QUICK (the user asked for quick), never over a weak match's bail, never on a song with
    // more than FINISH_MAX_S left (no 7-minute holds). The exit stays before the audible end.
    if (o.finish && o.mode !== "quick" && !weak && o.rem > 90 && o.rem <= FINISH_MAX_S) {
      return { min: Math.max(60, o.rem - 50), max: Math.max(70, o.rem - 6), xf: 24, label: "FULL·finish" };
    }
    if (o.mode === "long") return WINDOWS.long;
    if (o.mode === "quick") return weak ? WINDOWS.bail : WINDOWS.quick;
    if (weak) return WINDOWS.bail;
    return WINDOWS[hybridWindowKey(o.energy)];
  }
  // Exit window on the playing song (track seconds), from the play window.
  // o: {w (play window), entryPos, trackDur (Infinity when unknown)}
  function exitBounds(o) {
    const trackEnd = o.trackDur - o.w.xf - 2;
    return { trackEnd, lo: Math.min(o.entryPos + o.w.min, trackEnd), hi: Math.min(o.entryPos + o.w.max, trackEnd) };
  }
  // Where a file stops being audible: the end of its last energy frame above
  // SILENT_FLOOR (normalized RMS, 1 s hop), never past the buffer. A trailing
  // silence under SILENT_TAIL_MIN_S is left alone. Unknown analysis -> dur.
  // A 10 s silent tail on BICEP and leavemealone's -180 dB last second came from
  // planning against the raw buffer length.
  const SILENT_FLOOR = 0.01, SILENT_TAIL_MIN_S = 2, END_ROOM_S = 60;
  function audibleEnd(analysis, dur) {
    const tt = analysis && analysis.energy_times, cv = analysis && analysis.energy_curve;
    if (!(dur > 0) || !tt || !cv || !tt.length || tt.length !== cv.length) return dur;
    let i = cv.length - 1;
    while (i >= 0 && !(cv[i] > SILENT_FLOOR)) i--;
    if (i < 0) return dur;                       // all quiet: no evidence, keep dur
    const hop = tt.length > 1 ? tt[1] - tt[0] : 1;
    const end = Math.min(dur, tt[i] + hop);
    return dur - end >= SILENT_TAIL_MIN_S ? end : dur;
  }
  // A song must not be started in its last END_ROOM_S seconds (Sabrina loaded at
  // 199 s of 208): pull the start back so there is room to play.
  function entryClamp(t, endS) {
    return endS > END_ROOM_S && t > endS - END_ROOM_S ? endS - END_ROOM_S : t;
  }
  // (exitPick below, then exitTiming, exitHighPush)
  // The planned exit inside the window: a blend / layer point wins, else the
  // matcher's point clamped into the window, never before the song's first drop.
  // o: {lo, hi, trackEnd, layerStart, blendExit, candidateATime, minExit, hasBlend}
  function exitPick(o) {
    let exitAt = o.layerStart != null ? o.layerStart : o.hasBlend ? o.blendExit : o.candidateATime;
    if (!o.hasBlend && !(exitAt >= o.lo && exitAt <= o.hi)) exitAt = Math.max(o.lo, Math.min(o.hi, exitAt || o.hi));
    // never leave before the playing song's first drop has played (server floor)
    if (!o.hasBlend && o.minExit != null && exitAt < o.minExit && o.minExit < o.trackEnd) exitAt = o.minExit;
    return exitAt;
  }
  // Exit kept on A's phrase grid (whole phrases, never seconds) and the crossfade length.
  // o: {exitAt, nowPos, phraseS, w, oneSong, vocalShort, peak}
  function exitTiming(o) {
    let t = o.exitAt;
    while (t < o.nowPos + 15) t += o.phraseS;
    // vocalShort: xfDuration < 16 halves every bar count in executeTransition
    // (Bass Swap 8 -> 4 bars) so the overlap ends before B's vocal.
    const xfDuration = o.oneSong ? Math.max(16, o.w.xf) : o.vocalShort ? Math.min(8, o.w.xf) : o.peak ? Math.max(16, o.w.xf) : o.w.xf;
    return { effectiveATime: t, xfDuration };
  }
  // Never transition out of A while it is at its energy high: the exit moves past it by
  // whole phrases (not for PEAK / LAYER / pre-planned: they chose their line).
  // o: {t (effectiveATime), phraseS, bpm, trackEnd, energyTimes, energyCurve} -> {t, moved}
  function exitHighPush(o) {
    if (!o.energyTimes || !o.energyCurve) return { t: o.t, moved: 0 };
    const spans = highSpans(o.energyTimes, o.energyCurve, 240 / o.bpm);
    const ex = exitPastHigh(o.t, 16 * 240 / o.bpm, spans, o.phraseS, o.trackEnd);
    return { t: ex.moved ? ex.t : o.t, moved: ex.moved };
  }
  // S22: never start the blend inside A's breakdown (A owns the room). Same exemptions as
  // exitHighPush. o: {t, lo (earliest allowed, e.g. now + 15 s), phraseS, bpm, trackEnd,
  // energyTimes, energyCurve} -> {t, moved (phrases, negative = earlier), clear}
  function exitBreakdownPush(o) {
    if (!o.energyTimes || !o.energyCurve || !(o.bpm > 0)) return { t: o.t, moved: 0, clear: true };
    const spans = breakdownSpans(o.energyTimes, o.energyCurve, 240 / o.bpm);
    return exitOutOfBreakdown(o.t, spans, o.phraseS, o.lo == null ? -Infinity : o.lo, o.trackEnd);
  }
  // ---- pre-render readiness (next song's stems + key-locked tempo stems made BEFORE the booking) ------
  // Measured on 25 real transitions (data/cache/sessions): the booking follows the deck load by a
  // median 6 s, the fire comes a median 206 s later, and 9 of 25 songs had no stems on the deck at the
  // booking (they landed 38-159 s after the load), so the merge was refused for a state that would
  // have been fine 1-2 minutes on. The merge gate therefore WAITS (bounded) instead of deciding at once.
  const READY_MIN_GAP = 0.02;          // below 2 % the pitched mix locks; above it key-locked stems are needed
  const DEFER_MAX_S = 100;             // never hold a booking longer (the search watchdog fires at 150 s)
  const DEFER_MIN_LEAD_S = 45;         // the booking needs this long before the earliest exit (preplan, plan, 15 s floor)
  const PREFER_READY_JUMP = 1;         // a ready candidate may pass at most this many not-ready ones (taste still wins)
  const roundBpm = (b) => Math.round(b / 0.5) * 0.5;
  // Tempi a candidate may have to play at, so its key-locked sets are rendered ahead: the tempo A has
  // now and A's native tempo (where an easing-home deck ends up), each folded to B by half / double
  // time, kept when the gap is inside the key-lock cap and past the pitch-only range.
  // o: {aEff, aNative, bBpm, lim} -> [bpm, ...] (0.5 BPM steps, unique)
  function prerenderTargets(o) {
    if (!(o.bBpm > 0)) return [];
    const out = [];
    for (const a of [o.aEff, o.aNative]) {
      if (!(a > 0)) continue;
      const m = [1, 2, 0.5].reduce((b, x) => (Math.abs(a / (o.bBpm * x) - 1) < Math.abs(a / (o.bBpm * b) - 1) ? x : b));
      const gap = Math.abs(a / (o.bBpm * m) - 1);
      if (gap > READY_MIN_GAP && gap <= o.lim) {
        const t = roundBpm(a / m);
        if (!out.includes(t)) out.push(t);
      }
    }
    return out;
  }
  // What a merge-capable booking still waits for. o: {aStems, bStems, aEff, bBpm, tempoStemsBpm, lim, keyScore}
  // -> {needs: ["stems" | "tempo stems"], skip: why waiting is pointless | null}. Waiting is pointless
  // when the merge cannot happen anyway (A has no stems, keys clash, the tempo gap is past the cap).
  function readinessNeeds(o) {
    if (!o.aStems) return { needs: [], skip: "A has no stems" };
    if (o.keyScore != null && o.keyScore < KEY_SAFE_MIN) return { needs: [], skip: `keys clash (camelot ${o.keyScore})` };
    if (!(o.aEff > 0) || !(o.bBpm > 0)) return { needs: [], skip: "tempo unknown" };
    const gap = Math.abs(o.aEff / o.bBpm - 1);
    if (gap > o.lim) return { needs: [], skip: `gap ${(gap * 100).toFixed(1)} % over the cap` };
    const needs = [];
    if (!o.bStems) needs.push("stems");
    if (gap > READY_MIN_GAP && !(o.tempoStemsBpm && Math.abs(o.tempoStemsBpm / o.aEff - 1) < 0.01)) needs.push("tempo stems");
    // A is still easing back to its own tempo after its lock: the tempo stems B needs are the ones for A's HOME
    // tempo (where it will be when the merge runs), and every gate (tempo-rule.planFit, planHold) reads A's
    // tempo right now, so the booking waits for the glide to end instead of chasing a moving tempo.
    if (o.aSettled === false) needs.push("A's tempo home");
    return { needs, skip: null };
  }
  const SETTLED_PCT = 0.05;            // |pitch| under this: the deck is at its own tempo (tempo-rule STILL_PCT)
  // The tempo A will have when the merge runs: its own (native) tempo while it is still easing home, else its
  // live tempo. o: {bpm, rate, pitchPct} -> {bpm, settled}
  function aTempoAtEntry(o) {
    const settled = Math.abs(o.pitchPct || 0) < SETTLED_PCT;
    return { bpm: settled ? o.bpm * o.rate : o.bpm, settled };
  }
  // Seconds the booking may wait: until the earliest exit minus the lead the plan needs, at most DEFER_MAX_S.
  // o: {nowPos, exitLo (track s), minLeadS?, maxS?}
  function deferBudgetS(o) {
    const lead = o.minLeadS == null ? DEFER_MIN_LEAD_S : o.minLeadS, max = o.maxS == null ? DEFER_MAX_S : o.maxS;
    return Math.max(0, Math.min(max, o.exitLo - o.nowPos - lead));
  }
  // o: {needs, waitedS, budgetS} -> {wait, why}. `why` is the line the log and the sim read.
  function deferDecision(o) {
    if (!o.needs.length) return { wait: false, why: "ready" };
    const what = o.needs.join(" and ");
    if (o.waitedS >= o.budgetS) return { wait: false, why: `gave up after ${o.waitedS.toFixed(0)} s waiting for ${what}` };
    return { wait: true, why: `waiting for ${what}` };
  }
  // Prefer candidates whose stems and tempo sets are already made, bounded: a ready one may move ahead of at
  // most `maxJump` not-ready ones, so the AI's own order still decides between candidates that differ.
  function orderByReadiness(pool, isReady, maxJump = PREFER_READY_JUMP) {
    const out = pool.slice();
    for (let i = 1; i < out.length; i++) {
      if (!isReady(out[i])) continue;
      let j = i, jumped = 0;
      while (j > 0 && jumped < maxJump && !isReady(out[j - 1])) { [out[j - 1], out[j]] = [out[j], out[j - 1]]; j--; jumped++; }
    }
    return out;
  }
  const api = { prerenderTargets, readinessNeeds, aTempoAtEntry, deferBudgetS, deferDecision, orderByReadiness, DEFER_MAX_S, DEFER_MIN_LEAD_S, PREFER_READY_JUMP,
    emptyRetryMs, useLibraryFallback, searchPlan, DEADLINE_LEAD_S, rankAtlasBackups, backupStale, backupNeedsStems, rememberPairReject, pairRejected, awaitJob, keySafeRecipe, KEY_SAFE_MIN, energyStepOk, hybridWindowKey, highSpans, quantileLinear, median, exitPastHigh, learnedRecipe, vocalRecipe, stemBlendBars, stemBlendFader, phraseWaitS, introBars, FADER_PARK_BARS, homePlan, maskedGlideBars, maskedDropAt, HOME_DROP_PCT, LADDER_STEP_PCT,
    tempoLockableAt, recipeKind, decideRecipe, planSkipReason, WINDOWS, playWindowFor, exitBounds, exitPick, exitTiming, exitHighPush, audibleEnd, entryClamp, SILENT_FLOOR,
    breakdownSpans, exitOutOfBreakdown, exitBreakdownPush, energyAtTarget, FINISH_MAX_S, forcedExit, forcedRecipe, forcedLine, forcedBooking };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  return api;
})();

// ---- runtime: the engine reaches the world only through the Host port (engine.js): host.decks, host.clock,
// host.api, host.bus, host.ui, host.mod (djMind, stemMoves, tempoRule, riffOverRap ...), host.log, host.random.
function createAutopilotEngine({ host, ai }) {
  const { setTimeout, clearTimeout, setInterval, clearInterval } = host.clock;
  const audioCtx = host.audio;
  const ui = host.ui;
  const loadIntoDeck = (...a) => host.loadIntoDeck(...a);
  // Every request the autopilot makes has a deadline. Without one, a request
  // lost in a server restart never settled and the set sat in HOLD LOOP
  // forever ("Matching transition..." stuck). Budgets match the work behind
  // each endpoint (LLM queue, Demucs stems, 30-50 MB FLAC audio).
  const _fetch = (url, opts) => host.api.fetch(url, opts);
  function deadlineFor(url) {
    const u = String(url);
    if (u.includes("/api/download")) return 300000;
    if (u.includes("/api/blend/plan") || u.includes("/api/mashup/plan")) return 180000;
    if (u.includes("/api/layer/plan")) return 60000;   // vocal maps already cached by the blend plan
    if (u.includes("/api/bridge/plan")) return 10000;
    // background jobs (app/ui/services/bg_jobs.py): the POST starts one, the GET polls it; both answer at once
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
  let setStartedAt = 0;       // host.clock.now() when the set started (elapsed_seconds for suggest)
  // One id per set in this browser tab: the server scopes its suggestion memory
  // to it, so another tab's set (or this tab's previous set) never counts as
  // "this set", and two tabs never share a cached suggestion.
  const newSetId = () => host.random.uuid();
  let setId = newSetId();
  let beatMutedByLayer = false; // a LAYER paused the live beat layer (restore after / on stop)
  function unmuteBeatLayer() {
    if (beatMutedByLayer && host.mod.beatLayer) host.mod.beatLayer.setEnabled(true);
    beatMutedByLayer = false;
  }

  // ── UI refs ───────────────────────────────────────────────────────────────
  const seedInput      = ui.el("ap-seed-input");
  const occasionInput  = ui.el("ap-occasion-input");
  const startBtn       = ui.el("ap-start-btn");
  const stopBtn        = ui.el("ap-stop-btn");
  const statusEl       = ui.el("ap-status");
  const queueEl        = ui.el("ap-queue");
  const xfader         = ui.el("crossfader");

  if (!startBtn) return; // panel not present

  // ── helpers ───────────────────────────────────────────────────────────────
  function apStatus(msg) {
    if (statusEl) statusEl.textContent = msg;
    ui.status(msg);
  }

  function stagingDeck() { return activeDeck === "a" ? "b" : "a"; }

  function deckPosition(id) {
    const d = host.decks && host.decks[id];
    return d ? d._currentPosition() : 0;
  }
  // A chosen move whose level dip IS the move (stemMoves.core.DIP_ALLOWED):
  // said out loud, never a silently skipped floor check.
  function dipAllowed(kind, what) {
    const D = host.mod.stemMoves && host.mod.stemMoves.core.DIP_ALLOWED;
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
    ui.queryAll("[data-ai-audio]").forEach((el) => { delete el.dataset.aiAudio; });
  }

  // Equal-power gains for crossfader value v (-1..1), +XF_CENTER_BOOST_DB at the centre.
  function xfGains(v) {
    const x = (v + 1) / 2;
    const boost = Math.pow(10, (XF_CENTER_BOOST_DB * Math.sin(Math.PI * x)) / 20);
    return [Math.cos(x * 0.5 * Math.PI) * boost, Math.cos((1 - x) * 0.5 * Math.PI) * boost];
  }

  function audioTargetOf(el) {
    if (!el || !host.decks) return null;
    if (el === xfader) return { kind: "xf" };
    if (el.classList && el.classList.contains("eq-knob")) {
      const d = host.decks[el.dataset.deck];
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
      const p = host.decks[d].crossfaderGain.gain;
      hold(p);
      p.setValueAtTime(xfGains(n === 1 ? to : from)[idx], when);
      for (let i = 1; i < n; i++) {
        p.linearRampToValueAtTime(xfGains(from + (to - from) * (i / (n - 1)))[idx], when + dur * (i / (n - 1)));
      }
    }
    return when;
  }

  function eqEl(deck, band) {
    return ui.query(`.eq-knob[data-deck="${deck}"][data-band="${band}"]`);
  }
  function fxBtn(deck, type) {
    return ui.query(`.fx-type-btn[data-deck="${deck}"][data-type="${type}"]`);
  }
  function loopBtn(deck) {
    return ui.query(`.deck-btn[data-deck="${deck}"][data-action="loop-toggle"]`);
  }

  function setRange(el, v) {
    if (!el) return;
    el.value = String(v);
    ui.fire(el, "input", true);
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
    const d = host.decks && host.decks[deck];
    const bpm = d && d.bpm > 0 ? d.bpm : 128;
    return 240000 / bpm;
  }

  function setLoopLength(deck, beats) {
    const d = host.decks && host.decks[deck];
    if (d && typeof d.setLoopBeats === "function") d.setLoopBeats(beats);
    const v = ui.el(`loop-value-${deck}`);
    if (v) v.textContent = String(beats);
  }

  function setLoop(deck, on) {
    const d = host.decks && host.decks[deck];
    const btn = loopBtn(deck);
    if (!d || !btn) return;
    if (!!d.loopOn !== on) btn.click();
  }

  function setFx(deck, type, wet) {
    const btn = fxBtn(deck, type);
    if (btn) btn.click();
    if (wet != null) setRange(ui.query(`.fx-wet[data-deck="${deck}"]`), wet);
  }

  // Put a deck back to neutral so it is clean when it becomes the staging deck.
  // Level-match the incoming song to the playing one (trim knob, like a DJ
  // gain-staging on the mixer): vibe gate reports gain_match_db = playing RMS
  // minus candidate RMS. Clamped to the knob's range (-12 dB .. +6 dB).
  function matchGain(outDeck, inDeck, db) {
    const knob = (d) => ui.query(`.gain-knob[data-deck="${d}"]`);
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
    setRange(ui.query(`.pitch-fader[data-deck="${deck}"]`), 0);
  }

  const MASHUP_VOX = 0.7;     // B's voice under A's music but never buried (user)
  // {entry, M, why, variant} when a mashup transition fits A -> B, else null. t0: the transition's audio time
  // (null at booking). Artist variants (stem-moves.js, batch B): a sung vocal over clashing keys may still ride
  // A's drums alone (S9 drums host, owner's drumsOnlyKeyWaiver); no room left in A for the full mashup: a
  // filtered loop of A's last vocal-free bars under B's vocal (S1 filter loop).
  let lastVariantTag = "";   // the variant line is logged when it changes, not on every poll
  function mashupFits(od, idk, t0) {
    const ve = idk._vocalEntry;
    if (!od.stemsReady || !idk.stems || !ve || ve.entry == null || !od.bpm || !idk.bpm) return null;
    const aEff = od.bpm * od._playbackRate();
    const gap = Math.abs(aEff / idk.bpm - 1);
    if (gap > keyLockLim()) return null;
    if (gap > 0.02 && !(idk.tempoStems && Math.abs(idk.tempoStems.bpm / aEff - 1) < 0.01)) return null;
    const cs = host.mod.djMind && host.mod.djMind.core && host.mod.djMind.core.camelotScore;
    const ka = od.analysis && od.analysis.key && od.analysis.key.camelot, kb = idk.analysis && idk.analysis.key && idk.analysis.key.camelot;
    const keyOk = !cs || !ka || !kb || cs(ka, kb) >= 0.8;
    const barS = 240 / aEff;
    const aLeft = od.buffer ? (od.buffer.duration - od._currentPosition()) / od._playbackRate() : 0;
    const M = ve.vocal32 >= 0.7 && aLeft >= 44 * barS ? 32 : aLeft >= 26 * barS && ve.vocal16 >= 0.5 ? 16 : 0;
    const sm = host.mod.stemMoves, pA = Number.isFinite(t0) && od._positionAt ? od._positionAt(t0) : null;
    const choose = sm && sm.core && sm.core.mashupVariant;
    const v = choose ? choose({ keyOk, rap: !!ve.rap, M,
      drums: () => (sm.drumsHostFits ? sm.drumsHostFits(od, idk, M, pA) : null),
      loop: () => (sm.filterLoopFits ? sm.filterLoopFits(od, idk, ve, MASHUP_VOX, pA) : null) })
      : { name: keyOk || ve.rap ? (M ? "mashup" : null) : null, why: "no variant chooser" };
    const tag = `variant: ${v.name || "none"} for ${od.id.toUpperCase()} -> ${idk.id.toUpperCase()}: ${v.why}`;
    if (tag !== lastVariantTag) { lastVariantTag = tag; console.info(tag); }
    if (!v.name) return null;
    if (v.name !== "mashup") return { entry: ve.entry, M: v.fit.M, variant: v.fit, why: v.fit.why };
    return { entry: ve.entry, M, why: `${M}-bar mashup: B's ${ve.rap ? "rap" : "vocal"} over A's instrumental${gap > 0.02 ? `, B key-locked ${(gap * 100).toFixed(0)} %` : ""}, then B's beat on the line` };
  }

  // SONG MERGE needs what a mashup needs except a key/vocal verdict (each combo is
  // judged on its own: stem-moves mergeRank): stems on both, B's entry line, B at
  // A's tempo (key-locked stems when they differ), room for M bars + 8.
  function mergeFits(od, idk) {
    const ve = idk._vocalEntry;
    if (!od.stemsReady || !idk.stems || !ve || ve.entry == null || !od.bpm || !idk.bpm) return null;
    const aEff = od.bpm * od._playbackRate(), gap = Math.abs(aEff / idk.bpm - 1);
    if (gap > keyLockLim()) return null;
    if (gap > 0.02 && !(idk.tempoStems && Math.abs(idk.tempoStems.bpm / aEff - 1) < 0.01)) return null;
    const barS = 240 / aEff;
    const aLeft = od.buffer ? (od.buffer.duration - od._currentPosition()) / od._playbackRate() : 0;
    const M = ve.vocal32 >= 0.7 && aLeft >= 44 * barS ? 32 : aLeft >= 26 * barS ? 16 : 0;
    return M ? { entry: ve.entry, M, rap: !!ve.rap, gap } : null;
  }
  // Mean RMS per stem over [songT, songT + bars) of deck d (null: no decoded stems).
  function stemMeans(d, songT, bars) {
    const sm = host.mod.stemMoves, e = sm && sm.stemEnergyBars ? sm.stemEnergyBars(d, songT, 240 / (d.bpm || 128), bars) : null;
    if (!e) return null;
    const m = {};
    for (const n of ["drums", "bass", "vocals", "other"]) m[n] = e[n].reduce((a, b) => a + b, 0) / (e[n].length || 1);
    return m;
  }
  // The gate that stopped the hold plan (or every merge): one console line the sim parses
  // ("merge gate: <gate>: <why>[; classic merge]") and one step for the live step log.
  let lastMergeGate = null;   // the last gate logged, for the on-demand refusal (mergeNow)
  function mergeGateLog(deck, gate, why, classic, tried) {
    lastMergeGate = { gate, why, classic: !!classic };
    console.info("merge gate:", `${gate}: ${why}${classic ? "; classic merge" : ""}`);
    host.log.step("merge_gate", { deck, decision: classic ? "classic merge" : "refused", why: `${gate}: ${why}`, result: { gate, tried: tried || [] } });
  }
  // MERGE -> HOLD -> TRANSITION (user: "best transition is when tracks merge and play"):
  // the preferred plan whenever the gates pass. stem-moves core.holdPlan picks the hold
  // (whole 8-bar phrases, measured stems + voices) and checks tempo, key >= 0.8, room,
  // clean stems, one sub-bass owner and the level floor. -> {plan} | {gate, why, tried}.
  // bFallback: B's start when the vocal-entry fetch has not landed (measured stems decide).
  // prefer (a forced plan's stored merge): {entry, M, label}; B enters on the stored line
  function planHold(od, idk, aT, bFallback, sm, prefer = null) {
    const no = (gate, why) => ({ gate, why, tried: [] });
    if (!sm.core.holdPlan) return no("module", "no holdPlan");
    if (!od.stemsReady || !idk.stems || !od.bpm || !idk.bpm) {
      // say which song is waiting (owner): the fallback reads "stems for B still rendering"
      const who = !idk.stems ? "B (incoming)" : !od.stemsReady ? "A (playing)" : null;
      return no("stems", who ? `stems for ${who} still rendering` : "no BPM on both decks");
    }
    const ve = idk._vocalEntry, entry = prefer && Number.isFinite(prefer.entry) ? prefer.entry : ve && ve.entry != null ? ve.entry : bFallback;
    if (entry == null) return no("entry", "no entry line for B");
    const aEff = od.bpm * od._playbackRate(), gap = Math.abs(aEff / idk.bpm - 1);
    if (gap > keyLockLim() || (gap > 0.02 && !(idk.tempoStems && Math.abs(idk.tempoStems.bpm / aEff - 1) < 0.01))) {
      return no("tempo", gap > keyLockLim() ? `gap ${(gap * 100).toFixed(1)} % over the ${(keyLockLim() * 100).toFixed(0)} % cap`
        : `gap ${(gap * 100).toFixed(1)} % needs key-locked tempo stems at A's tempo, B has none`);
    }
    const barA = 240 / od.bpm, barB = 240 / idk.bpm, barS = 240 / aEff;
    const roomBars = od.buffer ? (od.buffer.duration - aT) / barA : 0;
    const n = Math.max(1, Math.min(sm.core.HOLD_MAX_PHRASES, Math.floor((roomBars - 10) / 8))) * 8 + 10;
    const cs = host.mod.djMind && host.mod.djMind.core && host.mod.djMind.core.camelotScore;
    const ka = od.analysis && od.analysis.key && od.analysis.key.camelot, kb = idk.analysis && idk.analysis.key && idk.analysis.key.camelot;
    const eA = sm.stemEnergyBars(od, aT, barA, n), eB = sm.stemEnergyBars(idk, entry, barB, n);
    const hp = sm.core.holdPlan({
      gap, keyScore: cs && ka && kb ? cs(ka, kb) : null, roomBars, barS, aT, bT: entry, barA, barB, bRap: !!(ve && ve.rap), eA, eB,
      aVox: od.analysis && od.analysis.vocal_active_regions, bVox: idk.analysis && idk.analysis.vocal_active_regions,
      aud: (() => { const aA = sm.stemEnergyBars(od, aT, barA, n, "audible"), aB = sm.stemEnergyBars(idk, entry, barB, n, "audible"); return aA && aB ? { aA, aB } : null; })(),
      preferM: prefer && prefer.M, preferLabel: prefer && prefer.label,
    });
    if (!hp.ok) return hp;
    return { plan: { entry, M: hp.M, rap: !!(ve && ve.rap), gap, ranked: hp.ranked, pick: hp.pick, aT, heard: false, hold: hp, phases: hp.phases, holdE: { eA, eB },
                     baseM: (mergeFits(od, idk) || {}).M || null } };
  }
  // Plan the merge for A -> B at A's song time aT: the hold plan first; when a gate refuses
  // it (the reason is logged for the sim), the classic fixed-length merge below; then the
  // silent ear re-ranks the top 3 in the background (offline clips, nothing plays) before
  // the transition fires. Stored on B's deck as _mergePlan.
  function planMerge(aId, bId, od, idk, aT, bFallback = null, prefer = null) {
    const sm = host.mod.stemMoves;
    if (!sm || !sm.core.mergeRank) return null;
    const hd = planHold(od, idk, aT, bFallback, sm, prefer);
    let plan = hd.plan || null;
    if (!plan) {
      const mf = mergeFits(od, idk);
      mergeGateLog(activeDeck, hd.gate, hd.why, !!mf, hd.tried);
      if (!mf) return null;
      const cs = host.mod.djMind && host.mod.djMind.core && host.mod.djMind.core.camelotScore;
      const ka = od.analysis && od.analysis.key && od.analysis.key.camelot, kb = idk.analysis && idk.analysis.key && idk.analysis.key.camelot;
      const ranked = sm.core.mergeRank({ eA: stemMeans(od, aT, mf.M), eB: stemMeans(idk, mf.entry, mf.M),
        keyScore: cs && ka && kb ? cs(ka, kb) : null, bRap: mf.rap });
      if (!ranked.length) { mergeGateLog(activeDeck, "no_combo", "no stem combination plays", false); return null; }
      plan = { ...mf, ranked, pick: ranked[0], aT, heard: false };
    }
    const mf = plan;
    const ranked = plan.ranked;
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
        // the ear may not pick a combo whose stems stop playing inside the hold
        if (plan.holdE) {
          const clean = plan.ranked.filter((r) => !sm.core.holdUnclean(r.combo, plan.holdE.eA, plan.holdE.eB, plan.M / 8));
          if (clean.length) plan.ranked = clean;
        }
        plan.pick = plan.ranked[0];
        plan.heard = true;
        console.info("merge (silent ear):", plan.ranked.slice(0, 3).map((r) => `${r.label} ${r.score}${r.ear ? ` ear ${r.ear.score}` : ""}`).join(" | "));
      })
      .catch(() => {});
    return plan;
  }

  const recipeKind = autopilotCore.recipeKind;

  /**
   * Run a recipe-aware, EQ-first transition from `out` to `inn`.
   * Assumes `inn` was cued at b_time and starts playing at t0.
   * `xfDuration` (seconds) is the caller's budget: < 16 means an early bail,
   * so every bar count is halved to keep the set moving.
   * Returns total duration in ms; the outgoing deck may be stopped after that.
   */
  // The move executeTransition actually ran (label for the track event: a Stem Bridge
  // that was refused and fell to the EQ path must not be logged as a Stem Bridge).
  let executedMove = null;
  function executeTransition(recipe, out, inn, xfDuration, t0Audio) {
    executedMove = recipe;
    clearRun();
    xT0 = Number.isFinite(t0Audio) ? t0Audio : audioCtx.currentTime;
    let kind = recipeKind(recipe);    // "echo" when a drums-host mashup on clashing keys is refused at the line
    // a Quick Cut booked under the Punjabi scene profile really cuts (every other cut runs as a bass swap)
    if (recipe === "Quick Cut" && bookedProfileCut) kind = "profile-cut";
    bookedProfileCut = false;
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
      const smM = host.mod.stemMoves, odM = host.decks && host.decks[out], idM = host.decks && host.decks[inn];
      const mp = idM && idM._mergePlan;
      if (recipe === "Stem Merge" && smM && smM.mergeTransition && mp && odM) {
        ["low", "mid", "high"].forEach((b) => { setRange(eqEl(out, b), 0); setRange(eqEl(inn, b), 0); });
        const hp = mp.hold ? mp.phases : null;
        const ph = hp ? `merge ${mp.pick.label}; hold ${hp.hold.bars} bars (${hp.hold.phrases} phrases, ${hp.hold.seconds} s); ` +
          `handover at bar ${mp.M}, ${hp.handover.bars} bars, vocal overlap ${hp.handover.vocal_overlap}` : undefined;
        let M = mp.M, phases = hp;
        let secs = smM.mergeTransition(out, inn, xT0, mp.entry, M, mp.pick, ph, mp.ranked);
        if (!(secs > 0) && mp.hold && mp.baseM && mp.baseM !== mp.M) {
          // the measured hold failed the level gate at fire time: the classic fixed-length merge, never a worse move
          M = mp.baseM; phases = null;
          mergeGateLog(out, "level", `hold of ${mp.M} bars refused at fire time`, true);
          secs = smM.mergeTransition(out, inn, xT0, mp.entry, M, mp.pick, undefined, undefined);
        }
        if (secs > 0) {
          runMergeFader(smM, M, 240 / (odM.bpm || 128) / odM._playbackRate());
          executedMove = "Stem Merge";
          if (phases) {
            const barS = 240 / (odM.bpm || 128) / odM._playbackRate();
            console.info("merge phases:", JSON.stringify({ merge_start: phases.merge_start, hold: phases.hold, handover: phases.handover }));
            host.log.step("merge_start", { deck: out, decision: "merge_start", why: phases.merge_start.combo, result: { ...phases.merge_start, seconds: Math.round(phases.merge_start.bars * barS * 10) / 10 } });
            host.log.step("hold", { deck: out, decision: "hold", why: `${phases.hold.phrases} phrases together`, result: phases.hold });
            host.log.step("handover", { deck: out, decision: "handover", why: "sub-bass and kick change hands on the line", result: phases.handover });
            host.bus.emit("vis-moment", { at: xT0 + M * barS, name: "HOLD->DROP", tier: "super", deck: inn, bar: barS });   // renames the MERGE cue's takeover (mascot.js)
          }
          return secs * 1000;
        }
      }
    }

    // MASHUP -> TRANSITION (user): A's instrumental under B's vocal phrase, hold
    // vox, A's beat drops out, B's beat takes over on the line, 8-bar crossfade.
    // Needs: stems on both, B's vocal phrase, keys that agree (or B raps), and
    // B on key-locked tempo stems at A's tempo when they differ.
    {
      const sm1 = host.mod.stemMoves, od1 = host.decks && host.decks[out], id1 = host.decks && host.decks[inn];
      const mt = sm1 && od1 && id1 ? mashupFits(od1, id1, xT0) : null;
      if (mt && kind !== "double" && kind !== "profile-cut") {
        ["low", "mid", "high"].forEach((b) => { setRange(eqEl(out, b), 0); setRange(eqEl(inn, b), 0); });
        const secs = sm1.mashupTransition(out, inn, xT0, mt.entry, mt.M, MASHUP_VOX, mt.why, mt.variant || null);
        if (secs > 0) {
          runMergeFader(sm1, mt.M, 240 / (od1.bpm || 128) / od1._playbackRate());   // B's voice fades in with the fader
          executedMove = "Mashup → Transition";
          return secs * 1000;
        }
        if (mt.variant && mt.variant.kind === "drums_host") kind = "echo";   // keys clash: only the key-safe stem bridge / echo path
      }
    }

    // Tempo gap, both songs have stems: STEM BRIDGE instead of an echo-out (user:
    // "Echo Out is painful"). Strip A, hold its voice, B's pads in beatless, the
    // crossfader sweeps across the beatless stretch, B's beat drops on its own line.
    {
      const sm0 = host.mod.stemMoves, od0 = host.decks && host.decks[out], id0 = host.decks && host.decks[inn];
      if (od0 && !od0.stemsReady && od0.rearmStems) od0.rearmStems("stem bridge");
      if (sm0 && (kind === "echo" || recipe === "Stem Bridge") && od0 && id0 && od0.stemsReady && id0.stems) {
        ["low", "mid", "high"].forEach((b) => { setRange(eqEl(out, b), 0); setRange(eqEl(inn, b), 0); });
        const secs = sm0.stemBridge(out, inn, xT0, id0.startOffset || 0);
        if (secs > 0) {
          executedMove = "Stem Bridge";
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
    const sm = host.mod.stemMoves;
    const od = host.decks && host.decks[out], idk = host.decks && host.decks[inn];
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
        // [S2] melodic long blend (fx-moves.js): B's mids under A's melody until it resolves, reverb swell on A's
        // exit. Stem blend swap line = bars / 2 (stemBlendPlan). Refused (logged) unless 16+ bars, stems, key >= 0.8.
        const fm = host.mod.fxMoves;
        if (fm && bars >= 16 && kind !== "double" && fm.midBlend(out, inn, { t0: xT0, barS, total: bars, swapBar: bars / 2 })) {
          fm.tailSwell(out, { t0: xT0, barS, total: bars });
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
      case "profile-cut": // [[Quick Cut]] (Punjabi scene profile only; recipeKind never returns it): A out and B in on the line, B's lows open on
        // the same downbeat, so one record owns the sub at every moment; no overlap, no stem handoff
        setAt(lowIn, 0);
        rampParam(xfEl, fromXf, toXf, 12);
        return bar;

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
        // [S5] the last word of A's vocal into a stem echo as the vocal stem mutes (fx-moves.js); the deck-level
        // echo stays the fallback when A has no vocal stem / no line to throw.
        if (!(host.mod.fxMoves && host.mod.fxMoves.vocalThrow(out, inn, { t0: xT0, barS: bar / 1000, swapBar: 4 }))) setFx(out, "echo", 0.7);
        rampParam(xfEl, fromXf, toXf, 8 * bar);
        bassSwapAt(4);
        at(4, () => rampParam(highOut, null, HIGH_SWEEP, 4 * bar));
        total = 8;
        break;

      case "filter": { // sweep A's mids down over 4 bars; lows swap on the bar-4 line
        // [S6] direction from B's first bars (fx-moves.js sweepDir): B brighter -> A washes out low-pass (its
        // highs go), B darker -> A thins out high-pass (its mids go on, its highs stay). Unmeasured: low-pass.
        const dir = host.mod.fxMoves ? host.mod.fxMoves.sweepDir(out, inn, xT0) : "lowpass";
        rampParam(midOut, null, -10, 4 * bar);
        rampParam(xfEl, fromXf, 0, 4 * bar);
        bassSwapAt(4);
        at(4, () => {
          rampParam(xfEl, 0, toXf, 4 * bar);
          if (dir === "highpass") rampParam(midOut, null, LOW_KILL, 4 * bar);
          else rampParam(highOut, null, HIGH_SWEEP, 4 * bar);
        });
        total = 8;
        break;
      }

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
        host.mod.stemMoves && host.mod.stemMoves.eqIntro && kind !== "double") {
      host.mod.stemMoves.eqIntro(out, inn, xT0, (total * bar) / 1000, (swapBar * bar) / 1000);
    }
    if (recipe === "Stem Bridge" || recipe === "Stem Merge") executedMove = "EQ blend";   // the stem move was refused
    return total * bar;
  }

  // One singer through the blend (user: the outgoing vocal goes onto the
  // incoming stems): B enters as its instrumental, A's vocal rides B's beat on
  // the vocal bus, B's own vocal returns as A's fades (stem-moves.js).
  function stemHandoff(kind, out, inn, totalS, swapS = 0) {
    if (!host.mod.stemMoves || kind === "double" || totalS < 4) return false;
    const od = host.decks && host.decks[out], id = host.decks && host.decks[inn];
    if (!od || !id) return false;
    const ka = od.analysis && od.analysis.key && od.analysis.key.camelot;
    const kb = id.analysis && id.analysis.key && id.analysis.key.camelot;
    const core = host.mod.djMind && host.mod.djMind.core;
    const keyScore = core && core.camelotScore ? core.camelotScore(ka, kb) : 0;
    const p0 = od._positionAt ? od._positionAt(xT0) : od._currentPosition();
    const outVocal = host.mod.stemMoves.vocalShare(od.analysis && od.analysis.vocal_active_regions, p0, p0 + totalS);
    if (!od.stemsReady && od.rearmStems) od.rearmStems("vocal handoff");
    const fits = host.mod.stemMoves.core.handoffFits({ outStems: od.stemsReady, inStems: id.stemsReady, keyScore, outVocal });
    return fits && host.mod.stemMoves.handoff(out, inn, xT0, totalS,
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
    const oa = host.decks && host.decks[out];
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
    if (layer.third && host.mod.mashup) {
      later(400, async () => {
        try {
          const t = layer.third;
          if (await host.mod.mashup.play(inn, t.plan, t.host_entry)) {
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
  let preplanFor = null;      // song name while the silent ear pre-plans (read by vibe-ui.js)
  let bookedRecipe = null;    // recipe the booked transition will play (read by vibe-ui.js)
  // Punjabi scene profile (scene-profile.js). profileNext: genre of the song being evaluated /
  // booked as B; bookedProfileCut: the booked Quick Cut is the profile's (executeTransition cuts it).
  let profileNext = "", bookedProfileCut = false;
  const sceneProfile = () => host.mod.sceneProfile || null;
  function punjabiMode() {
    const sp = sceneProfile(), el = ui.el("ap-punjabi-profile");
    return sp ? sp.normalizeMode(el ? el.value : sp.DEFAULT_MODE) : "off";
  }
  function profileLevel() {
    const sp = sceneProfile();
    return sp ? sp.level(punjabiMode(), currentGenre, profileNext) : null;
  }
  // the toggle's state line: mode, and "Punjabi profile active" when a transition used it
  function showProfile(lvl) {
    const el = ui.el("ap-punjabi-state"), sp = sceneProfile();
    if (el && sp) el.textContent = sp.statusLabel(punjabiMode(), lvl);
  }
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
  let topUpPromise = null;   // the look-ahead in flight, so prepareTransition can wait for it
  const TOPUP_WAIT_MS = 30000;
  function topUpPool(afterId) {
    if (toppingUp || !active || ready.length >= 2) return topUpPromise;
    topUpPromise = topUpPoolRun(afterId);
    return topUpPromise;
  }
  async function topUpPoolRun(afterId) {
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
  const measuredById = {};   // measured 1-10 level (energy.py) per track id, learnt at the energy gate
  let dipAsked = false;      // a "dip" note went to the picker: a deliberate fall is allowed until the next song plays
  const playedEnergies = () => playedIds.map((id) => measuredById[id]).filter((v) => Number.isFinite(v)).slice(-8);
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
    const hint = host.mod.djMind && !opts.lookAhead ? host.mod.djMind.nextEnergyNote(setPos, history) : null;
    const energyNote = hint ? hint.note : null;
    const energyHook = hint ? hint.hook : null;
    if (energyNote === "dip") dipAsked = true;
    const data = await ai.suggest({ set_id: setId, track_id: trackId, occasion: occasionWithBridge(opts), ...leadFields(opts), history: history.slice(-30), avoid: avoid.slice(-6), queue: queueNames(), set_position: setPos, set_mode: setMode(), relaxed: !!host.session.relaxed, energy_note: energyNote, energy_hook: energyHook, energy_history: playedEnergies(), lookahead: !!opts.lookAhead,
        variety_run: varietyRun().run, variety_genre: varietyRun().genre,
        tempo_target: bridgeTarget(opts.lookAhead), tempo_note: bridgeNote(opts.lookAhead) || null,
        elapsed_seconds: setStartedAt ? (host.clock.now() - setStartedAt) / 1000 : null, punjabi_profile: punjabiMode() });
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
    if (host.mod.djMind && trackId === currentTrackId) host.mod.djMind.setProfileEnergy(profileById[trackId]);
    // Look-ahead calls describe the NEXT song: they must not overwrite the
    // playing song's energy (it drives the set-mode window).
    if (Number.isFinite(e) && !opts.lookAhead) {
      currentEnergy = e <= 1 ? e * 10 : e;
      if (host.mod.djMind && energyNotedFor !== trackId) { energyNotedFor = trackId; host.mod.djMind.noteEnergy(currentEnergy); }
    }
    return data.suggestions || [];
  }

  async function matchTracks(aId, bId) {
    const res = await fetch("/api/match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // no_cuts: the matcher never hands the autopilot a Hard Cut / Quick Cut (user rule)
      // punjabi_profile: the server resolves the pair's scene from the songs' genres (scene_profile.py)
      body: JSON.stringify({ track_a_id: aId, track_b_id: bId, top_n: 1, no_cuts: true, punjabi_profile: punjabiMode() }),
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
  const pairRejects = new Map();   // "A>B" -> {why, forced}: pairs that failed a pairwise gate (see autopilotCore.pairRejected)
  let failedSearches = 0, failedFor = null; // whole searches (all rounds) that booked nothing for this song
  let emptyStreak = 0;             // consecutive song searches that returned no pick (backoff + library fallback)
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
    const tracks = host.mod.dlJobs
      ? await host.mod.dlJobs.run(s.search_query, label)
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
    syncPrerender();   // its stems and key-locked tempo stems start now, not at the booking
  }

  // ── pre-render: the next songs' stems + key-locked tempo stems are made AHEAD of the booking ──
  // The ranked candidates (the one being tried, the booked one, the pool) go to the server, which makes
  // their stems and the tempo sets they may need (A's tempo now and A's native tempo) one heavy job at
  // a time, and cancels the queued work of a song that left the list (app/ui/services/prerender.py). The answer
  // says what is ready; the pool order prefers ready songs (bounded) and the booking waits for B's stems
  // instead of refusing a merge for a state that is minutes from being fine (awaitBReady).
  const PRERENDER_POLL_MS = 8000;
  let heldPool = [];            // the pool prepareTransition is working through (ready[] is empty meanwhile)
  let focusCand = null;         // the candidate being tried right now: rank 0 for the server
  let prerenderSig = "";        // the last list sent
  let prerenderTimer = null;
  let prerenderBusy = false;
  let deferNote = null;         // while the booking waits: "B's stems" (VIBE strip)
  const rdyById = {};           // track_id -> the server's readiness row (+ seen_at)
  const aTempoOf = (d) => autopilotCore.aTempoAtEntry({ bpm: d.bpm, rate: d._playbackRate(), pitchPct: d._pitchPercent });
  function poolRanked() {
    const seen = new Set(), out = [];
    const bk = atlasBackup && atlasBackup.cand && autopilotCore.backupNeedsStems(atlasBackup.cand.plan) ? [atlasBackup.cand] : [];
    for (const c of [focusCand, scheduledNext, ...ready, ...heldPool, ...bk]) {
      if (c && c.track_id && !c._dead && !history.includes(c.name) && !seen.has(c.track_id)) { seen.add(c.track_id); out.push(c); }
    }
    return out;
  }
  function prerenderItems() {
    const d = host.decks && host.decks[activeDeck];
    const aEff = d && d.bpm > 0 ? aTempoOf(d).bpm : 0, aNative = (d && d.bpm) || 0;
    // PLAY MACRO: the macro's next songs first (their stems early), then the pool
    const mm = host.mod.macroMode, ahead = mm && mm.upcoming ? mm.upcoming() : [];
    const seen = new Set(ahead.map((c) => c.track_id));
    return ahead.concat(poolRanked().filter((c) => !seen.has(c.track_id))).map((c) => ({ track_id: c.track_id,
      bpms: autopilotCore.prerenderTargets({ aEff, aNative, bBpm: c.bpm, lim: keyLockLim() }) }));
  }
  function noteReady(row) {
    const prev = rdyById[row.track_id] || {};
    const now = host.clock.now();
    rdyById[row.track_id] = Object.assign({}, row, { seen_at: now });
    const c = poolRanked().find((x) => x.track_id === row.track_id);
    const name = c ? c.name : row.track_id;
    for (const [stage, on, was] of [["stems", row.stems, prev.stems], ["tempo stems", row.ready, prev.ready]]) {
      if (!on || was) continue;
      host.log.step("prepare_ready", { track_id: row.track_id, phase: "planning", decision: `${stage} ready`, why: name,
        result: { stage, at: now, asked_at: row.asked_at, stems_at: row.stems_at, tempo_at: row.tempo_at, rank: row.rank } });
    }
  }
  async function syncPrerender(force) {
    if (!active || prerenderBusy) return;
    const items = prerenderItems(), sig = JSON.stringify(items);
    const allReady = items.every((i) => rdyById[i.track_id] && rdyById[i.track_id].ready);
    if (!force && sig === prerenderSig && allReady) return;
    prerenderSig = sig;
    prerenderBusy = true;
    try {
      const res = await fetch("/api/prerender", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items }) });
      if (!res.ok) return;
      const data = await res.json();
      for (const row of data.items || []) noteReady(row);
    } catch (e) { /* the server is restarting: the next poll retries */ }
    finally { prerenderBusy = false; }
    if (!prerenderTimer && active) prerenderTimer = setInterval(() => { if (active) syncPrerender(); }, PRERENDER_POLL_MS);
  }
  function stopPrerender() {
    if (prerenderTimer) { clearInterval(prerenderTimer); prerenderTimer = null; }
    heldPool = []; focusCand = null; deferNote = null; prerenderSig = "";
    fetch("/api/prerender", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items: [] }) }).catch(() => {});
  }
  const candReady = (c) => !!(rdyById[c.track_id] && rdyById[c.track_id].ready);

  // Match + gates + load + schedule one downloaded candidate. True = scheduled.
  // Can `cand` be tempo-locked to the playing deck (half/double time counts)?
  // +/-8 % on pitch; +/-15 % when the playing deck has stems: the next song
  // then plays on key-locked tempo stems (multi-BPM stem sets), no pitch shift.
  function stemsOn() { const d = host.decks && host.decks[activeDeck]; return !!(d && d.stems); }
  // key-locked stems range, shared with tempo-rule.js (+-16 %: 174 -> 125 is 28 %, never locks)
  function keyLockLim() { return ((host.mod.tempoRule && host.mod.tempoRule.KEYLOCK_RANGE_PCT) || 8) / 100; }
  function lockLimit() { return stemsOn() ? keyLockLim() : 0.08; }
  function tempoLockableAt(cand, lim) {
    const d = host.decks && host.decks[activeDeck];
    if (!d || !d.bpm || !cand.bpm) return true;
    const aEff = d.bpm * d._playbackRate();
    return autopilotCore.tempoLockableAt(aEff, cand.bpm, lim);
  }
  function tempoLockable(cand) {
    const d = host.decks && host.decks[activeDeck];
    if (!d || !d.bpm || !cand.bpm) return true; // unknown: let the matcher decide
    return autopilotCore.tempoLockableAt(d.bpm * d._playbackRate(), cand.bpm, lockLimit());
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
  // host.session ({relaxed}) is owned by the host
  function applySessionMood() {
    const relaxed = isRelaxedOccasion(occasion);
    host.session.relaxed = relaxed;
    const modeEl = ui.el("ap-mode");
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
  const leadStatusEl = ui.el("ap-lead-status");
  const leadBox = ui.el("ap-lead");
  const leadCancel = ui.el("ap-lead-cancel");

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
    const input = ui.el("ap-lead-input");
    const text = picked ? picked.title : (input ? input.value.trim() : "");
    hideLeadResults();
    if (!text) { leadStatus("Type a song ('Artist - Title'), an artist or a genre"); return; }
    if (!active) { leadStatus("Start a set first; LEAD steers a running set"); return; }
    const stepsEl = ui.el("ap-lead-steps");
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
        const tracks = host.mod.dlJobs ? await host.mod.dlJobs.run(picked.url, `LEAD TO: ${text}`)
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
    const d = host.decks && host.decks[activeDeck];
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
    const d = host.decks && host.decks[activeDeck];
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

  function pairKeyScore(od, sd) {
    const cs = host.mod.djMind && host.mod.djMind.core && host.mod.djMind.core.camelotScore;
    const ka = od.analysis && od.analysis.key && od.analysis.key.camelot, kb = sd.analysis && sd.analysis.key && sd.analysis.key.camelot;
    return cs && ka && kb ? cs(ka, kb) : null;
  }
  // The booking waits for B's stems and key-locked tempo stems (bounded by autopilotCore.deferBudgetS: to
  // the earliest exit minus the lead the plan needs, at most 100 s). Waiting is pointless, and skipped, when
  // no merge could happen (A has no stems, keys clash, the tempo gap is past the cap). On timeout the
  // booking goes on exactly as before (the merge gate refuses, the old chain plays). Logs
  // `merge deferred: waiting for <stems|tempo stems>` and how long it waited; returns the evidence the
  // step log and the sim read, or null when merges are off / no deck.
  async function awaitBReady(currentId, nextId, nextName, candidate, gen) {
    const od = host.decks && host.decks[activeDeck], sd = host.decks && host.decks[stagingDeck()];
    if (!od || !sd || !mergesOn() || !(od.bpm > 0)) return null;
    const probe = () => {
      if (od.stems && !od.stemsReady && od.rearmStems) od.rearmStems("readiness check");
      const at = aTempoOf(od), aEff = at.bpm;
      return { aEff, r: autopilotCore.readinessNeeds({ aStems: !stemsWhy(od), bStems: !!sd.stems, aEff, bBpm: sd.bpm,
        tempoStemsBpm: sd.tempoStems && sd.tempoStems.bpm, lim: keyLockLim(), keyScore: pairKeyScore(od, sd),
        aSettled: at.settled || !!bridge }) };
    };
    let p = probe();
    const res = { firstReady: p.r.needs.length === 0 && !p.r.skip, deferS: 0, gaveUp: false, skip: p.r.skip, needs: p.r.needs };
    const budget = autopilotCore.deferBudgetS({ nowPos: deckPosition(activeDeck), exitLo: exitWindow(candidate.score || 50).lo });
    const t0 = host.clock.now();
    let waited = 0, lastTry = -1e9;
    let d = autopilotCore.deferDecision({ needs: p.r.needs, waitedS: 0, budgetS: budget });
    if (p.r.needs.length) {
      console.info("merge deferred:", `${d.why} (budget ${budget.toFixed(0)} s)`);
      host.log.step("merge_deferred", { track_id: nextId, phase: "planning", decision: d.wait ? "deferred" : "not deferred", why: `${nextName}: ${d.why}`, result: { needs: p.r.needs, budget_s: budget } });
    }
    while (d.wait) {
      deferNote = `B's ${p.r.needs.join(" and ")}`;
      host.session.deferring = true;         // learned-moves.js: no slice move while B's stems load (loop_extend may fill)
      apStatus(`Waiting for ${nextName}: ${p.r.needs.join(" and ")} (${Math.round(waited)} s of ${Math.round(budget)} s)`);
      // native stems on the deck but the tempo set not asked for (or the last try failed): ask now
      if (p.r.needs.includes("tempo stems") && sd.stems && sd.useTempoStems && host.clock.now() - lastTry > 15000) {
        lastTry = host.clock.now();
        sd._tempoStemsJob = sd.useTempoStems(p.aEff);
      }
      await new Promise((r) => setTimeout(r, 1000));
      if (!active || currentTrackId !== currentId || (gen !== undefined && gen !== prepGen)) break;
      prepStartedAt = host.clock.now();      // waiting is progress, not a stalled search (watchdog)
      p = probe();
      waited = (host.clock.now() - t0) / 1000;
      d = autopilotCore.deferDecision({ needs: p.r.needs, waitedS: waited, budgetS: budget });
    }
    deferNote = null;
    host.session.deferring = false;
    res.deferS = waited; res.needs = p.r.needs; res.gaveUp = p.r.needs.length > 0;
    if (waited > 0) {
      console.info("merge deferred:", res.gaveUp ? d.why : `waited ${waited.toFixed(0)} s, B ready`);
      host.log.step("merge_deferred", { track_id: nextId, phase: "planning", decision: res.gaveUp ? "gave up" : "ready", why: `${nextName}: ${res.gaveUp ? d.why : `waited ${waited.toFixed(0)} s`}`, result: { waited_s: waited, needs: p.r.needs } });
    }
    const rd = rdyById[nextId] || {};
    res.stems = !!sd.stems; res.tempo = sd.tempoStems ? sd.tempoStems.bpm : null;
    console.info("prepare ready:", `${nextName} at_booking=${res.firstReady ? 1 : 0} defer_s=${waited.toFixed(0)} stems=${res.stems ? 1 : 0} tempo=${res.tempo ? 1 : 0} gave_up=${res.gaveUp ? 1 : 0} skip=${res.skip ? 1 : 0}`);
    host.log.step("prepare_ready", { track_id: nextId, phase: "planning", decision: res.gaveUp ? "not ready at booking" : res.firstReady ? "ready at booking" : "ready after waiting",
      why: nextName, result: { at_booking: res.firstReady, defer_s: waited, stems: res.stems, tempo_stems_bpm: res.tempo, skip: res.skip,
        stems_at: rd.stems_at || null, tempo_at: rd.tempo_at || null, asked_at: rd.asked_at || null } });
    return res;
  }

  async function evaluateCandidate(currentId, cand, gen) {
    if (!active || !cand) return false;
    const nextId = cand.track_id;
    const nextName = cand.name;
    profileNext = (cand.suggestion && cand.suggestion.genre) || cand.genre || "";
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
    // ONE artist-spacing rule for every path that ends here (suggest pool, combo, macro,
    // atlas, library fallback): "it's going back to playing Fred again". The last round
    // may exceed the window cap, never the gap (artist-spacing.js, = autopilot_service.py).
    const spacing = host.mod.artistSpacing && host.mod.artistSpacing.spacingBlock(nextName, history, forceJump);
    if (spacing) {
      apStatus(`Not after this song: ${nextName} (${spacing})`);
      console.log(`[spacing] skipped ${nextName}: ${spacing}`);
      return false;
    }
    if (cand.duration && cand.duration < minSongSecs()) {
      apStatus(`Skipping ${nextName}: ${fmtTime(cand.duration)} is too short for a ${setMode().toUpperCase()} set`);
      return false;
    }

    const known = autopilotCore.pairRejected(pairRejects, currentId, nextId, forceJump);
    if (known) {
      apStatus(`Not after this song: ${nextName} (${known.why}) — already checked, kept for later`);
      cand.keep = true;
      return false;
    }
    apStatus(`Matching transition → ${nextName}…`);
    let candidate = await matchTracks(currentId, nextId);
    if (!candidate) return false;
    // the pair atlas / macro step's plan is the default (macro-mode.js); every gate below re-validates it
    if (host.mod.macroMode) candidate = host.mod.macroMode.defaultPlan(currentId, cand, candidate);

    // Measured vibe gate: reject candidates whose loudness / brightness /
    // onset density / energy sit too far from what is playing right now.
    if (candidate.vibe && candidate.vibe.ok === false) {
      const why = (candidate.vibe.reasons || []).join("; ") || `distance ${candidate.vibe.distance}`;
      console.warn("Autopilot vibe reject:", nextName, why); host.log.step("candidate_reject", { track_id: nextId, phase: "selection", decision: "vibe reject", why });
      apStatus(`Not after this song: ${nextName} (${why}) — kept for later`);
      autopilotCore.rememberPairReject(pairRejects, currentId, nextId, why, true);   // the vibe gate ignores forceJump
      cand.keep = true; // pairwise: may fit fine after the next song
      return false;
    }

    // Measured energy gate (app/music_brain/energy.py, 1-10 vs the library): the next
    // song stays within 2 levels (1 relaxed), the set arc decides the direction.
    // The last-round fallback allows one more level so the set never stalls.
    const ev = candidate.vibe;
    if (ev && Number.isFinite(ev.energy_a) && Number.isFinite(ev.energy_b)) {
      measuredById[currentId] = ev.energy_a; measuredById[nextId] = ev.energy_b;
      const verdict = autopilotCore.energyStepOk(ev.energy_a, ev.energy_b, {
        relaxed: !!(host.session && host.session.relaxed), songs: history.length, force: forceJump, recent: playedEnergies(), reset: dipAsked,
        rawDelta: Number.isFinite(ev.energy_raw_a) && Number.isFinite(ev.energy_raw_b) ? ev.energy_raw_b - ev.energy_raw_a : null });
      if (!verdict.ok) {
        console.warn("Autopilot energy reject:", nextName, verdict.why); host.log.step("candidate_reject", { track_id: nextId, phase: "selection", decision: "energy reject", why: verdict.why });
        apStatus(`Not after this song: ${nextName} (${verdict.why}) — kept for later`);
        autopilotCore.rememberPairReject(pairRejects, currentId, nextId, verdict.why, forceJump);
        cand.keep = true;
        return false;
      }
      console.info("energy:", nextName, verdict.why);
      // vibe-ui.js: measured energy of the pair the gate just passed
      host.bus.emit("ai-energy", { a: ev.energy_a, b: ev.energy_b, next: nextName });
    }

    // Show match score on the NEXT queue card.
    const scoreEl = ui.el("ap-match-score");
    if (scoreEl) {
      const sc = Math.round(candidate.score || 0);
      const good = sc >= 65;
      scoreEl.textContent = `${good ? "⭐" : "⚡"} ${sc}/100${good ? "" : " · early exit"}`;
      scoreEl.style.cssText = `display:inline;font-weight:700;color:${good ? "#4ade80" : "#f97316"};margin-left:6px`;
    }

    // AI plan for this pair (candidate, exit phrase, DJ-mind moves), fetched
    // while the next track loads. Rules-only if it fails or times out.
    // Stems on A and no peak moves: the plan may turn out skippable once B's stems and
    // the tempo lock are known (planSkipReason), so it is asked after the load below.
    const peakElP = ui.el("ap-peak-toggle"), peakOnP = !peakElP || peakElP.checked;
    const deferPlan = !!(host.decks && host.decks[activeDeck] && host.decks[activeDeck].stems) && !peakOnP;
    // a forced plan (macro step / studied combo / FOLLOW SET) is the plan: no LLM re-pick
    let aiPlan = deferPlan || candidate.forced ? null : requestMindPlan(currentId, nextId, candidate);

    // Preload next track into staging deck
    apStatus(`Loading ${nextName} into deck ${stagingDeck().toUpperCase()}…`);
    const audioRes = await fetch(`/api/audio/tracks/${nextId}`);
    if (!audioRes.ok) return false;
    const blob = await audioRes.blob();
    if (!active) return false;
    await loadIntoDeck(stagingDeck(), nextId, nextName, blob);
    // tempo gap 2-15 %: render its key-locked tempo stems now, long before the blend
    {
      const oa1 = host.decks && host.decks[activeDeck], sd1 = host.decks && host.decks[stagingDeck()];
      if (oa1 && sd1 && oa1.bpm && sd1.useTempoStems) {
        // stems land 40-160 s after the load when the song was just downloaded (measured): the old 20 s
        // wait gave up first and the tempo sets were never asked for. Wait until this deck's song changes.
        const an1 = sd1.analysis;
        const waitStems = async () => { for (let i = 0; i < 300 && !sd1.stems && active && sd1.analysis === an1; i++) await new Promise((r) => setTimeout(r, 500)); };
        waitStems().then(() => {
          if (!sd1.bpm || !sd1.stems || !active || sd1.analysis !== an1) return;
          const aEff1 = aTempoOf(oa1).bpm;     // A's HOME tempo while it is still easing back (not a moving target)
          const m1 = [1, 2, 0.5].reduce((b, m) => (Math.abs(aEff1 / (sd1.bpm * m) - 1) < Math.abs(aEff1 / (sd1.bpm * b) - 1) ? m : b));
          const g1 = Math.abs(aEff1 / (sd1.bpm * m1) - 1);
          if (g1 > 0.02 && g1 <= keyLockLim()) sd1._tempoStemsJob = sd1.useTempoStems(aEff1 / m1);
          // where its vocal phrase starts (for a mashup transition)
          fetch(`/api/tracks/${nextId}/vocal_entry`).then((r) => r.json()).then((v) => { sd1._vocalEntry = v; }).catch(() => {});
        });
      }
    }
    // Over 8 % the blend needs the key-locked stems: book the song only once they're on.
    if (cand.bpm && !tempoLockableAt(cand, 0.08) && tempoLockableAt(cand, keyLockLim())) {
      const sd2 = host.decks && host.decks[stagingDeck()];
      apStatus(`Key-locking ${nextName} to this tempo (tempo stems)…`);
      const t2 = host.clock.now();
      while (sd2 && !sd2._tempoStemsJob && host.clock.now() - t2 < 25000) await new Promise((r) => setTimeout(r, 500));
      const ok2 = sd2 && sd2._tempoStemsJob ? await Promise.race([sd2._tempoStemsJob, new Promise((r) => setTimeout(() => r(false), 90000))]) : false;
      if (!ok2 && !forceJump) {
        apStatus(`Not now: ${nextName} needs key-locked stems that aren't ready — kept for later`);
        cand.keep = true;
        return false;
      }
    }
    let planFitState = null;
    // Plan-before-pick (user): only accept a candidate whose transition is
    // already smooth. Same pure gate scheduleTransition uses (tempoRule.planFit),
    // so pick time and play time can't disagree. Skip while a forced tempo jump
    // is the explicit fallback (allowTempoJump): that path exists to accept the
    // hard Echo Out on purpose when nothing beat-matchable is left.
    {
      const odF = host.decks && host.decks[activeDeck], sdF = host.decks && host.decks[stagingDeck()];
      if (odF && sdF && odF.bpm > 0) {
        if (odF.stems && !odF.stemsReady && odF.rearmStems) odF.rearmStems("plan-fit check");
        const aStemsWhyF = stemsWhy(odF), stemsBothF = !aStemsWhyF && !!sdF.stems;
        const fit = host.mod.tempoRule.planFit({
          aEff: odF.bpm * odF._playbackRate(), bBpm: sdF.bpm, stemsBoth: stemsBothF,
          tempoStemsBpm: sdF.tempoStems && sdF.tempoStems.bpm,
        });
        planFitState = { stemsBoth: stemsBothF, beat: !!fit.beat };
        candidate.plannedFit = fit; // scheduleTransition re-derives with the same fn + live state
        // a forced move keeps its song: the booking falls back to an Echo Out (never a cut)
        if (!fit.smooth && !allowTempoJump && candidate.forced) {
          console.info(`plan-fit: ${nextName} (${fit.why}): stored move kept, the booking falls back`);
        } else if (!fit.smooth && !allowTempoJump) {
          console.warn("Autopilot plan-fit reject:", nextName, fit.why);
          host.log.step("candidate_reject", { track_id: nextId, phase: "selection", decision: "plan-fit reject", why: fit.why });
          apStatus(`Not after this song: ${nextName} (${fit.why}) — kept for later`);
          autopilotCore.rememberPairReject(pairRejects, currentId, nextId, fit.why, forceJump);
          cand.keep = true;
          return false;
        }
      }
    }
    matchGain(activeDeck, stagingDeck(), candidate.vibe && candidate.vibe.gain_match_db);
    if (deferPlan && !candidate.forced) {
      const skip = autopilotCore.planSkipReason({ stemsBoth: !!(planFitState && planFitState.stemsBoth),
        lockBeat: !!(planFitState && planFitState.beat), peakOn: peakOnP });
      if (skip) {
        console.info("AI plan skipped:", `${nextName}: ${skip}`);
        host.log.step("plan_skip", { track_id: nextId, decision: "skip plan LLM", why: skip });
      } else aiPlan = requestMindPlan(currentId, nextId, candidate);
    }
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
    let layer = blend && !candidate.forced ? await requestLayer(currentId, nextId, candidate, plan, cand) : null;
    if (!active || currentTrackId !== currentId) return false;
    if (layer && !(layer.start >= deckPosition(activeDeck) + 16)) layer = null; // start slipped past

    if (gen !== undefined && gen !== prepGen) return false; // superseded by a restarted search
    // B's stems and key-locked tempo stems are on their way (server pre-render): wait for them, bounded,
    // instead of booking a merge-less transition for a state a minute from being fine.
    let prep = null;
    if (!layer) {
      prep = await awaitBReady(currentId, nextId, nextName, candidate, gen);
      if (!active || currentTrackId !== currentId || (gen !== undefined && gen !== prepGen)) return false;
    }
    // The silent ear pre-plans the transition (when B starts inside A, from which
    // of B's lines, how long both play, who owns each stem); the master plays it.
    if (!layer && !candidate.forced) {
      apStatus(`Ear pre-planning the mix into ${nextName}…`);
      // the plan may take a while (renders + the ear): never hold the booking past
      // PREPLAN_WAIT_MS; the server keeps going and caches a heard plan for next time
      preplanFor = nextName;
      const pp = await Promise.race([requestPreplan(currentId, nextId, candidate),
        new Promise((r) => setTimeout(() => r(null), PREPLAN_WAIT_MS))]).finally(() => { preplanFor = null; });
      if (!active || currentTrackId !== currentId || (gen !== undefined && gen !== prepGen)) return false;
      if (pp) candidate = Object.assign({}, candidate, { preplan: pp });
    }
    const fireAt = scheduleTransition(currentId, nextId, nextName, candidate, blend, minExit, layer);
    scheduledFireAt = fireAt;
    if (!layer && !candidate.forced) tryMashup(currentId, nextId, nextName, fireAt); // fire-and-forget; B itself enters under a LAYER
    scheduledNext = cand;
    pendingSugs = []; // leftovers show up as READY when their download lands
    showQueue();
    topUpPool(nextId); // fire-and-forget: songs for AFTER the next one
    return true;
  }

  // Exit window (track seconds) for the current song, same maths as scheduleTransition.
  function exitWindow(score) {
    const w = playWindow(score);
    const od = host.decks && host.decks[activeDeck];
    const trackEnd = (od && od.buffer ? autopilotCore.audibleEnd(od.analysis, od.buffer.duration) : Infinity) - w.xf - 2;
    return { lo: Math.min(entryPos + w.min, trackEnd), hi: Math.min(entryPos + w.max, trackEnd) };
  }

  // Beat-to-beat blend plan: vocal-free exit phrase in the playing song,
  // vocal-free entry phrase in the next one, and B's tempo-lock rate.
  // null -> fall back to the matcher's points (still phrase + tempo aligned).
  async function requestBlend(currentId, nextId, candidate) {
    const win = exitWindow(candidate.score || 50);
    const od = host.decks && host.decks[activeDeck];
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
      const peakEl = ui.el("ap-peak-toggle");
      if (host.mod.djMind && host.mod.djMind.planPeak && (!peakEl || peakEl.checked)) {
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
    const mind = host.mod.djMind;
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
    const od = host.decks && host.decks[activeDeck];
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
    const d = host.decks && host.decks[deckId];
    if (!d) return;
    const v = Math.max(-range, Math.min(range, pct));
    // gradient rule: instant only while B is silent, else a glide
    if (typeof d.aiSetPitch === "function") d.aiSetPitch(v);   // the ramping setter: no bare jump
    const fader = ui.query(`.pitch-fader[data-deck="${deckId}"]`);
    if (fader) fader.value = String(v.toFixed(1));
    const readout = ui.el(`pitch-readout-${deckId}`);
    if (readout) readout.textContent = `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;
  }

  // Pitch fader + readout follow the deck's live rate (cosmetic only). Writes
  // .value without an "input" event, so the fader's listener never sets the
  // rate (a real user drag still does, and cancels the glide: deck._applyRate).
  function showPitch(deckId, pct) {
    const fader = ui.query(`.pitch-fader[data-deck="${deckId}"]`);
    if (fader) fader.value = String(pct.toFixed(1));
    const readout = ui.el(`pitch-readout-${deckId}`);
    if (readout) readout.textContent = `${pct > 0 ? "+" : ""}${pct.toFixed(1)}%`;
  }
  function followPitch(deckId, untilAudioT) {
    const d = host.decks && host.decks[deckId];
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
    const d = host.decks && host.decks[deckId];
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
    if (!host.mod.djMind || !host.mod.djMind.requestPlan) return null;
    const win = exitWindow(candidate.score || 50);
    if (!(win.hi > win.lo)) return null;
    // Never wait past the point where the transition must be booked.
    const pos = deckPosition(activeDeck);
    apStatus("AI planning the next transition…");
    return host.mod.djMind.requestPlan(currentId, nextId, candidate, Object.assign(win, {
      setPosition: Math.min(history.length / 10, 1.0),
      mashupPossible: mashupsOn() && !!host.mod.mashup,
      deadlineS: Math.max(win.lo, win.hi - 30) - pos - 20,
    }));
  }

  // ── preplanned backup B (atlas partners) ─────────────────────────────────
  // Fetched as soon as A plays (prepareTransition start), re-ranked when stale (backupStale),
  // its analysis warmed and, when its plan plays stems, its stems prerendered (poolRanked).
  let atlasBackup = null;     // {for, played, energyA, list, skipped, cand}
  let backupPromise = null;
  function preplanBackup(currentId) {
    const ctx = { aId: currentId, played: playedIds, energyA: measuredById[currentId] };
    if (!autopilotCore.backupStale(atlasBackup, ctx)) return Promise.resolve(atlasBackup);
    if (backupPromise && backupPromise.for === currentId) return backupPromise;
    const p = (async () => { try {
      let data = {};
      try { data = await (await fetch(`/api/atlas/backup?a=${encodeURIComponent(currentId)}&n=40&set_id=${encodeURIComponent(setId || "")}`)).json(); }
      catch (e) { data = {}; }
      const energyA = Number.isFinite(measuredById[currentId]) ? measuredById[currentId] : data.a_level;
      const r = autopilotCore.rankAtlasBackups(data.partners || [], {
        aId: currentId, played: playedIds, recent: history, energyA, songs: history.length,
        relaxed: !!(host.session && host.session.relaxed), recentLevels: playedEnergies(),
        spacing: (n) => (host.mod.artistSpacing ? host.mod.artistSpacing.spacingBlock(n, history, false) : null),
        rejected: (b) => autopilotCore.pairRejected(pairRejects, currentId, b, false) });
      atlasBackup = { for: currentId, played: playedIds.length, energyA: measuredById[currentId], list: r.list, skipped: r.skipped, cand: r.list[0] || null };
      const c = atlasBackup.cand;
      if (c) {
        host.log.step("atlas_backup", { track_id: c.track_id, phase: "planning", decision: `backup ${c.name}`,
          why: `${c.tier}, works ${c.works}${c.level != null ? `, energy ${energyA} -> ${c.level}` : ""}${c.earlier ? ", heard in an earlier set" : ""}` });
        fetch(`/api/tracks/${encodeURIComponent(c.track_id)}/analysis`).catch(() => {});   // warm B's analysis
        if (autopilotCore.backupNeedsStems(c.plan)) syncPrerender();                        // B's stems via the prerender path
      } else if (data.built !== false) {
        const why = r.skipped.slice(0, 3).map((s) => `${s.name}: ${s.why}`).join("; ") || "no atlas partners for this song";
        host.log.step("atlas_backup", { phase: "planning", decision: "no backup", why });
        for (const s of r.skipped.slice(0, 5)) host.log.step("suggest_reject", { track_id: s.b, phase: "selection", decision: `atlas: ${s.name}`, why: s.why });
      }
      return atlasBackup;
    } catch (e) { console.warn("atlas backup:", e.message); return atlasBackup; } })();
    p.for = currentId;
    backupPromise = p;
    p.finally(() => { if (backupPromise === p) backupPromise = null; });
    return p;
  }
  // Deadline fallback, in tier order: the atlas backup (studied combo / atlas combo / best partner,
  // up to 4 tried), then a library song that tempo-locks. Null = nothing booked: HOLD LOOP stays.
  async function deadlineFallback(currentId, gen, reason) {
    const b = await preplanBackup(currentId);
    if (!active || gen !== prepGen) return null;
    for (const c of (b && b.list || []).slice(0, 4)) {
      if (!active || gen !== prepGen) return null;
      if (c.track_id === currentId || playedIds.includes(c.track_id)) continue;
      apStatus(`Backup (${c.tier}): trying ${c.name}`);
      const cand = { track_id: c.track_id, name: c.name, bpm: c.bpm, duration: c.duration, keep: false, fromLibrary: true };
      if (await tryCandidate(currentId, cand, gen)) {
        host.log.step("atlas_fallback", { track_id: c.track_id, phase: "selection", decision: `${currentId}>${c.track_id}`,
          why: `${c.tier}, works ${c.works}${c.level != null ? `, energy level ${c.level}` : ""} (${reason})` });
        host.log.step("deadline_fallback", { phase: "selection", decision: "atlas", why: reason });
        return "atlas";
      }
    }
    if (!active || gen !== prepGen) return null;
    if (await tryLibraryLockable(currentId, gen)) {
      host.log.step("deadline_fallback", { phase: "selection", decision: "library", why: reason });
      return "library";
    }
    host.log.step("deadline_fallback", { phase: "selection", decision: "hold",
      why: `${reason}: no atlas partner or library song passed the gates${b && b.skipped && b.skipped.length ? ` (${b.skipped.slice(0, 2).map((s) => `${s.name}: ${s.why}`).join("; ")})` : ""}` });
    return null;
  }

  async function tryLibraryLockable(currentId, gen) {
    const d = host.decks && host.decks[activeDeck];
    if (!d || !d.bpm) return false;
    const aEff = d.bpm * d._playbackRate();
    const key = d.analysis && d.analysis.key && d.analysis.key.camelot || "";
    const exclude = [...playedIds, currentId].join(",");
    try {
      // genre: the server only offers library songs known to share the playing
      // song's scene (tempo + key alone paired Barbie Girl with Bicep "Glue")
      const res = await fetch(`/api/library/lockable?bpm=${aEff.toFixed(2)}&key=${encodeURIComponent(key)}&exclude=${encodeURIComponent(exclude)}&max_gap=${lockLimit()}&genre=${encodeURIComponent(currentGenre || "")}&era=${encodeURIComponent(currentEra || "")}&punjabi_profile=${encodeURIComponent(punjabiMode())}&energy=${measuredById[currentId] || 0}`);
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
    focusCand = cand;
    syncPrerender();     // the song being tried is rank 0: its stems and tempo sets come first
    try {
      const ok = await evaluateCandidate(currentId, cand, gen);
      if (!ok && cand && !cand.keep) cand._dead = true;     // rejected for good: no more work for it
      return ok;
    } catch (e) {
      console.warn("Autopilot candidate failed:", cand && cand.name, e.message);
      apStatus(`Skipping ${cand && cand.name}: ${e.message}`);
      if (cand) cand._dead = true;
      return false;
    } finally {
      if (focusCand === cand) focusCand = null;
      deferNote = null;
    }
  }

  // ── core loop ─────────────────────────────────────────────────────────────
  let prepGen = 0;        // bumps on every (re)started next-song search
  let prepStartedAt = 0;  // ms timestamp of the current search
  async function prepareTransition(currentId) {
    if (!active) return;
    const gen = ++prepGen;
    prepStartedAt = host.clock.now();
    showQueue();
    preplanBackup(currentId);    // backup B from the atlas as soon as A plays (no model round trip)

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

    // 0a) Pre-knowledge first (macro-mode.js): a saved macro's next step (MACRO_PREFERENCE of the
    // time), then the atlas COMBOS for this song (the other deck's song first). Taste (the LLM
    // pool below) decides only when none of them passes the gates.
    if (host.mod.macroMode && !leadDue()) {
      const st = host.state || {}, other = activeDeck === "a" ? st.trackB : st.trackA;
      const first = await host.mod.macroMode.firstCandidates(currentId, { played: playedIds, recent: history, aName: history[history.length - 1] || "", aStyle: currentGenre || "", loadedId: other && other !== currentId ? other : null });
      for (let c of first) {
        if (!active || gen !== prepGen) return;
        if (c._download) {        // FOLLOW SET: a studied set's song the library lacks, via the normal suggest -> download path
          apStatus(`FOLLOW SET ${c._follow.set_id}: downloading ${c.name}`);
          try {
            c = Object.assign(await downloadSuggestion(c._download), { _follow: c._follow });
          } catch (e) {
            host.log.step("studied", { phase: "selection", decision: "download failed", why: `studied: ${c.name}: ${e.message}` });
            continue;
          }
          if (!active || gen !== prepGen) return;
        }
        if (c.track_id === currentId || playedIds.includes(c.track_id)) continue;
        apStatus(`${c._macro ? `Macro ${c._macro.name || ""} step ${c._macro.step.n}` : c._follow ? `FOLLOW SET ${c._follow.dj} #${c._follow.position}` : `COMBO ${c._combo.label}`}: trying ${c.name}`);
        if (await tryCandidate(currentId, c, gen)) return;
      }
      if (!active || gen !== prepGen) return;
    }

    // 0b) The look-ahead for THIS song is still in flight (asked when it was booked):
    // its picks are exactly what a fresh suggest would ask for, and the model runs one
    // call at a time, so a second ask only queues behind it. Wait (bounded) instead.
    if (toppingUp && topUpPromise && !ready.length) {
      apStatus("Waiting for the look-ahead picks already in flight");
      await new Promise((resolve) => {
        const id = setTimeout(resolve, TOPUP_WAIT_MS);
        topUpPromise.then(() => { clearTimeout(id); resolve(); }, () => { clearTimeout(id); resolve(); });
      });
      if (!active || gen !== prepGen) return;
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
    // a song whose stems and tempo stems are already made may pass ONE not-ready song (taste still wins)
    if (pool.length > 1) {
      const ordered = autopilotCore.orderByReadiness(pool, candReady);
      if (ordered.some((c, i) => c !== pool[i])) {
        const moved = ordered.filter((c, i) => pool.indexOf(c) > i).map((c) => c.name);
        console.info("pool order:", `readiness moved ${moved.join(", ")} up (${ordered.map((c) => `${c.name}${candReady(c) ? " ready" : ""}`).join(" > ")})`);
        host.log.step("candidate_order", { phase: "selection", decision: "ready song first", why: ordered.map((c) => `${c.name}${candReady(c) ? " (ready)" : ""}`).join(" > ") });
        pool.splice(0, pool.length, ...ordered);
      }
    }
    heldPool = pool;     // ready[] is empty while the pool is tried: keep them in the server's list
    syncPrerender(true); // A's tempo changed with the new song: the tempo sets to make are recomputed
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
    const plan = autopilotCore.searchPlan({
      pos: deckPosition(activeDeck), exitLo: exitWindow(50).lo,
      failedSearches: failedFor === currentId ? failedSearches : 0, emptyStreak });
    if (plan.fallbackFirst) {
      if (await deadlineFallback(currentId, gen, plan.deadline ? "deadline" : "search failed before")) return;
      if (!active || gen !== prepGen) return;
    }
    const rejected = [];
    let deadlineTried = plan.fallbackFirst;
    for (let round = 1; round <= MAX_ROUNDS; round++) {
      // the exit came close while the model was still answering: the backup, not another round
      if (!deadlineTried && autopilotCore.searchPlan({ pos: deckPosition(activeDeck), exitLo: exitWindow(50).lo }).deadline) {
        deadlineTried = true;
        if (await deadlineFallback(currentId, gen, "deadline")) return;
        if (!active || gen !== prepGen) return;
      }
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
        preplanBackup(currentId);   // the model's pick landed: re-rank the backup if its context moved
      } catch (e) {
        aiPicking = false;
        console.warn("Autopilot suggest failed:", e.message);
        apStatus(`Suggest error: ${e.message}`);
        continue;
      }
      pendingSugs = suggestions;
      showQueue();
      if (!suggestions.length) {
        emptyStreak++;
        console.warn(`Autopilot: model returned 0 picks (${emptyStreak} in a row)`);
        host.log.step("suggest_empty", { decision: "0 picks", why: `${emptyStreak} empty answer(s) in a row` });
        if (autopilotCore.useLibraryFallback(emptyStreak) && await tryLibraryLockable(currentId, gen)) { emptyStreak = 0; return; }
        if (!active || gen !== prepGen) return;
        continue;
      }
      emptyStreak = 0;
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
    if (failedFor !== currentId) { failedFor = currentId; failedSearches = 0; }
    failedSearches++;
    const retryMs = autopilotCore.searchPlan({ failedSearches, emptyStreak }).retryMs;
    apStatus(`No next song yet after ${MAX_ROUNDS} tries — retrying in ${Math.round(retryMs / 1000)} s (music keeps playing)`);
    setTimeout(() => { if (active && gen === prepGen) prepareTransition(currentId); }, retryMs);
  }

  function scheduleTransition(currentId, nextId, nextName, candidate, blend = null, minExit = null, layer = null) {
    // Tempo gap 2-15 %: render B's stems key-locked at A's tempo now, while A plays
    // (multi-BPM stem sets, cached on the server), so the blend keeps B's key.
    {
      const oa0 = host.decks && host.decks[activeDeck], sd0 = host.decks && host.decks[stagingDeck()];
      // Never on a deck already reaching the master: a stem swap there is an instant
      // tempo jump (chanel on B jumped to 122.5 BPM 10 s after landing).
      const live0 = sd0 && (sd0 === oa0 || (sd0.onMaster && sd0.onMaster()));
      if (oa0 && sd0 && !live0 && oa0.bpm > 0 && sd0.bpm > 0 && sd0.useTempoStems) {
        const aEff0 = oa0.bpm * oa0._playbackRate();
        const m0 = [1, 2, 0.5].reduce((b, m) => (Math.abs(aEff0 / (sd0.bpm * m) - 1) < Math.abs(aEff0 / (sd0.bpm * b) - 1) ? m : b));
        const gap0 = Math.abs(aEff0 / (sd0.bpm * m0) - 1);
        if (gap0 > 0.02 && gap0 <= keyLockLim()) {
          sd0.useTempoStems(aEff0 / m0).then((ok) => ok && host.bus.emit("ai-activity", {
            kind: "stem-move", deck: stagingDeck(), label: `TEMPO STEMS · ${(aEff0 / m0).toFixed(1)} BPM`,
            why: `${nextName}: stems key-locked ${(gap0 * 100).toFixed(1)} % to this tempo, no pitch shift` }));
        }
      }
    }
    if (!active) return;
    let bTime = candidate.b_time || 0;
    let vocalShort = false, vocalCut = "";
    let recipe = candidate.recipe || "Blend";
    const forced = candidate.forced || null;   // a stored move: booked as stored below, never re-picked
    // Stems on both decks: the set plays as ONE song (user). Every transition is
    // a long stem blend: one owner per layer, one singer, kick + bass swapped on
    // a line. No vocal-driven shortening, no cuts or spinbacks (those were only
    // there to stop two vocals or two beats clashing, which stems already solve).
    const odS = host.decks && host.decks[activeDeck], sdS = host.decks && host.decks[stagingDeck()];
    let vocalRule = false;
    // A stems-readiness blip (a dead source, a set swap in flight) must not
    // decide the recipe: re-arm A's decoded stems first (Open Eye Signal ->
    // Delilah was a Quick Cut because A reported "no stems").
    if (odS && odS.stems && !odS.stemsReady && odS.rearmStems) odS.rearmStems("transition planning");
    const aStemsWhy = stemsWhy(odS), aStems = !aStemsWhy, bStems = !!(sdS && sdS.stems);
    const stemsBoth = aStems && bStems;
    const aEffS = odS ? odS.bpm * odS._playbackRate() : 0;
    const gapS = sdS && sdS.bpm ? Math.min(...[1, 2, 0.5].map((m) => Math.abs(aEffS / (sdS.bpm * m) - 1))) : 1;
    // Tempo gate (tempo-rule.js): a beat-to-beat recipe only when B locks to
    // A's heard tempo right now, inside B's range (+-8 % pitch, or +-16 % on
    // key-locked stems rendered for exactly this tempo). leavemealone 174 ->
    // Sexy Magic 125 was a Long Blend because this used to be "gapS <= 0.25"
    // (tempo stems assumed, never checked), so B then played unlocked.
    // Re-derive with the SAME pure fn pick time used (tempoRule.planFit), only
    // re-validating live state (stems readiness, current pitch) as inputs; the
    // pick-time verdict is candidate.plannedFit, kept here only for a sanity log.
    const keyScoreS = (() => {
      const cs = host.mod.djMind && host.mod.djMind.core && host.mod.djMind.core.camelotScore;
      const ka = odS && odS.analysis && odS.analysis.key && odS.analysis.key.camelot;
      const kb = sdS && sdS.analysis && sdS.analysis.key && sdS.analysis.key.camelot;
      return cs && ka && kb ? cs(ka, kb) : null;
    })();
    // The whole recipe decision is autopilotCore.decideRecipe (pure, node-checked; the
    // virtual set in app/sim drives the same function).
    const profLvl = forced ? null : profileLevel();   // a forced (stored) move is performed as stored
    const decIn = {
      recipe, blend, layer: !!layer, aStems, bStems, aEff: aEffS, bBpm: sdS && sdS.bpm,
      tempoStemsBpm: sdS && sdS.tempoStems && sdS.tempoStems.bpm, keyScore: keyScoreS,
      mashupFits: () => !!(odS && sdS && mashupFits(odS, sdS)),
    };
    if (profLvl) decIn.profile = { level: profLvl, fallback: sceneProfile().PUNJABI_PROFILE.fallback_recipe };
    const dec = autopilotCore.decideRecipe(decIn, host.mod.tempoRule);
    bookedProfileCut = !!(profLvl && dec.quickCut);
    showProfile(profLvl);
    if (profLvl) {
      const why = `${sceneProfile().statusLabel(punjabiMode(), profLvl)}: ${currentGenre || "?"} -> ${profileNext || "?"}` +
        `${dec.profileCut ? ", Quick Cut instead of Echo Out" : dec.quickCut ? ", Quick Cut kept" : ""}`;
      console.info("transition profile:", why);
      host.log.step("scene_profile", { deck: activeDeck, decision: profLvl, why });
    }
    const lockS = dec.lockS;
    if (candidate.plannedFit && candidate.plannedFit.smooth !== lockS.smooth) {
      console.info("transition plan-fit:", `live state changed since pick (${candidate.plannedFit.why} -> ${lockS.why})`);
    }
    const oneSong = dec.oneSong;
    if (dec.dropLayer) {
      if (blend || layer) console.info("transition tempo:", `beatless, ${lockS.lock.why}`);
      layer = null;
    }
    // B enters on the blend's line even when a key rewrite then drops the blend itself
    if (blend && !dec.dropLayer) { bTime = blend.entry; blend.clean = dec.blendClean; }
    blend = dec.blend;
    recipe = dec.recipe; vocalShort = dec.vocalShort; vocalCut = dec.vocalCut; vocalRule = dec.vocalRule;
    if (dec.keyRewrite) console.info("transition recipe:", `${dec.keyRewrite.from} -> ${dec.keyRewrite.to} (keys clash, camelot ${keyScoreS})`);
    if (dec.cutRewrite) console.info("transition recipe:", "cut -> 4-bar Bass Swap (no hard cuts)");
    const od0bpm = (host.decks && host.decks[activeDeck] && host.decks[activeDeck].bpm) || 128;
    if (layer) { bTime = layer.entry; recipe = `LAYER ${layer.hold_bars}+${layer.unwind_bars} bars`; }
    jumpPending = !blend && !oneSong;
    // why an echo / non-stem recipe: on the status line and in the console,
    // so the next time a transition sounds like a cut the reason is visible
    if (!layer && !stemsBoth) {
      const why = `${recipe}: A stems ${aStemsWhy || "live"}, B stems ${bStems ? "loaded" : "not loaded"}` +
        `${vocalCut ? `, ${vocalCut}` : ""}${blend ? "" : `, tempo gap ${(gapS * 100).toFixed(1)}%`}`;
      console.info("transition recipe:", why); host.log.step("recipe", { deck: activeDeck, decision: recipe, why });
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
    const od = host.decks && host.decks[activeDeck];
    const { trackEnd, lo, hi } = autopilotCore.exitBounds({ w, entryPos, trackDur: od && od.buffer ? autopilotCore.audibleEnd(od.analysis, od.buffer.duration) : Infinity });
    let exitAt = autopilotCore.exitPick({ lo, hi, trackEnd, layerStart: layer ? layer.start : null, hasBlend: !!blend,
      blendExit: blend && blend.exit, candidateATime: candidate.a_time, minExit });
    // PEAK mode (dj-mind.js peakTransition): tempo-locked pairs only, land B's
    // drop on A's drop downbeat - Double Drop or Drop Swap. null -> blend.
    const peakT = !forced && !layer && blend && blend.drop && host.mod.djMind && host.mod.djMind.planPeak
      ? host.mod.djMind.planPeak({ drop: blend.drop, lo: Math.max(lo, nowPos + 15), hi,
                                 plannedExit: exitAt, entryPos, inDeck: stagingDeck() })
      : null;
    if (peakT) { recipe = peakT.recipe; bTime = peakT.bTime; exitAt = peakT.exitAt; }
    // Keep the exit on A's phrase grid: push by whole phrases, never by seconds.
    const phraseS = 32 * 60 / od0bpm;
    const timing = autopilotCore.exitTiming({ exitAt, nowPos, phraseS, w, oneSong, vocalShort, peak: !!peakT });
    let effectiveATime = timing.effectiveATime;
    const xfDuration = timing.xfDuration;
    playPlanTag = ` | ${w.label} ${fmtTime(effectiveATime - entryPos)}`;
    // Pre-planned by the silent ear: B starts where the ear chose (inside A), from
    // the line it chose, the merge it heard. Only when the plan is still ahead.
    const pp = candidate.preplan;
    let preplanned = false;
    if (pp && !forced && lockS.beat && !layer && !peakT && stemsBoth && odS && sdS && pp.a_in >= nowPos + 15) {
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
    // S22 breakdown ownership: the blend never starts inside A's breakdown; back to the
    // section before it, else on to the drop (whole phrases). Before the high push so a
    // move onto the drop is then carried past A's high.
    if (!peakT && !layer && !preplanned && od && od.analysis && host.ui.flag("ap-breakdown-toggle", true)) {
      const bd = autopilotCore.exitBreakdownPush({ t: effectiveATime, lo: Math.max(lo, nowPos + 15), phraseS, bpm: od0bpm, trackEnd,
        energyTimes: od.analysis.energy_times, energyCurve: od.analysis.energy_curve });
      if (bd.moved) {
        console.info("transition timing:", `exit moved ${bd.moved} phrase(s) to ${fmtTime(bd.t)}: A is in its breakdown`); host.log.step("exit_moved", { deck: activeDeck, decision: `exit ${bd.moved > 0 ? "+" : ""}${bd.moved} phrase(s)`, why: "A is in its breakdown", result: { from: effectiveATime, to: bd.t } });
        effectiveATime = bd.t;
      } else if (!bd.clear) {
        console.info("transition timing:", `exit at ${fmtTime(effectiveATime)} is inside A's breakdown, no phrase fits outside it`);
      }
    }
    // Never transition out of A while it's at its energy high: push the exit past it
    // by whole phrases (not for PEAK / LAYER / pre-planned: they chose their line).
    if (!peakT && !layer && !preplanned && od && od.analysis) {
      const ex =autopilotCore.exitHighPush({ t: effectiveATime, phraseS, bpm: od0bpm, trackEnd,
        energyTimes: od.analysis.energy_times, energyCurve: od.analysis.energy_curve });
      if (ex.moved) {
        console.info("transition timing:", `exit moved ${ex.moved} phrase(s) to ${fmtTime(ex.t)}: A is at its energy high`); host.log.step("exit_moved", { deck: activeDeck, decision: `exit +${ex.moved} phrase(s)`, why: "A is at its energy high", result: { from: effectiveATime, to: ex.t } });
        effectiveATime = ex.t;
      }
    }
    // Song merge beats the plain mashup (it is its generalization) when a combo fits;
    // never over LAYER / PEAK. The mashup stays the fallback if the merge is refused.
    if (forced) {
      // FORCED plan (macro-mode.js): EXACTLY the stored move at the stored points. The gates
      // above (tempo lock, keys, stems, vocal rule) and the merge's own hold gates only
      // refuse it; a refusal is logged and books the closest allowed move (never a cut).
      lastMergeGate = null;
      const fb = autopilotCore.forcedBooking({ forced: Object.assign({ b_name: nextName }, forced), nowPos, phraseS, trackEnd,
        liveATime: effectiveATime, liveBTime: bTime, beat: !!lockS.beat, stemsBoth: !!(stemsBoth && odS && sdS), keyScore: keyScoreS,
        mashupFits: !!(stemsBoth && odS && sdS && mashupFits(odS, sdS)), mergeOn: mergesOn(),
        vocalRule: vocalRule ? { why: vocalCut, recipe: "Bass Swap" } : null,
        planMerge: (aT, bT, prefer) => planMerge(currentId, nextId, odS, sdS, aT, bT, prefer),
        mergeGate: () => (lastMergeGate ? `${lastMergeGate.gate}: ${lastMergeGate.why}` : null) });
      recipe = fb.recipe; effectiveATime = fb.aT; bTime = fb.bT;
      if (fb.refused) {
        console.warn(`${forced.n != null ? `macro: step ${forced.n}` : `${forced.source || "studied"}:`} refused: ${fb.refused} -> ${fb.recipe}`);
        host.log.step("macro", { phase: "booking", decision: "refused", why: fb.refused, result: { want: forced.recipe, ran: fb.recipe, step: forced.n } });
      }
      const line = fb.line;
      console.info(line);
      host.log.step("macro", { phase: "booking", decision: "perform", why: line });
      host.bus.emit("ai-activity", { kind: "macro", deck: activeDeck, label: `${forced.n != null ? `MACRO ${forced.n}` : "STUDIED"} · ${recipe}`, why: line });
      playPlanTag = ` | ${w.label} ${fmtTime(effectiveATime - entryPos)} · ${forced.n != null ? `macro step ${forced.n}` : "stored move"}`;
    } else if (!preplanned && lockS.beat && !layer && !peakT && mergesOn() && stemsBoth && odS && sdS) {
      const mp = planMerge(currentId, nextId, odS, sdS, effectiveATime, bTime);
      if (mp) {
        recipe = "Stem Merge";   // merge -> hold -> transition beats the matcher's / learned pick when its gates pass
        console.info("transition recipe:", `Stem Merge: ${mp.pick.label} (${mp.pick.reasons.join(", ")}), ${mp.M} bars` +
          (mp.phases ? `, hold ${mp.phases.hold.bars} bars (${mp.phases.hold.phrases} phrases)` : ", fixed length"));
      }
    } else if (!preplanned && !layer && !peakT) {
      // merge was not even tried: the gate that stopped it (counted by the sim)
      const gate = !mergesOn() ? "off" : !stemsBoth ? "stems" : !lockS.beat ? "tempo" : "no_deck";
      mergeGateLog(activeDeck, gate, "not attempted", false);
    }

    bookedRecipe = recipe;
    let executed = false;
    let filled = false;
    let fireAt = effectiveATime; // B's entry lands exactly on this phrase line

    // Hand the plan to the DJ mind: it may pre-clear the outgoing bass or hold
    // the exit one phrase longer (bounded by the set-mode window + 16 bars).
    const barS = 240 / ((od && od.bpm) || 128);
    if (host.mod.djMind) {
      host.mod.djMind.setPlan({
        fireAt,
        // a vocal-free blend window is exact: the mind must not hold past it
        // vocal-aware plans are exact: a DJ-mind hold would move the overlap into a vocal
        maxFireAt: peakT || layer || preplanned || (blend && (blend.instrumental || blend.vocals_known)) ? fireAt
          : Math.max(fireAt, Math.min(trackEnd, hi + 16 * barS)),
        style: peakT ? "peak" : overlapStyle,
        peakKind: peakT ? peakT.kind : null, peakWhy: peakT ? peakT.why : null, brake: !!(peakT && peakT.brake),
        preClearBars: Number.isFinite(candidate.pre_clear_bars) ? candidate.pre_clear_bars : 8,
        bEntry: layer ? null : bTime,             // B's cue point (artist-moves.js cue tease reads B's first hits there)
      });
    }

    // Learned from studied sets (app/music_brain/set_learner.py): the move that DJ
    // made most on pairs like this one, when the console already allows it here.
    let learned = null;
    if (!forced && !layer && !peakT && learnedOn()) {
      const facts = { layer, peak: peakT, blend, oneSong, stemsBoth, vocalRule, recipe, keyScore: keyScoreS,
                      mashupFits: !!(stemsBoth && odS && sdS && mashupFits(odS, sdS)),
                      profile: profLvl ? { level: profLvl } : null };   // Punjabi profile: the scene's own learned evidence
      fetch(`/api/learned/pick?a=${encodeURIComponent(currentId)}&b=${encodeURIComponent(nextId)}&keylock=${!!(sdS && sdS.useTempoStems)}`
            + (profLvl ? `&profile=${encodeURIComponent(profLvl)}` : ""))
        .then((res) => (res.ok ? res.json() : null))
        .then((r) => {
          const pick = r && r.pick;
          const ch = autopilotCore.learnedRecipe(pick, { ...facts, riff: !!riff, recipe }, sceneProfile());
          if (!ch || executed || !active || currentTrackId !== currentId) return;
          learned = pick;
          recipe = ch.recipe;
          bookedRecipe = recipe;
          // a learned move degraded to the profile's Quick Cut really cuts on the downbeat
          bookedProfileCut = !!(profLvl && recipe === sceneProfile().PUNJABI_PROFILE.fallback_recipe);
          console.info("transition recipe (learned):", `${ch.recipe}: ${ch.why}`, pick.reasons);
          host.bus.emit("ai-activity", {
            kind: "learned", deck: activeDeck, label: `LEARNED · ${ch.recipe}`, why: ch.why });
        })
        .catch((e) => console.info("learned pick: none -", e.message));
    }

    // Riff over rap (riff-over-rap.js): when the pair fits (3-15 % tempo gap,
    // A has a groove running into its own breakdown, B raps) and A's
    // key-locked stems render in time, the fire line moves to A's groove start
    // and the whole 64-bar move replaces the recipe.
    let riff = null, riffEntry = null;
    if ((!forced || /riff/i.test(forced.recipe)) && !layer && !peakT && riffOn() && host.mod.riffOverRap) {
      const notBefore = deckPosition(activeDeck) + 25;
      host.mod.riffOverRap.prepare(currentId, nextId, notBefore).then((r) => {
        // why no riff: console for detail, the status line for a glance
        const riffNo = (why) => {
          console.info("riff over rap: no -", why);
          if (!executed && active && currentTrackId === currentId) apStatus(`Riff over rap: no - ${why}`);
        };
        if (!r.ok) { riffNo((r.reasons || []).join("; ") || "no plan"); return; }
        if (executed || !active || currentTrackId !== currentId) return;
        const g0 = r.plan.a_groove[0], pos = deckPosition(activeDeck);
        const sdB = host.decks && host.decks[stagingDeck()];
        if (g0 < pos + 6 || g0 > hi + 60 || !(sdB && sdB.stems)) {
          riffNo(g0 < pos + 6 ? "A is past its groove" : g0 > hi + 60 ? "the groove comes too late" : "B's stems aren't loaded");
          return;
        }
        riff = r;
        fireAt = g0;
        recipe = "RIFF OVER RAP";
        if (host.mod.djMind) host.mod.djMind.setPlan({ fireAt, maxFireAt: fireAt, style: "layer", preClearBars: 0 });
      }).catch((e) => console.warn("riff over rap:", e.message));
    }

    const tick = setInterval(() => {
      if (!active) { clearInterval(tick); return; }
      if (host.mod.djMind && !forced) fireAt = host.mod.djMind.fireAt(fireAt);
      const pos = deckPosition(activeDeck);
      const left = fireAt - pos;

      // Live drums: 2-bar fill leading into the crossfade (glues the records),
      // rationed by the mind (study rule 8: FX stay the exception).
      const od0 = host.decks && host.decks[activeDeck];
      const barSecs = (60 / ((od0 && od0.bpm) || 128)) * 4;
      if (!filled && !layer && left > 0 && left <= 2 * barSecs && host.mod.beatLayer) {
        filled = true;
        if (!host.mod.djMind || host.mod.djMind.fxAllowed("fill")) host.mod.beatLayer.fill(2);
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
      if (host.mod.djMind) host.mod.djMind.onTransition();

      if (host.mod.mashup) host.mod.mashup.cancel();
      mashupTag = "";
      apStatus(`Blending → ${nextName} (${recipe})…`);

      if (riff) {
        const outgoing = activeDeck, incoming = stagingDeck();
        const oaR = host.decks[outgoing];
        const leadR = Math.max(0.05, (fireAt - deckPosition(outgoing)) / oaR._playbackRate());
        const t0R = audioCtx.currentTime + leadR;
        const ui = {
          xf: (inn, f) => setRange(xfader, (inn === "b" ? 1 : -1) * f),
          eq: (d, band, v) => setRange(eqEl(d, band), v),
          pitch: (d, pct) => setDeckPitch(d, pct),
        };
        dipAllowed("riffRelease", "RIFF OVER RAP");
        const totalMs = host.mod.riffOverRap.run(riff, outgoing, incoming, t0R, ui);
        if (host.mod.djMind && host.mod.djMind.layering) {
          host.mod.djMind.layering(totalMs / 1000, { source: "RIFF",
            why: `riff over rap: A's groove key-locked to ${riff.plan.target_bpm} BPM, B's rap on bar 40` });
        }
        riffEntry = riff.plan.b_entry;
        later(totalMs + 500, afterBlend);
        return;
      }

      // Tempo-lock B to A, then start it sample-accurately so B's entry
      // downbeat lands exactly on A's phrase line.
      const sd = host.decks && host.decks[stagingDeck()];
      const oa = host.decks && host.decks[activeDeck];
      const rateA = oa ? oa._playbackRate() : 1;
      if (sd && oa && oa.bpm > 0 && sd.bpm > 0) {
        // Live A tempo (A may still be easing back from its own tempo lock);
        // half/double time counts as a match.
        // Same gate as the recipe (tempo-rule.js beatLock): key-locked tempo
        // stems keep B's key up to +-16 %, the pitched mix stays inside +-8 %.
        const lk = host.mod.tempoRule.beatLock({ aEff: oa.bpm * rateA, bBpm: sd.bpm,
          tempoStemsBpm: sd.tempoStems && sd.tempoStems.bpm });
        if (lk.ok) setDeckPitch(stagingDeck(), lk.pct, lk.range);
      }
      const leadS = Math.max(0.05, (fireAt - deckPosition(activeDeck)) / rateA);
      const t0 = audioCtx.currentTime + leadS;
      if (sd && sd.buffer) bTime = autopilotCore.entryClamp(bTime, autopilotCore.audibleEnd(sd.analysis, sd.buffer.duration));
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
          if (host.mod.beatLayer && host.mod.beatLayer.isEnabled()) {
            host.mod.beatLayer.setEnabled(false);
            beatMutedByLayer = true;
          }
          totalMs = executeLayer(outgoing, incoming, layer, t0) + XF_LOOKAHEAD_MS;
          // NULL-BOT supermove (mascot.js): the LAYER starts on B's first downbeat
          host.bus.emit("ai-supermove", { at: t0, name: "LAYER", deck: incoming });
          if (host.mod.djMind && host.mod.djMind.layering) {
            host.mod.djMind.layering(totalMs / 1000, { source: layer.source,
              why: `${layer.why} - ${layer.hold_bars} bars together, bass to B on the line, A unwinds ${layer.unwind_bars} bars` });
          }
        } else {
          totalMs = executeTransition(recipe, outgoing, incoming, xfDuration, t0) + XF_LOOKAHEAD_MS;
          // every label from here on is the move that ran, not the one that was booked
          const ranMove = executedMove || recipe;
          bookedRecipe = ranMove;   // VIBE strip ("playing X") and ap.next read this
          if (ranMove !== recipe) host.log.step("recipe_executed", { deck: outgoing, decision: ranMove, why: `booked ${recipe}, ran ${ranMove}` });
          sessionEvent("track", { event: "transition_start", from: history[history.length - 1] || null, to: nextName, recipe: ranMove,
                                  planned: ranMove !== recipe ? recipe : undefined, out: outgoing, in: incoming, seconds: Math.round(totalMs / 100) / 10,
                                  a_pos: Math.round(deckPosition(outgoing) * 100) / 100 });   // A's song s at the exit (sim: exits in a breakdown)
          host.bus.emit("ai-cue", { at: t0, kind: "transition",
            deck: incoming, bar: 240 / ((host.decks[incoming] && host.decks[incoming].bpm) || 128), why: `${ranMove}: B's first downbeat` });
          host.bus.emit("vis-moment", { at: t0, name: String(ranMove), tier: "accent", deck: incoming });   // NULL-BOT pops on every blend (a super cue at t0 wins)
        }
        later(totalMs + 500, afterBlend);
      });

      // After the transition completes, update state and continue
      function afterBlend() {   // declaration: hoisted, the riff branch above calls it
        endAudioClock(); // controls are the user's again
        if (!active) return;

        // Stop the outgoing deck and put it back to neutral for its next load
        const od = host.decks && host.decks[outgoing];
        if (od) od.stopNow();
        if (host.mod.stemMoves) host.mod.stemMoves.reset(od);   // never leave its mix muted
        resetDeck(outgoing);

        history.push(nextName);
        dipAsked = false;
        sessionEvent("track", { event: "transition_end", now_playing: nextName, deck: incoming, set_songs: history.length });
        if (host.mod.liveEar && host.mod.liveEar.flush) host.mod.liveEar.flush("transition done");
        advanceLead();
        if (host.mod.macroMode) host.mod.macroMode.landed(currentId, nextId, { recipe: bookedRecipe, bName: nextName, bTime: bTime });   // combo streak, set record (macro-mode.js)
        playedIds.push(nextId);
        unmuteBeatLayer();
        genreLog.push(currentGenre || "");
        currentGenre = (scheduledNext && scheduledNext.suggestion && scheduledNext.suggestion.genre) || "";
        currentEra = (scheduledNext && scheduledNext.suggestion && scheduledNext.suggestion.era) || currentEra;
        songsSinceJump = jumpPending ? 0 : songsSinceJump + 1;
        jumpPending = false;
        if (steering === "move") steerStep++;
        scheduledNext = null;
        profileNext = "";
        heldPool = [];
        activeDeck = stagingDeck();
        currentTrackId = nextId;
        entryPos = riffEntry != null ? riffEntry : nextEntry;
        currentEnergy = null;
        if (host.mod.beatLayer) host.mod.beatLayer.follow(activeDeck);
        if (host.mod.djMind) {
          host.mod.djMind.follow(activeDeck);
          host.mod.djMind.setProfileEnergy(profileById[currentTrackId]);
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
    const el = ui.el("ap-mode");
    const v = el ? el.value : "hybrid";
    return ["long", "quick", "hybrid"].includes(v) ? v : "hybrid";
  }

  // The windows themselves (autopilotCore.WINDOWS) and the choice (playWindowFor) are pure.
  function playWindow(score) {
    // A famous song plays in full: exit only in its last ~50 s, i.e. the outro. Stem
    // breakdowns (stem-moves.js) keep it from sounding long.
    const pd = host.decks && host.decks[activeDeck];
    const famous = !!(pd && pd.fame && pd.fame.famous && pd.buffer);
    // let the song finish: the set holds its energy target; never two songs in a row (GUESS: variety)
    const idx = history.length;
    const finish = !famous && !!(pd && pd.buffer) && host.ui.flag("ap-finish-toggle", true)
      && (lastFinishIdx === idx || idx - lastFinishIdx >= 2) && autopilotCore.energyAtTarget(playedEnergies(), idx).ok;
    const sp = sceneProfile(), profileWindow = sp ? sp.playWindow(profileLevel(), steering) : null;
    const w = autopilotCore.playWindowFor({ steering, famous, finish, profileWindow,
      rem: famous || finish ? autopilotCore.audibleEnd(pd.analysis, pd.buffer.duration) - (entryPos || 0) : 0,
      mode: setMode(), score, energy: currentEnergy });
    if (w.label === "FULL·finish") lastFinishIdx = idx;
    return w;
  }
  let lastFinishIdx = -9;

  // ── live mashup ("A x B") ─────────────────────────────────────────────────
  // Before the transition, lay the NEXT track's vocal over one instrumental
  // 8/16-bar phrase of the current track (the Fred again.. "x" move: tease the
  // next record's voice over this beat, then bring the record itself in).
  // Restraint: at most one layer per track; skipped unless key and tempo fit.
  // One line in this session's event log (app/ui/services/session_log.py). Fire and forget.
  function sessionEvent(kind, data) {
    try {
      fetch("/api/session/event", { method: "POST", headers: { "Content-Type": "application/json" }, keepalive: true,
        body: JSON.stringify({ kind, data }) }).catch(() => {});
    } catch (e) { /* logging never breaks the set */ }
  }
  host.bus.on("ear-flush", (e) => sessionEvent("ear_flush", e.detail));
  // every AI move (stem moves, remix, merges, hook drops, learned moves) with the deck
  // position, so a move that "killed the vibe" can be found in the session log
  host.bus.on("ai-activity", (e) => {
    const d = e.detail || {}, dk = d.deck && host.decks && host.decks[d.deck];
    sessionEvent("move", { move: d.kind || "", label: d.label || "", why: d.why || "", deck: d.deck || null,
      song: history[history.length - 1] || null, pos: dk && dk._currentPosition ? Math.round(dk._currentPosition() * 10) / 10 : null });
  });

  // POST /api/transition/preplan (app/music_brain/preplan.py) for the booked pair:
  // the exit window of this song, now, and A's live tempo. null when stems are
  // missing, B can't sit on A's tempo, the toggle is off, or nothing fits.
  async function requestPreplan(currentId, nextId, candidate) {
    const od = host.decks && host.decks[activeDeck], sd = host.decks && host.decks[stagingDeck()];
    if (!mergesOn() || !od || !sd || !od.stemsReady || !sd.stems || !od.bpm || !sd.bpm) return null;
    const aEff = od.bpm * od._playbackRate(), gap = Math.abs(aEff / sd.bpm - 1);
    if (gap > keyLockLim() || (gap > 0.02 && !(sd.tempoStems && Math.abs(sd.tempoStems.bpm / aEff - 1) < 0.01))) return null;
    const w = playWindow(candidate.score || 50);
    const trackEnd = (od.buffer ? autopilotCore.audibleEnd(od.analysis, od.buffer.duration) : Infinity) - w.xf - 2;
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
    if (host.session && host.session.relaxed) return false;
    const t = ui.el("ap-merge-toggle");
    return !t || t.checked;
  }

  // Moves learned from studied sets: on unless the (optional) toggle is off.
  function learnedOn() {
    const t = ui.el("ap-learned-toggle");
    return !t || t.checked;
  }

  function riffOn() {
    if (host.session && host.session.relaxed) return false;
    const t = ui.el("ap-riff-toggle");
    return !t || t.checked;
  }

  function mashupsOn() {
    const t = ui.el("ap-mashup-toggle");
    return !t || t.checked;
  }

  function fmtTime(s) {
    const m = Math.floor(s / 60);
    return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  }

  async function tryMashup(hostId, guestId, guestName, fireAt) {
    if (!mashupsOn() || !host.mod.mashup) return;
    const hostDeck = activeDeck;
    const d = host.decks && host.decks[hostDeck];
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
      if (await host.mod.mashup.play(hostDeck, plan, entry)) {
        mashupTag = ` | ✕ ${guestName} vocal @${fmtTime(entry)} (${plan.bars} bars${plan.mute_host_vocals ? ", host instrumental" : ""})`;
        if (plan.mute_host_vocals && host.mod.stemMoves) {
          // host goes instrumental for exactly the guest's phrase
          const sm = host.mod.stemMoves;
          const onAt = sm.audioAt(d, entry), offAt = sm.audioAt(d, entry + plan.host_duration);
          setTimeout(() => d.stemMix({ vocals: 0 }, onAt, 0.05), Math.max(0, (onAt - audioCtx.currentTime) * 1000 - 200));
          setTimeout(() => d.stemMix(null, offAt, 0.2), Math.max(0, (offAt - audioCtx.currentTime) * 1000 - 200));
          host.bus.emit("ai-activity", { kind: "stem-move", deck: hostDeck,
            label: `FULL MASHUP · ${plan.bars} bars`, why: `${guestName} vocal over this song's instrumental` });
          host.bus.emit("vis-moment", { at: onAt, name: "MASHUP", tier: "super", deck: hostDeck });   // NULL-BOT: the guest vocal lands
        }
        // stem remix inside the mashup: host drums + bass out for its last quarter,
        // the guest's vocal over the host's synths, everything back on the line
        if (host.mod.stemMoves && d.stemsReady) host.mod.stemMoves.mashupBreak(d, entry, plan.bars, !!plan.mute_host_vocals);
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
      const d = host.decks && host.decks[id];
      const tid = host.state && (id === "a" ? host.state.trackA : host.state.trackB);
      if (!d || !d.playing || !tid) continue;
      const g = (d.crossfaderGain ? d.crossfaderGain.gain.value : 1) * (d.volumeGain ? d.volumeGain.gain.value : 1);
      if (g < 0.3) continue;
      heard[tid] = (heard[tid] || 0) + 1;
      if (heard[tid] === SESSION_MIN_S && !session.some((x) => x.id === tid)) {
        const el = ui.el(`title-${id}`);
        session.push({ id: tid, name: el ? el.textContent.trim() : tid });
        if (session.length > 60) session.shift();
      }
    }
  }, 1000);
  host.expose("setSession", session);

  // The audible deck, else a loaded one: {deck, trackId, name, playing}
  function currentDeck() {
    let best = null;
    for (const id of ["a", "b"]) {
      const d = host.decks && host.decks[id];
      const tid = host.state && (id === "a" ? host.state.trackA : host.state.trackB);
      if (!d || !d.buffer || !tid) continue;
      const g = d.playing ? (d.crossfaderGain ? d.crossfaderGain.gain.value : 1) * (d.volumeGain ? d.volumeGain.gain.value : 1) : -1;
      const el = ui.el(`title-${id}`);
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
    const d = host.decks[cur.deck];
    if (!d.playing) d.play(d._currentPosition() || 0, true);
    if (xfader) { xfader.value = cur.deck === "a" ? "-1" : "1"; ui.fire(xfader, "input"); }
    entryPos = Math.max(0, d._currentPosition() - (heard[cur.trackId] || 0));
    currentEnergy = null; lastFinishIdx = -9;
    if (host.mod.beatLayer) host.mod.beatLayer.follow(cur.deck);
    if (host.mod.djMind) { host.mod.djMind.reset(); host.mod.djMind.follow(cur.deck); }
    const past = session.filter((x) => x.id !== cur.trackId);
    history = [...past.map((x) => x.name), cur.name];
    playedIds = [...past.map((x) => x.id), cur.trackId];
    setStartedAt = host.clock.now() - 1000 * past.reduce((sum, x) => sum + (heard[x.id] || 0), 0);
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
    const modeEl = ui.el("ap-mode");
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
      if (xfader) { xfader.value = "-1"; ui.fire(xfader, "input"); }

      // Play deck A
      const da = host.decks && host.decks.a;
      if (da) da.play(0, true);
      entryPos = 0;
      currentEnergy = null; lastFinishIdx = -9;
      if (host.mod.beatLayer) host.mod.beatLayer.follow("a");
      if (host.mod.djMind) { host.mod.djMind.reset(); host.mod.djMind.follow("a"); }

      // the session so far steers the set too
      const past = session.filter((x) => x.id !== currentTrackId);
      history = [...past.map((x) => x.name), seedName];
      playedIds = [...past.map((x) => x.id), currentTrackId];
      setStartedAt = host.clock.now();
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
      if (host.clock.now() - prepStartedAt < WATCHDOG_MS) return;
      console.warn("Autopilot watchdog: next-song search stalled, restarting it");
      apStatus("⚠ Next-song search stalled — restarting it");
      prepareTransition(currentTrackId);
    }, 10000);
  }

  function stop() {
    if (host.session) host.session.relaxed = false;   // manual play: sampler etc. back
    if (watchdog) { clearInterval(watchdog); watchdog = null; }
    active = false;
    ready.length = 0;
    stopPrerender();
    cancelLead();
    scheduledNext = null;
    pendingSugs = [];
    if (host.mod.mashup) host.mod.mashup.cancel();
    mashupTag = "";
    clearRun();
    unmuteBeatLayer();
    if (host.mod.beatLayer) host.mod.beatLayer.stop();
    if (host.mod.djMind) host.mod.djMind.stop();
    apStatus("Autopilot stopped.");
    updateButtons();
  }

  function updateButtons() {
    if (startBtn) startBtn.disabled = active;
    const scb = ui.el("ap-start-current-btn");
    if (scb) scb.disabled = active;
    if (stopBtn)  stopBtn.disabled  = !active;
  }

  // Test hook: run one transition's automation on the empty decks (no audio,
  // no set) to check the audio-clock scheduling from the console.
  host.expose("autopilotDebug", { executeTransition, xfGains });

  // Read-only view for helpers (beat-grid-ai.js).
  host.expose("autopilotState", {
    get active() { return active; },
    get activeDeck() { return activeDeck; },
    get trackId() { return currentTrackId; },
    get genre() { return currentGenre; },
    get entryPos() { return entryPos; },
    get energy() { return currentEnergy; },
    get fireAt() { return scheduledNext ? scheduledFireAt : null; },
    get layering() { return !!(host.mod.djMind && host.mod.djMind.layerActive); },
    get preplanning() { return preplanFor; },
    get preparing() { return deferNote; },      // "B's stems": the booking waits for them (VIBE strip)
    get next() { return scheduledNext ? { name: scheduledNext.name || null, recipe: bookedRecipe } : null; },
  });

  const leadGo = ui.el("ap-lead-go");
  if (leadGo) leadGo.addEventListener("click", () => {
    // LEAD with the list open: selected row, else the first song result, else steer
    if (leadRows.length) pickLead(leadSel >= 0 ? leadSel : (leadRows.length > 1 ? 1 : 0));
    else startLead();
  });
  if (leadCancel) leadCancel.addEventListener("click", () => cancelLead("lead cancelled — the set carries on"));
  // LEAD TO search: YouTube results as you type (debounced); pick one = that
  // song is the destination; the first row steers toward the typed genre/artist.
  const leadInput = ui.el("ap-lead-input");
  const leadResults = ui.el("ap-lead-results");
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
  const startCurBtn = ui.el("ap-start-current-btn");
  if (startCurBtn) startCurBtn.addEventListener("click", () => { if (!active) startFromCurrent(); });
  if (stopBtn) stopBtn.addEventListener("click", stop);
  if (seedInput) seedInput.addEventListener("keydown", (e) => { if (e.key === "Enter") start(); });

  // On demand (ai-actions.js MERGE -> HOLD): merge -> hold -> transition on the loaded pair, A's song time aT
  // (a phrase line) at audio time t0. The same planMerge / holdPlan gates as the set; a refusal names the
  // gate. Not while the set runs: its own booking owns the transition timers (clearRun).
  // o: {out, inn (deck ids), aId, bId (track ids), aT, t0} -> {ok, why}
  function mergeNow(o) {
    if (active) return { ok: false, why: "autopilot: the set is running, it books its own merges" };
    const od = host.decks[o.out], idk = host.decks[o.inn];
    if (!od || !idk || !od.playing) return { ok: false, why: "deck: nothing is playing" };
    if (idk.playing) return { ok: false, why: `deck: deck ${o.inn.toUpperCase()} is already playing` };
    if (!mergesOn()) return { ok: false, why: "off: MERGE is off (ap-merge-toggle)" };
    lastMergeGate = null;
    const bFallback = ((idk.analysis && idk.analysis.phrase_boundaries_8bar) || [0])[0] || 0;
    const mp = planMerge(o.aId, o.bId, od, idk, o.aT, bFallback);
    if (!mp) {
      const g = lastMergeGate;
      return { ok: false, why: g ? `${g.gate}: ${g.why}` : "merge: no plan for this pair" };
    }
    const totalMs = executeTransition("Stem Merge", o.out, o.inn, 16, o.t0);
    const ran = executedMove || "Stem Merge";
    later(Math.max(0, (o.t0 - audioCtx.currentTime) * 1000) + totalMs + 300, () => { if (!active) od.stopNow(); });
    const hp = mp.phases;
    return { ok: true, ran, why: ran === "Stem Merge"
      ? `${mp.pick ? mp.pick.label + " · " : ""}${mp.M} bars${hp ? `, hold ${hp.hold.bars} bars` : ", fixed length"}`
      : `merge refused at fire time, ran ${ran}` };
  }
  // PLAY STEP with the set not running (macro-mode.js): the stored move now, same gates as a
  // booking. o: {out, inn, aId, bId, forced, aT (the stored exit, already on a line), t0}
  function performNow(o) {
    if (active) return { ok: false, why: "autopilot: the set is running, arm the step instead" };
    const od = host.decks[o.out], idk = host.decks[o.inn], f = o.forced || {};
    if (!od || !idk || !od.playing) return { ok: false, why: "deck: nothing is playing" };
    if (idk.playing) return { ok: false, why: `deck: deck ${o.inn.toUpperCase()} is already playing` };
    const aEff = od.bpm * od._playbackRate();
    const lk = host.mod.tempoRule.beatLock({ aEff, bBpm: idk.bpm, tempoStemsBpm: idk.tempoStems && idk.tempoStems.bpm });
    const cs = host.mod.djMind && host.mod.djMind.core && host.mod.djMind.core.camelotScore;
    const ka = od.analysis && od.analysis.key && od.analysis.key.camelot, kb = idk.analysis && idk.analysis.key && idk.analysis.key.camelot;
    const stemsBoth = !!(od.stemsReady && idk.stems);
    lastMergeGate = null;
    // o.aT is already the stored exit (or the next line after it): no phrase push here
    const fb = autopilotCore.forcedBooking({ forced: Object.assign({}, f, { a_time: o.aT }), nowPos: -Infinity, phraseS: 0, trackEnd: Infinity,
      liveATime: o.aT, liveBTime: 0, beat: !!lk.ok, stemsBoth, keyScore: cs && ka && kb ? cs(ka, kb) : null,
      mashupFits: !!(stemsBoth && mashupFits(od, idk)), mergeOn: mergesOn(), vocalRule: null,
      planMerge: (aT, bT, prefer) => planMerge(o.aId, o.bId, od, idk, aT, bT, prefer),
      mergeGate: () => (lastMergeGate ? `${lastMergeGate.gate}: ${lastMergeGate.why}` : null) });
    if (fb.refused) console.warn(`macro: step ${f.n} refused: ${fb.refused} -> ${fb.recipe}`);
    if (lk.ok) setDeckPitch(o.inn, lk.pct, lk.range);
    // a merge plays B itself from its plan's line; every other move needs B running from the stored entry
    if (fb.recipe !== "Stem Merge") idk.play(autopilotCore.entryClamp(fb.bT || 0, autopilotCore.audibleEnd(idk.analysis, idk.buffer.duration)), false, o.t0);
    const totalMs = executeTransition(fb.recipe, o.out, o.inn, 16, o.t0);
    later(Math.max(0, (o.t0 - audioCtx.currentTime) * 1000) + totalMs + 300, () => { if (!active) od.stopNow(); });
    const ran = executedMove || fb.recipe;
    return { ok: true, ran, refused: fb.refused, line: ran === fb.recipe ? fb.line : `${fb.line} [ran ${ran} at fire time]` };
  }
  return { core: autopilotCore, mergeNow, performNow };
}
if (typeof Engine !== "undefined") Engine.mount("autopilot", createAutopilotEngine);
