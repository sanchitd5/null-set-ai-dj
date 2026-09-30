"""Re-analyse the library's structure: every <hash>.v5.json -> <hash>.v6.json.

v5 -> v6 changes only sections / drops / main drop (analysis/structure.py), derived from the
stored phrase grid and energy curve plus the cached drums+bass stems. No mix decode, no Demucs,
no network. Idempotent and resumable: a song that already has its v6 record is skipped, and each
record is written atomically (tmp + replace), so a stopped run just continues next time. The v5
records are left in place. Run with the app stopped:

    python3 -m app.music_brain.analysis.reanalyse --all [--jobs N] [--dry-run] [--force]

AIDJ_CACHE_DIR points it at another cache (a trial copy). Prints one JSON summary line.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import List, Optional


def _one(job: tuple) -> dict:
    src, dst, digest, dry = job
    from app.music_brain.analysis import analyzer

    t0 = time.time()
    try:
        data = json.loads(Path(src).read_text(encoding="utf-8"))
        rec = analyzer._refine(data, digest)
        if not dry:
            analyzer._write_json(Path(dst), rec)
        return {"digest": digest, "ok": True, "drops": len(rec["drops"]), "stems": rec["structure"]["stems"],
                "s": time.time() - t0}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"digest": digest, "ok": False, "error": f"{type(exc).__name__}: {exc}", "s": time.time() - t0}


def pending(adir: Path, force: bool = False) -> List[tuple]:
    """(v5 path, v6 path, digest) for every v5 record without a v6 one (all of them with force)."""
    out = []
    for p in sorted(adir.glob("*.v5.json")):
        digest = p.name.split(".")[0]
        dst = adir / f"{digest}.v6.json"
        if force or not dst.exists():
            out.append((str(p), str(dst), digest))
    return out


def run(all_: bool, jobs: int = 4, dry: bool = False, force: bool = False, limit: Optional[int] = None,
        log=print) -> dict:
    from app.music_brain.config import ANALYSIS_CACHE_DIR

    if not all_:
        raise SystemExit("nothing to do: pass --all")
    todo = pending(ANALYSIS_CACHE_DIR, force)[:limit]
    t0 = time.time()
    res: List[dict] = []
    work = [(s, d, g, dry) for s, d, g in todo]
    if jobs <= 1 or len(work) <= 1:
        res = [_one(w) for w in work]
    else:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            for i, r in enumerate(ex.map(_one, work, chunksize=4), 1):
                res.append(r)
                if i % 50 == 0:
                    log(f"[reanalyse] {i}/{len(work)}", file=sys.stderr, flush=True)
    bad = [r for r in res if not r["ok"]]
    return {"cache": str(ANALYSIS_CACHE_DIR), "done": len(res) - len(bad), "failed": len(bad),
            "errors": bad[:10], "no_drop": sum(1 for r in res if r["ok"] and not r["drops"]),
            "with_stems": sum(1 for r in res if r["ok"] and r["stems"]), "dry_run": dry,
            "seconds": round(time.time() - t0, 1),
            "per_track_s": round(sum(r["s"] for r in res) / max(1, len(res)), 3)}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true", help="every song with a v5 record and no v6 one")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="compute, write nothing")
    ap.add_argument("--force", action="store_true", help="redo songs that already have a v6 record")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)
    out = run(a.all, max(1, a.jobs), a.dry_run, a.force, a.limit)
    print(json.dumps(out))
    return 1 if out["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
