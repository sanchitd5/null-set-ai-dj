"""Punjabi scene profile (app/music_brain/scene_profile.py): resolution, era gate, octave
fold, Quick Cut fallback, cut key scoring, JS parity, and off == before the profile."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.music_brain import genre
from app.music_brain import scene_profile as sp
from app.music_brain.knowledge_parser import KnowledgeParser
from app.music_brain.recipe_matcher import BYPASS_KEY_SCORE, RecipeMatcher
from app.tests.py import profile_off_vectors as vectors

HERE = Path(__file__).parents[1]  # app/tests
GOLDEN = json.loads((HERE / "fixtures" / "punjabi_off_golden.json").read_text())
JS = HERE.parent / "ui" / "static" / "scene-profile.js"


@pytest.fixture(scope="module")
def matcher():
    return RecipeMatcher(KnowledgeParser())


@pytest.mark.parametrize("mode,a,b,want", [
    ("auto", "punjabi hip hop", "bhangra", "full"),        # both Punjabi
    ("auto", "punjabi", "pop", "handover"),                # one side (language-block change)
    ("auto", "english pop", "Desi", "handover"),
    ("auto", "house", "techno", None),                     # neither
    ("auto", "", None, None),                              # unknown
    ("auto", "punjabi", "", "handover"),                   # unknown B = not Punjabi
    ("on", "house", "techno", "full"),
    ("off", "punjabi", "bhangra", None),
    ("nonsense", "punjabi", "bhangra", "full"),            # bad mode -> default auto
])
def test_level(mode, a, b, want):
    assert sp.level(mode, a, b) == want


def test_whole_words_and_neighbour():
    assert not sp.is_punjabi("desire")
    assert not sp.is_punjabi("bollywood")
    assert sp.is_neighbour("Bollywood dance")
    assert sp.scene_near("bhangra", "punjabi hip hop", True) is True
    assert genre.genre_near("bhangra", "punjabi hip hop") is False       # global SCENES untouched
    assert sp.scene_near("bhangra", "punjabi hip hop", False) is False
    assert sp.scene_near("punjabi", "bollywood", True) is True
    assert sp.scene_near("house", "metal", True) is False                # non-Punjabi pairs keep genre_near


def test_era_gate_widened_only_under_profile():
    assert genre.MAX_ERA_GAP == 1
    assert sp.era_jump("1980s", "2020s", False)
    assert not sp.era_jump("1980s", "2020s", True)                       # 4 decades: inside the profile
    assert sp.era_jump("1970s", "2020s", True)                           # 5: still a jump
    assert not sp.era_jump(None, "2020s", True)


def test_handover_has_no_era_gate_full_has_the_wider_one():
    assert sp.vibe_score("punjabi", "bollywood", "1970s", "2020s", "handover") == 1.0
    assert sp.vibe_score("punjabi", "bhangra", "1970s", "2020s", "full") == pytest.approx(genre.ERA_JUMP_PENALTY)
    assert sp.vibe_score("punjabi", "bhangra", "1980s", "2020s", "full") == 1.0
    assert genre.vibe_score("punjabi", "bhangra", "1980s", "2020s") == pytest.approx(genre.ERA_JUMP_PENALTY)


def test_handover_does_not_fold_tempo(matcher):
    a, b = vectors._track("8A", 88.0), vectors._track("8A", 176.0)
    hand = {x.recipe.name: x for x in matcher.match(a, b, top_n=100, profile="handover")}
    assert hand["Long Blend"].bpm_score < 1.0


def test_bpm_octave_fold(matcher):
    assert sp.fold_bpm(88, 176) == 88
    assert sp.fold_bpm(88, 130) == 130
    a, b, c = vectors._track("8A", 88.0), vectors._track("8A", 176.0), vectors._track("8A", 130.0)
    folded = {x.recipe.name: x for x in matcher.match(a, b, top_n=100, profile="full")}
    plain = {x.recipe.name: x for x in matcher.match(a, b, top_n=100)}
    far = {x.recipe.name: x for x in matcher.match(a, c, top_n=100, profile="full")}
    assert folded["Long Blend"].bpm_score == 1.0                          # 88 vs 176 = same feel
    assert plain["Long Blend"].bpm_score < 1.0                            # today: half-time 0.75
    assert far["Long Blend"].bpm_score <= 0.2                             # 88 vs 130 does not fold


def test_cut_not_penalised_on_key_clash_under_profile(matcher):
    a, b = vectors._track("8A", 128.0), vectors._track("2A", 128.0)
    plain = {x.recipe.name: x for x in matcher.match(a, b, top_n=100)}
    prof = {x.recipe.name: x for x in matcher.match(a, b, top_n=100, profile="full")}
    assert plain["Quick Cut"].camelot_score == pytest.approx(BYPASS_KEY_SCORE)
    assert prof["Quick Cut"].camelot_score == 1.0
    assert prof["Quick Cut"].score > plain["Quick Cut"].score
    assert prof["Echo Out"].camelot_score == pytest.approx(BYPASS_KEY_SCORE)  # non-cuts unchanged


def test_quick_cut_survives_no_cuts_only_under_profile(matcher):
    a, b = vectors._track("8A", 88.0), vectors._track("8A", 130.0)
    plain = {x.recipe.name for x in matcher.match(a, b, top_n=100, no_cuts=True)}
    prof = {x.recipe.name for x in matcher.match(a, b, top_n=100, no_cuts=True, profile="full")}
    assert "Quick Cut" not in plain and "Quick Cut" in prof
    assert "Hard Cut" not in prof


def test_suggest_filter_under_profile():
    import contextlib, io
    from app.ui.services import autopilot_service as svc
    case = vectors.SUGGEST_CASES[0]                                     # punjabi 1980s playing
    with contextlib.redirect_stdout(io.StringIO()):
        off = svc._filter_suggestions(json.loads(json.dumps(case)), [])
        on = svc._filter_suggestions(json.loads(json.dumps(case)), [], punjabi_profile="auto")
        house = svc._filter_suggestions(json.loads(json.dumps(vectors.SUGGEST_CASES[2])), [], punjabi_profile="auto")
        house_off = svc._filter_suggestions(json.loads(json.dumps(vectors.SUGGEST_CASES[2])), [])
    assert {s["title"] for s in off} < {s["title"] for s in on}
    assert {"One", "Two", "Five"} <= {s["title"] for s in on}           # 1990s / 2020s bhangra + hip hop kept
    assert [s["title"] for s in house] == [s["title"] for s in house_off]  # auto, house playing: unchanged


def test_library_lockable_under_profile(monkeypatch):
    import app.ui.server as srv
    from app.music_brain.analyzer import KeyEstimate
    tracks = {"t1": Path("/x/a - One.mp3"), "t2": Path("/x/b - Two.mp3")}
    monkeypatch.setattr(srv, "_tracks", tracks)
    monkeypatch.setattr(srv, "_track_names", {"t1": "A - One", "t2": "B - Two"})
    monkeypatch.setattr(srv, "_suggested_genres", {srv._genre_key("One"): "bhangra", srv._genre_key("Two"): "punjabi pop"})
    monkeypatch.setattr(srv, "_suggested_eras", {srv._genre_key("One"): "1990s", srv._genre_key("Two"): "2020s"})
    fake = type("T", (), {"bpm": 176.0, "duration": 200.0, "key": KeyEstimate("8A", "", False, 0.9)})()
    monkeypatch.setattr(srv, "analyze_track", lambda p: fake)
    names = lambda **kw: sorted(t["name"] for t in srv.get_library_lockable(bpm=88.0, genre="punjabi hip hop", era="1980s", **kw)["tracks"])
    assert names() == []                                                 # today: scene + era refuse both
    assert names(punjabi_profile="off") == []
    assert names(punjabi_profile="auto") == ["A - One", "B - Two"]


def test_library_lockable_unlabelled_under_profile(monkeypatch):
    """Session 2026-09-30_102327: genre labels are in memory only, so after a server
    restart the deadline fallback had nothing. Under the profile an unlabelled song
    is kept, ranked after the labelled ones; off the profile it is still left out.
    A cached energy level within MAX_STEP of the playing song ranks first."""
    import app.ui.server as srv
    from app.music_brain import energy as en
    from app.music_brain.analyzer import KeyEstimate
    tracks = {"t1": Path("/x/a - One.mp3"), "t2": Path("/x/b - Two.mp3"), "t3": Path("/x/c - Three.mp3")}
    monkeypatch.setattr(srv, "_tracks", tracks)
    monkeypatch.setattr(srv, "_track_names", {"t1": "A - One", "t2": "B - Two", "t3": "C - Three"})
    monkeypatch.setattr(srv, "_suggested_genres", {srv._genre_key("Three"): "punjabi pop"})
    monkeypatch.setattr(srv, "_suggested_eras", {})
    fake = type("T", (), {"bpm": 88.0, "duration": 200.0, "key": KeyEstimate("8A", "", False, 0.9)})()
    monkeypatch.setattr(srv, "analyze_track", lambda p: fake)
    names = lambda **kw: [t["name"] for t in srv.get_library_lockable(bpm=88.0, genre="punjabi pop", **kw)["tracks"]]
    assert names() == ["C - Three"]                                      # off: today's filter
    assert names(punjabi_profile="auto") == ["C - Three", "A - One", "B - Two"]
    levels = {"/x/a - One.mp3": 4, "/x/b - Two.mp3": 7}
    monkeypatch.setattr(en, "_cache", lambda p: type("C", (), {"exists": lambda self: True})())
    monkeypatch.setattr(en, "level", lambda p, bpm: {"level": levels.get(str(p), 8)})
    assert names(punjabi_profile="auto", energy=8) == ["C - Three", "B - Two", "A - One"]


def test_match_endpoint_passes_profile(monkeypatch):
    import app.ui.server as srv
    seen = []

    class FakeMatcher:
        def match(self, a, b, top_n=3, **kw):
            seen.append(kw.get("profile"))
            return []
    monkeypatch.setattr(srv, "_matcher", FakeMatcher())
    monkeypatch.setattr(srv, "_track_path", lambda tid: f"/nowhere/{tid}.mp3")
    monkeypatch.setattr(srv, "analyze_track", lambda path: type("T", (), {"bpm": 128.0})())
    monkeypatch.setattr(srv, "_cached_vocal_regions", lambda tid: None)
    monkeypatch.setattr(srv, "_pair_vibe", lambda a, b: {"genre_a": "punjabi", "genre_b": "bhangra", "era_a": None, "era_b": None})
    for body, want in (({}, None), ({"punjabi_profile": "off"}, None), ({"punjabi_profile": "auto"}, "full")):
        srv.post_match(srv.MatchRequest(track_a_id="a", track_b_id="b", **body))
        assert seen[-1] == want


def test_off_equals_before_the_profile():
    """Same vectors, profile off (explicit and default), hash to what main made before this change."""
    for kw in ({}, {"match_kw": {"profile": None}, "filter_kw": {"punjabi_profile": "off"}}):
        now = json.loads(json.dumps(vectors.compute(**kw)))
        for k in ("match", "suggest"):
            assert len(now[k]) == GOLDEN["python"][k]["n"]
            assert hashlib.sha256(json.dumps(now[k], sort_keys=True).encode()).hexdigest() == GOLDEN["python"][k]["sha256"], k


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_copy_matches_python():
    probe = (
        f"const S = require({json.dumps(str(JS))});"
        "const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        "process.stdout.write(JSON.stringify({profile: S.PUNJABI_PROFILE, modes: S.MODES, def: S.DEFAULT_MODE,"
        " level: cases.level.map(([m, a, b]) => S.level(m, a, b)),"
        " fold: cases.fold.map(([a, b]) => S.foldBpm(a, b))}));"
    )
    labels = ["punjabi", "Bhangra", "desi", "desire", "punjabi hip-hop", "bollywood", "house", "", None, "Punjabi Pop"]
    cases = {"level": [[m, a, b] for m in ("auto", "on", "off", "x") for a in labels for b in labels],
             "fold": [[88, 176], [88, 130], [176, 88], [90, 45], [0, 100], [128, 124]]}
    out = subprocess.run(["node", "-e", probe], input=json.dumps(cases), capture_output=True, text=True, check=True)
    js = json.loads(out.stdout)
    py = {k: (list(v) if isinstance(v, tuple) else v) for k, v in sp.PUNJABI_PROFILE.items()}
    assert js["profile"] == py
    assert js["modes"] == list(sp.MODES) and js["def"] == sp.DEFAULT_MODE
    assert js["level"] == [sp.level(m, a, b) for m, a, b in cases["level"]]
    assert js["fold"] == [sp.fold_bpm(a, b) for a, b in cases["fold"]]
