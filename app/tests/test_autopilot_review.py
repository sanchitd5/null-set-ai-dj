"""Autopilot suggest review fixes: loudness (6), ALLOWED KEYS (7), key validation +
BPM coercion (8), few-shot schema (9), set position by clock + tempo bridge (10)."""

import json
import re
from types import SimpleNamespace

import pytest

import app.ui.autopilot_service as svc


def _capture(monkeypatch, reply):
    seen = {}

    def fake(system, user, **kw):
        seen["system"], seen["user"] = system, user
        return json.dumps(reply)

    monkeypatch.setattr(svc, "chat_raw", fake)
    return seen


def _sug(title, key, bpm=124, steer=None):
    return {"artist": "X", "title": title, "expected_key": key, "expected_bpm": bpm}


# --- 6 loudness -------------------------------------------------------------

def test_loudness_label():
    assert svc.loudness_label(-9.2) == "-9 dBFS (loud)"
    assert svc.loudness_label(-12) == "-12 dBFS (medium)"
    assert svc.loudness_label(-18) == "-18 dBFS (quiet)"
    assert svc.loudness_label(None) == "unknown"


def test_prompt_carries_loudness(monkeypatch):
    seen = _capture(monkeypatch, {"suggestions": []})
    svc.suggest_next_tracks("T", "A", 124.0, "8A", 200.0, 0.5, "", [], loudness_dbfs=-8.7)
    assert "Loudness: -9 dBFS (loud)" in seen["user"]


# --- 7 allowed keys ---------------------------------------------------------

def test_allowed_keys_line():
    assert svc.allowed_keys("8A") == "8A, 7A, 9A, 8B, 10A (+2 boost), 7B/9B (diagonal)"
    assert svc.allowed_keys("12B") == "12B, 11B, 1B, 12A, 2B (+2 boost), 11A/1A (diagonal)"
    assert svc.allowed_keys("1a").startswith("1A, 12A, 2A, 1B, 3A")
    assert "unknown" in svc.allowed_keys("unknown")


def test_prompt_has_allowed_keys_next_to_tempo(monkeypatch):
    seen = _capture(monkeypatch, {"suggestions": []})
    svc.suggest_next_tracks("T", "A", 124.0, "8A", 200.0, 0.5, "", [])
    lines = seen["user"].splitlines()
    i = next(k for k, l in enumerate(lines) if l.startswith("TEMPO WINDOW"))
    assert lines[i + 1].startswith("ALLOWED KEYS") and "10A (+2 boost)" in lines[i + 1]


# --- 8 validation -----------------------------------------------------------

def test_clashing_expected_key_dropped(monkeypatch):
    _capture(monkeypatch, {"suggestions": [_sug("Near", "9A"), _sug("Far", "2A"), _sug("Unk", "")]})
    out = svc.suggest_next_tracks("T", "A", 124.0, "8A", 200.0, 0.5, "", [])
    assert [s["title"] for s in out] == ["Near", "Unk"]


def test_steering_keeps_clashing_keys(monkeypatch):
    _capture(monkeypatch, {"steering": "move", "suggestions": [_sug("Far", "2A"), _sug("Near", "8B")]})
    out = svc.suggest_next_tracks("T", "A", 124.0, "8A", 200.0, 0.5, "", [])
    assert {s["title"] for s in out} == {"Far", "Near"}


def test_all_clash_keeps_the_best_one(monkeypatch):
    _capture(monkeypatch, {"suggestions": [_sug("Far1", "2A"), _sug("Far2", "3B")]})
    out = svc.suggest_next_tracks("T", "A", 124.0, "8A", 200.0, 0.5, "", [])
    assert [s["title"] for s in out] == ["Far1"]
    assert "key clash" in out[0]["rejected_reason"]


@pytest.mark.parametrize("raw, want", [(124, 124.0), ("124", 124.0), ("~126 BPM", 126.0),
                                       ("fast", None), (None, None), (0, None)])
def test_expected_bpm_coerced(monkeypatch, raw, want):
    _capture(monkeypatch, {"suggestions": [_sug("S", "8A", bpm=raw)]})
    out = svc.suggest_next_tracks("T", "A", 124.0, "8A", 200.0, 0.5, "", [])
    assert out[0]["expected_bpm"] == want


# --- 9 few-shot -------------------------------------------------------------

def _examples():
    blocks = re.split(r"\n\s*\n(?=EXAMPLE )", svc._FEW_SHOT)
    out = {}
    for b in blocks:
        name = b.split("\n", 1)[0]
        body = b[b.index("{"):]
        out[name] = json.loads(body)
    return out


def test_few_shot_json_parses_and_new_examples_use_current_schema():
    ex = _examples()
    assert len(ex) == 4
    new = [v for k, v in ex.items() if k.startswith(("EXAMPLE 3", "EXAMPLE 4"))]
    assert len(new) == 2
    for d in new:
        assert {"steering", "occasion_fit", "current_genre", "current_profile"} <= set(d)
        assert len(d["suggestions"]) == 3
        for s in d["suggestions"]:
            assert {"occasion_fit", "track_profile", "genre", "vibe_link", "expected_key"} <= set(s)
    assert "half-time" in svc._FEW_SHOT.lower() and "drum & bass" in svc._FEW_SHOT


def test_few_shot_examples_obey_allowed_keys():
    ex = _examples()
    currents = re.findall(r"^Current: .*?\| [\d.]+ BPM \| (\d{1,2}[AB]) \|", svc._FEW_SHOT, re.M)
    for cur, d in zip(currents, ex.values()):
        for s in d["suggestions"]:
            assert svc._key_clash_reason(cur, s["expected_key"]) is None


def test_system_prompt_stays_small():
    assert len(svc._SYSTEM) < 16000  # ~4k tokens: fits gemma3:4b's context with room


# --- 10 tempo bridge + set position by clock ---------------------------------

def test_tempo_target_replaces_tempo_window_without_occasion(monkeypatch):
    seen = _capture(monkeypatch, {"suggestions": [_sug("S", "8A", bpm=130)]})
    calls = {}
    real = svc._filter_suggestions

    def spy(data, history, occasion_set=False, current_key=None):
        calls["occasion_set"] = occasion_set
        return real(data, history, occasion_set=occasion_set, current_key=current_key)

    monkeypatch.setattr(svc, "_filter_suggestions", spy)
    svc.suggest_next_tracks("T", "A", 124.0, "8A", 200.0, 0.5, "", [],
                            tempo_target=130.0, tempo_note="bridge step 2/5 toward 174 BPM")
    assert "TEMPO BRIDGE: pick songs natively near 130 BPM" in seen["user"]
    assert "bridge step 2/5 toward 174 BPM" in seen["user"]
    assert "TEMPO WINDOW" not in seen["user"]
    assert calls["occasion_set"] is False
    assert "Occasion: general DJ set" in seen["user"]


def _run_server_suggest(monkeypatch, **req):
    from app.music_brain import vibe
    from app.ui import server

    captured = {}

    def fake_suggest(**kw):
        captured.update(kw)
        return []

    analysis = SimpleNamespace(energy_curve=[0.5], key=SimpleNamespace(camelot="8A"),
                               bpm=124.0, duration=200.0)
    monkeypatch.setattr(server, "_track_path", lambda tid: server.Path(f"/tmp/{tid}.mp3"))
    monkeypatch.setattr(server, "analyze_track", lambda p: analysis)
    monkeypatch.setattr(server, "_set_memory", SimpleNamespace(record=lambda h: None,
                                                               earlier_sets=lambda h: []))
    monkeypatch.setattr(svc, "suggest_next_tracks", fake_suggest)
    monkeypatch.setattr(vibe, "analyze_vibe", lambda p: SimpleNamespace(loudness_dbfs=-9.0))
    out = server.autopilot_suggest(server.AutopilotSuggestRequest(track_id="t", **req))
    return out, captured


def test_set_position_by_elapsed_time(monkeypatch):
    out, kw = _run_server_suggest(monkeypatch, history=["a"] * 9,
                                  elapsed_seconds=900, set_length_seconds=3600)
    assert out["set_position"] == 0.25 and kw["set_position"] == 0.25
    assert kw["loudness_dbfs"] == -9.0


def test_set_position_elapsed_default_length_and_fallbacks(monkeypatch):
    out, _ = _run_server_suggest(monkeypatch, elapsed_seconds=1800)
    assert out["set_position"] == 0.5  # DEFAULT_SET_LENGTH_S = 1 h
    out, _ = _run_server_suggest(monkeypatch, history=["a"] * 3)
    assert out["set_position"] == 0.3  # unchanged: history length / 10
    out, _ = _run_server_suggest(monkeypatch, set_position=0.8)
    assert out["set_position"] == 0.8


def test_server_passes_tempo_target(monkeypatch):
    _, kw = _run_server_suggest(monkeypatch, tempo_target=130.0, tempo_note="step 2/5")
    assert kw["tempo_target"] == 130.0 and kw["tempo_note"] == "step 2/5"
    assert kw["occasion"] == ""  # the hint never becomes an occasion
