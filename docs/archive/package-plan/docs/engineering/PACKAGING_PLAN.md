# Packaging plan: NULL::SET AI DJ as one container image (Windows, macOS, Linux hosts)

Status: PLAN + DRAFT FILES, nothing built or published. Branch `package-plan`, 2026-09-30.
Owner's words: "it needs to be single container with backend and frontend running".
Draft files on this branch: `Dockerfile`, `.dockerignore`, `docker-compose.yml` (profiles `cpu`, `gpu`, `llm`).

Legend: **[measured]** = measured on the owner's Mac (M-series, arm64, Python 3.14.7) or in this repo on
2026-09-30. **[source]** = web fact, URL in Sources (all retrieved 2026-09-30). **[unverified]** = estimate
or model knowledge, check before relying on it. No image was built: Docker CLI + Compose 5.5.1 are
installed, but the Colima daemon was not running and buildx is missing, so the size and `analyze`
numbers below are estimates.

---

## 0. What runs today (from code) and what that means for a container

| Part | Today | In the container |
|---|---|---|
| API + console | `python -m uvicorn app.ui.server:app` (`start.sh`), FastAPI serves `app/ui/static` itself | **One process** is enough: uvicorn serves API and frontend on one port. |
| Stems | `stem_service.py` runs `sys.executable -m demucs`; `stem_worker.py` runs `sys.executable -m app.music_brain.audio.stem_worker` with `cwd=parents[3]` | Works unchanged: in an image `sys.executable` is the venv Python, `cwd` resolves to `/app`. Children are reaped by `tini`. |
| Device pick | CUDA, else MPS, else CPU (`stem_service._detect_device`) | CUDA tag: CUDA. CPU tag: CPU. Never MPS (no Metal in Linux VMs). |
| ffmpeg, rubberband | `shutil.which`, rubberband falls back to `/opt/homebrew/bin/rubberband` | Debian `ffmpeg`, `rubberband-cli` on PATH, so `which` finds them. |
| yt-dlp | imported as a module; YouTube now needs a JS runtime, Deno is the default one **[source]**; works on the Mac only because `/opt/homebrew/bin/deno` is on PATH **[measured]** | Deno copied in from `denoland/deno:bin-2.9.7` **[source]**, plus `yt-dlp-ejs` 0.8.0 (the Mac's version **[measured]**). |
| Decision LLM | `model_runtime.py`: `LLM_BACKEND` auto / mlx / ollama; MLX spawns `mlx_lm.server`; Ollama probed via `/api/generate` | `LLM_BACKEND=ollama` default. MLX cannot run in the container. Ollama on the host (`host.docker.internal`) or the optional `ollama` compose service. None reachable = the rule paths, as today. |
| Live ear | `mlx_vlm.server` in its own venv on 8901 | Not in the image. The DSP watchdog `rule_decision()` is the designed fallback. |
| Claude Code review | `claude` CLI on PATH, `AI_REVIEW_BACKEND=claudecode` | **Off** (`AI_REVIEW_BACKEND=local`), see section 5. |
| Stores | `CACHE_DIR/app.db`, `user.db` (WAL), `CACHE_DIR = AIDJ_CACHE_DIR or <repo>/data/cache` | `AIDJ_CACHE_DIR=/data/cache` on the volume. |
| Writes under the repo | `set_learner.py:81` `SONGS_DIR = ROOT_DIR/"data"/"songs"`, `agent_bridge.py:188` `ROOT_DIR/"data"/"output"/"hook_drops"` | The Dockerfile links `/app/data -> /data`, so both land on the volume with no code change. |
| maintain jobs | `./maintain.sh` = `python -m app.music_brain.maintain`; `stems` / `flac` refuse while the server answers on `127.0.0.1:$PORT`; `atlas` needs `node` | Same image, run on demand (section 1.4). `node` is not in the image, so `atlas` rebuild stays a dev-machine job. |
| Sim | `app/sim` needs node | Dev-only, does not ship. Its `.py` files stay because `agent_bridge.py` imports `app.sim.stem_capture`; `fixtures/` and `out/` are excluded. |

---

## 1. Image

### 1.1 Layout (the draft `Dockerfile`)

- Base `python:3.14-slim-trixie` (Debian 13; Python 3.14 = what CI and the Mac run, `requirements-ci.txt`
  pins are the 3.14 set). The old `requirements.txt` pins (numpy 1.26, scipy 1.10) do not install on 3.14.
- Stage `build`: venv at `/opt/venv`; torch 2.14.0 first from `TORCH_INDEX` (CPU or `cu130`), then
  `requirements-ci.txt` + `demucs==4.1.0` + `yt-dlp-ejs==0.8.0` (Mac versions **[measured]**).
  torchaudio and sklearn are not imported by the app **[measured, grep]**, so they are left out.
- Stage `runtime`: apt `ffmpeg rubberband-cli tini ca-certificates`, Deno binary, the venv, the code.
- Non-root user `aidj` (uid 1000), `/data` owned by it, code owned by root (read-only to the app).
- `HEALTHCHECK` = GET `/api/recipes` (what `start.sh` polls; there is no `/api/health`).
- `CMD uvicorn app.ui.server:app --host 0.0.0.0 --port 8000`, `ENTRYPOINT tini`.
- Env defaults: `AIDJ_CACHE_DIR=/data/cache`, `HF_HOME=/data/models/hf`, `TORCH_HOME=/data/models/torch`,
  `LLM_BACKEND=ollama`, `OLLAMA_URL=http://host.docker.internal:11434`, `AI_REVIEW_BACKEND=local`.
- In the image: `app/` (minus tests, sim fixtures/output), `DJ/`, `research/notes` (tracked `.md`/`.py`
  only), `app/music_brain/knowledge/` (6.5 MB **[measured]**), seed JSONs. Out (`.dockerignore`): `data/`,
  `.env*`, `.git`, `.claude`, venvs, docs, e2e, all audio files, cookies.

### 1.2 Demucs weights

demucs 4.1.0 depends on `huggingface-hub` + `safetensors` **[measured, package metadata]**; `htdemucs` is
~84 MB, `htdemucs_ft` is a bag of 4 **[source]**. Plan: **first-run download onto the volume**
(`HF_HOME` / `TORCH_HOME` under `/data/models`), so the image stays small and one download serves every
tag. Option for offline hosts: a `bake-models` build target that runs one separation of a generated
second of silence at build time into `/opt/models` and points `HF_HOME` there **[unverified: demucs
4.1's exact cache location must be checked in the first build]**.

### 1.3 Tags and sizes (all **[unverified]**, no build ran)

| Tag | Platforms | torch | Uncompressed | Pull (compressed) |
|---|---|---|---|---|
| `cpu`, `X.Y.Z-cpu` | linux/amd64, linux/arm64 | CPU wheel | ~2 to 2.5 GB | ~0.8 to 1.1 GB |
| `cuda`, `X.Y.Z-cuda` | linux/amd64 | `cu130` wheel + NVIDIA pip libs | ~7 to 9 GB | ~3.5 to 4.5 GB |

Known parts: on the Mac torch is 590 MB, llvmlite 125 MB, scipy 99 MB, numpy 34 MB, numba 30 MB, yt_dlp
24 MB **[measured]**. Debian ffmpeg pulls a large codec stack (~300 MB **[unverified]**). The CUDA wheels
bundle their own CUDA runtime, so no `nvidia/cuda` base image is needed; the host only needs the driver.

### 1.4 One process; everything else is optional and outside it

- The app container runs **only uvicorn**. The startup hooks already present (LLM probe, analysis upgrade
  queue, keylock cap, stem backfill, genre labels) run in-process as today.
- Decision LLM: a **separate optional container** (`ollama` profile), or Ollama / MLX natively on the host.
  llama.cpp's server is not a drop-in: `model_runtime._start_ollama` probes Ollama's `/api/generate`.
- maintain: the same image, on demand, never a second long-running service:
  - light steps with the server running: `docker compose exec aidj python -m app.music_brain.maintain --steps labels,review,export`
    (`exec` shares the server's network namespace, so the "server is up" guard still works);
  - `stems` / `flac`: stop the server first (a separate `run` container has its own 127.0.0.1 and **would not
    see the running server**, so the guard would not protect the DBs):
    `docker compose stop aidj && docker compose run --rm aidj python -m app.music_brain.maintain && docker compose start aidj`.
  - `export` writes into `app/music_brain/knowledge/`, which is read-only in the image: export stays a
    dev-checkout job.

---

## 2. Host GPU per OS

| Host | Runtime | Demucs device | Notes |
|---|---|---|---|
| Linux + NVIDIA | Docker Engine + NVIDIA Container Toolkit | **CUDA** (`cuda` tag, `--profile gpu`) | Needs the host driver only. |
| Windows + NVIDIA | Docker Desktop, WSL2 backend | **CUDA** via GPU paravirtualisation **[source]** | Docker Desktop GPU support is Windows + WSL2 only **[source]**; check with `docker run --rm --gpus=all nvcr.io/nvidia/k8s/cuda-sample:nbody nbody -gpu -benchmark`. |
| Windows, no NVIDIA / AMD / Intel GPU | Docker Desktop | CPU | |
| macOS (Apple silicon) | Docker Desktop, Colima, OrbStack: all a Linux VM on Hypervisor.framework | **CPU only**, no MPS / Metal passthrough **[source]** | Use the `cpu` tag, it is native arm64 (no emulation). |

**Demucs on CPU (macOS host):** measured on this Mac, `htdemucs` 2-stem on a 4 min song took 21 s on MPS vs
49 s on CPU natively (comment in `stem_service.py`, M-series, torch 2.14 / demucs 4.1 **[measured by the
owner]**). Inside a VM with the default share of cores expect roughly 1.5 to 3x the native CPU time: about
1 to 2.5 min for a 4-stem `htdemucs` pass and 5 to 10 min for `htdemucs_ft` per 4 min song **[unverified]**.
The library already has 823 stem sets **[measured]**, so only new songs pay this.

**Option: Demucs natively on the Mac host.** Because stem sets are content-hash keyed, a native
`python -m app.music_brain.maintain --steps stems` run on the Mac (MPS) against the same bind-mounted data
folder fills the cache and the container reuses it. This needs the relative-path change (section 4), because
a manifest written natively holds `/Users/...` paths and one written in the container holds `/data/...`.
A live "remote separation" helper (the container asking a host process over HTTP) would be new code; not
planned for v1.

---

## 3. Data

- **One volume at `/data`**: `/data/cache` (= `AIDJ_CACHE_DIR`, the same tree as today's `data/cache`),
  `/data/models`, `/data/songs`, `/data/output`. Default a **named volume** (`aidj-data`); `AIDJ_DATA=./data`
  bind-mounts the repo's `data/` folder instead.
- Size today: `data/cache` 81 GB, stems 52 GB (823 folders), keylock 15 GB, uploads 12 GB, app.db 475 MB **[measured]**.

**Absolute paths (blocking for moving data)** **[measured on the owner's cache]**:

| Where | Count | Effect in the container |
|---|---|---|
| `analysis/*.json` field `path` | 1363 of 1612 files | points at `/Users/me/...`, not found |
| `stems/*/manifest.json` (v2 stores absolute FLAC paths) | every stem set | `read_manifest` returns None when a path is missing, so **all 823 sets count as cache misses and are separated again** |
| `user.db` `song_steps.data` | 667 rows | stale paths inside JSON |
| `app.db` `atlas_meta.value` | 1 row | stale path |
| `sets/**/study.json`, `set_import` track paths | per the deployment plan | stale |

Fix in two steps:
1. **Now, zero code:** also bind the host folder at its old absolute path, so every old path resolves:
   `- /Users/me/dev/AI-DJ-Mixing-System/data:/Users/me/dev/AI-DJ-Mixing-System/data` next to
   the `/data` mount (both the same host folder). New files are still written as `/data/...`.
2. **Proper:** store paths relative to `CACHE_DIR` (manifests, analysis JSON, the two DB columns) and resolve
   on read, accepting old absolute paths; plus a one-shot `python -m app.music_brain.migrate_paths --old
   <prefix> [--apply]` that rewrites the JSON files and the DB rows inside one transaction per DB, dry run by
   default, then runs `PRAGMA integrity_check`. The deployment plan's `rewrite_paths.py` covers manifests and
   study files only; it misses analysis JSON and the two DB columns.

**Bind mount vs named volume per host:**

| Host | Recommendation |
|---|---|
| Linux | Bind mount is native speed. Match ownership: the image runs as uid 1000, so `chown -R 1000:1000` the folder or set `user: "<uid>:<gid>"` in compose. |
| macOS | Bind mounts go through VirtioFS (slower on many small files **[unverified]**). For 81 GB of mostly large FLAC files a bind mount is acceptable and keeps the data visible to native tools; a named volume is faster but lives inside the VM disk image. |
| Windows | Keep the data **inside the WSL2 filesystem** (`\\wsl$\...`) or in a named volume; bind-mounting from `C:\` (`/mnt/c`) goes through a Plan9 share and is slow **[source]**. |

---

## 4. Code changes the image needs (listed, not implemented)

1. **None needed for `sys.executable -m`** children: correct in a container (confirmed by reading
   `stem_service.py`, `stem_worker.py`; `model_runtime.py`'s MLX spawn is skipped with `LLM_BACKEND=ollama`).
2. Vendor wavesurfer.js + regions plugin (`index.html:16-17` loads them from unpkg.com) and the Google Fonts
   into `app/ui/static/vendor/`, so the console works offline and loads nothing from third parties.
3. Bind address: `0.0.0.0` inside the container is set by the image `CMD`; no code change. The host side
   stays `127.0.0.1` (section 5).
4. Config via env only: already true (`os.environ` everywhere, `load_dotenv()` in `server.py`). Add an
   `/api/health` route (cheap, no model calls) and point the healthcheck at it.
5. Relative stem / analysis / DB paths + the migration command (section 3).
6. `keylock.py`: drop the `/opt/homebrew/bin/rubberband` fallback or keep it after `which`; harmless in Linux.
7. `SONGS_DIR` / `hook_drops` under `CACHE_DIR` (the `/app/data` symlink covers it for now).
8. MediaRecorder capture is in the user's browser, not the container: no change.

---

## 5. Security

- **The API has no auth, no CORS middleware**; write routes include `POST /api/download`,
  `POST /api/download/jobs` (start yt-dlp) and `DELETE /api/liked`. A published port exposes all of that.
- Default publish **`127.0.0.1:8000:8000`** only (the draft compose does this). Publishing `0.0.0.0` or
  `8000:8000` exposes it to the LAN. Also note Docker on Linux writes its own iptables rules, so a host
  firewall like ufw does not block a published port **[unverified: well known Docker behaviour]**.
- Before any non-localhost use (the Proxmox box): (1) reject requests whose `Host` is not an allowed name,
  (2) a shared token (cookie set by a login page, or a header) checked by middleware on every `/api` route,
  (3) no permissive CORS; or put it behind a reverse proxy with auth (Tailscale / Caddy basic auth).
- **No secrets in the image**: `.dockerignore` drops `.env*`, cookies, keys; settings come from an optional
  `aidj.env` (gitignored) via `env_file`. CI greps the image filesystem for `sk-`, `OPENAI_API_KEY=`,
  `DASHSCOPE_API_KEY=` and fails on a hit.
- **Claude CLI backend: off in the image.** It would need the owner's personal Claude login inside the
  container (mounting `~/.claude`), which the image must never contain. If wanted, run the AI review natively
  on the host against the same data folder.
- YouTube cookies (`YTDLP_COOKIES_FILE`): mount read-only from the host (`~/.config/ai-dj/youtube-cookies.txt:/run/secrets/yt-cookies:ro`)
  and set the env var; never copy into the image.
- Licences: Debian's `ffmpeg` is built with GPL parts **[unverified]**, rubberband is GPL-2.0-or-later
  **[source]**; publishing the image redistributes them. Debian's source packages satisfy the source offer;
  add a `THIRD_PARTY.md` listing them. For a private GHCR image used only by the owner this does not arise.

---

## 6. Fit with the home-server plan (`docs/engineering/DEPLOYMENT_PLAN.md`, branch `deploy-plan`, not on main)

That plan picks an unprivileged Proxmox LXC with the host NVIDIA driver and a venv + systemd, "not a container
image" because none existed. With this image:

- **Runs there unchanged** as the `cuda` tag, either (a) Docker inside the LXC (`features: nesting=1`, NVIDIA
  Container Toolkit inside the CT; in an unprivileged CT the toolkit usually needs `no-cgroups = true`
  **[unverified]**), or (b) a small VM with VFIO passthrough (documented on the PVE wiki, costs locked RAM).
  (a) keeps the RAM argument of that plan.
- Section 4 (venv, apt list, pip commands) and the `aidj.service` unit are replaced by
  `docker compose --profile gpu up -d` with `restart: unless-stopped`. The llama.cpp unit stays a host / CT
  service or becomes the `ollama` container (the app speaks Ollama natively, see 1.4).
- Section 3 storage: bind the HDD / SSD datasets into the container under `/data/cache/...` the same way.
- Section 5: publish on the Tailscale / LAN address only together with the auth from section 5 here.
- Section 7: its table says "app.db, user.db: no absolute path"; measured on 2026-09-30 that is wrong
  (`user.db song_steps.data` 667 rows, `app.db atlas_meta.value` 1 row). Its `rewrite_paths.py` also skips
  `analysis/*.json`. Use the migration in section 3 here.

---

## 7. CI

GitHub Actions workflow `.github/workflows/image.yml` (to write in Phase 2):

```yaml
on: { push: { tags: ["v*"] }, workflow_dispatch: {} }
jobs:
  image:
    runs-on: ubuntu-24.04
    permissions: { contents: read, packages: write }
    strategy: { matrix: { flavour: [cpu, cuda] } }
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-qemu-action@v3
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with: { registry: ghcr.io, username: "${{ github.actor }}", password: "${{ secrets.GITHUB_TOKEN }}" }
      - uses: docker/build-push-action@v6
        with:
          platforms: ${{ matrix.flavour == 'cpu' && 'linux/amd64,linux/arm64' || 'linux/amd64' }}
          build-args: TORCH_INDEX=https://download.pytorch.org/whl/${{ matrix.flavour == 'cpu' && 'cpu' || 'cu130' }}
          tags: ghcr.io/${{ github.repository }}:${{ matrix.flavour }},ghcr.io/${{ github.repository }}:${{ github.ref_name }}-${{ matrix.flavour }}
          cache-from: type=gha,scope=${{ matrix.flavour }}
          cache-to: type=gha,mode=max,scope=${{ matrix.flavour }}
          push: true
```

arm64 under QEMU emulation is slow for the pip stage; a native `ubuntu-24.04-arm` runner is the better
choice once available to the repo **[unverified]**. The CUDA image may hit the runner's disk limit; free
space first (remove preinstalled SDKs) **[unverified]**.

**Smoke test** (job after the build, amd64 `cpu` image, no network except the one Demucs weight download,
which is cached as a CI artefact): start the container with a temp volume; wait for the healthcheck; generate
a 30 s click track + tone with numpy (no real music in the repo); `POST /api/tracks`,
`GET /api/tracks/{id}/analysis` (has a BPM), `POST /api/tracks/{id}/separate` on a 10 s clip (4 stems,
budget 180 s CPU), second track, `POST /api/match`, `POST /api/preview` (MP3 plays = ffmpeg works); assert
the process runs as uid 1000 and no port listens outside the container. Image size budget: fail on > 15 %
growth over the last tag.

---

## 8. Run commands per host

```bash
# macOS (Docker Desktop, Colima or OrbStack) - CPU; decision LLM = native Ollama on the Mac
ollama serve &                                   # optional, Metal-accelerated on the host
docker compose --profile cpu up -d               # http://127.0.0.1:8000
# owner's existing library, with old absolute paths still resolving (section 3, step 1):
AIDJ_DATA=./data docker compose --profile cpu up -d   # plus the extra /Users/... bind in an override file

# Linux + NVIDIA (Docker Engine + NVIDIA Container Toolkit)
docker compose --profile gpu --profile llm up -d  # set OLLAMA_URL=http://ollama:11434 in aidj.env

# Linux without NVIDIA
docker compose --profile cpu up -d

# Windows 11 (PowerShell, Docker Desktop with the WSL2 backend); keep AIDJ_DATA inside \\wsl$ or use the named volume
docker compose --profile gpu up -d               # NVIDIA GPU
docker compose --profile cpu up -d               # otherwise

# plain docker, any host
docker run -d --name aidj -p 127.0.0.1:8000:8000 -v aidj-data:/data ghcr.io/OWNER/null-set-ai-dj:cpu
```

---

## 9. Phases, risks, open questions

| # | Phase | Effort (rough) |
|---|---|---|
| 1 | First local build on the Mac (start Colima, install buildx), fix the draft until `analyze`, `separate`, `preview` work; record real image sizes | 1 day |
| 2 | Vendor wavesurfer + fonts, `/api/health`, relative paths + migration command, CI workflow + smoke test, GHCR | 3 to 4 days |
| 3 | Auth (Host check + token) before any non-localhost publish; Proxmox deployment with the `cuda` tag | 2 to 3 days |
| 4 | Optional: baked-weights target, native-Mac stem helper docs, THIRD_PARTY licences | 1 day |

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Absolute paths re-separate 823 stem sets / lose analyses | Certain with a moved path | Hours of CPU, empty library | Same-path bind now, relative paths + migration later |
| Unauthenticated API published beyond localhost | Medium (one wrong `-p`) | Anyone on the LAN starts downloads / edits data | 127.0.0.1 publish default, auth before Proxmox |
| Demucs slow on Mac (CPU in a VM) | Certain | Minutes per new song | Existing 823 sets; native Mac stems run; `htdemucs` not `_ft` by default |
| yt-dlp breaks when YouTube changes | High, recurring | Downloads fail | Rebuild / retag often; yt-dlp pinned in `requirements-ci.txt` |
| CUDA image size (~4 GB pull) and CI disk | High | Slow pulls, failed CI | Separate tag, CPU default |
| Draft never built: Python 3.14 wheels for torch 2.14 / demucs deps on linux/arm64 | Medium | Build fails | Phase 1 build; fall back to Python 3.12 base if a wheel is missing |

Open questions for the owner:
1. Private image (GHCR private, only you) or public? Public raises licences and YouTube-download exposure.
2. CUDA tag now (Proxmox 3090 / Windows) or CPU only first?
3. Mac: accept CPU Demucs in the container, or keep stems as a native Mac job?
4. Data: keep `data/` as a bind mount with the same-path trick, or migrate to relative paths now?
5. Decision LLM: Ollama container in the compose, or always the host's Ollama / MLX?
6. Should the LAN (or Tailscale) ever reach the container? If yes, auth is a Phase 3 must.

---

## Rejected: desktop installers (first version of this plan)

The first version planned a PyInstaller onedir app with a pywebview window per OS (DMG, Inno Setup,
AppImage). Rejected by the owner in favour of one container. Why it was also the harder road: frozen
`sys.executable -m` children need a launcher dispatch, three signing programmes (Apple 99 USD / year,
Azure Artifact Signing 9.99 USD / month, eligibility limited to some countries), WebKit differences
(MediaRecorder MIME, pywebview's default `private_mode=True` drops localStorage), bundled ffmpeg /
rubberband / Deno per OS, and a 1 GB+ bundle per platform. The container keeps one Linux build, uses the
user's own browser for Web Audio, and needs no signing. What it gives up: a double-click app (the user
needs Docker), and MPS on macOS.

---

## Sources (retrieved 2026-09-30)

- Docker Desktop GPU support, Windows + WSL2 only, NVIDIA GPU-PV: https://docs.docker.com/desktop/features/gpu/ , https://www.docker.com/blog/wsl-2-gpu-support-is-here/
- No Metal / GPU passthrough for containers on Apple silicon: https://github.com/apple/container/discussions/62 , https://chariotsolutions.com/blog/post/apple-silicon-gpus-docker-and-ollama-pick-two/
- WSL2 bind mounts from `/mnt/c` are slow, keep data in the Linux filesystem: https://docs.docker.com/desktop/features/wsl/best-practices/ , https://www.docker.com/blog/docker-desktop-wsl-2-best-practices/
- Deno `bin` image and `COPY --from=denoland/deno:bin-2.9.x /deno`: https://github.com/denoland/deno_docker
- yt-dlp external JS runtime (Deno default, yt-dlp-ejs): https://github.com/yt-dlp/yt-dlp/issues/15012 , https://github.com/yt-dlp/yt-dlp/wiki/EJS
- PyTorch CPU / CUDA wheel indexes: https://docs.astral.sh/uv/guides/integration/pytorch/
- Demucs models (htdemucs_ft = bag of 4; htdemucs ~84 MB): https://github.com/facebookresearch/demucs , https://huggingface.co/AEmotionStudio/htdemucs-models
- Rubber Band Library licence: https://breakfastquay.com/rubberband/license.html
- FFmpeg licensing: https://www.ffmpeg.org/legal.html
- Rejected-section facts: pywebview API defaults https://pywebview.flowrl.com/api/ ; Apple Developer Program https://developer.apple.com/help/account/membership/program-enrollment/ ; Azure Artifact Signing https://azure.microsoft.com/en-us/pricing/details/artifact-signing/
