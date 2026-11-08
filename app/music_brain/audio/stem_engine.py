"""Streaming Demucs separation: any length in bounded memory, segments batched on the GPU.

What `demucs.apply.apply_model(model, mix, shifts=0, split=True, overlap=0.25)` computes, done as a
stream (docs/engineering/STEMS_PERF.md has the numbers):

* The input is decoded ONCE by ffmpeg (44.1 kHz stereo float32) into a raw file in a local work dir
  and memory-mapped, so a 2-hour set costs the same RAM as a 3-minute song.
* Global normalisation as Demucs does it (mean / std of the mono mix, std + 1e-8), measured in a
  streaming pass.
* The track is cut into the model's own 7.8 s segments at a 75 % stride, each segment padded the way
  `TensorChunk.padded` pads it (centred, real neighbouring audio, zeros only outside the track), and
  overlap-added with Demucs' triangle weights. A bag of models (htdemucs_ft) is combined with the bag's
  per-stem weights. No second level of chunking exists, so there are no extra seams: the output equals
  the whole-track apply_model up to float rounding (app/tests/py/test_stem_engine.py checks it).
* Segments run in batches of `batch` on the device; a sample is final once no later segment can touch
  it, and final samples stream straight to 16-bit FLAC (tmp file + os.replace) on a writer thread.

shifts=0: Demucs' default shifts=1 is ONE random time offset, not an average, so it does not raise the
expected quality; 0 makes a split deterministic (a re-split is bit-identical).
Clipping: samples beyond full scale are clamped per sample. The Demucs CLI instead rescales a whole stem
when its peak passes 1.0, which changes that stem's level and breaks "stems sum to the mix".
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Callable, Dict, Optional

import numpy as np

SAMPLE_RATE = 44100
CHANNELS = 2
OVERLAP = 0.25
# Measured on the 3090 (docs/engineering/STEMS_PERF.md): the allocator's reserved peak grows ~500 MB per
# 7.8 s segment in a batch (allocated ~385 MB + fragmentation), fp32 and fp16 autocast alike (the STFT and
# attention stay fp32), htdemucs and each htdemucs_ft sub-model alike (they run one after another).
#
# The segment length itself is the model's trained 7.8 s (the transformer's positional layers expect it, a
# different length changes the output), so the knob that fits a job to the free VRAM is the batch: how many
# segments go through one forward. Per batch: 1 -> ~0.9 GB, 2 -> 1.4, 4 -> 2.4, 8 -> 4.4 GB (+ the model's
# weights while they sit on the GPU, ~1 GB for htdemucs_ft). All three numbers are env-tunable.
SEG_MB = int(os.environ.get("STEM_SEG_MB", "500"))
VRAM_RESERVE_MB = int(os.environ.get("STEM_VRAM_RESERVE_MB", "400"))   # allocator slack + cuFFT / cuDNN workspaces
VRAM_MARGIN_MB = int(os.environ.get("STEM_VRAM_MARGIN_MB", "300"))     # left free for the other tenants' jitter
MAX_BATCH = 8                             # past 8 the 3090 is saturated (8 -> 16: <5 % faster)


class GpuOutOfMemory(RuntimeError):
    """The device ran out of memory even at batch 1 (or the wait for free VRAM ran out: wait_expired)."""

    wait_expired = False


def _is_oom(exc: BaseException) -> bool:
    return "out of memory" in str(exc).lower()


def usable_mb(torch) -> tuple[float, float]:
    """(VRAM this process can still take, card total), MB: the card's free memory, held to the
    STEM_SERVER_VRAM_GB cap minus what this process already reserves."""
    free, total = torch.cuda.mem_get_info()
    frac = torch.cuda.get_per_process_memory_fraction(0) if hasattr(torch.cuda, "get_per_process_memory_fraction") else 1.0
    if frac < 1.0:
        free = min(free, frac * total - torch.cuda.memory_reserved(0))
    return free / 2**20, total / 2**20


def wait_for_vram(torch, need_mb: float, wait_s: float, poll_s: float = 2.0,
                  sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                  log: Callable[[str], None] = lambda s: None) -> tuple[float, float, float]:
    """Poll until `need_mb` is usable, at most `wait_s`. -> (seconds waited, MB usable when it started
    waiting, card MB). Raises GpuOutOfMemory (wait_expired) when it never frees up."""
    t0 = clock()
    free, total = usable_mb(torch)
    if free >= need_mb:
        return 0.0, free, total
    log(f"GPU has {free:.0f} MB free, a split needs {need_mb:.0f} MB: waiting up to {wait_s:g}s")
    first = free
    while clock() - t0 < wait_s:
        sleep(poll_s)
        free, total = usable_mb(torch)
        if free >= need_mb:
            waited = clock() - t0
            log(f"GPU freed up after {waited:.0f}s ({free:.0f} MB free, needs {need_mb:.0f} MB)")
            return waited, first, total
    err = GpuOutOfMemory(f"GPU wait expired after {wait_s:g}s: {free:.0f} MB free, needs {need_mb:.0f} MB")
    err.wait_expired = True
    raise err


def decode(src: Path | str, dest: Path | str, sr: int = SAMPLE_RATE) -> int:
    """ffmpeg -> interleaved float32 stereo raw file at `sr`. -> frames."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", str(src),
           "-map", "0:a:0", "-ac", str(CHANNELS), "-ar", str(sr), "-f", "f32le", "-c:a", "pcm_f32le", str(dest)]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg could not decode {Path(src).name}: {r.stderr.decode(errors='replace')[-300:]}")
    frames = Path(dest).stat().st_size // (4 * CHANNELS)
    if frames <= 0:
        raise RuntimeError(f"{Path(src).name}: no audio")
    return frames


def mix_stats(x: np.ndarray, block: int = 1 << 20) -> tuple[float, float]:
    """Mean and unbiased std (+1e-8) of the mono mix, as Demucs normalises; streamed in blocks."""
    n = x.shape[0]
    s = ss = 0.0
    for i in range(0, n, block):
        m = x[i:i + block].astype(np.float64).mean(axis=1)
        s += float(m.sum())
        ss += float(np.dot(m, m))
    mean = s / n
    var = max(0.0, (ss - n * mean * mean) / max(1, n - 1))
    return mean, float(np.sqrt(var)) + 1e-8


def triangle(seg: int) -> np.ndarray:
    """apply_model's overlap-add weight (transition_power 1)."""
    w = np.concatenate([np.arange(1, seg // 2 + 1), np.arange(seg - seg // 2, 0, -1)]).astype(np.float32)
    return w / w.max()


def padded_segment(x: np.ndarray, offset: int, seg: int, mean: float = 0.0, std: float = 1.0
                   ) -> tuple[np.ndarray, int, int]:
    """TensorChunk((x - mean) / std, offset, seg).padded(seg): (input (2, seg), trim start, valid
    length). Normalised before padding: the zeros outside the track are zeros of the normalised mix."""
    n = x.shape[0]
    length = min(seg, n - offset)
    delta = seg - length
    start = offset - delta // 2
    lo, hi = max(0, start), min(n, start + seg)
    out = np.zeros((CHANNELS, seg), dtype=np.float32)
    out[:, lo - start:hi - start] = (x[lo:hi].T - mean) / std
    return out, delta // 2, length


def to_pcm16(y: np.ndarray) -> tuple[np.ndarray, int]:
    """float (frames, ch) -> int16, clamped per sample. -> (pcm, clipped sample count)."""
    s = np.rint(y * 32768.0)
    clipped = int(np.count_nonzero((s > 32767) | (s < -32768)))
    return np.clip(s, -32768, 32767).astype(np.int16), clipped


class StemEngine:
    """One model (or bag) loaded once on one device; `separate()` runs any number of songs."""

    def __init__(self, model: str, device: str, half: bool = False, batch: Optional[int] = None,
                 threads: Optional[int] = None, loaded=None, resident: bool = True):
        import torch

        self.torch = torch
        self.name, self.device = model, device
        if threads and device == "cpu":
            torch.set_num_threads(threads)
        if loaded is None:
            from demucs.pretrained import get_model
            loaded = get_model(model)
        subs = list(loaded.models) if hasattr(loaded, "models") else [loaded]
        weights = loaded.weights if hasattr(loaded, "models") else [[1.0] * len(loaded.sources)]
        segs = {float(m.segment) for m in subs}
        if len(segs) != 1:
            raise ValueError(f"{model}: sub-models with different segments {sorted(segs)} are not supported")
        self.sources = list(loaded.sources)
        self.samplerate = int(loaded.samplerate)
        self.seg = int(self.samplerate * subs[0].segment)        # apply_model: int(samplerate * segment)
        self.stride = int((1 - OVERLAP) * self.seg)
        self.weight = triangle(self.seg)
        totals = np.sum(np.asarray(weights, dtype=np.float64), axis=0)
        self.mix_w = [torch.tensor((np.asarray(w, dtype=np.float64) / totals).astype(np.float32)) for w in weights]
        self.models = [m.eval() for m in subs]
        self.mix_w = [w.view(1, -1, 1, 1) for w in self.mix_w]
        self.model_mb = sum(p.numel() * p.element_size() for m in subs for p in m.parameters()) / 2**20
        self.on_device = False
        if resident:
            self.acquire()
        self.half = bool(half and device == "cuda")
        # fixed batch (argument / STEM_BATCH), else picked per song from the VRAM free right then
        self.fixed_batch = int(batch or int(os.environ.get("STEM_BATCH", "0")) or 0) or None
        self.batch = self.fixed_batch or (MAX_BATCH if device == "cuda" else 1)

    # ---- weights on / off the device ------------------------------------------------------------
    def acquire(self) -> None:
        """Weights onto the device (no-op when they are there). May raise the device's OOM."""
        if self.on_device:
            return
        self.models = [m.to(self.device) for m in self.models]
        self.mix_w = [w.to(self.device) for w in self.mix_w]
        self.on_device = True

    def release(self) -> None:
        """Weights back to host RAM and the allocator's cache emptied, so an idle worker holds only the
        CUDA context (~300 MB) instead of the model (~1 GB). No-op on the CPU."""
        if self.device == "cpu" or not self.on_device:
            return
        self.models = [m.to("cpu") for m in self.models]
        self.mix_w = [w.to("cpu") for w in self.mix_w]
        self.on_device = False
        if self.device == "cuda":
            self.torch.cuda.empty_cache()

    def min_need_mb(self) -> float:
        """VRAM the smallest split (batch 1) needs: its segment, the allocator reserve, the jitter margin,
        and the weights when they are not on the card yet."""
        return SEG_MB + VRAM_RESERVE_MB + VRAM_MARGIN_MB + (0.0 if self.on_device else self.model_mb)

    def pick_batch(self) -> int:
        """Segments per forward that fit the VRAM usable now (the card is shared with the LLM, which
        can hold all but ~3 GB of it): (usable - reserve - margin) / measured MB per segment, 1..MAX_BATCH.
        A wrong guess costs nothing worse than the OOM backoff in _run_batch."""
        if self.fixed_batch:
            return self.fixed_batch
        if self.device != "cuda":
            return 1
        n = int((usable_mb(self.torch)[0] - VRAM_RESERVE_MB - VRAM_MARGIN_MB) // SEG_MB)
        return max(1, min(MAX_BATCH, n))

    # ---- one batch of segments ------------------------------------------------------------------
    def _forward(self, xb: np.ndarray) -> np.ndarray:
        torch = self.torch
        t = torch.from_numpy(xb).to(self.device)
        amp = torch.autocast("cuda", dtype=torch.float16) if self.half else nullcontext()
        with torch.inference_mode(), amp:
            out = None
            for m, w in zip(self.models, self.mix_w):
                y = m(t).float() * w
                out = y if out is None else out + y
        return out.cpu().numpy()

    def _run_batch(self, xb: np.ndarray) -> np.ndarray:
        """Forward with OOM backoff: halve the batch (kept for the rest of the run), GpuOutOfMemory at 1."""
        while True:
            try:
                if len(xb) <= self.batch:
                    return self._forward(xb)
                return np.concatenate([self._forward(xb[i:i + self.batch]) for i in range(0, len(xb), self.batch)])
            except RuntimeError as exc:
                if not _is_oom(exc):
                    raise
                oom_msg = str(exc)[:200]
            # outside the except block: the failed forward's tensors are only freed once its traceback is gone
            if self.device == "cuda":
                import gc

                gc.collect()
                self.torch.cuda.empty_cache()
            if self.batch <= 1:
                raise GpuOutOfMemory(oom_msg)
            nxt = max(1, self.batch // 2)
            print(f"[stem-engine] out of memory at batch {self.batch}; retrying at batch {nxt}", file=sys.stderr, flush=True)
            self.batch = nxt

    # ---- whole song -----------------------------------------------------------------------------
    def separate_array(self, x: np.ndarray, sink: Callable[[np.ndarray], None],
                       progress: Optional[Callable[[float], None]] = None) -> None:
        """x: (frames, 2) float32 (a memmap is fine). sink gets (sources, frames_block, 2) float32 blocks
        in order, covering every frame exactly once."""
        n = x.shape[0]
        mean, std = mix_stats(x)
        seg, stride, S = self.seg, self.stride, len(self.sources)
        offsets = list(range(0, n, stride))
        step = self.batch
        cap = (step - 1) * stride + seg
        acc = np.zeros((S, CHANNELS, cap), dtype=np.float32)
        wsum = np.zeros(cap, dtype=np.float32)
        base = 0
        i = 0
        while i < len(offsets):
            group = offsets[i:i + step]
            parts = [padded_segment(x, o, seg, mean, std) for o in group]
            yb = self._run_batch(np.stack([p[0] for p in parts]))                  # (B, S, 2, seg)
            for (_, trim, length), o, y in zip(parts, group, yb):
                a = o - base
                acc[:, :, a:a + length] += self.weight[:length] * y[:, :, trim:trim + length]
                wsum[a:a + length] += self.weight[:length]
            i += len(group)
            end = min(n, offsets[i]) if i < len(offsets) else n
            k = end - base
            block = acc[:, :, :k] / wsum[:k]
            sink(np.ascontiguousarray((block * std + mean).transpose(0, 2, 1)))
            rest = cap - k
            acc[:, :, :rest] = acc[:, :, k:].copy()
            acc[:, :, rest:] = 0.0
            wsum[:rest] = wsum[k:].copy()
            wsum[rest:] = 0.0
            base = end
            step = min(step, self.batch)                                 # OOM backoff only ever shrinks it
            if progress:
                progress(min(1.0, i / len(offsets)))

    def separate(self, src: Path | str, out_dir: Path | str, work_dir: Optional[str] = None,
                 progress: Optional[Callable[[float], None]] = None, batch: Optional[int] = None) -> dict:
        """Separate an audio file into out_dir/<stem>.flac (16-bit, atomic). batch None: pick_batch().
        -> info dict."""
        import soundfile as sf

        from app.music_brain.audio.audio_io import _SF_LEVEL

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        self.batch = int(batch or self.pick_batch())
        batch0 = self.batch
        tmpdir = Path(tempfile.mkdtemp(prefix="stems_", dir=work_dir or os.environ.get("STEM_WORK_DIR") or None))
        files, tmps = {}, {}
        q: "queue.Queue" = queue.Queue(maxsize=4)
        err: list = []
        clipped = [0]
        try:
            raw = tmpdir / "mix.f32"
            n = decode(src, raw, self.samplerate)
            t_decode = time.monotonic() - t0
            x = np.memmap(raw, dtype=np.float32, mode="r", shape=(n, CHANNELS))
            for name in self.sources:
                # encoded in the local work dir: many small FLAC writes onto the FUSE/HDD pool cost
                # ~50 s a song on CT116; one sequential copy per stem at the end costs ~1 s
                tmps[name] = out_dir / f".{name}.flac.tmp{os.getpid()}"
                files[name] = sf.SoundFile(str(tmpdir / f"{name}.flac"), "w", samplerate=self.samplerate, channels=CHANNELS,
                                           format="FLAC", subtype="PCM_16", compression_level=_SF_LEVEL)

            def writer():
                while True:
                    blk = q.get()
                    if blk is None:
                        return
                    if err:
                        continue
                    try:
                        for k, name in enumerate(self.sources):
                            pcm, c = to_pcm16(blk[k])
                            clipped[0] += c
                            files[name].write(pcm)
                    except Exception as exc:  # noqa: BLE001 -- surfaced after the run
                        err.append(exc)

            wt = threading.Thread(target=writer, daemon=True, name="stem-writer")
            wt.start()
            try:
                self.separate_array(x, q.put, progress)
            finally:
                q.put(None)
                wt.join()
            if err:
                raise err[0]
            for f in files.values():
                f.close()
            written = {}
            for name in self.sources:
                shutil.copyfile(tmpdir / f"{name}.flac", tmps[name])
            for name in self.sources:
                dest = out_dir / f"{name}.flac"
                os.replace(tmps[name], dest)
                written[name] = str(dest)
            return {"stems": written, "frames": n, "seconds": round(time.monotonic() - t0, 2),
                    "decode_seconds": round(t_decode, 2), "clipped_samples": clipped[0],
                    "device": self.device, "model": self.name, "half": self.half, "batch": self.batch,
                    "batch_start": batch0}
        finally:
            for f in files.values():
                if not f.closed:
                    f.close()
            for p in tmps.values():
                p.unlink(missing_ok=True)
            shutil.rmtree(tmpdir, ignore_errors=True)
