# Stem separation: performance and design (CT116, RTX 3090)

All numbers were measured on CT116 on 2026-11-06 (8 cores, 16 GB RAM, RTX 3090 shared with the LLM in
CT113). Songs were normal-length uploads of 215 s and 229 s. Re-measure with
`python -m app.stem_server.bench SONG ... --what cli,warm,engine,enginehalf,exact,half,cpu`.

## Before / after (one 3.5-3.8 min song)

| path | model | wall time per song | notes |
|---|---|---|---|
| old stem server (`python -m demucs` per job) | htdemucs | 12.8-16.4 s | process start + model load every song, 2.4 GB RSS, GPU ~45 % busy |
| old stem server | htdemucs_ft | 32.6-33.3 s | 3.2 GB RSS; RSS grows with track length (13 GB on a 75 min file, OOM-killed) |
| old console warm worker (`stem_worker.py`) | htdemucs | 8.3 s | model resident forever on the GPU, a second GPU user |
| old console warm worker | htdemucs_ft | 21.8-24.9 s | same |
| **new engine, fp32, batch 8 (VRAM free)** | htdemucs | 3.9 s | |
| **new engine, fp32, batch 8 (VRAM free)** | htdemucs_ft | 9.1-9.9 s | |
| **new engine, fp16, batch 4, LLM loaded (~3.4 GB free)** | htdemucs_ft | **8.9-9.1 s** | this is the live setting |
| new engine on the deployed queue (backfill, real HDD pool) | htdemucs_ft | 7.6-14 s for 3-5.5 min songs | from the stem server log, decode + split + FLAC + copy to the pool |
| new engine, CPU, 8 threads | htdemucs_ft | 437 s | emergency fallback only |
| new engine, CPU, 4 / 8 threads | htdemucs | 90 s / 128 s | the 8-thread run competed with other load; CPU is 10-40x slower |

The target was htdemucs_ft in well under 20 s per song. The live setting does it in about 9 s, 3.6x
faster than the old stem server.

## VRAM (htdemucs_ft, per process)

| batch (7.8 s segments per forward) | torch allocated peak | allocator reserved peak | time (fp32 / fp16) |
|---|---|---|---|
| 1 | 1.05 GB | 1.75 GB | 20.7-25.6 s / 14.6-16.1 s |
| 2 | 1.43 GB | 1.83 GB | 14.0-15.9 s / 11.4-13.0 s |
| 3 | 1.82 GB | 2.47 GB | 11.1-12.6 s / 9.9-10.8 s |
| 4 | 2.20 GB | 2.97 GB | 10.8-12.8 s / 8.9-9.1 s |
| 8 | 3.73 GB | ~4.3 GB | 9.1-9.9 s |

The CUDA context adds about 0.3 GB per process on top of the reserved figure. Above batch 4 the 3090 is
nearly saturated (4 to 16 gains less than 15 %). fp16 autocast barely lowers peak memory because the STFT
and attention stay in fp32, but it is about 1.3x faster.

**The budget with the LLM loaded:** CT113 holds about 19.5-21.5 GB, which leaves 3-4.5 GB.
`STEM_SERVER_VRAM_GB=3.8` caps the worker's allocator, so its total is about 4.1 GB with the context.
The engine picks the batch per song from the free VRAM (`(free - 0.4 GB) / 0.5 GB`, clamped to 1-8).
On the live system that comes out as batch 5. A CUDA out-of-memory error halves the batch for the rest
of that song. At batch 1 the job falls back to the CPU and the job's `note` says why. A model of its own
is only ever loaded in the stem server's worker. The console in stem-server mode loads none, and the
worker exits after 120 s idle so its VRAM goes back to the card.

## Quality

* **Matches Demucs exactly:** the streaming engine computes the same thing as whole-track
  `apply_model(shifts=0, split=True, overlap=0.25)`. In true fp32 the residual against Demucs's own
  output on the same decoded audio is -124 dB (max abs diff 4e-6, htdemucs_ft). With TF32 on, which
  is PyTorch's default on Ampere, both differ from each other by about -73 dB, the TF32 rounding.
  There are no chunk seams beyond Demucs's own overlap-add. The tests (`test_stem_engine.py`) check
  this at many lengths and batch sizes.
* **fp16 vs fp32 (SDR of the fp16 stem against the fp32 stem, 2 songs):** htdemucs_ft drums
  56.3-56.4 dB, bass 57.0-65.1 dB, other 56.1-56.5 dB, vocals 58.8-61.1 dB. htdemucs is similar
  (55.9-64.2 dB). A difference 56+ dB down is far below what the model's own separation error (SDR
  about 9 dB) can show, so fp16 is on (`STEM_SERVER_HALF=1`). This was a quality-vs-speed call, and
  the measurement says it costs no audible quality.
* **shifts=0:** Demucs's CLI default `shifts=1` is one random time offset, not an average, so it does not
  improve expected quality. With 0 a re-split is deterministic.
* **Clipping:** samples beyond full scale are clamped per sample (about 150-1,150 per song). The Demucs
  CLI instead rescales the whole stem when its peak passes 1.0, which changes its level and breaks
  "stems sum to the mix".
* **Model:** everything served is htdemucs_ft. A cheaper htdemucs set already in the cache is served
  until the ft set is written and verified (all four stems the length of the decoded mix). After that
  `_auto_prune` removes the htdemucs copy, so nothing is deleted before its replacement exists.

## Memory: any length

The input is decoded once by ffmpeg into a float32 raw file in the local work dir and memory-mapped.
Output blocks are FLAC-encoded in the work dir and copied to the HDD pool in one sequential pass at the
end. Encoding straight onto the mergerfs pool cost up to about 25 s extra per song. RSS stays flat at
about 2 GB (mostly the CUDA runtime) whatever the track length:

* 75 min set recording (065028eaec446431, 4527 s): split at deck priority in 194 s, peak RSS 3.3 GB, no OOM. Before, it was OOM-killed at 13 GB.

Long files are split like any other. The engine has no length limit and the queue is per-song, so a
set recording only takes longer (194 s for the 75 min file). It runs at
the priority it was asked for. Nothing is refused for length because the memory no longer depends on it.

Guard rails: the worker runs in a systemd scope with `MemoryMax=6G` (`STEM_JOB_MEMORY_MAX`), and the
server also kills it above 6 GB RSS (`STEM_JOB_RSS_GB`). A memory kill hits the worker only. The job is
retried once at batch 1, and after that it fails with the reason. The CT limit is 16 GB.

## Queue, latency and the console flow

* The console calls `GET /api/tracks/{id}/stems?separate=1` (deck load, tempo stems, super-move) and
  `/vocals?separate=1`. In stem-server mode (`STEM_SERVER_URL`) those go to the stem server at `deck`
  priority. The library backfill goes at `backfill` priority, 2 jobs handed over at a time and only while
  the LLM is idle (the old rule). The phone gets `phone` priority. Order: deck/booked > phone > backfill.
* What a user waits for a deck song with no stems: the rest of the split in progress (at most one song,
  about 9 s) plus its own split (about 9 s), so **about 10-20 s**. Before, a deck song queued behind the
  console's own worker (8-25 s per song). On the stem server path it could wait 33 s per queued phone
  job, plus an OOM restart when a long file was in the queue.
* Status: `GET /v1/jobs` and `GET /v1/jobs/{job}` give position, real progress (segments done), elapsed,
  ETA, device and model. The console's STEMS badge shows them as one line, for example
  `htdemucs_ft · cuda · 42% · 4 s, ~6 s left · 12 queued`.
* Crash safety: the queue is saved to `data/cache/stem_server_jobs.json` on every change and re-queued
  after a restart with the same job ids. This was verified on CT116, where a restart re-queued 2 jobs
  and both finished. A job that was running starts over, because its stems are written atomically and
  a half-written set is never visible.
* No LLM (ollama down): `_llm_busy()` is false when the gate has nothing in flight, so the backfill
  simply runs.

## Re-split to htdemucs_ft

At startup in stem-server mode the console queues every library song without an htdemucs_ft set at
backfill priority. That covers the new tracks and the older htdemucs-only sets: about 330 songs at about
10 s each, under an hour. They are done one at a time, 2 parked at the server, and only while the LLM is
idle.
