# AI DJ Mixing System

## Overview

The **AI DJ Mixing System** is a professional, AI-powered DJ mixing pipeline that creates seamless, radio-quality mixes from your local music library. Using advanced audio analysis, machine learning, and OpenAI's GPT models, it automatically selects tracks, analyzes their musical characteristics, detects optimal transition points, and creates professional DJ transitions with perfect beat-grid alignment, gradual tempo sync, and intelligent energy flow management.

### What Makes This Special?

This isn't just audio stitching—it's **real professional DJ technique** automated with cutting-edge AI:

- 🎯 **Intelligent Transition Detection** - AI finds 3 optimal transition points per song (verse_end, chorus_end, breakdown_start, pre_drop)
- 🎛️ **Perfect Beat-Grid Alignment** - Sample-accurate beat synchronization with downbeat matching
- 🌊 **Gradual Tempo Sync** - Professional CDJ-style tempo ramping (32 micro-steps) instead of instant stretching
- 📊 **Energy Curve Analysis** - Detects buildups, drops, and energy transitions for context-aware mixing
- 🎵 **Harmonic Mixing** - Uses the Camelot Wheel to ensure key compatibility
- 🔬 **Multi-Factor Scoring** - Evaluates 6 factors (genre, vocals, energy, keys, BPM, type) to select best transitions
- 🎚️ **Progressive EQ Filtering** - Prevents frequency mud during overlaps
- 🎼 **Phrase-Based Transitions** - Snaps to 8-bar musical boundaries for natural timing
- 🎧 **Genre-Adaptive Mixing** - Different strategies for Afrobeats, R&B, EDM, Hip-Hop, Dancehall
- ⚡ **Dynamic Overlap Duration** - 4-16 second transitions based on musical context

### From Natural Language to Professional Mix

Describe what you want in plain English, and the system handles everything:

```
"start with love me jeje, then sensational, work me out, and finally outside"
```

The system will:

1. **Select** matching songs from your `./data/songs/` folder using OpenAI GPT-4o
2. **Analyze** each track with librosa (BPM, energy, key, phrase boundaries)
3. **Detect** 3 transition candidates per song using GPT-4o + energy curve analysis
4. **Score** each transition point using intelligent multi-factor algorithm
5. **Align** beats perfectly with beat-grid warping and downbeat synchronization
6. **Sync** tempos gradually (32 micro-steps) for inaudible transitions
7. **Mix** with professional EQ filtering and harmonic compatibility
8. **Export** a polished `data/output/mix.mp3` ready to play

---

## ✨ Key Features (Latest Updates)

| Feature                                 | Description                                                              | Status               |
| --------------------------------------- | ------------------------------------------------------------------------ | -------------------- |
| **🎤 Natural Language Input**           | Describe your mix requirements in plain English                          | ✅ Fully Implemented |
| **🎵 Smart Track Selection**            | OpenAI GPT-4o analyzes your library and picks matching songs             | ✅ Fully Implemented |
| **🔊 Echo Transition**                  | 3-second echo fadeout at first chorus end + incoming track starts        | ✅ NEW               |
| **🎯 Intelligent Transition Detection** | AI finds 3 optimal exit points per song (breakdown, verse end, pre-drop) | ✅ Fully Implemented |
| **📊 Energy Curve Analysis**            | Detects buildups, drops, and energy transitions for smart mixing         | ✅ Fully Implemented |
| **🔬 Multi-Factor Transition Scoring**  | 6-factor algorithm (genre, vocals, energy, keys, BPM, type)              | ✅ Fully Implemented |
| **🎛️ Perfect Beat-Grid Alignment**      | Sample-accurate beat sync with downbeat matching & micro-corrections     | ✅ Fully Implemented |
| **🌊 Gradual Tempo Sync**               | CDJ-style tempo ramping (32 micro-steps) instead of instant stretch      | ✅ Fully Implemented |
| **🎼 Harmonic Mixing (Camelot Wheel)**  | Ensures key compatibility between transitions                            | ✅ Fully Implemented |
| **🎚️ Professional EQ Filtering**        | Progressive Butterworth filters (low-pass/high-pass)                     | ✅ Fully Implemented |
| **📐 Phrase-Based Transitions**         | Snaps to 8-bar musical boundaries                                        | ✅ Fully Implemented |
| **🎧 Genre-Specific Rules**             | Afrobeats, R&B, EDM, Hip-Hop, Dancehall adaptive mixing                  | ✅ Enhanced          |
| **⚡ Dynamic Overlap Duration**         | 4-16 second transitions based on context                                 | ✅ Fully Implemented |
| **💾 Intelligent Caching**              | Stores analysis results in `research/notes/` for instant re-runs                  | ✅ Fully Implemented |
| **🎬 OpenAI Whisper Integration**       | Fast vocal/structure detection via API                                   | ✅ Fully Implemented |
| **📈 Automatic BPM Correction**         | Fixes 2x/0.5x detection errors in slow R&B/Afrobeats                     | ✅ Fully Implemented |

### Echo Transition (New Default)

The system now uses **echo-transition** as the default mixing style:

1. **First song plays** until the end of its first chorus
2. **At first chorus end**, a 3-second echo effect is applied (decaying repeats)
3. **Second song starts** playing simultaneously over the echoing first song
4. **Echo fades out** while second song continues cleanly
5. **Repeat** for each song transition

This creates a professional radio-style transition that:

- Gives listeners a satisfying ending to each song's chorus
- Creates an exciting echo effect during transitions
- Allows the new song to start fresh without muddy overlaps

### Professional DJ Improvements

Our system implements **10 advanced DJ techniques**:

1. **Intelligent Transition Detection** - AI-selected optimal exit points (not fixed timestamps)
2. **Energy Curve Analysis** - Detects buildups/drops for perfect timing
3. **Multi-Factor Scoring** - Context-aware transition selection
4. **Perfect Beat Alignment** - Sample-accurate synchronization with beat-grid warping
5. **Gradual Tempo Sync** - Professional CDJ-style ramping (32 micro-steps)
6. **EQ Filtering** - Prevents muddy sound during transitions
7. **Harmonic Mixing** - Uses music theory for perfect key compatibility
8. **Dynamic Overlap** - Adjusts transition length intelligently
9. **Phrase Boundaries** - Transitions at natural musical breaks
10. **Genre Rules** - Different strategies per music style

---

## 📋 Prerequisites

| Requirement             | Version | Installation                                                      |
| ----------------------- | ------- | ----------------------------------------------------------------- |
| **Python**              | 3.8+    | [Download Python](https://www.python.org/downloads/)              |
| **FFmpeg**              | Latest  | [Download FFmpeg](https://ffmpeg.org/download.html) - Add to PATH |
| **OpenAI API Key**      | -       | Get from [platform.openai.com](https://platform.openai.com)       |
| **Local Music Library** | -       | Place MP3 files in `./data/songs/` folder                              |

### System Requirements

- **RAM**: 4GB minimum (8GB recommended for large libraries)
- **Storage**: ~500MB for dependencies + your music library
- **CPU**: Modern multi-core processor recommended for faster analysis

---

## 🚀 Installation

### 1. Clone the Repository

```bash
git clone https://github.com/kckDeepak/AI-DJ-Mixing-System.git
cd AI-DJ-Mixing-System
```

### 2. Create Virtual Environment (Recommended)

```bash
# Windows
python -m venv myenv
myenv\Scripts\activate

# macOS/Linux
python3 -m venv myenv
source myenv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

**Core dependencies installed:**

- `librosa` - Audio analysis and beat detection
- `pydub` - Audio manipulation and mixing
- `scipy` - Signal processing for EQ filters
- `numpy` - Array operations
- `openai` - OpenAI API integration
- `python-dotenv` - Environment variable management

### 4. Install FFmpeg

**Windows:**

1. Download from [ffmpeg.org](https://ffmpeg.org/download.html)
2. Extract to `C:\ffmpeg`
3. Add `C:\ffmpeg\bin` to System PATH

**macOS:**

```bash
brew install ffmpeg
```

**Linux:**

```bash
sudo apt-get install ffmpeg  # Ubuntu/Debian
sudo yum install ffmpeg      # CentOS/RHEL
```

### 5. Configure API Keys

Create a `.env` file in the project root:

```env
OPENAI_API_KEY=sk-your-openai-api-key-here
```

Get your OpenAI API key from: https://platform.openai.com/api-keys

---

## 📁 Project Structure

```
AI-DJ-Mixing-System/
│
├── songs/                          # 🎵 Your MP3 library (INPUT)
│   ├── Artist - Song1.mp3
│   ├── Artist - Song2.mp3
│   └── ...
│
├── output/                         # 📤 Generated files (OUTPUT)
│   ├── analyzed_setlist.json      # Selected songs with OpenAI analysis
│   ├── basic_setlist.json         # BPM, genre, key metadata
│   ├── structure_data.json        # Chorus/vocal/transition points
│   ├── mixing_plan.json           # Professional DJ cue sheet
│   └── mix.mp3                    # 🎧 FINAL MIX (your result!)
│
├── app/                             # ▶️ THE MAIN PLAYER
│   ├── music_brain/                # 🧠 AI Music Brain package (interactive workbench engine)
│   │   ├── knowledge_parser.py    # Parses DJ/05 - Transition Cookbook/ into TransitionRecipe objects
│   │   ├── analyzer.py            # BPM, beatgrid, Camelot key, structure, vocal presence (librosa)
│   │   ├── stem_service.py        # Demucs 4-stem/2-stem separation with disk caching
│   │   ├── recipe_matcher.py      # Scores/ranks the 28 recipes for a track pair, with explanations
│   │   ├── dsp_rack.py             # 3-band EQ, Linkwitz-Riley crossover, filter sweeps, delay, reverb, limiter
│   │   ├── transition_renderer.py # Renders 15-30s previews and full master mixes
│   │   ├── config.py               # Central paths, cache dirs, model settings
│   │   └── agent_bridge.py         # CLI + Python API for coding agents (JSON in/out)
│   │
│   ├── ui/                         # 🖥️ Interactive web workbench
│   │   ├── server.py               # FastAPI backend (upload, analyze, match, preview, render)
│   │   └── static/                 # Dual-waveform DJ console frontend (Wavesurfer.js)
│   │
│   ├── tests/                       # ✅ pytest suite for app.music_brain / app.ui
│   │
│   └── legacy_pipeline/             # 📼 Original end-to-end batch pipeline
│       ├── run_pipeline.py         # ▶️ legacy entry point
│       ├── track_analysis_openai_approach.py  # Stage 1: Song selection
│       ├── bpm_lookup.py           # Stage 2: BPM/metadata enrichment
│       ├── structure_detector.py   # Stage 3: Structure analysis
│       ├── generate_mixing_plan.py # Stage 4: Transition planning
│       ├── mixing_engine.py        # Stage 5: Final mix generation
│       ├── waveform_visualizer.py  # Waveform image generation
│       └── gemini_client.py        # Shared Gemini API client
│
├── downloader/                      # ⬇️ YouTube -> MP3 downloader
│   └── downloader.py                # yt-dlp based 320kbps MP3 downloader
│
├── DJ/                               # 📚 Ground-truth DJ knowledge base (Obsidian vault)
│   └── 05 - Transition Cookbook/   #    28 standardized transition recipes
│
├── research/                        # 🔬 Experimental / superseded code (not part of the app)
│   ├── research_stuff/             # Chorus-detection/OpenAI prompt experiments
│   ├── older_way/                  # Previous pipeline version (pre-librosa, Gemini-based)
│   └── notes/                      # 💾 Cached analysis (auto-generated)
│
├── data/                            # Runtime artifacts, not source (all gitignored)
│   ├── songs/                       # Local audio files to mix
│   ├── output/                      # Rendered mixes / mixing plans
│   └── cache/                       # Analysis/stem/preview/sample/recording cache
│
├── assets/                          # Reference screenshots
├── requirements.txt                 # 📦 Python dependencies
├── .env                             # 🔑 API keys (create this!)
└── README.md                        # 📖 This file
```

---

## 🧠 AI Music Brain: Interactive Transition Workbench & Live Maker

Alongside the automated `app/legacy_pipeline/run_pipeline.py` batch pipeline, this project features the **AI Music Brain Interactive Workbench** (spec: `MUSIC_BRAIN_IMPLEMENTATION_PROMPT.md`) — a club-grade browser DJ workstation (FastAPI + Web Audio API + Wavesurfer.js) grounded in the 28 professional transition recipes documented in `DJ/05 - Transition Cookbook/`.

### 🎛️ DJ Console Workbench Overview

![DJ Console Workbench Overview](assets/ui-workbench-overview.png)

The web console provides a full-featured digital DJ workstation:
- **PULSE AI Engine Bar**: Live tempo-matching telemetry, master output VU meters, and Deck A/B insert FX racks (Filter, Echo, Reverb, Flanger, Phaser, Crusher).
- **Dual-Stacked Waveforms**: Synchronized viewport with downbeat alignment, phrase boundaries, and live transition region trimming.
- **AI Transition Brain**: Automatic detection and ranking of optimal transition recipes with harmonic and phrase-matched compatibility scoring.
- **Hardware Deck Controllers & Mixer**: Responsive jog wheels, pitch/tempo sliders (±8%), 3-band kill EQs, crossfader with equal-power curves, and live recording engine with `djset-v1` event logging.
- **8-Slot Synthesized Sampler**: Real-time one-shot drum and FX pads (Kick, Snare, Clap, Hat, Open Hat, Tom, Zap, Sweep) routed directly to master.
- **Track Browser**: Fast library search, automatic sorting by BPM/Key/Energy, and one-click loading to Deck A or Deck B.

---

### 🌊 Dual-Stacked Waveform Viewport & Telemetry

![Waveform Stage & Transition Points](assets/ui-waveform-stage.png)

- **Sample-Accurate Navigation**: Click anywhere on the waveform to seek; **Shift-click** pins a custom AI exit/entry point without interrupting audio playback.
- **Interactive Region Trimming**: Drag region handles to define custom mix boundaries for Track A and Track B.
- **Live Transition Telemetry**: Readouts track precise exit timestamps, intro/outro trims, and incoming entry points in real time.
- **Tempo Match & Key Display**: Instant visual indicator showing BPM differential (+2.00 BPM) and Camelot key alignment (8A / 8A).

---

### 🧠 AI Transition Brain & Live Transition Maker

![AI Transition Brain](assets/ui-ai-transition-brain.png)

- **Context-Aware Suggestions**: Analyzes energy curves, vocal overlap risks, and harmonic compatibility to score candidate recipes (e.g. *Bass Swap 95%*, *Drop Swap 88%*, *Echo Out 82%*).
- **Ghost Cue Markers**: Hovering any suggestion card displays ghost markers on both waveforms to visualize exact entry and exit cue points before committing.
- **Live Maker Automation**:
  - **ARM ON CONSOLE**: Arms the transition for live execution. Automatically pre-rolls both decks, aligns beat grids within ±8% tempo bounds, and schedules low-EQ cutoffs and crossfader automation.
  - **CANCEL LIVE RUN**: Instantly aborts pending automation without muting playback.
  - **DJ Manual Takeover**: Touching any fader or knob during an automated transition seamlessly transfers control back to the DJ with zero audio dropouts.
- **Instant DSP Audition**: Click **PREVIEW AT MY POINTS** or render full mixes with Demucs stem separation (*STEMS A*, *STEMS B*).

---

### 🎚️ Hardware Decks & 2-Channel Mixer

![Decks and 2-Channel Mixer](assets/ui-decks-and-mixer.png)

- **CDJ-Style Deck Controllers**: Vinyl jog wheels with spinning playhead animations, pitch faders with ±8% range, Tap Tempo, beat jumping (1/4 to 16 bars), and instant loop roll controls.
- **Performance Pads**: 4-pad matrix supporting **HOT CUE**, **ROLL**, **SLICER**, and **SAMPLER** modes.
- **Club-Grade 2-Channel Mixer**:
  - Independent **TRIM** and 3-band **HI / MID / LOW** rotary EQ knobs with visual frequency cuts.
  - High-precision vertical channel volume faders and dual stereo LED level meters.
  - Smooth **Equal-Power Crossfader** with live automation binding.
  - Master volume control and **REC MIX** button that records the performance and downloads both stereo audio (`.webm`/`.wav`) and a standardized `djset-v1` event log.

---

### 📱 Responsive Mobile View (390px Viewport)

![Responsive Mobile Console](assets/ui-mobile-view.png)

- Fully optimized for tablets and mobile devices with touch-friendly controls, adaptive vertical stacking, and responsive layout wrapping.
- All waveform interactions, live transition controls, mixer dials, and sampler pads remain fully accessible without viewport overflow.

---

### What it does

1. Load two tracks in the web UI (or via the CLI/Python API).
2. `analyzer.py` extracts BPM, beatgrid, downbeats, 8/16-bar phrase boundaries, Camelot key, energy curve, and structural sections (intro/verse/breakdown/drop/outro).
3. `recipe_matcher.py` scores all 28 transition recipes against that pair — Camelot key distance, BPM compatibility, phrase-boundary alignment, and vocal-collision risk — and returns the top 3 with plain-English explanations.
4. `transition_renderer.py` renders an instant 15-30s preview of the chosen recipe (real DSP: sub-bass hand-off for Bass Swap, tempo-synced echo throw for Echo Out, hard cuts, filter sweeps — not a naive linear crossfade).
5. `stem_service.py` can separate either track into Demucs stems (vocals/drums/bass/other) for stem-based recipes (Acapella Overlay, Stems Transition, etc.), cached by file hash.

### Running it

```bash
# Web workbench (dual waveforms, AI suggestions, instant preview)
uvicorn app.ui.server:app --reload
# then open http://127.0.0.1:8000

# Run automated browser end-to-end tests (Playwright)
npm run test:e2e

# CLI / agent bridge (structured JSON in, structured JSON out)
python -m app.music_brain.agent_bridge analyze data/songs/track1.mp3
python -m app.music_brain.agent_bridge match data/songs/track1.mp3 data/songs/track2.mp3
python -m app.music_brain.agent_bridge preview data/songs/track1.mp3 data/songs/track2.mp3 --recipe "Bass Swap"

# Python test suite
pytest app/tests/
```

**Notes on dependencies:**
- Demucs stem separation needs `torch`. Install a CUDA build from https://download.pytorch.org/whl/ for GPU acceleration, or the plain PyPI wheel for CPU-only.
- `dsp_rack.py` was originally speced to use Spotify's `pedalboard`, but its native code segfaulted unconditionally in testing on at least one Windows setup, so it was reimplemented in pure `scipy`/`numpy` — no `pedalboard` dependency.

---

## 🎮 Usage

### Quick Start (3 Steps)

#### 1. Add Your Music

Place your MP3 files in the `data/songs/` folder:

```
./data/songs/
├── Michael Jackson - Billie Jean.mp3
├── Tyla - Jump.mp3
├── Anirudh - Why This Kolaveri Di.mp3
└── ...
```

**Supported file naming:**

- `Artist - Title.mp3` ✅
- `01 - Title.mp3` ✅
- `[Source] Title.mp3` ✅
- Any MP3 format ✅

> [!TIP]
> **Need to download tracks from YouTube?**
> Use the built-in [YouTube Audio Downloader](downloader/README.md) in `./downloader/` to fetch 320 kbps MP3s with embedded artwork and ID3 tags:
> ```bash
> python downloader/downloader.py "https://www.youtube.com/watch?v=VIDEO_ID"
> ```

#### 2. Run the Pipeline

```bash
python app/legacy_pipeline/run_pipeline.py
```

The system will prompt you:

```
Enter your DJ mix request: Create a 5-song mix with upbeat energy
```

#### 3. Get Your Mix!

```
✅ Mix complete! → output/mix.mp3
```

### Advanced Usage

#### Customize the Prompt in Code

Edit `app/legacy_pipeline/run_pipeline.py` (line ~70):

```python
user_input = (
    "Create a 10-song progressive mix starting from slowest BPM. "
    "Include these must-have tracks: "
    "Michael Jackson - Billie Jean, "
    "Tyla - Jump. "
    "Focus on R&B and Afrobeats genres."
)
```

#### Command-Line Options

The pipeline accepts natural language, so you can request:

- **Genre-specific mixes**: `"Mix 8 EDM tracks with high energy"`
- **BPM progression**: `"10 songs sorted slowest to fastest"`
- **Must-include tracks**: `"Must include Artist - Song and Artist2 - Song2"`
- **Energy levels**: `"Create a chill low-energy mix for background music"`

---

## 🔄 How the Pipeline Works

### The 5-Stage Process

```mermaid
graph LR
    A[User Prompt] --> B[Stage 1: Selection]
    B --> C[Stage 2: BPM Lookup]
    C --> D[Stage 3: Structure Detection]
    D --> E[Stage 4: Mixing Plan]
    E --> F[Stage 5: Final Mix]
    F --> G[mix.mp3]
```

### Stage-by-Stage Breakdown

#### **Stage 1: Song Selection** (`track_analysis_openai_approach.py`)

**What it does:**

- Scans your `./data/songs/` folder
- Uses OpenAI GPT-4o to understand your natural language prompt
- Selects best-matching songs based on genre, energy, artist, title
- **Optional**: Applies energy curve ordering (warm up → peak → cool down)

**Output:** `data/output/analyzed_setlist.json`

**Example:**

```json
{
  "analyzed_setlist": [
    {
      "time": "00:00",
      "analyzed_tracks": [
        {
          "title": "Billie Jean",
          "artist": "Michael Jackson",
          "file": "Michael Jackson - Billie Jean.mp3"
        }
      ]
    }
  ]
}
```

---

#### **Stage 2: BPM & Metadata Enrichment** (`app/legacy_pipeline/bpm_lookup.py`)

**What it does:**

- Loads each MP3 file with librosa
- Detects BPM using beat tracking (NOT OpenAI - pure librosa!)
- Calculates energy level (RMS normalization: 0-1 scale)
- Uses OpenAI for genre and key detection (optional)
- **Caches results** in `notes/{song}_metadata.json` for instant re-runs

**Output:** `data/output/basic_setlist.json` + cached metadata

**Example metadata (`research/notes/` folder):**

```json
{
  "title": "Billie Jean",
  "artist": "Michael Jackson",
  "file": "Michael Jackson - Billie Jean.mp3",
  "bpm": 117,
  "genre": "Pop",
  "key": "F#m",
  "key_semitone": 18,
  "scale": "minor",
  "energy": 0.82
}
```

**Key Innovation:** Librosa-only BPM detection ensures accuracy without external API costs!

---

#### **Stage 3: Structure Detection** (`app/legacy_pipeline/structure_detector.py`)

**What it does:**

- Uses OpenAI Whisper API for fast transcription
- Detects vocal presence in first 8 seconds (prevents vocal overlap)
- Finds optimal transition points using beat tracking
- Calculates **phrase boundaries** (every 8 bars = 32 beats)
- **Caches results** in `notes/{song}_structure.json`

**Output:** `data/output/structure_data.json` + cached structure

**Example structure:**

```json
{
  "has_vocals": true,
  "has_vocals_in_first_8s": false,
  "transition_point": 72.0,
  "intro_duration": 8.5
}
```

**Why this matters:**

- `has_vocals_in_first_8s`: If true, previous song must finish cleanly (no overlap)
- `transition_point`: Best time to start transitioning OUT
- `intro_duration`: Safe zone before vocals start

---

#### **Stage 4: Mixing Plan Generation** (`app/legacy_pipeline/generate_mixing_plan.py`)

**What it does:**

- **Sorts songs by BPM** (lowest to highest) for smooth energy build
- Calculates **harmonic compatibility** using Camelot Wheel
- Determines **dynamic overlap duration** (4-16 seconds)
- Applies **genre-specific rules** (EDM = longer, Hip-Hop = shorter)
- **Snaps transitions to phrase boundaries** (8-bar alignment)

**Output:** `data/output/mixing_plan.json`

**Example plan:**

```json
{
  "mixing_plan": [
    {
      "from_track": "Billie Jean",
      "to_track": "Jump",
      "incoming_start_sec": 183.5,
      "start_time": "00:03:03",
      "transition_point": 192.0,
      "overlap_duration": 12.0,
      "transition_type": "Transition Overlap",
      "to_bpm": 120,
      "from_bpm": 117,
      "comment": "Billie Jean (BPM 117) -> Jump (BPM 120). Dynamic 12.0s overlap (no early vocals). ✓ Keys match (F#m→G). Transition at 192.0s, BPM change at 184.0s"
    }
  ]
}
```

**Professional Features:**

- ✅ **Harmonic mixing**: "Keys match (F#m→G)" or "⚠️ Key clash (C→F#)"
- ✅ **Dynamic overlap**: Adjusts based on BPM difference, key compatibility, genre
- ✅ **Vocal-aware**: Prevents overlapping vocals
- ✅ **Phrase alignment**: Transitions at natural musical breaks

---

#### **Stage 5: Final Mix Generation** (`app/legacy_pipeline/mixing_engine.py`)

**What it does:**

- Loads MP3 files based on mixing plan
- Applies **professional DJ techniques**:
  - 🎚️ **Progressive EQ filtering** (low-pass on outgoing, high-pass on incoming)
  - 🎵 **Beat-matching** with minimal time-stretching (±2%)
  - 🎯 **Phrase-aligned transitions**
  - 🎧 **Genre-specific overlap** (EDM = 16s, Hip-Hop = 4s, etc.)
- Normalizes final output for consistent volume
- Exports polished `data/output/mix.mp3`

**Output:** `data/output/mix.mp3` (final professional mix!)

**Technical Details:**

- **EQ Filters**: 4th-order Butterworth (scipy.signal)
  - Low-pass: Starts at 12kHz → gradually cuts to 4kHz
  - High-pass: Starts at 100Hz → gradually cuts to 300Hz
- **Time-stretching**: Librosa phase vocoder (preserves pitch)
- **Normalization**: pydub normalize() ensures consistent loudness

---

## 🎵 Understanding the Cache System (`research/notes/` folder)

The system intelligently caches analysis results to avoid re-processing:

### Why Caching Matters

- **Speed**: Instant re-runs (no re-analysis needed)
- **Cost**: Saves OpenAI API calls
- **Consistency**: Same results every time

### Cache File Types

#### 1. Metadata Cache (`*_metadata.json`)

```json
{
  "bpm": 117, // Librosa beat tracking
  "genre": "Pop", // OpenAI classification
  "key": "F#m", // OpenAI key detection
  "energy": 0.82 // RMS energy (0-1 scale)
}
```

#### 2. Structure Cache (`*_structure.json`)

```json
{
  "has_vocals": true, // OpenAI Whisper detection
  "has_vocals_in_first_8s": false,
  "transition_point": 72.0, // Librosa beat analysis
  "intro_duration": 8.5
}
```

### Cache Invalidation

To force re-analysis:

```bash
# Delete specific song cache
rm notes/"Song Name"*.json

# Clear all cache
rm notes/*.json
```

---

## 🔬 Research & Development

### `research_stuff/` Directory

Contains experimental features and proof-of-concepts:

| File                               | Purpose                                  | Status      |
| ---------------------------------- | ---------------------------------------- | ----------- |
| `denis_chorus.py`                  | Chorus detection using NMF decomposition | 🔬 Research |
| `test_pychorus.py`                 | PyChorus library evaluation              | 🔬 Research |
| `test_madmom.py`                   | Madmom beat tracking comparison          | 🔬 Research |
| `openai/dj_transition_detector.py` | AI-based transition detection            | 🔬 Research |

**Why these aren't in production:**

- PyChorus/Madmom: Slower than librosa, similar accuracy
- NMF chorus detection: Unreliable on diverse genres
- AI transition detection: OpenAI Whisper API is faster/cheaper

### `older_way/` Directory

Previous pipeline version (pre-professional improvements):

**Key differences from current version:**

| Feature               | Old Way                       | New Way (Current)                    |
| --------------------- | ----------------------------- | ------------------------------------ |
| **BPM Detection**     | OpenAI GPT → librosa fallback | Librosa-only (faster, more accurate) |
| **Song Ordering**     | OpenAI decides order          | BPM-sorted (progressive energy)      |
| **Transitions**       | Fixed 8s overlap              | Dynamic 4-16s based on context       |
| **Key Compatibility** | Not considered                | Full Camelot Wheel implementation    |
| **EQ Filtering**      | None                          | Progressive Butterworth filters      |
| **Phrase Alignment**  | Random timing                 | 8-bar boundary snapping              |
| **API Provider**      | Google Gemini                 | OpenAI GPT-4o + Whisper              |

**Why we switched:**

- ✅ Librosa BPM is more reliable than AI guessing
- ✅ BPM sorting creates better energy flow
- ✅ Professional DJ techniques (EQ, harmonic mixing, phrases)
- ✅ OpenAI API is more stable/powerful than Gemini

---

## 🎯 Console Output Explained

When running the pipeline, you'll see these messages:

### Good Signs ✅

```
✅ Perfect key match: C → G
  → System found harmonically compatible keys (perfect fifth)

Dynamic 12.0s overlap (no early vocals)
  → Context-aware transition (not fixed 8s)

Applying EQ: Low-pass on outgoing, High-pass on incoming
  → Frequency filtering active (prevents muddy sound)

→ Snapped 70.0s to phrase boundary at 72.0s
  → Transition aligned to musical phrase (8-bar boundary)

Librosa: BPM=128, Energy=0.75
  → Accurate beat tracking + energy analysis working
```

### Warnings ⚠️

```
⚠️ Key clash: C → F#, shorter transition
  → System detected dissonant keys, using quick cut (4s)

⚠️ Energy data not available, keeping original order
  → Need to run BPM analysis stage first
```

### Errors ❌

```
❌ No songs found in ./songs directory
  → Add MP3 files to the songs/ folder

❌ OpenAI API key not found
  → Create .env file with OPENAI_API_KEY
```

---

## ⚙️ Running on Other Systems

### Windows

```bash
# Activate virtual environment
myenv\Scripts\activate

# Run pipeline
python app/legacy_pipeline/run_pipeline.py
```

### macOS/Linux

```bash
# Activate virtual environment
source myenv/bin/activate

# Run pipeline
python3 app/legacy_pipeline/run_pipeline.py
```

### Docker (Coming Soon)

```bash
docker build -t ai-dj-mixer .
docker run -v ./songs:/app/songs -v ./output:/app/output ai-dj-mixer
```

### Cloud Deployment Considerations

**For AWS/Google Cloud/Azure:**

1. Install FFmpeg in container: `apt-get install ffmpeg`
2. Mount music library as volume
3. Set environment variables for API keys
4. Allocate sufficient RAM (8GB recommended)

**Estimated Processing Time:**

- 5 songs: ~30 seconds
- 10 songs: ~1-2 minutes
- 20 songs: ~3-5 minutes
- 50+ songs: ~10-15 minutes

(Times vary based on CPU and whether cache exists)

---

## 🚧 Current Limitations & Future Roadmap

### What We've Achieved ✅

- Professional-grade harmonic mixing (Camelot Wheel)
- Accurate BPM detection (librosa beat tracking)
- Intelligent EQ filtering (prevents muddy sound)
- Phrase-aligned transitions (natural musical timing)
- Genre-adaptive mixing strategies
- Energy curve management for professional flow
- Intelligent caching for fast re-runs

### Known Limitations ⚠️

| Limitation                  | Impact                                            | Workaround                              |
| --------------------------- | ------------------------------------------------- | --------------------------------------- |
| **No real-time tempo sync** | Time-stretch entire songs instead of just overlap | Minimal BPM difference recommended      |
| **No advanced effects**     | Missing reverb, echo, delays                      | Could add with pydub effects            |
| **Beat grid drift**         | Long transitions may lose sync                    | Current: limited to 16s overlap         |
| **No live input**           | Pre-recorded mixes only                           | Future: real-time DJ controller support |
| **Genre detection via AI**  | May misclassify niche genres                      | Can manually edit `notes/*.json`        |

### Future Enhancements 🚀

**High Priority:**

- [ ] **Real-time beat grid sync** - Keep beats aligned throughout transition
- [ ] **Smart BPM stretch** - Only stretch during overlap (not full song)
- [ ] **Advanced effects** - Reverb, echo, filter sweeps
- [ ] **Vocal isolation** - Better vocal detection using Spleeter/Demucs
- [ ] **Visual waveform output** - See transitions visually

**Medium Priority:**

- [ ] **Web UI** - Drag-and-drop interface for non-technical users
- [ ] **Spotify/YouTube integration** - Auto-download from playlists
- [ ] **BPM range filtering** - "Only use 120-130 BPM songs"
- [ ] **Custom transition rules** - User-defined mixing preferences
- [ ] **Live preview** - Listen to transitions before final render

**Research Phase:**

- [ ] **AI-generated transitions** - Let GPT-4 decide transition type
- [ ] **Mood-based mixing** - Analyze emotional arc beyond just energy
- [ ] **Multi-genre bridging** - Smooth transitions between distant genres
- [ ] **Crowd reaction prediction** - ML model for "will this transition work?"

---

## 🤝 Contributing

We welcome contributions! Here's how you can help:

### Areas Where We Need Help

1. **Audio Processing** - Improve EQ algorithms, add more effects
2. **Machine Learning** - Better energy/mood prediction models
3. **Testing** - Add more test cases, edge cases
4. **Documentation** - Improve tutorials, add video walkthroughs
5. **UI/UX** - Build a web interface or desktop app

### How to Contribute

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### Code Style

- Follow PEP 8 for Python code
- Add docstrings to all functions
- Include type hints where possible
- Write tests for new features

---

## 📊 Performance Benchmarks

**Test System:** Windows 11, Intel i7-12700K, 32GB RAM

| Mix Size | Analysis Time | Mixing Time | Total Time   |
| -------- | ------------- | ----------- | ------------ |
| 5 songs  | ~15 seconds   | ~15 seconds | ~30 seconds  |
| 10 songs | ~30 seconds   | ~45 seconds | ~1.5 minutes |
| 20 songs | ~1 minute     | ~2 minutes  | ~3 minutes   |
| 50 songs | ~3 minutes    | ~7 minutes  | ~10 minutes  |

**With cache (re-runs):**

- 5 songs: ~5 seconds ⚡
- 10 songs: ~10 seconds ⚡
- 20 songs: ~30 seconds ⚡

**Factors affecting speed:**

- CPU speed (librosa analysis is CPU-intensive)
- MP3 quality/bitrate (higher quality = slower)
- Cache availability (huge speedup on re-runs)
- Number of songs in library (scanning takes time)

---

## 🐛 Troubleshooting Guide

### Common Issues

**1. "FFmpeg not found"**

```bash
# Windows - Add FFmpeg to PATH
1. Download FFmpeg from ffmpeg.org
2. Extract to C:\ffmpeg
3. Add C:\ffmpeg\bin to System PATH
4. Restart terminal

# Verify installation
ffmpeg -version
```

**2. "OpenAI API key not found"**

```bash
# Create .env file in project root
echo "OPENAI_API_KEY=sk-your-key-here" > .env
```

**3. "No songs found"**

```bash
# Check songs directory
ls songs/

# Add MP3 files
cp /path/to/music/*.mp3 songs/
```

**4. "librosa fails to load audio"**

```python
# Check file format
from pydub import AudioSegment
audio = AudioSegment.from_mp3("songs/problematic.mp3")
audio.export("songs/fixed.mp3", format="mp3")
```

**5. "Mix sounds distorted"**

```bash
# Clear cache and re-analyze
rm notes/*.json
python app/legacy_pipeline/run_pipeline.py
```

**6. "Out of memory error"**

```python
# Reduce number of songs in one mix
# Or increase system RAM
# Or process in batches
```

### Debug Mode

Enable detailed logging:

```python
# In app/legacy_pipeline/run_pipeline.py, change:
logging.basicConfig(level=logging.DEBUG)
```

---

## 📖 Additional Resources

### Music Theory Background

**Camelot Wheel Explained:**

- [Wikipedia: Harmonic Mixing](https://en.wikipedia.org/wiki/Harmonic_mixing)
- [Mixed In Key: Camelot Wheel Guide](https://mixedinkey.com/harmonic-mixing-guide/)

**DJ Techniques:**

- [Cross DJ Blog: Beatmatching Guide](https://blog.mixvibes.com/beatmatching-guide/)
- [DJ TechTools: EQ Mixing](https://djtechtools.com/2014/09/22/eq-mixing-fundamentals/)

### Technical Documentation

**Libraries Used:**

- [Librosa Documentation](https://librosa.org/doc/latest/)
- [PyDub Documentation](https://github.com/jiaaro/pydub)
- [OpenAI API Reference](https://platform.openai.com/docs/api-reference)
- [SciPy Signal Processing](https://docs.scipy.org/doc/scipy/reference/signal.html)

### Related Projects

- [Essentia](https://essentia.upf.edu/) - Audio analysis library
- [Madmom](https://github.com/CPJKU/madmom) - Music information retrieval
- [PyChorus](https://github.com/vivjay30/pychorus) - Chorus detection
- [Spleeter](https://github.com/deezer/spleeter) - Source separation

---

## ⚖️ License

**MIT License** - Free to use, modify, and distribute.

See [LICENSE](LICENSE) file for full text.

---

## ⚠️ Legal Disclaimer

This tool is for **personal, educational, and non-commercial use only** with **legally obtained music**.

**You must:**

- ✅ Own the MP3 files you're mixing
- ✅ Have proper licenses for commercial use
- ✅ Respect copyright laws in your jurisdiction

**Not for:**

- ❌ Pirated music
- ❌ Commercial DJ gigs without proper licensing
- ❌ Public broadcast without rights clearance

**The developers are not responsible for any misuse of this software.**

---

## 🙏 Acknowledgments

**Built with:**

- [OpenAI](https://openai.com) - GPT-4o and Whisper API
- [Librosa](https://librosa.org) - Audio analysis
- [PyDub](https://github.com/jiaaro/pydub) - Audio manipulation
- [SciPy](https://scipy.org) - Signal processing

**Inspired by:**

- Professional DJ techniques from real DJs
- Music information retrieval research
- Open-source audio processing community

**Special thanks to:**

- Contributors who helped test and improve the system
- The librosa community for excellent documentation
- DJ TechTools for mixing theory resources

---

## 📞 Support & Contact

**Found a bug?** Open an issue on [GitHub Issues](https://github.com/kckDeepak/AI-DJ-Mixing-System/issues)

**Have a question?** Check existing issues or start a discussion

**Want to contribute?** See the [Contributing](#-contributing) section above

---

**Made with ❤️ by DJs, for DJs**  
_Let the AI handle the math. You handle the vibe._ 🎧

---

## 📝 Changelog

### Version 4.0 (Latest) - September 2026
- 🧠 **AI Music Brain** — new `app/music_brain/` package: knowledge-driven transition matching against all 28 recipes in `DJ/05 - Transition Cookbook/`, librosa-based analysis (BPM/key/structure/vocal presence), Demucs stem separation, a pure scipy/numpy DSP rack (3-band EQ, Linkwitz-Riley sub-bass crossover, filter sweeps, delay, reverb, limiter), and an instant preview/full-mix renderer.
- 🖥️ **Interactive web workbench** — new `ui/` FastAPI app + Wavesurfer.js frontend: upload two tracks, get ranked AI transition suggestions with explanations, preview instantly, render the mix.
- 🖇️ **Agent bridge** — `python -m app.music_brain.agent_bridge` CLI + Python API, JSON in/out, for use by coding agents.
- ✅ **Test suite** — `app/tests/` pytest suite covering all of the above (103 tests), run against real sample audio and a real Demucs GPU pass.

### Version 3.0 - December 2025

**Major Features:**

- ✅ **Intelligent Transition Detection** - AI finds 3 optimal candidates per song (verse_end, chorus_end, breakdown_start, pre_drop)
- ✅ **Energy Curve Analysis** - Detects buildups and drops using RMS energy analysis
- ✅ **Multi-Factor Scoring System** - 6-factor algorithm evaluates transition quality (genre, vocals, energy, keys, BPM, type)
- ✅ **Perfect Beat-Grid Alignment** - Sample-accurate synchronization with downbeat matching and micro-corrections
- ✅ **Gradual Tempo Sync** - CDJ-style tempo ramping (32 micro-steps) instead of instant stretching
- ✅ **Genre-Specific Transition Rules** - Afrobeats, R&B, EDM, Hip-Hop, Dancehall preferences
- ✅ **Context-Aware Selection** - Transition points chosen based on next song's characteristics
- ✅ **Automatic BPM Correction** - Fixes 2x/0.5x detection errors in slow R&B/Afrobeats tracks

**Technical Improvements:**

- Enhanced GPT-4o prompts for better structure detection
- Beat-grid warping algorithm for continuous beat sync
- Energy compatibility scoring between consecutive tracks
- Vocal overlap prevention system
- Phrase boundary snapping for all transition candidates

**What Changed:**

- Transition points now vary per song pair (not fixed at 50s)
- Tempo changes are gradual over 8-16 seconds (not instant)
- Beat alignment maintains <10ms accuracy throughout overlaps
- Each transition includes scoring explanation and reasoning

### Version 2.0 - October 2024

- ✅ Complete rewrite with professional DJ features
- ✅ Harmonic mixing (Camelot Wheel)
- ✅ Progressive EQ filtering
- ✅ Phrase-based transitions
- ✅ Energy curve management
- ✅ Genre-specific mixing rules
- ✅ Librosa-only BPM detection
- ✅ Dynamic overlap duration
- ✅ Intelligent caching system
- ✅ BPM-sorted song ordering

---

## 🆕 What's New in Version 3.0

### Intelligent Transition Point Detection

**Before (v2.0):**

```
All transitions at fixed 50 seconds
No context awareness
One-size-fits-all approach
```

**After (v3.0):**

```json
{
  "transition_candidates": [
    { "time": 67.5, "type": "breakdown_start", "score": 92 },
    { "time": 82.3, "type": "chorus_end", "score": 78 },
    { "time": 95.0, "type": "pre_drop", "score": 85 }
  ],
  "selected": 67.5,
  "reasoning": "Instrumental breakdown, no vocals, perfect for beat-sync"
}
```

### Perfect Beat-Grid Alignment

**Before (v2.0):**

```python
# Only aligned first beat
lag = first_beat_outgoing - first_beat_incoming
# Hoped beats stay aligned after tempo stretch
```

**After (v3.0):**

```python
# Aligns ALL beats with micro-corrections
1. Detect downbeats (beat 1 of each bar)
2. Align first downbeat (<10ms)
3. Check each subsequent beat
4. Apply micro time-stretch per segment
5. Result: <10ms drift throughout overlap
```

### Gradual Tempo Sync

**Before (v2.0):**

```python
# Instant tempo change (audible artifacts)
stretched = time_stretch(audio, 1.15)  # 15% faster instantly
```

**After (v3.0):**

```python
# Gradual ramp over 32 micro-steps
for i in range(32):
    progress = i / 31
    stretch = 1.0 + (1.15 - 1.0) * progress  # 1.00→1.15 gradually
    # Each step ~0.3% change (inaudible)
```

### Energy Curve Analysis

**New capability:**

```python
{
  "energy_analysis": {
    "has_buildup": true,
    "buildups": [
      {"time": 58, "peak_time": 64, "intensity": 0.045}
    ],
    "drops": [
      {"time": 120, "depth": 0.08}
    ]
  }
}
```

**Use case:** Transition at buildup → drop in next song = massive energy payoff

---

## 🎯 Advanced Usage Examples

### Example 1: Afrobeats Party Mix

```python
user_input = "Create a 6-song Afrobeats mix with smooth transitions"
```

**System will:**

1. Find Afrobeats tracks from library
2. Prefer `verse_end` transitions (genre rule)
3. Use longer overlaps (10-12s) for smooth blending
4. Avoid vocal overlaps (Afrobeats heavy on vocals)
5. Select breakdown sections for instrumental blends

### Example 2: EDM Energy Build

```python
user_input = "EDM mix with energy building to climax"
```

**System will:**

1. Sort EDM tracks by BPM (low → high)
2. Prefer `pre_drop` transitions
3. Time transitions at buildups
4. Use long overlaps (14-16s)
5. Align to phrase boundaries (8-bar)

### Example 3: R&B Smooth Flow

```python
user_input = "Chill R&B mix for late night"
```

**System will:**

1. Find low-energy R&B tracks
2. Prefer `breakdown_start` transitions
3. Avoid vocal clashes (R&B vocal-heavy)
4. Use moderate overlaps (8-10s)
5. Maintain smooth energy curve

### Version 1.0 (Legacy) - October 2025

- Google Gemini API for song selection
- Basic BPM analysis
- Simple crossfade transitions
- Fixed 8-second overlaps
- No harmonic compatibility
- No EQ filtering

---

_Last Updated: December 12, 2025_
