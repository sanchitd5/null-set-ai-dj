"""The stem server's side of the GPU worker (app/stem_server/worker.py): start it on demand, guard its
memory, and walk the retry ladder.

Memory guard (two layers, either is enough to keep the service alive):
* STEM_JOB_MEMORY_MAX (e.g. "6G"): when systemd-run exists and we are root, the worker runs in its own
  transient scope with that MemoryMax (and no swap), so the kernel kills the worker, never the service.
* STEM_JOB_RSS_GB (default 6): this process polls the worker's RSS every 250 ms and kills it above that.
The streaming engine keeps a split's RAM flat in track length (~2-3 GB, mostly the CUDA runtime), so
either limit firing means something is wrong; the job is retried once at batch 1 and reported.

Retry ladder for one split: device as configured -> (GPU out of memory even at batch 1) the CPU;
worker killed for memory -> once more at batch 1; worker crashed -> once more on a fresh worker.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

REPO = Path(__file__).resolve().parents[2]


class WorkerDied(RuntimeError):
    def __init__(self, msg: str, memory: bool = False):
        super().__init__(msg)
        self.memory = memory


def rss_bytes(pid: Optional[int]) -> int:
    """Resident set size of a process (Linux /proc); 0 when unknown."""
    if not pid:
        return 0
    try:
        with open(f"/proc/{pid}/status", "r", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


class GpuWorker:
    def __init__(self, rss_limit: int = 0, scope_mem: Optional[str] = None, cmd: Optional[list] = None,
                 ready_timeout: float = 300.0):
        self.rss_limit, self.scope_mem = rss_limit, scope_mem
        self.cmd = cmd or [sys.executable, "-m", "app.stem_server.worker"]
        self.ready_timeout = ready_timeout
        self.p: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self.peak_rss = 0

    @property
    def alive(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def _command(self) -> list:
        if self.scope_mem and shutil.which("systemd-run") and hasattr(os, "geteuid") and os.geteuid() == 0:
            return ["systemd-run", "--scope", "--quiet", "--collect", "-p", f"MemoryMax={self.scope_mem}",
                    "-p", "MemorySwapMax=0", "--"] + self.cmd
        return list(self.cmd)

    def start(self) -> None:
        self.stop()
        self.lines = queue.Queue()
        self.p = subprocess.Popen(self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                  bufsize=1, cwd=str(REPO))
        lines, out = self.lines, self.p.stdout

        def read() -> None:
            for ln in out:
                lines.put(ln)
            lines.put(None)

        threading.Thread(target=read, daemon=True, name="stem-worker-out").start()
        while True:
            try:
                ln = self.lines.get(timeout=self.ready_timeout)
            except queue.Empty:
                self.stop()
                raise WorkerDied("worker did not start")
            if ln is None:
                raise WorkerDied(f"worker exited at start ({self.p.wait()})")
            msg = _parse(ln)
            if msg and msg.get("ready"):
                self.pid = int(msg.get("pid") or self.p.pid)
                return

    def stop(self) -> None:
        if self.p is not None and self.p.poll() is None:
            self.p.kill()
            try:
                self.p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        if self.pid and self.pid != getattr(self.p, "pid", None):
            try:
                os.kill(self.pid, 9)                     # the scope's child, when systemd-run sat between us
            except OSError:
                pass
        self.p, self.pid = None, None

    def run(self, msg: dict, on_progress: Callable[..., None]) -> dict:
        """Send one job; -> the worker's result message. Raises WorkerDied."""
        if not self.alive:
            self.start()
        try:
            self.p.stdin.write(json.dumps(msg) + "\n")
            self.p.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.stop()
            raise WorkerDied(f"worker gone ({exc})") from exc
        self.peak_rss = 0
        while True:
            try:
                ln = self.lines.get(timeout=0.25)
            except queue.Empty:
                ln = ""
            if ln is None:
                rc = self.p.wait() if self.p else None
                self.stop()
                raise WorkerDied(f"worker died (exit {rc})", memory=rc in (-9, 137))
            m = _parse(ln) if ln else None
            if m and m.get("job") == msg["job"]:
                if "progress" in m and "ok" not in m:
                    on_progress(float(m["progress"]), m.get("device"))
                elif "ok" in m:
                    m["peak_rss"] = self.peak_rss
                    return m
            rss = rss_bytes(self.pid)
            self.peak_rss = max(self.peak_rss, rss)
            if self.rss_limit and rss > self.rss_limit:
                self.stop()
                raise WorkerDied(f"split used {rss >> 20} MB RAM, over the {self.rss_limit >> 20} MB job limit",
                                 memory=True)


def _parse(ln: str) -> Optional[dict]:
    try:
        m = json.loads(ln)
        return m if isinstance(m, dict) else None
    except ValueError:
        return None


def split(worker: GpuWorker, job: str, path: Path, out_dir: Path, model: str, device: Optional[str],
          on_progress: Callable[..., None], half: bool = False) -> tuple[dict, Optional[str], Optional[str]]:
    """One split through the retry ladder (device None: the worker picks). on_progress(fraction, device).
    -> (engine info, device asked for, note or None)."""
    notes: list[str] = []
    batch: Optional[int] = None
    crashed = memory = False
    for _ in range(5):
        on_progress(0.0)
        try:
            r = worker.run({"job": job, "path": str(path), "out_dir": str(out_dir), "model": model,
                            "device": device, "batch": batch, "half": half}, on_progress)
        except WorkerDied as exc:
            if exc.memory and not memory:
                memory, batch = True, 1
                notes.append(f"{exc}; retried at batch 1")
                print(f"[stem-server] {job}: {exc}; retrying at batch 1", file=sys.stderr, flush=True)
                continue
            if not exc.memory and not crashed:
                crashed = True
                notes.append(f"{exc}; retried on a fresh worker")
                continue
            raise RuntimeError("; ".join(notes + [str(exc)])) from exc
        if r.get("ok"):
            info = r["info"]
            info["peak_rss_mb"] = r.get("peak_rss", 0) >> 20
            return info, device, "; ".join(notes) or None
        if r.get("oom") and device != "cpu":
            notes.append(f"{r.get('error')}; split on CPU")
            print(f"[stem-server] {job}: GPU out of memory at batch 1; retrying on CPU", file=sys.stderr, flush=True)
            device, batch = "cpu", None
            continue
        raise RuntimeError(r.get("error") or "split failed")
    raise RuntimeError("; ".join(notes) or "split failed")
