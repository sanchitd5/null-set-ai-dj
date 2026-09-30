# ANNEX: glossary of the app's concepts

This is a reference for the words you meet in the console UI, the CLI, the logs and the docs.
It is written for the owner, a new contributor or another AI agent. Each entry says what the
thing is, where it lives in code (`file:symbol`, no line numbers because they drift), how you
meet it, and what it touches next. Anything the code did not confirm is marked **UNVERIFIED**.

DJ theory lives in the Obsidian vault, not here. Entries point at notes by name, for example
[[Bass Swap]] in `DJ/05 - Transition Cookbook/`. See also `README.md`, `docs/product/FEATURES.md`,
`app/sim/README.md` and `CLAUDE.md`.

## How it fits together

1. **Songs** land in the library (upload, `downloader/`, or a studied set's tracklist).
2. **Analysis and stems.** Every song gets a beat grid, 8-bar phrases, Camelot key, energy and
   vocal regions (`analyze`), plus Demucs stems (`separate`). Both are cached by content hash.
3. **Pair atlas.** Offline, every ordered pair of library songs is scored by the console's own
   rules: key, tempo, energy, vocals, merge-hold, riff, mashup, supermove. Evidence from real
   sets marks a pair as studied.
4. **Learning from real sets.** `learn-set` cuts a recorded DJ set at tracklist boundaries,
   separates it, finds which song plays where and detects techniques. The local model reviews
   them (AI review) and they merge into `learned_techniques.json`. The owner can add user rules.
5. **Macros and knowledge.** The atlas and the studied sets produce macros (replayable sets and
   transitions). `knowledge/` exports a slim copy into git so a fresh checkout starts strong.
6. **Autopilot plays with gates.** In the browser console the autopilot picks the next song
   (studied combos first, then atlas combos, then the LLM), picks a recipe, and every choice
   still passes the live gates: key gate, tempo rules, energy rules, phrase snapping, stem
   energy. A refused move falls back; it is never forced.
7. **Virtual set sim** runs that same browser JS in node against the real API and scores a
   whole set, so rule changes are tested before anyone trusts them.

## Entries (alphabetical)

- [8% keylock](#8-keylock)
- [agent_bridge CLI](#agent_bridge-cli)
- [AI review](#ai-review)
- [artist moves](#artist-moves)
- [atlas (pair_atlas)](#atlas-pair_atlas)
- [AUTO MIX / automix](#auto-mix--automix)
- [autopilot](#autopilot)
- [baseline](#baseline)
- [build-set / import-set](#build-set--import-set)
- [BYPASS_KEY_SCORE](#bypass_key_score)
- [Camelot](#camelot)
- [combo](#combo)
- [dj-mind](#dj-mind)
- [Echo Out, Bass Swap, Long Blend, Drop Swap, Quick Cut](#named-recipes)
- [energy rules](#energy-rules)
- [era gap](#era-gap)
- [FOLLOW SET](#follow-set)
- [genre_near / genre scenes](#genre_near--genre-scenes)
- [key gate](#key-gate)
- [KEY_SAFE_MIN](#key_safe_min)
- [keySafeRecipe](#keysaferecipe)
- [knowledge/ folder](#knowledge-folder)
- [learn mode / learn-set](#learn-mode--learn-set)
- [learned moves](#learned-moves)
- [learned_techniques.json](#learned_techniquesjson)
- [list-recipes](#list-recipes)
- [live ear](#live-ear)
- [local wins](#local-wins)
- [macro](#macro)
- [macro kinds](#macro-kinds)
- [macro-mode / MACRO MODE](#macro-mode)
- [--macros-only](#--macros-only)
- [merge-hold](#merge-hold)
- [observations](#observations)
- [octave fold](#octave-fold)
- [pair atlas vs matcher](#pair-atlas-vs-matcher)
- [performance pads / sampler](#performance-pads--sampler)
- [phrase snapping](#phrase-snapping)
- [recipes](#recipes)
- [recordings](#recordings)
- [REST surface](#rest-surface)
- [replay](#replay)
- [rules hash](#rules-hash)
- [scene profiles (Punjabi)](#scene-profiles-punjabi)
- [seed combos / auto_seed](#seed-combos--auto_seed)
- [set_learner](#set_learner)
- [slim atlas](#slim-atlas)
- [stem intro](#stem-intro)
- [stems / Demucs / separation](#stems--demucs--separation)
- [step log](#step-log)
- [studied combos](#studied-combos)
- [suite](#suite)
- [supermove](#supermove)
- [tempo rules](#tempo-rules)
- [titles](#titles)
- [toggle drawer](#toggle-drawer)
- [track list set-&lt;id&gt;](#track-list-set-id)
- [user rules](#user-rules)
- [vibe](#vibe)
- [virtual set sim](#virtual-set-sim)
- [works score](#works-score)

### 8% keylock
The biggest tempo stretch allowed for key-locked (pitch-invariant) stems. Past about 8% the
stretch smears, so bigger gaps route through Echo Out or a Stem Bridge. Lives at
`app/ui/static/tempo-rule.js:KEYLOCK_RANGE_PCT` and its Python twin
`app/music_brain/matching/techniques.py:MAX_KEYLOCK_STRETCH`. Key-locked renders come from
`app/music_brain/audio/keylock.py` (Rubber Band), cached under `data/cache/keylock/` as 24-bit FLAC
(about 0.46x the float WAV, residual -143 dB). See [tempo rules](#tempo-rules).

### agent_bridge CLI
The engine's command line and Python API. Every command prints JSON and failures print
`{"error": ...}` with a non-zero exit. Subcommands include `analyze`, `separate`, `match`,
`preview`, `list-recipes`, `learn-set`, `learned`, `learn-status`, `hook-drop` and `lyrics`.
Lives in `app/music_brain/agent_bridge.py` (`add_parser` calls). Run as
`python -m app.music_brain.agent_bridge <cmd>` from the repo root.

### AI review
A stage of [learn-set](#learn-mode--learn-set) where the local model checks each detected move
and keeps or rejects it. Rejected observations never count as a technique. Shows in the
LEARNING panel as the "AI review" stage (`app/music_brain/learning/learn_progress.py:STAGES`,
`app/ui/static/learn-progress.js`). Skip it with `learn-set --no-ai`.

### artist moves
Named in-song moves copied from specific DJs (specs S1 to S22 in
`research/notes/artist-signature-techniques.md`, plus set-study moves). Each is a pure planner in a
module's `core` (node-tested) that says ok or refuses with a named gate; the runtime plays it only
through the Host port, logs an `ai-activity` event (`kind: "artist_move"`, `move`, `why`) and a
refusal once per phrase. HUD toggles `ap-artist-<move>`, AI ACTIONS buttons in the ARTIST LOOPS
group. `app/ui/static/artist-moves.js` holds the loop / tease family:
`slip_loop` (S13), `cue_tease` (S14), `roll` (S12), `perc_bridge` (S11, always refused: two decks),
`pad_lead` (Lane 8 pads first: B's other stem alone under A's last 8 or 4 bars before a tonal
blend, key >= 0.8, one every 3 transitions), `chant_gate` (S20: A's vocal gated on 16ths over the
last 1 or 2 bars of a build, once per song, one "vocal" unit of the FX budget), `dhol_drop` (desi
drum-bed hand-off: B's drums under A's last 1 or 2 bars before a cut, every other cut; owner rule:
both songs Punjabi and the scene profile level "full", never an experiment elsewhere) and
`chop_duck` (S18 leave room: A's drums 6 to 10 dB down under a learned vocal chop). The FX family
(S2 to S6) is `fx-moves.js`, the vocal pair (S7, S8) is in `learned-moves.js`, S1 / S9 are mashup
slots in `stem-moves.js`. Owner rule "never vocal mix a drop line": `chant_gate` and `chop_duck` refuse
(gate `drop_line`) any window over a drop or a sung line running into one. The sim counts them as
`artist_<move>` features (informational, no gate).

### atlas (pair_atlas)
Offline, deterministic pre-knowledge of which library songs go well together and how. Every
ordered pair (A to B) with cached analysis is judged by the console's own rules (Camelot score,
tempo lock, energy step, vocals, recipe, merge-hold, riff, mashup, supermove) and gets a
[works score](#works-score). Played evidence from sessions, set logs and studied sets is
attached, so a studied pair becomes a [combo](#combo). Stored in `data/cache/pair_atlas.json`;
a pair is rescored only when its inputs or the [rules hash](#rules-hash) change. Code:
`app/music_brain/atlas/pair_atlas.py`, node half `app/music_brain/pair_atlas_rules.js`. CLI:
`python -m app.music_brain.pair_atlas build | show | best | studied | import-set`. REST:
`app/ui/services/atlas_api.py` (`/api/atlas/status`, `/api/atlas/partners`, `/api/atlas/pair`). Feeds
[macros](#macro) and the autopilot's combo list.

### AUTO MIX / automix
The console's "mix into the other deck now" action: a 16-bar stem blend (synths first, kick and
bass swap on bar 8, one singer), or a 16-bar EQ blend when either deck has no stems. With a
macro selected in the MACRO panel it plays the macro's next step instead. Lives in
`app/ui/static/ai-actions.js`. Not the same as a crate automix queue, which the app
deliberately does not have (`app/ui/static/browser.js`).

### autopilot
The live decision loop that picks the next song, books the transition and plays it. It asks in
order: an armed [macro](#macro) step, [studied combos](#studied-combos), atlas combos, then the
LLM. Every pick then runs the gates (`evaluateCandidate`, `decideRecipe`, `keySafeRecipe`,
`energyStepOk`). Browser side: `app/ui/static/autopilot.js`. Server side (suggest, plan,
genre filter): `app/ui/services/autopilot_service.py`. The same code runs in the [virtual set sim](#virtual-set-sim).

### baseline
The committed score of the sim panel, `app/sim/baseline.json`. `suite --check` fails when a
change makes the mean score worse or breaks a hard metric. Per `CLAUDE.md` the current baseline
is from the stub-LLM era until it is re-recorded. Refresh with `app.sim.suite --update-baseline`.

### build-set / import-set
`pair_atlas import-set <set_id>` registers a studied set's songs as library tracks the same way
an upload does (id is `sha256(bytes)[:16]`), so nothing is separated or analysed twice
(`app/music_brain/learning/set_import.py`). `pair_atlas build` then rescores the atlas. `learn-set` runs
both at its end unless `--no-macros` is passed. **UNVERIFIED:** no subcommand literally named
`build-set` was found; the words refer to this import-then-build step.

### BYPASS_KEY_SCORE
The score (0.4) a key-agnostic recipe gets from the matcher when the pair's keys clash: a
penalty, never neutral. `app/music_brain/matching/recipe_matcher.py:BYPASS_KEY_SCORE`. See [key gate](#key-gate).

### Camelot
The 24-slot key wheel (1A to 12B) used for harmonic mixing. The live table is
`app/ui/static/dj-mind.js:camelotScore` (per `CLAUDE.md`: diagonal 0.75, minus two hours 0.6,
two hours with the letter flipped 0.3, three or more hours 0), mirrored in Python as
`app/music_brain/matching/techniques.py:camelot_score`. Analysis reports each song's key in Camelot
form. Theory: [[Harmonic Mixing & Camelot System]].

### combo
A pair the atlas scores as working well that also fits a combo move (merge-hold, riff over rap,
mashup, double drop, drop swap). The autopilot tries combos for the playing song before asking
the LLM, and a combo chain lights "COMBO x3" in the VIBE strip. A combo needs a works score of
at least `COMBO_MIN_WORKS` (65) unless it is studied. `app/ui/static/macro-mode.js:comboCandidates`.

### dj-mind
The console module that decides the musical state (build, peak, cool) and picks one move per
8-bar phrase. It owns `camelotScore` and `peakTransition` (double drop or drop swap).
`app/ui/static/dj-mind.js`.

### Named recipes
- **Echo Out:** an 8-bar echo tail on A, then B. The fallback for clashing keys and big tempo gaps.
- **Bass Swap:** both tracks play, the low end hands over on a downbeat. Only one owns sub bass.
- **Long Blend:** a long tonal overlap; needs a safe key.
- **Drop Swap:** B's drop lands where A's drop would; a tonal move and a [supermove](#supermove).
- **Quick Cut:** a hard cut on the phrase. The autopilot never plays a Hard Cut or Quick Cut
  except as the handover fallback under the Punjabi profile.

Each is a note in `DJ/05 - Transition Cookbook/` (for example [[Bass Swap]], [[Echo Out]]),
parsed into a [recipe](#recipes).

### energy rules
Dance-floor energy on a 1 to 10 scale, measured from onset density, low-end share, folded
tempo and brightness, then ranked against your own library. The next song may move at most
`MAX_STEP` levels (`RELAXED_STEP` in a relaxed session) in the direction the set arc allows;
the last-round `force` widens rises only, never falls. Python:
`app/music_brain/analysis/energy.py:next_ok`; browser twin `app/ui/static/autopilot.js:energyStepOk`.
Theory: [[Energy Management & Dynamics]].

### era gap
Release-decade distance between two songs. A gap above `MAX_ERA_GAP` decades trims the score by
`ERA_JUMP_PENALTY` rather than refusing. `app/music_brain/analysis/genre.py:era_gap`. Used by the matcher
and autopilot suggest.

### FOLLOW SET
Play a song by an artist from a studied set and the autopilot follows that set's tracklist in
order, downloading any song the library lacks. REST `GET /api/studied/sets`
(`app/ui/services/atlas_api.py`); console side in `app/ui/static/autopilot.js`. A forced plan (macro
step, studied combo, FOLLOW SET) gets no LLM re-pick but still runs the gates.

### genre_near / genre scenes
Genre-family distance shared by the autopilot's suggestion filter and the matcher, so no
unrelated-genre jump. Genre labels come from metadata or the model, never from audio.
`app/music_brain/analysis/genre.py:genre_near`. A scene profile can override it (see
[scene profiles](#scene-profiles-punjabi)).

### key gate
The rule that tonal blends need compatible keys. A tonal blend (Long Blend, Bass Swap, Drop
Swap, learned stem intro) needs a Camelot score of at least [KEY_SAFE_MIN](#key_safe_min), else
it becomes Echo Out ([keySafeRecipe](#keysaferecipe)). Stem merge, handoff and peak moves keep
their own 0.8 floors. Key-agnostic recipes on a clashing pair score
[BYPASS_KEY_SCORE](#bypass_key_score).

### KEY_SAFE_MIN
The minimum Camelot score (0.6) for a tonal blend. `app/ui/static/autopilot.js:KEY_SAFE_MIN`,
parity-tested twin `app/music_brain/atlas/pair_atlas.py:KEY_SAFE_MIN`.

### keySafeRecipe
Rewrites a tonal recipe to Echo Out when the key score is below KEY_SAFE_MIN.
`app/ui/static/autopilot.js:keySafeRecipe`; Python side `app/music_brain/matching/techniques.py:learned_pick`.

### knowledge/ folder
`app/music_brain/knowledge/`, tracked in git: macros, the learner's observations and a
[slim atlas](#slim-atlas), JSON only (no audio, stems, paths or e-mail addresses). Files:
`macros/<name>.json`, `learned_techniques.json`, `pair_atlas.json.gz`, `names.json`. The owner's `seed_combos.json`
sits beside it in `app/music_brain/`. CLI `python -m app.music_brain.knowledge export | import`. Import resolves
songs by name onto the local library. Code: `app/music_brain/matching/knowledge.py`.

### learn mode / learn-set
Study a recorded DJ set: `python -m app.music_brain.agent_bridge learn-set <url|file>
[--tracklist list.txt] [--split-minutes 60] [--keep-files]`. Stages: fetch, cut, separate,
analyze, detect, lyrics, ai_review, merge, cleanup. A set longer than `--split-minutes` is learned
in parts cut at tracklist boundaries (`set_learner.plan_parts`: neighbouring parts share one song,
each handover belongs to one part), each part checkpointed in `sets/<set_id>/parts/` so a killed run
resumes at the next part; the panel shows "part k/n". The cleanup (`app/music_brain/learning/learn_cleanup.py`)
registers every good song and ID cut in the library first, then deletes clips, clip stems, registered
song files, yt-dlp leftovers and the set recording; unregistered songs stay, listed in the result's
`cleanup.kept`. `--keep-files` skips it. A progress file whose pid is gone reads `stale`. Output: [observations](#observations) in `learned_techniques.json`, then
[import-set](#build-set--import-set) and studied macros. Code:
`app/music_brain/learning/set_learner.py`. Progress shows in the console LEARNING panel
(`app/ui/static/learn-progress.js`, `GET /api/learn/progress`) or `learn-status` on the CLI.

### learned moves
In-song moves copied from studied sets: vocal loops, vocal re-cuts, chops on the 1/8 grid, loop
extends. The console runs one kind per phrase, and each kind has its own gates (store present,
not disabled, seen in a studied set, user toggles). `app/ui/static/learned-moves.js`, reading
`app/music_brain/matching/techniques.py:learned_moves`. HUD checkboxes in `app/ui/static/index.html`.

### learned_techniques.json
The learner's store: per technique kind, its observations, count, the tempo gap and key ranges
it was seen at, and [user rules](#user-rules). Path `data/cache/learned_techniques.json`
(`app/music_brain/learning/set_learner.py:LEARNED_PATH`); a copy is exported to `knowledge/`.

### list-recipes
CLI and `GET /api/recipes`: every parsed cookbook recipe with its 17-part fields plus
`requires_stems`, `max_bpm_delta`, `camelot_compatible_only`.

### live ear
The local omni model listening to the master during a set (for example a loop that has gone on
too long). `app/ui/services/live_ear.py`, console side `app/ui/static/live-ear.js`. Advisory.

### local wins
When importing `knowledge/`, the local cache always wins: a local macro, atlas pair or studied
set is never overwritten. `app/music_brain/matching/knowledge.py`.

### macro
A fully specified, replayable set or transition: ordered track ids and per transition the
recipe, A's exit and B's entry, the merge-hold plan, the tempo decision and any learned move.
Stored in `data/cache/macros/<name>.json`. A stored decision that is no longer valid is logged
and falls back. Sources: the current set, a past session, atlas-ordered picks or the CLI
(`python -m app.music_brain.macros list | show | from-session | picks`). Code:
`app/music_brain/atlas/macros.py`; REST `/api/macros` (`app/ui/services/atlas_api.py`).

### macro kinds
`studied`, `chain`, `combo`, `seed`, `yours` (`app/music_brain/atlas/macros.py:KINDS`). The MACRO
dropdown groups them as STUDIED SETS, CHAINS, COMBOS, YOUR MACROS
(`app/ui/static/macro-mode.js:MACRO_GROUPS`).

### macro-mode
Console mode that plays the loaded macro deterministically, step by step, through every live
gate. Controls: PLAY MACRO, PLAY STEP (Shift+M), AUTO MIX, SKIP / REPEAT / EDIT / SAVE. When the
playing song is in a macro, the autopilot takes the next step with probability
`MACRO_PREFERENCE` (0.8). `app/ui/static/macro-mode.js`.

### --macros-only
`learn-set --macros-only`: no set audio and no Demucs on the mix. It only finds or downloads each
tracklist song, registers it, builds the atlas and writes `studied-set-<set_id>`
(`app/music_brain/agent_bridge.py`).

### maintain.sh
One command that keeps the library, stems and atlas current: `./maintain.sh [--steps
stems,flac,labels,review,atlas,export] [--dry-run] [--jobs N] [--max-minutes M] [--no-network]`
(wrapper for `python3 -m app.music_brain.maintain`, code `app/music_brain/maintain.py`). Steps run
in that order, each through its own existing function. stems: first the [ft cleanup](#one-stem-set-per-song)
for songs that already have a complete htdemucs_ft set (dry-run lists the folders and GB), then
`stem_service.separate` (htdemucs_ft) for every track with no stems or only fast htdemucs stems
(files over 15 min are skipped as albums, failures counted), `audio_convert.run`
for leftover WAVs, `genre_labels.label_library` missing-only, `set_learner.review_learned` for
sets none of whose observations has an `ai_rule`, incremental `pair_atlas.build` (full rescore
when the rules hash moved), and `knowledge.export_safe` with its privacy check. The labels and
review backend is `AI_REVIEW_BACKEND` (local default, `claudecode` optional); an unavailable
backend skips the step with a note, and `--no-network` skips claudecode. While the app answers
on `$PORT`, stems and flac refuse; the other steps take their own locks. Never downloads, never
runs the learner, never deletes user data. Report: JSON on stdout, saved to
`data/cache/maintain/<timestamp>.json`, logs in `data/cache/maintain/maintain.log`. When to run:
after adding songs, after learning a set, weekly. Optional schedule (not installed), saved as
`~/Library/LaunchAgents/com.aidj.maintain.plist`, then `launchctl load` it:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.aidj.maintain</string>
  <key>ProgramArguments</key><array>
    <string>/path/to/AI-DJ-Mixing-System/maintain.sh</string><string>--max-minutes</string><string>120</string>
  </array>
  <key>StartCalendarInterval</key><dict>
    <key>Weekday</key><integer>1</integer><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>/tmp/ai-dj-maintain.out</string>
  <key>StandardErrorPath</key><string>/tmp/ai-dj-maintain.err</string>
</dict></plist>
```

### merge-hold
Merge, then hold, then transition: the two tracks share stems (B's drums and bass under A's vocal
and synths, one tonal owner, one sub owner), hold together for whole 8-bar phrases while the pair
stays clean, then hand over on a downbeat. Hold length comes from measured stem energy and vocal
gaps, never a fixed constant. `app/ui/static/stem-moves.js:holdPlan` and `mergeTransitionPlan`;
Python scoring `app/music_brain/render/merge.py`. It is what a merge combo plays. Theory:
[[Stems Transition]].

### one stem set per song
Owner rule: once a song has a complete htdemucs_ft 4-stem set (manifest ok, all 4 files on
disk), its other stem folders (`<hash>_htdemucs`, `<hash>_htdemucs_vocals`, any non-ft model) are
removed: renamed out to `cache/stems_trash/` (atomic) and deleted. It happens right after an ft set
is written (`stem_service.separate`, `StemWorker.finish`) and in the maintain.sh stems step; never
when the ft set is incomplete. The 2-stem vocal readers (`server._vocals_stem_impl`,
`_cached_vocal_regions`, `_vocals_cached`) use the ft vocals stem first, so a pruned 2-stem folder
never forces a new separation. Code: `app/music_brain/audio/stem_service.py:prune_non_ft`.

### observations
One detected technique sighting from a studied set (set, DJ, position, songs, overlap, tempo gap,
key score). Stored per kind in `learned_techniques.json`; counted in `knowledge export`.

### octave fold
Tempos are folded so half and double time count as a match: `app/ui/static/tempo-rule.js:lockRate`
tries rates 1, 2 and 0.5 and takes the closest. Energy folds tempo into 80 to 150 BPM, so half-time
DnB counts at its full feel (`app/music_brain/analysis/energy.py`).

### pair atlas vs matcher
The [atlas](#atlas-pair_atlas) is an offline table over the whole library, scored with the
console's own JS rules. The matcher (`app/music_brain/matching/recipe_matcher.py:RecipeMatcher`) scores
one pair on request (`agent_bridge match`, `POST /api/match`) against the cookbook recipes and
returns explained candidates. Manual points go through `RecipeMatcher.resolve_candidate`.

### performance pads / sampler
The PERFORMANCE panel: sample pads that trigger one-shots, including user uploads
(`POST /api/samples`, stored in `data/cache/samples/`). `app/ui/static/performance.js:SAMPLE_PADS`
and `app/ui/static/sampler-deck.js`. The auto sampler fires rationed one-shots on its own
(`app/ui/static/auto-sampler.js`).

### phrase snapping
Transition entry and exit points snap to 8-bar (32-beat) phrase boundaries. Analysis emits
`phrase_boundaries_8bar[]`; manual `--a-time` / `--b-time` snap to the nearest real boundary.
Theory: [[Phrasing & Structure]].

### recipes
The 28 cookbook transitions (the 17-part notes in `DJ/05 - Transition Cookbook/`), parsed into
executable recipes by `app/music_brain/matching/knowledge_parser.py:TransitionRecipe`.

### recordings
MediaRecorder captures of a live console mix, uploaded with `POST /api/recordings` and stored in
`data/cache/recordings/` (`app/ui/server.py`).

### REST surface
`uvicorn app.ui.server:app`. Tracks, analysis, separation, recipes, match, preview, audio,
samples, recordings (`app/ui/server.py`), atlas, macros and studied sets (`app/ui/services/atlas_api.py`).
All ids are content hashes.

### replay
Running the sim from a recorded fixture with zero network: `virtual_set --replay NAME`. Keyed by
subject, not prompt text; a call with no recorded reply is a replay miss. See `app/sim/README.md`.

### rules hash
A hash of the files whose rules the atlas applies (`app/music_brain/atlas/pair_atlas.py:RULE_FILES`,
`rules_hash`). Editing any of them invalidates every atlas pair; `knowledge import` checks it.

### scene profiles (Punjabi)
The autopilot's rules for a Punjabi / bhangra / desi set, with the console setting `PUNJABI`
(sent as `punjabi_profile`) set to `off`, `auto` or `on`. `off` is today's behaviour byte for
byte; `auto` applies the full profile when both songs are Punjabi and a handover-only level
(Quick Cut fallback, no era gate) when one is. Code: `app/music_brain/analysis/scene_profile.py`, console
copy `app/ui/static/scene-profile.js`. Most values are marked GUESS in their source note. The task
brief said this lives only on branch `punjabi-scene-profile`; in fact that branch is already
merged into main (commit c28f1ee), so this entry describes main.

### seed combos / auto_seed
`app/music_brain/seed_combos.json` (beside `knowledge/`) holds the owner's committed combos for pairs the atlas
does not serve yet. `app/music_brain/matching/knowledge.py:auto_seed` imports `knowledge/` when it or the
local library changes.

### set_learner
The learning pipeline behind [learn-set](#learn-mode--learn-set), plus the user-rule store.
`app/music_brain/learning/set_learner.py`.

### slim atlas
A compact copy of the atlas (the best partners per song and per move) for git.
`app/music_brain/matching/knowledge.py:slim_atlas`.

### stem intro
Bringing B in stem by stem (for example synths first) instead of a full-track fade. A learned stem
intro is a tonal blend, so it obeys the key gate. Refused when the stem has no energy in the window
(`app/ui/static/stem-moves.js:pickIntro`).

### stems / Demucs / separation
4-stem (vocals, drums, bass, other) or 2-stem (vocals, instrumental) separation with Demucs
(`htdemucs_ft`), keyed by SHA-256 and cached under `data/cache/stems/`, so a track is never
separated twice. `app/music_brain/audio/stem_service.py`; CLI `agent_bridge separate`; REST
`POST /api/tracks/{id}/separate`. Stems are stored as 16-bit FLAC (bit-exact, about 0.42x the
WAV size) with a v2 `manifest.json` (`version`, `format`, `stems`). Readers try `.flac` first and
fall back to `.wav`, so old WAV entries still load. `python3 -m app.music_brain.audio_convert`
converts an existing WAV cache in place (dry run by default; `--apply`, `--only stems|keylock`,
`--limit N`, `--jobs N`, `--max-mem-gb G`); every file is decoded and compared before its WAV is
deleted. `--jobs` converts N folders at once in worker processes (default min(4, cpus / 2), capped
at cpus - 1 and at `--max-mem-gb` / 1.6 GB per folder); Ctrl-C stops handing out folders and lets
running ones finish, so every folder ends all-WAV or all-FLAC.

### step log
Per-song log of each step the autopilot took (song, recipe, why), shown in the browser and saved
server side. `app/ui/static/step-log.js`, `app/ui/services/song_log.py`, `/api/session/steps`.

### studied combos
The transitions real DJs played in studied sets, taken from each studied set's tracklist and
learner observations. Both songs resolve to library ids by name; a resolved pair becomes the
`studied` evidence class on its atlas pair and a combo the console tries first, badged
"STUDIED COMBO (&lt;DJ&gt; set)". `app/music_brain/atlas/studied_combos.py`.

### suite
The regression gate: the fixed panel of recorded sets (`app/sim/panel.json`), replayed and scored.
`python3 -m app.sim.suite --check` exits 1 when worse than [baseline](#baseline).
`app/sim/suite.py`.

### supermove
The big announced moves: double drop, drop swap, plus merge, mashup and riff over rap, called out
by the mascot. `app/ui/static/dj-mind.js:peakTransition`, `app/ui/static/mascot.js:CUE_MOVES`; the
atlas scores them as the `supermove` move (`app/music_brain/atlas/pair_atlas.py:MOVE_ALIASES`).

### tempo rules
- Pitch-locked blends: within about 6% (`app/ui/services/autopilot_service.py:TEMPO_LOCK_PCT`).
- Key-locked stems: up to [8%](#8-keylock).
- Console pitch fader range: `app/ui/static/tempo-rule.js:PITCH_RANGE_PCT`.
- Tempo changes are gradual: at most `MAX_TEMPO_PCT_PER_BAR` per bar on an audible deck.
- Half and double time count as a match ([octave fold](#octave-fold)).
- Bigger gaps go Echo Out or Stem Bridge. Theory: [[Beatmatching & Tempo]], [[Genre Bridge Playbook]].

Whole-song BPM uses a section-consensus estimate (`app/music_brain/analysis/tempo.py:robust_tempo`).

### titles
Human names for macros, shown in the MACRO dropdown, for example "Anyma @ Live from Atomium
(studied set, 12 songs)". The slug stays the stable id. `app/music_brain/atlas/macros.py:title_of`.

### toggle drawer
The panel of on/off switches for console features, grouped by kind.
`app/ui/static/toggle-drawer.js`.

### track list set-&lt;id&gt;
A studied set's tracklist and study live under `data/cache/sets/<set_id>/` (`study.json`, `songs/`).
Its macros are `studied-set-<set_id>` (whole set) and `studied-<set_id>-<n>` (per transition).

### user rules
The owner's refinement of a learned technique after hearing it live (for example "rap about 9 dB
under the riff"), or `--disable` to keep it out of ranking. Kept across re-learning.
`app/music_brain/learning/set_learner.py:add_user_rule`.

### vibe
Measured "vibe" features the name cannot tell the LLM: loudness, brightness, percussive density,
mean energy. `vibe_distance` turns the deltas into one number plus reasons, so the autopilot can
reject a mood flip. `app/music_brain/analysis/vibe.py:vibe_distance`. The VIBE strip in the console
(`app/ui/static/vibe-ui.js`) shows what the AI hears, plans and does, including the combo streak.

### virtual set sim
Runs the console's real browser JS in node against the real API on a virtual clock, and scores a
whole set with one number (lower is better). Record once with the real model, then replay.
`app/sim/virtual_set.py`, scorer `app/sim/scorer.py`, compare `app/sim/compare.py`, log
`app/sim/LEARNINGS.md`. It judges decisions, not sound.

### works score
The atlas's 0 to 1 blend (shown as 0 to 100) of key, tempo gap, energy, vocal cleanliness, stems,
merge and combo fit for one pair. `app/music_brain/atlas/pair_atlas.py:works_score`.

## Key numbers

| Name | Value | Source |
|---|---|---|
| KEY_SAFE_MIN | 0.6 | `app/ui/static/autopilot.js:KEY_SAFE_MIN` |
| BYPASS_KEY_SCORE | 0.4 | `app/music_brain/matching/recipe_matcher.py:BYPASS_KEY_SCORE` |
| Key-lock stretch cap | 8% | `app/ui/static/tempo-rule.js:KEYLOCK_RANGE_PCT` |
| Pitch-lock tempo gap | 6% | `app/ui/services/autopilot_service.py:TEMPO_LOCK_PCT` |
| Max tempo change per bar | 0.25% | `app/ui/static/tempo-rule.js:MAX_TEMPO_PCT_PER_BAR` |
| Combo min works score | 65 | `app/ui/static/macro-mode.js:COMBO_MIN_WORKS` |
| Macro step preference | 0.8 | `app/ui/static/macro-mode.js:MACRO_PREFERENCE` |
| Max era gap (decades) | 1 | `app/music_brain/analysis/genre.py:MAX_ERA_GAP` |
| Phrase | 8 bars / 32 beats | [[Phrasing & Structure]] |
| Sub-bass owner | below 120 Hz, one deck | [[EQ & Frequency Management]] |
