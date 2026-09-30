"""The optional Claude Code backend (AI_REVIEW_BACKEND=claudecode), with the `claude`
subprocess stubbed: no real CLI call, no network."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from app.music_brain.analysis import genre_labels as gl
from app.music_brain.learning import set_ai
from app.music_brain.learning import set_learner as sl
from app.music_brain.llm import claudecode as cc


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    for k in (cc.BACKEND_ENV, cc.MODEL_ENV, cc.CONCURRENCY_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(set_ai, "AI_CACHE_DIR", tmp_path / "ai")


def _proc(out="", err="", code=0):
    return SimpleNamespace(stdout=out, stderr=err, returncode=code)


def _result(structured=None, **kw):
    return json.dumps({"type": "result", "is_error": False, "result": "", "structured_output": structured} | kw)


SCHEMA = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}


# ------------------------------------------------------------------ backend selection
def test_backend_default_local_env_and_bad_value(monkeypatch):
    assert cc.backend() == "local"
    monkeypatch.setenv(cc.BACKEND_ENV, " ClaudeCode ")
    assert cc.backend() == "claudecode"
    assert cc.model() == "sonnet"
    with pytest.raises(ValueError):
        cc.backend("openai")


def test_missing_cli_is_a_clear_error_not_a_silent_fallback(monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)
    with pytest.raises(cc.ClaudeCodeError, match="not on PATH"):
        set_ai.backend_chat(set_ai.REVIEW_SCHEMA, "claudecode")
    # local never looks for the CLI
    assert set_ai.backend_chat(set_ai.REVIEW_SCHEMA, "local") == (None, "", 0)


# ------------------------------------------------------------------ the subprocess call
def test_command_has_no_tools_schema_and_model_and_no_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    seen = {}

    def runner(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return _proc(_result({"x": 3}))
    assert cc.ask("SYS", "USER", SCHEMA, model_name="sonnet", runner=runner, exe="claude") == {"x": 3}
    cmd = seen["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == SCHEMA
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--no-session-persistence" in cmd and "--strict-mcp-config" in cmd
    assert seen["input"] == "USER"
    assert "ANTHROPIC_API_KEY" not in seen["env"]


def test_invalid_answer_retried_once_then_none():
    outs = iter([_result({"x": "nope"}), _result({"x": 1})])
    assert cc.ask("s", "u", SCHEMA, runner=lambda c, **k: _proc(next(outs)), exe="claude") == {"x": 1}
    calls = []

    def bad(c, **k):
        calls.append(1)
        return _proc(_result({"y": 1}))
    assert cc.ask("s", "u", SCHEMA, runner=bad, exe="claude") is None and len(calls) == 2


def test_result_text_json_used_when_no_structured_output():
    out = _result(None, result='{"x": 5}')
    assert cc.ask("s", "u", SCHEMA, runner=lambda c, **k: _proc(out), exe="claude") == {"x": 5}


def test_not_logged_in_raises():
    out = json.dumps({"type": "result", "is_error": True, "result": "Not logged in. Please run /login"})
    with pytest.raises(cc.ClaudeCodeError, match="not logged in"):
        cc.ask("s", "u", SCHEMA, runner=lambda c, **k: _proc(out, code=1), exe="claude")


def test_rate_limit_backs_off_then_succeeds():
    outs = iter([_proc(err="API Error: 429 rate_limit_error", code=1), _proc(_result({"x": 2}))])
    waits = []
    assert cc.ask("s", "u", SCHEMA, runner=lambda c, **k: next(outs), sleep=waits.append,
                  exe="claude") == {"x": 2}
    assert waits == [cc.RATE_BACKOFF_S[0]]


def test_rate_limit_still_hit_after_backoff_raises():
    waits = []
    with pytest.raises(cc.ClaudeCodeError, match="limit"):
        cc.ask("s", "u", SCHEMA, runner=lambda c, **k: _proc(err="usage limit reached", code=1),
               sleep=waits.append, exe="claude")
    assert waits == list(cc.RATE_BACKOFF_S)


def test_timeout_raises():
    def slow(c, **k):
        raise subprocess.TimeoutExpired(c, 1)
    with pytest.raises(cc.ClaudeCodeError, match="timed out"):
        cc.ask("s", "u", SCHEMA, runner=slow, exe="claude")


# ------------------------------------------------------------------ set_ai.review
def _obs(n=3):
    return [sl.Observation("hard_cut", "setA", float(10 * i), "A", "B") for i in range(n)]


def _review_answer(user):
    items = json.loads(user)["items"]
    return {"items": [{"id": it["id"], "keep": it["id"] != 1, "rule": f"rule {it['id']}", "why": "bad"}
                      for it in items]}


def _stub_claudecode(monkeypatch, calls):
    monkeypatch.setattr(cc, "find_cli", lambda: "claude")

    def make_chat(schema, **kw):
        def chat(system, user):
            calls.append(user)
            return json.dumps(_review_answer(user))
        return chat
    monkeypatch.setattr(cc, "make_chat", make_chat)


def test_review_claudecode_parses_keep_rule_why(monkeypatch):
    calls = []
    _stub_claudecode(monkeypatch, calls)
    res = set_ai.review(_obs(), backend="claudecode")
    assert res["ai"] == "reviewed" and len(calls) == 1          # one batch of 40
    assert [o.at for o in res["kept"]] == [0.0, 20.0]
    assert res["kept"][0].detail["ai_rule"] == "rule 0"
    assert res["rejected"][0].detail["ai_reject"] == "bad"


def test_cache_keys_differ_per_backend(monkeypatch, tmp_path):
    calls = []
    _stub_claudecode(monkeypatch, calls)
    local_calls = []

    def local_chat(system, user):
        local_calls.append(user)
        return json.dumps(_review_answer(user))
    monkeypatch.setattr(set_ai, "_default_chat", lambda: local_chat)
    monkeypatch.setattr(set_ai, "REVIEW_BATCH", set_ai.CLAUDECODE_REVIEW_BATCH)  # same prompt both ways
    set_ai.review(_obs(), backend="local")
    set_ai.review(_obs(), backend="claudecode")
    assert len(local_calls) == 1 and len(calls) == 1              # no cross-backend cache hit
    assert len(list((tmp_path / "ai").glob("*.json"))) == 2
    set_ai.review(_obs(), backend="claudecode")
    assert len(calls) == 1                                        # own cache hit


def test_claudecode_error_propagates_from_review(monkeypatch):
    monkeypatch.setattr(cc, "find_cli", lambda: "claude")

    def make_chat(schema, **kw):
        def chat(system, user):
            raise cc.ClaudeCodeError("`claude` is not logged in")
        return chat
    monkeypatch.setattr(cc, "make_chat", make_chat)
    with pytest.raises(cc.ClaudeCodeError):
        set_ai.review(_obs(), backend="claudecode")


# ------------------------------------------------------------------ learned-review
def _store(tmp_path):
    p = tmp_path / "learned.json"
    sl.merge(_obs(), p, set_ids=("setA",))
    return p


def test_learned_review_dry_run_makes_no_calls(monkeypatch, tmp_path):
    calls = []
    _stub_claudecode(monkeypatch, calls)
    p = _store(tmp_path)
    out = sl.review_learned(None, backend="claudecode", dry_run=True, path=p, sidecar_dir=tmp_path / "side")
    assert calls == [] and out["dry_run"] is True
    assert out["sets"] == [{"set_id": "setA", "observations": 3, "calls": 1,
                            "prompt_chars": out["sets"][0]["prompt_chars"]}]
    assert out["sets"][0]["prompt_chars"] > len(set_ai.REVIEW_SYSTEM)


def test_learned_review_merges_kept_and_writes_sidecar(monkeypatch, tmp_path):
    calls = []
    _stub_claudecode(monkeypatch, calls)
    p = _store(tmp_path)
    side = tmp_path / "side"
    out = sl.review_learned("setA", backend="claudecode", path=p, sidecar_dir=side)
    row = out["sets"][0]
    assert row["kept"] == 2 and row["rejected"] == 1 and row["merged"] is True
    stored = [o for e in sl.load_learned(p).values() for o in e["observations"]]
    assert sorted(o["at"] for o in stored) == [0.0, 20.0]
    rej = json.loads((side / "setA.json").read_text(encoding="utf-8"))
    assert rej["backend"] == "claudecode" and rej["rejected"][0]["detail"]["ai_reject"] == "bad"


def test_learned_review_unknown_set(tmp_path):
    with pytest.raises(ValueError):
        sl.review_learned("nope", backend="local", path=_store(tmp_path), dry_run=True)


# ------------------------------------------------------------------ genre / era labels
def _library(tmp_path, names, aliases=None):
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "_names.json").write_text(json.dumps(names), encoding="utf-8")
    if aliases:
        (tmp_path / "track_aliases.json").write_text(json.dumps(aliases), encoding="utf-8")


def test_library_titles_resolves_aliases(tmp_path):
    _library(tmp_path, {"a": "Artist - Song", "b": "Artist - Song (dup)", "c": "Other - Tune"}, {"b": "a"})
    assert sorted(gl.library_titles(tmp_path).values()) == ["Artist - Song", "Other - Tune"]


def test_label_library_local_wins_and_parses(tmp_path):
    _library(tmp_path, {"a": "Artist - Song", "c": "Other - Tune"})
    ka, kc = gl.name_key("Artist - Song"), gl.name_key("Other - Tune")
    gl.save({ka: "house"}, {}, gl.path(tmp_path))
    sent = []

    def chat(system, user):
        sent.append(json.loads(user))
        return json.dumps({"labels": [
            {"title_key": ka, "genre": "Techno", "era": "late 90s"},
            {"title_key": kc, "genre": "Punjabi Pop", "era": "2021"},
            {"title_key": "made up", "genre": "x", "era": "1980s"}]})
    out = gl.label_library(cache_dir=tmp_path, chat=chat)
    genres, eras = gl.load(gl.path(tmp_path))
    assert genres == {ka: "house", kc: "punjabi pop"}           # local label won
    assert eras == {ka: "1990s", kc: "2020s"}
    assert out["added"] == 3 and out["answered"] == 2 and out["overwritten"] == 0
    assert {s["name"] for s in sent[0]["songs"]} == {"Artist - Song", "Other - Tune"}   # names only
    gl.label_library(cache_dir=tmp_path, chat=chat)
    assert len(sent) == 1                                          # everything labelled now
    gl.label_library(missing_only=False, overwrite=True, cache_dir=tmp_path, chat=chat)
    assert gl.load(gl.path(tmp_path))[0][ka] == "techno"


def test_label_dry_run_no_calls(tmp_path, monkeypatch):
    _library(tmp_path, {str(i): f"A{i} - S{i}" for i in range(45)})
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)   # dry run needs no CLI
    out = gl.label_library(backend="claudecode", dry_run=True, cache_dir=tmp_path)
    assert out["to_label"] == 45 and out["calls"] == 2 and out["prompt_chars"] > 0
    assert not gl.path(tmp_path).exists()
