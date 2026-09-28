# AI DJ Mixing System

A local, browser-based AI DJ. It analyses your tracks, separates them into stems, picks the next song with a local LLM, and mixes live on two virtual decks using transition rules taken from a DJ knowledge base kept in this repository (`DJ/`).

Everything runs on your machine: FastAPI backend, a local LLM (MLX on Apple Silicon, Ollama as fallback), Demucs for stems, and a vanilla-JS console in the browser that does the actual audio playback and mixing.

## Features

### AI automix: the whole set plays as one song

The autopilot picks the transition for every pair itself, in this order of preference, and every move lands on the 8-bar phrase grid:

| Move | When | What you hear |
|---|---|---|
| **Riff over rap** | A has a groove running into its own breakdown, B raps, tempos 3-15 % apart | A key-locked to B's tempo, the first half of A's drop clean, then looped under B's rap (entering after its opening hook), rap "hold on" moments, A drops out, B slams in, 8-bar crossfade. Learned from Fred again.. & Thomas Bangalter's USB002 set (1:06:00, Aerodynamic x Victory Lap). |
| **Mashup, then transition** | B has a vocal phrase, keys agree or B raps, tempos within 25 % | A drops to its instrumental, B's vocal rides over it, hold vox, A's beat drops out for 2 bars, B's drums and bass take over on the line, 8-bar crossfade. |
| **Stem blend** | Tempos lock (up to 25 % on key-locked tempo stems) | 16 or 32 bars: B's synths first, kick and bass change hands together on one line, one singer at a time, one tonal owner when the keys clash, the crossfader sweeps smoothly across. |
| **Stem bridge** | Tempos can't lock | A's drums then bass fall away, A's voice (held) and synths carry a beatless stretch, B starts beatless at its own tempo, the tonal layers swap in one crossfade, B's beat drops on its own line. No two beats ever overlap. |
| **Echo out** | Only when stems are missing | An 8-bar echo with one continuous crossfader sweep. |

Inside a song the AI also plays with its stems:

- **Strip & rebuild** for famous songs (20M+ YouTube views, played in full): drums out, bass out, voice alone, bass back, build, drop on the line (the USB002 leavemealone move).
- **Stem remix on the go** in 16-bar sections: hold on (vocal's last bar looped), acapella, drum break, bass out, synth hold. Max 3 per song, 32 bars apart.
- **Stem holds inside mashups** and a drums-and-bass drop for a mashup's last quarter.
- **Hold loop** when the next song isn't ready: up to 32 bars, seams checked silently on the buffer first, loops over vocals play the instrumental, never a shrinking 4-bar stutter.
- **Auto sampler**: riser, snare roll, clap and open hat into drops, a riser into transitions. Own high-passed bus that follows the music's level; rationed.

### Stems everywhere

- Every track is separated into 4 stems (drums, bass, vocals, other) as soon as it is uploaded or downloaded; the rest of the library backfills in the background while the LLM is idle. One persistent Demucs worker, next song decoded while the current one separates: about 8-12 s per song.
- **Key-locked tempo stems**: any song's stems rendered at another BPM with Rubber Band (cached per song and tempo), so pairs up to 25 % apart are beatmatched at their original key.
- Decks play the stems sample-locked beside the mix; a stem move is inaudible until a layer actually changes.

### Song selection

- Local LLM (Qwen3-30B-A3B on MLX by default) with a 15 s decision budget.
- Every suggested song is checked against YouTube before download (invented titles are dropped); sequel titles ("Victory Lap Five" for "Victory Lap") and other uploads of a song already played are refused.
- Real-tempo gate after download, then a library fallback (songs you already have that lock in tempo and key) before any tempo jump.
- Remixes are welcome when they fit the vibe; a remix of a played song only after 8 other songs.
- Start a set from a seed URL, or **from the song playing now**: the songs already played this session steer the suggestions.
- Genre continuity, Camelot key rules, occasion steering, set memory across sessions.

### AI ACTIONS (on demand)

Buttons that run any move now, on the next phrase line: **AUTO MIX, AUTO MASHUP, AUTO SAMPLE, STRIP & REBUILD, STEM REMIX, HOLD VOX, VOCAL SWAP, RIFF x RAP**. Each says why when it can't go. Toggles let you switch any automatic behaviour off (SET MIND, AI ASSIST, PEAK MOVES, MASHUPS, RIFF x RAP, AUTO SAMPLER, STEM REMIX, LIVE EAR).

### Console and overlays

- Two decks: jog wheels, hot cues, beat loops and jumps, 3-band EQ, pitch and volume faders, crossfader, per-deck FX rack, sampler pads with a 16-step beat grid, mix recording.
- **Stem rail** per deck: mute or solo drums, bass, vocals, synths, each with a live mini player.
- **Stem-shaded waveforms**: each stem in its own shade of the deck colour; a muted stem dims live.
- **NULL AT WORK** panel: every AI job with a live timer (song picks, downloads, stems, planning, listening), plus background separation progress.
- Controls the AI moves glow with a tag naming the move; markers on the track overviews show the planned exit, the hold loop and vocal regions.
- **TRACK ID** strip: now playing and next, with BPM, key and view count.
- NULL, an animated mascot that reacts to what the AI is doing.
- Master brick-wall limiter; the heavy DSP runs in a web worker.

### Analysis and listening

- Tempo, beat grid, downbeats, 8/16-bar phrases, Camelot key, sections, energy curve, vocal regions. The BPM is the tempo at which an 8-bar loop actually repeats, not a rounded tempogram bin.
- **Live ear** (optional): Qwen3-Omni listens to hold loops through mlx-vlm; a DSP watchdog measures seams, clipping and low-end clash, and rules answer when the model is off.
- **Technique library** (`app/music_brain/techniques.py`): every learned move with the conditions it needs; `GET /api/techniques?a=&b=` explains which fit a pair and why.

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
| `YTDLP_COOKIES_FILE` | `~/.config/ai-dj/youtube-cookies.txt` | YouTube cookies.txt for yt-dlp bot checks (used only after a refusal; set by `start.sh` when the file exists; keep it out of the repo) |
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

# Learn from a real DJ set: ffmpeg clips +-96 s around every tracklist boundary,
# Demucs on the clips and the source songs (2 at a time), per-stem song matching,
# synced lyrics. Learned moves merge into data/cache/learned_techniques.json and
# show up in GET /api/techniques as learned:<kind>.
python -m app.music_brain.agent_bridge learn-set "https://www.youtube.com/watch?v=mDtud5fLgFQ" \
    --tracklist research/notes/tracklists/mDtud5fLgFQ.txt --jobs 2
python -m app.music_brain.agent_bridge learned
python -m app.music_brain.agent_bridge learn-feedback acapella_over "rap ~9 dB under the riff"
python -m app.music_brain.agent_bridge learn-feedback vocal_loop --disable
```

`learn-set` learns: `bass_swap`, `stem_intro` (B's drums/top before its bass), `acapella_over`,
`hard_cut`, `loop_extend`, `vocal_resequence` (lines played out of order into a new lyric, with
the words), `vocal_loop`, and `acapella_drop` (beat out under a sung line, drop back in, with the
words and whether it was the hook). Songs not in `data/songs/` are fetched by search into
`data/cache/sets/<id>/songs/`; each is checked against the set and flagged `likely_wrong_song`
when it is never heard. Your `learn-feedback` rules are kept across re-learning and shown in the
technique's reasons.

Failures print `{"error": "..."}` and exit non-zero.

## REST API

The main endpoints in `app/ui/server.py`:

| Area | Endpoints |
|---|---|
| LLM | `GET /api/llm/status` |
| Tracks | `POST/GET /api/tracks`, `GET /api/tracks/{id}/analysis`, `GET /api/tracks/{id}/fame`, `GET /api/tracks/{id}/hook-drops`, `GET /api/audio/tracks/{id}` |
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
