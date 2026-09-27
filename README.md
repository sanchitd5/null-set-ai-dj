# AI DJ Mixing System

A local, browser-based AI DJ. It analyses your tracks, separates them into stems, picks the next song with a local LLM, and mixes live on two virtual decks using transition rules taken from a DJ knowledge base kept in this repository (`DJ/`).

Everything runs on your machine: FastAPI backend, a local LLM (MLX on Apple Silicon, Ollama as fallback), Demucs for stems, and a vanilla-JS console in the browser that does the actual audio playback and mixing.

## What it does

- **Two-deck console** (`app/ui/static/`): jog wheels, transport, hot cues, beat loops, 3-band EQ, pitch/volume faders, crossfader, per-deck FX rack, stem-shaded waveforms, sampler pads with a 16-step beat grid, and mix recording (MediaRecorder).
- **AI autopilot** (`autopilot.js`, `app/ui/autopilot_service.py`): seed track → LLM suggests next song → download → analyse → transition → repeat. Suggestions are checked against YouTube, filtered by tempo window and Camelot key, and remembered across sets (`app/ui/set_memory.py`) so the next set from the same seed is not a replay.
- **Stems-first mixing**: every track is separated into 4 stems (drums, bass, vocals, other) by a persistent background Demucs worker (`app/music_brain/stem_worker.py`, default model `htdemucs`). Library tracks are backfilled while the LLM is idle. With stems on both decks, transitions become long stem blends, vocal hand-offs, mashups and layered transitions.
- **Transition planners** (`app/music_brain/`): beat-to-beat blend, mashup ("A x B"), layer, BPM-ladder bridge for large tempo gaps, "riff over rap", and a library of learned techniques, each explaining why it fits or does not.
- **Key-locked tempo stems** (`keylock.py`): for pairs up to 25 % apart in tempo, stems are re-rendered at the target tempo with Rubber Band so pitch does not drift.
- **DJ mind** (`dj-mind.js`, `app/ui/mind_plan.py`): one LLM plan per song pair (candidate, exit phrase, phrase-level moves such as hold, pre-clear, sub drop, layer), validated against hard rules before and during playback.
- **Live ear** (`live-ear.js`, `app/ui/live_ear.py`): during a hold loop, a DSP watchdog measures loop seams, clipping and low-end clash; an optional audio model (Qwen3-Omni via mlx-vlm) proposes one of a few safe moves. Rules answer if the model is not running.
- **Downloads**: YouTube / YouTube Music search and download to FLAC via yt-dlp, with filters that skip mixes, live sets and interviews.
- **Set logs**: export a played set as a validated `djset-v1` log and an Obsidian note for `DJ/17 - Set Logs`.

## Requirements

- Python 3 with the packages in `requirements.txt` (librosa, numpy, scipy, soundfile, demucs, torch, fastapi, uvicorn, yt-dlp, and others)
- `ffmpeg` (needed for downloads)
- `rubberband` CLI (for key-locked tempo stems; e.g. `brew install rubberband`)
- An LLM backend, one of:
  - Apple Silicon: `mlx-lm` (default model `mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit`)
  - Anywhere: [Ollama](https://ollama.com) (default fallback model `gemma3:27b`)
- Optional: a separate venv at `~/.venvs/mlx-vlm` with `mlx-vlm` for the live ear model (it needs a Starlette version the app's FastAPI pin does not allow, hence the separate venv)
- Optional: Node.js to run the JS unit checks; Playwright for browser tests

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./start.sh            # starts the server, waits for the LLM, opens http://localhost:8000
```

`start.sh` options:

```bash
./start.sh --no-open       # do not open a browser tab
PORT=8010 ./start.sh       # another port
```

It stops any previous server, starts the live ear model if `~/.venvs/mlx-vlm` exists, starts `uvicorn app.ui.server:app`, and waits until `/api/llm/status` reports ready. Logs go to `/tmp/ai-dj-server.log` (server), `/tmp/ai-dj-mlx-server.log` (MLX) and `/tmp/ai-dj-omni-server.log` (live ear).

Without `start.sh`:

```bash
uvicorn app.ui.server:app --reload
```

The server boots the LLM itself on startup (`app/ui/model_runtime.py`): MLX first, Ollama if MLX is unavailable.

## Configuration (`.env`)

All settings are optional.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_BACKEND` | `auto` | `auto`, `mlx` or `ollama` |
| `MLX_MODEL` | `mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit` | MLX model |
| `MLX_PORT` | `8081` | port for `mlx_lm.server` |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server |
| `OLLAMA_MODEL` | `gemma3:27b` | Ollama fallback model |
| `AUTOPILOT_MODEL`, `OLLAMA_BASE_URL` | set by the runtime | override the OpenAI-compatible endpoint/model the autopilot calls |
| `AUTOPILOT_PLAN_MODEL` | `AUTOPILOT_MODEL` | model for the per-pair DJ-mind plan |
| `SUGGEST_BUDGET_S` | `15` | time budget for a suggestion |
| `SUGGEST_VERIFY` | `1` | set `0` to skip checking suggestions against YouTube |
| `DJ_LIBRARY_DIRS` | none | semicolon-separated local music folders to scan |
| `OMNI_BASE_URL` | `http://127.0.0.1:8901/v1` | live ear model endpoint (local mlx-vlm, or a DashScope URL) |
| `OMNI_MODEL` | `mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit` | live ear model (`qwen3.8-omni-flash` when the URL is cloud) |
| `OMNI_API_KEY` / `DASHSCOPE_API_KEY` | none | key for a cloud live ear endpoint |
| `OMNI_TIMEOUT_S` | `10` | live ear per-call timeout |

## Repository layout

```
app/
  music_brain/        engine: analysis, tempo, stems, recipe matching, DSP rack,
                      blend / mashup / layer / bridge / keylock planners, set logs
  ui/                 FastAPI server + services (autopilot, downloads, library,
                      LLM runtime and gate, DJ-mind plan, live ear, set memory)
  ui/static/          browser console (vanilla JS, no build step)
  tests/              pytest suite + Node checks (*_check.js)
  legacy_pipeline/    the original batch pipeline (see below)
downloader/           standalone YouTube → MP3 downloader
DJ/                   Obsidian DJ knowledge base (theory, 28 transition recipes, playbooks)
research/             experiments and set-study notes
data/                 songs, output and cache (gitignored)
e2e/                  Playwright browser tests
```

All runtime data lives under `data/cache/` (analysis, stems, previews, samples, recordings, set logs). Track ids are content hashes, so re-adding the same file is idempotent.

## The DJ knowledge base

`DJ/` is an Obsidian vault covering fundamentals, music theory (phrasing, Camelot), EQ and frequency ownership, 28 transition recipes in a fixed template, energy management, genre playbooks and artist case studies. The code reads it directly: `knowledge_parser.py` turns `DJ/05 - Transition Cookbook/` into executable recipes, and `dj_knowledge.py` condenses the decision rules into short briefs for LLM prompts. Rules applied in code include 8-bar phrase snapping, one track owning sub-bass (< 120 Hz) at a time, and Camelot distance scoring.

## Agent bridge (CLI)

`app/music_brain/agent_bridge.py` is a Python API and CLI that prints JSON, for scripts and coding agents. Run from the repo root:

```bash
python -m app.music_brain.agent_bridge analyze data/songs/a.mp3
python -m app.music_brain.agent_bridge separate data/songs/a.mp3 --stems 4
python -m app.music_brain.agent_bridge match data/songs/a.mp3 data/songs/b.mp3 --top-n 3
python -m app.music_brain.agent_bridge preview data/songs/a.mp3 data/songs/b.mp3 \
    --recipe "Bass Swap" --a-time 60 --b-time 10 --seconds 20 --out preview.mp3
python -m app.music_brain.agent_bridge list-recipes
```

Failures print `{"error": "..."}` and exit non-zero.

## REST API

The main endpoints in `app/ui/server.py`:

| Area | Endpoints |
|---|---|
| LLM | `GET /api/llm/status` |
| Tracks | `POST/GET /api/tracks`, `GET /api/tracks/{id}/analysis`, `GET /api/tracks/{id}/fame`, `GET /api/audio/tracks/{id}` |
| Library | `GET /api/library`, `POST /api/library/scan`, `GET /api/library/lockable` |
| Downloads | `POST /api/download`, `POST/GET /api/download/jobs`, `GET /api/download/jobs/{job_id}`, `GET /api/search/youtube` |
| Stems | `POST /api/tracks/{id}/separate`, `GET /api/tracks/{id}/stems`, `GET /api/tracks/{id}/stems/{name}`, `GET /api/tracks/{id}/vocals`, `GET /api/tracks/{id}/vocal_entry`, `GET /api/stems/status`, `GET /api/audio/stems/{id}/vocals` |
| Transitions | `GET /api/recipes`, `GET /api/techniques`, `POST /api/match`, `POST /api/blend/plan`, `POST /api/mashup/plan`, `POST /api/layer/plan`, `POST /api/bridge/plan`, `POST /api/riff/plan`, `GET /api/riff/{key}`, `POST /api/riff/{key}/balance` |
| Rendering | `POST /api/preview`, `GET /api/audio/previews/{filename}`, `POST /api/render`, `GET /api/audio/renders/{filename}` |
| Autopilot | `POST /api/autopilot/suggest`, `POST /api/autopilot/plan` |
| Live ear | `GET/POST /api/live/ear` |
| Sampler / recording | `POST/GET /api/samples`, `GET /api/samples/{id}`, `POST /api/recordings`, `GET /api/recordings/{id}` |
| Set logs | `POST /api/set-logs`, `GET /api/set-logs/{id}`, `GET /api/set-logs/{id}/markdown` |

## Tests

```bash
python -m pytest app/tests/ -q -m "not slow"   # "slow" = real Demucs separation
npm run test:e2e                                # Playwright browser tests
```

The Node checks for the browser modules (`app/tests/*_check.js`) are run from the pytest suite and are skipped if `node` is not installed.

## Standalone downloader

```bash
python downloader/downloader.py -o data/songs "<youtube_url>"   # 320 kbps MP3
```

Options: `-q` bitrate, `-f` file of URLs, `--no-playlist`, `--open`.

## Legacy batch pipeline

`app/legacy_pipeline/` holds the original offline pipeline: OpenAI/Gemini song selection over `data/songs/`, librosa structure detection, a mixing plan, and a rendered `data/output/mix.mp3`. It is kept for reference and is not used by the console.

```bash
python app/legacy_pipeline/run_pipeline.py
```

## License

MIT. See [LICENSE](LICENSE). Use only music you are legally allowed to use.
