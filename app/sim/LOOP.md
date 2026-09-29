# Improvement loop log

Method: run the suite, compare to `baseline.json`, change one rule per commit, keep it only if the
mean score improves without regressing a hard metric (tempo over cap, key-clash blends, repeat
songs, stalls) or losing a feature (`suite --check` / `app.sim.compare` exit 1), refresh
`baseline.json` for kept changes only.

## Status: iterations NOT run (model server down)

PART 0 of the task made the real model the source of the baseline. The app's model server was not
reachable when this was written (`llm_probe.resolve()`: MLX :8081, Omni :8901, Ollama :11434 all
refused), and the sim must not start it or fall back to the stub, so the panel could not be
re-recorded and no iteration was run: a keep / revert decision on a stub-recorded panel would say
nothing about the model the app ships with.

`baseline.json` is therefore PROVISIONAL (`fixture_sources: ["library"]`, written with
`--allow-stub`): the seeded StubLLM panel, seeds 1-6, after the stub / cap artifacts were fixed
(mean score 6.53, 0 stalls, 0 replay misses; the earlier 21.4 with seed-1 / seed-2 long stalling was
a stub artifact, see below). It is only good for regression-checking sim code changes.

To run the loop for real:

```
./start.sh --no-open                      # the model (the sim never starts it)
python3 -m app.sim.suite --record-panel   # seeds 1-5 long + quick + 6 hybrid, real LLM + YouTube + Demucs
python3 -m app.sim.suite --update-baseline
```

then one iteration per candidate below.

## Candidates (from the last report), read but not applied

| # | candidate | where | note |
|---|---|---|---|
| 1 | Camelot table alignment, console vs KB | `dj-mind.js` `camelotScore` (0 for a diagonal move and for 2 hours with the letter flipped; the KB's `camelot_distance_score` gives 0.75 and 0.3, and 0.6 for -2 hours where the console says 0.8), then `KEY_SAFE_MIN` (autopilot.js:126, 0.8), `MERGE_KEY_OK` (stem-moves.js:568) and `merge.KEY_OK` | with the KB table a diagonal (0.75) is below 0.8 and would still be rewritten to Echo Out: set the safe floor to 0.75 (KB legal, not a 3-hour clash) and re-test 0.6. `tests/dj_mind_check.js` pins the old table. CLAUDE.md section 4 stays law: 3+ hours is still 0. |
| 2 | Stem Bridge refused -> Echo Out | `autopilot.js` `executeTransition`, after the bridge block: a refused `Stem Bridge` falls through to the 16-bar EQ blend across a tempo gap | set `kind = "echo"` and `recipe = "Echo Out"` when the bridge is refused, report `executedMove` accordingly |
| 3 | cap how deep into a song B enters | entry-point choice (`match` `b_time`, `startOffset`) | needs a metric first: deepest B entry as a share of B's length |
| 4 | worst unlocked overlap / stretch | stub panel: `lib-s5-long` 76.5 s unlocked overlap, `lib-s1-long` 32 s | the `worst` list of each report names the transitions |

## Iterations

| # | change | mean score before | after | hard metrics | kept / reverted |
|---|---|---|---|---|---|
| - | none run | - | - | - | - |

## Sim fixes that changed the numbers (not loop iterations)

| artifact | effect |
|---|---|
| The committed fixtures were empty (no recorded reply, no download): every replay ran on the stub with 16-135 misses | rebuilt; 0 misses |
| The StubLLM remembered every song it had suggested, so after ~14 rejected rounds the catalog ran dry ("model returned 0 picks" x68) | seed-1 / seed-2 long stalled for good (28 stalls, empty picks 70-110); memory removed |
| The 14-download cap (a YouTube guard) applied to replays | "download cap reached" failures re-rolled the picks; cap is live-only |
| The stub's call ordinal counted per kind, so an unrelated extra call shifted its noise | counted per subject |
| `net.js` posted FormData as the string "[object FormData]" | every live-ear call was a 422 (251 in seed 1); now multipart |
| Seeds 8 and 9 (long) still stall on the stub (first song cannot find a beat-matchable, vibe-compatible pick): a stub with no taste, left out of the panel | |
