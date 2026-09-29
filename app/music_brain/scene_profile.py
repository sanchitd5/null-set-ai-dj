"""Punjabi scene profile: the autopilot's rules for a Punjabi / bhangra / desi set.

Evidence: research/notes/punjabi-original-sets.md ("Scene profile draft" in section 7,
"Ranked" list in the summary). Every number below is marked GUESS in that note unless
said otherwise; a listening test has not confirmed any of them.

Owner of the values. app/ui/static/scene-profile.js is a copy for the console,
parity-tested (app/tests/test_scene_profile.py runs it in node and compares).

Mode (the console's PUNJABI setting, sent as `punjabi_profile`):
  off   today's behaviour, byte for byte
  on    the profile on every transition
  auto  the songs decide: both sides Punjabi -> "full"; exactly one side -> "handover"
        (cut-style handover only: Quick Cut fallback, no era gate; no snippet length);
        neither, or a genre unknown -> no profile.
Genre labels come from metadata / the model (genre.py), never from audio.
"""

from __future__ import annotations

import re
from typing import Optional

from app.music_brain import genre as _genre

MODES = ("auto", "on", "off")
DEFAULT_MODE = "auto"

PUNJABI_PROFILE = {
    "name": "punjabi",
    # s7 "same_scene_terms: [punjabi, bhangra, desi]" (plus the note's subgenre names)
    "scene_terms": ("punjabi", "bhangra", "desi", "punjabi hip hop", "punjabi pop"),
    # s7 "neighbour: bollywood"; bollywood-original-sets.md item 7: Punjabi songs sit inside Bollywood sets
    "neighbour_terms": ("bollywood",),
    "max_era_gap": 4,                 # s7 "max_era_gap: 4" (GUESS); Set 2 puts 1980s Chamkila next to 2020s Sidhu (SOURCED)
    "play_seconds": (45, 90),         # s7 "target_play_seconds: [45, 90]" (lengths SOURCED Set 2, rule GUESS)
    "fallback_recipe": "Quick Cut",   # s7 "a refused tonal blend should become a Quick Cut on beat 1" (GUESS)
    "cuts_skip_key_bypass": True,     # s7 "cuts skip the gate, no 0.4 bypass penalty" (GUESS)
    "bpm_octave_fold": True,          # s7 "bpm_octave_fold: true # 176 == 88" (octave reading GUESS)
    "repeat_anthems_after_min": 45,   # s7 "repeat_anthems_after_min: 45" (GUESS); not wired: history has no play times
}

LEVEL_FULL, LEVEL_HANDOVER = "full", "handover"


def normalize_mode(mode) -> str:
    m = str(mode or "").strip().lower()
    return m if m in MODES else DEFAULT_MODE


def _has_term(genre_label, terms) -> bool:
    # whole words only: "desi" must not match "desire"
    g = " " + " ".join(re.sub(r"[^a-z0-9&]+", " ", str(genre_label or "").lower()).split()) + " "
    return any(f" {t} " in g for t in terms)


def is_punjabi(genre_label) -> bool:
    """True when the label names the Punjabi scene (punjabi, bhangra, desi...). Unknown -> False."""
    return _has_term(genre_label, PUNJABI_PROFILE["scene_terms"])


def is_neighbour(genre_label) -> bool:
    return _has_term(genre_label, PUNJABI_PROFILE["neighbour_terms"])


def level(mode, genre_a, genre_b) -> Optional[str]:
    """Which part of the profile applies to the transition A -> B: "full", "handover" or None."""
    m = normalize_mode(mode)
    if m == "off":
        return None
    if m == "on":
        return LEVEL_FULL
    a, b = is_punjabi(genre_a), is_punjabi(genre_b)
    if a and b:
        return LEVEL_FULL
    return LEVEL_HANDOVER if a != b else None


def selection_active(mode, playing_genre) -> bool:
    """Suggestion / library filtering: on, or auto while the playing song is Punjabi."""
    m = normalize_mode(mode)
    return m == "on" or (m == "auto" and is_punjabi(playing_genre))


def scene_near(genre_a, genre_b, active: bool) -> Optional[bool]:
    """genre.genre_near, except under the profile Punjabi / bhangra / desi are one scene
    and bollywood its neighbour. Non-Punjabi pairs keep genre_near's answer."""
    if active and in_scene_pair(genre_a, genre_b):
        return True
    return _genre.genre_near(genre_a, genre_b)


def in_scene_pair(genre_a, genre_b) -> bool:
    """Both Punjabi, or Punjabi next to its neighbour (bollywood): the profile's own pairs."""
    pa, pb = is_punjabi(genre_a), is_punjabi(genre_b)
    return (pa and pb) or (pa and is_neighbour(genre_b)) or (pb and is_neighbour(genre_a))


def era_gap_limit(active: bool) -> int:
    return PUNJABI_PROFILE["max_era_gap"] if active else _genre.MAX_ERA_GAP


def era_jump(era_a, era_b, active: bool) -> bool:
    gap = _genre.era_gap(era_a, era_b)
    return gap is not None and gap > era_gap_limit(active)


def fold_bpm(bpm_a: float, bpm_b: float) -> float:
    """B's tempo moved by x2 / x0.5 to sit nearest A (176 -> 88 next to 88)."""
    if not (bpm_a and bpm_a > 0 and bpm_b and bpm_b > 0):
        return bpm_b
    return min((bpm_b * m for m in (1.0, 2.0, 0.5)), key=lambda x: abs(bpm_a / x - 1))


def vibe_score(genre_a=None, genre_b=None, era_a=None, era_b=None) -> float:
    """genre.vibe_score with the profile's era gate (max_era_gap) in place of MAX_ERA_GAP."""
    era = _genre.ERA_JUMP_PENALTY if era_jump(era_a, era_b, True) else 1.0
    return _genre.genre_score(genre_a, genre_b) * era
