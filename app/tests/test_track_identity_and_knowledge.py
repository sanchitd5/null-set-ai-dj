import pytest

from app.music_brain.dj_knowledge import MAX_BRIEF_CHARS, playbook_for, selection_brief
from app.ui.track_identity import clean_identity


@pytest.mark.parametrize("display,expected", [
    ("Fred again.., The Blessed Madonna - Marea (we’ve lost dancing)", ("Fred again..", "Marea (we’ve lost dancing)")),
    ("Skrillex, Fred again.. & Flowdan - Rumble [Official Audio]", ("Skrillex", "Rumble")),
    ("BICEP ｜ GLUE (Official Video)", ("Bicep", "Glue")),
    ("Disclosure - Latch (feat. Sam Smith)", ("Disclosure", "Latch")),
    ("Fred again.. & Baby Keem - leavemealone", ("Fred again..", "leavemealone")),
    ("Jamie xx - Gosh", ("Jamie xx", "Gosh")),
    ("Four Tet - Baby (Official Music Video)", ("Four Tet", "Baby")),
    ("Hackney Pigeon (Sammy Virji VIP)", ("Unknown", "Hackney Pigeon (Sammy Virji VIP)")),
    ("Daft Punk - Da Funk (Armand van Helden Remix)", ("Daft Punk", "Da Funk (Armand van Helden Remix)")),
])
def test_clean_identity(display, expected):
    assert clean_identity(display) == expected


def test_playbook_mapping():
    assert playbook_for("melodic house") == "Melodic Electronic Playbook"
    assert playbook_for("Drum & Bass") == "Drum & Bass (DnB) Playbook"
    assert playbook_for("tech house") == "House & Tech House Playbook"
    assert playbook_for("UK garage / deep house") == "House & Tech House Playbook"
    assert playbook_for("") is None


def test_selection_brief_grounded_and_capped():
    b = selection_brief("drum and bass")
    assert len(b) <= MAX_BRIEF_CHARS
    assert "GENRE PLAYBOOK (Drum & Bass (DnB) Playbook)" in b
    assert "Two Anchors" in b and "CONTINUITY OR CONTRAST" in b
    assert "[[" not in b and "**" not in b  # wiki markup stripped


def test_credited_artists():
    from app.ui.track_identity import credited_artists
    assert credited_artists("LATIN MAFIA, Fred again.. - Te Estoy Correteando") == ["LATIN MAFIA", "Fred again.."]
    assert credited_artists("Disclosure - Latch (feat. Sam Smith)") == ["Disclosure", "Sam Smith"]
    assert credited_artists("Jamie xx - Gosh") == ["Jamie xx"]
    assert credited_artists("Hackney Pigeon (Sammy Virji VIP)") == []


def test_steering_skips_profile_clash_filter():
    from app.ui.autopilot_service import _filter_suggestions
    cur = {"energy": 6, "tempo_feel": "driving", "mood": "bittersweet"}
    bhangra = {"energy": 9, "tempo_feel": "driving", "mood": "euphoric"}
    data = {"current_profile": {"energy": 3, "tempo_feel": "laid-back", "mood": "chill"},
            "suggestions": [{"artist": "Diljit Dosanjh", "title": "Proper Patola", "track_profile": bhangra},
                            {"artist": "Panjabi MC", "title": "Mundian To Bach Ke", "track_profile": bhangra}]}
    assert len(_filter_suggestions(dict(data, steering="stay"), [])) == 1    # continuity keeps only the closest clash
    assert len(_filter_suggestions(dict(data, steering="move"), [])) == 2    # steering: both kept


def test_repeat_filter_catches_remix_and_feat_variants():
    from app.ui.autopilot_service import _filter_suggestions
    data = {"suggestions": [
        {"artist": "Badshah", "title": "Proper Patola (Remix) [feat. Diljit Dosanjh]"},
        {"artist": "Panjabi MC", "title": "Mundian To Bach Ke (Bhangra Remix)"},
        {"artist": "AP Dhillon", "title": "Excuses (Remix) - DJ Cut"},
        {"artist": "Divine", "title": "Bombay Slums"},
    ]}
    played = ["Diljit Dosanjh - Proper Patola", "Panjabi MC - Mundian To Bach Ke", "AP Dhillon - Excuses"]
    assert [s["title"] for s in _filter_suggestions(data, played)] == ["Bombay Slums"]


def test_theme_lock_drops_off_theme_suggestions():
    from app.ui.autopilot_service import _filter_suggestions
    data = {"steering": "stay", "occasion_fit": 9, "suggestions": [
        {"artist": "Divine", "title": "Bombay Slums", "occasion_fit": 4},
        {"artist": "Ritviz", "title": "Udd Gaye", "occasion_fit": 3},
        {"artist": "Karan Aujla", "title": "Tauba Tauba", "occasion_fit": 9},
    ]}
    assert [s["artist"] for s in _filter_suggestions(data, [], occasion_set=True)] == ["Karan Aujla"]
    assert len(_filter_suggestions(data, [], occasion_set=False)) == 3      # no occasion: no theme lock
    all_off = {"steering": "stay", "suggestions": data["suggestions"][:2]}
    assert [s["artist"] for s in _filter_suggestions(all_off, [], occasion_set=True)] == ["Divine"]  # best of the rest
    steer = {"steering": "move", "occasion_fit": 3, "suggestions": [
        {"artist": "X", "title": "Worse Bridge", "occasion_fit": 2},
        {"artist": "Y", "title": "Closer Bridge", "occasion_fit": 5}]}
    assert [s["title"] for s in _filter_suggestions(steer, [], occasion_set=True)] == ["Closer Bridge"]


def test_bullet_and_colon_separators_split_artist_and_title():
    from app.ui.track_identity import clean_identity
    a, ti = clean_identity("RÜFÜS DU SOL ●● Treat You Better (Official Single Edit Video)")
    assert (a.lower(), ti) == ("rüfüs du sol", "Treat You Better")
    assert clean_identity("SIDHU MOOSE WALA ： DOLLAR ｜ BYG BYRD")[0].lower() == "sidhu moose wala"


def test_other_uploads_and_early_remixes_of_a_played_song_are_dropped(monkeypatch):
    import json
    import app.ui.autopilot_service as svc
    reply = {"current_genre": "melodic house", "suggestions": [
        {"artist": "RÜFÜS DU SOL", "title": "Treat You Better (Purple Disco Machine Remix)", "genre": "melodic house", "genre_hop": 0, "expected_bpm": 120},
        {"artist": "Lane 8", "title": "Brightest Lights", "genre": "melodic house", "genre_hop": 0, "expected_bpm": 120}]}
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: json.dumps(reply))
    out = svc.suggest_next_tracks("After Earth", "Ben Böhmer", 120.0, "8A", 300.0, 0.5, "",
                                  ["RÜFÜS DU SOL ●● Treat You Better (Official Single Edit Video)"])
    assert [s["title"] for s in out] == ["Brightest Lights"]


def test_remix_of_a_song_played_long_ago_is_welcome(monkeypatch):
    import json
    import app.ui.autopilot_service as svc
    reply = {"current_genre": "melodic house", "suggestions": [
        {"artist": "RÜFÜS DU SOL", "title": "Treat You Better (Purple Disco Machine Remix)", "genre": "melodic house", "genre_hop": 0, "expected_bpm": 120},
        {"artist": "RÜFÜS DU SOL", "title": "Treat You Better", "genre": "melodic house", "genre_hop": 0, "expected_bpm": 120}]}
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: json.dumps(reply))
    history = ["RÜFÜS DU SOL ●● Treat You Better (Official Single Edit Video)"] + [f"Artist {i} - Song {i}" for i in range(9)]
    out = svc.suggest_next_tracks("After Earth", "Ben Böhmer", 120.0, "8A", 300.0, 0.5, "", history)
    assert [s["title"] for s in out] == ["Treat You Better (Purple Disco Machine Remix)"]   # the original never again


def test_unplayed_remixes_pass():
    import app.ui.autopilot_service as svc
    data = {"current_genre": "house", "suggestions": [
        {"artist": "Lane 8", "title": "Brightest Lights (Yotto Remix)", "genre": "house", "genre_hop": 0}]}
    assert [s["title"] for s in svc._filter_suggestions(data, [])] == ["Brightest Lights (Yotto Remix)"]
