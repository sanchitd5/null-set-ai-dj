# Virtual set

Runs the console's own browser JS (unmodified, in node) against the real Python API on a virtual
clock. No audio device. Decks are the console's `Deck` class on a recording Web Audio graph. Every
set is scored by one deterministic number (lower is better) and a feature-coverage table.

```
python3 -m app.sim.virtual_set --seed 1 --tracks 10 --mode long|quick|hybrid --out app/sim/out/run
python3 -m app.sim.virtual_set --seed 1 --tracks 10 --mode quick --record NAME   # real LLM/YouTube/Demucs, once
python3 -m app.sim.virtual_set --replay NAME --out DIR                           # zero network, deterministic
python3 -m app.sim.suite [--jobs 3] [--check] [--update-baseline] [--build-panel]
python3 -m app.sim.compare A/report.json B/report.json
```

Live mode fails clearly when the LLM or network is unreachable; the seeded stub LLM is the replay
and test fallback only. Recording respects `yt_guard`, caps tracks/downloads and uses its own cache
(`SIM_DATA_DIR`). Fixtures: `fixtures/<name>/run.json` plus a shared content-hash pool
`fixtures/_pool/` (analysis, vibe, energy, vocals, stem curves) and a frozen `_shared/`.

## Reading a run

`report.json`: `score` (weights in `scorer.py` docstring), `breakdown` by category, the worst 5
transitions, `features` (triggered / refused with reasons / executed / audible-risk flags /
never-triggered). Other files: `events.jsonl` (server session log), `console.jsonl`, `audible.json`
(dead air, bass overlap, vocal clash, unlocked overlap, stretch, from the audio graph + real stem
energy), `net.jsonl`, `status.jsonl`, `ui_states.json`, `songs/*/steps.jsonl`.

## Baseline and improvement loop

`baseline.json` is the committed suite result (seeds 1-5, long and quick). `suite --check` exits 1
if the mean score, any HARD metric (tempo over cap, key-clash blends, repeat songs, stalls) or
feature coverage regresses. Loop: run suite, read top failures, fix one rule per commit (respect
CLAUDE.md section 4), keep only changes that improve the score, update the baseline.

## Architecture: one Host port

`app/ui/static/engine.js` defines the Host port (clock, decks, audio, api transport, bus, log, ui,
storage, random, mod) and the composition root: `Engine.use(host, ai)`, `Engine.mount(name, create)`.
Ported modules are `create({host, ai})` and touch no window/document/AudioContext/Date/timer/fetch:
`autopilot`, `dj-mind`, `stem-moves`, `riff-over-rap`, `ai-actions`, `tempo-rule` (pure).

- Live console host: `host-browser.js` (`createWindowHost(window, ...)`).
- Sim host: `js/host-sim.js` (virtual clock, recording graph, fetch to the real API).
- AI backend (`{suggest, plan}`): default HTTP to `/api/autopilot/*`; which model answers (real LLM,
  replay fixture, stub) is decided server side by `world.py`'s patched `_chat_call`.
- Contract: `app/tests/host_contract_check.js` (both hosts satisfy `assertHost`, same members,
  registry, static purity scan of the engine runtimes).

Deck contract (`Engine.DECK_API`): fields `bpm analysis buffer playing stems stemsReady tempoStems
cuePoint startOffset stemState crossfaderGain volumeGain lowFilter midFilter highFilter inputGain
mixGain fame hookDrops`; methods `_playbackRate _currentPosition _positionAt play stopNow
stopSourcesAt stemMix holdStem rearmStems rampPitchPercent aiSetPitch setEQ setLoopBeats
useTempoStems fetchTempoStems swapTempoStemsAt onMaster brake seek jumpBeats toggleLoop`.

### Not ported yet (still read window/document directly)

`app.js`, `deck-controller.js`, `live-ear.js`, `mashup-layer.js`, `beat-layer.js`, `auto-sampler.js`,
`master-watch.js`, `mascot.js`, `ai-overlay.js`, `session-log.js`, and the other UI scripts. They
run unmodified in the sim against the fake browser. Python side: the sim still installs its edges by
monkeypatching in `world.py` (LLM `_chat_call`, download/search, stems queue, session/song logs); an
`Engine(host, ai_backend, config)` class replacing that is not done.

## Determinism

Seeded `Math.random`, virtual `Date`/`performance`/timers/rAF, serialised fetch with modelled
latency, inline job executors. Same fixture twice gives the same `report.json`
(`app/tests/test_sim_replay.py`, marked slow). `events.jsonl`/`net.jsonl` carry wall-clock elapsed
and job ids and are not byte-stable.

## Adding seeds/fixtures

Edit `panel.json`, then `suite --build-panel` (offline library world) or `--record NAME` for a real
run; commit `fixtures/<name>/` and new `_pool` entries.
