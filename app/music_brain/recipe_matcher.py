"""Evaluates a pair of analyzed tracks against the 28 transition recipes,
ranks the best-fitting ones, and explains the choice in plain English.

Implements the "DJ Brain" heuristics from spec 3.4 / CLAUDE.md section 4:
  1. Camelot key distance scoring (12-hour wheel).
  2. BPM difference classification (seamless / ramp / cut-required).
  3. Phrase-boundary alignment: candidate exit points on Track A (outgoing)
     land on an 8-bar phrase boundary inside a breakdown/outro/high-energy
     section; candidate entry points on Track B (incoming) land on a phrase
     boundary inside an intro/early-verse section.
  4. Vocal-collision penalty: overlapping active vocals on both tracks during
     the transition window is penalized unless the chosen recipe mutes one
     side via stems (`requires_stems`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.music_brain.analyzer import TrackAnalysis
from app.music_brain.config import BARS_PER_PHRASE, BEATS_PER_BAR
from app.music_brain.knowledge_parser import KnowledgeParser, TransitionRecipe

_CAMELOT_RE = re.compile(r"^(\d{1,2})([AB])$", re.IGNORECASE)

# Sections on the outgoing track (A) that are good places to leave from.
_EXIT_SECTIONS = {"breakdown", "outro", "drop", "build"}
# Sections on the incoming track (B) that are unconditionally good entry points.
_ENTRY_SECTIONS = {"intro", "breakdown"}

# Recipe name fragments → energy direction they suit best.
_HIGH_ENERGY_RECIPES = {
    "Drop Swap", "Double Drop", "Build-to-Drop Transition", "Loop Roll",
    "Hard Cut", "Stutter Transition", "Beat Jump Transition",
}
_LOW_ENERGY_RECIPES = {
    "Basic Blend", "Long Blend", "EQ Blend", "Filter Transition",
    "Echo Out", "Reverb Transition", "Breakdown Transition",
}


# How long the two records overlap, per recipe (set study gfF8jzBVWvM, section 8
# item 3): clean drop swaps there ran under ~2 s, breakdown/filter entries 15-20 s+
# with the low end pre-cleared first. One constant per kind can't hold both.
#   instant  - bass moves on one downbeat, no pre-clear
#   slow     - long overlap, outgoing bass pre-cleared ~16 bars early
#   standard - everything else, ~8 bars of pre-clear
_INSTANT_RECIPES = {
    "Drop Swap", "Bass Swap", "Double Drop", "Hard Cut", "Quick Cut",
    "Vocal Punchline Drop Snap", "Backspin (Spinback)",
}
_SLOW_RECIPES = {
    "Breakdown Transition", "Filter Transition", "Long Blend", "Echo Out",
    "Reverb Transition", "Genre Bridge", "Tempo Bridge", "3-Deck Layering",
    "Stems Transition", "Instrumental Overlay", "Acapella Overlay",
}
PRE_CLEAR_BARS = {"instant": 0, "standard": 8, "slow": 16}


def overlap_style(recipe_name: str) -> str:
    """'instant' | 'standard' | 'slow' overlap for a recipe name."""
    if recipe_name in _INSTANT_RECIPES:
        return "instant"
    if recipe_name in _SLOW_RECIPES:
        return "slow"
    return "standard"


# Where each recipe class leaves Track A (cookbook "Setup" sections): drop
# recipes swap on the drop, breakdown/echo moves leave from a breakdown or the
# outro, everything else blends out of the outro or a breakdown.
_DROP_RECIPES = {
    "Drop Swap", "Double Drop", "Build-to-Drop Transition", "Vocal Punchline Drop Snap",
}
_BREAKDOWN_ECHO_RECIPES = {
    "Breakdown Transition", "Echo Out", "Reverb Transition", "Filter Transition",
}
_EXIT_ROLES = {
    "drop": {"drop"},
    "breakdown_echo": {"breakdown", "outro"},
    "blend": {"outro", "breakdown"},
}


# Candidate points per side per recipe (keeps match() at <= 28 x 8 x 8 scorings).
MAX_POINTS_PER_SIDE = 8


def recipe_class(recipe_name: str) -> str:
    """'drop' | 'breakdown_echo' | 'blend' — which exit sections suit a recipe."""
    if recipe_name in _DROP_RECIPES:
        return "drop"
    if recipe_name in _BREAKDOWN_ECHO_RECIPES:
        return "breakdown_echo"
    return "blend"


@dataclass
class TransitionCandidate:
    recipe: TransitionRecipe
    score: float  # 0-100
    a_time: float  # exit point on Track A, seconds
    b_time: float  # entry point on Track B, seconds
    camelot_score: float
    bpm_score: float
    phrase_score: float
    vocal_penalty: float
    explanation: str
    exit_section: Optional[str] = None  # A's section at a_time
    entry_section: Optional[str] = None  # B's section at b_time

    def to_dict(self) -> dict:
        style = overlap_style(self.recipe.name)
        return {
            "overlap_style": style,
            "pre_clear_bars": PRE_CLEAR_BARS[style],
            "recipe": self.recipe.name,
            "score": round(self.score, 1),
            "a_time": round(self.a_time, 2),
            "b_time": round(self.b_time, 2),
            "camelot_score": round(self.camelot_score, 2),
            "bpm_score": round(self.bpm_score, 2),
            "phrase_score": round(self.phrase_score, 2),
            "vocal_penalty": round(self.vocal_penalty, 2),
            "explanation": self.explanation,
            "exit_section": self.exit_section,
            "entry_section": self.entry_section,
            "recipe_class": recipe_class(self.recipe.name),
        }


def _parse_camelot(camelot: str) -> Tuple[int, str]:
    match = _CAMELOT_RE.match(camelot.strip())
    if not match:
        raise ValueError(f"Invalid Camelot notation: {camelot!r}")
    return int(match.group(1)), match.group(2).upper()


# Confidence below this on either key softens a clash (KeyEstimate.confidence).
KEY_CONFIDENCE_MIN = 0.6
UNCERTAIN_CLASH_SCORE = 0.5
BYPASS_KEY_SCORE = 0.7  # key-agnostic recipes on a clashing pair: neutral, not a clash


def is_key_clash(camelot_a: str, camelot_b: str) -> bool:
    """3+ hours apart, not the relative key and not a diagonal move."""
    hour_a, _ = _parse_camelot(camelot_a)
    hour_b, _ = _parse_camelot(camelot_b)
    return min((hour_a - hour_b) % 12, (hour_b - hour_a) % 12) >= 3


def camelot_distance_score(
    camelot_a: str, camelot_b: str,
    confidence_a: float = 1.0, confidence_b: float = 1.0,
) -> Tuple[float, str]:
    """Score (0-1) and reason for moving from key A to key B on the Camelot wheel.

    Direction matters: +2 hours is an energy boost (0.8), -2 an energy drop
    (0.6). A clash (3+ hours) scores 0.0, softened to 0.5 "uncertain key"
    when either key estimate is below KEY_CONFIDENCE_MIN.
    """
    hour_a, letter_a = _parse_camelot(camelot_a)
    hour_b, letter_b = _parse_camelot(camelot_b)

    if hour_a == hour_b and letter_a == letter_b:
        return 1.0, "identical keys"

    up = (hour_b - hour_a) % 12  # clockwise steps A -> B
    hour_delta = min(up, 12 - up)

    if letter_a == letter_b and hour_delta == 1:
        return 0.9, "adjacent keys on the Camelot wheel (+/-1 hour)"
    if hour_a == hour_b:
        return 0.85, "relative major/minor of the same key"
    if letter_a != letter_b and hour_delta == 1:
        return 0.75, "diagonal move (+/-1 hour with the letter changed)"
    if letter_a == letter_b and up == 2:
        return 0.8, "+2 energy-boost key change"
    if letter_a == letter_b and up == 10:
        return 0.6, "-2 energy-drop key change"
    uncertain = min(confidence_a, confidence_b) < KEY_CONFIDENCE_MIN
    if hour_delta >= 3:
        if uncertain:
            return UNCERTAIN_CLASH_SCORE, f"uncertain key (would clash, {hour_delta} hours apart)"
        return 0.0, f"clashing keys ({hour_delta} hours apart on the Camelot wheel)"
    # 2 hours with the letter changed: distant but not a hard clash.
    return 0.3, "distant keys (2 hours apart, letter changed)"


def bpm_compatibility(bpm_a: float, bpm_b: float) -> Tuple[float, str]:
    """Score (0-1) and classification label for a BPM pairing."""
    """Half/double time counts: 87 vs 174 BPM locks beat-for-beat on every other
    beat, so it scores like a ramp and is labelled "half_time"."""
    if bpm_a <= 0 or bpm_b <= 0:
        return 0.0, "unknown"
    pct_diff = abs(bpm_a - bpm_b) / max(bpm_a, bpm_b)
    if pct_diff <= 0.03:
        return 1.0, "seamless"
    if pct_diff <= 0.06:
        return 0.75, "ramp"
    half = min(abs(bpm_a - 2 * bpm_b) / max(bpm_a, 2 * bpm_b),
               abs(2 * bpm_a - bpm_b) / max(2 * bpm_a, bpm_b))
    if half <= 0.06:
        return (0.75 if half <= 0.03 else 0.6), "half_time"
    return 0.2, "cut_required"


def _section_at(track: TrackAnalysis, time: float) -> Optional[str]:
    for section in track.sections:
        if section.start <= time < section.end:
            return section.label
    return track.sections[-1].label if track.sections else None


def find_exit_candidates(track: TrackAnalysis) -> List[float]:
    """Phrase boundaries on the outgoing track that sit in a good exit section."""
    return [
        t for t in track.phrase_boundaries_8bar
        if _section_at(track, t) in _EXIT_SECTIONS
    ]


def find_entry_candidates(track: TrackAnalysis) -> List[float]:
    """Phrase boundaries on the incoming track that make a good entry point:
    anywhere in an intro/breakdown, or the boundary marking the *start* of a
    verse (i.e. right where the intro ends) per spec 3.4.3 ("intro or verse
    start") — not an arbitrary boundary buried in the middle of a verse.
    """
    verse_starts = {s.start for s in track.sections if s.label == "verse"}
    candidates = []
    for t in track.phrase_boundaries_8bar:
        section = _section_at(track, t)
        if section in _ENTRY_SECTIONS:
            candidates.append(t)
        elif section == "verse" and any(abs(t - vs) < 1e-6 for vs in verse_starts):
            candidates.append(t)
    return candidates


def nearest_phrase_boundary(track: TrackAnalysis, time: float) -> float:
    """Snaps a rough, manually-picked time to the nearest 8-bar phrase
    boundary — per CLAUDE.md's "transition entry/exit timestamps must snap
    to 8-bar (32-beat) phrase boundaries" rule. Falls back to the raw time
    if the track has no detected phrase boundaries.
    """
    boundaries = track.phrase_boundaries_8bar
    if not boundaries:
        return time
    return min(boundaries, key=lambda b: abs(b - time))


def _on_grid(track: TrackAnalysis, time: float, tol: float = 0.05) -> bool:
    """On an 8-bar phrase line (a track with no grid can't be judged: True)."""
    boundaries = track.phrase_boundaries_8bar
    return not boundaries or min(abs(b - time) for b in boundaries) <= tol


def _is_entry_role(track: TrackAnalysis, time: float) -> bool:
    section = _section_at(track, time)
    if section in _ENTRY_SECTIONS:
        return True
    return section == "verse" and any(
        s.label == "verse" and abs(s.start - time) < 1e-6 for s in track.sections
    )


def phrase_fit(
    recipe_name: str, track_a: TrackAnalysis, a_time: float,
    track_b: TrackAnalysis, b_time: float,
) -> float:
    """1.0 on-grid with matching section roles, 0.5 on-grid only, 0.0 off-grid."""
    if not (_on_grid(track_a, a_time) and _on_grid(track_b, b_time)):
        return 0.0
    exit_ok = _section_at(track_a, a_time) in _EXIT_ROLES[recipe_class(recipe_name)]
    return 1.0 if exit_ok and _is_entry_role(track_b, b_time) else 0.5


def recipe_exit_candidates(track: TrackAnalysis, recipe_name: str) -> List[float]:
    """Exit points on A for one recipe: phrase lines in the recipe class's
    sections that leave room for the recipe's overlap before A ends (runway).
    Falls back to any good exit section, then any phrase line, that fits."""
    window = _phrase_transition_window(track, bars=OVERLAP_BARS[overlap_style(recipe_name)])
    fits = [t for t in track.phrase_boundaries_8bar if t + window <= track.duration + 1e-6]
    roles = _EXIT_ROLES[recipe_class(recipe_name)]
    for pool in (
        [t for t in fits if _section_at(track, t) in roles],
        [t for t in fits if _section_at(track, t) in _EXIT_SECTIONS],
        fits,
    ):
        if pool:
            return pool
    return [max(track.duration - window, 0.0)]


def _phrase_transition_window(track: TrackAnalysis, bars: int = 16) -> float:
    """Seconds spanned by `bars` bars at the track's BPM."""
    if track.bpm <= 0:
        return 30.0
    seconds_per_beat = 60.0 / track.bpm
    return bars * BEATS_PER_BAR * seconds_per_beat


# How long both records actually play together, per overlap style: the vocal
# collision window (instant swaps barely overlap, slow blends ride 4 phrases).
OVERLAP_BARS = {"instant": 4, "standard": 2 * BARS_PER_PHRASE, "slow": 4 * BARS_PER_PHRASE}
# A non-stems recipe whose vocals collide this much is halved (decisive, not a nudge).
VOCAL_CLASH_CUTOFF = 0.5


def vocal_overlap_penalty(
    track_a: TrackAnalysis, a_time: float,
    track_b: TrackAnalysis, b_time: float,
    bars: int = 2 * BARS_PER_PHRASE,
) -> float:
    """0.0 (no clash) to 1.0 (full overlap) vocal-collision penalty over `bars` bars."""
    window = min(
        _phrase_transition_window(track_a, bars=bars),
        _phrase_transition_window(track_b, bars=bars),
    )
    a_window = (a_time, a_time + window)
    b_window = (b_time, b_time + window)

    def _active_in(regions: List[Tuple[float, float]], window: Tuple[float, float]) -> float:
        start, end = window
        covered = 0.0
        for r_start, r_end in regions:
            overlap = min(end, r_end) - max(start, r_start)
            if overlap > 0:
                covered += overlap
        span = end - start
        return min(covered / span, 1.0) if span > 0 else 0.0

    a_active = _active_in(track_a.vocal_active_regions, a_window)
    b_active = _active_in(track_b.vocal_active_regions, b_window)
    return a_active * b_active


def _explain(
    recipe: TransitionRecipe, camelot_reason: str, bpm_label: str,
    a_time: float, b_time: float, vocal_penalty: float,
) -> str:
    parts = [
        f"{recipe.name}: Track A exits at {a_time:.1f}s, Track B enters at {b_time:.1f}s.",
        f"Keys are {camelot_reason}; BPM gap is {bpm_label}.",
    ]
    if vocal_penalty > 0.3:
        if recipe.requires_stems:
            parts.append("Both tracks have active vocals here, but this recipe uses stem isolation to avoid a clash.")
        else:
            parts.append("Caution: both tracks have active vocals here, risking a vocal collision.")
    parts.append(recipe.problem_it_solves.split(".")[0].strip() + ".")
    return " ".join(parts)


class RecipeMatcher:
    """Scores every (recipe x candidate transition point) combination for a
    track pair and returns the top-ranked, explained transition blueprints."""

    def __init__(self, knowledge: Optional[KnowledgeParser] = None):
        self.knowledge = knowledge or KnowledgeParser()

    def _score_one(
        self,
        recipe: TransitionRecipe,
        track_a: TrackAnalysis,
        a_time: float,
        track_b: TrackAnalysis,
        b_time: float,
    ) -> TransitionCandidate:
        camelot_score, camelot_reason = 1.0, "not evaluated (no key estimate)"
        key_blocked = False
        if track_a.key and track_b.key:
            camelot_score, camelot_reason = camelot_distance_score(
                track_a.key.camelot, track_b.key.camelot,
                track_a.key.confidence, track_b.key.confidence,
            )
            if camelot_score == 0.0:  # confident 3+ hour clash
                if recipe.camelot_compatible_only:
                    key_blocked = True
                else:
                    camelot_score = BYPASS_KEY_SCORE
                    camelot_reason += "; this recipe bypasses the key clash"

        bpm_score, bpm_label = bpm_compatibility(track_a.bpm, track_b.bpm)
        already_compatible = bpm_score >= 0.9 and camelot_score >= 0.8
        if recipe.max_bpm_delta is None:
            # Bridge/cut/echo recipes are designed for large BPM/key gaps.
            bpm_score = max(bpm_score, 0.8)

        phrase_score = phrase_fit(recipe.name, track_a, a_time, track_b, b_time)

        penalty = vocal_overlap_penalty(
            track_a, a_time, track_b, b_time,
            bars=OVERLAP_BARS[overlap_style(recipe.name)],
        )
        if recipe.requires_stems:
            # Stems surgically isolate vocals; penalty drops more aggressively.
            penalty *= 0.3

        raw = (0.35 * camelot_score) + (0.30 * bpm_score) + (0.20 * phrase_score) + (0.15 * (1 - penalty))
        # Two vocals talking over each other is a train wreck, not a -15% nudge.
        if penalty > VOCAL_CLASH_CUTOFF and not recipe.requires_stems:
            raw *= 0.5
        # Hard gate: a camelot-only recipe is blocked outright on a confident clash.
        if key_blocked:
            raw = 0.0
        # Overkill penalty: per each bridge recipe's own "When NOT to use it"
        # section, don't reach for a big-gap tool (Echo Out, Backspin, ...)
        # when the pair already blends cleanly on key and BPM.
        if recipe.max_bpm_delta is None and already_compatible:
            raw *= 0.85

        score = max(0.0, min(100.0, raw * 100.0))
        explanation = _explain(recipe, camelot_reason, bpm_label, a_time, b_time, penalty)

        return TransitionCandidate(
            recipe=recipe, score=score, a_time=a_time, b_time=b_time,
            camelot_score=camelot_score, bpm_score=bpm_score,
            phrase_score=phrase_score, vocal_penalty=penalty,
            explanation=explanation,
            exit_section=_section_at(track_a, a_time),
            entry_section=_section_at(track_b, b_time),
        )

    def match(
        self,
        track_a: TrackAnalysis,
        track_b: TrackAnalysis,
        top_n: int = 3,
        energy_hint: str = "maintain",
    ) -> List[TransitionCandidate]:
        entry_points = find_entry_candidates(track_b) or (
            [track_b.phrase_boundaries_8bar[0]] if track_b.phrase_boundaries_8bar else [0.0]
        )
        entry_points = entry_points[:MAX_POINTS_PER_SIDE]  # earliest good entries

        # Every recipe is scored at its own exits (by recipe class, with
        # runway) x B's entries; the best pair per recipe is kept. Ties keep
        # the latest exit + earliest entry, what a DJ reaches for first.
        best_per_recipe: dict[str, TransitionCandidate] = {}
        for recipe in self.knowledge.get_all():
            exits = recipe_exit_candidates(track_a, recipe.name)[-MAX_POINTS_PER_SIDE:]
            for a_time in reversed(exits):
                for b_time in entry_points:
                    candidate = self._score_one(recipe, track_a, a_time, track_b, b_time)
                    existing = best_per_recipe.get(recipe.name)
                    if existing is None or candidate.score > existing.score:
                        best_per_recipe[recipe.name] = candidate

        for name, candidate in best_per_recipe.items():
            # Apply energy_hint nudge: ±5 points to steer recipe selection.
            if energy_hint == "up" and name in _HIGH_ENERGY_RECIPES:
                candidate.score = min(100.0, candidate.score + 5.0)
            elif energy_hint == "down" and name in _LOW_ENERGY_RECIPES:
                candidate.score = min(100.0, candidate.score + 5.0)
            elif energy_hint == "up" and name in _LOW_ENERGY_RECIPES:
                candidate.score = max(0.0, candidate.score - 5.0)
            elif energy_hint == "down" and name in _HIGH_ENERGY_RECIPES:
                candidate.score = max(0.0, candidate.score - 5.0)

        ranked = sorted(best_per_recipe.values(), key=lambda c: c.score, reverse=True)
        return ranked[:top_n]

    def score_pair(
        self,
        recipe_name: str,
        track_a: TrackAnalysis,
        a_time: float,
        track_b: TrackAnalysis,
        b_time: float,
        snap_to_phrase: bool = True,
    ) -> TransitionCandidate:
        """Scores one specific recipe at explicit, user-chosen times — the
        manual-override path (as opposed to `match`, which picks its own
        points). Unlike `match`, this never substitutes the AI's own points;
        it only snaps the *given* rough time to the nearest real phrase
        boundary on each track (never mid-phrase), per CLAUDE.md.
        """
        recipe = self.knowledge.get(recipe_name)
        if recipe is None:
            raise ValueError(f"Unknown recipe: {recipe_name!r}")

        if snap_to_phrase:
            a_time = nearest_phrase_boundary(track_a, a_time)
            b_time = nearest_phrase_boundary(track_b, b_time)

        return self._score_one(recipe, track_a, a_time, track_b, b_time)

    def resolve_candidate(
        self,
        track_a: TrackAnalysis,
        track_b: TrackAnalysis,
        recipe_name: Optional[str] = None,
        a_time: Optional[float] = None,
        b_time: Optional[float] = None,
        top_n_for_default: int = 3,
    ) -> TransitionCandidate:
        """The single entry point both the CLI/agent bridge and the web API
        use to decide what to actually render:

        - No recipe_name and no times: the AI's own top suggestion (`match`).
        - recipe_name given, times omitted: that recipe scored at the AI's
          own best guess for its exit/entry points.
        - Either time given (with or without recipe_name): a genuine manual
          override — the given time(s) win, snapped to the nearest phrase
          boundary; any omitted time falls back to the AI's best guess.

        This is what fixes the bug where a manually-picked time was silently
        discarded whenever the chosen recipe happened to already appear
        among the AI's own scored candidates.
        """
        manual_override = a_time is not None or b_time is not None

        if not manual_override and recipe_name is None:
            candidates = self.match(track_a, track_b, top_n=1)
            if not candidates:
                raise ValueError("No transition candidates found for this track pair")
            return candidates[0]

        # Need the AI's own best-guess points as defaults for whichever of
        # a_time/b_time (and whichever recipe) wasn't explicitly given.
        # Points differ per recipe, so a named recipe must find its own entry
        # even when it isn't in the top few.
        default_candidates = self.match(
            track_a, track_b,
            top_n=top_n_for_default if recipe_name is None else len(self.knowledge.get_all()),
        )
        if recipe_name is None:
            if not default_candidates:
                raise ValueError("No transition candidates found for this track pair")
            recipe_name = default_candidates[0].recipe.name

        matched_default = next((c for c in default_candidates if c.recipe.name == recipe_name), None)
        default_a = matched_default.a_time if matched_default else (default_candidates[0].a_time if default_candidates else 0.0)
        default_b = matched_default.b_time if matched_default else (default_candidates[0].b_time if default_candidates else 0.0)

        final_a = a_time if a_time is not None else default_a
        final_b = b_time if b_time is not None else default_b

        return self.score_pair(recipe_name, track_a, final_a, track_b, final_b, snap_to_phrase=manual_override)


if __name__ == "__main__":
    import json
    import sys

    from app.music_brain.analyzer import analyze

    if len(sys.argv) < 3:
        print("Usage: python -m music_brain.recipe_matcher <track_a> <track_b>")
        raise SystemExit(1)

    a = analyze(sys.argv[1])
    b = analyze(sys.argv[2])
    matcher = RecipeMatcher()
    results = matcher.match(a, b)
    print(json.dumps([c.to_dict() for c in results], indent=2))
