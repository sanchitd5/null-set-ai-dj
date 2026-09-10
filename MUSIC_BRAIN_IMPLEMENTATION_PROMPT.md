# Master Implementation Prompt: AI Music Brain Engine & DJ Assistant

> **Target Repository:** `AI-DJ-Mixing-System` (`DJAITest`)
> **This is the single unified spec** for the AI Music Brain — it supersedes and merges the original
> functional spec (`ai_dj_assistant_spec.md`) and every feature discussed since. Do not maintain a
> separate spec document; extend this one.
> **Ground-Truth Knowledge Base:** `./DJ/` (78+ markdown notes, 28 transition recipes)
> **Existing Python DSP Pipeline:** `mixing_engine.py`, `structure_detector.py`, `bpm_lookup.py`
> **Agent Ecosystem:** Google Antigravity (`AGENTS.md`), Claude Code (`CLAUDE.md`), OpenAI Codex (`CODEX.md`)

---

## 0. Vision & the DJ Mental Model We're Encoding

A personal, interactive tool: load 2+ songs, split them into parts (vocals/drums/bass/instrumental),
loop and play with those parts, and merge tracks at AI-suggested points using real DJ transition
technique — without needing to know DJ theory going in. Recreate, with AI as co-pilot, the magic of
a real DJ performing a live set.

**Not real-time performance software.** You render results and can re-tweak. The DJ **console** (deck
controls, jog wheels, EQ, faders) is for auditioning and hands-on feel, not for a live audience.

Before any feature list, here's the mental model professional DJs use — this is what the AI
recommendation layer and the console both exist to imitate:

- **Phrasing, not random timing.** Songs are built in blocks of 8 or 16 bars. A DJ almost never starts
  a transition mid-phrase — every transition point we compute or let you pick snaps to a real phrase
  boundary.
- **The EQ is the real mixer, not the volume fader.** Pros blend by cutting bass on the outgoing track
  and bringing in the incoming track's bass, so the low end never clashes ("two basslines fighting" =
  mud). This is a hard rule in our DSP (`dsp_rack.py`'s Linkwitz-Riley crossover), not a suggestion.
- **Energy arc over the whole set, not just track-to-track.** Chosen not just for "do these songs mix
  well" but "does this raise, hold, or lower the energy right now."
- **Reading structure, not just BPM/key.** A good transition point is an outro, a breakdown, or right
  before a build/drop — not the middle of a verse or chorus hook.
- **Effects are used with intent.** Echo/reverb throws happen on the way out; filter sweeps build or
  release tension on the way in.
- **Loops buy time and build tension.** Looping a bar or two of a breakdown lets you line up the next
  track perfectly or build anticipation.
- **Acapella/instrumental swaps are deliberate technique**, not an accident of a bad blend.

Every feature below maps back to one of these ideas.

---

## 1. Feature Spec (by category, with implementation status)

Legend: `[x]` done and tested · `[~]` partially done / needs polish · `[ ]` not started ·
🧱 needs new infrastructure (external library, hardware, or new backend surface) — flagged, don't
attempt casually.

### 1.1 Track Import, Analysis & Splitting
- [x] Load local audio files (MP3/WAV) via upload.
- [x] Auto-detect BPM, musical key (Camelot), beatgrid, phrase boundaries (8/16-bar), structural
      sections (intro/verse/breakdown/build/drop/outro), energy curve, vocal presence map.
      → `music_brain/analyzer.py`
- [x] 4-stem (vocals/drums/bass/other) and 2-stem (vocals/instrumental) separation via Demucs, disk
      cached by content hash, GPU-accelerated. → `music_brain/stem_service.py`
- [x] Display analysis as an annotated waveform: waveform, phrase/exit/entry point markers, beat-grid
      overlay (tick per beat, brighter on downbeats), and key display, all on the console itself.
- [ ] Stem control UI (mute/solo/fader per stem in the console) — placeholders exist, not wired.

### 1.2 Stem Playground ("play with the parts")
- [ ] Loop any stem or section of a stem (deck-level loop exists; per-stem not yet).
- [ ] Slice/trim a stem to a region (full-track trim exists via Regions plugin; per-stem not yet).
- [ ] Reverse playback. 🧱 needs a reversed-buffer copy, not hard, just not built.
- [ ] Pitch shift independent of tempo, time-stretch independent of pitch, on a stem specifically.
- [x] Volume/gain per track (not yet per stem).

### 1.3 Effects Library
| Effect | Status | Notes |
|---|---|---|
| 3-band EQ | [x] | `dsp_rack.py` (offline render) + live rotary knobs in the console |
| HPF/LPF filter sweep | [x] | `dsp_rack.py`, used by the Filter Transition recipe |
| Crossfade | [x] | equal-power crossfader in the console + renderer |
| Echo/Delay | [x] | tempo-synced, used by Echo Out recipe |
| Reverb | [x] | algorithmic comb-filter reverb, `dsp_rack.py` |
| Reverse | [ ] | 🧱 trivial (reversed buffer), just not built |
| Loop roll/stutter | [ ] | |
| Gate/beat-repeat | [ ] | |
| Backspin | [ ] | |
| Pitch bend | [ ] | temporary tap-nudge on the pitch fader |
| Flanger / Phaser / Bit-crusher / Ping-pong delay / Slicer | [ ] | each a real new DSP unit |
| FX trigger pads + wet/dry control | [ ] | needs the FX rack above first |

### 1.4 Merge / Transition Feature (the core ask)
- [x] Pick a rough point on Track A and Track B by clicking the waveform.
- [x] Points snap to the nearest real 8-bar phrase boundary (never mid-phrase).
- [x] AI-recommended transition points, each scored 0-100% and explained in plain English.
- [x] Manual override: pick any of the 28 recipes yourself via a dropdown, with your own points —
      genuinely honored, not silently replaced by the AI's own guess (see Phase 4.1 below; this was a
      real bug that got found and fixed).
- [x] Preview before committing (renders in ~2-3s on this dev machine's CPU).
- [x] Render the transition or the full multi-track mix.
- [x] Technique categories implemented as dedicated DSP chains: Bass Swap, Echo Out, Quick/Hard Cut,
      Filter Transition. The other 24 recipes get a real (non-naive) EQ-blend fallback, not a plain
      volume crossfade.

### 1.5 Interactive DJ Console UI
**Decks**
- [x] 2-deck layout (A/B), clear visual separation.
- [x] Play/Pause, Cue button + cue point, track seeking (jog-wheel drag scrub + waveform click).
- [x] Waveform visualization, per-deck BPM display, live elapsed/total time.
- [x] Tempo/pitch slider (±8%).
- [x] Pitch bend (momentary nudge, layered on top of the sustained pitch fader via `_bendPercent`).
- [ ] Keylock. 🧱 needs a phase-vocoder/granular time-stretch (e.g. SoundTouchJS) — raw Web Audio
      `playbackRate` always changes pitch with speed.
- [x] Beat Sync: SYNC button matches one deck's effective tempo to the other via the pitch fader
      (clamped to ±8%, so it correctly declines wildly mismatched tempos rather than pretending).
- [ ] Manual beat *phase* alignment (tempo-match exists; phase/beat-grid alignment does not).
- [x] Beatjump (±1/±4/±8/±16 beats per deck).

**Mixer**
- [x] Crossfader (equal-power curve), per-deck volume fader, 3-band EQ (rotary knobs, drag-to-turn,
      wired to the same low/mid/high split `dsp_rack.py` uses for real rendering).
- [x] Separate pre-EQ gain knob per channel, distinct from the volume fader.
- [ ] Headphone cue/preview bus. 🧱 needs a second audio output route
      (`HTMLMediaElement.setSinkId`, inconsistent browser support) or real hardware split.
- [x] Master volume (shared `masterGain` node both decks' crossfader output feeds through).
- [x] Real-time VU meters (`AnalyserNode`-driven) per deck.

**Performance**
- [x] Hot cues: 4 per deck, click to set, click again to jump.
- [x] Loop controls: beat-length stepper (1/2/4/8/16/32 beats), loop on/off toggle.
- [ ] Explicit Loop-In/Loop-Out (current loop is length-based from the current position, not a
      two-point in/out marker).
- [ ] Slip-style loop performance. 🧱 needs dual-position bookkeeping (loop plays while the "real"
      playhead keeps advancing underneath).
- [x] Vinyl/scratch-style jog wheel interaction: click to play/pause, drag to scrub, spinning-groove
      animation while playing, animated tonearm that engages on play.
- [x] Reverse (cached reversed-buffer copy, built once per track).
- [x] Brake/start-stop effect (`playbackRate` ramps 1→0 over 0.8s via `linearRampToValueAtTime`).

**Sampler**
- [x] 8-pad synthesized one-shot sampler (kick/snare/clap/hat/open-hat/tom/zap/sweep), per-pad volume
      knob, keyboard-triggerable. Custom sample upload (`/api/samples`) exists on the backend but isn't
      wired into the pad grid yet.

**DJ Workflow**
- [ ] Track browser/library, drag/load onto decks (currently file-picker upload only), playlist/queue,
      automix. 🧱 This is a new product surface (persistence + metadata management), not an
      audio-engine feature — needs its own backend design.
- [x] BPM + key metadata (computed and shown live in the console's jog-wheel label ring).
- [x] Recording: `MediaStreamDestination` off the master output + `MediaRecorder` → client-side webm
      download link. (`POST /api/recordings` exists on the backend for persistence but isn't wired in.)
- [x] Keyboard shortcuts: full transport/hot-cue/loop/beatjump/FX/sampler scheme with an on-screen
      toggleable legend (see the console's `?`/backtick-toggled panel for the exact key map).
- [ ] MIDI/controller mapping. 🧱 Web MIDI API works, but needs real MIDI hardware to test against
      and a mapping UI — large scope, untestable in this dev environment.

**UI/UX**
- [x] Clear Deck A/B separation, per-deck waveform, dark performance-focused interface.
- [x] Beat-grid visualization overlay on the waveform (tick per beat, brighter on downbeats).
- [x] Key prominently displayed (Camelot key in each deck's jog-wheel label ring).
- [x] Active loop indicator (button state), active hot-cue indicator (button state, RGB-backlit pads).
- [x] FX state indicators (lit pad + active-effect label per deck).
- [~] Responsive layout: works down to ~1100px (console reflows to a single column below that); not
      tuned for phone-sized screens, and a jog-wheel/knob-drag console is inherently a desktop/tablet
      tool by nature, same as real DJ software.

### 1.6 AI Recommendation Layer
- [x] Rules layer (fast, deterministic, free): BPM gap, Camelot key compatibility, phrase-boundary
      alignment, vocal-overlap avoidance. → `music_brain/recipe_matcher.py`
- [ ] LLM layer on top: plain-English explanations exist (template-based, not LLM-generated), but
      free-form requests ("give me something more dramatic here") are not implemented.

### 1.8 Backend Blob Storage (added, not in original spec)
- [x] `POST/GET /api/samples`, `POST/GET /api/recordings` — content-addressed blob storage (same
      sha256-hash convention as `/api/tracks`) added to support the sampler and recording features
      above. 10 tests in `tests/test_ui_server.py`. `AGENTS.md`/`CLAUDE.md` updated with `music_brain`
      CLI invocation instructions for other agents (closes the Phase 4 checkbox that was left open).

### 1.7 Brainstormed Extras (not started)
Hot-cue *naming* (cues exist but aren't named/labeled), transition presets (save a favorite effect
chain), whole-set energy-curve visualizer (once merging 3+ tracks), auto playlist ordering by
BPM/key/energy, A/B compare (render two options for the same points), personal clip/loop library,
"explain like I'm not a DJ" mode (already a standing principle in the scoring explanations, could go
further), practice/learning mode (why a merge worked or didn't), harmonic path planner for a whole
set, "one more song" suggestion mode, manual-mix recorder (log your own picks as a taste dataset).

### 1.9 "Pulse DJ Pro" Visual/UX Restyle (added, not in original spec) — done
Restyled the console to a user-supplied hardware mockup: cyan (A) / coral (B) duotone, JetBrains Mono
+ Space Grotesk, a top header (REC/clock/master VU/help), split per-deck FX bars, dual-stacked
waveforms (WaveSurfer autoCenter/autoScroll rather than a rebuilt fixed-needle renderer, to keep the
existing click-to-set-point/trim/beat-grid features intact), 12-segment VU ladders, and a real track
browser (search/sort/Load-to-A/B against `GET /api/tracks`, no fake data). Also added: TAP tempo,
help overlay (replacing an always-visible legend), universal knob drag-to-turn (touch-friendly, with
Shift-fine mode and keyboard support), a square backlit FX-power pad per deck, and vinyl spin-up/
spin-down on PLAY/PAUSE itself (CUE/hot cues/beatjump stay instant). Confirmed via web research
against YouDJ's actual feature set (turntables, 3-band EQ, crossfader, pitch, scratching, sync,
keylock, loops/cues, automix, 16 FX, 80-sample sampler, MIDI, headphone cue) that everything requested
either already existed or was already flagged 🧱 (keylock, MIDI, headphone cue, automix/library) —
those stay honestly omitted/disabled, not faked. Also fixed a real pre-existing bug found during this
pass: the 3-band EQ knobs rotated visually but were never actually wired to the BiquadFilterNodes.

### Explicitly out of scope
Live/real-time performance hardware controllers with live audience mixing, multi-user/cloud
collaboration, commercial/broadcast use. (Personal/educational use with music you own — same
disclaimer as any DJ software.)

---

## 2. Directory Structure

```
DJAITest/
├── DJ/                                 # Ground-truth Obsidian Knowledge Base
│   ├── 02 - Music Theory/              # Camelot key, phrasing & 32-beat boundaries
│   ├── 04 - Core Techniques/           # EQ real estate, loops, effects mastery
│   └── 05 - Transition Cookbook/       # 28 standardized 17-part transition recipes
│
├── music_brain/                        # The Core AI Music Brain Package
│   ├── __init__.py
│   ├── config.py                       # Paths, cache directories, model settings
│   ├── knowledge_parser.py             # Parses & indexes ./DJ/ markdown notes into executable recipes
│   ├── analyzer.py                     # BPM, beatgrid, downbeats, Camelot key, energy & vocal map
│   ├── stem_service.py                 # Demucs (htdemucs_ft) 4-stem / 2-stem separation with disk caching
│   ├── recipe_matcher.py               # Evaluates track pairs against 28 recipes; ranks, explains, and
│   │                                    #   resolves manual point/recipe overrides
│   ├── dsp_rack.py                     # Pure scipy/numpy DSP rack (3-band EQ, LR crossover, sweeps, delay, reverb, limiter)
│   ├── transition_renderer.py          # Renders micro-previews (15-30s) and full master mixes
│   └── agent_bridge.py                 # Python API & CLI interface for Antigravity / external scripts
│
├── ui/                                 # Interactive Web Workbench + DJ Console
│   ├── server.py                       # FastAPI backend exposing the Music Brain REST endpoints
│   └── static/
│       ├── index.html                  # Console layout: 2 decks + mixer
│       ├── app.js                      # Upload, AI suggestions, manual point picking, preview
│       ├── deck-controller.js          # Web Audio DJ engine: jog wheels, EQ, faders, loop, hot cues, sync
│       └── style.css
│
├── cache/                              # Persistent local storage (gitignored)
│   ├── stems/                          # Cached Demucs stems by audio file hash
│   ├── analysis/                       # Cached JSON analysis data
│   ├── previews/                       # Rendered transition preview snippets (.mp3)
│   └── uploads/                        # Uploaded track files, keyed by content hash
│
├── tests/                              # pytest suite (112 tests as of Phase 5)
├── MUSIC_BRAIN_IMPLEMENTATION_PROMPT.md # This document — the single unified spec
└── AGENTS.md / CLAUDE.md / CODEX.md    # Cross-agent operating rules
```

---

## 3. Detailed Component Specifications

### 3.1 Knowledge Parser (`music_brain/knowledge_parser.py`)
- Reads all 28 recipe markdown files in `DJ/05 - Transition Cookbook/` at startup.
- Extracts YAML frontmatter (`difficulty`, `tags`); parses the standardized 16-section template
  (`Musical principle`, `Setup`, `Step-by-step`, `When to use it`, `When NOT to use it`, `Best genres`,
  `Risk of sounding gimmicky`, etc.) into structured `TransitionRecipe` objects.
- Auto-tags each recipe with acoustic prerequisites: `requires_stems`, `max_bpm_delta`,
  `camelot_compatible_only`.

### 3.2 Stem Service (`music_brain/stem_service.py`)
- 4-stem (`vocals`, `drums`, `bass`, `other`) and 2-stem (`vocals`, `instrumental`) separation via
  Demucs (`htdemucs_ft`).
- SHA-256 content hash → `cache/stems/{hash}_{model}[_{two_stems}]/`; returns cached paths instantly on
  a repeat call. `two_stems="vocals"` mode is ~2x faster when a full 4-way split isn't needed.

### 3.3 Audio Analyzer (`music_brain/analyzer.py`)
1. **Tempo & Beatgrid:** BPM + beat timestamps; downbeats as every 4th beat.
2. **Phrase Boundaries:** 8-bar (32-beat) and 16-bar (64-beat) markers.
3. **Camelot Key Detection:** chroma features correlated against major/minor key profiles, mapped to
   Camelot notation (1A-12B).
4. **Vocal Presence Map:** moving RMS of the Demucs `vocals.wav` stem; regions where RMS > -38 dBFS.
5. **Energy Curve & Structure:** moving RMS/energy segmented into intro/verse/breakdown/build/drop/outro.

### 3.4 Recipe Matcher & Scorer (`music_brain/recipe_matcher.py`)
- **Scoring**, given Track A (outgoing) and Track B (incoming):
  1. Camelot key distance: 1.0 same key, 0.9 ±1 hour, 0.85 relative major/minor, 0.8 +2 energy boost,
     0.1 clashes ≥3 hours.
  2. BPM difference: seamless (≤3%), ramping (≤6%), cut/echo required (>6%).
  3. Phrase-boundary alignment: Track A's exit lands in a breakdown/outro/drop/build section; Track B's
     entry lands in an intro/breakdown, or the exact start of a verse.
  4. Vocal collision penalty: heavily penalized unless the recipe uses stems to mute one side.
  5. Overkill penalty: a big-gap tool (Echo Out, Backspin, ...) is penalized when the pair already
     blends cleanly on key and BPM — matches each recipe's own "When NOT to use it" guidance.
- **`resolve_candidate()`:** the manual-override entry point. No args → AI's own top pick. Recipe name
  only → that recipe at the AI's own best-guess points. Either time given → genuine override, snapped
  to the nearest phrase boundary, with any omitted time falling back to the AI's guess. This is what
  fixes the "manual point silently discarded" bug found during Phase 4.1.
- **Output:** top-N ranked `TransitionCandidate`s, each with recipe name, score (0-100%), timestamps,
  and a plain-English explanation.

### 3.5 Studio DSP Rack (`music_brain/dsp_rack.py`)
- **Engine:** pure `scipy.signal` + `numpy`. (Originally speced to use Spotify's `pedalboard`, which was
  found to segfault unconditionally on the dev machine across 2 versions and multiple numpy versions —
  reimplemented from scratch, no `pedalboard` dependency.)
- 3-band isolator EQ (independent low/mid/high gain via LR-style split-scale-recombine).
- 4th-order Linkwitz-Riley crossover at 120Hz — the structural guarantee that sub-bass is never owned
  by two tracks simultaneously during a Bass Swap.
- Time-varying HPF/LPF filter sweeps with resonance.
- Tempo-synced delay (1/4, 1/2, 3/4, 1 beat) and algorithmic comb-filter reverb.
- Per-stem/track gain, and a transparent peak limiter (threshold -0.5dB) as the final mastering stage.

### 3.6 Transition & Mix Renderer (`music_brain/transition_renderer.py`)
- **Micro-Preview Mode:** extracts a 15-30s window around the transition point, applies the recipe's
  DSP chain, masters, exports `.mp3`. Measured render time on this dev machine's CPU: ~1.7-2.9s for a
  15s clip (short of the original <1.5s target, still fast enough for interactive use).
- **Full Master Mix Mode:** stitches the complete set of tracks per the matched transition blueprints.

### 3.7 Agent Bridge & CLI (`music_brain/agent_bridge.py`)
```bash
python -m music_brain.agent_bridge analyze songs/track1.mp3
python -m music_brain.agent_bridge separate songs/track1.mp3 --stems 4
python -m music_brain.agent_bridge match songs/track1.mp3 songs/track2.mp3
python -m music_brain.agent_bridge preview songs/track1.mp3 songs/track2.mp3 --recipe "Bass Swap" \
    --a-time 60 --b-time 10 --out preview.mp3
python -m music_brain.agent_bridge list-recipes
```
Every command prints structured JSON; `--a-time`/`--b-time` are genuine manual overrides (see 3.4).

### 3.8 Interactive Web Workbench + DJ Console (`ui/`)
- FastAPI backend (`ui/server.py`): upload, analysis, stem separation, matching, preview rendering,
  audio streaming — all as REST endpoints returning JSON.
- Frontend (`ui/static/`): dual Wavesurfer.js waveforms with click-to-set (phrase-snapped) transition
  points and drag-to-trim regions; AI suggestion cards; an effect dropdown covering all 28 recipes for
  manual override; a real two-deck DJ console built on raw Web Audio API (see 1.5 above for the full
  feature breakdown) — vinyl jog wheels, rotary EQ knobs, pitch/volume faders, crossfader, VU meters,
  hot cues, loop stepper, SYNC.

---

## 4. Implementation History (phases actually executed)

### Phase 1: Core Foundation & Knowledge Ingestion — done
`music_brain/` package scaffolded; `knowledge_parser.py` parses all 28 recipes; 18 tests
(`tests/test_knowledge_parser.py`).

### Phase 2: Stem Separation & Audio Analysis Engine — done
`stem_service.py` (Demucs, GPU-accelerated via CUDA torch, hash-cached); `analyzer.py` (librosa BPM/
beatgrid/key/structure/vocal-presence); JSON caching in `cache/analysis/`.

### Phase 3: DSP Rack & Transition Snippet Renderer — done, with a deviation
`dsp_rack.py` built in pure scipy/numpy instead of `pedalboard` (see 3.5 for why); `transition_renderer.py`
with dedicated chains for 4 recipes + a real EQ-blend fallback for the rest; DSP tests verify zero
sub-bass doubling and zero clipping.

### Phase 4: Recipe Matcher & Agent Bridge — done
`recipe_matcher.py` multi-criteria scoring; `agent_bridge.py` CLI + Python API.
Still open: update `AGENTS.md`/`CLAUDE.md` with music_brain invocation instructions for other agents.

### Phase 4.1: Manual Transition-Point Override — done
Fixed a real bug (manual points silently discarded whenever the AI had already scored that recipe —
true for nearly every recipe) via `RecipeMatcher.resolve_candidate()`. Added click-to-set phrase-snapped
points, an effect dropdown for all 28 recipes, and `--a-time`/`--b-time` CLI/API support. 12 tests.

### Phase 4.2: DJ Console UI — done
Extended the workbench into a real two-deck console on raw Web Audio API: vinyl jog wheels (click/
drag/spin/tonearm), transport (CUE/PLAY/SYNC), rotary EQ knobs, pitch/volume faders, VU meters,
crossfader, 4 hot cues/deck, loop stepper, waveform trim (Regions plugin), live BPM/time readouts.

### Phase 5: Interactive Web UI Workbench — done
`ui/server.py` FastAPI endpoints; verified end-to-end live in a browser (upload → AI suggests →
preview plays) and via CLI/curl against real Bollywood test tracks.

### Phase 6: DJ-controller feature backlog — not started
See section 1.5 above for the full item-by-item breakdown of what's feasible next vs. what needs new
infrastructure (keylock, headphone cue, MIDI mapping, track library/playlist/automix).

---

## 5. Verification & Acceptance Criteria

1. **Knowledge Grounding:** The system correctly identifies and executes recipes from
   `DJ/05 - Transition Cookbook/` without falling back to naive linear volume crossfades. ✅
2. **Sub-Bass Protection:** Sub-bass (<120Hz) is never owned by two tracks at once across a Bass Swap
   transition. ✅ verified by `tests/test_dsp_rack.py` and `tests/test_transition_renderer.py`.
3. **Phrase Snapping:** All transition entry/exit points — AI-suggested or manually picked — land on a
   real 8-bar phrase boundary. ✅
4. **Fast Auditioning:** A 15s preview renders in a few seconds on CPU (~1.7-2.9s measured on this dev
   machine; short of the original <1.5s target but still fast enough for interactive use).
5. **Agent Usability:** `python -m music_brain.agent_bridge match <song1> <song2>` outputs structured
   JSON. ✅ Verified via subprocess test and live against real Bollywood tracks.
6. **Manual Override Honesty:** Manually-picked points are never silently discarded once a recipe is
   also AI-scored. ✅ verified by `tests/test_recipe_matcher.py`.
7. **Live UI Verification:** Full loop verified live in a real browser — upload, click-to-set points,
   drag-to-trim, jog wheel play/scrub, knob drag, SYNC, crossfader, preview playback — not just
   automated tests.
