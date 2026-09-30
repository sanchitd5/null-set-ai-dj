# CLAUDE.md — Claude Code Project Instructions

> **Repository:** `null-set-ai-dj`  
> **Dual Architecture:** Python Audio Processing Pipeline + Native Obsidian DJ Knowledge Base (`./DJ/`)  
> **Cross-Agent Compatibility:** Claude Code (`CLAUDE.md`), Antigravity (`AGENTS.md`), and OpenAI Codex (`CODEX.md`).

---

## 1. Project Overview & Quick Reference

This repository unites two complementary domains:
1. **Python AI Audio Mixing Pipeline:** Automates song selection (GPT-4o/Gemini), librosa acoustic feature extraction, phrase-boundary detection, Camelot harmonic scoring, 32-micro-step tempo warping, and scipy/pydub audio stitching (`data/output/mix.mp3`).
2. **Native Obsidian DJ Knowledge Base (`./DJ/`):** 78+ interconnected markdown notes covering music theory, acoustic physics, 28 standardized transition blueprints, 10 genre playbooks, energy management, crowd psychology, and artist case studies (Fred again.., Skrillex, Martin Garrix).

---

## 2. Environment & CLI Commands

### Python Virtual Environment
```bash
# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# Install / update dependencies
pip install -r requirements.txt
```

### Repository layout

```
app/                 The main player: music_brain/ engine, ui/ (FastAPI + console frontend),
                      tests/, and legacy_pipeline/ (the older end-to-end batch scripts below)
downloader/           YouTube -> MP3 downloader (downloader.py)
DJ/                   Obsidian knowledge base (unchanged, see Section 3)
research/             Experimental code: research_stuff/, notes/
data/                 Runtime artifacts, not source: songs/, output/, cache/ (all gitignored)
docs/                 product/, engineering/, archive/ docs; images/ screenshots
```

### Execution Commands
```bash
# Download audio from YouTube directly into data/songs/ for mixing (320kbps MP3)
python downloader/downloader.py -o data/songs "<youtube_url>"

# Run the full automated end-to-end DJ mixing pipeline (legacy)
python app/legacy_pipeline/run_pipeline.py

# Test individual legacy pipeline stages
python app/legacy_pipeline/structure_detector.py     # Librosa phrase & section boundary detection
python app/legacy_pipeline/bpm_lookup.py              # BPM enrichment & Spotify/local cache lookup
python app/legacy_pipeline/generate_mixing_plan.py    # AI transition planning & Camelot scoring
python app/legacy_pipeline/mixing_engine.py           # DSP crossfade, EQ filter & audio rendering
python app/legacy_pipeline/waveform_visualizer.py     # Generate audio visual waveforms
```

### The Music Brain Engine (`app/music_brain/`) — preferred entry point for new work

`app/music_brain/` is the knowledge-base-grounded engine (analysis, Demucs stems, 28-recipe transition
matching, scipy DSP rack, preview rendering). Drive it through `app/music_brain/agent_bridge.py` — an
importable Python API *and* a CLI whose every command prints structured JSON to stdout. Run all
commands from the repo root, as the `app.` package:

```bash
python -m app.music_brain.agent_bridge analyze data/songs/input.mp3            # BPM, beatgrid, Camelot key, phrases, sections, energy
python -m app.music_brain.agent_bridge separate data/songs/input.mp3 --stems 4 # 4-stem; --stems 2 = vocals/no_vocals, ~2x faster
python -m app.music_brain.agent_bridge match data/songs/input.mp3 data/songs/input2.mp3 --top-n 3
python -m app.music_brain.agent_bridge preview data/songs/input.mp3 data/songs/input2.mp3 \
    --recipe "Bass Swap" --a-time 60 --b-time 10 --seconds 20 --out preview.mp3
python -m app.music_brain.agent_bridge list-recipes
```

**JSON shapes returned:**
* `analyze` → `path`, `duration`, `bpm`, `beat_times[]`, `downbeat_times[]`, `phrase_boundaries_8bar[]`,
  `phrase_boundaries_16bar[]`, `key` (Camelot), `energy_curve[]`, `energy_times[]`, `sections[]`,
  `vocal_active_regions[]`, and (v6, `analysis/structure.py`) `drops[]` (each: `start`, `end`, `energy`,
  `jump`, `contrast`, `confidence`, `source` stems|mix), `main_drop`, `structure`. Sections sit on the 8-bar
  phrase grid. Readers go through `blend.track_drop_lines` / `dj-mind.js trackDropLines` (v5 records fall back
  to the energy-curve rule). After a merge that bumps it: `python3 -m app.music_brain.analysis.reanalyse --all`.
* `separate` → `audio_hash`, `model`, `two_stems`, `stems{name: abs_path}`, `cache_dir`, `from_cache`.
* `match` → `track_a`, `track_b`, `candidates[]` (each: `recipe`, `score` 0–100, `a_time`, `b_time`,
  `explanation`, and the `camelot_score` / `bpm_score` / `phrase_score` / `vocal_penalty` sub-scores).
* `preview` → `recipe`, `explanation`, `score`, `output_path`, `duration_seconds`,
  `render_time_seconds`, `peak_dbfs`, `sample_rate`.
* `list-recipes` → `recipes[]` (17-part template fields + `requires_stems`, `max_bpm_delta`,
  `camelot_compatible_only`).
* Failures print `{"error": "..."}` and exit non-zero.

**`--a-time` / `--b-time` are genuine manual overrides**, not hints: your point wins (snapped to the
nearest real 8-bar phrase boundary) and any omitted side falls back to the AI's guess. Always route
overrides through `RecipeMatcher.resolve_candidate()`; re-running `match()` and hoping the manual point
survives is the exact bug fixed in Phase 4.1.

**Which pipeline to reach for:**
* Use `app.music_brain.agent_bridge` for anything needing per-track analysis JSON, stems, a scored and
  explained KB-grounded transition recommendation, a fast 15–30s audition, or backing `app/ui/server.py`.
  **New work goes here.**
* Use `app/legacy_pipeline/run_pipeline.py` / `structure_detector.py` / `generate_mixing_plan.py` only for the legacy
  end-to-end batch mix over a whole `data/songs/` setlist (producing `data/output/mix.mp3`), or when debugging
  that older librosa/GPT-4o chain itself.

**REST surface** (`uvicorn app.ui.server:app --reload`) — all ids are content hashes (`sha256(bytes)[:16]`,
so re-uploading identical bytes is idempotent) and all blobs live under the gitignored `data/cache/`:
`POST/GET /api/tracks`, `GET /api/tracks/{id}/analysis`, `POST /api/tracks/{id}/separate`,
`GET /api/recipes`, `POST /api/match`, `POST /api/preview`, `GET /api/audio/tracks/{id}`,
`GET /api/audio/previews/{filename}`, `POST/GET /api/samples` + `GET /api/samples/{id}`
(user-uploaded sampler one-shots → `data/cache/samples/`), `POST /api/recordings` +
`GET /api/recordings/{id}` (MediaRecorder captures of live console mixes → `data/cache/recordings/`).

```bash
# Test suite (112+ tests); -m "not slow" skips the Demucs separation tests
.venv\Scripts\python.exe -m pytest app/tests/ -q -m "not slow"
```

**Virtual set sim** (`app/sim/`, docs in `app/sim/README.md`): runs the console's real browser JS in node
against the real API on a virtual clock and scores a whole set with one number (lower is better). Use it
to test any autopilot / transition rule change before trusting it. Learning log: `app/sim/LEARNINGS.md`.

```bash
python3 -m app.sim.virtual_set --seed 1 --tracks 10 --mode quick --out app/sim/out/run
python3 -m app.sim.virtual_set --seed 1 --tracks 10 --mode quick --record NAME   # once, real network/LLM
python3 -m app.sim.virtual_set --replay NAME --out DIR                            # deterministic, no network
python3 -m app.sim.suite --check            # seeds 1-5, long+quick; exit 1 on regression vs baseline.json
python3 -m app.sim.compare A/report.json B/report.json
```

It cannot judge sound quality or real vocal clash; `baseline.json` is stub-LLM era until re-recorded.

---

## 3. The Obsidian DJ Knowledge Base (`./DJ/`) as the DJ Wiki

The `./DJ/` directory contains ground-truth musical theory and transition specifications that inform all audio engineering in this repository. Together with `docs/product/IDEAS.md`, `docs/engineering/PERFORMANCE_AUDIT.md`, and `docs/product/FEATURES.md`, it forms the **central DJ Wiki**.

### Conceptual Grounding & Token Efficiency Rules for Agents
1. **Understand DJing as a Discipline:** The knowledge base exists so AI assistants understand *why* DJ moves work musically. When building features (crossfader curves, EQ hand-offs, phrase detection, stem isolation, track recommendations), always ground implementation details in the relevant `./DJ/` note.
2. **Surgical Token Usage:** **Do not dump entire directories or huge markdown files into context.**
   * Use the index below to identify the exact 1–2 notes needed.
   * View only the necessary line ranges or grep for specific rules/crossover frequencies.
   * Consult `docs/product/FEATURES.md` for what's currently working vs. stubbed.
   * Consult `docs/product/IDEAS.md` for planned UX interactions and hardware mappings.
   * Consult `docs/engineering/PERFORMANCE_AUDIT.md` before touching real-time audio or animation loops to avoid main-thread jank and dropouts.

### Quick Directory Navigation:
* `DJ/00 - Dashboard/` $\to$ [[DJ Wiki.md]], [[DJ - Start Here.md]], [[DJ - Learning Roadmap.md]], [[DJ - Wiki Overview.md]].
* `DJ/01 - Fundamentals/` $\to$ [[What Makes a Great DJ.md]], [[Rules vs Principles.md]], [[Beatmatching & Tempo.md]].
* `DJ/02 - Music Theory/` $\to$ [[Phrasing & Structure.md]] (32-beat phrases), [[Harmonic Mixing & Camelot System.md]].
* `DJ/03 - Track Anatomy/` $\to$ [[Track Anatomy & Structure.md]] (16 modular blocks, adapting pop radio edits).
* `DJ/04 - Core Techniques/` $\to$ [[EQ & Frequency Management.md]] (sub-bass ownership), [[Loops & Beat Jumps.md]].
* `DJ/05 - Transition Cookbook/` $\to$ 28 standardized 17-part recipes ([[Drop Swap.md]], [[Bass Swap.md]], [[Echo Out.md]], [[Double Drop.md]], [[Stems Transition.md]], [[Live Mashup.md]], etc.).
* `DJ/06 - Energy & Crowd/` $\to$ [[Energy Management & Dynamics.md]] (visual set curves), [[Reading the Room & Crowd Psychology.md]].
* `DJ/07 - Track Selection/` $\to$ [[Track Selection Framework.md]] (7 dimensions), [[DJ - What Do I Play Next.md]].
* `DJ/08 - Open Format/` $\to$ [[Open-Format DJing Guide.md]] (6 bridges), [[Genre Bridge Playbook.md]] (Pop $\leftrightarrow$ DnB, 128 $\leftrightarrow$ 174 BPM).
* `DJ/09 - Genre Playbooks/` $\to$ 10 playbooks (Pop, House, DnB, EDM, Hip-Hop, Trap, Dubstep, Future Bass, etc.).
* `DJ/10 - Creative DJing/` $\to$ [[Stems, Live Remixing & Ableton Integration.md]].
* `DJ/11 - Live Performance/` $\to$ [[Live Performance & Crisis Management.md]].
* `DJ/12 - Set Construction/` $\to$ [[Set Construction & Architecture.md]] (15m, 30m, 60m, 90m sets).
* `DJ/13 - DJ Case Studies/` $\to$ [[Fred again.. Case Study.md]], [[Skrillex Case Study.md]], [[Martin Garrix Case Study.md]], [[DJ Comparison Matrix.md]].
* `DJ/14 - Practice Lab/` $\to$ [[Practice Lab & Progressive Curriculum.md]], [[First 10 Practice Missions.md]].
* `DJ/15-17 Databases/` $\to$ Templates and populated logs for Tracks, Transitions, and Sets.
* `DJ/18 - Glossary/` $\to$ [[DJ Glossary.md]] (alphabetical with backlinks).
* `DJ/99 - Sources/` $\to$ [[Source Index.md]] (Tier 1–3 citations).

---

## 4. How Knowledge Base Theory Governs Code Implementation

When writing or modifying code in `mixing_engine.py`, `structure_detector.py`, or `generate_mixing_plan.py`:
1. **Phrasing & Snapping:** All transition entry/exit timestamps must snap to **8-bar (32-beat) phrase boundaries** (per [[Phrasing & Structure.md]]).
2. **Frequency Ownership & Low-End Cut:** Never allow two tracks to play sub-bass ($<120\text{ Hz}$) simultaneously. Implement high-pass filtering (butterworth / `scipy.signal`) to ensure only ONE track owns the low end during transitions (per [[EQ & Frequency Management.md]] and [[Bass Swap.md]]).
3. **Harmonic Key Scoring:** Use the 12-hour Camelot wheel distance function in `generate_mixing_plan.py`:
   * Distance $0$ (Same key): 1.0
   * Distance $\pm 1$ hour: 0.9
   * Relative Major/Minor ($\text{A} \leftrightarrow \text{B}$): 0.85
   * $+2$ Energy Boost: 0.8
   * Clashing keys ($\ge 3$ hours): Disallowed unless using Echo Out or Breakdown transition logic.
   * Live console gate: `dj-mind.js:camelotScore` is the matcher's table (diagonal 0.75, $-2$ hours 0.6, 2 hours with the letter flipped 0.3, 3+ hours 0). A tonal blend (Long Blend, Bass Swap, Drop Swap, learned stem intro) needs Camelot $\ge 0.6$ (`KEY_SAFE_MIN`), else it becomes Echo Out (`autopilot.js:keySafeRecipe`, `techniques.py:learned_pick`); stem merge / handoff / peak keep their own 0.8 floors. Key-agnostic recipes on a clashing pair score `BYPASS_KEY_SCORE = 0.4` in the matcher, never neutral.
   * Echo Out is the fallback only for a key clash or a tempo gap. A move refused for any other reason (stems missing, no blend plan, merge / mashup / vocal gate) on a tempo-locked pair falls back to the key-safe Bass Swap (`autopilot.js:safeBlend`, used by `forcedRecipe` and `decideRecipe`). A planned or stored Echo Out (matcher, model, macro step, atlas plan) is never rewritten.
4. **BPM Gaps:** For BPM differences $\le 6\%$, use 32-micro-step pitch ramping. For massive gaps (e.g., 128 to 174 BPM), route through an **Echo Out** or **Breakdown Transition** rather than stretching audio. Live key-locked stem stretch is capped at 8% (`tempo-rule.js:KEYLOCK_RANGE_PCT`, `techniques.py:MAX_KEYLOCK_STRETCH`).
5. **Live console selection and silence gates:** the energy last-round `force` widens rises only, never falls (`energy.py:next_ok`, `autopilot.js:energyStepOk`). A stem intro, voice-alone strip or synth hold on a stem with no energy in the window is refused (`stem-moves.js:pickIntro`, `breakdownVocalOk`, `keepsVibe`); a refused move falls back, it is not booked. Evidence and open questions: `app/sim/LEARNINGS.md`.

---

## 5. Rules for Modifying Notes in `./DJ/`

1. **The 5 Pedagogical Questions:** Any conceptual guide must answer:
   * *What is it?*
   * *Why does it matter?*
   * *What does it sound/feel like?*
   * *How do I practice it?*
   * *When should I deliberately break the rule?*
2. **17-Part Transition Template:** All recipes in `05 - Transition Cookbook/` must follow the 17-part structure.
3. **Obsidian Wiki-Links:** Always link using double brackets (e.g., `[[Phrasing & Structure]]`, `[[Bass Swap]]`). Never use relative filesystem file paths in markdown bodies.
4. **Tagging:** Use the established hierarchy: `#dj/fundamental`, `#dj/technique`, `#dj/transition`, `#dj/music-theory`, `#dj/energy`, `#dj/open-format`, `#dj/genre/...`.
5. **Location Constraint:** All knowledge base markdown notes must remain inside `./DJ/`.

---

## 6. Full Cross-Agent Documentation

For extended details, review the master multi-agent specification in [`AGENTS.md`](AGENTS.md).
