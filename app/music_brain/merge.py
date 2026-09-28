"""Song merge: a transition where the two records share the stems.

Each role (drums, bass, vocals, other) comes from exactly ONE deck for the
merge section, so there is always one sub owner and one singer
([[EQ & Frequency Management]], [[Stems Transition]]). Example: A's drums +
A's bass + B's vocals + B's synths, then B takes everything on the line.

    combos()             every split (14: all-A and all-B are not a merge)
    rank(ctx)            algorithmic pick: every chosen stem must really play in
                         its window, tonal layers from different decks need keys
                         that agree (drums are keyless, a rap vocal ignores key),
                         kick + bass from one deck lock the groove
    render_clip(...)     the merge as a short offline clip from the cached stems,
                         B time-stretched (key-locked) to A's tempo
    ear_rate(...)        the "silent ear": the local omni model hears each clip
                         (nothing plays) and rates it; advisory, cached

The console (static/stem-moves.js mergeTransitionPlan) plays the chosen combo.
The same scoring rules live there as mergeRank() so a merge can be planned with
no server round trip; this module adds the listening.
"""
from __future__ import annotations

import base64
import hashlib
import io
import itertools
import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

from app.music_brain.config import CACHE_DIR

ROLES = ("drums", "bass", "vocals", "other")
TONAL = ("bass", "vocals", "other")
MIN_RMS = 0.01                 # ~ -40 dBFS: below this the stem is not really playing
KEY_OK = 0.8                   # camelot score two tonal layers need to share the air
CLIP_BARS = 8
AUDITION_DIR = CACHE_DIR / "merge"


def combos() -> List[Dict[str, str]]:
    """Every {role: 'a'|'b'} except all-A and all-B."""
    out = []
    for srcs in itertools.product("ab", repeat=len(ROLES)):
        if len(set(srcs)) == 2:
            out.append(dict(zip(ROLES, srcs)))
    return out


def label(c: Dict[str, str]) -> str:
    """'A drums + A bass + B vocals + B other'"""
    return " + ".join(f"{c[r].upper()} {r}" for r in ROLES)


def rank(e_a: Dict[str, float], e_b: Dict[str, float], key_score: Optional[float],
         b_rap: bool = False, a_rap: bool = False) -> List[dict]:
    """Combos that can play, best first: [{combo, score, reasons}].
    e_a / e_b: mean RMS per stem over each deck's merge window."""
    out = []
    for c in combos():
        why, ok = [], True
        for r in ROLES:
            e = (e_a if c[r] == "a" else e_b).get(r, 0.0)
            if e < MIN_RMS:
                ok = False
                why.append(f"{c[r].upper()}'s {r} is silent there")
        # tonal layers that come from different decks must agree in key
        tonal_src = {c[r] for r in TONAL if not (r == "vocals" and ((c[r] == "b" and b_rap) or (c[r] == "a" and a_rap)))}
        if len(tonal_src) == 2 and key_score is not None and key_score < KEY_OK:
            ok = False
            why.append(f"keys clash ({key_score:.2f}) between tonal layers from both decks")
        if not ok:
            continue
        score = 50.0
        if c["drums"] == c["bass"]:
            score += 20; why.append("kick and bass from one record: the groove stays locked")
        if (e_a if c["vocals"] == "a" else e_b).get("vocals", 0) >= MIN_RMS:
            score += 10; why.append(f"{c['vocals'].upper()}'s voice carries the merge")
        if c["vocals"] == "b" and c["drums"] == "a":
            score += 10; why.append("B's song over A's beat: the crowd hears what's coming")
        if key_score is not None and len(tonal_src) == 2:
            score += 10 * key_score
        # balance: roughly half the energy from each side
        ea = sum((e_a.get(r, 0) ** 2) for r in ROLES if c[r] == "a")
        eb = sum((e_b.get(r, 0) ** 2) for r in ROLES if c[r] == "b")
        if ea + eb > 0:
            score += 10 * (1 - abs(ea - eb) / (ea + eb))
        out.append({"combo": c, "label": label(c), "score": round(score, 1), "reasons": why})
    return sorted(out, key=lambda x: -x["score"])


# ------------------------------------------------------------------- listen
def _slice(path: str, t0: float, dur: float, sr: int) -> np.ndarray:
    import librosa

    y, _ = librosa.load(path, sr=sr, mono=True, offset=max(0.0, t0), duration=dur)
    return y


def render_clip(stems_a: Dict[str, str], stems_b: Dict[str, str], combo: Dict[str, str], a_time: float,
                b_time: float, bpm_a: float, bpm_b: float, bars: int = CLIP_BARS, sr: int = 16000) -> np.ndarray:
    """The merge section, mono at sr: A's chosen stems from a_time, B's chosen stems
    from b_time stretched to A's tempo (Rubber Band key-lock when available,
    else librosa's phase vocoder)."""
    dur_a = bars * 240.0 / bpm_a
    rate = bpm_a / bpm_b                           # B plays this much faster to sit on A's tempo
    parts = []
    for r in ROLES:
        if combo[r] == "a":
            parts.append(_slice(stems_a[r], a_time, dur_a, sr))
        else:
            y = _slice(stems_b[r], b_time, dur_a * rate, sr)
            if abs(rate - 1) > 0.005 and len(y):
                y = _stretch(y, sr, rate)
            parts.append(y)
    n = int(dur_a * sr)
    mix = np.zeros(n, dtype=np.float32)
    for y in parts:
        y = np.asarray(y, dtype=np.float32)[:n]
        mix[:len(y)] += y
    peak = float(np.max(np.abs(mix))) or 1.0
    return mix * min(1.0, 0.95 / peak)


def _stretch(y: np.ndarray, sr: int, rate: float) -> np.ndarray:
    try:
        import pyrubberband as pyrb

        return pyrb.time_stretch(y, sr, rate)
    except Exception:
        import librosa

        return librosa.effects.time_stretch(y, rate=rate)


def wav_bytes(y: np.ndarray, sr: int = 16000) -> bytes:
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, y, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


EAR_SYSTEM = """You are a club DJ's ears. You hear a few seconds of two records merged:
some stems come from one song, the rest from the other, both at one tempo.
Judge only what you hear: does the groove lock (kick and bass together, no
flam), do the chords and the voice agree (no key clash), is the voice clear,
does it feel like one new song with energy a crowd would move to?
Answer JSON only: {"score": 1-10, "why": "<one short sentence>"}"""


def ear_rate(wav: bytes, what: str, ask: Optional[Callable[[str, bytes, str], str]] = None) -> Optional[dict]:
    """{score, why} from the omni model for one clip, or None (no model, bad answer)."""
    if ask is None:
        ask = _ask_omni
    try:
        raw = ask(EAR_SYSTEM, wav, f"Merged layers: {what}. Rate this merge.")
    except Exception:
        return None
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        s = float(d.get("score"))
    except (ValueError, TypeError):
        return None
    if not 1 <= s <= 10:
        return None
    return {"score": round(s, 1), "why": str(d.get("why", ""))[:160]}


def _ask_omni(system: str, wav: bytes, text: str) -> str:
    import openai

    from app.ui.live_ear import config

    c = config()
    b64 = base64.b64encode(wav).decode()
    data = f"data:audio/wav;base64,{b64}" if c["cloud"] else b64
    client = openai.OpenAI(base_url=c["base_url"], api_key=c["api_key"], timeout=max(c["timeout"], 20.0), max_retries=0)
    stream = client.chat.completions.create(
        model=c["model"], temperature=0.2, max_tokens=120, stream=True,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": data, "format": "wav"}},
                                               {"type": "text", "text": text}]}],
        extra_body={"modalities": ["text"]} if c["cloud"] else None,
    )
    return "".join(ch.choices[0].delta.content for ch in stream
                   if ch.choices and ch.choices[0].delta and ch.choices[0].delta.content)


def audition(stems_a: Dict[str, str], stems_b: Dict[str, str], candidates: Sequence[Dict[str, str]],
             a_time: float, b_time: float, bpm_a: float, bpm_b: float, key: str,
             ask: Optional[Callable] = None, cache_dir: Path = AUDITION_DIR) -> List[dict]:
    """The silent ear on each candidate combo: [{combo, label, ear: {score, why}|None}],
    cached per (pair, times, combos) so a booked transition is heard once."""
    cands = [dict(c) for c in candidates][:3]
    h = hashlib.sha256(json.dumps([key, round(a_time, 2), round(b_time, 2), cands], sort_keys=True).encode()).hexdigest()[:20]
    path = Path(cache_dir) / f"{h}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    out = []
    for c in cands:
        wav = wav_bytes(render_clip(stems_a, stems_b, c, a_time, b_time, bpm_a, bpm_b))
        out.append({"combo": c, "label": label(c), "ear": ear_rate(wav, label(c), ask)})
    if any(x["ear"] for x in out):                 # only real answers are cached
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out), encoding="utf-8")
    return out
