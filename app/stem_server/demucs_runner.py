"""`python -m demucs` with the stem server's guards (stem_service.separate(runner=...) runs this).

- Model weights are checked against the sha256 prefix in their file name before loading (host RAM
  is non-ECC); a bad file is deleted so Demucs downloads it again.
- The CUDA allocator is capped at STEM_SERVER_VRAM_GB (the card is shared with other services).
- CUDA out of memory exits with OOM_EXIT so the server can say so and fall back to the CPU.
The process exits after one song, so all VRAM is released between jobs.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

OOM_EXIT = 75
_WEIGHT = re.compile(r"^[0-9a-f]+-([0-9a-f]{8,})\.th$")


def verify_weights(checkpoints: Path) -> list[str]:
    """Delete every Demucs checkpoint whose sha256 does not start with the hash in its name.
    -> names removed."""
    from app.stem_server.windows import sha256_file

    bad = []
    if not checkpoints.is_dir():
        return bad
    for f in sorted(checkpoints.glob("*.th")):
        m = _WEIGHT.match(f.name)
        if m and not sha256_file(f).startswith(m.group(1)):
            f.unlink(missing_ok=True)
            bad.append(f.name)
    return bad


def _cap_vram(torch) -> None:
    gb = float(os.environ.get("STEM_SERVER_VRAM_GB", "6"))
    if gb <= 0 or not torch.cuda.is_available():
        return
    total = torch.cuda.get_device_properties(0).total_memory
    torch.cuda.set_per_process_memory_fraction(min(1.0, gb * (1 << 30) / total), 0)


def main(argv: list[str]) -> int:
    import torch

    bad = verify_weights(Path(torch.hub.get_dir()) / "checkpoints")
    if bad:
        print(f"[stem-server] model weights failed sha256, re-downloading: {', '.join(bad)}", file=sys.stderr)
    if "cuda" in argv:
        _cap_vram(torch)
    from demucs.separate import main as demucs_main

    try:
        demucs_main(argv)
    except RuntimeError as exc:              # torch.cuda.OutOfMemoryError is a RuntimeError
        if "out of memory" not in str(exc).lower():
            raise
        print(f"[stem-server] CUDA out of memory: {exc}", file=sys.stderr)
        return OOM_EXIT
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
