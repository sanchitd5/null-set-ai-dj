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
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from app.music_brain.config import CACHE_DIR

AI_CACHE_DIR = CACHE_DIR / "set_ai"
MLX_URL = f"http://127.0.0.1:{os.environ.get('MLX_PORT', '8081')}/v1"
REVIEW_BATCH = 12             # observations per model call
TIMEOUT_S = 120.0

Chat = Callable[[str, str], str]      # (system, user) -> raw text (JSON expected)


def _default_chat() -> Optional[Chat]:
    """The autopilot's client (priority gate, Qwen3 /no_think, JSON repair), pointed
    at the local MLX server when nothing else is configured. None if unreachable."""
    import urllib.request

    base = os.environ.get("OLLAMA_BASE_URL")
    if not base:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"{MLX_URL}/models", timeout=2) as r:
                models = json.loads(r.read()).get("data") or []
        except Exception:
            return None
        os.environ["OLLAMA_BASE_URL"] = MLX_URL
        if models and not os.environ.get("AUTOPILOT_MODEL"):
            os.environ["AUTOPILOT_MODEL"] = models[0]["id"]
    from app.ui import autopilot_service as ap
    from app.ui import llm_gate

    return lambda system, user: ap.chat_raw(system, user, temperature=0.2, timeout=TIMEOUT_S,
                                            max_tokens=1500, priority=llm_gate.LOOKAHEAD)


def _ask(system: str, user: str, chat: Optional[Chat], cache_key: str, call: bool = True) -> Optional[dict]:
    """JSON answer, cached. None when no model or an unusable answer. call=False: cache only."""
    path = AI_CACHE_DIR / f"{hashlib.sha256((system + user).encode()).hexdigest()[:20]}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if not call:
        return None
    chat = chat or _default_chat()
    if chat is None:
        return None
    from app.ui.autopilot_service import _extract_json

    try:
        data = _extract_json(chat(system, user))
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
    from app.music_brain.lyrics import sample_of

    smp = sample_of(title)
    src = f"Vocal sampled from: {smp['title']}" + (f" ({smp['note']})" if smp.get("note") else "") + "\n" if smp else ""
    data = _ask(HOOK_SYSTEM, f"Song: {title}\n{src}Lyrics:\n" + "\n".join(uniq), chat, f"hooks:{title}", call)
    real = {t.strip().lower(): t for t in uniq}
    out = []
    for x in (data or {}).get("lines") or []:
        t = str(x.get("text", "")).strip().lower() if isinstance(x, dict) else ""
        if t in real:                          # the model may paraphrase: only exact lines count
            try:
                inten = max(1, min(10, int(x.get("intensity", 5))))
            except (TypeError, ValueError):
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
For kept items write the rule a DJ could reuse, one sentence, concrete
(what, when, why it works). Answer JSON:
{"items": [{"id": <int>, "keep": true|false, "rule": "<sentence or empty>", "why": "<short>"}]}"""


def _brief(i: int, o) -> dict:
    d = o.detail
    words = d.get("words")
    if isinstance(words, list):
        words = [w for w in words if w][:6]
    return {"id": i, "kind": o.kind, "at_s": round(o.at, 1), "a": o.track_a, "b": o.track_b or None,
            "tempo_gap": o.tempo_gap, "key_score": o.key_score, "words": words or None,
            **{k: d[k] for k in ("held_s", "hook", "order", "lead_s", "vocal_from", "repeats", "jumps", "levels_db") if k in d}}


def review(observations: list, chat: Optional[Chat] = None, log: Callable[[str], None] = lambda m: None) -> dict:
    """-> {"kept": [...], "rejected": [...], "ai": "reviewed" | "skipped (...)"}.
    Kept observations gain detail.ai_rule; with no model everything is kept."""
    if not observations:
        return {"kept": [], "rejected": [], "ai": "nothing to review"}
    verdict: Dict[int, dict] = {}
    for s in range(0, len(observations), REVIEW_BATCH):
        items = [_brief(i, observations[i]) for i in range(s, min(s + REVIEW_BATCH, len(observations)))]
        data = _ask(REVIEW_SYSTEM, json.dumps({"items": items}, ensure_ascii=False), chat, "review")
        if data is None:
            if not verdict:
                return {"kept": list(observations), "rejected": [], "ai": "skipped (no local model answering)"}
            continue
        for x in data.get("items") or []:
            if isinstance(x, dict) and isinstance(x.get("id"), int) and s <= x["id"] < s + REVIEW_BATCH:
                verdict[x["id"]] = x
        log(f"ai reviewed {min(s + REVIEW_BATCH, len(observations))}/{len(observations)}")
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
