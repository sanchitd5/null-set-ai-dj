"""AI review of learned moves and AI emotional-line picks, with a fake model."""
import json

import numpy as np
import pytest

from app.music_brain import hook_drop, lyrics, set_ai
from app.music_brain import set_learner as sl
from app.music_brain import techniques as tq

LRC = "[00:10.00] verse one\n[00:14.00] take me higher\n[00:22.00] take me higher\n[00:28.00] don't let me go\n[00:31.00] outro"


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setattr(set_ai, "AI_CACHE_DIR", tmp_path / "ai")


def test_emotional_lines_only_exact_lines_and_cached():
    calls = []

    def chat(system, user):
        calls.append(user)
        return json.dumps({"lines": [{"text": "Don't let me go", "intensity": 9, "why": "a plea"},
                                     {"text": "made up line", "intensity": 10, "why": "x"}]})
    lines = lyrics.parse_lrc(LRC)
    got = set_ai.emotional_lines("A - B", lines, chat=chat)
    assert got == [{"text": "don't let me go", "intensity": 9, "why": "a plea"}]
    assert set_ai.emotional_lines("A - B", lines, chat=chat) == got and len(calls) == 1
    assert set_ai.emotional_lines("C - D", lines, call=False) == []          # cache-only never calls


def test_ai_line_sung_once_wins_the_hook_drop():
    lines = lyrics.parse_lrc(LRC)
    picks = [{"text": "don't let me go", "intensity": 10, "why": "a plea"}]
    p = hook_drop.plan(lines, 120.0, [0, 16, 32, 48], ai_lines=picks)       # bar 2 s; line ends 31 -> drop 32
    assert p[0]["text"] == "don't let me go" and p[0]["ai"] and p[0]["why"][0].startswith("AI: a plea")


def test_review_keeps_rules_drops_rejects_and_falls_back():
    obs = [sl.Observation("bass_swap", "s", 32.0, "A", "B", 0.0, 1.0, {}),
           sl.Observation("vocal_resequence", "s", 90.0, "A", detail={"words": ["x", "y"]})]
    chat = lambda s, u: json.dumps({"items": [{"id": 0, "keep": True, "rule": "swap bass on the 16-bar line"},
                                              {"id": 1, "keep": False, "why": "chorus repeat"}]})
    r = set_ai.review(obs, chat=chat)
    assert r["ai"] == "reviewed" and [o.kind for o in r["kept"]] == ["bass_swap"]
    assert r["kept"][0].detail["ai_rule"] == "swap bass on the 16-bar line"
    assert r["rejected"][0].detail["ai_reject"] == "chorus repeat"
    none = set_ai.review([sl.Observation("hard_cut", "t", 1.0, "C", "D")], chat=lambda s, u: "not json")
    assert none["ai"].startswith("skipped") and len(none["kept"]) == 1


def test_ai_rules_reach_rank(tmp_path):
    o = sl.Observation("bass_swap", "s", 32.0, "A", "B", 0.0, 1.0, {"ai_rule": "swap bass on the 16-bar line"})
    store = sl.merge([o], tmp_path / "l.json")
    r = next(x for x in tq.rank(tq.PairFeatures(128, 128), learned=store) if x["name"] == "learned:bass_swap")
    assert "ok: ai rule: swap bass on the 16-bar line" in r["reasons"]


def test_sample_source_reaches_the_model(tmp_path, monkeypatch):
    from app.music_brain import sources
    monkeypatch.setattr(lyrics, "LYRICS_DIR", tmp_path / "ly")
    monkeypatch.setattr(sources, "SOURCES_DIR", tmp_path / "src")
    d = tmp_path / "src" / "aqu4ezLQEUA"
    d.mkdir(parents=True)
    (d / "info.json").write_text(json.dumps({"title": "Sabrina Benaim - Explaining My Depression to My Mother",
                                             "uploader": "Button Poetry", "url": "https://youtu.be/aqu4ezLQEUA"}))
    (d / "links.json").write_text(json.dumps([{"song": "Fred again.. - Sabrina (i am a party)", "note": "poem about depression"}]))
    lyrics.set_manual("Fred again.. - Sabrina (i am a party)", "[00:01.00] I am a party\n[00:05.00] Inside of my head")
    seen = []
    chat = lambda s, u: seen.append(u) or json.dumps({"lines": [{"text": "I am a party", "intensity": 9, "why": "irony"}]})
    got = set_ai.emotional_lines("Fred again.. - Sabrina (i am a party)", lyrics.fetch("Fred again.. - Sabrina (i am a party)"), chat=chat)
    assert "Explaining My Depression to My Mother (Button Poetry) (poem about depression)" in seen[0] and got[0]["intensity"] == 9


def test_odd_model_answers_and_a_bad_cache_never_crash(tmp_path):
    lines = lyrics.parse_lrc(LRC)
    inf = lambda s, u: '{"lines": [{"text": "don\'t let me go", "intensity": Infinity}]}'
    assert set_ai.emotional_lines("E - F", lines, chat=inf) == [
        {"text": "don't let me go", "intensity": 5, "why": ""}]
    assert set_ai.emotional_lines("G - H", lines, chat=lambda s, u: '{"lines": 7}') == []
    obs = [sl.Observation("bass_swap", "s", 32.0, "A", "B", 0.0, 1.0, {})]
    r = set_ai.review(obs, chat=lambda s, u: '{"items": 3}')
    assert [o.kind for o in r["kept"]] == ["bass_swap"]
    for p in (tmp_path / "ai").glob("*.json"):        # a cache file that is not an object
        p.write_text("[1, 2]", encoding="utf-8")
    assert set_ai.emotional_lines("E - F", lines, call=False) == []


def test_find_model_prefers_a_loaded_one_and_skips_dead_ports():
    import io

    class Op:
        def open(self, url, timeout=0):
            if "8081" in url:
                raise OSError("refused")
            body = {"data": [{"id": "text", "loaded": False}, {"id": "omni", "loaded": True}]}
            return io.BytesIO(json.dumps(body).encode())
    assert set_ai.find_model(["http://127.0.0.1:8081/v1", "http://127.0.0.1:8901/v1"], Op()) == ("http://127.0.0.1:8901/v1", "omni")
    assert set_ai.find_model(["http://127.0.0.1:8081/v1"], Op()) is None


def test_review_prompt_explains_single_song_moves_and_sends_positions():
    seen = []
    o = sl.Observation("vocal_resequence", "s", 90.0, "A - B", detail={"source_lines": [[65.4, 72.7], [10.0, 13.0]], "words": ["x", "y"]})
    set_ai.review([o], chat=lambda s, u: seen.append((s, u)) or json.dumps({"items": [{"id": 0, "keep": True, "rule": "r"}]}))
    system, user = seen[0]
    assert '"b" is null by design' in system and "Not a definition of the move" in system
    assert json.loads(user)["items"][0]["source"] == [[65.4, 72.7], [10.0, 13.0]]
