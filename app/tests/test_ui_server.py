"""Tests for ui.server (Phase 5 FastAPI web workbench) via FastAPI's
TestClient. Exercises the full loop from spec 4: upload -> analyze ->
match -> preview.
"""

import pytest

from app.music_brain.config import ROOT_DIR
from app.music_brain.knowledge_parser import KnowledgeParser
from app.tests.testclient_compat import TestClient
from app.ui.server import app

SAMPLE_A = ROOT_DIR / "data" / "songs" / "input.mp3"
SAMPLE_B = ROOT_DIR / "data" / "songs" / "input2.mp3"

pytestmark = pytest.mark.skipif(
    not (SAMPLE_A.exists() and SAMPLE_B.exists()), reason="sample tracks not present"
)

client = TestClient(app)


def _upload(path) -> str:
    with open(path, "rb") as f:
        res = client.post("/api/tracks", files={"file": (path.name, f, "audio/mpeg")})
    assert res.status_code == 200
    return res.json()["track_id"]


def test_get_recipes_returns_all():
    res = client.get("/api/recipes")
    assert res.status_code == 200
    assert len(res.json()["recipes"]) == len(KnowledgeParser())


def test_upload_returns_track_id():
    track_id = _upload(SAMPLE_A)
    assert isinstance(track_id, str) and len(track_id) > 0


def test_upload_same_file_twice_returns_same_track_id():
    id1 = _upload(SAMPLE_A)
    id2 = _upload(SAMPLE_A)
    assert id1 == id2  # content-hashed track_id


def test_list_tracks_includes_uploaded():
    track_id = _upload(SAMPLE_A)
    res = client.get("/api/tracks")
    ids = [t["track_id"] for t in res.json()["tracks"]]
    assert track_id in ids


def test_analysis_endpoint_for_unknown_track_returns_404():
    res = client.get("/api/tracks/doesnotexist/analysis")
    assert res.status_code == 404


def test_analysis_endpoint_returns_analysis():
    track_id = _upload(SAMPLE_A)
    res = client.get(f"/api/tracks/{track_id}/analysis")
    assert res.status_code == 200
    payload = res.json()
    assert payload["bpm"] > 0
    assert "key" in payload


def test_match_endpoint_returns_candidates():
    id_a = _upload(SAMPLE_A)
    id_b = _upload(SAMPLE_B)
    res = client.post("/api/match", json={"track_a_id": id_a, "track_b_id": id_b, "top_n": 3})
    assert res.status_code == 200
    assert len(res.json()["candidates"]) == 3


def test_match_endpoint_unknown_track_404():
    id_a = _upload(SAMPLE_A)
    res = client.post("/api/match", json={"track_a_id": id_a, "track_b_id": "nope", "top_n": 3})
    assert res.status_code == 404


def test_preview_endpoint_end_to_end_and_audio_download():
    id_a = _upload(SAMPLE_A)
    id_b = _upload(SAMPLE_B)
    res = client.post("/api/preview", json={
        "track_a_id": id_a, "track_b_id": id_b, "recipe": "Bass Swap", "preview_seconds": 8.0,
    })
    assert res.status_code == 200
    payload = res.json()
    assert payload["recipe"] == "Bass Swap"
    assert payload["peak_dbfs"] <= 0.0

    audio_res = client.get(payload["audio_url"])
    assert audio_res.status_code == 200
    assert audio_res.headers["content-type"] == "audio/mpeg"


def test_preview_endpoint_unknown_recipe_400():
    id_a = _upload(SAMPLE_A)
    id_b = _upload(SAMPLE_B)
    res = client.post("/api/preview", json={
        "track_a_id": id_a, "track_b_id": id_b, "recipe": "Not A Recipe",
    })
    assert res.status_code == 400


def test_render_endpoint_end_to_end_and_audio_download():
    id_a = _upload(SAMPLE_A)
    id_b = _upload(SAMPLE_B)
    res = client.post("/api/render", json={
        "track_a_id": id_a, "track_b_id": id_b, "recipe": "Bass Swap",
    })
    assert res.status_code == 200
    payload = res.json()
    assert payload["recipe"] == "Bass Swap"
    assert payload["duration_seconds"] > 0

    audio_res = client.get(payload["audio_url"])
    assert audio_res.status_code == 200
    assert audio_res.headers["content-type"] == "audio/mpeg"


def test_render_endpoint_unknown_recipe_400():
    id_a = _upload(SAMPLE_A)
    id_b = _upload(SAMPLE_B)
    res = client.post("/api/render", json={
        "track_a_id": id_a, "track_b_id": id_b, "recipe": "Not A Recipe",
    })
    assert res.status_code == 400


def test_track_audio_download():
    track_id = _upload(SAMPLE_A)
    res = client.get(f"/api/audio/tracks/{track_id}")
    assert res.status_code == 200


# --- Sampler one-shots & mix recordings ---------------------------------------
# These endpoints are content-addressed blob storage, not analysis: arbitrary
# bytes are enough, no decodable audio needed.

SAMPLE_BYTES = b"RIFF\x00\x00\x00\x00WAVEfake-one-shot-payload"
RECORDING_BYTES = b"\x1a\x45\xdf\xa3fake-webm-mix-recording"


def _upload_sample(payload: bytes, filename: str = "kick.wav", label=None) -> dict:
    data = {"label": label} if label is not None else None
    res = client.post(
        "/api/samples",
        files={"file": (filename, payload, "audio/wav")},
        data=data,
    )
    assert res.status_code == 200
    return res.json()


def test_upload_sample_returns_id_and_url():
    payload = _upload_sample(SAMPLE_BYTES, label="Kick 01")
    assert isinstance(payload["sample_id"], str) and len(payload["sample_id"]) > 0
    assert payload["url"] == f"/api/samples/{payload['sample_id']}"
    assert payload["label"] == "Kick 01"


def test_upload_sample_without_label_falls_back_to_filename():
    payload = _upload_sample(b"unlabeled-one-shot", filename="clap.wav")
    assert payload["label"] == "clap"


def test_upload_same_sample_twice_returns_same_id():
    id1 = _upload_sample(SAMPLE_BYTES)["sample_id"]
    id2 = _upload_sample(SAMPLE_BYTES)["sample_id"]
    assert id1 == id2  # content-hashed sample_id


def test_list_samples_includes_uploaded():
    payload = _upload_sample(SAMPLE_BYTES, label="Kick 01")
    res = client.get("/api/samples")
    assert res.status_code == 200
    entry = next(s for s in res.json()["samples"] if s["sample_id"] == payload["sample_id"])
    assert entry["label"] == "Kick 01"
    assert entry["url"] == payload["url"]


def test_sample_download_round_trips_bytes():
    payload = _upload_sample(SAMPLE_BYTES)
    res = client.get(payload["url"])
    assert res.status_code == 200
    assert res.content == SAMPLE_BYTES


def test_sample_upload_rejects_non_audio_suffix():
    # served back same-origin by suffix, so .html would be stored XSS
    res = client.post("/api/samples", files={"file": ("pad.html", b"<script>1</script>", "text/html")})
    assert res.status_code == 400


def test_sample_upload_rejects_empty_file():
    res = client.post("/api/samples", files={"file": ("empty.wav", b"", "audio/wav")})
    assert res.status_code == 400


def test_sample_upload_rejects_oversize(monkeypatch):
    import app.ui.server as server
    monkeypatch.setattr(server, "SAMPLE_MAX_BYTES", 8)
    res = client.post("/api/samples", files={"file": ("big.wav", b"0123456789", "audio/wav")})
    assert res.status_code == 413


def test_sample_download_unknown_id_404():
    res = client.get("/api/samples/doesnotexist")
    assert res.status_code == 404


def _upload_recording(payload: bytes, filename: str = "mix.webm") -> dict:
    res = client.post(
        "/api/recordings",
        files={"file": (filename, payload, "audio/webm")},
    )
    assert res.status_code == 200
    return res.json()


def test_upload_recording_returns_id_and_url():
    payload = _upload_recording(RECORDING_BYTES)
    assert isinstance(payload["recording_id"], str) and len(payload["recording_id"]) > 0
    assert payload["url"] == f"/api/recordings/{payload['recording_id']}"


def test_upload_same_recording_twice_returns_same_id():
    id1 = _upload_recording(RECORDING_BYTES)["recording_id"]
    id2 = _upload_recording(RECORDING_BYTES)["recording_id"]
    assert id1 == id2  # content-hashed recording_id


def test_recording_download_round_trips_bytes():
    payload = _upload_recording(RECORDING_BYTES)
    res = client.get(payload["url"])
    assert res.status_code == 200
    assert res.content == RECORDING_BYTES
    assert res.headers["content-type"] == "audio/webm"


def test_recording_download_unknown_id_404():
    res = client.get("/api/recordings/doesnotexist")
    assert res.status_code == 404
