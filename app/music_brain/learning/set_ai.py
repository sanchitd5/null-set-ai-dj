"""AI assistance for set learning and hook drops (local LLM, same one the autopilot uses).

The algorithm measures (stems, positions, levels, which words); the model
judges what a measurement means, the way the USB002 study was done by hand:

* emotional_lines(): from a song's lyrics, the lines that carry the song's
  emotion (not only the most repeated one): the line a room would scream with
  the beat gone. hook_drop.plan() ranks those first.
* review(): each observation the learner extracted is shown to the model with
  its evidence (kind, time, tracks, words, stem levels); it keeps the ones that
  read like a deliberate DJ move and writes the rule in one sentence. Rejected
  observations are dropped before they reach the store.

Everything degrades: no model reachable -> the algorithm's answer stands and the
result says `ai: "skipped (...)"`. Model answers are cached per input on disk.

review() can optionally run on Claude Code (AI_REVIEW_BACKEND=claudecode, see
app/music_brain/llm/claudecode.py for what leaves the machine). Default is the
local model. Cached answers are keyed by backend + model so the two never mix.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from app.music_brain.config import CACHE_DIR

AI_CACHE_DIR = CACHE_DIR / "set_ai"
# the text model (start.sh) first, then the live ear's omni model (start.sh --single-omni serves both jobs)
MODEL_URLS = [f"http://127.0.0.1:{os.environ.get('MLX_PORT', '8081')}/v1",
              f"http://127.0.0.1:{os.environ.get('OMNI_PORT', '8901')}/v1"]
REVIEW_BATCH = 12             # observations per model call
CLAUDECODE_REVIEW_BATCH = 40  # fewer, bigger calls spend the subscription limits better
TIMEOUT_S = 120.0

Chat = Callable[[str, str], str]      # (system, user) -> raw text (JSON expected)


def _default_chat() -> Optional[Chat]:
    """The autopilot's client (priority gate, Qwen3 /no_think, JSON repair), pointed
    at the local MLX server when nothing else is configured. None if unreachable."""
    import urllib.request

    base = os.environ.get("OLLAMA_BASE_URL")
    if not base:
        found = find_model()
        if found is None:
            return None
        os.environ["OLLAMA_BASE_URL"], model = found
        os.environ.setdefault("AUTOPILOT_MODEL", model)
    from app.ui.services import autopilot_service as ap
    from app.ui.services import llm_gate

    return lambda system, user: ap.chat_raw(system, user, temperature=0.2, timeout=TIMEOUT_S,
                                            max_tokens=1500, priority=llm_gate.LOOKAHEAD, kind="set_ai")


def find_model(urls: Optional[List[str]] = None, opener=None) -> Optional[tuple]:
    """(base_url, model id) of the first local server answering, preferring a model it
    reports as already loaded (asking an unloaded one makes the server load 16 GB)."""
    import urllib.request

    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in urls or MODEL_URLS:
        try:
            with opener.open(f"{url}/models", timeout=2) as r:
                models = json.loads(r.read()).get("data") or []
        except Exception:
            continue
        if models:
            best = next((m for m in models if m.get("loaded")), models[0])
            return url, best["id"]
    return None


def _ask(system: str, user: str, chat: Optional[Chat], cache_key: str, call: bool = True,
         tag: str = "") -> Optional[dict]:
    """JSON answer, cached. None when no model or an unusable answer. call=False: cache only.
    tag: backend + model of a non-local chat, so its answers never share a cache entry
    with the local model's (empty for local keeps the existing cache valid)."""
    key = (tag + "\n" if tag else "") + system + user
    path = AI_CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()[:20]}.json"
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(cached, dict):           # a truncated / hand-edited file is a miss
            return cached
    except (OSError, ValueError):
        pass
    if not call:
        return None
    chat = chat or _default_chat()
    if chat is None:
        return None
    from app.music_brain.llm.claudecode import ClaudeCodeError
    from app.ui.services.autopilot_service import _extract_json

    try:
        data = _extract_json(chat(system, user))
    except ClaudeCodeError:
        raise                                   # not logged in / limit / missing CLI: say so, loudly
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data | {"_key": cache_key}, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


# ------------------------------------------------------------ emotional lines
HOOK_SYSTEM = """You are a club DJ reading a song's lyrics to plan one move: pull the
drums and bass out under a single sung line so the crowd hears the voice alone,
then slam the beat back in on the next phrase. Pick the lines where that hits
hardest: the emotional peak, the line people shout back, a question or a plea,
a title line. Prefer short lines. Quote lines EXACTLY as written.
If the vocal is sampled from another recording, read the words in that recording's
meaning (a line can mean the opposite of how it sounds out of context).
Answer JSON: {"lines": [{"text": "<exact line>", "intensity": 1-10, "why": "<6-12 words>"}]}
with at most 3 lines, best first."""


def emotional_lines(title: str, lines: Sequence[dict], chat: Optional[Chat] = None, call: bool = True) -> List[dict]:
    """[{text, intensity, why}] chosen by the model, only lines that exist in the lyrics.
    call=False answers from the cache only (request paths that must not wait on a model)."""
    if not lines:
        return []
    uniq = list(dict.fromkeys(l["text"] for l in lines))[:120]
    from app.music_brain.analysis.lyrics import sample_of

    smp = sample_of(title)
    src = f"Vocal sampled from: {smp['title']}" + (f" ({smp['note']})" if smp.get("note") else "") + "\n" if smp else ""
    data = _ask(HOOK_SYSTEM, f"Song: {title}\n{src}Lyrics:\n" + "\n".join(uniq), chat, f"hooks:{title}", call)
    real = {t.strip().lower(): t for t in uniq}
    out = []
    picked = (data or {}).get("lines")
    for x in picked if isinstance(picked, list) else []:
        t = str(x.get("text", "")).strip().lower() if isinstance(x, dict) else ""
        if t in real:                          # the model may paraphrase: only exact lines count
            try:
                inten = max(1, min(10, int(x.get("intensity", 5))))
            except (TypeError, ValueError, OverflowError):   # json allows Infinity / NaN
                inten = 5
            out.append({"text": real[t], "intensity": inten, "why": str(x.get("why", ""))[:120]})
    return out[:3]


# ------------------------------------------------------------------- review
REVIEW_SYSTEM = """You review moves a program detected in a recorded DJ set, from 4-stem
separation (vocals/drums/bass/other, levels in dB, -45 = silent) matched against
the original songs and their lyrics. Detection can be fooled: a repeated chorus,
a vocal bleeding into another stem, a similar-sounding section. For each item
decide whether the evidence reads like a deliberate DJ move of that kind.
Kinds: bass_swap, stem_intro, acapella_over, hard_cut, loop_extend,
vocal_resequence (lines played out of order into a new lyric), vocal_loop
(one line repeated), vocal_chop (short fragments re-triggered),
acapella_drop (beat out under a sung line, then the drop).
vocal_resequence, vocal_loop, vocal_chop and acapella_drop are moves on ONE
song: "b" is null by design, never a reason to reject. "source" lists the
song positions (seconds) the set played, in set order.
Reject only when the evidence itself looks wrong: no words where words are
claimed, a jump between two copies of the same chorus, stem levels that
contradict the move, a duplicate of another item.
Never reject a move for its key score or tempo gap: a real DJ may blend clashing
keys or big tempo gaps on purpose, and the player applies its own key and tempo
limits when it plays. A large tempo gap is a reason to reject only when the
levels or sources also show the detection is wrong. A bass swap (the bass owner
flipping from one record to the other while the drums keep running) is a core
move: keep it whenever the bass levels support it.
For kept items write the rule a DJ could reuse, ONE concrete sentence built
from this item's evidence: name the stems and their order, the bars or seconds,
which vocal line (by its position, e.g. "the second hook line"; never quote the
lyrics), and what the two records were doing. Not a definition of the move
("fade in stems one by one" says nothing). Answer JSON:
{"items": [{"id": <int>, "keep": true|false, "rule": "<sentence or empty>", "why": "<short>"}]}"""


def _brief(i: int, o, lyrics: bool = True) -> dict:
    d = o.detail
    words = d.get("words") if lyrics else None
    if isinstance(words, list):
        words = [w for w in words if w][:6]
    src = d.get("source_lines") or d.get("fragments") or ([d["src_span"]] if d.get("src_span") else None)
    return {"id": i, "kind": o.kind, "at_s": round(o.at, 1), "a": o.track_a, "b": o.track_b or None,
            "tempo_gap": o.tempo_gap, "key_score": o.key_score, "words": words or None,
            "source": [[round(float(x), 1) for x in s][:2] for s in src][:8] if src else None,
            **{k: d[k] for k in ("held_s", "hook", "order", "lead_s", "vocal_from", "repeats", "jumps", "levels_db", "drop_at") if k in d}}


REVIEW_SCHEMA = {"type": "object", "required": ["items"], "properties": {"items": {
    "type": "array", "items": {"type": "object", "required": ["id", "keep", "rule", "why"], "properties": {
        "id": {"type": "integer"}, "keep": {"type": "boolean"},
        "rule": {"type": "string"}, "why": {"type": "string"}}}}}}


def backend_chat(schema: dict, backend: Optional[str] = None):
    """(chat, cache tag, batch size) for the configured backend. local -> (None, "", 0):
    the caller keeps its own default. claudecode raises ClaudeCodeError when the CLI is
    missing, never falls back to sending nothing silently."""
    from app.music_brain.llm import claudecode as cc

    if cc.backend(backend) == "local":
        return None, "", 0
    cc.find_cli()
    return cc.make_chat(schema), f"claudecode:{cc.model()}", CLAUDECODE_REVIEW_BATCH


def review_requests(observations: list, batch: int = REVIEW_BATCH, lyrics: bool = True) -> List[str]:
    """The user prompts review() would send, one per model call (for --dry-run).
    lyrics=False drops the sung words (the retry after a content-filter block)."""
    return [json.dumps({"items": [_brief(i, observations[i], lyrics) for i in range(s, min(s + batch, len(observations)))]},
                       ensure_ascii=False) for s in range(0, len(observations), batch)]


def _content_blocked(exc: Exception) -> bool:
    return "content filtering" in str(exc).lower()


def review(observations: list, chat: Optional[Chat] = None, log: Callable[[str], None] = lambda m: None,
           backend: Optional[str] = None) -> dict:
    """-> {"kept": [...], "rejected": [...], "ai": "reviewed" | "skipped (...)"}.
    Kept observations gain detail.ai_rule; with no model everything is kept.
    backend: "local" | "claudecode" (None: AI_REVIEW_BACKEND, default local); ignored
    when a chat is passed."""
    if not observations:
        return {"kept": [], "rejected": [], "ai": "nothing to review"}
    tag, batch = "", REVIEW_BATCH
    if chat is None:
        chat, tag, n = backend_chat(REVIEW_SCHEMA, backend)
        batch = n or REVIEW_BATCH
    who = "Claude Code" if tag else "local model"
    from app.music_brain.llm.claudecode import ClaudeCodeError

    verdict: Dict[int, dict] = {}
    plain = review_requests(observations, batch, lyrics=False)
    for k, (s, user) in enumerate(zip(range(0, len(observations), batch), review_requests(observations, batch))):
        try:
            data = _ask(REVIEW_SYSTEM, user, chat, "review", tag=tag)
        except ClaudeCodeError as exc:
            # Claude's output filter can block a batch that carries sung lines (it may quote them
            # back): retry once without the words; still blocked -> the batch stays unreviewed and
            # its measurements stand, the other batches go on
            if not _content_blocked(exc):
                raise
            log(f"batch {k + 1} blocked by content filtering: retrying without lyric lines")
            try:
                data = _ask(REVIEW_SYSTEM, plain[k], chat, "review", tag=tag)
            except ClaudeCodeError as exc2:
                if not _content_blocked(exc2):
                    raise
                log(f"batch {k + 1} still blocked: left unreviewed (measurements stand)")
                continue
        if data is None:
            if not verdict:
                return {"kept": list(observations), "rejected": [], "ai": f"skipped (no {who} answering)"}
            continue
        items_ans = data.get("items")
        for x in items_ans if isinstance(items_ans, list) else []:
            if isinstance(x, dict) and isinstance(x.get("id"), int) and s <= x["id"] < s + batch:
                verdict[x["id"]] = x
        log(f"ai reviewed {min(s + batch, len(observations))}/{len(observations)}")
    kept, rejected = [], []
    for i, o in enumerate(observations):
        v = verdict.get(i)
        if v is None or v.get("keep", True) is not False:    # unanswered: the measurement stands
            if v and v.get("rule"):
                o.detail["ai_rule"] = str(v["rule"])[:300]
            kept.append(o)
        else:
            o.detail["ai_reject"] = str(v.get("why", ""))[:200]
            rejected.append(o)
    return {"kept": kept, "rejected": rejected, "ai": "reviewed"}
