# Sim learnings

Living log of what the virtual-set sim and the past-session analysis taught us. Append after every
loop run (template at the bottom). Every claim carries evidence; anything model-derived is marked
UNVERIFIED. Numbers from `baseline.json` are **stub-era**: recorded with the seeded stub LLM
(`stubllm.py`), not a real model. A real-LLM re-record and `app/sim/LOOP.md` were not on main when
this file was written (iterations from that loop are not logged here yet; see section (e)).

**Baseline label (current): `omni-text`.** `baseline.json` now comes from replays of `real-*` fixtures
recorded with `mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit` as the TEXT model (the single-Omni
server on :8901; the MLX text server was down), real YouTube resolve/download, and the console driven
by graph data (analysis, energy, stem envelopes), not decoded audio. Every fixture stamps the model in
`llm_endpoint`; `baseline.json` carries `llm_labels` / `llm_models`. Panel: 3 runs x 4 songs (seeds
1-3), so it is small and noisy: one stalled run dominates the mean. The stub-era numbers below and in
sections (b)-(e) predate it. Songs the library has are served from cached analysis and stems; a
suggested song it lacks is downloaded and analysed only, so it has NO stem lanes (stem moves refuse it).

## How to run the learning loop again

```
python3 -m app.sim.virtual_set --seed N --tracks 10 --mode quick --record NAME  # once, real LLM/network
python3 -m app.sim.virtual_set --replay NAME --out app/sim/out/before           # zero network
python3 -m app.sim.suite --jobs 3                                                # seeds 1-5, long+quick
python3 -m app.sim.compare app/sim/out/before/report.json app/sim/out/after/report.json
python3 -m app.sim.suite --check                                                 # exit 1 on regression
python3 -m app.sim.suite --update-baseline                                       # only after a kept change
```

1. **record** a real run only when the world changes (new model, new rule inputs); fixtures land in
   `app/sim/fixtures/<name>/` plus the shared `_pool/`.
2. **replay** or **suite** for the "before" numbers. Read `report.json` `breakdown` top entries and
   `worst` (five worst transitions with reasons). Reports go to `app/sim/out/<dir>/` (`report.json`,
   `audible.json`, `events.jsonl`, `console.jsonl`, `songs/*/steps.jsonl`).
3. **fix** one rule per commit; respect CLAUDE.md section 4. Put logic in pure `core` functions so a
   node check can pin it (`app/tests/js/*_check.js`).
4. **compare** before and after with `compare`. Keep the change only if the mean score improves and no
   HARD metric worsens (`suite --check`: tempo over cap, key-clash blends, repeat songs, stalls,
   feature coverage). Revert otherwise and log why.
5. **log** the rule in section (b), surprises in (c), new unknowns in (d). Update `baseline.json` in
   its own commit.

## (a) What the sim measures, and how far to trust it

Measures (deterministic, same fixture gives the same `report.json`; `scorer.py` docstring has weights,
lower is better): key clashes on tonal blends, tempo stretch and unlockable jumps, dead air seconds
(estimated master level), silent intro stems, booked-vs-executed recipe mismatch, energy falls,
same-artist runs, repeats, stalls and empty LLM picks, plus a feature-coverage table.
It runs the console's real browser JS in node against the real Python API on a virtual clock.

Trust it for: rule regressions, gate logic, call-count and stall behaviour, key/tempo/energy policy,
anything a number in the audio graph or the session log can decide.

It cannot judge:
- **Sound quality.** No audio device; decks are a recording Web Audio graph and stems are synthetic
  for Demucs / key-lock (`synth.py`). Whether an 8 % key-locked stretch or a drums-only intro sounds
  bad is not measured. The 8 % cap is a KB/rule choice; the audible smearing above it is UNVERIFIED.
- **Real vocal clash.** `audible.json` uses real stem energy only where fixtures carry it; two vocals
  fighting or a mis-labelled singer (findings-2 #9) cannot be seen.
- **LLM taste.** Stub-era numbers reflect the stub, not a model. Stalls (36.0 in breakdown), empty picks
  (34.2), `http_errors` 58.1 and `replay_misses` 16.7 per run in `baseline.json` are dominated by the
  stub world and replay gaps. Treat them as plumbing checks, not selection quality. Whether they drop
  with a real LLM is open.
- **Two key tables.** The scorer judges by the KB table (`key_kb`); the console decides with
  `dj-mind.js camelotScore` (0 for anything outside the first four KB rows). `key_false_rewrites`
  (2.2 per run) and `key_zero_count` (3.7) come from that gap, not necessarily from bad picks.

## (b) Per-rule learnings

Evidence "before" is the past-session analysis (`.claude/agent-prompts/findings-{1,2,3}.txt`, session
ids under `data/cache/sessions/`). The four fix commits landed before the sim existed, so there is no
sim before/after pair for them; "after" is `baseline.json` (stub-era, mean of 10 runs) and a node/pytest
check. Do not read the baseline as proof a fix improved audible quality.

| Rule / threshold | Evidence | Verdict | Where |
|---|---|---|---|
| Tonal blends need Camelot >= 0.8; clash rewrites Long Blend / Bass Swap / Drop Swap to Echo Out | Before: 5 of 6 auto transitions had Camelot 0 and Long Blend / Stem Merge played (015929 02:13:06, 02:17:24; 030703 03:13:15 Long Blend 31.1 s across 5 h). After: `key_clash_blends` 0.0, `key_hard_clash_count` 1.1 per run (stub). Fix commit 390ccd9 | kept | `autopilot.js:keySafeRecipe`, `learnedRecipe` (`keyOk`); `techniques.py:learned_pick(key_score)`, `LEARNED_MIN_KEY`; `server.py:get_learned_pick` |
| Learned technique picks skip bass_swap / stem_intro on clashing pairs | stem_intro sightings key_score `[0,.8,.9,0,0,0,.8,.8,0,0,0]`: 7 of 11 clash, so NEAR_KEY matched any clash pair (findings-3 #1) | kept | `techniques.py:learned_pick`, `_KEY_SENSITIVE` |
| Key-locked stretch cap 16 % / 15 % -> 8 % | Before: TEMPO STEMS 14.7 and 12.9 % (032428), 14.0 and 12.9 % (015929), 11.4 and 12.7 % (013125). After: `tempo_over_cap` 0.0, `tempo_stretch_max_pct` 5.77, mean 2.81 (stub). Audible benefit UNVERIFIED | kept | `tempo-rule.js:KEYLOCK_RANGE_PCT`, `techniques.py:MAX_KEYLOCK_STRETCH`, `autopilot.js:keyLockLim` |
| `BYPASS_KEY_SCORE` 0.7 -> 0.4 (key-agnostic recipes on clashing pairs) | Matcher scored clash pairs 83.5-91, above clean blends (findings-2 #3, Kaito 83.5, Four Tet 83.5) | kept, test in `test_key_scoring.py` | `recipe_matcher.py:BYPASS_KEY_SCORE` |
| Silent intro stem: refuse, do not book it (`pickIntro` returns null on clash with silent drums; `introAudible`) | Cmon 015929 02:13:06 drums lane silent 0-58 s, bass whole song (findings-3 #3); dead-air 2 s on Stem Bridge entry in 2 of 3 (findings-1 #2). After: `silent_intros` 2.1 per run, still nonzero | kept, **open**: gate covers the stem-move path; the EQ-path `eqIntro` gate is unconfirmed | `stem-moves.js:pickIntro`, `introAudible`, `eqIntro` |
| Empty stem gates: strip and rebuild needs a singing vocal; `keepsVibe` needs energy >= 15 % of section mix RMS | 030703 03:15:03 STRIP & REBUILD to -72.6 dB; 015929 02:03:14 SYNTH HOLD on empty "other", 1.5 s at -57.3 dB. After: `dead_air_seconds` 2.55 in 1.0 transitions per run (stub) | kept, floor 0.15 is a guess (UNVERIFIED) | `stem-moves.js:breakdownVocalOk`, `keepsVibe`, `stemPlays`, `REMIX_REL_FLOOR` |
| Logged recipe = executed move (`executedMove`, `planned` field) | Recipe label and executed move disagreed (findings-1 #2, findings-2 #1). After: `plan_exec_mismatch` 1.3 per run | kept, mismatch still occurs | `autopilot.js` `executedMove` |
| Energy: `force` widens rises only | 015929 9>7>4>2 slide; force widened falls (findings-3 #2). After: `energy_falls_ge3` 0.0, `energy_max_fall` 1.2 (stub) | kept | `energy.py:next_ok`, `autopilot.js:energyStepOk` |
| Measured energy in profile clash; no clash kept as last resort (retry naming rejects) | 6 of 8 LLM picks rejected after the expensive match (findings-2 #4); `_filter_suggestions` returned `clashes[:1]` | kept, real-LLM effect open | `autopilot_service.py:_profile_clash`, `_filter_suggestions`, `suggest_next_tracks` |
| Hybrid window: E<=3 rides MEDIUM, not LONG | E2 song ran 283 s (015929) | kept | `autopilot.js:hybridWindowKey` |
| Plan wait floor 3 s -> 12 s | 13 of 19 plans discarded; plans take 4-13 s (findings-1 #4) | kept; after: `no_plan` 1.6 per run, `discarded_plans` 3.5 | `dj-mind.js:requestPlan` |
| `PLAN_BUDGET_S` 40 -> 20, 8-bar overlap when window ends <= 120 s | ear_preplan ok=false in 5 of 5 quick-mode samples (findings-3 #6); measured ear time 0.5-16 s | kept, preplan hit-rate in the sim not yet measured | `preplan.py` |
| Empty-search backoff 20-120 s, library pick from 2nd empty; rejected (A,B) not re-matched while A plays; riff prepare dedupe + 10 min failure cache | 0 picks x60 over 12 min, hold loops 102 s and 215 s (findings-1 #5); suggest x5 in 70 s, RIFF re-planned 8x (findings-3 #7). After: `empty_picks` 17.1, `stalls` 6.0 per run (stub, unreliable) | kept, **open** | `autopilot_service.py`, `autopilot.js`, `riff-over-rap.js:prepare` |
| Deterministic sim (seeded random, virtual clock, inline jobs) | same fixture twice gives same `report.json` (`test_sim_replay.py`, slow) | kept | `app/sim/*`, `host-sim.js` |

Analysis findings not addressed by any merged commit (verdict open, no change made): plan LLM wasted when
`stemsBoth` (findings-2 #1); same-artist cap (2 #5); exit pushed late by the energy-high rule (2 #6);
overlap share and recipe monotony (2 #7); tempo jump >15 % without Echo Out (1 #6); vocal-handoff
mislabel (2 #9); dead air at file end (3 #8); song-end edge cases (1 #8).

## (c) Surprises and dead ends

- **The matcher was right and the console overrode it.** For every clashing pair the matcher ranked
  Backspin / Breakdown / Echo Out; `scheduleTransition` rewrote to Long Blend without reading the key,
  and `learned_pick` agreed because its similarity check accepted clashes (findings-1 #1, findings-3 #1).
  Fixing the matcher score alone would not have helped; the rewrite stage needed the gate.
- **Recipe label lied.** The track event said "Stem Bridge" while `eqIntro` ran; two "Echo Out"
  transitions executed STEM BRIDGE (findings-1 #2). Any metric on recipe mix from old logs is suspect.
- **Two Camelot tables.** The console scores 0 where the KB scores 0.3-0.75; `key_false_rewrite`
  (2.2 per run) counts blends lost to that difference. Unresolved (section d).
- **Stub-era baseline is dominated by plumbing.** `stall` 36.0 and `empty_pick` 34.2 are the largest
  breakdown terms and are stub or replay artefacts (`replay_misses` 16.7, `http_errors` 58.1 per run).
  Do not tune selection against them until the real-LLM baseline exists.
- **No sim-measured reverts yet.** No fix has been reverted on sim evidence in what is on main. The
  loop agent's kept and reverted iterations (and any noisy comparisons) belong here once LOOP.md lands.
- **Manual-testing sessions look like failures.** 044743, 023052, 025548 and the second half of 034230
  show 0-5 s songs and skips; they are manual loading, not AI errors. Filter them before counting.

## (d) Open questions, ranked by expected audible impact

1. Does an 8 % key-lock cap remove audible smearing, or is it too strict (blends refused for nothing)?
   Needs a listening test; the sim cannot say. UNVERIFIED.
2. `eqIntro` (EQ path) still books B's drums on the swap line with no `camelotClash` layer handling and
   no loudness floor (findings-3 #1d, #3); `silent_intros` 2.1 per run remains. Confirm which path leaks.
3. Two Camelot tables: should the console use the KB tiers (0.3-0.75) so `key_false_rewrites` goes to 0
   without letting clashes through?
4. Dead air 2.55 s per run remains in stub runs; identify which move (`worst` list) and whether the 0.15
   RMS floor is right.
5. Stem Bridge / bridge cases: tempo above 8 % is allowed for beatless bridges only in intent; check the
   sim's `tempo_jumps` 0.9 per run.
6. Energy arc with a real LLM: force fix stops falls, but the suggest prompt still anchors "within 2" to
   the playing song (findings-3 #2, `autopilot_service.py` prompt); a 3-song cumulative drop rule is
   untried.
7. Recipe monotony and overlap share (8 of 10 Long Blend, blend ~50 % of on-air time; findings-2 #7);
   baseline `long_blend_share` 0.216, `echo_out_share` 0.419. Echo Out share rose after the key gate:
   is that too many Echo Outs?
8. Same-artist runs (7 Fred again.. in a row, findings-2 #5); baseline `max_artist_run` 1.1 is stub.
9. Preplan hit-rate in quick mode after the budget cut; not measured.
10. Selection with a real LLM: stalls, empty picks, retry storms.

## (e) Appending a learning after each loop run

Copy this block under the newest heading, one per run. Add or edit rows in section (b) for rules; put
dead ends in (c); re-rank (d).

```
### YYYY-MM-DD loop run N (branch, base commit)
- Change: <rule/threshold, file:function, commit>
- Panel: fixtures <names>, LLM <stub|real|replay of X>, seeds <list>
- Before: score <n>, <metric> <n>   After: score <n>, <metric> <n>   (compare output path)
- Noise: <run-to-run variance seen, or "deterministic replay">
- Verdict: kept | reverted | open   Why: <one line>
- Unverified: <anything not proven by a number>
```

### 2026-09-29 pre-render earlier (branch worktree-agent-a1a0e0237349d3f19, base 8a42765)
- Evidence (25 real transitions with names in both logs, `data/cache/sessions` + cache mtimes; NOT measured: download time, per-render
  Rubber Band time): booking follows the deck load by a median 6 s (p90 26 s); the fire is a median 206 s (p90 395 s) after the load;
  stems were on the deck at the load for 16 of 25 songs, the other 9 landed 3-159 s later (median about 56 s), all 25 before the
  fire; tempo sets (12 of 25 songs needed one) landed a median 241 s (p90 486 s) after the load, only 7 of 12 before the fire.
  Separation: median gap between consecutive finished separations 26 s (p10 7, p90 81). LLM: suggest 8.7 s, look-ahead 13.0 s,
  plan 5.0 s (medians). Root causes: (1) the merge is decided AT the booking, seconds after the load, minutes before the fire, so B's
  stems were "not loaded"; (2) `evaluateCandidate` waited only 20 s for the stems before asking for the tempo set, so with stems landing
  40-160 s after the load the tempo set was never asked for ("B has no key-locked tempo stems yet"); (3) the tempo sets were asked for
  A's tempo at that instant, and A was still easing home (a moving target).
- Change: `app/ui/prerender.py` (ranked candidates, stems then tempo sets, one heavy job at a time, cancel on drop),
  `autopilot.js` (`syncPrerender`, `orderByReadiness`, `awaitBReady`, `aTempoAtEntry`), `deck-controller.js` stem poll 10 s -> 3 s.
- Panel: `lib-s1-long` replay (StubLLM), one run, sim world with modelled separation (26 s) and tempo render (30 s) latency.
- Before (the merge-hold agent, same stub replays, instant server-side stems): every merge refused, 4 of 8 "B stems not loaded",
  4 of 8 "B has no key-locked tempo stems yet". After (first version, before the A-home-tempo fix): 1 of 9 merged (`merged_play_share`
  0.111), refusals stems 3, tempo 4, key 1; 5 bookings deferred (146 s in total), 3 gave up; `ready_at_booking_share` 0.0.
  The two "tempo" gate refusals on transitions 7 and 8 were key clashes (holdPlan checks the tempo first).
- Verdict: open. The fix for cause (3) landed after the single allowed sim run and is covered by node checks only (UNVERIFIED in the sim).
- Cache growth: `keylock/t*` tempo sets are now capped by `KEYLOCK_CACHE_MAX_GB` (default 20, negative disables), evicted
  oldest-first by dir mtime (bumped on every serve, at most once a minute), never while rendering, in `protect`, or younger than
  30 min; runs at server start and after each tempo render (`app/music_brain/keylock_cache.py`). Preview with
  `python3 -m app.music_brain.keylock_cache --dry-run [--max-gb N]`, delete with `--apply`. `data/cache/stems` (98.9 GB / 615 sets)
  stays uncapped: each set costs a Demucs run (~26 s). A separate policy is needed (e.g. by last use, sparing library songs); not implemented.

No iterations logged yet: `app/sim/LOOP.md` was not present on main or in any worktree when this file was
written. Add its kept and reverted iterations here when it merges.
