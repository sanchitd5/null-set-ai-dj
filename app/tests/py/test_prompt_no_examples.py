"""Guard: no example song or artist from the DJ/ wiki reaches any model prompt.

The watch list is built from the real notes (every `Artist - "Title"` credit plus
the DJ/13 case-study artists), so a new example in a note is caught without
editing this test. Context songs the prompt is about are not examples: they are
only substituted into templates at call time and are never checked here."""
import re

import pytest

from app.music_brain.matching import dj_knowledge as dk
from app.ui.services import autopilot_service as ap
from app.ui.services import mind_plan as mp


def _watch() -> list:
    names = dk.example_names()
    assert "Fred again.." in names and "Skrillex" in names and "Martin Garrix" in names
    assert "Dua Lipa" in names          # `Dua Lipa - "Levitating"` in DJ/ notes
    return list(names)


def _found(text: str, names: list) -> list:
    return [n for n in names if re.search(r"(?<![\w])" + re.escape(n) + r"(?![\w])", text)]


def _kb_prompts() -> dict:
    out = {"brief:none": dk.selection_brief(""), "move_rules": mp.move_rules()}
    for keys, _stem in dk._GENRE_PLAYBOOKS:
        out[f"brief:{keys[0]}"] = dk.selection_brief(keys[0])
    recipes = [r.name for r in mp._knowledge().get_all()]
    assert len(recipes) >= 28
    for name in recipes:
        out[f"grounding:{name}"] = mp.grounding([name])
    return out


def _code_prompts() -> dict:
    from app.music_brain.analysis import genre_labels
    from app.music_brain.learning import set_ai
    from app.music_brain.render import merge
    from app.ui.services import live_ear
    return {
        "suggest system": ap._SYSTEM, "suggest system plain": ap._SYSTEM_PLAIN,
        "suggest few-shot": ap._FEW_SHOT, "suggest user": ap._USER_TEMPLATE,
        "lead system": ap._LEAD_SYSTEM, "lead user": ap._LEAD_TEMPLATE,
        "knowledge": ap._KNOWLEDGE_TEMPLATE, "relaxed": ap.RELAXED_LINE,
        "favourite": ap._FAVOURITE_LINE, "earlier": ap._EARLIER_LINE,
        "tempo window": ap._TEMPO_WINDOW_LINE,
        "set modes": "\n".join(ap.SET_MODE_LINES.values()),
        "plan system": mp._SYSTEM, "plan move help": "\n".join(mp.MOVE_HELP.values()),
        "live ear": live_ear.SYSTEM, "merge ear": merge.EAR_SYSTEM,
        "hooks": set_ai.HOOK_SYSTEM, "review": set_ai.REVIEW_SYSTEM,
        "genre labels": genre_labels._label_system(),
    }


@pytest.mark.parametrize("source", ["kb", "code"])
def test_no_wiki_example_reaches_a_prompt(source):
    names = _watch()
    prompts = _kb_prompts() if source == "kb" else _code_prompts()
    bad = {k: _found(v, names) for k, v in prompts.items() if _found(v, names)}
    assert not bad, bad


def test_kb_prompts_carry_no_quoted_song_titles():
    # `(e.g., "Title")` style examples go whole; a quoted title would slip past the
    # name list if it were new, so no KB prompt quotes a Capitalised Title at all.
    for k, v in _kb_prompts().items():
        assert not re.search(r"\"[A-Z][a-z]+(?: [A-Z][a-z]+)+\"", v), (k, v)


def test_guard_is_not_vacuous():
    # the raw notes still carry examples for human readers; only prompts are clean
    raw = dk._note("Melodic Electronic Playbook") + dk._note("Dubstep Playbook")
    assert _found(raw, _watch())


def test_strip_keeps_the_rule_and_drops_the_example():
    s = dk.strip_examples('Drop the piano chords of a Fred again.. track over it (e.g., Dua Lipa - "Levitating").')
    assert s == "Drop the piano chords of a track over it."
    s = dk.strip_examples("Drop a 140 BPM drop on Beat 1! (See Skrillex Case Study).")
    assert s == "Drop a 140 BPM drop on Beat 1!"
    rule = "Same BPM and drum swing (e.g., 126 BPM Tech House -> 126 BPM Bass House)."
    assert dk.strip_examples(rule) == rule


def test_the_two_selection_notes_steer_contrast_inside_the_scene():
    brief = dk.selection_brief("")
    assert "radical genre leap" not in brief
    assert "Drum & Bass, House" not in brief            # the old Pop -> DnB contrast example
    assert "Genre stays the same or a close neighbour" in brief
    assert "never license a genre leap" in brief
