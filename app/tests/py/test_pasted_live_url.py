from app.ui import download_service as ds


def test_pasted_url_may_be_a_live_version_search_may_not():
    info = {"title": "Anyma - Atoma [Live from ÆDEN Mexico City]", "duration": 152}
    assert ds._search_match_filter([], check_live=False)(info) is None          # direct URL the user pasted
    assert "live" in (ds._search_match_filter([], check_live=True)(info) or "")  # a search still skips live uploads
