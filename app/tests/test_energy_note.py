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
