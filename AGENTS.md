# AGENTS.md — AI Agent Operating Guide

> **Repository:** `AI-DJ-Mixing-System` (`DJAITest`)  
> **Dual Architecture:** Python Audio Processing Pipeline + Native Obsidian DJ Knowledge Base  
> **Last Updated:** September 2026

This guide instructs AI agents, autonomous coding assistants, and human engineers on how this repository is structured, how to navigate and query the embedded **Obsidian DJ Knowledge Base** (`./DJ/`), and how the DJ theory informs the **Python Mixing Engine** (`mixing_engine.py`, `structure_detector.py`, etc.).

---

## Multi-Agent Ecosystem Interoperability

This repository is pre-configured to work out-of-the-box across all leading AI coding assistants:

| AI Assistant / Environment | Configuration Entry Point | Role & Discovery Mechanism |
| :--- | :--- | :--- |
| **Claude Code** (`claude`) | [`CLAUDE.md`](CLAUDE.md) | Automatically loaded at startup by Claude Code CLI. Defines commands, test scripts, and coding style. |
| **Google Antigravity** (`agy`) | [`AGENTS.md`](AGENTS.md) | Natively loaded as an active workspace rule (`<RULE[...AGENTS.md]>`). Master repository operating guide. |
| **OpenAI Codex / Copilot** | [`CODEX.md`](CODEX.md) & [`.github/copilot-instructions.md`](.github/copilot-instructions.md) | Loaded by Codex, ChatGPT code interpreter, Cursor, and GitHub Copilot for DSP pipeline contracts. |

All three assistants share the identical ground-truth specifications in `./DJ/` and follow the same execution and coding discipline.

---

## 1. Repository Architecture Overview

This repository unites two complementary domains:

```
DJAITest/
├── app/                        # Main application suite
│   ├── music_brain/            # Core AI transition & DSP engine (analyzer, matcher, stems, renderer)
│   ├── ui/                     # FastAPI backend (server.py) + Web Audio DJ Console (static/)
│   ├── legacy_pipeline/        # Older batch pipeline (run_pipeline, structure_detector, etc.)
│   └── tests/                  # Automated test suite (112+ tests)
├── downloader/                 # High-quality 320kbps YouTube to MP3 audio downloader (downloader.py)
├── DJ/                         # The Interconnected DJ Knowledge Base & Musical Wiki (78+ Notes)
├── IDEAS.md                    # Master architectural blueprints (AI Transition Maker, DDJ-FLX4, Set Logs)
├── PERFORMANCE_AUDIT.md        # Real-time efficiency, zero-lag & 60 FPS performance audit
├── FEATURES.md                 # Reality check: implemented vs stubbed vs planned features
├── data/                       # Runtime artifacts: songs/, output/, cache/ (gitignored)
└── .obsidian/                  # Native Obsidian vault configuration
```

---

## 2. The Knowledge Base & Docs as the DJ Wiki for Agents

### A. The Core Principle: Ground Every Feature in DJ Theory
The `./DJ/` knowledge base and project documentation (`IDEAS.md`, `PERFORMANCE_AUDIT.md`, `FEATURES.md`) serve as the **authoritative musical and architectural wiki** for all AI agents.
* **Never build DJ features in an acoustic vacuum.** 
* When implementing or refactoring features (such as EQ cuts, crossfader curves, phrase snapping, stem isolation, track recommendations, or emergency panic buttons), agents must understand **DJing as a discipline** by referencing the corresponding notes in `./DJ/`.
* The code implements the DSP and UI; the knowledge base defines **why** it must behave that way to sound like a world-class DJ rather than a naive linear crossfade.

### B. Token-Efficient Knowledge Base Navigation
Agents must manage context windows and token usage strictly when querying the knowledge base:
1. **Never dump entire directories or multiple full notes into context.**
2. **Step 1 — Index Lookup:** Use the navigation matrix in Section 3 below to pinpoint the single relevant note (e.g. `Bass Swap.md` or `Track Selection Framework.md`).
3. **Step 2 — Targeted Reading:** Use `view_file` with precise line ranges (e.g. viewing the 17-part recipe's *Musical Principle* and *Step-by-step* sections only), or run `grep_search` for exact technical parameters (such as crossover frequencies or Camelot rules).
4. **Step 3 — Engineering Alignment:**
   * Consult `FEATURES.md` first to confirm what is currently built vs. stubbed.
   * Consult `IDEAS.md` for planned UX interactions and hardware mappings before architecting new features.
   * Consult `PERFORMANCE_AUDIT.md` before touching real-time audio code or animation loops to avoid introducing main-thread jank, GC pauses, or layout thrashing.

---

## 3. The Obsidian DJ Knowledge Base (`./DJ/`)

### How to Navigate & Query the Vault
When a user asks questions about DJing, transition mechanics, song selection, track structure, or performance case studies, agents should consult the corresponding modular notes in `./DJ/`:

| If the User Asks About... | Look in Directory / Primary Note | Key Concepts Inside |
| :--- | :--- | :--- |
| **Where to begin learning** | `DJ/00 - Dashboard/DJ - Start Here.md` | Orientation protocol, first 5 transitions, mindset |
| **Structured curriculum** | `DJ/00 - Dashboard/DJ - Learning Roadmap.md` | 7 stages: concepts, techniques, listening, tests, exit criteria |
| **Complete vault index** | `DJ/00 - Dashboard/DJ - Wiki Overview.md` | Full inventory of all 78 notes, core techniques, next steps |
| **Central home dashboard** | `DJ/00 - Dashboard/DJ Wiki.md` | Navigation matrix, active skill state, favorite tools |
| **Core DJ mindset & ear training** | `DJ/01 - Fundamentals/` | `What Makes a Great DJ.md`, `Rules vs Principles.md`, `Listen Like a DJ.md` |
| **Manual beatmatching & Sync** | `DJ/01 - Fundamentals/Beatmatching & Tempo.md` | Pitch fader, nudging, phase alignment, professional sync utility |
| **Hardware / software concepts** | `DJ/01 - Fundamentals/DJ Equipment & Software Concepts.md` | Decks, channels, gain staging, slip mode, vinyl mode, stems |
| **Phrasing & bar counting** | `DJ/02 - Music Theory/Phrasing & Structure.md` | 4-beat bars, 8-bar (32-beat) phrases, why transitions feel wrong |
| **Harmonic mixing & Camelot** | `DJ/02 - Music Theory/Harmonic Mixing & Camelot System.md` | 12-hour wheel, $\pm 1$ shifts, $+2$ energy boost, tool not a rule |
| **Track sections & non-DJ songs** | `DJ/03 - Track Anatomy/Track Anatomy & Structure.md` | 16 modular blocks, adapting radio edits without intros |
| **EQ & frequency management** | `DJ/04 - Core Techniques/EQ & Frequency Management.md` | "Who owns the spectrum?", bass clashes, headroom, isolator mode |
| **Loops, beat jumps & cues** | `DJ/04 - Core Techniques/` | `Loops & Beat Jumps.md`, `Hot Cues & Performance Pads.md` |
| **Effects & delay usage** | `DJ/04 - Core Techniques/Effects (FX) Mastery.md` | Echo, reverb wash, filters, avoiding the "nervous flanger" |
| **Specific transition recipes** | `DJ/05 - Transition Cookbook/` | 28 standardized recipes (see list below) |
| **Energy curves & tension** | `DJ/06 - Energy & Crowd/Energy Management & Dynamics.md` | Tension, release, silence, 30m/60m/Festival/DnB visual curves |
| **Crowd reading & mistakes** | `DJ/06 - Energy & Crowd/Reading the Room & Crowd Psychology.md` | Non-verbal body feedback, fatigue, handling song requests |
| **What song to play next** | `DJ/07 - Track Selection/` | `Track Selection Framework.md` (7 dimensions), `DJ - What Do I Play Next.md` (Decision tree) |
| **Cross-genre / tempo jumps** | `DJ/08 - Open Format/` | `Open-Format DJing Guide.md` (6 bridges), `Genre Bridge Playbook.md` (Exact recipes) |
| **Genre-specific tactics** | `DJ/09 - Genre Playbooks/` | 10 playbooks: Pop, EDM, House, DnB, Hip-Hop, Trap, Dubstep, etc. |
| **Stems & Ableton Live** | `DJ/10 - Creative DJing/Stems, Live Remixing & Ableton Integration.md` | Real-time stems, ROI matrix, Ableton Link & hybrid setups |
| **Stage presence & emergencies** | `DJ/11 - Live Performance/Live Performance & Crisis Management.md` | 1-second trainwreck recovery, handling dead silence |
| **Building full sets** | `DJ/12 - Set Construction/Set Construction & Architecture.md` | 8-stage narrative arc; 15m, 30m, 60m, 90m blueprints |
| **Artist Case Studies** | `DJ/13 - DJ Case Studies/` | `Fred again.. Case Study.md`, `Skrillex Case Study.md`, `Martin Garrix Case Study.md`, `DJ Comparison Matrix.md` |
| **Practice drills & missions** | `DJ/14 - Practice Lab/` | `Practice Lab & Progressive Curriculum.md` (Levels 1–6), `First 10 Practice Missions.md` (Missions 01–10) |
| **Track & Set logging** | `DJ/15 - Track Database/`, `DJ/16 - Transition Database/`, `DJ/17 - Set Logs/` | Templates & reference archives for tracks, transitions, sets |
| **Terminology & Definitions** | `DJ/18 - Glossary/DJ Glossary.md` | Alphabetical compendium with backlinks |
| **Research citations** | `DJ/99 - Sources/Source Index.md` | Tier 1 (manuals, artist breakdowns), Tier 2, Tier 3 citations |

---

## 3. The 28 Transition Recipes in `05 - Transition Cookbook/`

Every file in `05 - Transition Cookbook/` strictly adheres to a **17-part standardized template**:
1. *Technique Name*
2. *What it is*
3. *What problem it solves*
4. *Musical principle*
5. *Setup*
6. *Step-by-step*
7. *When to use it*
8. *When NOT to use it*
9. *Best genres*
10. *Beginner difficulty (1–5)*
11. *Risk of sounding gimmicky (0–5)*
12. *Common mistakes*
13. *Advanced variation*
14. *Example scenario*
15. *Practice drill*
16. *Related techniques*
17. *Backlinks to other notes*

### Index of Available Transition Files:
* **Blends & EQ:** `Basic Blend.md`, `Long Blend.md`, `EQ Blend.md`, `Bass Swap.md`, `Filter Transition.md`.
* **Cuts & Drops:** `Quick Cut.md`, `Hard Cut.md`, `Drop Swap.md`, `Build-to-Drop Transition.md`, `Fake Drop.md`, `Double Drop.md`.
* **Spatial & FX:** `Echo Out.md`, `Reverb Transition.md`, `Breakdown Transition.md`, `Loop Transition.md`, `Loop Roll.md`, `Beat Jump Transition.md`, `Stutter Transition.md`, `Backspin (Spinback).md`.
* **Creative & Open-Format:** `Vocal Transition.md`, `Acapella Overlay.md`, `Instrumental Overlay.md`, `Drum Bridge.md`, `Genre Bridge.md`, `Tempo Bridge.md`, `Stems Transition.md`, `3-Deck Layering.md`, `Live Mashup.md`.

---

## 4. Bridging Knowledge Base Theory to Python Code

Agents working on the codebase (`mixing_engine.py`, `structure_detector.py`, `generate_mixing_plan.py`) should use the knowledge base as the ground-truth specification:

### A. Transition Timing & Phrasing
* **Theory Note:** `DJ/02 - Music Theory/Phrasing & Structure.md`
* **Code Implementation:** When `structure_detector.py` identifies candidate transition points, snap them to **8-bar (32-beat) phrase boundaries**. Ensure that incoming build-ups drop on Beat 1 of a new phrase.

### B. EQ Filtering & Low-End Management
* **Theory Note:** `DJ/04 - Core Techniques/EQ & Frequency Management.md` & `DJ/05 - Transition Cookbook/Bass Swap.md`
* **Code Implementation:** In `mixing_engine.py`, ensure that during overlapping transitions, the sub-bass ($<120\text{ Hz}$) is strictly allocated to only **one track** at a time using high-pass filtering (`scipy.signal` / butterworth filter) to prevent comb filtering and clipping.

### C. Harmonic Compatibility & Camelot Key Scoring
* **Theory Note:** `DJ/02 - Music Theory/Harmonic Mixing & Camelot System.md`
* **Code Implementation:** In `generate_mixing_plan.py`, score key compatibility using the Camelot 12-hour wheel:
  * Same key ($0$ distance): Score 1.0
  * Adjacent key ($\pm 1$ hour): Score 0.9
  * Relative Major/Minor ($\text{A} \leftrightarrow \text{B}$): Score 0.85
  * Energy Boost ($+2$ hours / whole tone): Score 0.8 (valid for high-energy drops)
  * Dissonant clash ($\ge 3$ hours): Disallow unless using an Echo Out, Breakdown, or Drum Bridge.
  * Live console gate: a tonal blend (Long Blend, Bass Swap, Drop Swap, learned stem intro) needs Camelot $\ge 0.8$, else it becomes Echo Out (`autopilot.js:keySafeRecipe`, `techniques.py:learned_pick`). Key-agnostic recipes on a clashing pair score `BYPASS_KEY_SCORE = 0.4` in the matcher.

### D. Tempo Ramping & Open-Format Transitions
* **Theory Note:** `DJ/08 - Open Format/Open-Format DJing Guide.md` & `DJ/05 - Transition Cookbook/Tempo Bridge.md`
* **Code Implementation:** When BPM difference is $\le 6\%$, use 32 micro-step gradual tempo warping. When BPM difference is massive (e.g., 128 to 174 BPM), implement the **Echo Out** or **Breakdown Transition** logic instead of linear pitch stretching. Live key-locked stem stretch is capped at 8% (`tempo-rule.js:KEYLOCK_RANGE_PCT`, `techniques.py:MAX_KEYLOCK_STRETCH`).

### E. Live Console Silence and Energy Gates, and the Virtual Set Sim
* **Code Implementation:** the energy last-round `force` widens rises only (`energy.py:next_ok`). A stem intro, voice-alone strip or synth hold on a stem with no energy in the window is refused (`stem-moves.js:pickIntro`, `breakdownVocalOk`, `keepsVibe`).
* **Verify rule changes with the sim** (`app/sim/README.md`): `python3 -m app.sim.suite --check`, `python3 -m app.sim.virtual_set --replay NAME --out DIR`, `python3 -m app.sim.compare A/report.json B/report.json`. It cannot judge sound quality; `baseline.json` is stub-LLM era. Log results in `app/sim/LEARNINGS.md`.

---

## 4b. The Music Brain Package (`music_brain/`) — Agent Entry Point

`music_brain/` is the newer, knowledge-base-grounded engine (BPM/key/beatgrid analysis, Demucs stem
separation, 28-recipe transition matching, scipy DSP rack, preview rendering). Agents interact with it
through **`music_brain/agent_bridge.py`**, which is both an importable Python API and a CLI. Every CLI
command prints structured JSON to stdout — parse stdout, do not scrape logs.

### A. CLI Commands
```bash
# Full analysis: BPM, beatgrid, downbeats, Camelot key, phrase boundaries, sections, energy
python -m music_brain.agent_bridge analyze songs/input.mp3

# Demucs separation (--stems 4 = vocals/drums/bass/other; --stems 2 = vocals/no_vocals, ~2x faster)
python -m music_brain.agent_bridge separate songs/input.mp3 --stems 4

# Top-N scored & explained transition blueprints for a track pair (A = outgoing, B = incoming)
python -m music_brain.agent_bridge match songs/input.mp3 songs/input2.mp3 --top-n 3

# Render a transition preview snippet to disk
python -m music_brain.agent_bridge preview songs/input.mp3 songs/input2.mp3 \
    --recipe "Bass Swap" --a-time 60 --b-time 10 --seconds 20 --out preview.mp3

# All 28 parsed recipes with tags and acoustic prerequisites
python -m music_brain.agent_bridge list-recipes
```

`--a-time` / `--b-time` are **genuine manual overrides**, not hints: the given point wins (snapped to
the nearest real 8-bar phrase boundary), and any omitted side falls back to the AI's own best guess.
Omit both and `--recipe` for the AI's own top pick. Never bypass `resolve_candidate()` by re-running a
match and hoping the manual point survives — that was a real bug (Phase 4.1).

### B. JSON Response Shapes
| Command | Top-level keys |
| :--- | :--- |
| `analyze` | `path`, `duration`, `bpm`, `beat_times[]`, `downbeat_times[]`, `phrase_boundaries_8bar[]`, `phrase_boundaries_16bar[]`, `key` (Camelot), `energy_curve[]`, `energy_times[]`, `sections[]`, `vocal_active_regions[]` |
| `separate` | `audio_hash`, `model`, `two_stems`, `stems{name: abs_path}`, `cache_dir`, `from_cache` |
| `match` | `track_a`, `track_b`, `candidates[]` — each with `recipe`, `score` (0–100), `a_time`, `b_time`, `explanation`, plus sub-scores `camelot_score`, `bpm_score`, `phrase_score`, `vocal_penalty` |
| `preview` | `recipe`, `explanation`, `score`, `output_path`, `duration_seconds`, `render_time_seconds`, `peak_dbfs`, `sample_rate` |
| `list-recipes` | `recipes[]` — each with `name`, `slug`, `difficulty`, `tags`, `genres`, the 17-part template fields (`what_it_is`, `musical_principle`, `setup`, `steps`, `when_to_use`, `when_not_to_use`, ...), and the auto-tagged prerequisites `requires_stems`, `max_bpm_delta`, `camelot_compatible_only` |

Errors print `{"error": "..."}` and exit non-zero. Python API equivalents (`analyze()`, `separate()`,
`match()`, `preview()`, `list_recipes()`) return the same dicts — `from music_brain import agent_bridge`.

### C. Which Pipeline Should an Agent Use?
| Use `music_brain.agent_bridge` when... | Use `run_pipeline.py` / `structure_detector.py` when... |
| :--- | :--- |
| You need per-track analysis JSON (key, phrases, sections, energy) | You want the original end-to-end batch mix over a whole `songs/` setlist |
| You need stems, or a stem-aware transition | You are debugging the legacy librosa detector or the GPT-4o plan generator |
| You need a scored, explained, KB-grounded transition recommendation | You need `output/mix.mp3` produced by the original 32-micro-step warping engine |
| You need a fast 15–30s audition of one specific transition | |
| You are backing the web workbench / DJ console (`ui/server.py`) | |

Rule of thumb: **new work goes through `music_brain`.** The `run_pipeline.py` chain is the legacy
end-to-end mixer and is kept working, but it is not knowledge-base-grounded the way `music_brain` is.

### D. REST Surface (`ui/server.py`)
The same capabilities are exposed over HTTP (`uvicorn ui.server:app --reload`). All ids are
content hashes (`sha256(bytes)[:16]`), so re-uploading the same bytes is idempotent, and all blobs
live under `cache/` (gitignored):
`POST/GET /api/tracks`, `GET /api/tracks/{id}/analysis`, `POST /api/tracks/{id}/separate`,
`GET /api/recipes`, `POST /api/match`, `POST /api/preview`, `GET /api/audio/tracks/{id}`,
`GET /api/audio/previews/{filename}`, `POST/GET /api/samples` + `GET /api/samples/{id}`
(user-uploaded sampler one-shots → `cache/samples/`), and `POST /api/recordings` +
`GET /api/recordings/{id}` (MediaRecorder captures of live console mixes → `cache/recordings/`).

---

## 5. Audio Acquisition: YouTube to MP3 Downloader (`YouTube_Audio_Downloader/`)

The repository includes a dedicated high-fidelity audio extraction tool located at `YouTube_Audio_Downloader/`. Agents should use this tool when the user provides YouTube links or requests new tracks to be downloaded for the mixing pipeline.

### A. Role in the Pipeline
The DJ mixing engine requires high-quality `.mp3` audio files in `./songs/` to extract librosa features, detect downbeats, and perform harmonic alignment. The downloader ensures:
* **Max Audio Bitrate:** Automatically selects `bestaudio/best` and encodes to **320 kbps CBR MP3** via `ffmpeg`.
* **Metadata & Artwork:** Embeds ID3v2 tags (title, artist, release date) and high-res cover art.
* **Direct Pipeline Feeding:** Saves audio straight to `./songs/` so `run_pipeline.py` or `structure_detector.py` can immediately process them.

### B. Execution Commands for Agents
```bash
# Download one or more YouTube videos directly into the pipeline library
python YouTube_Audio_Downloader/downloader.py -o songs "<youtube_url_1>" "<youtube_url_2>"

# Download from a batch text file of URLs
python YouTube_Audio_Downloader/downloader.py -o songs --file links.txt

# Download a single video without downloading the rest of its playlist
python YouTube_Audio_Downloader/downloader.py -o songs --no-playlist "<playlist_item_url>"
```

### C. Agent Usage Protocol
1. When a user provides YouTube links, run `python YouTube_Audio_Downloader/downloader.py -o songs "<url>"`.
2. Verify the downloaded `.mp3` file appears in `./songs/`.
3. Proceed to execute the mixing pipeline stages (`python run_pipeline.py` or individual stage modules).

---

## 6. Rules for Agents Modifying the Knowledge Base

When adding or updating notes inside `DJ/`, agents must follow these rules:

1. **The 5 Pedagogical Questions:** Every conceptual guide must explicitly address:
   - *What is it?*
   - *Why does it matter?*
   - *What does it sound/feel like?*
   - *How do I practice it?*
   - *When should I deliberately break the rule?*
2. **Standardized Transition Template:** Any new transition added to `05 - Transition Cookbook/` must use the 17-part template described in Section 3.
3. **Obsidian Wiki-Links:** Always use standard Obsidian double-bracket links (e.g., `[[Phrasing & Structure]]`, `[[Bass Swap]]`, `[[Fred again.. Case Study]]`). Do not use raw relative filesystem paths inside the notes.
4. **Restrained Tagging:** Use the established tag hierarchy:
   - `#dj/fundamental`, `#dj/technique`, `#dj/transition`, `#dj/music-theory`
   - `#dj/energy`, `#dj/open-format`, `#dj/live`, `#dj/case-study`, `#dj/practice`
   - `#dj/genre/pop`, `#dj/genre/house`, `#dj/genre/dnb`, `#dj/genre/edm`, `#dj/genre/hiphop`
5. **No Speculation on Artists:** Clearly distinguish verified facts (documented gear/DAW workflows) from aesthetic interpretations. Cite sources in `DJ/99 - Sources/Source Index.md`.
6. **Keep All Notes in `./DJ/`:** Never create notes outside `./DJ/` unless specifically directed by the user.

---

## 7. Verification & Health Checks

Agents can verify the health of the repository and vault by checking:
* **Audio Library:** Tracks present in `./songs/`.
* **Audio Downloader:** `YouTube_Audio_Downloader/downloader.py` and `run.bat` present.
* **Vault File count:** All 78+ notes present in `./DJ/`.
* **Configuration:** `.obsidian/app.json`, `.obsidian/core-plugins.json`, and `.obsidian/appearance.json` exist.
* **Vault Registration:** `C:\Users\kaiwa\Documents\DJAITest` registered in `%APPDATA%\obsidian\obsidian.json`.
* **Git Status:** `.obsidian/` and `DJ/` tracked locally.
