"""The repo is public: no tracked file may carry a real home directory path."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
# test data uses placeholder homes (/Users/me, /Users/x, /Users/someone) on purpose
PLACEHOLDER = {"me", "x", "someone"}
HOME = re.compile(r"(?:/Users|/home)/([A-Za-z0-9._-]+)/")


def test_no_real_home_paths_in_tracked_files():
    files = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).split("\n")
    hits = []
    for rel in filter(None, files):
        p = ROOT / rel
        if not p.is_file() or p.stat().st_size > 5_000_000:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in HOME.finditer(text):
            if m.group(1) not in PLACEHOLDER:
                hits.append(f"{rel}: {m.group(0)}")
    assert not hits, "real home paths in tracked files:\n" + "\n".join(hits[:20])
