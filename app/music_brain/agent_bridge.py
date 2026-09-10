"""Python API + CLI bridge for coding agents (Antigravity, etc.) and the
developer terminal. Every command prints structured JSON to stdout so it's
directly consumable by another program (spec 3.7 / acceptance criterion 5).

    python -m music_brain.agent_bridge analyze songs/track1.mp3
    python -m music_brain.agent_bridge separate songs/track1.mp3 --stems 4
    python -m music_brain.agent_bridge match songs/track1.mp3 songs/track2.mp3
    python -m music_brain.agent_bridge preview songs/track1.mp3 songs/track2.mp3 \\
        --recipe "Bass Swap" --out preview.mp3
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from app.music_brain.analyzer import analyze as _analyze
from app.music_brain.knowledge_parser import KnowledgeParser
from app.music_brain.recipe_matcher import RecipeMatcher
from app.music_brain.stem_service import separate as _separate
from app.music_brain.transition_renderer import render_preview

_knowledge = None


def _get_knowledge() -> KnowledgeParser:
    global _knowledge
    if _knowledge is None:
        _knowledge = KnowledgeParser()
    return _knowledge


# --- Python API --------------------------------------------------------------

def analyze(audio_path: str) -> dict:
    """Full track analysis: BPM, beatgrid, key, structure, energy."""
    result = _analyze(audio_path)
    return result.to_dict()


def separate(audio_path: str, stems: int = 4) -> dict:
    """Demucs stem separation. stems=2 uses --two-stems vocals for speed."""
    two_stems = "vocals" if stems == 2 else None
    result = _separate(audio_path, two_stems=two_stems)
    return result.to_dict()


def match(track_a_path: str, track_b_path: str, top_n: int = 3) -> dict:
    """Top-N recommended transition blueprints for a track pair."""
    track_a = _analyze(track_a_path)
    track_b = _analyze(track_b_path)
    matcher = RecipeMatcher(_get_knowledge())
    candidates = matcher.match(track_a, track_b, top_n=top_n)
    return {
        "track_a": track_a_path,
        "track_b": track_b_path,
        "candidates": [c.to_dict() for c in candidates],
    }


def preview(
    track_a_path: str,
    track_b_path: str,
    recipe_name: Optional[str] = None,
    out: Optional[str] = None,
    preview_seconds: float = 20.0,
    a_time: Optional[float] = None,
    b_time: Optional[float] = None,
) -> dict:
    """Renders a preview snippet.

    - recipe_name and a_time/b_time all omitted: uses the AI's own top match.
    - recipe_name given, a_time/b_time omitted: that recipe at the AI's own
      best-guess points.
    - a_time and/or b_time given: a genuine manual override — your point(s)
      win (snapped to the nearest real phrase boundary), not the AI's.
    """
    track_a = _analyze(track_a_path)
    track_b = _analyze(track_b_path)
    matcher = RecipeMatcher(_get_knowledge())
    candidate = matcher.resolve_candidate(
        track_a, track_b, recipe_name=recipe_name, a_time=a_time, b_time=b_time,
    )

    result = render_preview(
        track_a_path, track_b_path, candidate,
        output_path=out, preview_seconds=preview_seconds,
    )
    payload = {
        "recipe": candidate.recipe.name,
        "explanation": candidate.explanation,
        "score": candidate.score,
    }
    payload.update({
        "output_path": result.output_path,
        "duration_seconds": result.duration_seconds,
        "render_time_seconds": result.render_time_seconds,
        "peak_dbfs": result.peak_dbfs,
        "sample_rate": result.sample_rate,
    })
    return payload


def list_recipes() -> dict:
    """All 28 parsed transition recipes with their tags/prerequisites."""
    return {"recipes": [r.to_dict() for r in _get_knowledge().get_all()]}


# --- CLI ----------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m music_brain.agent_bridge",
        description="AI Music Brain agent bridge: analyze, separate, match, and preview DJ transitions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="Full audio analysis for one track.")
    p_analyze.add_argument("audio_path")

    p_separate = sub.add_parser("separate", help="Demucs stem separation.")
    p_separate.add_argument("audio_path")
    p_separate.add_argument("--stems", type=int, choices=(2, 4), default=4)

    p_match = sub.add_parser("match", help="Top-N recommended transitions for a track pair.")
    p_match.add_argument("track_a")
    p_match.add_argument("track_b")
    p_match.add_argument("--top-n", type=int, default=3)

    p_preview = sub.add_parser("preview", help="Render a transition preview snippet.")
    p_preview.add_argument("track_a")
    p_preview.add_argument("track_b")
    p_preview.add_argument("--recipe", default=None)
    p_preview.add_argument("--out", default=None)
    p_preview.add_argument("--seconds", type=float, default=20.0)
    p_preview.add_argument("--a-time", type=float, default=None, help="Manual exit point on track_a (seconds); overrides the AI's own point.")
    p_preview.add_argument("--b-time", type=float, default=None, help="Manual entry point on track_b (seconds); overrides the AI's own point.")

    sub.add_parser("list-recipes", help="List all 28 parsed transition recipes.")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "analyze":
            payload = analyze(args.audio_path)
        elif args.command == "separate":
            payload = separate(args.audio_path, stems=args.stems)
        elif args.command == "match":
            payload = match(args.track_a, args.track_b, top_n=args.top_n)
        elif args.command == "preview":
            payload = preview(
                args.track_a, args.track_b,
                recipe_name=args.recipe, out=args.out, preview_seconds=args.seconds,
                a_time=args.a_time, b_time=args.b_time,
            )
        elif args.command == "list-recipes":
            payload = list_recipes()
        else:  # pragma: no cover - argparse enforces valid choices
            parser.error(f"Unknown command: {args.command}")
            return 2
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 1

    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
