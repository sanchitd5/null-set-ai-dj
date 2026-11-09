# GENERATED from dj-config c56fa33 - do not edit
# config-hash: 0ea7eccf825e
"""Generated from dj-config/config/*.json. Do not edit; run dj-config/tool/gen.py."""

SHA = "c56fa33"
CONFIG_HASH = "0ea7eccf825e"

ARC_TARGET = {"warm-up": 4, "build": 6, "peak": 8, "cool-down": 5}  # level
BARS_PER_PHRASE = 8  # bars
BEATS_PER_BAR = 4  # beats
BIG_COOLDOWN_S = 240  # s
BOOK_AFTER_LOAD_S = 6  # s
BOOK_GIVE_UP_S = 25.0  # s
BOOK_LEAD_S = 75.0  # s
BYPASS_KEY_SCORE = 0.4  # score
CAMELOT_TABLE = {"same": 1, "hour": 0.9, "relative": 0.85, "up2": 0.8, "diagonal": 0.75, "down2": 0.6, "two_flip": 0.3, "clash": 0}
CAMELOT_TABLE_LEARNED = {"same": 1, "hour": 0.9, "relative": 0.85, "up2": 0.8, "diagonal": 0, "down2": 0.8, "two_flip": 0, "clash": 0}
DEFER_MIN_LEAD_S = 45.0  # s
ELECTRONIC_CLUSTERS = {"house": ["house", "deep house", "tech house", "bass house", "synth house", "disco house", "afro house", "latin house", "funky house", "uk garage", "garage", "speed garage"], "melodic": ["melodic", "melodic techno", "melodic house", "progressive house", "progressive", "organic house"], "trance": ["trance", "psytrance", "uplifting trance"], "techno": ["techno", "hard techno", "minimal techno", "industrial techno", "acid techno"], "bass": ["dubstep", "melodic dubstep", "bass music", "bass house", "future bass", "riddim", "brostep"], "dnb": ["drum and bass", "drum & bass", "dnb", "jungle", "liquid", "neurofunk"], "chill": ["downtempo", "ambient", "chillout", "chill", "trip hop", "idm", "electronica"], "edm": ["edm", "big room", "electro", "electro house", "eurodance", "dance"], "breaks": ["breakbeat", "breaks", "big beat"]}
ENERGY_MATCH_TOL = 3  # levels
ENERGY_MAX_STEP = 2  # levels
ENERGY_MIN_RAW = 0.1  # ratio
ENERGY_RELAXED_STEP = 1  # levels
EQ_BOOST_MAX_DB = 6  # dB
EQ_HIGH_HZ = 5000  # Hz
EQ_HIGH_SWEEP_DB = -18  # dB
EQ_KILL_DB = -26.0  # dB
EQ_LOW_HZ = 120  # Hz
EQ_MID_HZ = 1000  # Hz
EQ_MID_Q = 0.7
FX_CRUSHER_LEVELS = 8  # levels
FX_CRUSHER_TONE_HZ = 6000  # Hz
FX_DRY_LAW = 0.85  # ratio
FX_ECHO_BEATS = 0.75  # beats
FX_ECHO_FEEDBACK = 0.45  # ratio
FX_ECHO_TONE_HZ = 4500  # Hz
FX_FILTER_CENTER_HZ = 3000  # Hz
FX_FILTER_DEPTH_HZ = 2600  # Hz
FX_FILTER_LFO_HZ = 0.25  # Hz
FX_FILTER_Q = 8  # dB
FX_FLANGER_DELAY_S = 0.005  # s
FX_FLANGER_DEPTH_S = 0.0025  # s
FX_FLANGER_FEEDBACK = 0.55  # ratio
FX_FLANGER_RATE_HZ = 0.3  # Hz
FX_PHASER_BASE_HZ = 900  # Hz
FX_PHASER_DEPTH_HZ = 700  # Hz
FX_PHASER_Q = 1.2  # Q
FX_PHASER_RATE_HZ = 0.35  # Hz
FX_PHASER_SPACING_HZ = 350  # Hz
FX_PINGPONG_BEATS = 0.5  # beats
FX_PINGPONG_FEEDBACK = 0.5  # ratio
FX_REVERB_DAMP_HZ = 3200  # Hz
FX_REVERB_FEEDBACK = 0.78  # ratio
FX_REVERB_LEVEL = 0.25  # gain
FX_WET_DEFAULT = 0.6  # ratio
GAIN_TRIM_MAX = 2  # gain
GAIN_TRIM_MIN = 0  # gain
GENRE_FAMILIES = {"south_asian": ["punjabi", "bhangra", "desi", "bollywood", "hindi", "urdu", "pakistani", "indian", "filmi", "sufi", "qawwali", "haryanvi", "tamil", "telugu"], "electronic": ["house", "techno", "garage", "trance", "edm", "electronic", "electronica", "dubstep", "drum & bass", "drum and bass", "dnb", "bass music", "breakbeat", "downtempo", "ambient", "future bass", "electro", "idm", "jungle", "dance", "breaks", "trip hop", "chillout", "big room", "riddim"], "hiphop": ["hip-hop", "hip hop", "rap", "trap", "drill", "grime"], "rnb": ["r&b", "rnb", "soul"], "latin": ["reggaeton", "latin", "dembow", "cumbia", "bachata", "salsa", "urbano"], "afro": ["afrobeat", "afro", "amapiano", "afropop"], "rock": ["rock", "punk", "metal", "grunge"], "pop": ["pop"], "country": ["country", "folk", "americana"], "jazz": ["jazz", "funk", "disco"]}
GLITCH_COUNTER_EVERY_S = 1.0  # s
GLITCH_REPORT_GAP_S = 8  # s
HOLD_KEY_MIN = 0.8  # score
KEYLOCK_RANGE_PCT = 8.0  # pct
KEY_CONFIDENCE_MIN = 0.6  # score
KEY_SAFE_MIN = 0.6  # score
LAYER_MIN_KEY = 0.8  # score
LIMITER_ATTACK_S = 0.001  # s
LIMITER_KNEE_DB = 0  # dB
LIMITER_RATIO = 20.0  # ratio
LIMITER_RELEASE_S = 0.1  # s
LIMITER_THRESHOLD_DB = -4  # dB
LOW_ENERGY_SET_MAX = 4  # level
MASTER_OUT_DB = -0.5  # dB
MASTER_TRIM = 0.9  # gain
MAX_BELOW_PEAK = 2  # levels
MAX_KEYLOCK_STRETCH = 0.08  # fraction
MAX_RATE_STRETCH = 0.06  # fraction
MAX_TEMPO_PCT_PER_BAR = 0.25  # pct/bar
MERGE_AUDITION_BUDGET_MS = 90000  # ms
MERGE_KEY_OK = 0.8  # score
MICRO_STEPS = 32  # steps
NEIGHBOUR_FAMILIES = ["bass|dnb", "bass|edm", "bass|electronic", "bass|house", "bass|melodic", "bass|trance", "breaks|dnb", "breaks|electronic", "breaks|house", "chill|electronic", "chill|melodic", "dnb|electronic", "edm|electronic", "edm|house", "edm|melodic", "edm|trance", "electronic|house", "electronic|melodic", "electronic|techno", "electronic|trance", "hiphop|rnb", "house|melodic", "house|techno", "house|trance", "melodic|techno", "melodic|trance", "techno|trance"]
OUTPUT_CEIL = 0.998  # linear
OVERLAP_BARS = {"instant": 4, "standard": 16, "slow": 32}  # bars
PEAK_KEY_MIN = 0.8  # score
PEAK_WINDOW = 6  # songs
PICK_TIMEOUT_MS = 240000  # ms
PITCH_RANGE_PCT = 8.0  # pct
PLAN_ASK_WAIT_MS = 20000  # ms
PLAN_MAX_WAIT_MS = 65000  # ms
PLAN_MIN_WAIT_MS = 12000  # ms
PREPLAN_WAIT_MS = 35000  # ms
PRE_CLEAR_BARS = {"instant": 0, "standard": 8, "slow": 16}  # bars
REPLAN_LEAD_S = 20.0  # s
RIFF_MAX_STRETCH = 0.15  # fraction
SESSION_PAUSE_CLOSE_MIN = 15  # min
SLEEP_FADE_S = 60.0  # s
STEM_BOOKING_WINDOW_S = 45  # s
STEM_FILL_CHUNK_S = 240  # s
STEM_HOLD_EDGE_S = 0.02  # s
STEM_RETRY_AFTER_S = 60  # s
STEM_SLICE_EDGE_S = 0.008  # s
STEM_WINDOW_MAX_S = 600  # s
STEM_WINDOW_PREROLL_S = 8  # s
SUBDROP_COOLDOWN_S = 240  # s
SUB_BASS_HPF_HZ = 120  # Hz
UNCERTAIN_CLASH_SCORE = 0.5  # score
VOCAL_CLASH_CUTOFF = 0.5  # ratio
WARMUP_SONGS = 5  # songs
XF_CENTER_BOOST_DB = 1.5  # dB

# Entries a server may override at runtime, with the bounds both sides enforce.
LIVE = {
    "BOOK_AFTER_LOAD_S": {"min": 0, "max": 60, "unit": "s"},
    "BYPASS_KEY_SCORE": {"min": 0, "max": 1, "unit": "score"},
    "ENERGY_MATCH_TOL": {"min": 0, "max": 10, "unit": "levels"},
    "ENERGY_MIN_RAW": {"min": 0, "max": 1, "unit": "ratio"},
    "FX_WET_DEFAULT": {"min": 0, "max": 1, "unit": "ratio"},
    "GLITCH_REPORT_GAP_S": {"min": 1, "max": 60, "unit": "s"},
    "KEY_CONFIDENCE_MIN": {"min": 0, "max": 1, "unit": "score"},
    "KEY_SAFE_MIN": {"min": 0, "max": 1, "unit": "score"},
    "MERGE_AUDITION_BUDGET_MS": {"min": 10000, "max": 300000, "unit": "ms"},
    "PICK_TIMEOUT_MS": {"min": 30000, "max": 600000, "unit": "ms"},
    "PLAN_MAX_WAIT_MS": {"min": 5000, "max": 120000, "unit": "ms"},
    "PLAN_MIN_WAIT_MS": {"min": 1000, "max": 60000, "unit": "ms"},
    "PREPLAN_WAIT_MS": {"min": 5000, "max": 120000, "unit": "ms"},
    "SESSION_PAUSE_CLOSE_MIN": {"min": 1, "max": 240, "unit": "min"},
    "UNCERTAIN_CLASH_SCORE": {"min": 0, "max": 1, "unit": "score"},
    "VOCAL_CLASH_CUTOFF": {"min": 0, "max": 1, "unit": "ratio"},
    "XF_CENTER_BOOST_DB": {"min": 0, "max": 3, "unit": "dB"},
}
