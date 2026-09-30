"""Claude Code as an optional backend for two offline text jobs: the AI review of
learned moves (set_ai.review) and genre / era labels for library songs.

Default is OFF. With AI_REVIEW_BACKEND unset (or "local") nothing leaves the
machine: the jobs use the local model, exactly as before. The live ear and song
picks never use this module.

Config (env, or .env loaded by the server / CLI):
  AI_REVIEW_BACKEND   local (default) | claudecode
  CLAUDECODE_MODEL    model alias or full name passed to `claude --model` (default sonnet)
  CLAUDECODE_CONCURRENCY  parallel `claude` calls allowed (default 1, max 2)

How: the owner's installed `claude` CLI in print mode, logged in with his own
Claude subscription (claude.ai login). One subprocess per batch:
  claude -p --output-format json --json-schema <schema> --model <m>
         --system-prompt <sys> --tools "" --strict-mcp-config --no-session-persistence
The prompt goes on stdin. No tools, no MCP servers, no session saved. The answer is
the `structured_output` field of the result JSON, validated against our schema and
retried once when invalid. ANTHROPIC_API_KEY is removed from the child's env so the
call runs on the subscription login, never on an API key by accident.

What leaves the machine when enabled: review sends observation facts (move kind,
set times, stem levels in dB, tempo gap, key score) plus song names and short
lyric lines; labels send song names only.

Policy (code.claude.com/docs/en/legal-and-compliance): "OAuth authentication is
intended exclusively for purchasers of Claude Free, Pro, Max, Team, and Enterprise
subscription plans and is designed to support ordinary use of Claude Code and other
native Anthropic applications." and "Anthropic does not permit third-party
developers to offer Claude.ai login into their own applications, or to route
requests through Free, Pro, or Max plan credentials on behalf of their users."
So: personal use by the owner through his own installed Claude Code only. If this
app is ever given to other people, each must run their own `claude` login (or this
backend stays off); never bundle, share or proxy credentials.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from typing import Callable, Optional

BACKEND_ENV = "AI_REVIEW_BACKEND"
MODEL_ENV = "CLAUDECODE_MODEL"
CONCURRENCY_ENV = "CLAUDECODE_CONCURRENCY"
BACKENDS = ("local", "claudecode")
DEFAULT_MODEL = "sonnet"
TIMEOUT_S = 300.0
RATE_BACKOFF_S = (30.0, 90.0)      # waits before the 2nd and 3rd try on a limit error
_RATE_WORDS = ("rate limit", "rate_limit", "usage limit", "overloaded", "429", "529", "limit reached")
_AUTH_WORDS = ("login", "log in", "logged in", "authenticat", "oauth", "401", "credential")


class ClaudeCodeError(RuntimeError):
    """The claudecode backend was asked for but cannot answer (never a silent fallback)."""


def backend(value: Optional[str] = None) -> str:
    b = (value if value is not None else os.environ.get(BACKEND_ENV, "")).strip().lower() or "local"
    if b not in BACKENDS:
        raise ValueError(f"{BACKEND_ENV}={b!r}: expected one of {', '.join(BACKENDS)}")
    return b


def model() -> str:
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def _concurrency() -> int:
    try:
        return max(1, min(2, int(os.environ.get(CONCURRENCY_ENV, "1"))))
    except ValueError:
        return 1


_slots = threading.BoundedSemaphore(_concurrency())


def find_cli() -> str:
    exe = shutil.which("claude")
    if not exe:
        raise ClaudeCodeError(f"{BACKEND_ENV}=claudecode but the `claude` CLI is not on PATH "
                              "(install Claude Code and log in, or unset the backend)")
    return exe


def command(system: str, schema: dict, model_name: str, exe: str = "claude") -> list:
    return [exe, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
            "--model", model_name, "--system-prompt", system, "--tools", "",
            "--strict-mcp-config", "--no-session-persistence"]


def _env() -> dict:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)          # run on the subscription login, not an API key
    return env


def valid(value, schema: dict) -> bool:
    """The subset of JSON Schema our schemas use: type, properties, required, items, enum."""
    t = schema.get("type")
    if "enum" in schema and value not in schema["enum"]:
        return False
    if t == "object":
        if not isinstance(value, dict) or any(k not in value for k in schema.get("required", [])):
            return False
        return all(valid(value[k], s) for k, s in schema.get("properties", {}).items() if k in value)
    if t == "array":
        return isinstance(value, list) and all(valid(v, schema.get("items", {})) for v in value)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    return True


def _run_once(cmd: list, user: str, timeout: float, runner) -> dict:
    try:
        p = runner(cmd, input=user, capture_output=True, text=True, timeout=timeout, env=_env())
    except subprocess.TimeoutExpired:
        raise ClaudeCodeError(f"`claude -p` timed out after {timeout:.0f} s")
    except FileNotFoundError:
        raise ClaudeCodeError("the `claude` CLI could not be started (not installed?)")
    out = (p.stdout or "").strip()
    try:
        res = json.loads(out) if out else {}
    except ValueError:
        res = {}
    if p.returncode != 0 or not isinstance(res, dict) or res.get("is_error") or not res:
        msg = str((res or {}).get("result") or p.stderr or out or f"exit {p.returncode}")[:300]
        low = msg.lower()
        if any(w in low for w in _RATE_WORDS):
            raise _RateLimited(msg)
        if any(w in low for w in _AUTH_WORDS):
            raise ClaudeCodeError(f"`claude` is not logged in (run `claude`, then /login): {msg}")
        raise ClaudeCodeError(f"`claude -p` failed: {msg}")
    return res


class _RateLimited(ClaudeCodeError):
    pass


def ask(system: str, user: str, schema: dict, model_name: Optional[str] = None,
        timeout: float = TIMEOUT_S, runner: Callable = subprocess.run,
        sleep: Callable[[float], None] = time.sleep, exe: Optional[str] = None) -> Optional[dict]:
    """One structured answer: the dict matching `schema`, or None when the model gave
    invalid JSON twice. Raises ClaudeCodeError when the CLI is missing, not logged in,
    still rate limited after the backoff, or fails."""
    cmd = command(system, schema, model_name or model(), exe or find_cli())
    for attempt in range(2):                     # one retry on an invalid answer
        for wait in (*RATE_BACKOFF_S, None):
            try:
                with _slots:
                    res = _run_once(cmd, user, timeout, runner)
                break
            except _RateLimited as exc:
                if wait is None:
                    raise ClaudeCodeError(f"Claude usage limit still hit after backoff: {exc}")
                sleep(wait)
        data = res.get("structured_output")
        if data is None and isinstance(res.get("result"), str):
            try:
                data = json.loads(res["result"])
            except ValueError:
                data = None
        if valid(data, schema):
            return data
    return None


def make_chat(schema: dict, **kw) -> Callable[[str, str], str]:
    """set_ai's Chat shape, (system, user) -> JSON text; "" when no valid answer."""
    def chat(system: str, user: str) -> str:
        data = ask(system, user, schema, **kw)
        return json.dumps(data) if data is not None else ""
    return chat
