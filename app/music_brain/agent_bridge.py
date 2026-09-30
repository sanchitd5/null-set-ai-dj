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


def match(
    track_a_path: str,
    track_b_path: str,
    top_n: int = 3,
    genre_a: Optional[str] = None,
    genre_b: Optional[str] = None,
    era_a: Optional[str] = None,
    era_b: Optional[str] = None,
) -> dict:
    """Top-N recommended transition blueprints for a track pair. Optional
    genre/era labels penalise an unrelated-genre or multi-decade jump."""
    track_a = _analyze(track_a_path)
    track_b = _analyze(track_b_path)
    matcher = RecipeMatcher(_get_knowledge())
    candidates = matcher.match(
        track_a, track_b, top_n=top_n,
        genre_a=genre_a, genre_b=genre_b, era_a=era_a, era_b=era_b,
    )
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
    genre_a: Optional[str] = None,
    genre_b: Optional[str] = None,
    era_a: Optional[str] = None,
    era_b: Optional[str] = None,
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
        genre_a=genre_a, genre_b=genre_b, era_a=era_a, era_b=era_b,
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


def learn_set(source: str, tracklist: Optional[str] = None, download: bool = True, jobs: int = 2, ai: bool = True,
              macros: bool = True, macros_only: bool = False, split_minutes: Optional[float] = None,
              keep_files: bool = False) -> dict:
    """Study a DJ set (URL or file): stems, per-stem song matching, transition and
    vocal re-cut extraction; merges learned techniques into the store. Then (macros=True)
    imports the set's songs as library tracks and rebuilds the pair atlas incrementally,
    which writes studied-<set_id>-<n> + studied-set-<set_id>; offline, never downloads.
    That step never fails the learn: its outcome is the `macros` field
    ({imported, skipped, written[], error}).
    macros_only: no set audio, no study; the tracklist alone becomes the macro set-<set_id>
    (set_import.learn_tracklist_macro). Both end by exporting app/music_brain/knowledge/
    (the `knowledge` field; nothing is committed).
    split_minutes: a set longer than this is studied in checkpointed parts (None = the
    learner's SPLIT_MIN, 0 = never split); a killed run resumes at the next part.
    keep_files: skip the cleanup; by default every good song and ID cut is registered in the
    library, then clips, clip stems, registered song files and the set recording are deleted
    (the `cleanup` field: freed_bytes, deleted, kept[{file, reason}])."""
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    if macros_only:
        from app.music_brain.set_import import learn_tracklist_macro

        report = learn_tracklist_macro(source, tracklist=tracklist, download=download, log=log)
    else:
        from app.music_brain.set_learner import learn_set as _learn

        from app.music_brain.set_learner import SPLIT_MIN

        report = _learn(source, tracklist=tracklist, download=download, jobs=jobs, ai=ai, log=log,
                        split_minutes=SPLIT_MIN if split_minutes is None else split_minutes, keep_files=keep_files)
        if macros:
            from app.music_brain.set_import import learn_macros

            report["macros"] = learn_macros(report["set_id"], log=log)
    from app.music_brain import knowledge

    report["knowledge"] = knowledge.export_safe(log=log)
    return report


def hook_drops(path: str, title: str, top_n: int = 3, render: bool = False, ai: bool = True) -> dict:
    """Where to go acapella on the song's emotional hook and drop back in; render=True
    writes an audition WAV per pick (plus the untouched span) to data/output/hook_drops/."""
    import re

    import librosa

    from app.music_brain import hook_drop as hd, lyrics as ly, set_ai
    from app.music_brain.config import ROOT_DIR
    from app.music_brain.set_learner import load_learned
    from app.music_brain.stem_service import separate as _sep

    a = _analyze(path)
    stems = _sep(path).stems
    y, sr = librosa.load(stems["vocals"], sr=11025, mono=True)
    lines = ly.for_file(title, y, sr, duration=a.duration, log=lambda m: print(m, file=sys.stderr))
    picks = set_ai.emotional_lines(title, lines, call=ai) if lines else []
    plan = hd.plan(lines, a.bpm, a.phrase_boundaries_8bar, a.energy_times, a.energy_curve,
                   learned=load_learned(), top_n=top_n, ai_lines=picks)
    out = {"title": title, "bpm": a.bpm, "lyrics_lines": len(lines), "ai": "used" if picks else "not used",
           "hook_drops": plan}
    if render:
        slug = re.sub(r"[^\w]+", "_", title).strip("_")[:60]
        for i, item in enumerate(plan, 1):
            item["render"] = hd.render(stems, item, a.bpm, ROOT_DIR / "data" / "output" / "hook_drops" / f"{slug}_{i}_{int(item['cut_at'])}s.wav")
    return out


def learned() -> dict:
    """Techniques learned so far, with their observations."""
    from app.music_brain.set_learner import load_learned

    return {"learned": load_learned()}


def list_recipes() -> dict:
    """Every parsed transition recipe with its tags/prerequisites."""
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

    for p in (p_match, p_preview):
        p.add_argument("--genre-a", default=None, help="Genre label for track_a (e.g. 'melodic house'); penalises an unrelated-genre jump.")
        p.add_argument("--genre-b", default=None, help="Genre label for track_b.")
        p.add_argument("--era-a", default=None, help="Release era for track_a (e.g. '1990s'); penalises a multi-decade jump.")
        p.add_argument("--era-b", default=None, help="Release era for track_b.")

    sub.add_parser("list-recipes", help="List every parsed transition recipe.")

    p_learn = sub.add_parser("learn-set", help="Learn transition + vocal techniques from a DJ set (URL or file).")
    p_learn.add_argument("source", help="YouTube/SoundCloud URL or local set audio file.")
    p_learn.add_argument("--tracklist", default=None, help="Text file of '1:06:30 Artist - Title' lines (default: video description).")
    p_learn.add_argument("--no-download", action="store_true", help="Use only songs already in data/songs/.")
    p_learn.add_argument("--no-ai", action="store_true", help="Skip the local-LLM review of detected moves.")
    p_learn.add_argument("--jobs", type=int, default=2, help="Demucs runs at once (default 2; each holds a model in memory).")
    p_learn.add_argument("--no-macros", action="store_true",
                         help="Skip the import-set + incremental atlas build that writes this set's studied macros.")
    p_learn.add_argument("--macros-only", action="store_true",
                         help="No set audio, no Demucs on the mix: find or download each tracklist song, register it, "
                              "build the atlas and write the macro set-<set_id> in tracklist order. A later full "
                              "learn of the same set writes studied-set-<set_id>; both macros coexist.")
    p_learn.add_argument("--split-minutes", type=float, default=None,
                         help="Study a set longer than this in parts of about this length, each checkpointed so a "
                              "killed run resumes at the next part (default 60; 0 = never split).")
    p_learn.add_argument("--keep-files", action="store_true",
                         help="Skip the cleanup. By default every good song and ID cut is registered in the library, "
                              "then clips, clip stems, registered song files and the set recording are deleted.")

    sub.add_parser("learned", help="List techniques learned from studied sets.")
    sub.add_parser("learn-status", help="Progress of running / recent set studies (human readable, no server needed).")

    p_hd = sub.add_parser("hook-drop", help="Plan (and audition) an acapella drop on a song's emotional hook.")
    p_hd.add_argument("audio")
    p_hd.add_argument("--title", required=True, help='"Artist - Title" (for the lyrics)')
    p_hd.add_argument("--top-n", type=int, default=3)
    p_hd.add_argument("--render", action="store_true", help="write audition WAVs to data/output/hook_drops/")
    p_hd.add_argument("--no-ai", action="store_true")

    p_ly = sub.add_parser("lyrics", help="Show a song's synced lyrics, or pin them by hand when the online ones are wrong.")
    p_ly.add_argument("title", help='"Artist - Title"')
    g2 = p_ly.add_mutually_exclusive_group()
    g2.add_argument("--lrc", help="LRC file to pin ([mm:ss.xx] words lines)")
    g2.add_argument("--lrclib", type=str, help="pin one LRCLIB entry: id or https://lrclib.net/tracks/<id>")
    g2.add_argument("--none", action="store_true", help="pin 'no lyrics' (online ones are for another song)")

    p_src = sub.add_parser("source", help="Recordings a song's vocal was sampled from, and how they were rebuilt.")
    ssub = p_src.add_subparsers(dest="source_cmd", required=True)
    s_add = ssub.add_parser("add", help="fetch a source recording + its captions")
    s_add.add_argument("url")
    s_add.add_argument("--song", help='link it to "Artist - Title"')
    s_add.add_argument("--note", default="", help="what the source means")
    s_learn = ssub.add_parser("learn", help="how the source was made into a song or a live set")
    s_learn.add_argument("source", help="source video id or url")
    s_learn.add_argument("target", help="song/set audio file, or a YouTube url")
    s_learn.add_argument("--start", type=float, default=0.0, help="seconds: cut a long set to the part using the source")
    s_learn.add_argument("--end", type=float, default=None)
    s_learn.add_argument("--label", default=None)
    s_show = ssub.add_parser("show")
    s_show.add_argument("source")

    p_fb = sub.add_parser("learn-feedback", help="Refine a learned technique with your own rule (wins over the set).")
    p_fb.add_argument("kind", help="e.g. acapella_over, bass_swap, vocal_resequence")
    p_fb.add_argument("rule", nargs="?", default="", help='e.g. "rap ~9 dB under the riff"')
    g = p_fb.add_mutually_exclusive_group()
    g.add_argument("--disable", action="store_const", const=True, dest="disable", help="keep it out of the autopilot")
    g.add_argument("--enable", action="store_const", const=False, dest="disable")

    p_sr = sub.add_parser("session-report", help="Songs of a set session with their AI step counts (newest session by default).")
    p_sr.add_argument("--session", default=None, help="session id, e.g. 2026-09-28_212853")
    p_sr.add_argument("--render", action="store_true", help="(re)render waveform.png for every song, synchronously")

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
            payload = match(
                args.track_a, args.track_b, top_n=args.top_n,
                genre_a=args.genre_a, genre_b=args.genre_b, era_a=args.era_a, era_b=args.era_b,
            )
        elif args.command == "preview":
            payload = preview(
                args.track_a, args.track_b,
                recipe_name=args.recipe, out=args.out, preview_seconds=args.seconds,
                a_time=args.a_time, b_time=args.b_time,
                genre_a=args.genre_a, genre_b=args.genre_b, era_a=args.era_a, era_b=args.era_b,
            )
        elif args.command == "list-recipes":
            payload = list_recipes()
        elif args.command == "learn-set":
            payload = learn_set(args.source, tracklist=args.tracklist, download=not args.no_download, jobs=args.jobs, ai=not args.no_ai,
                                macros=not args.no_macros, macros_only=args.macros_only,
                                split_minutes=args.split_minutes, keep_files=args.keep_files)
            if args.macros_only and not payload.get("macro"):
                print(json.dumps(payload, indent=2, default=str))
                return 1                  # the macro was the whole job
        elif args.command == "hook-drop":
            payload = hook_drops(args.audio, args.title, top_n=args.top_n, render=args.render, ai=not args.no_ai)
        elif args.command == "source":
            from app.music_brain import sources as src

            log = lambda m: print(m, file=sys.stderr, flush=True)
            if args.source_cmd == "add":
                payload = src.add(args.url, song=args.song, note=args.note, log=log)
            elif args.source_cmd == "show":
                payload = src.get(src.video_id(args.source)) | {"captions_text": src.captions(src.video_id(args.source))["phrases"]}
            else:
                target = args.target
                if target.startswith("http"):
                    from app.music_brain.set_learner import fetch_set

                    target = str(fetch_set(target)[0])
                payload = src.learn_transform(src.video_id(args.source), target, label=args.label,
                                              start=args.start, end=args.end, log=log)
        elif args.command == "learned":
            payload = learned()
        elif args.command == "learn-status":
            from app.music_brain import learn_progress as lp

            docs = lp.read_all()
            print("\n".join(lp.summary(d) for d in docs) or "no set study recorded yet")
            return 0
        elif args.command == "lyrics":
            from app.music_brain import lyrics as ly

            if args.none:
                lines = ly.set_manual(args.title, None)
            elif args.lrclib:
                lines = ly.set_from_lrclib(args.title, int(args.lrclib.rstrip("/").rsplit("/", 1)[-1]))
            elif args.lrc:
                lines = ly.set_manual(args.title, Path(args.lrc).expanduser().read_text(encoding="utf-8"))
            else:
                lines = ly.fetch(args.title)
            payload = {"title": args.title, "manual": bool(args.none or args.lrc or args.lrclib), "sample": ly.sample_of(args.title), "lines": lines}
        elif args.command == "learn-feedback":
            from app.music_brain.set_learner import add_user_rule

            payload = {"technique": add_user_rule(args.kind, args.rule, disable=args.disable)}
        elif args.command == "session-report":
            from app.ui import song_log

            payload = song_log.report(args.session)
            if args.render and payload["session"]:
                payload["rendered"] = song_log.render_all(payload["session"])
        else:  # pragma: no cover - argparse enforces valid choices
            parser.error(f"Unknown command: {args.command}")
            return 2
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 1
    except Exception as exc:          # yt-dlp / network / Demucs: still one JSON error line (spec: never a traceback on stdout)
        print(json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:400]}"}, indent=2))
        return 1

    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
