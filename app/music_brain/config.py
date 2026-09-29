"""Central paths, cache directories, and model settings for the Music Brain package."""

import os
from pathlib import Path

# Repository root (this file lives at <root>/app/music_brain/config.py)
ROOT_DIR = Path(__file__).resolve().parent.parent.parent

# Ground-truth Obsidian DJ knowledge base
DJ_KB_DIR = ROOT_DIR / "DJ"
TRANSITION_COOKBOOK_DIR = DJ_KB_DIR / "05 - Transition Cookbook"
MUSIC_THEORY_DIR = DJ_KB_DIR / "02 - Music Theory"
CORE_TECHNIQUES_DIR = DJ_KB_DIR / "04 - Core Techniques"

# Persistent local cache (gitignored)
# AIDJ_CACHE_DIR points every cache (uploads, analysis, stems, sessions, yt_guard, ...) somewhere
# else: the virtual set (app/sim) runs on a private cache and never touches data/cache.
CACHE_DIR = Path(os.environ["AIDJ_CACHE_DIR"]).expanduser().resolve() if os.environ.get("AIDJ_CACHE_DIR") \
    else ROOT_DIR / "data" / "cache"
STEMS_CACHE_DIR = CACHE_DIR / "stems"
ANALYSIS_CACHE_DIR = CACHE_DIR / "analysis"
PREVIEWS_CACHE_DIR = CACHE_DIR / "previews"
SAMPLES_CACHE_DIR = CACHE_DIR / "samples"        # user-uploaded sampler one-shots
RECORDINGS_CACHE_DIR = CACHE_DIR / "recordings"  # MediaRecorder captures of live console mixes
SET_LOGS_CACHE_DIR = CACHE_DIR / "set_logs"      # validated djset-v1 JSON and Obsidian exports

# Acoustic thresholds referenced by DJ/04 - Core Techniques/EQ & Frequency Management.md
SUB_BASS_CROSSOVER_HZ = 120
VOCAL_PRESENCE_THRESHOLD_DBFS = -38.0

# Phrase structure per DJ/02 - Music Theory/Phrasing & Structure.md
BEATS_PER_BAR = 4
BARS_PER_PHRASE = 8
BEATS_PER_PHRASE = BEATS_PER_BAR * BARS_PER_PHRASE

# BPM compatibility thresholds per recipe_matcher scoring rules
BPM_SEAMLESS_PCT = 0.03
BPM_RAMP_PCT = 0.06

# Demucs model default (used by stem_service in Phase 2)
DEMUCS_MODEL = "htdemucs_ft"

for _dir in (
    STEMS_CACHE_DIR, ANALYSIS_CACHE_DIR, PREVIEWS_CACHE_DIR,
    SAMPLES_CACHE_DIR, RECORDINGS_CACHE_DIR, SET_LOGS_CACHE_DIR,
):
    _dir.mkdir(parents=True, exist_ok=True)
