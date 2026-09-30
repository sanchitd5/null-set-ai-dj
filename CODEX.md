# CODEX.md — OpenAI Codex & Copilot Instructions

> **Repository:** `null-set-ai-dj`  
> **Environment:** Windows PowerShell / Python 3.10+ / Web Audio API / Librosa / PyTorch (Demucs) / FastAPI  
> **Knowledge Base:** Embedded Obsidian DJ Knowledge Base (`./DJ/`)  
> **Cross-Agent Standards:** Multi-agent operating model alongside `AGENTS.md` and `CLAUDE.md`.  
> **Last Updated:** September 2026  

---

## 1. Multi-Agent Ecosystem Interoperability

This repository is pre-configured to ensure identical execution and ground-truth alignment across all major AI coding assistants:

| Assistant / Tool | Configuration Entry Point | Discovery & Role |
| :--- | :--- | :--- |
| **OpenAI Codex / Copilot** | [`CODEX.md`](CODEX.md) & [`.github/copilot-instructions.md`](.github/copilot-instructions.md) | Loaded by Codex, GitHub Copilot, ChatGPT Code Interpreter, and Cursor. |
| **Google Antigravity** (`agy`) | [`AGENTS.md`](AGENTS.md) | Loaded natively as workspace rule (`<RULE[...AGENTS.md]>`). |
| **Claude Code** (`claude`) | [`CLAUDE.md`](CLAUDE.md) | Loaded automatically at startup by Claude Code CLI. |

All assistants share identical ground-truth specifications in `./DJ/`, `docs/product/IDEAS.md`, `docs/engineering/PERFORMANCE_AUDIT.md`, and `docs/product/FEATURES.md`.

---

## 2. Repository Layout & Architecture Overview

```
null-set-ai-dj/
├── app/                        # Main application suite
│   ├── music_brain/            # Core AI transition & DSP engine (analyzer, matcher, stems, renderer)
│   ├── ui/                     # FastAPI backend (server.py) + Web Audio DJ Console (static/)
│   ├── legacy_pipeline/        # Older batch pipeline (run_pipeline, structure_detector, etc.)
│   └── tests/                  # Automated pytest test suite (112+ tests)
├── downloader/                 # High-quality 320kbps YouTube to MP3 audio downloader (downloader.py)
├── DJ/                         # The Interconnected DJ Knowledge Base & Musical Wiki (78+ Notes)
├── docs/
│   ├── product/                # IDEAS.md (blueprints), FEATURES.md (built vs stubbed), PRODUCT.md
│   ├── engineering/            # PERFORMANCE_AUDIT.md (60 FPS / zero-lag audit), ANNEX.md (glossary)
│   ├── archive/                # Stale handoffs and one-off prompts/audits
│   └── images/                 # Screenshots
├── data/                       # Runtime artifacts: songs/, output/, cache/ (gitignored)
└── .obsidian/                  # Native Obsidian vault configuration
```

---

## 3. The Knowledge Base & Docs as the DJ Wiki for Codex

### A. Ground Every Feature in DJ Theory
The `./DJ/` knowledge base and project documentation (`docs/product/IDEAS.md`, `docs/engineering/PERFORMANCE_AUDIT.md`, `docs/product/FEATURES.md`) serve as the **authoritative musical and architectural wiki** for all AI models.
* **Never build DJ features in an acoustic vacuum:** When implementing or refactoring features (such as EQ cuts, crossfader curves, phrase snapping, stem isolation, track recommendations, or emergency panic buttons), Codex must understand **DJing as a discipline** by referencing the corresponding notes in `./DJ/`.
* The code implements the DSP and UI; the knowledge base defines **why** it must behave that way to sound like a world-class DJ rather than a naive linear crossfade.

### B. Token-Efficient Knowledge Base Navigation
Codex and Copilot agents must manage context windows strictly:
1. **Never dump entire directories or multiple full notes into context.**
2. **Step 1 — Index Lookup:** Use the navigation matrix in Section 4 below to pinpoint the single relevant note (e.g. `Bass Swap.md` or `Track Selection Framework.md`).
3. **Step 2 — Targeted Reading:** View precise line ranges or search for exact technical parameters (such as crossover frequencies or Camelot rules).
4. **Step 3 — Engineering Alignment:**
   * Consult `docs/product/FEATURES.md` first to confirm what is currently built vs. stubbed.
   * Consult `docs/product/IDEAS.md` for planned UX interactions and hardware mappings before architecting new features.
   * Consult `docs/engineering/PERFORMANCE_AUDIT.md` before touching real-time audio code or animation loops to avoid introducing main-thread jank, GC pauses, or layout thrashing.

---

## 4. The Obsidian DJ Knowledge Base Directory (`./DJ/`)

When generating code, analyzing transitions, or recommending track pairings, consult the corresponding modular notes in `./DJ/`:

* **Phrasing & Alignment:** `DJ/02 - Music Theory/Phrasing & Structure.md`. Transition points MUST align to 8-bar (32-beat) phrase boundaries.
* **Frequency Real Estate & EQ:** `DJ/04 - Core Techniques/EQ & Frequency Management.md` and `DJ/05 - Transition Cookbook/Bass Swap.md`. Sub-bass ($<120\text{ Hz}$) must never overlap on two active channels. Apply high-pass Butterworth filtering (`scipy.signal`) to prevent comb filtering.
* **Camelot Wheel Harmonic Scoring:** `DJ/02 - Music Theory/Harmonic Mixing & Camelot System.md`. Score compatibility using the 12-hour wheel distance matrix ($0 \to 1.0$, $\pm 1 \to 0.9$, $+2 \to 0.8$, $\ge 3 \to \text{disallow}$).
* **Transition Recipes:** `DJ/05 - Transition Cookbook/` for 28 standardized recipes (Drop Swap, Bass Swap, Echo Out, Double Drop, Stems Transition, etc.).
* **Energy Management:** `DJ/06 - Energy & Crowd/Energy Management & Dynamics.md` (visual curves: Ramp, Wave, Mountain, Plateau).
* **Track Selection (7 Dimensions):** `DJ/07 - Track Selection/Track Selection Framework.md`.
* **Cross-Genre & Tempo Ramping:** `DJ/08 - Open Format/Open-Format DJing Guide.md` and `DJ/08 - Open Format/Genre Bridge Playbook.md` for 128 $\leftrightarrow$ 174 BPM math and halftime/doubletime logic.
* **Live Performance & Crisis Management:** `DJ/11 - Live Performance/Live Performance & Crisis Management.md` (1-second trainwreck recovery).
* **Set Construction:** `DJ/12 - Set Construction/Set Construction & Architecture.md` (8-stage narrative arc).

---

## 5. Primary CLI Commands & APIs

```bash
# Analyze track (BPM, Camelot key, beatgrid, phrases, energy curve, vocal regions)
python -m app.music_brain.agent_bridge analyze data/songs/input.mp3

# Demucs stem separation (4 stems: drums, bass, vocals, other)
python -m app.music_brain.agent_bridge separate data/songs/input.mp3 --stems 4

# Match track pair against 28 knowledge-base recipes
python -m app.music_brain.agent_bridge match data/songs/input.mp3 data/songs/input2.mp3 --top-n 3

# Render transition preview snippet
python -m app.music_brain.agent_bridge preview data/songs/input.mp3 data/songs/input2.mp3 --recipe "Bass Swap" --seconds 20 --out preview.mp3

# Start FastAPI server & Web Audio console (serves both frontend and backend on port 8000)
.\.venv\Scripts\python.exe -m uvicorn app.ui.server:app --reload --host 127.0.0.1 --port 8000

# Download YouTube audio (320kbps CBR MP3)
python downloader/downloader.py -o data/songs "<youtube_url>"

# Virtual set sim: score a whole autopilot set offline (lower is better); see app/sim/README.md
python3 -m app.sim.suite --check
python3 -m app.sim.virtual_set --replay NAME --out DIR
python3 -m app.sim.compare A/report.json B/report.json
```

Live-console rules the sim enforces (details in `CLAUDE.md` section 4 and `app/sim/LEARNINGS.md`): tonal blends need Camelot >= 0.6 (`KEY_SAFE_MIN`) else Echo Out, stem merge / handoff / peak keep a 0.8 floor; key-locked stretch capped at 8%; the energy last-round `force` widens rises only; a stem intro or strip on a silent stem is refused; electronic sub-families follow the neighbour table in `analysis/genre.py`; a studied-set macro's next song is the first deadline fallback (gates waived); hand-started moves fire on the phrase line; $Up3R-M@SS!V3-M0v3 runs only when a variant (`app/music_brain/supermove/variants/`) is loaded or pressed; analysis v6 sections / drops need `python3 -m app.music_brain.analysis.reanalyse --all` after a version bump.

---

## 6. Rules for Modifying Code

1. **Preserve Audio Alignment Precision:** Ensure millisecond/sample-accurate transient placement when splicing audio arrays in DSP modules.
2. **Handle DSP Artifacts:** Always apply gentle crossfade smoothing ($5\text{--}15\text{ ms}$) on cut transitions to eliminate digital audio zero-crossing clicks.
3. **Graceful Fallbacks:** If external APIs are absent or rate-limited, fall back cleanly to local Librosa acoustic heuristics.
4. **Follow Performance Constraints:** Consult `docs/engineering/PERFORMANCE_AUDIT.md` before altering `deck-controller.js` or `app/ui/static/` animation frames to ensure 60 FPS locked UI and $<10\text{ms}$ audio latency.

---

## 7. Rules for Modifying Notes in `./DJ/`

1. **5 Pedagogical Questions:** Every conceptual guide must answer:
   * *What is it?*
   * *Why does it matter?*
   * *What does it sound/feel like?*
   * *How do I practice it?*
   * *When should I deliberately break the rule?*
2. **17-Part Template:** All new transition recipes must use the standardized 17-part format in `05 - Transition Cookbook/`.
3. **Obsidian Wiki-Links:** Always format links as `[[Note Name]]`. Never use raw relative filesystem paths inside note bodies.
4. **Location Constraint:** All markdown knowledge base notes must remain inside `./DJ/`.
