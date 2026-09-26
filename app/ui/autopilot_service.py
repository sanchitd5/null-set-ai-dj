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


_SYSTEM = (
    "You are an expert DJ selector. Suggest tracks using Camelot harmonic mixing "
    "(same key=1.0, ±1 hour=0.9, relative A↔B=0.85, +2 energy boost=0.8, avoid ≥3 hr clashes). "
    "Stay within 6% BPM for smooth mixing. Build energy in arcs. "
    "Reply ONLY with valid JSON — no markdown, no explanation."
)

_USER_TEMPLATE = (
    'Current track: "{title}" by {artist}\n'
    "BPM: {bpm:.1f} | Key: {camelot} | Duration: {duration:.0f}s | Energy: {energy:.2f}/1.0\n"
    "Occasion: {occasion}\n"
    "Already played: {history}\n\n"
    'Suggest {n} tracks to play next as JSON:\n'
    '{{"suggestions":[{{"artist":"","title":"","reason":"one sentence",'
    '"expected_bpm":0,"expected_key":""}}]}}'
)


def _extract_json(text: str) -> dict:
    """Robustly pull the first {...} block from LLM output."""
    text = text.strip()
    # strip markdown code fences if present
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
    n: int = 3,
) -> list[dict]:
    """
    Call local Ollama (gemma3:4b) to suggest next n tracks.
    Returns list of {artist, title, reason, expected_bpm, expected_key, search_query}.

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
        history=", ".join(history[-5:]) if history else "none",
        n=n,
    )

    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    # Detect API version: v1+ has OpenAI class; v0.x/3.x uses module-level functions.
    if hasattr(openai, "OpenAI"):
        # v1.x / v2.x
        client = openai.OpenAI(base_url=base_url, api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.7,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
    else:
        # v0.x / v3.x (openai.ChatCompletion.create style)
        openai.api_key = api_key
        openai.api_base = base_url
        resp = openai.ChatCompletion.create(
            model=model,
            messages=messages,
            temperature=0.7,
        )
        raw = resp["choices"][0]["message"]["content"] or "{}"

    data = _extract_json(raw)
    suggestions = data.get("suggestions", [])[:n]

    # Attach yt-dlp search query for each suggestion.
    for s in suggestions:
        artist_s = s.get("artist", "")
        title_s = s.get("title", "")
        s["search_query"] = f"ytsearch1:{artist_s} {title_s} audio"

    return suggestions
