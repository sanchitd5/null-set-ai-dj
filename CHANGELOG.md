# Changelog

All notable changes to NULL::SET. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-10-01

**NULL::SET 1.1 is the release where the player grew up.** It has a peak-time signature move of its own, it listens to how a song is built (sections, drops, the main drop) instead of a raw energy curve, it keeps a set inside one scene, it plays studied sets reliably, and everything it knows now lives in a real database that travels with the repo.

### ✨ Highlights

- **$Up3R-M@SS!V3-M0v3.** A live multi-song stem mashup on the real decks: the cores of several songs layered by stem, with rolling Bass Swap handovers, key-locked tempo and one owner of the sub-bass at all times. Variant `v1` ships with the app. The autopilot may fire it by itself at a high-energy moment, or you pick it from the MACRO list and press PLAY MACRO (or `Shift+S`). NULL-BOT dances in the centre of the screen with its name for the whole move.
- **Analysis v6: sections, drops and the main drop.** Every song is cut into sections on the 8-bar phrase grid, with drops found from the drums and bass stems, and one main drop chosen by the deepest, longest dip before it. Upgrade once with `python3 -m app.music_brain.analysis.reanalyse --all`.
- **Electronic music is no longer one genre.** House, melodic, trance, techno, bass music, drum and bass, chill, festival EDM and breaks are sub-families with a neighbour table, so the autopilot knows melodic techno next to techno is a normal move and trance into downtempo is a jump.
- **Studied sets play as studied.** A running studied-set macro's next song is the first fallback when a deadline hits, and its own steps are booked past the measured gates. A stuck deadline no longer drops to a hold loop while a known-good pair is waiting.
- **A real database.** Pair atlas, macros, learned moves and genre labels live in `app.db`; your own history (sessions, plays, set memory, vetoes, liked transitions) lives in `user.db`. Nothing is pruned.
- **Replay, time travel and liked transitions.** Replay any past set from any transition, jump to a moment by its clock, and like a transition so the autopilot performs it exactly as stored whenever that pair comes up again.
- **Hand-started moves land on the line.** PLAY STEP and MERGE -> HOLD now fire on the next phrase line, not the moment you press, and A no longer echoes out before the transition starts.

### 🎛️ Mixing and transitions

- **Late entry.** B can come in on any strong downbeat of the song, not only its first bar: a real drum hit on the line (from the drums stem, or the mix low band when there are no stems), with measured thresholds and a seeded pick among the lines that fit the set energy.
- **Set energy chooses the recipe.** At every set energy the choice runs Mashup, then Bass Swap, then Long Blend, and B's entry point follows the energy too. A liked pair keeps its stored recipe.
- **Echo Out only when it has to be.** Echo Out is kept for a key clash or a tempo gap. A move refused for any other reason on a tempo-locked pair falls back to the key-safe Bass Swap.
- **Hold loops fit the track.** A held loop never runs past the end of the song, is capped at one 8-bar phrase and at 2 cycles, then hands off. A short outro prefers Echo Out or a breakdown.
- **No vocal over a drop line.** One shared rule refuses any vocal layered over another song's drop line, and the guard now covers Acapella Overlay, Live Mashup and Double Drop.
- **Mashups stay in the family.** A mashup never joins songs from unrelated genre families, and the mashup scene gate holds.
- **New artist moves.** Pad lead, chant gate, dhol drop-in (Punjabi songs only) and chop duck, each with the owner's rules for when it is allowed.

### 👂 Listening to the music

- **Sections on the phrase grid.** One label per 8-bar phrase, so no section is shorter than a phrase. v5 labelled every 1 s window and flickered.
- **Stem-aware drops.** A drop is the phrase line where the drums and bass come back at the song's full-groove level after a build or breakdown. Without stems the mix energy curve stands in.
- **A main drop by its dip.** Among drops within 20 % of the loudest one, the one with the deepest, longest dip before it wins, so a one-phrase intro drop no longer does.
- **v5 still reads.** Songs not yet re-analysed keep working on the old energy-curve rule until you run the reanalyse command.
- **Live ear timeouts.** A slow answer from the live ear is logged as the adaptive wait it is, not as a model error.

### 🧭 Staying in the scene

- **Electronic neighbours.** The owner's cluster decisions: chill sits next to melodic, bass music next to house, melodic techno next to techno, festival EDM next to melodic and house. Trance keeps melodic's neighbours except chill.
- **Hip-hop and R&B are neighbours.** Moving between them is a normal step, not a genre jump.
- **Fallbacks keep the scene.** A deadline waits for a prepared song when one is coming, every fallback (library, atlas, veto, suggest) holds the scene from the stored genre labels, and the scene anchor recovers after a mistake. Deadline fallback runs atlas, then library, then hold, and near the exit or after a failed search the library is tried before the model.
- **Punjabi profile.** Dhol only ever plays on Punjabi songs, and unlabelled songs are kept under the Punjabi profile with the best energy fit first.
- **BAD PAIR.** A button to veto a pair for good. Vetoes persist and count as evidence against the pair.
- **FOLLOW SET, macros and studied bookings obey the picks' rules**, including repeats and genre.

### 📼 Studied sets and macros

- **First fallback is the studied set.** A running or armed studied-set macro's next song is the deadline's first fallback, with the measured gates waived.
- **Triggered studied sets book their own step** past the measured gates, and a stuck deadline falls back to atlas rows the measured gates refused instead of a hold loop.
- **PLAY MACRO steps and stuck deadlines book a known pair** past the measured gates.
- **Long sets learn in parts.** `learn-set` cuts long sets at tracklist boundaries (`--split-minutes`, default 60), checkpoints every part and picks up at the next part after a crash. `--keep-files` skips the cleanup.
- **Reviewed learned moves.** Every learned move was reviewed on its evidence (274 kept, each with a rule). An optional Claude Code backend (`AI_REVIEW_BACKEND=claudecode`) runs that review and the genre / era labels. Lyrics are never quoted to it.
- **New studied sets** from Argy (Cercle) and Lane 8.

### 🗄️ Memory and knowledge

- **Two SQLite files.** `app.db` holds the shared knowledge (pair atlas, macros, learned techniques, genre / era labels); `user.db` holds your private history (sessions, plays, transitions, moves, set logs, set memory, vetoes, liked). Both are WAL, one writer, short transactions.
- **Knowledge sync.** Each file in `app/music_brain/knowledge/` is versioned by its git commit SHA and every row carries its provenance. A pull updates the rows you have not touched; rows you changed stay yours and show up in `python -m app.music_brain.knowledge conflicts`, resolved with `knowledge take ITEM --published` or `--local`.
- **Bigger atlas.** Stems for 40 more songs and the owner-approved knowledge base fixes, rebuilt on the 1.1 rules: 584 tracks, about 340k pairs, 260 macros and 566 genre labels ship with the app.
- **Knowledge base fixes.** Loop caps, drop-line wording, same-scene playbook exits, and selection notes that keep contrast inside the set's scene with no example tracks in any prompt.

### 🎚️ Audio and storage

- **FLAC everywhere.** Stems are written as FLAC and key-locked renders as 24-bit FLAC, with WAV as the fallback. Study clips and ID cuts are 16-bit FLAC.
- **Converter.** `audio_convert` turns leftover WAVs into FLAC in place, verified, with `--jobs` parallel folders, a memory guard (`--max-mem-gb`) and a clean interrupt.
- **Fine-tuned stems.** A complete `htdemucs_ft` stem set replaces the song's other stem folders, and vocal readers use the fine-tuned vocals.

### 🖥️ Console

- **$Up3R-M@SS!V3-M0v3 in the MACRO list** (`Shift+S`): it starts on the press, with a countdown to the first layer, and NULL-BOT dances in the centre of the screen with the move's name.
- **HISTORY view.** Replay from here, replay this transition, time travel, like.
- **Deck title marquee.** A long title scrolls instead of stretching the deck column.
- **BAD PAIR** veto button.

### 🛠️ Tools for the owner

| Command | What it does |
|---|---|
| `python3 -m app.music_brain.analysis.reanalyse --all` | re-analyse the library to v6 (resumable, `--dry-run`, `--jobs N`) |
| `python -m app.music_brain.agent_bridge stem-preview ...` | render a transition exactly as the console plays it; `--full` renders whole songs, `--set-energy` lets the energy pick the recipe |
| `python -m app.music_brain.render.swap_mix --cache ... --out ...` | offline swap mix: each song leads for 16 bars, handed over by stem Bass Swaps |
| `python -m app.music_brain.render.mashup_mix --cache ... --out ...` | offline multi-song stem mashup; `--plan-only` prints the plan |
| `python3 -m app.music_brain.supermove save --plan PLAN.json --name NAME` | save a $Up3R-M@SS!V3-M0v3 variant as a shipped file |
| `./maintain.sh` | stems, FLAC, labels, review, atlas and knowledge export in one command |
| `python -m app.music_brain.learning.replay timeline SESSION` | a past set's transitions; `build`, `at` for replay and time travel |

### 🩹 Fixes

- PLAY MACRO never loads a song onto the deck the autopilot is playing.
- `stem-preview --full` renders are no longer silent at the start (A plays from 0:00).
- Tests never open or migrate the real cache's stores.
- Genre labels merge with the file on save instead of overwriting a newer run.
- Learned review survives the model's content filter, and one set's error no longer stops `--all`.
- Learn progress no longer shows a dead process as running.
- `/api/session/event` accepts deck load, unload and veto events.

### ⬆️ Upgrading from 1.0.1

The first start moves the old JSON stores into `app.db` / `user.db` and keeps the old files as `*.migrated`. Then, once, with the app stopped:

1. Update and install:
   ```bash
   git pull && pip install -r requirements.txt
   ```
2. Re-analyse the library to v6 (resumable; a stopped run continues next time):
   ```bash
   python3 -m app.music_brain.analysis.reanalyse --all
   ```
3. Rebuild the pair atlas on the new analysis:
   ```bash
   python3 -m app.music_brain.maintain --steps atlas
   ```
4. Start as usual with `./start.sh`.

### 🗑️ Deprecated

- The compat shims in `app/music_brain/*.py` (for example `app.music_brain.analyzer`, `app.music_brain.recipe_matcher`, `app.music_brain.pair_atlas`) still work but are removed in the next release. Import from the sub-packages instead: `analysis`, `audio`, `atlas`, `learning`, `matching`, `render`.

## [1.0.1] - 2026-09-30

### Added
- **Punjabi scene profile.** A `PUNJABI` setting in the toggle drawer (`auto`, `on`, `off`, default `auto`).
  When both songs are Punjabi it treats punjabi, bhangra and desi as one scene (Bollywood as a neighbour),
  widens the era gate to 4 decades, plays 45 to 90 s snippets, folds 88 and 176 BPM as the same feel and
  falls back to a Quick Cut on the downbeat instead of an Echo Out. When only one side is Punjabi it
  changes only the handover. The step log says when the profile is active and why. With the profile
  `off`, every decision is byte-identical to before (golden-tested). Profile values come from the research
  notes and are marked as guesses until tested by ear.
- **Scene-tagged learned moves.** Moves learned from a set tagged Punjabi count toward that scene only, so
  they never loosen the global rules. Under the full Punjabi profile, a learned stem intro seen on a key
  clash in at least 3 Punjabi sightings may blend on a clash. The 8 % key-lock stretch cap still holds: a
  bigger gap becomes a Quick Cut.
- **`learn-set` writes macros.** After a successful learn it imports the set's songs and rebuilds the pair
  atlas incrementally, writing `studied-<set_id>-<n>` per transition and `studied-set-<set_id>` for the
  whole set. The result reports them under `macros`. `--no-macros` skips the step.
- **`learn-set --macros-only`.** Builds a playable `set-<set_id>` macro from a tracklist alone: no set
  download, no stem separation of the mix.
- **Tracked knowledge (`app/music_brain/knowledge/`).** Every macro, the learned observations and a slim
  pair atlas are exported to the repo, so a fresh checkout starts with the player's knowledge. Songs are
  matched by name on another machine, and the local cache always wins. `learn-set` exports at the end;
  `python -m app.music_brain.knowledge export` does it by hand.
- **Newly studied sets:** Argy at Tomorrowland Brasil 2025 and DJ Timeless "NYC Live Sessions 1", with their
  macros. More studied-set songs are now in the library, so the published atlas covers 492 tracks.
- **CI.** GitHub Actions runs the console JS checks and the pytest suite (not slow) on every push and pull
  request, on Python 3.14 and Node 24, from `requirements-ci.txt`.
- **`ANNEX.md`**, a glossary of every named concept (atlas, macros, supermove, merge-hold, recipes, gates,
  scene profiles, the sim), each tied to the code that defines it.
- `--punjabi auto|on|off` for the virtual set sim and its suite.

### Changed
- **The pair atlas is a segmented folder** (`pair_atlas/meta.json`, `tracks/<id>.json`, `pairs/<a>.json`).
  A build that changes nothing writes nothing, a request reads only the shards it needs, and the
  published copy is split the same way. The old single file is migrated once and kept as
  `pair_atlas.json.migrated`.
- Exports no longer rewrite a macro whose only change is its `created` timestamp.
- The repository is named `null-set-ai-dj` throughout the docs; machine-specific paths from an earlier
  origin were removed.

### Fixed
- Two `learn-set` runs finishing together could drop each other's observations: the learned store is now
  locked across load and save, and the atlas build is locked too.
- Concurrent writers no longer share one temp file (per-process temp names).
- `learn-set` reported `written: []` for a set id with capital letters although its macros were saved.

### Removed
- 717 lines of unused code in `app/legacy_pipeline/` (unreachable functions, rule tables and a
  commented-out block). The six documented legacy entry points are unchanged.
- The studied "Desi Beats on Mumbai Streets" rickshaw set and its 16 observations.

## [1.0.0] - 2026-09-29

First public release (tag `v1`): two-deck autopilot with merge, hold and transition on 4 stems, learned
moves from studied DJ sets, the pair atlas, macros, NULL-BOT and the visuals, the virtual set sim and the
Obsidian DJ knowledge base. See the [v1 release notes](https://github.com/sanchitd5/null-set-ai-dj/releases/tag/v1).

[1.1.0]: https://github.com/sanchitd5/null-set-ai-dj/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/sanchitd5/null-set-ai-dj/compare/v1...v1.0.1
[1.0.0]: https://github.com/sanchitd5/null-set-ai-dj/releases/tag/v1
