# Deployment Plan: NULL::SET AI DJ on the home server

Status: PLAN ONLY. Nothing here has been run on the server. Written 2026-09-30 from the code on
`main` (0e697b8), measurements on the owner's Mac, and web sources cited inline (all retrieved
2026-09-30). Items marked **unverified** have no primary source yet.

Target box: AMD Ryzen 9 5950X (16C/32T), 32 GB RAM, Proxmox VE host, RTX 3090 24 GB, 10 TB HDD.

---

## 0. What actually runs (from the code, not memory)

| Piece | Where | Deployment fact |
|---|---|---|
| API + static console | `app/ui/server.py` (FastAPI), started by `start.sh` as `python -m uvicorn app.ui.server:app --port $PORT` | No `--host`, so it binds **127.0.0.1:8000**. `load_dotenv()` at import. |
| Console | `app/ui/static/*.js` | Runs in the **listener's browser**; audio plays client-side. The server ships whole stem files (one FLAC 4-stem set is about 96 MB). |
| Auth / CORS | none | **No auth, no CORS middleware.** Unauthenticated write routes include `POST /api/download`, `POST /api/download/jobs` (both start yt-dlp) and `DELETE /api/liked`. |
| Health | no `/api/health` | `start.sh` polls `/api/recipes`; `/api/llm/status` and `/api/stems/status` report sub-systems. |
| Demucs | `audio/stem_service.py`, `stem_worker.py` | `DEMUCS_MODEL=htdemucs_ft` (config.py:41) for batch; the live worker uses `htdemucs`. Device order **cuda, then mps, then cpu**. No `--segment`/`--jobs`/`--shifts` passed. One GPU worker, `STEM_IN_FLIGHT=2`; backfill only while the LLM is idle. |
| Stem cache | `data/cache/stems/<sha256>_<model>[_vocals]/` | 16-bit FLAC plus `manifest.json` holding **absolute paths** (see section 7). |
| Keylock | `keylock.py` | `rubberband` CLI (`shutil.which` or `/opt/homebrew/bin/rubberband`); LRU cap `KEYLOCK_CACHE_MAX_GB` default 20. |
| Local LLM | `model_runtime.py`, `autopilot_service._chat_call` | OpenAI-compatible client. `LLM_BACKEND=auto|mlx|ollama`. Mac default: one `mlx_vlm.server` serving `mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit` on 8901 for text **and** audio. Off Apple Silicon it falls back to Ollama (`OLLAMA_URL` default `http://localhost:11434`, `OLLAMA_MODEL` default `gemma3:27b`). |
| LLM gate | `services/llm_gate.py` | One call at a time, priority LIVE > PLAN > EAR > SUGGEST > LOOKAHEAD. |
| Claude Code | `music_brain/llm/claudecode.py` | Only for learn-set review and genre labels (`AI_REVIEW_BACKEND=claudecode`). Runs `claude -p ... --json-schema`, strips `ANTHROPIC_API_KEY` from the child env. No API key in the app. |
| yt-dlp | `download_service.py`, `sources.py`, `set_learner.py` via `yt_guard.py` | Circuit breaker on bot check / 429 (120 s doubling to 3600 s, state in `yt_guard.json`). Cookies from `YTDLP_COOKIES_FILE` (start.sh default `~/.config/ai-dj/youtube-cookies.txt`, chmod 600). |
| ffmpeg | yt-dlp postprocessing, clip cuts, mp3 export | Required. `audio_convert.py` (WAV to FLAC) uses soundfile, not ffmpeg. |
| SQLite | `db.py` | `app.db` (shared knowledge) + `user.db` (private). WAL, `synchronous=NORMAL`, `busy_timeout` 30 s, `BEGIN IMMEDIATE`. No absolute paths stored in either DB. No backup helper. |
| Knowledge | `app/music_brain/knowledge/` (tracked in git) | `python3 -m app.music_brain.matching.knowledge sync` imports into app.db; owner-edited rows kept as conflicts. `auto_seed` runs it on change. |
| Maintenance | `./maintain.sh` = `python3 -m app.music_brain.maintain` | Steps: stems, flac, labels, review, atlas, export. `stems`/`flac` refuse to run while the server answers on `127.0.0.1:$PORT`. Reports in `data/cache/maintain/`. |
| Sim | `app/sim/` | Needs `node`. Starts its **own** in-process uvicorn on a random port and a private cache (`~/.cache/aidj-sim`). No base-URL flag: it cannot test a deployed server. |
| Packaging | none | No Dockerfile, no systemd/launchd files, no `.nvmrc`. `requirements.txt` leaves `torch` and `demucs` unpinned. |

### Current data size (owner's Mac, `du -sh`, 2026-09-30)

| Path | Size | Count | Growth driver |
|---|---|---|---|
| `data/cache/stems` | 52 GB | 822 dirs (341 ft, 295 htdemucs, 186 vocals) | about 96 MB per track for ft; htdemucs/vocals copies until `maintain stems` prunes them |
| `data/cache/keylock` | 15 GB | | capped by `KEYLOCK_CACHE_MAX_GB` (20) |
| `data/cache/uploads` | 12 GB | 592 songs | the library itself |
| `data/cache/app.db` | 480 MB | | atlas pairs, learned techniques |
| `data/cache/previews` | 205 MB | | rebuildable |
| `data/cache/sessions` | 173 MB | | per-set `events.jsonl` |
| `data/cache/user.db` | 9.1 MB | | vetoes, liked, history |
| `data/output` | 566 MB | | legacy mixes |
| **total `data/`** | **81 GB** | | |

At about 110 MB per new song (source + ft stems) plus a 20 GB keylock cap, 10 TB covers tens of thousands
of songs. Disk space is not the constraint; disk latency is (section 3).

---

## 1. Topology

**Recommendation: one unprivileged LXC for the app, sharing the 3090 through host device nodes.**
Do not pass the GPU to a VM with VFIO unless the owner needs the 3090 in another VM.

| | LXC + host NVIDIA driver | VM + VFIO passthrough |
|---|---|---|
| GPU sharing | Several containers can use the card at once | "If you pass through a device to a virtual machine, you cannot use that device anymore on the host or in any other VM." ([PVE wiki PCI(e) Passthrough](https://pve.proxmox.com/wiki/PCI(e)_Passthrough)) |
| RAM | No fixed guest reservation; the 32 GB is shared | VM RAM is reserved (pinned for passthrough), so the host and ARC get less |
| Driver upkeep | Host and container must run the **same** driver version; the container installs with `--no-kernel-module` ([FileFlows guide](https://fileflows.com/docs/guides/linux/proxmox-lxc-nvidia), community) | Driver lives only in the guest; host upgrades do not touch it |
| Isolation | Shared kernel | Full |
| Official docs | PVE docs do not cover NVIDIA in LXC (checked Linux_Container page) | Documented on the PVE wiki |

Why LXC: the 32 GB budget is tight (section 2) and a VM would lock away RAM. Demucs and the LLM server
also sit better as separate containers or processes on one card. The cost is keeping the host driver and
the container driver the same version at every upgrade.

Proxmox facts: PVE 9.2 is current (Debian 13 "Trixie", kernel 7.0, LXC 7.0, ZFS 2.4),
[press release 2026-05-21](https://proxmox.com/en/about/company-details/press-releases/proxmox-virtual-environment-9-2).
`dev[n]` device passthrough is in `pct.conf`
([pct.conf(5)](https://pve.proxmox.com/pve-docs/pct.conf.5.html)); API/CLI since 8.1, GUI since 8.2
([4sysops](https://4sysops.com/archives/new-features-in-proxmox-82/), secondary).

```bash
# Host (PVE shell): install the NVIDIA driver on the host. Production branch R595, 595.91.07 (2026-08-03):
# https://www.gamingonlinux.com/2026/08/nvidia-stable-driver-595-91-07-and-new-feature-driver-610-57-04-released-for-linux/
# Host prerequisites (headers / DKMS package names on PVE 9): unverified, check before running.
nvidia-smi                       # must list the RTX 3090 on the host
ls -l /dev/nvidia*               # note the nvidia-uvm major number, it can change across boots

# Container config /etc/pve/lxc/<CTID>.conf: dev[n] entries survive the dynamic uvm major better
# than lxc.cgroup2.devices.allow lines.
dev0: /dev/nvidia0
dev1: /dev/nvidiactl
dev2: /dev/nvidia-uvm
dev3: /dev/nvidia-uvm-tools
dev4: /dev/nvidia-caps/nvidia-cap1
dev5: /dev/nvidia-caps/nvidia-cap2

# Inside the container: same driver version, user-space only
./NVIDIA-Linux-x86_64-595.91.07.run --no-kernel-module
nvidia-smi                       # must show the same driver version as the host
```

Suggested guests:

| CTID | Role | Contents |
|---|---|---|
| 110 `aidj` | the app | venv, uvicorn, Demucs (CUDA torch), rubberband, ffmpeg, yt-dlp, node, the git checkout |
| 111 `llm` (optional) | model server | llama.cpp server (section 10). Can also run inside 110; a separate CT keeps model upgrades apart from app upgrades. |

---

## 2. Resource budget

### RAM (32 GB)

| Consumer | Budget | Basis |
|---|---|---|
| Proxmox host + services | 2 GB | "2 GB for OS and PVE services" ([PVE requirements](https://www.proxmox.com/en/products/proxmox-virtual-environment/requirements)) |
| ZFS ARC | **4 GB cap** | Installer default is 10% of RAM capped at 16 GiB ([PVE ZFS wiki](https://pve.proxmox.com/wiki/ZFS_on_Linux)); guidance "2 GiB base + 1 GiB/TiB" would ask for 12 GiB on 10 TB, which this box cannot give. The hot data (DBs) goes on SSD instead (section 3). |
| App CT: uvicorn + analysis (librosa, numpy) | 4 GB | unknown exactly; the server was not profiled for RSS |
| Demucs peak (host side of a GPU run: audio decode + tensors) | 4 GB | estimate, unverified |
| LLM server (weights live in VRAM, host keeps mmap/page cache) | 2 to 4 GB | estimate |
| `audio_convert --jobs` / `maintain` | 2 to 4 GB | `--max-mem-gb` limits it |
| Headroom | about 8 GB | |

```bash
# Host: cap ARC at 4 GiB
echo "options zfs zfs_arc_max=4294967296" > /etc/modprobe.d/zfs.conf
update-initramfs -u -k all && reboot
cat /sys/module/zfs/parameters/zfs_arc_max
```

Give CT 110 a 20 GB memory limit and 4 GB swap. Do not overcommit with a second heavy guest.

### VRAM (24 GB)

| Consumer | VRAM | Source |
|---|---|---|
| Demucs, default segment | about 7 GB | "about 7GB of RAM will be required if you use the default arguments"; 3 GB is possible with `--segment 8` ([demucs README](https://github.com/facebookresearch/demucs)) |
| Live LLM (section 10), Q4 + 8k context | 6 to 15 GB | per model below |
| CUDA contexts, fragmentation | 1 to 2 GB | estimate |

Rules:
- **Concurrent during a live set:** the live `htdemucs` worker (about 7 GB) plus the live LLM (at most about
  15 GB). This is the reason the live model must be 20B-class or smaller on this card.
- **Serialised (never during a set):** `maintain stems` (htdemucs_ft batch), big background model loads
  (the Qwen3.6-35B-A3B Q4_K_M file alone is 22.1 GB), `audio_convert`. `maintain` already refuses `stems`
  and `flac` while the server answers, and backfill already waits for LLM idle, so the code's gates match
  this rule.
- One GPU process per role. The app's `PriorityGate` serialises model calls; nothing serialises Demucs
  against a second model server, so never run two model servers on the card at once.

---

## 3. Storage

**Ask the owner first: is there an SSD or NVMe in the box, and how big?** Do not assume one.

SQLite on the HDD is the main latency risk. Under WAL with `synchronous=NORMAL`, commits append to the
WAL and checkpoints do random writes. On a 7200 rpm disk a random write is around 10 ms (typical figure,
unverified for this drive). `busy_timeout` is 30 s, so the risk is slow writes, not errors. `app.db` is
480 MB and is read randomly by the matcher and atlas, and the live console calls those reads.

| Data | Rebuildable? | Place | ZFS dataset (if ZFS) |
|---|---|---|---|
| OS, venv, CUDA torch, model files (5 to 22 GB each) | yes | SSD | `rpool/ROOT`, `rpool/data/models` |
| `app.db`, `user.db` + WAL | **no** | **SSD** | `rpool/data/aidj-db` recordsize=16K (matches `page_size=16384`) |
| `uploads/` (songs) | **no** (re-downloadable only in part) | HDD | `tank/aidj/songs` recordsize=1M, compression=off (already compressed audio) |
| `stems/` FLAC | yes (hours of GPU) | HDD | `tank/aidj/stems` recordsize=1M, compression=off |
| `keylock/`, `previews/`, `renders/`, `waveforms/` | yes | HDD | `tank/aidj/scratch`, no snapshots |
| `sessions/`, `sets/`, `learn_progress/`, `set_ai/`, `maintain/` | partly | HDD | `tank/aidj/state` |

The code has one root, `AIDJ_CACHE_DIR`, and fixed sub-folders under it. To split DBs onto the SSD without
changing code, mount datasets at the sub-folder paths and put the DB files on SSD through a symlink:

```bash
# Host: HDD pool (single disk, no redundancy: backups in section 6 matter)
zpool create -o ashift=12 tank /dev/disk/by-id/<hdd-id>
zfs create -o mountpoint=/tank/aidj tank/aidj
for d in songs stems scratch state; do zfs create -o recordsize=1M -o compression=off tank/aidj/$d; done
zfs set compression=lz4 recordsize=128K tank/aidj/state
# Bind into CT 110
pct set 110 -mp0 /tank/aidj,mp=/srv/aidj/cache
```

Whether `db.py` follows a symlinked `app.db` cleanly for its `-wal`/`-shm` siblings: **unverified**. SQLite
creates `-wal` beside the path it opened, so a symlinked file puts WAL on the HDD. Safer: put the whole
`AIDJ_CACHE_DIR` on SSD and bind-mount only `uploads/`, `stems/` and `keylock/` from the HDD. That keeps
DBs, sessions and small state on SSD (under 1 GB today).

ZFS vs ext4 on a single HDD: ZFS gives snapshots and checksums, which the backup plan uses; ext4/LVM is
lighter on RAM. With ARC capped at 4 GB, ZFS is still fine for big sequential audio files. Choose ZFS.

---

## 4. Runtime

Use a **Python venv inside the LXC**, not a container image. There is no Dockerfile today; adding one is
code work outside this plan, and a venv matches how `start.sh` and `maintain.sh` already work.

```bash
# CT 110 (Debian 13)
apt install -y python3-venv python3-dev git ffmpeg rubberband-cli nodejs sqlite3
git clone <repo> /srv/aidj/app && cd /srv/aidj/app
python3 -m venv .venv && . .venv/bin/activate
# torch is unpinned in requirements.txt; install the CUDA build explicitly first.
# PyTorch 2.14 (2026-09-02) stable CUDA 12.6 / 13.0 / 13.2, includes sm_86 (RTX 3090):
# https://github.com/pytorch/pytorch/blob/main/RELEASE.md . Driver R595 supports CUDA 13.2
# (https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html).
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu130
pip install -r requirements.txt
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

Notes:
- `requirements.txt` pins `numpy==1.26.4`, `scipy==1.10.1`, `librosa==0.9.2`, and CI uses Python 3.14
  with numpy 2.5.3. Debian 13's Python version against these pins: **unverified**. Use the Python the
  owner's Mac venv uses (check `.venv/bin/python -V`) through `uv` or pyenv if Debian's is different.
- Demucs upstream is archived ("this repository is not maintained anymore", README); pin the version the
  Mac uses (`pip freeze | grep -i demucs`).
- `keylock.py` falls back to `/opt/homebrew/bin/rubberband`; on Linux it finds `rubberband` on `PATH`.
- `start.sh` is Mac-shaped (mlx on arm64, opens a browser, its own supervisor). On the server, use systemd
  directly and skip `start.sh`.

### systemd units (CT 110)

```ini
# /etc/systemd/system/aidj-llm.service  (llama.cpp server, section 10)
[Unit]
Description=AI DJ local model (llama.cpp)
After=network-online.target
[Service]
User=aidj
ExecStart=/srv/aidj/llama.cpp/build/bin/llama-server -m /srv/aidj/models/<live-model>.gguf \
  --host 127.0.0.1 --port 8081 -ngl 99 -c 8192 --jinja
Restart=on-failure
[Install]
WantedBy=multi-user.target

# /etc/systemd/system/aidj.service
[Unit]
Description=NULL::SET AI DJ
After=aidj-llm.service
Wants=aidj-llm.service
[Service]
User=aidj
WorkingDirectory=/srv/aidj/app
EnvironmentFile=/srv/aidj/aidj.env
ExecStart=/srv/aidj/app/.venv/bin/python -m uvicorn app.ui.server:app --host 127.0.0.1 --port 8000 --log-level info --no-access-log
Restart=on-failure
[Install]
WantedBy=multi-user.target
```

```bash
# /srv/aidj/aidj.env (chmod 600, owner aidj; never in git)
AIDJ_CACHE_DIR=/srv/aidj/cache
LLM_BACKEND=ollama               # "ollama" path = any OpenAI-compatible URL, see model_runtime.py
OLLAMA_BASE_URL=http://127.0.0.1:8081/v1
AUTOPILOT_MODEL=<live-model-name>
OMNI_BASE_URL=http://127.0.0.1:8081/v1   # ear: only if the live model takes audio, else the ear falls back to rules
YTDLP_COOKIES_FILE=/srv/aidj/secrets/youtube-cookies.txt
KEYLOCK_CACHE_MAX_GB=20
```

Whether `model_runtime` in `ollama` mode accepts a non-Ollama OpenAI server (it refreshes keep-alive
through Ollama's API every 4 min): **unverified**. Test in phase 3; if it fails, the fallback is to set
`OLLAMA_BASE_URL`/`AUTOPILOT_MODEL` and leave `LLM_BACKEND` unset.

```ini
# /etc/systemd/system/aidj-maintain.service + .timer : weekly, at a time no set is played
[Service]
Type=oneshot
User=aidj
WorkingDirectory=/srv/aidj/app
EnvironmentFile=/srv/aidj/aidj.env
ExecStartPre=/bin/systemctl stop aidj.service      # stems/flac refuse to run while the server answers
ExecStart=/srv/aidj/app/maintain.sh --max-minutes 240
ExecStopPost=/bin/systemctl start aidj.service
[Timer]
OnCalendar=Tue 04:00
Persistent=true
```

`ExecStartPre` needs root; either run the unit as root with `User=` on a wrapper, or grant the `aidj` user
a polkit rule for those two units. The owner should decide whether a weekly stop at 04:00 is acceptable.

Startup order: storage mounts, then `aidj-llm`, then `aidj` (auto_seed knowledge sync runs on import),
then the reverse proxy.

Logs: `journalctl -u aidj -u aidj-llm`. The app prints to stdout (no `logging.basicConfig`). Session events
stay in `$AIDJ_CACHE_DIR/sessions/<stamp>/events.jsonl`; maintain reports in `$AIDJ_CACHE_DIR/maintain/`.

### Update and rollback

```bash
cd /srv/aidj/app
git fetch && git log --oneline HEAD..origin/main          # read what is coming
PREV=$(git rev-parse HEAD)
/srv/aidj/bin/db-backup.sh                                # section 6, before any schema change
git pull --ff-only origin main && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.music_brain.matching.knowledge sync
.venv/bin/python -m pytest app/tests/ -q -m "not slow" --ignore=app/tests/py/test_ui_server.py
systemctl restart aidj && curl -fsS http://127.0.0.1:8000/api/recipes >/dev/null && echo up
# rollback: git checkout $PREV, restore the DB backup if schema_version moved, restart
```

Schema versions live in each store's `schema_version` table; code that ran a migration may not run on an
older commit, which is why the DB backup comes before the pull.

---

## 5. Network and security

The app has **no authentication and no CORS**, and some of its write routes start downloads. It must never
be reachable without an auth layer in front.

- uvicorn stays on `127.0.0.1:8000` (as today).
- A reverse proxy in CT 110 terminates TLS and adds auth. Caddy with `basic_auth` is the least work; the
  console is a single-user app.
- **LAN only by default.** For remote access use Tailscale (or WireGuard) into the CT. Never port-forward.
  Stems are about 96 MB per track, so remote playback over a home uplink will be slow to load (see section 11).
- `user.db` is private: never served, never exported (`knowledge export` covers app.db content only), never
  in git. The backup copy stays on the server and the owner's own backup target.
- Secrets: `aidj.env` and the cookies file outside the repo, mode 600. `.env` is gitignored; `load_dotenv()`
  would read one in the working directory, so do not leave a `.env` in `/srv/aidj/app` with real values.
- YouTube: a server IP downloads with no browser session, so bot checks are likely (unverified for this
  ISP). `yt_guard` already backs off and retries other clients. Export cookies from the owner's browser into
  `YTDLP_COOKIES_FILE`. With the file present `start.sh` sets `YT_GUARD=off` (cookies on every call); under
  systemd leave `YT_GUARD` on so cookies are only used after a refusal.

```caddy
# /etc/caddy/Caddyfile (CT 110)
aidj.lan {
    tls internal
    basic_auth {
        owner <bcrypt-hash from `caddy hash-password`>
    }
    reverse_proxy 127.0.0.1:8000
}
```

Check that `MediaRecorder` capture and audio playback work under `tls internal` (browsers need a secure
context for some media APIs; the CA must be trusted on the listener's device).

---

## 6. Backups

| Data | Precious? | Method |
|---|---|---|
| `user.db` (vetoes, liked, history, set memory) | **yes** | `sqlite3 .backup`, nightly, 30 kept |
| `app.db` (atlas, learned, labels, macros, knowledge rows) | **yes** (knowledge/ in git covers exported rows only) | `sqlite3 .backup`, nightly |
| `uploads/` songs | yes | ZFS snapshot + off-box copy |
| `knowledge/` | in git | push to the remote |
| stems, keylock, previews, renders, waveforms | no, rebuildable (stems cost GPU hours) | snapshot `stems` weekly only if space allows; exclude the rest |
| `sessions/`, `sets/` | nice to have | ZFS snapshot |

Do not copy a live SQLite file under WAL: the copy can miss pages still in `-wal`. Use the online backup API:

```bash
#!/bin/sh
# /srv/aidj/bin/db-backup.sh
set -eu
D=/srv/aidj/backup/$(date +%F_%H%M); mkdir -p "$D"
for db in app user; do
  sqlite3 "$AIDJ_CACHE_DIR/$db.db" ".backup '$D/$db.db'"
  sqlite3 "$D/$db.db" "PRAGMA integrity_check;" | grep -qx ok
done
find /srv/aidj/backup -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +
```

```bash
# Host: snapshots and CT backup
zfs snapshot -r tank/aidj@$(date +%F)
vzdump 110 --mode snapshot --storage <backup-store> --exclude-path /srv/aidj/cache/stems --exclude-path /srv/aidj/cache/keylock
```

The pool is one HDD with no redundancy, so snapshots do not protect against disk failure. `user.db`,
`app.db` and `uploads/` also need an off-box copy (the owner's Mac or a USB disk); the combined size is
about 12.5 GB today.

---

## 7. Migration from the Mac

### Path assumptions (checked in code and on disk)

| Where | Absolute path? | Effect on the server |
|---|---|---|
| `app.db`, `user.db` | no: folders by leaf name, knowledge by relative key, marks by file name | copy as is |
| `stems/*/manifest.json` | **yes**, e.g. `/Users/me/dev/AI-DJ-Mixing-System/data/cache/stems/<id>/vocals.flac` | `read_manifest` returns None when any path is missing, so **every stem set would count as a cache miss and be separated again** (822 dirs). Must rewrite. |
| `sets/**/study.json` `study_path` | yes (`set_learner.py:1409`) | rewrite, or re-run the study |
| `set_import` track `path` | yes (`set_import.py:315`) | rewrite |
| sim `stem_capture` | yes, but in the sim cache | ignore |
| `/api/tracks` paths | built at runtime | fine |

Rewrite script (run on the server copy, dry run first):

```python
# /srv/aidj/bin/rewrite_paths.py  (one-off migration helper, not app code)
import json, pathlib, sys
OLD = "/Users/me/dev/AI-DJ-Mixing-System/data/cache"
NEW = sys.argv[1]            # e.g. /srv/aidj/cache
apply = "--apply" in sys.argv
n = 0
for pattern in ("stems/*/manifest.json", "sets/**/study.json"):
    for f in pathlib.Path(NEW).glob(pattern):
        txt = f.read_text()
        if OLD in txt:
            n += 1
            if apply:
                json.loads(txt.replace(OLD, NEW))          # still valid JSON
                f.write_text(txt.replace(OLD, NEW))
print(("rewrote" if apply else "would rewrite"), n, "files")
```

### What to copy, what to regenerate

| Copy | Skip (regenerate) |
|---|---|
| `app.db`, `user.db` (via `.backup`, with the Mac server **stopped**) | `keylock/` (15 GB, LRU cache) |
| `uploads/` + `_names.json` (12 GB) | `previews/`, `renders/`, `waveforms/` |
| `stems/*_htdemucs_ft` **FLAC only** (run `audio_convert --apply` on the Mac first so no WAV is left) | `stems/*_htdemucs`, `*_vocals` (live copies; `maintain stems` prunes them) |
| `analysis/`, `sessions/`, `sets/`, `set_ai/`, `learned_review/`, `lyrics/`, `sources/` | `*.migrated` leftovers (`pair_atlas.migrated`, `macros.migrated`, `learned_techniques.json.migrated`) after a check |
| `knowledge_seed.json`, `yt_guard.json` (optional) | |

### Dry-run checklist

1. On the Mac: stop the app; `sqlite3 app.db .backup`, same for `user.db`; `PRAGMA integrity_check` both.
2. `find stems -name '*.wav' | wc -l` is 0 (else run `audio_convert --apply`).
3. `rsync -aHn --info=stats2` the copy list to the server (dry run), check the byte total (about 30 GB with ft stems only, estimate).
4. Real rsync over LAN.
5. `rewrite_paths.py /srv/aidj/cache` (count should be about 340 ft manifests plus study files), then `--apply`.
6. Start with `AIDJ_CACHE_DIR` pointing at the copy; `curl /api/stems/status` and the backfill line in the
   journal should report **0 tracks to separate** (it said "0 of 577" on the Mac).
7. `GET /api/tracks` count equals 592; play one known track in the console; veto and liked lists match.
8. Keep the Mac copy untouched until the server has run one full set.

---

## 8. Monitoring

- GPU: `nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv -l 30`
  logged to the journal by a small unit, or `nvtop` interactively.
- Disk: `zpool status -x`, `zfs list -o name,used,avail`, `smartctl -a` on the HDD monthly.
- Health: no `/api/health` exists. Use `curl -fsS http://127.0.0.1:8000/api/recipes` (what `start.sh`
  uses) plus `/api/llm/status` for the model and `/api/stems/status` for the worker. A systemd timer that
  runs these every 5 min and logs failures is enough for one user.
- LLM latency: every call is already logged as `kind: "llm"` in `sessions/*/events.jsonl` with `elapsed`,
  `waited`, `prompt_chars`, `reply_chars`, `cut_off`. Compare with the Mac baseline in section 11.
- Smoke test: `python3 -m app.sim.suite --check` runs its own in-process server, so it tests the **code and
  model config** on the box, not the deployed service. Run it after each update. For the deployed service,
  Playwright (`npm run test:e2e`) targets `http://127.0.0.1:8000`.

---

## 9. Phased rollout

1. **Ask the open questions below.** Nothing is bought or wiped before answers.
2. Host: PVE 9.2 updates, NVIDIA driver on the host, `nvidia-smi` works. ARC cap. Create `tank` and datasets.
3. CT 110: device nodes, same driver user-space, CUDA torch, `torch.cuda.is_available()` is True.
4. llama.cpp server with the live model (section 10); `curl /v1/chat/completions` with a JSON schema returns valid JSON.
5. App venv + tests (`pytest -m "not slow"`, JS checks), empty cache: upload 3 songs, separate, match, preview.
6. Time one htdemucs_ft separation on the 3090 and one on the Mac (first real numbers for section 11).
7. Migration dry run (section 7), then the real copy, rewrite, verify 0 to separate.
8. Caddy + TLS + basic auth; LAN test from the listener's device.
9. Backups and timers on; restore test: restore `user.db` from last night into a scratch dir and open it.
10. Play one full set on the server with the Mac as fallback; compare `events.jsonl` LLM timings with the Mac.
11. Optional: Tailscale for remote access.

### Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Stem manifests hold Mac absolute paths, so the server re-separates 822 sets | certain if skipped | days of GPU | `rewrite_paths.py`, check "0 to separate" |
| No auth on the app, including download routes | certain | anyone on the network drives yt-dlp and edits data | 127.0.0.1 bind + Caddy basic auth + LAN/Tailscale only |
| VRAM over-commit (Demucs 7 GB + too big a model) causes CUDA OOM mid-set | medium | stems or AI plan fail in a set | live model at most about 15 GB; batch jobs serialised; `--segment 8` as a last resort (needs code) |
| SQLite on HDD slows live writes | medium without SSD | UI stalls | DBs on SSD (ask); ZFS recordsize 16K |
| Host/CT NVIDIA driver drift after `apt upgrade` | medium | GPU gone in the CT | pin the driver on the host; upgrade both together; `nvidia-smi` in both after each upgrade |
| 32 GB RAM contention (ARC + model mmap + Demucs) | medium | swap, stalls | ARC cap 4 GB, CT limit 20 GB, no other heavy guests |
| YouTube bot checks from the server IP | medium | downloads refused | cookies file, `yt_guard` backoff |
| Ear (audio in) has no verified server model yet | high | ear falls back to rules | keep the rule fallback; see section 10 |

### Open questions for the owner

1. Is there an SSD or NVMe in the box? Size?
2. Remote access wanted, or LAN only?
3. Other guests on this Proxmox box now or planned? Does any other VM need the 3090?
4. Claude Code on the server (needs a `claude` login there) or on the Mac only?
5. Is the HDD a single disk? Is there an off-box backup target?
6. Is a weekly maintenance stop at 04:00 acceptable?
7. Which device plays sets (browser on the Mac, a phone, a TV)? It decides the LAN speed needed for stems.

---

## 10. LLM for decisions

### 10a. Inventory (every call site)

All text calls go through `autopilot_service.chat_raw` then `engine.AIBackend.chat`, serialised by
`PriorityGate`. JSON is asked for with `response_format={"type":"json_object"}` and parsed leniently
(`_extract_json`, one strict retry). No schema is enforced by the server today, except the claudecode path.

Measured on the Mac from `sessions/*/events.jsonl` (710 LLM events, Qwen3-Omni-30B-A3B 4-bit on MLX):

| Role | Code | Decides | Prompt / reply | Budget | How often | Audio | Measured (p50 / p90) |
|---|---|---|---|---|---|---|---|
| Next-song suggest | `autopilot_service.suggest_next_tracks` (:1293), `autopilot.js:2090` | next songs by name | ~12.9k / ~1.7k chars (p90 prompt 4.2k tokens) | queue wait up to 240 s; retry only inside `SUGGEST_BUDGET_S` 15 s | per track | no | 13.7 s / 17.1 s (n=324) |
| Look-ahead pool | same, `priority=lookahead`, `topUpPool` | songs ahead of need | ~12.3k / ~1.7k chars | background | pool top-ups | no | 13.4 s / 18.6 s (n=171); 27 failures |
| Transition plan | `mind_plan.plan_pair` (:615), `/api/autopilot/plan` | recipe, exit point, moves | ~5.4k / ~0.6k chars, `max_tokens` 600 | 60 s server; client gives up at clamp(deadline, 12, 65) s then rules | per transition | no | 4.6 s / 8.0 s (n=187) |
| Live ear | `live_ear._ask_omni` (:228) | keep / move_loop / filter_wash / restore | 8 s WAV clip + metrics, 200 tokens | 4 to 10 s adaptive; falls back to rules | once per 8-bar phrase in a hold loop | **yes** | ~1.1 s idle, 3 to 6 s busy (gate docstring) |
| Merge audition | `render/merge.py` `ear_rate` | 1 to 10 score for a stem merge | clip + 120 tokens | 20 s wait then skip | per merge candidate | **yes** | not logged separately |
| Learn-set review | `learning/set_ai.review` | keep or reject detected moves | batches of 12 (40 on claudecode), 1500 tokens | 120 s (300 s claudecode) | offline, per set | no | not in session logs |
| Emotional lines | `set_ai.emotional_lines` | hook lyric lines | small | 120 s | on demand, once per track | no | |
| Genre / era labels | `analysis/genre_labels.label_library` | genre + era per song | batch 5 local / 40 claudecode | 120 s | offline via `maintain labels` | no | |
| Legacy / research | `legacy_pipeline/*`, `research/*` | gpt-4o, gpt-4o-mini, whisper-1, Gemini | | batch | not used by the live app | whisper only | |

Raw MLX server log on the Mac (`/tmp/ai-dj-omni-server.log`): decode 61 tok/s p50 (48 to 69), prefill about
1,300 tok/s on a 2k-token prompt, prompt p50 about 2k tokens, p90 4.2k tokens.

So, for a live model: about 4k tokens in and 450 tokens out per suggest; 8k context is enough, 16k is safe.
Suggest also needs **music knowledge** (it names real songs), which favours a bigger model.

### 10b. Recommendation per role on the 3090

Serving stack: **llama.cpp `llama-server`**. It is OpenAI-compatible, which is what the app's client
expects, and `response_format` accepts `json_object` **or a JSON schema, converted to a grammar**
([llama.cpp server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)).
It loads GGUF quantisations and allocates only what the model needs, so it can sit beside Demucs.
vLLM pre-allocates `--gpu-memory-utilization` (default 0.92) of the card
([vLLM engine args](https://docs.vllm.ai/en/latest/configuration/engine_args.html)), which would starve
Demucs. Ollama adds a JSON-schema `format` ([Ollama docs](https://docs.ollama.com/capabilities/structured-outputs))
and is the app's built-in fallback, so it is the second choice. Its default keep-alive is 5 minutes, but
the app's `model_runtime` refreshes it.

| Role | Model (verified) | Released / licence | Quant, size | VRAM with 8k ctx | Expected speed on 3090 |
|---|---|---|---|---|---|
| **Live decision** (suggest, plan), primary | **gpt-oss-20b** (21B total / 3.6B active MoE, MXFP4, "runs within 16GB", structured outputs; needs the harmony format, so `--jinja`) ([HF card](https://huggingface.co/openai/gpt-oss-20b)) | Aug 2025, Apache 2.0 | native MXFP4, about 13 GB (unverified exact) | about 14 to 15 GB | unverified; MoE with 3.6B active should decode well above the 7B figure below |
| Live decision, safe floor | **Qwen3-8B** GGUF Q4_K_M ([HF](https://huggingface.co/Qwen/Qwen3-8B-GGUF)) | May 2025, Apache 2.0 | Q4_K_M, **5.03 GB** | about 6 to 7 GB | about 150 tok/s (7B Q4 on a 3090: tg128 158 t/s, pp512 5,175 t/s, [llama.cpp #15013](https://github.com/ggml-org/llama.cpp/discussions/15013)) |
| Background review / labels | **Qwen3.6-35B-A3B** Unsloth GGUF Q4_K_M ([HF](https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF), base [card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)) | 2026-04-16, Apache 2.0 | Q4_K_M, **22.1 GB** | does not fit beside Demucs; run **alone**, in `maintain` | unverified |
| Ear + merge audition (audio) | no verified server option yet. Candidate: **Gemma 4 12B** (audio input on E2B/E4B/12B, 256K context, [model card](https://ai.google.dev/gemma/docs/core/model_card_4)) | 2026-07-30, Apache 2.0 | GGUF size unverified | unverified | unverified; llama.cpp audio support for Gemma 4 unverified |

Qwen3-Omni-30B-A3B, which the Mac uses today, needs 78.85 GB at BF16
([HF card](https://huggingface.co/Qwen/Qwen3-Omni-30B-A3B-Instruct)); llama.cpp support is not stated and
vLLM needs a custom branch. A 4-bit build would be about 17 to 18 GB (estimate) and would not leave room for
Demucs. So on the server the ear either runs on a smaller audio model (Gemma 4 12B, after a test), stays on
the Mac, or falls back to its rule decision (it already does on any failure).

Using one model for text and ear (as the Mac does today) keeps VRAM to one model. If Gemma 4 12B passes
the audio test, it is the one-model option for the server: live text **and** ear. gpt-oss-20b has no audio input.

Temperature and prompts: keep the app's settings (0.75/0.4 suggest, 0.3 plan, 0.2 ear/review). The
`/no_think` suffix is only added for qwen3 names; Qwen3.6 has thinking on by default, so the
background role needs thinking disabled through the chat template or a larger `max_tokens`. Test this.

### 10c. Where the Claude Code path is better

| Role | Better on | Why |
|---|---|---|
| Learn-set review | **Claude Code** (`AI_REVIEW_BACKEND=claudecode`) | offline, batch of 40, real `--json-schema` enforcement, much stronger judgement than any model that fits the card; uses the owner's subscription, no API key |
| Genre / era labels | **Claude Code** | needs world knowledge of songs; offline; batch 40 |
| Suggest / plan / ear | local | live deadline (12 to 65 s for plan, 15 s suggest budget), runs every track; claudecode is not wired for these and has a 300 s timeout and rate-limit backoff of 30 to 90 s |

Run `claude -p` where the owner is logged in. On the Mac this is already true. On the server it means a
`claude` login inside CT 110 (open question 4). `claudecode.py` strips `ANTHROPIC_API_KEY`, so it cannot
silently fall back to paid API billing.

### 10d. How to evaluate the choice (no paid calls)

1. **Sim replay** as the regression floor: `python3 -m app.sim.suite --check` (seeds 1 to 5, long + quick)
   with `OLLAMA_BASE_URL`/`AUTOPILOT_MODEL` pointed at each candidate. Note: `baseline.json` is from the
   stub-LLM era, so first record a fresh baseline with the current Mac model
   (`python3 -m app.sim.virtual_set --seed N --tracks 10 --mode quick --record mac-omni-N`), then run each
   candidate on the same seeds and compare with `python3 -m app.sim.compare`.
2. **Liked / vetoed pairs** from `user.db` (`marks` table): for each vetoed pair, ask the candidate's
   `plan_pair` and suggest with the pair's context and count how often it proposes the vetoed move or song;
   for liked pairs, count agreement. Measure JSON validity rate (first try, after strict retry), `cut_off`
   rate, p50/p90 latency from `events.jsonl`.
3. Pass bar: JSON valid at least 98% on first try, plan p90 under 12 s (the client floor), suggest p90 under
   15 s, veto hit rate no worse than the Mac baseline.
4. For review/labels: run 50 already-reviewed observations through the local candidate and compare with the
   stored claudecode answers (cached per backend and model in `set_ai/`). This uses the cache, no new paid call.

### 10e. The Qwen omni 16 GB distillation slot (future)

A distilled omni model around 16 GB would slot in as the one model for text + ear on the server. That only
works if it and the live Demucs worker (about 7 GB) fit in 24 GB together with context, which is tight. It
plugs in through the existing env: `OMNI_BASE_URL`/`OMNI_MODEL` for audio, `OLLAMA_BASE_URL`/`AUTOPILOT_MODEL`
for text, or a new `engine.AIBackend`. No code path needs to change for the swap.

---

## 11. 3090 server vs M4 Max 64 GB (the owner's Mac)

### Measured on the Mac (read-only, 2026-09-30)

- Hardware: Apple M4 Max, 16 CPU cores, 64 GB. 64 GB is only sold with the 16-core CPU / 40-core GPU bin,
  546 GB/s ([Apple M4 Max MacBook Pro specs](https://support.apple.com/en-us/121553)). It was on battery at 74% when measured.
- Demucs device: `mps` (code order cuda, mps, cpu; one-shot MPS failures retry on CPU). **Per-track time:
  unknown.** Neither the server log nor `learn_progress` records separation time, and manifest mtimes were
  reset by the FLAC conversion. Phase 9 step 6 measures it.
- LLM (Qwen3-Omni-30B-A3B 4-bit, mlx_vlm): decode 61 tok/s p50, prefill about 1,300 tok/s; suggest 13.7 s
  p50, plan 4.6 s p50 (section 10a).

### Published figures

| Workload | RTX 3090 | M4 Max 40c | Source |
|---|---|---|---|
| llama.cpp 7B Q4_0 generation | 158 t/s | 83 t/s | [#15013](https://github.com/ggml-org/llama.cpp/discussions/15013), [#4167](https://github.com/ggml-org/llama.cpp/discussions/4167) |
| llama.cpp 7B Q4_0 prompt processing | 5,175 t/s | 886 t/s | same |
| Memory for a model | 24 GB VRAM, shared with Demucs | about 48 GB usable of 64 GB unified (estimate) | |
| Demucs htdemucs_ft per track | unknown (README has no GPU figures) | unknown | [demucs README](https://github.com/facebookresearch/demucs): CPU "roughly 1.5 times the duration of the track"; ft is "4 times more time" than htdemucs |
| Power | about 350 W under load, about 23 W idle (secondary, unverified) | 96 W adapter | |

For the same model at the same quantisation, the 3090 generates about 1.9x faster and processes prompts
about 5.8x faster (7B figures). Suggest prompts are about 4k tokens, so prefill matters: about 3 s on the
Mac today versus under 1 s on the 3090 (estimate from these rates). The Mac can load bigger models (the
30B-A3B omni runs there today with room to spare); the 3090 cannot fit a 30B-class model beside Demucs.

Console latency is the same either way for playback: audio runs in the listener's browser. What differs is
stem loading: on the Mac the browser loads from localhost; from the server it loads about 96 MB per track
over the LAN (under 1 s on gigabit Ethernet, several seconds on Wi-Fi, estimate).

Availability: the server is always on. The laptop sleeps, travels and runs on battery (74% with 2.5 h left
while idle-ish at measurement), and the `maintain` jobs and backfill stop when it sleeps.

### Verdict per role

| Role | Runs best on | Why |
|---|---|---|
| Demucs batch (`maintain stems`, backfill) | **server** | CUDA is the first device in the code, always on, can run overnight; the Mac's MPS time is unknown but the laptop is not always awake |
| Live Demucs worker | **wherever the app runs** | it serves the console; keep it with the app |
| Live-decision LLM | **Mac today; server once a 20B-class model passes the eval** | the Mac runs a 30B-A3B omni model with measured 4.6 s plans and 13.7 s suggests and the ear works. The server is faster per token but limited to about 15 GB beside Demucs, so it runs a smaller model that knows fewer songs. Only 10d can say whether that loses quality. |
| Ear (audio) | **Mac** until a server audio model is verified | no tested audio model for the 3090 yet; ear falls back to rules |
| Background review / labels | **Claude Code** (either box); local fallback on the **server** with Qwen3.6-35B-A3B run alone | quality plus schema enforcement; the server has idle nights |

**Plain answer:** yes, for the heavy batch work: the server should own the library, the stems and the
nightly jobs. For live decision making the Mac is at least as good today, because 64 GB lets it run the
bigger model plus the ear, and it is measured and working. The best setup now is a **split**: the server
runs the app 24/7 with Demucs and batch jobs. For a live set the owner either (a) plays from the server app
with the small live model after it passes the eval, or (b) keeps playing on the Mac with a synced copy of
the library. Option (b) needs a sync step and is the fallback during rollout. Pointing the server app at a
model served from the Mac over the LAN (`OMNI_BASE_URL=http://<mac>:8901/v1`) is possible through the
existing env, but it exposes an unauthenticated model port on the LAN and depends on the laptop being
awake, so it is not recommended.
