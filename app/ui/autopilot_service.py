"""
AI DJ Autopilot: LLM-powered next-track suggestion.

Uses a local Ollama model (default: gemma3:4b) via the OpenAI-compatible API.
Set OLLAMA_BASE_URL (default http://localhost:11434/v1) and
AUTOPILOT_MODEL (default gemma3:4b) in your .env to override.

Compatible with openai v0.x/3.x (ChatCompletion.create) and v1.x/v2.x (OpenAI client).

Ollama quick-start:
    curl -fsSL https://ollama.com/install.sh | sh
    ollama pull gemma3:4b
"""
from __future__ import annotations

import json
import os
import re

# ── Camelot wheel compatibility rules ─────────────────────────────────────────
# Listed explicitly so a small local model doesn't have to derive them.
_CAMELOT_COMPAT = """\
CAMELOT WHEEL — compatible moves from any position X:
  Same position (e.g. 8A→8A): perfect harmonic match, score 1.0
  ±1 hour same letter (e.g. 8A→7A or 8A→9A): smooth, score 0.9
  Same hour opposite letter (e.g. 8A→8B): relative major/minor, score 0.85
  +2 hours same letter (e.g. 8A→10A): energy boost key change, score 0.8
  All other moves: harmonic clash — avoid unless using Echo Out or Breakdown recipe.

12 positions: 1A/1B, 2A/2B, 3A/3B, 4A/4B, 5A/5B, 6A/6B,
              7A/7B, 8A/8B, 9A/9B, 10A/10B, 11A/11B, 12A/12B
A = minor keys, B = major keys. Hour arithmetic wraps: 12+1=1."""

# ── Few-shot examples ─────────────────────────────────────────────────────────
_FEW_SHOT = """\
EXAMPLE 1 — melodic house, building phase (set_position 0.45):
Current: "Marea (We've Lost Dancing)" by Fred again.. | 123 BPM | 3A | Energy 0.62 | Genre: melodic house
{"suggestions":[
  {"artist":"Bicep","title":"Glue","reason":"2A (−1 hour) smooth harmonic move, 130 BPM lifts tempo naturally into build phase. Same euphoric breakdown architecture and sustained pad textures — crowd will feel the continuity.","genre":"melodic techno","expected_bpm":130,"expected_key":"2A","mix_moment":"exit at final breakdown ~bar 64","energy_delta":"up","vibe_link":"same euphoric breakdown builds and sustained pads"},
  {"artist":"Lane 8","title":"Yard","reason":"3A identical key, 125 BPM near-seamless, emotive melodic techno shares Fred again..'s emotional weight without jarring the dancefloor.","genre":"melodic techno","expected_bpm":125,"expected_key":"3A","mix_moment":"exit at outro ~bar 80","energy_delta":"maintain","vibe_link":"shared emotional synth lead and rolling percussion texture"}
]}

EXAMPLE 2 — UK garage/deep house, peak (set_position 0.72):
Current: "Latch" by Disclosure | 120 BPM | 4B | Energy 0.78 | Genre: UK garage / deep house
{"suggestions":[
  {"artist":"Jamie xx","title":"I Know There's Gonna Be (Good Times)","reason":"4B identical key, 118 BPM seamless, shares UK bass DNA and vocal chop sensibility — crowd stays locked in.","genre":"UK bass / house","expected_bpm":118,"expected_key":"4B","mix_moment":"exit at second chorus ~bar 96","energy_delta":"maintain","vibe_link":"UK bass DNA, vocal chop style, same era"},
  {"artist":"Duke Dumont","title":"Won't Look Back","reason":"5B (+1 hour) smooth move, 122 BPM slight lift, warm UK deep house keeps genre neighbourhood tight while pushing energy forward.","genre":"deep house","expected_bpm":122,"expected_key":"5B","mix_moment":"exit at breakdown ~bar 48","energy_delta":"up","vibe_link":"UK deep house warmth, soulful vocal stabs"}
]}"""

_SYSTEM = f"""\
You are a professional DJ selector with deep music theory knowledge. Your job is to suggest the next tracks for a live DJ set.

{_CAMELOT_COMPAT}

ENERGY ARC RULES:
  set_position 0.0–0.3 (early/warm-up): prefer maintaining or gently lifting energy, stay within 5 BPM
  set_position 0.3–0.7 (building): push energy up, key changes of +2 allowed, BPM can rise 3–8%
  set_position 0.7–0.9 (peak): sustain or push harder, dramatic key changes OK with bridge recipes
  set_position 0.9–1.0 (cool-down): step energy down, gentle blends, return toward mellow keys

VIBE CONTINUITY (critical rule):
  First, infer the current track's genre from artist + title + BPM + key.
  Suggestions MUST stay within 1–2 genre hops maximum.
  Allowed genre hops: deep house → tech house → minimal techno (ok), melodic house → melodic techno (ok).
  Forbidden jumps without a bridge: house → drum & bass, pop → techno, ambient → peak-hour trance.
  The vibe_link field must name the specific sonic/atmospheric quality shared (e.g. "same dark warehouse energy", "similar synth texture and breakdown structure").

MIX MOMENT: For each suggestion specify WHERE in the outgoing track to begin the transition.
  Use format: "exit at [section] ~bar [N]" e.g. "exit at outro ~bar 64" or "exit at breakdown 2 ~bar 32"

DIVERSITY: Avoid suggesting the same artist twice in one response. Think beyond obvious choices.

OUTPUT FORMAT — return ONLY valid JSON, no markdown, no explanation:
{{"suggestions":[{{"artist":"","title":"","reason":"2-sentence reason referencing harmonic move, energy arc, and genre continuity","genre":"inferred genre of suggested track","expected_bpm":0,"expected_key":"","mix_moment":"exit at [section] ~bar N","energy_delta":"up|down|maintain","vibe_link":"short phrase naming the shared sonic quality"}}]}}

{_FEW_SHOT}"""

_USER_TEMPLATE = (
    'NOW PLAYING: "{title}" by {artist}\n'
    "BPM: {bpm:.1f} | Camelot Key: {camelot} | Duration: {duration:.0f}s | "
    "Avg Energy: {energy:.2f}/1.0 | Set position: {set_pos_pct}% through set\n"
    "Occasion: {occasion}\n"
    "Current track genre (infer from artist+title+BPM+key): [fill this before selecting]\n"
    "Suggestions MUST stay within this genre neighbourhood unless the occasion demands a shift.\n"
    "Already played (do NOT repeat): {history}\n\n"
    "Suggest {n} tracks. Prioritise: vibe continuity → harmonic compatibility → energy arc for {arc_phase} → diversity.\n"
    "Reply ONLY with the JSON object."
)


def _set_arc_phase(set_position: float) -> str:
    if set_position < 0.3:
        return "warm-up (build slowly)"
    if set_position < 0.7:
        return "build (push energy)"
    if set_position < 0.9:
        return "peak (sustain or escalate)"
    return "cool-down (wind down)"


def _extract_json(text: str) -> dict:
    """Robustly pull the first {...} block from LLM output."""
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text.strip())
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON in response: {text[:200]}")
    return json.loads(text[start:end])


def suggest_next_tracks(
    title: str,
    artist: str,
    bpm: float,
    camelot: str,
    duration: float,
    avg_energy: float,
    occasion: str,
    history: list[str],
    set_position: float = 0.0,
    n: int = 3,
) -> list[dict]:
    """
    Call local Ollama (gemma3:4b) to suggest next n tracks.
    Returns list of dicts with keys:
      artist, title, reason, genre, expected_bpm, expected_key,
      mix_moment, energy_delta, search_query.

    set_position: 0.0 = start of set, 1.0 = end of set.
    Compatible with both openai v0.x/3.x (ChatCompletion.create) and v1.x/v2.x (OpenAI client).
    """
    try:
        import openai
    except ImportError:
        raise RuntimeError("openai package not installed — run: pip install openai")

    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    model = os.environ.get("AUTOPILOT_MODEL", "gemma3:4b")
    api_key = os.environ.get("OPENAI_API_KEY", "ollama")

    user_msg = _USER_TEMPLATE.format(
        title=title,
        artist=artist,
        bpm=bpm,
        camelot=camelot,
        duration=duration,
        energy=avg_energy,
        occasion=occasion or "general DJ set",
        history=", ".join(history[-6:]) if history else "none",
        set_pos_pct=round(set_position * 100),
        arc_phase=_set_arc_phase(set_position),
        n=n,
    )

    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    if hasattr(openai, "OpenAI"):
        client = openai.OpenAI(base_url=base_url, api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.8,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
    else:
        openai.api_key = api_key
        openai.api_base = base_url
        resp = openai.ChatCompletion.create(
            model=model,
            messages=messages,
            temperature=0.8,
        )
        raw = resp["choices"][0]["message"]["content"] or "{}"

    data = _extract_json(raw)
    suggestions = data.get("suggestions", [])[:n]

    for s in suggestions:
        artist_s = s.get("artist", "")
        title_s = s.get("title", "")
        s["search_query"] = f"ytsearch1:{artist_s} {title_s} audio"
        # Ensure all expected fields present with defaults
        s.setdefault("genre", "")
        s.setdefault("mix_moment", "")
        s.setdefault("energy_delta", "maintain")
        s.setdefault("vibe_link", "")

    return suggestions
