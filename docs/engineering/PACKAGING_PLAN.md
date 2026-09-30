# Packaging plan: NULL::SET AI DJ as a desktop app (Windows, macOS, Linux)

Status: PLAN ONLY, nothing here is built yet. Written 2026-09-30 on branch `package-plan`.
Owner's words: "plan for packing in single executable application compatible with win, mac, linux".

Legend: **[measured]** = measured on the owner's Mac (M-series, arm64, Homebrew Python 3.14.7) on
2026-09-30. **[source]** = web fact with URL and retrieval date in the Sources section.
**[unverified]** = estimate or model knowledge, check before relying on it.

---

## 0. What actually runs today (read from code, not memory)

| Part | Where | What packaging must care about |
|---|---|---|
| API server | `app/ui/server.py` (FastAPI, 2742 lines), started by `start.sh` as `python -m uvicorn app.ui.server:app --port $PORT` | uvicorn's default host is 127.0.0.1 and `start.sh` passes no `--host`, so it is localhost-only today. Startup hooks: LLM boot, analysis upgrade queue, keylock cache cap, stem backfill for every library track, genre labels. |
| Console | `app/ui/static/*.js` served by FastAPI; audio plays client-side (Web Audio, one `audioWorklet`, one `new Worker("/dsp-worker.js")`, `MediaRecorder` for mix capture, `localStorage` in 8 files) | Needs a real web engine. Loads **wavesurfer.js from unpkg.com** and **Google Fonts** at runtime (`index.html:16-17`), so today it breaks offline. |
| Stems | `app/music_brain/audio/stem_service.py`: `subprocess.run([sys.executable, "-m", "demucs", ...])`; `stem_worker.py` is a long-lived `sys.executable -m app.music_brain.audio.stem_worker` with `cwd=parents[3]` (repo root) | In a frozen app `sys.executable` is the app binary, `-m` does not exist. Device: CUDA, else MPS, else CPU; MPS failure retries on CPU. Default `DEMUCS_MODEL = "htdemucs_ft"` (bag of 4), worker uses `htdemucs`. |
| Keylock | `audio/keylock.py`: `shutil.which("rubberband") or "/opt/homebrew/bin/rubberband"` | Hard-coded Homebrew path. rubberband is GPL. |
| ffmpeg | `set_learner.py`, `agent_bridge.py`, `render/graph_render.py` (`shutil.which("ffmpeg")`), audio_convert | Must ship a binary per OS. |
| Downloads | `app/ui/services/download_service.py`, `learning/set_learner.py`, `matching/sources.py` import `yt_dlp` as a module; `start.sh` sets `YTDLP_COOKIES_FILE` (`~/.config/ai-dj/youtube-cookies.txt`, chmod 600) and `YT_GUARD` | yt-dlp now needs an external JS runtime (Deno) for full YouTube support **[source]**. The code sets no JS runtime option; it works today only because `/opt/homebrew/bin/deno` is on the owner's PATH **[measured]**. |
| Decision LLM | `app/ui/services/model_runtime.py`: `LLM_BACKEND` auto/mlx/ollama. MLX spawns `sys.executable -m mlx_lm.server`; the live ear (`live_ear.py`) talks to `mlx_vlm.server` from a separate venv (`~/.venvs/mlx-vlm`, starlette conflict) on 8901 | Neither MLX server can live inside the frozen app. No backend = the app still runs (`"no LLM backend ready"`), hold loop uses DSP rules. |
| Offline AI review / genre labels | `app/music_brain/llm/claudecode.py`: `AI_REVIEW_BACKEND` local (default) or claudecode, finds `claude` via `shutil.which` | macOS GUI apps do not inherit the shell PATH **[unverified, well known]**, so `which claude` fails from a Finder launch. |
| Stores | `app/music_brain/db.py`: `CACHE_DIR/app.db` (shared knowledge, WAL) and `CACHE_DIR/user.db` (private, never exported) | Fine to relocate: everything keys off `CACHE_DIR`. |
| Knowledge | `app/music_brain/knowledge/` (6.5 MB **[measured]**: atlas shards, macros, `learned_techniques.json`, `genre_labels.json`, `names.json`), seeded into app.db by `knowledge seed`; `knowledge_sync.py` calls `git` but tolerates its absence (returns None) | Ships read-only inside the bundle. Updates arrive with app updates. |
| Read-only inputs at runtime | `DJ/` (1.5 MB, read by `matching/dj_knowledge.py`, `knowledge.py`), `research/notes` (read by `atlas/studied_combos.py`), `liked_seed.json`, `vetoes_seed.json`, `seed_combos.json` next to their modules | Must be bundled at the same relative paths, since code uses `Path(__file__).resolve().parents[3]`. |
| Writes relative to the repo | `config.py`: `ROOT_DIR = Path(__file__).resolve().parent.parent.parent`, `CACHE_DIR = AIDJ_CACHE_DIR or ROOT_DIR/"data"/"cache"`; `set_learner.py:81 SONGS_DIR = ROOT_DIR/"data"/"songs"`; `agent_bridge.py:188` writes `ROOT_DIR/"data"/"output"/"hook_drops"` | These two must move under CACHE_DIR (a signed .app bundle and `Program Files` are read-only). |
| Background jobs | `maintain.sh` / `python -m app.music_brain.maintain` (stems, flac, labels, review, atlas, export) | Offline tool. `atlas/pair_atlas.py:node_run` **requires `node`** to run `pair_atlas_rules.js`. The server only reads the atlas (`atlas_api.py` returns 409 if missing). |
| Sim | `app/sim/` (node + real API) | **Dev-only, does not ship.** |

Owner's data today **[measured]**: `data/cache` 81 GB total: stems 52 GB (823 stem folders),
keylock 15 GB, uploads 12 GB (593 files), app.db 475 MB, user.db 8.5 MB, analysis 42 MB,
sessions 173 MB, previews 205 MB. **1363 of 1612 analysis JSON files hold absolute
`/Users/sanchitdang/...` paths** and so do `app.db` and the stem `manifest.json` files
(v2 manifest stores absolute FLAC paths).

Python deps on disk **[measured]**, Homebrew site-packages, arm64: torch 590 MB (2.14.0),
llvmlite 125 MB, scipy 99 MB, sklearn 47 MB, numpy 34 MB, numba 30 MB, yt_dlp 24 MB,
librosa 4.3 MB, torchaudio 4.0 MB, demucs 0.8 MB (4.1.0). Whole site-packages 1.7 GB (includes
mlx 590 MB, which does NOT ship). Nothing packaging-related is installed yet (PyInstaller,
Nuitka, Briefcase, pywebview all "not found").

---

## 1. Shape: what "single executable" can honestly mean

With a 0.6 to 2.5 GB torch runtime inside, a literal one-file binary is the wrong goal. What the
owner can honestly get is **one downloadable thing per OS that starts with a double-click**:

| OS | Artefact | Is it "one file"? |
|---|---|---|
| macOS arm64 | `NullSet-AI-DJ.app` inside a signed, notarized `.dmg` | Yes to the user (one .app). |
| Windows x64 | `NullSet-AI-DJ-Setup.exe` (Inno Setup) plus a portable `.zip` | One installer. |
| Linux x64 | `NullSet-AI-DJ-x86_64.AppImage` | Yes, one file. |

Options compared **(sizes and times are [unverified] estimates unless tagged)**:

| Option | How it ships Python + torch | Size | Cold start | Torch fit | AV false positives (Win) | Maintenance |
|---|---|---|---|---|---|---|
| **PyInstaller onedir** (recommended) | Frozen interpreter + folder of libs | ~1.2 to 1.5 GB unpacked CPU build | 2 to 5 s | Good, mature hooks for torch/numba/librosa | Lower than onefile: nothing is unpacked at runtime **[source]** | Spec file + hooks; one build per OS |
| PyInstaller onefile | Same, self-extracting to a temp dir on every launch | Same download | 15 to 40 s per launch (unpacks ~1 GB) | Good | High: extraction behaviour trips heuristics **[source]** | Same |
| Nuitka standalone | C-compiled Python, folder | Similar | Similar to onedir | Works but torch compile is slow (tens of minutes) and less trodden | Moderate | Heavier CI time |
| Briefcase | Proper platform installers, signing built in | Similar | Similar | Uses pip wheels, torch fine in principle, fewer real-world torch reports | Signed MSI helps | Good tooling, extra layout rules |
| PyApp / uv bootstrap | Tiny launcher, installs Python + locked wheels into the user dir on first run | ~20 MB download, then ~1 GB fetched from PyPI | First run minutes, then fast | Best: can pick the CUDA or CPU torch wheel at first run | Low | Needs network on first run, PyPI outage = no install |
| Tauri or Electron shell + Python sidecar | Web shell in Rust/Node, Python frozen by PyInstaller as `externalBin` sidecar **[source]** | Sidecar size + 10 MB (Tauri) or +100 MB (Electron) | Sidecar start + shell | Same as PyInstaller | Same as the sidecar | Two toolchains (Rust or Node + PyInstaller). Only pays off if we want Tauri's updater/installer. |

**Recommendation: PyInstaller onedir + pywebview window, wrapped per OS (DMG / Inno Setup / AppImage).**
Reasons: (1) this codebase is pure Python + static JS, so one toolchain covers it; (2) onedir
avoids the per-launch unpack and most Windows AV heuristics; (3) pywebview gives a native window
on the same engines Tauri would use, without a Rust toolchain; (4) torch, numba and librosa have
PyInstaller hooks. Keep the uv bootstrap as the answer for the CUDA build (section 3).

---

## 2. Window

**Native window via pywebview**, with `--browser` as a fallback flag that opens the default
browser on the same localhost URL (identical code path, just no window).

- Engines: WebView2 on Windows (Chromium; preinstalled on Windows 11, installer bundles the
  Evergreen bootstrapper for Windows 10 **[unverified]**), WKWebView on macOS, WebKitGTK on Linux.
- Web Audio + AudioWorklet: supported in Chromium and in WebKit since Safari 14.1
  **[unverified]**; on Linux WebKitGTK routes WebAudio and MediaRecorder through GStreamer, and
  WebM output from MediaRecorder needs GStreamer 1.24.9+ **[source]**. The AppImage must
  therefore depend on the system `libwebkit2gtk-4.1` + GStreamer good/bad plugins; bundling WebKitGTK
  is not realistic. If the webview fails to start on Linux, fall back to browser mode automatically.
- **MediaRecorder MIME on WebKit (known risk):** `performance.js:pickMimeType()` tries only
  webm/ogg; WKWebView likely yields `audio/mp4` **[unverified]**, yet `performance.js:261` always
  names the download `.webm` and the server's `_BLOB_SUFFIXES` has no `.mp4`. Phase 1 must test a
  recording on macOS and add `.m4a`/`.mp4` handling if needed.
- **localStorage persistence:** pywebview's `start()` defaults to `private_mode=True`, which does
  not keep localStorage between sessions **[source]**. Launch with
  `webview.start(private_mode=False, storage_path=<user data>/webview)`.
- **Port:** localStorage is per origin, so the port must be stable across launches. Prefer 8000
  (today's default); if busy, pick a free port once and remember it in `<user data>/settings.json`.
- **Single-instance lock:** an OS file lock on `<user data>/app.lock` (`fcntl` / `msvcrt`); a second
  launch focuses the existing window (or, in browser mode, opens the URL) and exits. This also
  protects the WAL DBs and `maintain` from two writers.
- **Bind 127.0.0.1 explicitly** (`uvicorn.Config(host="127.0.0.1")`), never 0.0.0.0.
- Vendor wavesurfer.js (+ regions plugin) and the fonts into `app/ui/static/vendor/` so the app
  works offline and the page loads nothing from third-party hosts.

---

## 3. Heavy dependencies

**torch + demucs**
- macOS arm64: the standard PyPI torch wheel (MPS built in). Measured 590 MB unpacked. Intel Macs:
  PyTorch stopped publishing macOS x86_64 wheels after 2.2 **[unverified]**, so ship arm64 only.
- Windows / Linux: ship the **CPU-only** wheel from `https://download.pytorch.org/whl/cpu` **[source]**
  in the main artefact (roughly 200 to 300 MB unpacked on Linux **[unverified]**).
- CUDA: offer it as an **optional "GPU pack"**, not in the main download. Two ways, pick in Phase 5:
  (a) a second artefact `...-cuda` (adds ~2 to 3 GB **[unverified]**), or (b) a uv-managed side
  environment in `<user data>/gpu-env` holding the CUDA torch wheel, which the launcher prepends to
  `sys.path` when `torch.cuda.is_available()` would be true. (a) is simpler and reproducible; start there.
- Model weights: bundle **`htdemucs`** (single model, ~84 MB **[source]**, what the warm worker
  uses) so separation works offline on first run. Download `htdemucs_ft` (four models,
  ~4 x 80 MB **[unverified]**) on first use into `<user data>/models/torch`, verified by the SHA-256
  prefix Demucs embeds in each checkpoint filename (for example the `f7e0c4bc`, `04573f0d` parts
  of htdemucs_ft **[source]**). Set `TORCH_HOME=<user data>/models/torch` in the launcher so nothing
  lands in `~/.cache/torch`.

**Frozen subprocesses (blocking issue, needs a small code change):** `stem_service.py`,
`stem_worker.py` and `model_runtime.py` run `sys.executable -m <module>`. Add a launcher dispatch:
when the frozen binary is started as `<app> -m demucs ...` or `<app> -m app.music_brain.audio.stem_worker`,
it calls `runpy.run_module(name, run_name="__main__", alter_sys=True)` and exits, before any
GUI or server code runs. Also replace `cwd=str(Path(__file__).resolve().parents[3])` with a
`config.APP_ROOT` that equals `sys._MEIPASS` when frozen.

**ffmpeg**: ship a per-OS binary in `<bundle>/bin`, invoked as a separate process (it is never
linked). Use an **LGPL build** (no `--enable-gpl`, no `--enable-nonfree`) so no libx264 etc.
**[source]**; ship its license text and a source offer / link to the exact source tarball.
The app only decodes and encodes MP3/FLAC/WAV, so an LGPL build with LAME is enough
**[unverified: check which encoders the builds include]**.

**rubberband**: GPL-2.0-or-later, commercial licence available **[source]**. The repo is MIT. Shipping
the GPL CLI as a separate program next to an MIT app is "mere aggregation" in common reading
**[unverified, not legal advice]**; ship its license + source link. Replace the hard-coded
`/opt/homebrew/bin/rubberband` with a lookup in `<bundle>/bin` first. Keylock stays optional:
missing binary = keylock disabled, not a crash.

**yt-dlp** (Unlicense): keep the Python module in the bundle, but YouTube changes break it every
few weeks and a frozen app cannot `pip install -U`. Plan: on start (at most daily, owner can turn
it off) download the official `yt-dlp` zipapp release from GitHub, verify it against the release's
`SHA2-256SUMS`, store it in `<user data>/yt-dlp/yt-dlp.zip` and put it first on `sys.path` so
`import yt_dlp` picks it up **[unverified: zipapp importability must be spiked]**.
Also ship **Deno** in `<bundle>/bin` (yt-dlp enables only Deno by default **[source]**), plus the
`yt-dlp-ejs` component that the `yt-dlp[default]` extra brings in **[source]**.

---

## 4. Data location

Per-OS user data dir via `platformdirs.user_data_dir("NullSet AI DJ", appauthor=False)`:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/NullSet AI DJ` |
| Windows | `%APPDATA%\NullSet AI DJ` (roaming; use `user_data_dir(..., roaming=False)` → `%LOCALAPPDATA%` for the 50+ GB of stems) |
| Linux | `$XDG_DATA_HOME/NullSet AI DJ` (default `~/.local/share/...`) |

Layout: `<data>/cache` (becomes `AIDJ_CACHE_DIR`, same tree as today's `data/cache`), `<data>/models`,
`<data>/bin-overlay` (updated yt-dlp), `<data>/settings.env` (replaces `.env`), `<data>/logs`.
The launcher sets `AIDJ_CACHE_DIR` **before** importing `app.music_brain.config`, which already
honours it. The install dir is read-only.

Code changes (Phase 2):
1. `set_learner.py:81` SONGS_DIR and `agent_bridge.py:188` output dir → under `CACHE_DIR`.
2. New `config.APP_ROOT` (bundle root when frozen, repo root otherwise) for `DJ/`, `research/notes`,
   `knowledge/`, static files, seed JSONs.
3. **Stop storing absolute paths**: store paths relative to `CACHE_DIR` in stem manifests
   (`audio_io.write_manifest`), analysis JSON and DB rows, and resolve on read. Readers must keep
   accepting old absolute paths (resolve them if they exist, else rebase them, see migration).

**Migration of the owner's current `data/cache` (81 GB):** a one-shot
`python -m app.music_brain.migrate_cache --from <repo>/data/cache --to <user data>/cache` that
(1) moves (same volume: `os.rename`, instant) or copies, (2) rewrites the old prefix
`/Users/sanchitdang/dev/AI-DJ-Mixing-System/data/cache` to relative form in every `manifest.json`,
analysis JSON and the affected DB columns inside one transaction per DB, (3) runs
`PRAGMA integrity_check`, (4) writes a report. Dry run by default. Alternative with zero risk: the
owner keeps using the repo checkout and points the packaged app at it with
`AIDJ_CACHE_DIR=<repo>/data/cache` in `settings.env`.

---

## 5. Optional parts: what happens when each is missing

| Part | Detection | Missing → |
|---|---|---|
| Decision LLM | `LLM_BACKEND`: probe Ollama at `OLLAMA_URL`; on Apple Silicon probe an already-running `mlx_lm.server` on `MLX_PORT` (attach only, never spawn from the frozen app) | Autopilot and hold loop use the rule paths that already exist. Settings page shows "no model". |
| Bundled local LLM (later) | Optional download of a `llama.cpp` `llama-server` binary + one GGUF (checksum-verified) into `<data>/models`, run on 127.0.0.1 with an OpenAI-compatible API, which `autopilot_service` already speaks | Same as above |
| Live ear (Qwen-Omni via mlx-vlm) | `OMNI_BASE_URL` answers | DSP watchdog `rule_decision()` answers alone (already the designed fallback). Not shipped in the app. |
| Claude Code backend | `claude` found on PATH; the launcher first extends PATH with `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin` (macOS GUI launches get a minimal PATH) | `AI_REVIEW_BACKEND` stays `local`. An explicit claudecode request that cannot be served already errors instead of falling back silently. |
| Knowledge sync | Bundled `knowledge/`, stamp compare in `knowledge_sync.py` | `git` absent is already tolerated. |
| Pair atlas build | needs `node` | Server reads the shipped atlas shards; "rebuild atlas" is hidden unless node (or bundled Deno running the rules file, to be tested) is present. |
| maintain jobs | same module, run from a menu item as a background process of the frozen binary (`<app> -m app.music_brain.maintain`) | v1: not exposed; the startup stem backfill already covers the main job. |

---

## 6. Build and release

**CI matrix (GitHub Actions)**: `macos-14` (arm64), `windows-2022` (x64), `ubuntu-22.04` (x64,
older glibc so the AppImage runs on more distros). Intel macOS is dropped (see section 3).
Build Python pinned (3.12 recommended over the owner's 3.14 for wheel availability **[unverified]**).

**Reproducible deps**: a new `requirements-desktop.txt` locked with hashes (`uv pip compile --generate-hashes`),
one per OS/accelerator (`-macos-arm64`, `-win-cpu`, `-linux-cpu`, later `-cuda`). Excludes
`tensorflow`, `matplotlib`, `mlx*`, `pytest`, `openai` stays (used as the HTTP client for local LLMs).
Check with an import trace whether `sklearn` and `numba`-heavy paths are needed; each dropped
package saves 30 to 125 MB.

Launcher sketch (`app/desktop/launcher.py`, Phase 1):

```python
import os, runpy, sys
from pathlib import Path

def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "-m":           # frozen `sys.executable -m X` children
        mod = sys.argv[2]; sys.argv = [mod] + sys.argv[3:]
        runpy.run_module(mod, run_name="__main__", alter_sys=True); return
    from platformdirs import user_data_dir
    data = Path(user_data_dir("NullSet AI DJ", appauthor=False, roaming=False))
    os.environ.setdefault("AIDJ_CACHE_DIR", str(data / "cache"))
    os.environ.setdefault("TORCH_HOME", str(data / "models" / "torch"))
    bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    extra = [bundle / "bin", Path.home() / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    os.environ["PATH"] = os.pathsep.join([*map(str, extra), os.environ.get("PATH", "")])
    # single-instance lock, settings.env, port choice, uvicorn thread on 127.0.0.1,
    # then webview.start(private_mode=False, storage_path=str(data / "webview"))
```

PyInstaller spec sketch (`packaging/nullset.spec`):

```python
from PyInstaller.utils.hooks import collect_data_files, collect_submodules
datas = [("app/ui/static", "app/ui/static"), ("DJ", "DJ"), ("research/notes", "research/notes"),
         ("app/music_brain/knowledge", "app/music_brain/knowledge"),
         *collect_data_files("librosa"), *collect_data_files("demucs"), *collect_data_files("yt_dlp")]
datas += [(str(p), str(p.parent)) for p in Path("app").rglob("*_seed.json")] + [("app/music_brain/seed_combos.json", "app/music_brain")]
hidden = collect_submodules("app") + collect_submodules("uvicorn") + ["demucs.separate"]
a = Analysis(["app/desktop/launcher.py"], datas=datas, hiddenimports=hidden,
             binaries=[("packaging/bin/<os>/*", "bin")],       # ffmpeg, rubberband, deno
             excludes=["tensorflow", "matplotlib", "mlx", "mlx_lm", "mlx_vlm", "pytest", "app.sim", "app.tests"])
exe = EXE(PYZ(a.pure), a.scripts, exclude_binaries=True, name="NullSet AI DJ", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="NullSet AI DJ")
app = BUNDLE(coll, name="NullSet AI DJ.app", bundle_identifier="dev.nullset.aidj")  # macOS only
```

Build script per OS (`packaging/build.sh`, Windows twin `build.ps1`):

```bash
set -euo pipefail
uv venv .build && . .build/bin/activate
uv pip install --require-hashes -r "packaging/requirements-desktop-$TARGET.txt" pyinstaller pywebview platformdirs
python packaging/fetch_bins.py "$TARGET"          # ffmpeg / rubberband / deno, pinned URLs + sha256
python packaging/fetch_models.py htdemucs         # into packaging/models, sha256-checked
pyinstaller --noconfirm --clean packaging/nullset.spec
python packaging/smoke.py "dist/NullSet AI DJ"    # section 7
```

**Signing and notarization**
- macOS: Apple Developer Program, 99 USD / year, Developer ID Application cert, `codesign --options runtime`
  on every `.dylib`/`.so` inside the bundle (torch has many), then `xcrun notarytool submit --wait` and
  `xcrun stapler staple` **[source]**. torch/numba need the `com.apple.security.cs.allow-jit` and
  `disable-library-validation` entitlements **[unverified]**.
- Windows: Azure Artifact Signing (formerly Trusted Signing), Basic 9.99 USD / month, 5,000
  signatures; open to verified individuals in the US, Canada, EU and UK **[source]**. The owner's
  eligibility depends on country (open question). Otherwise an OV code-signing cert (hundreds of
  USD / year **[source]**). Unsigned = SmartScreen warning on every new version.
- Linux: AppImage signed with GPG (`appimagetool --sign`), checksums published. Flatpak later only
  if there is demand (its sandbox fights the 50 GB cache and subprocess binaries). `.deb` optional.

**Auto-update**: v1 = a simple version check against the GitHub Releases API over HTTPS on start (at
most daily, can be turned off), showing "new version, download" with a link. No in-place self-update
of a 1 GB bundle in v1. Later: Sparkle on macOS, WinSparkle on Windows, AppImageUpdate zsync on Linux.

**Costs**: Apple 99 USD/yr; Windows ~120 USD/yr (Azure Basic) or more for a cert; GitHub Actions
macOS minutes are billed at a higher multiplier on private repos **[unverified]**; release storage
~1.5 GB per version per OS.

---

## 7. Tests: smoke test of the built artefact on each OS

`packaging/smoke.py` runs in CI against the built folder, with `AIDJ_CACHE_DIR` in a temp dir, no network
(the network stub is an env flag that makes the yt-dlp updater and version check no-ops):

1. Start the binary in `--browser --no-open --port 0` mode; wait for `GET /api/recipes` 200 within the
   cold-start budget (**10 s** macOS/Linux, **20 s** Windows first launch because of AV scanning).
2. `POST /api/tracks` with a 30 s generated test tone + click track (made by numpy at test time, no
   real music in the repo), then `GET /api/tracks/{id}/analysis` returns a BPM.
3. `POST /api/tracks/{id}/separate` on a 10 s clip with `htdemucs` on CPU returns 4 stems (budget 120 s).
4. Two tracks: `POST /api/match` returns candidates; `POST /api/preview` returns a playable MP3 (ffmpeg works).
5. Frozen child check: `<app> -m app.music_brain.audio.stem_worker` prints `ready`.
6. Integrity: `codesign --verify --deep --strict` + `spctl -a -vv` (mac), `signtool verify /pa` (win),
   sha256 of the artefact published next to it.
7. Size budget: fail the job if the artefact grows > 15 % over the last release.
8. Manual per release (cannot be automated in CI): open the window, play two decks, record 10 s,
   check the recording plays back, check localStorage survives a restart.

---

## 8. Security

- No secrets in the bundle: the spec includes only listed paths; CI greps the built folder for `.env`,
  `sk-`, `OPENAI_API_KEY=`, `DASHSCOPE_API_KEY=` and fails the build if found. API keys live in
  `<data>/settings.env`; YouTube cookies stay outside the bundle (today's `~/.config/ai-dj/youtube-cookies.txt`, chmod 600).
- Localhost only: bind 127.0.0.1. The API has no auth today, so any web page in the user's normal browser
  could POST to `http://127.0.0.1:8000` (CSRF / DNS rebinding) and, for example, start downloads. Add:
  (1) reject requests whose `Host` is not `127.0.0.1:<port>` / `localhost:<port>`; (2) a per-launch random
  token that the launcher injects into the webview page and every `/api` call must carry (header), so
  cross-site requests fail; (3) no permissive CORS.
- yt-dlp download endpoint: only the local window can reach it (above); keep `YT_GUARD` behaviour; the
  updated zipapp is accepted only when its SHA-256 matches the signed release checksum file.
- Update channel: HTTPS to GitHub Releases only, no code download except the checksum-verified yt-dlp
  zipapp and model files with pinned hashes.
- Model downloads: pinned URLs + SHA-256, written to a temp name and renamed only after the hash matches.

---

## 9. Phased rollout

| # | Phase | What | Effort (rough) |
|---|---|---|---|
| 1 | Spike on macOS | launcher + pywebview + PyInstaller onedir of today's code with `AIDJ_CACHE_DIR` pointed at a copy of a few tracks; measure size and cold start; test AudioWorklet, Worker, MediaRecorder MIME, localStorage in WKWebView; `-m` dispatch for Demucs | 2 to 3 days |
| 2 | Make the code bundle-safe | `APP_ROOT`, SONGS_DIR/output under CACHE_DIR, rubberband lookup, PATH extension, vendored wavesurfer + fonts, relative paths in manifests / analysis / DB with old-path tolerance, Host check + token | 3 to 5 days |
| 3 | macOS release | signing, entitlements, notarization, DMG, smoke test in CI, migration command for the owner's 81 GB cache | 2 to 3 days (+ Apple enrolment wait) |
| 4 | Windows + Linux CPU | CPU torch locks, ffmpeg/rubberband/deno fetchers, Inno Setup, AppImage, WebView2 / WebKitGTK checks, CI matrix, signing | 4 to 6 days |
| 5 | Optional parts | yt-dlp zipapp updater, htdemucs_ft first-run download, Ollama / llama.cpp detection UI, CUDA artefact | 3 to 5 days |
| 6 | Updates | version check, then Sparkle / WinSparkle | 1 to 3 days |

Risks:

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Absolute paths in 1363 analysis files, DBs and stem manifests break after moving data | Certain if moved | Library looks empty / re-separates 52 GB of stems | Relative paths + migration tool + dry run; or keep `AIDJ_CACHE_DIR` on the old dir |
| Frozen `sys.executable -m` children (Demucs, stem worker) fail | Certain without the dispatch | No stems | launcher `-m` dispatch, smoke step 5 |
| yt-dlp breaks on YouTube changes / needs Deno | High, recurring | Downloads fail | bundled Deno + checksum-verified zipapp updater |
| Windows AV / SmartScreen flags the unsigned app | High unsigned | Users cannot start it | onedir, signing, submit false positives to vendors |
| WebKit differences (MediaRecorder MIME, AudioWorklet timing) on macOS/Linux | Medium | Recording or audio glitches | Phase 1 spike on WKWebView; browser-mode fallback |
| Bundle size (torch) | Certain | 0.5 to 0.8 GB download CPU, much more CUDA **[unverified]** | CPU default, drop unused deps, GPU pack separate |

---

## 10. Open questions for the owner

1. Target users: only you, or public? (Only you: skip signing on Windows/Linux, maybe skip packaging
   entirely on Mac and keep `start.sh`. Public: signing is mandatory.)
2. Is a GPU (CUDA) build required on Windows/Linux, or is CPU Demucs acceptable at first?
3. Bundle size tolerance: is a ~0.6 to 1 GB download OK, or should Demucs itself be an optional download?
4. Signing budget (~220 USD / year for Apple + Azure Basic), and are you eligible for Azure Artifact
   Signing in your country?
5. Ship Demucs in the main app, or make stem separation a download?
6. Move the 81 GB cache into the per-OS data dir, or keep pointing at the repo's `data/cache`?
7. Should the app bundle a local LLM (llama.cpp + GGUF, several GB) or only detect Ollama / MLX?
8. Should YouTube download ship at all in a public build (terms-of-service exposure)?

---

## Sources (retrieved 2026-09-30)

- PyInstaller onefile vs onedir and AV false positives: https://www.pythonguis.com/faq/problems-with-antivirus-software-and-pyinstaller/ , https://github.com/pyinstaller/pyinstaller/issues/6754
- Tauri v2 sidecar (`externalBin`, target-triple naming, PyInstaller sidecars): https://v2.tauri.app/develop/sidecar/
- pywebview `start()` defaults (`private_mode=True`, `storage_path`): https://pywebview.flowrl.com/api/
- WebKitGTK multimedia via GStreamer; MediaRecorder WebM needs GStreamer 1.24.9+: https://github.com/webkit/Documentation/blob/main/docs/Ports/WebKitGTK%20and%20WPE%20WebKit/Multimedia.md , https://blogs.igalia.com/webkit/blog/2024/wip-1/
- PyTorch CPU wheel index: https://docs.astral.sh/uv/guides/integration/pytorch/
- Demucs models, htdemucs_ft bag and model ids: https://github.com/facebookresearch/demucs , https://deepwiki.com/facebookresearch/demucs/5.1-models-and-variants ; htdemucs ~84 MB: https://huggingface.co/AEmotionStudio/htdemucs-models
- FFmpeg LGPL/GPL build checklist: https://www.ffmpeg.org/legal.html
- Rubber Band Library GPL / commercial licence: https://breakfastquay.com/rubberband/license.html
- yt-dlp external JS runtime (Deno default, yt-dlp-ejs): https://github.com/yt-dlp/yt-dlp/issues/15012 , https://github.com/yt-dlp/yt-dlp/wiki/EJS
- Apple Developer Program fee and notarization: https://developer.apple.com/help/account/membership/program-enrollment/ , https://developer.apple.com/documentation/notaryapi
- Azure Artifact Signing pricing and eligibility: https://azure.microsoft.com/en-us/pricing/details/artifact-signing/ , https://my-ssl.com/learn/azure-trusted-signing-vs-code-signing-certificate
