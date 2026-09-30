# DJ Console: Feature Spec

What is actually implemented in `app/ui` (the console), `app/music_brain` (the AI engine) and
`app/sim` (the virtual set sim) today, versus what is partial, stubbed or deliberately absent.
Written so you can tell "real" from "placeholder" at a glance.

Last verified 2026-09-30 against main `10bb79e`. Code is cited by module and symbol, not by line.
Names used here (atlas, macro, merge-hold, live ear, ...) are defined in
[ANNEX.md](../engineering/ANNEX.md); future plans live in [IDEAS.md](IDEAS.md); release history
lives in `CHANGELOG.md`.

Status legend:

- **Working**: implemented and wired end to end.
- **Partial**: implemented, with a caveat in the notes.
- **Stubbed**: UI or code exists but does nothing real yet.
- **Absent**: not implemented (see Known gaps or Out of scope).

---

## 1. Playback engine (per deck)

| Feature | Status | Where | Notes |
|---|---|---|---|
| Load track (file picker or browser) | Working | `deck-controller.js`, `engine.js` | Web Audio decode drives waveform and deck |
| Play / Pause / Cue, Reverse, Brake | Working | `deck-controller.js` (`BRAKE_SECONDS`) | |
| Pitch fader, pitch bend, tap tempo, sync | Working | `deck-controller.js` | Pitch fader shifts pitch (see keylock) |
| Beatjump, loop, Loop Roll pads | Working | `deck-controller.js`, `performance.js` | |
| Hot cues (4 per deck) | Working | `deck-controller.js` `renderHotCueMarkers` | Now drawn on the waveform at the stored position |
| Slicer pads | Absent | `index.html` (disabled SLICER tab), `performance.js` | No beat-slicing DSP on the deck pads. Remix mode CHOP pads cover the use case (see §4) |
| Keylock on the decks | Absent | `index.html` (KEYLOCK N/A) | Raw `playbackRate` still shifts pitch with tempo |
| Keylocked stems (server render) | Working | `keylock.py` `render` / `ensure`, `tempo-rule.js` `KEYLOCK_RANGE_PCT` | Tempo-matched stems rendered offline (rubberband), capped at 8%, served as FLAC |
| Jog wheel | Partial | `deck-controller.js` | Click to play, drag to scrub, spin while playing. Not a scratch control |

## 2. Mixer

| Feature | Status | Where | Notes |
|---|---|---|---|
| 3-band EQ, trim, channel faders, VU, master | Working | `deck-controller.js`, `engine.js` | |
| Crossfader (equal power) | Working | `deck-controller.js` | |
| Dedicated per-channel filter knob | Absent | `index.html` FX row `data-type="filter"` | Filter only via the FX row |
| Mix recording to file | Working | `performance.js` `startRecording`, `POST /api/recordings` | MediaRecorder on the master bus, uploaded and kept under `data/cache/recordings/` |
| Set-log download and archive | Partial | `performance.js`, `POST /api/set-logs`, `GET /api/set-logs/{id}/markdown` | `djset-v1` JSON plus Markdown export. Played sets replay from their session logs (next row) |
| Replay a past set, time travel, liked transitions | Working (live check pending) | `learning/replay.py`, `learning/history_api.py`, `learning/liked.py`, `services/replay_api.py`, `history-view.js`, `macro-mode.js` | MACROS panel HISTORY: replay a session or one transition as it played (recipe, exit, entry, merge, tempo, in-transition moves with params), jump to a moment, LIKE a row. Replays are macros (`replay:<session>`), every live gate runs. Log gaps are listed per step |

## 3. AI brain and autopilot

| Feature | Status | Where | Notes |
|---|---|---|---|
| Track analysis (BPM, beatgrid, Camelot, phrases, sections, vocals) | Working | `analyzer.py`, `GET /api/tracks/{id}/analysis` | |
| Transition matching over the 28-recipe cookbook | Working | `recipe_matcher.py`, `POST /api/match` | Manual points go through `RecipeMatcher.resolve_candidate()` |
| Preview and full offline render | Working | `transition_renderer.py` `render_preview` / `render_full_mix` | Dedicated DSP for Bass Swap, Echo Out, cuts, Filter Transition; the rest use `_generic_eq_blend` (see gaps) |
| Stem separation (Demucs, 4/2 stem) | Working | `stem_service.py`, `stem_worker.py`, `POST /api/tracks/{id}/separate` | Stems stored as FLAC, WAV fallback |
| Live autopilot (pick next, book, play) | Working | `autopilot.js` `decideRecipe` / `keySafeRecipe` / `energyStepOk`, `autopilot_service.py` | Replaces the old "no live automation" gap. Drives the real console controls |
| Live Transition Maker | Working | `automation.js` | Phrase-timed runs scheduled on the AudioContext clock |
| Merge, then hold, then transition | Working | `ai-actions.js` / `autopilot.js` `mergeNow`, `dj-mind.js` `pickHoldLoop` | Preferred plan when its gates pass; gate reasons and phase steps logged |
| Hold-loop preplan | Working | `preplan.py` `preplan`, `POST /api/transition/preplan` | |
| Atlas backup and deadline fallback | Working | `autopilot.js` `preplanBackup` / `deadlineFallback` | Tiers: atlas, then library, then hold |
| Suggest-reject log | Working | `autopilot_service.py` `note_rejects` | Every dropped pick logged with its reason as a session event |
| Stem moves (stem intro, voice strip, synth hold) | Working | `stem-moves.js` `pickIntro`, `engine.js` stem slices | Silent-stem moves refused, they fall back |
| Artist and FX moves (S1 to S22 family) | Working | `artist-moves.js`, `fx-moves.js`, `fx-rack.js`, `fx-budget.js` `canSpend` | Budget per transition and per song |
| Artist moves: pad lead, chant gate, dhol drop-in, chop duck | Working (unverified by ear) | `artist-moves.js` `planPadLead` / `planChantGate` / `planDholDrop` / `planChopDuck`; buttons PAD LEAD, CHANT GATE, DHOL DROP-IN, CHOP DUCK | Pad lead: B's pads first before a tonal blend (key >= 0.8, 8% cap, every 3rd transition at most). Chant gate: vocal gated on 16ths into a build, once per song, FX budget. Dhol drop-in: B's drums under A before a cut, only between two Punjabi songs at scene level full (owner rule). Chop duck: drums -6 to -10 dB under learned chops. Chant gate and chop duck never touch a drop line (owner rule). Levels are named constants, not judged by a listener |
| AI ACTIONS buttons and toggle drawer | Working | `ai-actions.js`, `toggle-drawer.js` | Grouped, searchable, favourites persisted per browser |
| AUTO MIX (mix into the other deck now) | Working | `ai-actions.js` | Not a crate automix queue |
| Mashup / layer / riff-over-rap / remix mode | Working | `mashup-layer.js`, `riff-over-rap.js`, `remix-mode.js`, `POST /api/mashup/plan`, `POST /api/layer/plan` | |
| LLM runtime | Working | `model_runtime.py` (`MLX_MODEL`, `OLLAMA_MODEL`), `llm_gate.py` | Local Qwen3 via MLX, Ollama as fallback |
| Live ear (does the loop sound off?) | Partial | `live_ear.py` `rule_decision`, `live-ear.js` | Qwen3-Omni via mlx-vlm; rule fallback when the model is down. Audible benefit not verified by listening |

## 4. Sampler, pads and FX

| Feature | Status | Where | Notes |
|---|---|---|---|
| 8-slot sampler bank | Working | `sampler-deck.js`, `auto-sampler.js` | Synthesized one-shots plus uploads |
| Custom sample upload | Working | `POST /api/samples` | Content-hashed |
| Performance pads and remix CHOP pads | Working | `performance.js`, `remix-mode.js` (`triggerPad`) | |
| Per-deck insert FX (Filter/Echo/Reverb/Flanger/Phaser/Bitcrusher/Ping-Pong) | Working | `fx-rack.js` | One wet/dry knob per deck |
| Per-effect parameters beyond wet/dry | Absent | | |

## 5. Learning from studied sets

| Feature | Status | Where | Notes |
|---|---|---|---|
| learn-set (study a recorded set) | Working | `set_learner.py`, `agent_bridge learn-set` | `--split-minutes` checkpointed parts, cleanup after each part, `--macros-only`, `--keep-files`. Study clips cut as FLAC |
| Learning progress panel | Working | `learn_progress.py`, `learn-progress.js`, `GET /api/learn/progress` | |
| Learned moves | Working | `techniques.py` `learned_pick` / `learned_moves`, `learned-moves.js`, `dj-mind.js` `learnedNow` | Scene-tagged sets count toward their scene only |
| Pair atlas | Working | `pair_atlas.py` `build`, `atlas_store.py`, `pair_atlas_rules.js`, `GET /api/atlas/pair` | Every library pair scored by the console's own rules; SQLite rows in `CACHE_DIR/app.db`, a build writes only changed rows |
| Studied combos | Working | `studied_combos.py`, `GET /api/studied/sets` | Ranked first as atlas evidence |
| Macros, macro mode, PLAY MACRO, FOLLOW SET | Working | `macros.py`, `set_import.py`, `macro-mode.js` `playMacro`, `/api/macros` | |
| Knowledge export and seed | Working | `knowledge.py` `export` / `seed` / `auto_seed` | Slim atlas, privacy check before export |
| Persisted genre labels | Working | `genre_labels.py` | Stored in `CACHE_DIR/app.db`, travel in the export |
| Local stores in SQLite | Working | `db.py`, `history.py`, `user_marks.py` | `app.db` (atlas, macros, learned, labels; exported as JSON) and `user.db` (set history, set memory, marks; private); old JSON migrates once, kept as `.migrated` |
| Set history | Working (API only) | `history.py` `sessions` / `timeline` / `state_at` / `pair_plays` | Every set in `user.db`, indexed from the session logs as they are written; for replay / time travel |
| Punjabi scene profile | Working | `scene_profile.py`, `scene-profile.js` | auto/on/off setting; scene-tagged learned moves |

## 6. Library, browser and downloads

| Feature | Status | Where | Notes |
|---|---|---|---|
| List/search/sort uploaded tracks | Working | `browser.js` | By name, BPM, key, energy |
| Local library scan | Working | `library_service.py` `scan_library`, `POST /api/library/scan` | `DJ_LIBRARY_DIRS` allowlist |
| Download by URL / search | Working | `download_service.py`, `download-progress.js`, `/api/download/jobs` | Guarded by `yt_guard.py` |
| Duplicate song cleanup | Working | `dedup_songs.py` | |
| FLAC conversion | Working | `audio_convert.py` | `--jobs` parallel, memory guard |
| Favorites / playlists / crates | Absent | | Deliberately not rendered (`browser.js`) |

## 7. Keyboard shortcuts

| Feature | Status | Where | Notes |
|---|---|---|---|
| Mirrored per-deck keys | Working | `performance.js` | Legacy default scheme |
| Combo keys (Shift picks deck B) | Working | `performance.js` | `Space`, `Enter`, `` ` ``, `'` |
| Editable shortcuts | Working | `performance.js` | Persist in `localStorage`, reset to defaults |
| Sampler pad keys | Working | `performance.js`, `sampler-deck.js` | Rebindable |

## 8. Visuals and show

| Feature | Status | Where | Notes |
|---|---|---|---|
| Waveforms, beat layer, stem waves | Working | `visuals.js`, `beat-layer.js`, `stem-wave.js` | |
| VIBE strip and marquee | Working | `vibe-ui.js`, `marquee.js` | |
| NULL-BOT mascot | Working | `mascot.js`, `null-bot.css` | |
| ANYMA look and SHOW mode | Working | `anyma-show.js`, `anyma-ui.js` | WebGL stage, drop detector, SHOW AUTO |

## 9. Virtual set sim and CI

| Feature | Status | Where | Notes |
|---|---|---|---|
| Virtual set sim | Working | `app/sim/virtual_set.py`, `app/sim/suite.py` | Real console JS in node on a virtual clock. Cannot judge sound |
| Sim baseline | Partial | `app/sim/baseline.json` | Recorded with the stub LLM; real-LLM re-record pending |
| CI | Working | `.github/workflows/ci.yml` | pytest (not slow) on push and PR |

## 10. Out of scope

A local, personal and educational tool, not a product. These are not gaps:

- Login / account system
- "Pro" paywall or unlock gating
- App-download CTA
- Streaming or Beatport-style catalog browsing

## 11. Known gaps

- **Offline renderer ignores stems.** `transition_renderer.py` renders every recipe without a
  dedicated chain through `_generic_eq_blend`, including stem-based ones. The live console does
  play stems; only preview/render lacks them.
- **No keylock on the decks.** Only server-rendered keylocked stems keep pitch.
- **No slicer on the deck pads**, no dedicated filter knob, no per-effect parameters.
- **Sim baseline is stub-LLM era.** See `app/sim/LEARNINGS.md`.
- **Live ear and learned moves are not verified by listening.**
- **Replay is only as exact as the session log.** Not logged: the FX rack state, the keylock
  decision when no macro drove the step, a merge's hold plan when only its audition was logged,
  and exact call times for artist moves (they fire on the next line after the stored time minus
  their lead). The replay lists these per step (`gaps`). Session logs are pruned to the newest 60.
