# Improvement loop log

Method: run the suite (replays of the recorded panel), compare to `baseline.json`, change one rule per
commit, keep it only if the mean score improves without regressing a hard metric (tempo over cap,
key-clash blends, repeat songs, stalls) or losing a feature (`suite --check` / `app.sim.compare` exit 1),
refresh `baseline.json` for kept changes only.

## Panel and baseline (label: omni-text)

Recorded with `SIM_LLM=omni`: `mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit` as the TEXT model (the
MLX text server was down), real YouTube resolve / download, the console driven by graph data (analysis,
energy, stem envelopes; no decoded audio, no Demucs). `real-s1-quick`, `real-s2-long`, `real-s3-quick`,
4 songs each, 0 replay misses. `real-s4-long` and `real-s5-hybrid` are in `panel.json` "pending": the model
servers (MLX, Omni, the app) all went down before they could be recorded. 3 runs is a small, noisy panel:
one stalled run (`real-s2-long`, 16 stalls = 96 of its 102 points) dominates the mean.

Baseline at the start: mean 39.487 (`real-s1-quick` 5.324, `real-s2-long` 102.0, `real-s3-quick` 11.137).

## Iterations

| # | change | mean before | after | hard metrics | verdict |
|---|---|---|---|---|---|
| 1 | a refused Stem Bridge falls back to Echo Out, not the 16-bar EQ blend (`autopilot.js` `executeTransition`) | 39.487 | 39.487 | unchanged | reverted: no effect on this panel (no refused Stem Bridge under that recipe; the refusals in it come from other paths) |
| 2 | console Camelot table = the KB matcher's (diagonal 0.75, -2 h 0.6, 2 h letter flipped 0.3); `KEY_SAFE_MIN` and `LEARNED_MIN_KEY` 0.8 -> 0.6 (`dj-mind.js`, `autopilot.js`, `techniques.py`, tests, CLAUDE.md s4) | 39.487 | 38.598 | key_clash_blends 0 -> 0, tempo_over_cap 0, repeat_songs 0, stalls 5.33 -> 5.33; key_false_rewrites 0.33 -> 0; soft: key_weak_blends 0 -> 0.33 | kept (commit b91d27b, baseline refreshed). `real-s3-quick` 11.137 -> 8.47. Sound of a diagonal / -2 h blend is UNVERIFIED (KB matcher scores them 0.75 / 0.6) |
| 3 | the vibe gate stops vetoing in the forced last round (the set's only stall, `real-s2-long`: 33 virtual minutes, 16 stalls, every pick refused by "darker tone" / "energy jump") | 38.598 | 12.135 | stalls 5.33 -> 0 | reverted: `suite --check` fails (hold_loop and live_ear lost: they only ran because of the stall) and 39 replay misses (the run left its recording, later LLM calls answered by the StubLLM) so the number is not measured on the model. Worth re-testing after re-recording `real-s2-long` with the change; the stall itself is real in the recording |

## Findings from the real-model panel (not loop iterations)

- The Omni text model ignores the prompt's "MEASURED ENERGY ... picks MUST be N-M": in `real-s2-long` it kept
  proposing picks 3-4 levels above a calm seed and the rules refused all of them for 33 minutes. The
  library fallback (`/api/library/lockable`, called 16 times) found nothing: the sim's server only knows
  the songs of the run, the live app has the whole library registered (and gates it by genre / era). So
  part of this stall is a sim fidelity gap, not only a rule gap.
- Songs the library lacks are analysed only (no stems): stem moves refuse them (`needs 4 stems on both songs`).
- Never triggered by the 3-run panel: bass_swap, energy_note, eq_blend, hook_drop, layer, learned_technique,
  mashup_break, mashup_transition, remix_acapella, remix_bass_out, remix_drum_break, silent_ear, stem_merge,
  tempo_stems. `set_memory` and `strip_rebuild` now trigger. The rest need more seeds (the model has to pick
  library songs with stems) and stems for the downloaded ones.

## Sim fixes that changed the numbers earlier (not loop iterations)

| artifact | effect |
|---|---|
| The committed fixtures were empty: every replay ran on the stub with 16-135 misses | rebuilt |
| The StubLLM remembered every song it suggested; the catalog ran dry and long sets stalled | memory removed |
| The 14-download cap applied to replays and to library songs | live YouTube downloads only |
| `net.js` posted FormData as "[object FormData]" | every live-ear call was a 422 |
| The verify timeout was wall-clock, so a slow lookup was "unknown" live and "known" in replay | virtual (`EngineConfig.verify_timeout_s`) |
