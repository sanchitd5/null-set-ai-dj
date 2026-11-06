# Stem server

Splits a song once with Demucs on the GPU and serves short AAC windows of its stems to the phone, so
the phone no longer runs htdemucs itself. It is a small standalone FastAPI app, not the console API:
only the routes below exist.

It shares the console's cache. Tracks live in `data/cache/uploads/<id>.<ext>` and stems in
`data/cache/stems/<sha256>_<model>/`. A song the console has already split is served without a second
split, and a song split here is available to the console too:

* The server reads the best complete 4-stem set first: its own model (`htdemucs_ft`), then the
  console's backfill set (`htdemucs`, from the console's warm `stem_worker`). This is the same order
  as `stem_service.cached_four_stems`. `X-Stem-Model` and `GET /v1/tracks/{id}` name the set that
  was served.
* An `htdemucs_ft` set written here is read first by the console, and its `htdemucs` copy is pruned
  (`stem_service._auto_prune`).
* Both pick the device the same way: cuda, then mps, then cpu (`stem_service._detect_device`,
  `stem_worker.py`).
* The console's library backfill (`_backfill_stems` at startup, `_stem_worker`) uses the GPU once
  CUDA torch is in the venv and the console's service user can open `/dev/nvidia*` (gid 44).

With `STEM_SERVER_URL` set in the console's env (CT116), the console splits nothing itself: its decks,
its library backfill and `POST /api/tracks/{id}/separate` all go through this server's queue
(`app/ui/services/stem_remote.py`), so one process owns the GPU. Library tracks outside `uploads/` are
linked into `data/cache/stem_links/<id>.<ext>`. Without it (a laptop) the console keeps its own warm
`htdemucs` worker as before. Encoded windows are derived files in
`data/cache/stem_windows/`, and deleting that folder is safe.

## API

Every route except `/health` needs `Authorization: Bearer $STEM_SERVER_TOKEN`, or a console device
token. Device tokens come from the console's account store (`DJ_AUTH_FILE`, set it to the
console's value in `/etc/null-set/stem-server.env`; see `docs/engineering/AUTH.md`), so a phone that signed in with Google once uses the
same token for both servers, and revoking it in the console's DEVICES panel cuts it off here too. Both
services must run as the same user (the store is chmod 600). The static token is compared in
constant time, and the server refuses to start without a token of at least 32 characters (`openssl rand -hex 32` gives 64). Failed attempts are
rate-limited per client IP (see Network). Track ids
are `sha256(file bytes)[:16]`, the same as the console and the phone.

| route | does |
|---|---|
| `GET /health` | `{"ok": true}` (no auth) |
| `HEAD/GET /v1/tracks/{id}` | 404 when unknown; otherwise `{"id", "separated", "model"}`. The phone hashes locally and skips the upload when this returns 200 |
| `POST /v1/tracks?id=<id>&ext=<ext>` | raw audio body (capped at `STEM_SERVER_MAX_UPLOAD_MB`, default 200). 422 when `sha256(body)[:16] != id` |
| `POST /v1/separate {"id", "stems": 2\|4, "priority"}` | returns a job. `priority`: deck / booked (only with `STEM_SERVER_TOKEN`, i.e. the console) > phone (default, and what a device token gets) > backfill. One job per song: asking again returns the same job, raised to the higher priority. A job is done once the song has an `htdemucs_ft` set; a cheaper set already in the cache is served meanwhile. The whole song is split once, 4-way. A 2-stem request is served from that split (`no_vocals` = drums + bass + other), because htdemucs_ft runs all of its sources either way |
| `GET /v1/tracks/{id}/analysis` | the console's analysis v6 of an uploaded track (`analyzer.analyze`, same disk cache as `/api/tracks/{id}/analysis`). The phone reads it instead of analysing on the device |
| `GET /v1/jobs/{job}` | `status`: queued / running / done / error, plus `priority`, `position` (1 = next), real `progress` (segments done), `elapsed`, `eta`, `device`, `model`, `error`, `note`, `seconds` |
| `GET /v1/jobs` | the queue in run order (running first), `queued`, `model`, `device`, `worker` (`alive`, `pid`) |
| `GET /v1/tracks/{id}/stems/{name}.m4a?start=S&end=E` | AAC-LC 256 kbps, 44.1 kHz stereo, covering that window only (at most 600 s). `name`: vocals, drums, bass, other, no_vocals. Returns 409 before the split, 416 past the end of the track |

Window response headers: `X-Stem-Sample-Rate`, `X-Stem-Start-Frame` (`round(S*44100)`),
`X-Stem-Frames`, `X-Stem-Total-Frames`, `X-Stem-Encoder-Delay`, `X-Stem-Encoder-Padding`,
`X-Stem-Model`, `X-Stem-Sha256` (of the body).

Routes are relative and the server never builds a URL to itself, so a proxy can mount it under a
stripped prefix (Caddy: `/stems/*` → `127.0.0.1:$STEM_PORT`). The phone's base URL may include
that path, for example `https://<domain>/stems`.

### Timing alignment

The phone mixes stems sample-aligned against the original track. ffmpeg's AAC encoder adds 1024
priming samples and pads the last frame. The m4a carries an edit list (`elst` media_time 1024,
duration = frames), so decoders behave like this:

* **ffmpeg / AVFoundation (`AVAudioFile`, iOS):** they honour the edit list and return exactly
  `frames` samples, starting at `start_frame`.
* **Android MediaExtractor + MediaCodec loop (the app's `decodeToWav`):** it keeps every decoded
  sample, priming included.

The client never relies on either behaviour. From the decoded length it computes how many samples to
drop (`leadingSkip`: drop `encoder_delay` when the priming is still there, else 0). It then
cross-correlates the window against its own decode of the original track. That second check also
catches any offset between the phone's decode of the source file and the server's decode.

`app/tests/py/test_stem_server.py::test_window_is_sample_exact` covers this. It encodes a click-track
window, decodes it with ffmpeg as an edit-list decoder, as a raw decoder (`-flags2 skip_manual
-ignore_editlist 1`) and with the edit list ignored, then applies the header trim. Every click must
land within 1 ms.

The phone side is `lib/separation/remote_stems.dart` in the mobile repo. It turns the windows into
full-length stem WAVs. Inside a window it uses the real stems. Outside every window, drums, bass and
vocals are silent and `other` carries the whole mix, so the stems always sum to the track and a deck
never goes quiet after its window. Its test runs the same decode as Android: it takes every sample,
priming included, from an AAC window served by a mocked server. The test then checks that each click
lands within 1 ms of its position in the phone's own decode, with a 1024-sample source offset applied
on purpose.

### Integrity (the host RAM is non-ECC)

* Every cache file is written to a temp file and then renamed into place.
* Demucs checkpoints are checked against the sha256 prefix in their file name before each split. A
  bad file is deleted, and Demucs downloads it again.
* Each stem set gets a sha256 sidecar (`.stem_server_sha256.json`). The first time the server sees
  a set it decodes it in full before writing the sidecar. Before a window is encoded, the stems are
  checked against the sidecar. On a mismatch the server deletes the set, starts a new split and
  returns 409.
* Each encoded window has a sha256 that is checked before it is served. On a mismatch the server
  re-encodes it.

### GPU sharing

One job runs at a time, in priority order, in ONE long-lived worker process (`app/stem_server/worker.py`,
started on the first job, exits after `STEM_WORKER_IDLE_S` idle so its VRAM goes back to the card). It
runs `app/music_brain/audio/stem_engine.py`: the model's 7.8 s segments batched on the GPU, fp16
autocast, the output streamed to FLAC, so RAM stays flat for any track length (a 2-hour set costs what a
song costs). The batch is picked per song from the VRAM free at that moment and the allocator cap
`STEM_SERVER_VRAM_GB` (~500 MB per segment); CUDA out of memory halves the batch, and at batch 1 the job
goes to the CPU (emergency only: ~7 min per song for htdemucs_ft). The job's `note` says why.
The worker's RAM is guarded twice (`app/stem_server/gpu.py`): a systemd scope with `STEM_JOB_MEMORY_MAX`
when set, and an RSS watchdog at `STEM_JOB_RSS_GB`; either kill hits the worker, never the server, and
the job is retried once at batch 1. The queue is saved to `data/cache/stem_server_jobs.json` on every
change and re-queued after a restart (a job that was running starts over). Numbers:
`docs/engineering/STEMS_PERF.md`; re-measure with `python -m app.stem_server.bench SONG ...`.
The device is picked automatically in the order cuda, mps, cpu, so the owner's Mac runs the same code
as a fallback host.

## Run locally (Mac fallback host)

```bash
export STEM_SERVER_TOKEN=$(openssl rand -hex 32)   # never commit or log it
STEM_SERVER_HOST=0.0.0.0 STEM_PORT=47830 .venv/bin/python -m app.stem_server
```

## Install on CT116 (Proxmox LXC, same checkout and venv as the app)

CT116 already has the RTX 3090 passed in (`/dev/nvidia0`, `nvidiactl`, `nvidia-uvm` and caps, gid
44). The host driver is NVIDIA 595.84.

1. Check the GPU from inside CT116: `nvidia-smi`. If it fails, install the matching **595.84
   userspace only**, with no kernel module:
   `./NVIDIA-Linux-x86_64-595.84.run --no-kernel-module`, or the distro's `libnvidia-compute` and
   `nvidia-utils` at exactly 595.84. Then run `nvidia-smi` again.
2. Install a CUDA build of torch into the app's venv. Keep the version the app already uses, so only
   the build changes:
   ```bash
   cd /opt/null-set-ai-dj
   V=$(.venv/bin/python -m pip show torch | awk '/^Version/{print $2}' | cut -d+ -f1)
   .venv/bin/python -m pip install --force-reinstall "torch==$V" torchaudio \
       --index-url https://download.pytorch.org/whl/cu130
   .venv/bin/python -m pip install -r requirements.txt   # demucs, fastapi, uvicorn, soundfile
   ```
   Use the `cuXYZ` index that pytorch.org lists for that torch version. Driver 595 runs any CUDA
   12.x or 13.x wheel. Run `apt install ffmpeg` if it is missing.
3. Create the env file:
   ```bash
   install -d -m 700 /etc/null-set
   install -m 600 -o root -g root deploy/stem-server.env.example /etc/null-set/stem-server.env
   # edit it: STEM_SERVER_TOKEN=$(openssl rand -hex 32), STEM_SERVER_HOST, STEM_PORT
   ```
4. Install the service. Set `User=` and `Group=` in the unit to the account that runs the console
   and owns `/opt/null-set-ai-dj` and `data/`:
   ```bash
   cp deploy/stem-server.service /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now stem-server
   journalctl -u stem-server -f
   curl http://127.0.0.1:47830/health
   ```
5. Optional: run the real-GPU test with `.venv/bin/python -m pytest app/tests/py/test_stem_server.py -m slow`.

### Network

**Primary mode: behind Caddy (TLS).** Set `STEM_SERVER_HOST=127.0.0.1`. Caddy, in the same CT,
terminates TLS on the public address and routes `/stems/*` here with the prefix stripped (see
`deploy/Caddyfile`). In the phone's settings the URL is `https://<domain>/stems`, and the phone
accepts only `https://` URLs. The server is never reachable except through Caddy.

Failed auth is limited per client IP with the console's limiter (`app/ui/services/auth.py`):
10 bad tokens in 10 minutes lock that IP out for 15 minutes, and a locked IP gets 429 until then.
The client IP is read from `X-Forwarded-For` only when the request comes from Caddy on
127.0.0.1 or ::1. Any other peer is counted by its own address.

*Secondary (testing only):* bind CT116's own address (`STEM_SERVER_HOST=10.0.0.116`) and reach it
over Tailscale, because the Proxmox host advertises 10.0.0.0/24. That path is plain HTTP, and
release builds of the phone refuse it. Never add a LAN port forward.

The token (at least 32 characters) is required in every mode.

CT116 has `onboot: 0`. If the phone should rely on the server, run `pct set 116 --onboot 1` on the
Proxmox host.

## Environment

| var | default | |
|---|---|---|
| `STEM_SERVER_TOKEN` | (required) | at least 32 chars; refuses to start without it |
| `STEM_SERVER_HOST` | 127.0.0.1 | bind address |
| `STEM_PORT` | 47830 | port |
| `STEM_SERVER_VRAM_GB` | 3.8 | CUDA allocator cap of the worker (the process adds ~0.3 GB of CUDA context) |
| `STEM_SERVER_HALF` | 1 | fp16 autocast on CUDA (0 = fp32) |
| `STEM_BATCH` | auto | fixed segments per forward instead of the VRAM-based pick |
| `STEM_WORKER_IDLE_S` | 120 | the worker exits (VRAM freed) after this long without a job |
| `STEM_JOB_MEMORY_MAX` | unset | e.g. `6G`: run the worker in a systemd scope with that MemoryMax (root + systemd-run) |
| `STEM_JOB_RSS_GB` | 6 | RSS watchdog: the worker is killed above this |
| `STEM_SERVER_STATE` | data/cache/stem_server_jobs.json | the saved queue |
| `STEM_WORK_DIR` | system tmp | where the decoded mix is memory-mapped (local disk, float32 stereo: ~0.64 GB per 30 min of audio) |
| `STEM_SERVER_MODEL` | htdemucs_ft | any 4-source Demucs model name; a RoFormer model can be swapped in here later |
| `STEM_SERVER_DEVICE` | auto | cuda / mps / cpu |
| `STEM_SERVER_MAX_UPLOAD_MB` | 200 | upload cap |
| `AIDJ_CACHE_DIR` | data/cache | shared with the console |
