# Virtual set

Runs the console's own browser JS (unmodified, in node) against the real Python API on a virtual
clock. No audio device. Decks are the console's `Deck` class on a recording Web Audio graph. Every
set is scored by one deterministic number (lower is better) and a feature-coverage table.

```
python3 -m app.sim.virtual_set --seed 1 --tracks 10 --mode long|quick|hybrid --out app/sim/out/run
python3 -m app.sim.virtual_set --seed 1 --tracks 10 --mode quick --record NAME   # real LLM/YouTube/Demucs, once
python3 -m app.sim.virtual_set --seed 1 --tracks 10 --library --record NAME      # offline: library songs + StubLLM
python3 -m app.sim.virtual_set --replay NAME --out DIR                           # zero network, deterministic
python3 -m app.sim.suite [--jobs 3] [--check] [--update-baseline] [--build-panel]
python3 -m app.sim.compare A/report.json B/report.json      # exit 1: score worse OR a feature lost
```

Live mode fails clearly when the LLM or network is unreachable; the seeded stub LLM is the replay
and test fallback only. Recording respects `yt_guard`, caps tracks/downloads (live only: the cap
protects YouTube, a fixture is never capped) and uses its own cache (`SIM_DATA_DIR`). Fixtures:
`fixtures/<name>/run.json` plus a shared content-hash pool `fixtures/_pool/` (analysis, vibe, energy,
vocals, stem curves) and a frozen `_shared/`.

## Recording with the real model (the baseline)

The baseline must come from the model the live app uses, not the seeded stub. `--record` (and
`suite --record-panel`) resolve the app's model read-only (`llm_probe.py`: `OLLAMA_BASE_URL` if set, else
the MLX server on `MLX_PORT` / the Omni server on `OMNI_PORT`, else Ollama), publish it the way
`model_runtime` does, and then every model call goes through the app's own code: suggestions,
look-aheads and plans via `autopilot_service.chat_raw` (priority gate, Qwen3 handling, JSON repair),
set-learning review via `set_ai`, the live ear via `live_ear._ask_omni`, the silent ear via
`merge._ask_omni`. YouTube search / verify / download and Demucs are the real ones (yt_guard, a
throttle, a per-run download cap). If the model server is down the run stops before YouTube is
touched (exit 4, `llm_probe.LLMDown`): the sim never starts or stops the model and never falls back
to the stub. Start it with `./start.sh`, then:

```
python3 -m app.sim.suite --record-panel          # seeds 1-5, long + quick, one run at a time
python3 -m app.sim.suite --update-baseline       # refuses a panel whose fixtures are not from the real model
```

A fixture keeps, per call: the prompt signature, a stable subject key, the reply text, the
wall-clock latency and a quality tag (`ok` / `empty` / `invalid`). Replay hands the recorded latency to
the console's virtual clock (`x-sim-latency`), so a slow model costs the same virtual time as it did.
The report counts `llm_empty_replies` and `llm_invalid_replies` (a model that failed the task, not a
rule that failed) and the mean latency. The StubLLM (`--library`, `suite --build-panel`) stays the
offline / pytest world; `--update-baseline --allow-stub` writes a provisional baseline from it.

## Reading a run

`report.json`: `score` (weights in `scorer.py` docstring), `breakdown` by category, the worst 5
transitions, `features` (triggered / refused with reasons / executed / audible-risk flags /
never-triggered). Other files: `events.jsonl` (server session log), `console.jsonl`, `audible.json`
(dead air, bass overlap, vocal clash, unlocked overlap, stretch, from the audio graph + real stem
energy), `net.jsonl`, `status.jsonl`, `ui_states.json`, `songs/*/steps.jsonl`.

Informational metrics (never in the score): `merged_play_share` (transitions that ran a measured
merge -> hold -> handover), `classic_merge_share` (the fixed 16 / 32 bar merge ran instead),
`hold_seconds_mean` / `hold_seconds_max`, and `merge_refusals` (histogram of the gate that stopped a
merge: stems, tempo, key, room, unclean, sub_owner, level, vocal_clash, ...). They are read from the
console lines `merge gate: ...` and `merge phases: {...}` (`runlog._merge_facts`).

Pre-render metrics (informational too, `autopilot.js awaitBReady` + `world.py`): `ready_at_booking_share`
(of the transitions where a merge was possible at all, B's stems and, when the tempo gap needs them, its
key-locked tempo stems were on the deck when the booking first looked; read from the console line
`prepare ready: ... at_booking=`), `defer_seconds` / `deferred_transitions` / `defer_gave_up` (how long the
booking waited for B, `merge deferred: waiting for stems | tempo stems`), `wasted_render_seconds` (modelled
separation and key-lock render seconds spent on songs that never played) and `max_concurrent_heavy_jobs`
(most separations + key-locked renders running at once, from the modelled intervals).

Readiness model (replay / library worlds): a downloaded song's stems are installed `World.SEP_S` = 26 s after
its registration on ONE serial worker (the median gap between consecutive real separations, 361 gaps in
`data/cache/stems`), a key-locked tempo set is served `World.TEMPO_S` = 30 s after the first ask on one serial
worker (the keylock.py docstring figure, UNVERIFIED), a song already separated this run is a cache hit. Server
side `app/ui/prerender.py` is stepped by the requests themselves (no worker thread: `SimHost.threaded = False`).

## Baseline, the gates and the improvement loop

`baseline.json` is the committed suite result (seeds 1-5, long and quick). `suite --check` exits 1
when

* the mean score regresses, or
* a HARD metric regresses (tempo over cap, key-clash blends, repeat songs, stalls), or
* a feature the baseline triggered somewhere in the panel is not triggered any more (the coverage
  gate, `features.lost`): the live features (`features.REQUIRED`: mashup, RIFF x RAP, stem bridge /
  merge / intro, cookbook recipes, learned techniques, remix moves, drum breaks, hook drops, tempo
  stems, live ear, DJ mind, NULL-BOT, sampler, set modes) and every cookbook recipe the matcher scored
  on a played pair.

`app.sim.compare` applies the same feature gate to two reports. `suite.md` ends with the coverage
table and lists the features no seed reached. Loop: run the suite, read the top failures, fix one
rule per commit (respect CLAUDE.md section 4), keep only changes that improve the score without
regressing a hard metric, refresh the baseline for kept changes. `LOOP.md` is the log of the runs so
far.

## Architecture: one Host port, on both sides

Console (JS): `app/ui/static/engine.js` defines the Host port (clock, decks, audio, api transport,
bus, log, ui, storage, random, mod) and the composition root: `Engine.use(host, ai)`,
`Engine.mount(name, create)`. Ported modules are `create({host, ai})` and touch no
window/document/AudioContext/Date/timer/fetch: `autopilot`, `dj-mind`, `stem-moves`,
`riff-over-rap`, `ai-actions`, `tempo-rule` (pure).

* Live console host: `host-browser.js` (`createWindowHost(window, ...)`).
* Sim host: `js/host-sim.js` (virtual clock, recording graph, fetch to the real API).
* Contract: `app/tests/host_contract_check.js`.

Brain (Python): `app/ui/engine.py` is `Engine(host, ai_backend, config)`, plain constructor
injection. `Host` (production) is the world: YouTube search / verify / views / download, stem queue,
separation, vocals stem, hook drops, key-lock renders, audio reads, file hash, the library's energy
distribution, the session and song logs, background job execution, ids, clock. `AIBackend`
(production) is the model: `chat` (suggest, look-ahead, plan) and `ear` (the live ear's audio
call). `Engine.suggest` is the service entry the server's `/api/autopilot/suggest` and the sim share.
The server, `autopilot_service`, `download_jobs`, `bg_jobs`, `live_ear`, `session_log`, `analyzer`,
`energy`, `merge`, `preplan`, `set_learner`, `song_waveform` reach their edges through
`engine.current()`; the composition root installs an Engine with `engine.use()`. The production
`Host` methods late-bind the module functions they wrap, so tests may still monkeypatch those.

The sim installs no monkeypatch: `World.install()` builds `Engine(SimHost(world), SimAI(world),
EngineConfig(suggest_budget_s=1e9))` (`simhost.py`). `SimHost` overrides only what the sim owns:
the edges in live / replay / library mode, synthetic-audio reads, inline background jobs, a counter
for ids, the virtual clock. There is no sim-only branch in the brain.

Deck contract (`Engine.DECK_API`): fields `bpm analysis buffer playing stems stemsReady tempoStems
cuePoint startOffset stemState crossfaderGain volumeGain lowFilter midFilter highFilter inputGain
mixGain fame hookDrops`; methods `_playbackRate _currentPosition _positionAt play stopNow
stopSourcesAt stemMix holdStem rearmStems rampPitchPercent aiSetPitch setEQ setLoopBeats
useTempoStems fetchTempoStems swapTempoStemsAt onMaster brake seek jumpBeats toggleLoop`.

### Not ported yet (still read window/document directly)

`app.js`, `deck-controller.js`, `live-ear.js`, `mashup-layer.js`, `beat-layer.js`, `auto-sampler.js`,
`master-watch.js`, `mascot.js`, `ai-overlay.js`, `session-log.js`, and the other UI scripts. They
run unmodified in the sim against the fake browser. Python side not behind the Host: the offline
learners (`set_learner`, `sources`, `agent_bridge`) call `stem_service` directly.

## Determinism

Seeded `Math.random`, virtual `Date`/`performance`/timers/rAF, serialised fetch with modelled
latency, inline job executors, counter job ids, the virtual clock as `Host.now`, wall-clock
`elapsed` fields dropped from the event log, verify-pool misses sorted. Same fixture twice gives
byte-identical outputs: every file under the run dir (`app/tests/test_sim_replay.py`, marked slow).

Replay is keyed by subject, not prompt text (`World._replayed`): a suggestion by the song playing,
a plan by its tempo / key pair, an ear call by its loop. A reworded prompt gets the reply recorded
for that situation (`replay_drift`, harmless); only a call with no recorded reply is a
`replay_misses` (answered by the seed-stable StubLLM). So a rule change does not make the comparison
noisy unless it really asks the model something new.

## Adding seeds/fixtures

Edit `panel.json`, then `suite --build-panel` (offline library world) or `--record NAME` for a real
run; commit `fixtures/<name>/` and new `_pool` entries. After changing the StubLLM, the prompts
or the fixtures, rebuild the panel and refresh `baseline.json` in the same commit series.
