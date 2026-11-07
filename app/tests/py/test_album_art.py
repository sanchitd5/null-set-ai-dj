"""Album art: source chain, matching, caches, resize, route, art_url. No real network: every source's
HTTP response is mocked (album_art._http_get)."""
import io
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image

from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import album_art as aa


def _jpeg(w=1000, h=1000, color=(200, 30, 30)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "JPEG")
    return b.getvalue()


ITUNES = {"results": [{"artistName": "Bicep", "trackName": "Glue",
                       "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/x/100x100bb.jpg"}]}
DEEZER = {"data": [{"title": "Glue", "artist": {"name": "Bicep"}, "album": {"cover_xl": "https://cdn-images.dzcdn.net/c/1000x1000.jpg"}}]}
MB = {"recordings": [{"title": "Glue", "artist-credit": [{"name": "Bicep"}], "releases": [{"id": "rel-1"}]}]}


class Net:
    """Routes aa._http_get by URL; records every call. routes: substring -> bytes | dict | Exception."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, headers=None, timeout=0):
        self.calls.append(url)
        for key, val in self.routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return json.dumps(val).encode() if isinstance(val, dict) else val
        raise OSError("404 " + url)

    def hosts(self):
        return [u.split("/")[2] for u in self.calls]


@pytest.fixture(autouse=True)
def art_env(monkeypatch, tmp_path):
    monkeypatch.setattr(aa, "art_dir", lambda: tmp_path / "art")
    monkeypatch.setattr(aa, "_sleep", lambda s: None)
    monkeypatch.setattr(aa, "embedded_art", lambda p: None)
    monkeypatch.setattr(aa, "_last_hit", {})
    return tmp_path


def net(monkeypatch, routes):
    n = Net(routes)
    monkeypatch.setattr(aa, "_http_get", n)
    return n


def test_chain_order_itunes_first_and_size_swapped(monkeypatch):
    n = net(monkeypatch, {"itunes.apple.com": ITUNES, "mzstatic.com": _jpeg(), "dzcdn": _jpeg()})
    meta = aa.find_art("t1", None, "Bicep - Glue (Official Video)")
    assert meta["source"] == "itunes" and meta["score"] > 0.9
    assert n.hosts() == ["itunes.apple.com", "is1-ssl.mzstatic.com"]      # later sources never touched
    assert n.calls[1].endswith("/600x600bb.jpg")
    assert "term=Bicep%20Glue" in n.calls[0]                              # artist + title only


def test_falls_through_itunes_deezer_musicbrainz_youtube(monkeypatch):
    n = net(monkeypatch, {"itunes.apple.com": {"results": []}, "deezer.com/search": {"data": []},
                          "musicbrainz.org": {"recordings": []}, "i.ytimg.com/vi/abcdefghijk/maxres": _jpeg(1280, 720)})
    aa.remember_source("t2", "https://www.youtube.com/watch?v=abcdefghijk&list=x")
    meta = aa.find_art("t2", None, "Bicep - Glue")
    assert meta["source"] == "youtube"
    assert n.hosts() == ["itunes.apple.com", "api.deezer.com", "api.deezer.com", "musicbrainz.org", "i.ytimg.com"]  # deezer: fielded + plain query
    assert Image.open(aa.art_path("t2")).size == (600, 600)               # 720 high crop, scaled to 600


def test_deezer_then_musicbrainz_with_cover_archive(monkeypatch):
    n = net(monkeypatch, {"itunes.apple.com": {"results": []}, "deezer.com/search": {"data": []},
                          "musicbrainz.org": MB, "coverartarchive.org/release/rel-1/front-500": _jpeg(500, 500)})
    meta = aa.find_art("t3", None, "Bicep - Glue")
    assert meta["source"] == "musicbrainz"
    n2 = net(monkeypatch, {"itunes.apple.com": {"results": []}, "deezer.com/search": DEEZER, "dzcdn.net": _jpeg()})
    assert aa.find_art("t4", None, "Bicep - Glue")["source"] == "deezer"
    assert n2.hosts()[-1] == "cdn-images.dzcdn.net"


def test_embedded_wins_without_any_network(monkeypatch):
    n = net(monkeypatch, {})
    monkeypatch.setattr(aa, "embedded_art", lambda p: _jpeg(700, 700))
    assert aa.find_art("t5", Path("x.mp3"), "Bicep - Glue")["source"] == "embedded"
    assert n.calls == []


def test_wrong_artist_is_rejected_no_art_and_negative_cached(monkeypatch):
    other = {"results": [{"artistName": "Somebody Else", "trackName": "Glue",
                          "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg"}]}
    n = net(monkeypatch, {"itunes.apple.com": other, "deezer.com/search": {"data": []}, "musicbrainz.org": {"recordings": []}})
    assert aa.find_art("t6", None, "Bicep - Glue") is None
    assert not aa.has_art("t6") and aa.art_url("t6") is None
    assert not any("mzstatic" in u for u in n.calls)                      # the wrong cover was never downloaded
    meta = aa.read_meta("t6")
    assert meta["source"] is None and meta["retry_after"] > time.time() + 6 * 86400
    assert aa.needs_fetch("t6", "Bicep - Glue") is False                  # negative cache holds...
    assert aa.needs_fetch("t6", "Bicep - Glue (Other Remix)") is True     # ...until the name changes
    meta["retry_after"] = time.time() - 1
    aa._meta_path("t6").write_text(json.dumps(meta))
    assert aa.needs_fetch("t6", "Bicep - Glue") is True                   # ...or the retry time passes


def test_unreachable_sources_retry_sooner(monkeypatch):
    net(monkeypatch, {"itunes.apple.com": OSError("down"), "deezer": OSError("down"), "musicbrainz": OSError("down")})
    assert aa.find_art("t7", None, "Bicep - Glue") is None
    assert aa.read_meta("t7")["retry_after"] < time.time() + 2 * 3600


def test_no_artist_means_no_api_lookup(monkeypatch):
    n = net(monkeypatch, {})
    assert aa.find_art("t8", None, "untitled jam") is None
    assert n.calls == []


def test_remix_scoring():
    c = lambda t, a="Bicep": aa.Cand(a, t, "x")
    want_orig, want_remix = aa.ident_from_name("Bicep - Glue"), aa.ident_from_name("Bicep - Glue (Other Remix)")
    assert want_remix.remix == "Other Remix" and want_remix.title == "Glue"
    exact = aa.score_candidate(want_remix, c("Glue - Other Remix"))
    orig = aa.score_candidate(want_remix, c("Glue"))
    assert exact > orig > 0                                               # exact remix preferred, original accepted
    assert aa.score_candidate(want_remix, c("Glue (Third Remix)")) == 0   # a different remix
    assert aa.score_candidate(want_remix, c("Glue", a="Nobody")) == 0     # original cover needs the artist
    assert aa.score_candidate(want_orig, c("Glue (Other Remix)")) == 0    # don't give a remix's cover to the original
    assert aa.score_candidate(want_orig, c("Glue - Radio Edit")) > 0.9    # plain tags are not remixes
    assert aa.score_candidate(want_orig, c("Glue", a="Bicep, Someone")) > 0.8
    assert aa.score_candidate(want_orig, c("Different Song")) == 0


def test_remix_chain_prefers_exact_over_original(monkeypatch):
    res = {"results": [
        {"artistName": "Bicep", "trackName": "Glue", "artworkUrl100": "https://is1-ssl.mzstatic.com/orig/100x100bb.jpg"},
        {"artistName": "Bicep", "trackName": "Glue (Other Remix)", "artworkUrl100": "https://is1-ssl.mzstatic.com/rmx/100x100bb.jpg"}]}
    n = net(monkeypatch, {"itunes.apple.com": res, "/rmx/": _jpeg(), "/orig/": _jpeg()})
    assert aa.find_art("t9", None, "Bicep - Glue (Other Remix)")["source"] == "itunes"
    assert "/rmx/600x600bb.jpg" in n.calls[1] and len(n.calls) == 2


def test_image_host_allowlist(monkeypatch):
    bad = {"results": [{"artistName": "Bicep", "trackName": "Glue", "artworkUrl100": "https://evil.example/100x100bb.jpg"}]}
    n = net(monkeypatch, {"itunes.apple.com": bad, "deezer.com/search": {"data": []}, "musicbrainz.org": {"recordings": []}})
    assert aa.find_art("t10", None, "Bicep - Glue") is None
    assert not any("evil" in u for u in n.calls)


def test_rate_limit_per_source(monkeypatch):
    sleeps, clock = [], [100.0]
    monkeypatch.setattr(aa, "_now", lambda: clock[0])
    monkeypatch.setattr(aa, "_sleep", lambda s: (sleeps.append(round(s, 2)), clock.__setitem__(0, clock[0] + s)))
    aa._throttle("musicbrainz")
    aa._throttle("musicbrainz")
    aa._throttle("deezer")
    assert sleeps == [1.1]                                                # MB 1 req/s honoured; deezer's own clock


def test_resize_sizes_pillow_and_ffmpeg_fallback(monkeypatch):
    big, small = aa.make_variants(_jpeg(1600, 900))
    assert Image.open(io.BytesIO(big)).size == (600, 600) and Image.open(io.BytesIO(small)).size == (160, 160)
    big, small = aa.make_variants(_jpeg(300, 300))
    assert Image.open(io.BytesIO(big)).size == (300, 300)                 # never upscaled
    if shutil.which("ffmpeg"):
        monkeypatch.setattr(aa, "_pil", lambda: None)
        big, small = aa.make_variants(_jpeg(1600, 900))
        assert Image.open(io.BytesIO(big)).size == (600, 600) and Image.open(io.BytesIO(small)).size == (160, 160)


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg missing")
def test_embedded_extraction_from_m4a(tmp_path, monkeypatch):
    monkeypatch.undo()
    cover = tmp_path / "c.jpg"
    cover.write_bytes(_jpeg(500, 500, (10, 200, 10)))
    m4a, plain = tmp_path / "t.m4a", tmp_path / "p.m4a"
    base = ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    subprocess.run(base + ["-i", str(cover), "-map", "0:a", "-map", "1:v", "-c:a", "aac", "-c:v", "mjpeg",
                           "-disposition:v", "attached_pic", str(m4a)], check=True)
    subprocess.run(base + ["-c:a", "aac", str(plain)], check=True)
    raw = aa.embedded_art(m4a)
    assert raw and Image.open(io.BytesIO(raw)).size == (500, 500)
    assert aa.embedded_art(plain) is None


def test_worker_queue_dedupes_and_skips_resolver_none(monkeypatch):
    seen = []
    monkeypatch.setenv("DJ_ART_FETCH", "1")
    monkeypatch.setattr(aa, "TRACK_GAP_S", 0)
    monkeypatch.setattr(aa, "find_art", lambda tid, p, name: seen.append((tid, name)))
    monkeypatch.setattr(aa, "_resolver", lambda tid: None if tid == "mix" else (None, "Bicep - Glue"))
    for tid in ("a1", "a1", "mix", "a2"):
        aa.enqueue(tid)
    deadline = time.time() + 5
    while time.time() < deadline and len(seen) < 2:
        time.sleep(0.02)
    assert sorted(seen) == [("a1", "Bicep - Glue"), ("a2", "Bicep - Glue")]


# ---- route + art_url in responses -------------------------------------------------------------
@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def lib(monkeypatch, tmp_path):
    p = tmp_path / "s.mp3"
    p.write_bytes(b"x")
    monkeypatch.setattr(server, "_tracks", {"aaa1": p, "bbb2": p})
    monkeypatch.setattr(server, "_track_names", {"aaa1": "Bicep - Glue", "bbb2": "Other - Song"})
    return p


def test_route_auth_404_etag_sizes(client, lib, monkeypatch):
    net(monkeypatch, {"itunes.apple.com": ITUNES, "mzstatic.com": _jpeg()})
    assert aa.find_art("aaa1", lib, "Bicep - Glue")
    assert TestClient(server.app, auth=False).get("/api/tracks/aaa1/art").status_code in (401, 403)
    assert client.get("/api/tracks/bbb2/art").status_code == 404          # no art
    assert client.get("/api/tracks/nope/art").status_code == 404
    assert client.get("/api/tracks/aaa1/art?size=77").status_code == 400
    r = client.get("/api/tracks/aaa1/art?size=600")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "max-age" in r.headers["cache-control"] and r.headers["etag"]
    assert Image.open(io.BytesIO(r.content)).size == (600, 600)
    assert Image.open(io.BytesIO(client.get("/api/tracks/aaa1/art?size=160").content)).size == (160, 160)
    r2 = client.get("/api/tracks/aaa1/art", headers={"If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304 and r2.content == b""


def test_art_url_in_track_list_analysis_and_mobile(client, lib, monkeypatch):
    net(monkeypatch, {"itunes.apple.com": ITUNES, "mzstatic.com": _jpeg()})
    aa.find_art("aaa1", lib, "Bicep - Glue")
    rows = {t["track_id"]: t for t in client.get("/api/tracks").json()["tracks"]}
    assert rows["aaa1"]["art_url"].startswith("/api/tracks/aaa1/art?size=600&v=") and rows["bbb2"]["art_url"] is None
    lib_rows = {t["track_id"]: t for t in client.get("/api/mobile/library").json()["tracks"]}
    assert lib_rows["aaa1"]["art_url"] == rows["aaa1"]["art_url"] and lib_rows["bbb2"]["art_url"] is None
    assert client.get("/api/mobile/tracks/aaa1").json()["art_url"] == rows["aaa1"]["art_url"]
    from types import SimpleNamespace
    monkeypatch.setattr(server, "analyze_track", lambda p: SimpleNamespace(to_dict=lambda: {"bpm": 120.0}))
    a = client.get("/api/tracks/aaa1/analysis").json()
    assert a["bpm"] == 120.0 and a["art_url"] == rows["aaa1"]["art_url"]
    assert client.get(rows["aaa1"]["art_url"]).status_code == 200
