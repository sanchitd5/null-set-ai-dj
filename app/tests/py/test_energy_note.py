"""/api/autopilot/suggest energy_note hints (set study gfF8jzBVWvM rules 7 and 9).

Kept out of test_ui_server.py so it runs without TestClient (no uploads needed).
"""

from app.ui.server import _occasion_with_note


def test_no_or_unknown_note_leaves_occasion():
    assert _occasion_with_note("wedding", None) == "wedding"
    assert _occasion_with_note("wedding", "bogus") == "wedding"
    assert _occasion_with_note(None, None) == ""


def test_dip_note():
    assert "dip" in _occasion_with_note("wedding", "dip")
    assert _occasion_with_note(None, "dip").startswith("(")


def test_callback_note():
    out = _occasion_with_note("club", "callback")
    assert out.startswith("club (") and "opening" in out


# research/notes/set-study-mDtud5fLgFQ.md section 5: repeated-hook reprise
def test_reprise_note_names_the_hook():
    out = _occasion_with_note("club", "reprise", hook="LATIN MAFIA - Quiereme")
    assert out.startswith("club (") and '"LATIN MAFIA - Quiereme"' in out
    assert "different version" in out


def test_reprise_without_hook_adds_nothing():
    assert _occasion_with_note("club", "reprise") == "club"
    assert _occasion_with_note("club", "reprise", hook="  \n ") == "club"


def test_reprise_hook_is_sanitised():
    out = _occasion_with_note(None, "reprise", hook='a"b\x00\n' + "x" * 500)
    assert "\x00" not in out and "\n" not in out and 'a\'b' in out
    assert len(out) < 400
