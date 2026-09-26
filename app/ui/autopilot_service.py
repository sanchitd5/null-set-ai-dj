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

from app.ui import llm_gate

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
EXAMPLE 1 — melodic house, early set (set_position 0.15), building vibe before branching out:
Current: "Alvaro" by Fred again.. | 122 BPM | 11A | Energy 0.55 | Genre: melodic house
History: [] (nothing played yet — safe to stay in same artist world)
{"current_genre":"melodic house","current_profile":{"energy":6,"tempo_feel":"driving","drums":"steady","vocals":"chopped","mood":"bittersweet","texture":"raw"},"suggestions":[
  {"artist":"Fred again..","title":"Marea (We've Lost Dancing)","reason":"11A identical key, 123 BPM seamless; same chopped vocal loop over a steady four-to-the-floor kick, same bittersweet lift, so the floor keeps moving.","genre":"melodic house","expected_bpm":123,"expected_key":"11A","mix_moment":"exit at vocal breakdown ~bar 48","energy_delta":"maintain","vibe_link":"same granular vocal chops and melodic 4-bar drops","track_profile":{"energy":7,"tempo_feel":"driving","drums":"steady","vocals":"chopped","mood":"bittersweet","texture":"raw"}},
  {"artist":"Fred again..","title":"Delilah (pull me out of this)","reason":"12A (+1 hour) gentle key lift, 118 BPM keeps the pocket tight; layered field-recording textures and a driving kick keep the same raw, bittersweet feel.","genre":"melodic house","expected_bpm":118,"expected_key":"12A","mix_moment":"exit at outro pad wash ~bar 64","energy_delta":"maintain","vibe_link":"layered field-recording texture and slow-burn emotional build","track_profile":{"energy":6,"tempo_feel":"driving","drums":"steady","vocals":"chopped","mood":"bittersweet","texture":"raw"}}
]}

EXAMPLE 2 — UK garage/deep house, peak (set_position 0.72):
Current: "Latch" by Disclosure | 120 BPM | 4B | Energy 0.78 | Genre: UK garage / deep house
History: ["Disclosure - When A Fire Starts To Burn", "Disclosure - Latch"]
{"current_genre":"UK garage / deep house","current_profile":{"energy":8,"tempo_feel":"driving","drums":"busy","vocals":"sung","mood":"euphoric","texture":"polished"},"suggestions":[
  {"artist":"Jamie xx","title":"I Know There's Gonna Be (Good Times)","reason":"4B identical key, 118 BPM seamless; punchy UK bass drum pattern and pitched vocal chops keep the same euphoric, busy groove.","genre":"UK bass / house","expected_bpm":118,"expected_key":"4B","mix_moment":"exit at second chorus ~bar 96","energy_delta":"maintain","vibe_link":"punchy UK bass kick and pitched vocal chop cadence","track_profile":{"energy":8,"tempo_feel":"driving","drums":"busy","vocals":"chopped","mood":"euphoric","texture":"polished"}},
  {"artist":"Duke Dumont","title":"Won't Look Back","reason":"5B (+1 hour) smooth harmonic lift, 122 BPM slight tempo push; soulful sung hook and swung hats keep the euphoric floor while nudging energy forward.","genre":"deep house","expected_bpm":122,"expected_key":"5B","mix_moment":"exit at breakdown ~bar 48","energy_delta":"up","vibe_link":"soulful vocal stabs and swung hi-hat groove","track_profile":{"energy":8,"tempo_feel":"driving","drums":"steady","vocals":"sung","mood":"euphoric","texture":"polished"}}
]}"""

_SYSTEM = f"""\
You are a professional DJ selector with deep music theory knowledge. Your job is to suggest the next tracks for a live DJ set.

{_CAMELOT_COMPAT}

ENERGY ARC RULES:
  set_position 0.0–0.3 (early/warm-up): prefer maintaining or gently lifting energy, stay within 5 BPM
  set_position 0.3–0.7 (building): push energy up, key changes of +2 allowed, BPM can rise 3–8%
  set_position 0.7–0.9 (peak): sustain or push harder, dramatic key changes OK with bridge recipes
  set_position 0.9–1.0 (cool-down): step energy down, gentle blends, return toward mellow keys

OCCASION FIRST (overrides VIBE CONTINUITY, SAME-ARTIST and CREDITS when they conflict):
  The occasion says who is on the floor and what music they came for. If it names a music
  culture or event with its own canon and the current song is outside it, STEER into that
  world as a quick journey of AT MOST 5-7 short bridge songs (each plays only 30-60 s):
    every step MUST sit inside the TEMPO WINDOW so the beats can be matched (the DJ blends
    beat to beat; no echo-outs): pick the version that fits - a remix, edit, bhangra-house /
    club mix, or a song that is natively at that tempo. If the target world lives at another
    tempo, climb there in a TEMPO LADDER of <= 6% per step. Each step is also clearly closer
    to the target world than the last; by step 5-7 you MUST be playing the occasion's own
    anthems. The occasion line
    tells you which step you are on - at step N be about N/6 of the way there.
  Once inside that world, apply VIBE CONTINUITY within it. Examples of canons:
    "punjabi wedding" / "bhangra" -> Diljit Dosanjh, AP Dhillon, Karan Aujla, Sidhu Moose Wala,
      Panjabi MC, Imran Khan, Yo Yo Honey Singh, Guru Randhawa, Jazzy B, Malkit Singh
      (bridges from electronic: Panjabi MC "Mundian To Bach Ke", Diljit x Sia, bhangra remixes)
    "bollywood night" -> Bollywood dance hits; "latin party" -> reggaeton / salsa / dembow;
    "afrobeats" -> Burna Boy, Wizkid, Rema; "90s hip-hop" -> 90s rap classics.
  Set top-level "steering":"move" while the current song is outside the occasion's music,
  otherwise "stay"; "occasion_fit" 0-10 = how well the CURRENT song fits the occasion.

VIBE CONTINUITY (critical rule):
  First, infer the current track's genre from artist + title + BPM + key.
  Suggestions MUST stay within 1–2 genre hops maximum.
  Allowed genre hops: deep house → tech house → minimal techno (ok), melodic house → melodic techno (ok).
  Forbidden jumps without a bridge: house → drum & bass, pop → techno, ambient → peak-hour trance.
  vibe_link MUST describe specific sonic characteristics, NOT genre labels:
    Good: "same granular vocal chops and melodic 4-bar drops"
    Good: "punchy UK bass kick and pitched vocal chop cadence"
    Bad: "same genre", "similar style", "UK bass DNA"

TRACK PROFILE (judge the SONG, not the artist):
  Artist reputation is NOT enough. The same artist has chill tracks and peak tracks.
  First describe the CURRENT track in "current_profile", then give every suggestion its own "track_profile":
    energy: integer 1-10 (how hard THIS song hits on a dancefloor; NOT the 0-1 Avg Energy number)
    tempo_feel: driving | mid | laid-back
    drums: none | sparse | steady | busy
    vocals: none | chopped | sung
    mood: euphoric | bittersweet | dark | chill
    texture: raw | polished
  A suggestion MUST stay close to the current profile:
    energy within 2 points, tempo_feel not flipped (driving <-> laid-back is forbidden),
    mood not flipped (euphoric <-> dark, euphoric <-> chill, dark <-> chill are forbidden).
  Pick songs you actually know the sound of. If unsure how a song sounds, do not suggest it.

FORBIDDEN SUGGESTIONS (never, regardless of any other rule):
  DJ sets, live sets, Boiler Room / Essential Mix / Fabric / radio shows, podcasts, mixes, megamixes,
  compilations, albums, EPs, interviews, remix packs, anything longer than 9 minutes.
  Suggest ONLY individual released songs, using the song's real title.

CREDITS RULE (collaborations): when the current song credits several artists
  ("A & B"), decide which credit defines its SOUND - usually the best-known
  producer whose discography you know best - and anchor on that artist's world:
  their other songs first, then close peers. A lesser-known featured or
  co-credited artist must not pull the set into their genre. Example: "LATIN MAFIA
  & Fred again.. - Te Estoy Correteando" -> anchor on Fred again.. (his own
  songs, UK dance / melodic house peers), not Latin house.

SAME-ARTIST RULE (important):
  Suggesting the same ARTIST as the current track is valid and often preferred early in a set to
  build a consistent vibe — back-to-back tracks by the same artist feel like chapters in one story.
  NEVER penalize a suggestion just because the artist matches. Only avoid repeating the exact same
  TRACK TITLE that appears in the history list.

MIX MOMENT: For each suggestion specify WHERE in the outgoing track to begin the transition.
  Use format: "exit at [section] ~bar [N]" e.g. "exit at outro ~bar 64" or "exit at breakdown 2 ~bar 32"

AVOID TRACKS: The history list contains track names already played. Do NOT suggest any track whose
  title appears in that list. Same artist is fine — only the exact title is banned.

OUTPUT FORMAT — return ONLY valid JSON, no markdown, no explanation:
{{"steering":"stay|move","occasion_fit":0,"current_genre":"","current_profile":{{"energy":0,"tempo_feel":"","drums":"","vocals":"","mood":"","texture":""}},"suggestions":[{{"artist":"","title":"","reason":"2-sentence reason referencing harmonic move, energy arc, and how THIS song's sound matches","genre":"inferred genre of suggested track","expected_bpm":0,"expected_key":"","mix_moment":"exit at [section] ~bar N","energy_delta":"up|down|maintain","vibe_link":"specific sonic characteristic shared — NOT a genre label","track_profile":{{"energy":0,"tempo_feel":"","drums":"","vocals":"","mood":"","texture":""}}}}]}}

{_FEW_SHOT}"""

TEMPO_LOCK_PCT = 0.06  # the autopilot pitch-locks the next song within +/-8%; aim inside 6%


def tempo_window(bpm: float) -> str:
    """Explicit BPM ranges the next song must sit in (small models get this
    arithmetic wrong, so hand it over pre-computed). Half/double time counts:
    the autopilot locks 87 BPM against 174 BPM."""
    if not bpm or bpm <= 0:
        return "unknown - pick songs close to the current tempo"
    lo, hi = bpm * (1 - TEMPO_LOCK_PCT), bpm * (1 + TEMPO_LOCK_PCT)
    parts = [f"{lo:.0f}-{hi:.0f} BPM"]
    parts.append(f"or half-time {lo / 2:.0f}-{hi / 2:.0f}" if bpm >= 140 else f"or double-time {lo * 2:.0f}-{hi * 2:.0f}")
    return " ".join(parts) + " (songs outside this cannot be blended beat to beat)"


_USER_TEMPLATE = (
    'NOW PLAYING: "{title}" by {artist}\n'
    "BPM: {bpm:.1f} | Camelot Key: {camelot} | Duration: {duration:.0f}s | "
    "Avg Energy: {energy:.2f}/1.0 | Set position: {set_pos_pct}% through set\n"
    "Occasion: {occasion}\n"
    "Set mode: {set_mode_line}\n"
    "TEMPO WINDOW (required, so the next song can be beat-matched - also while steering; "
    "choose a remix / edit that fits rather than leaving it): {tempo_window}\n"
    "First fill current_genre and current_profile for THIS song, then pick songs whose own "
    "track_profile stays close to it. Stay in this genre neighbourhood unless the occasion demands a shift.\n"
    "Already played titles (avoid exact titles, same artist OK): {history}\n\n"
    "Suggest {n} tracks. Prioritise: vibe continuity → harmonic compatibility → energy arc for {arc_phase} → diversity.\n"
    "Reply ONLY with the JSON object."
)
_KNOWLEDGE_TEMPLATE = "\n\nDJ KNOWLEDGE (from the ./DJ wiki):\n{brief}"


def _set_arc_phase(set_position: float) -> str:
    if set_position < 0.3:
        return "warm-up (build slowly)"
    if set_position < 0.7:
        return "build (push energy)"
    if set_position < 0.9:
        return "peak (sustain or escalate)"
    return "cool-down (wind down)"


_MOOD_CLASH = {
    frozenset({"euphoric", "dark"}),
    frozenset({"euphoric", "chill"}),
    frozenset({"dark", "chill"}),
}
_MAX_ENERGY_GAP = 2


def _profile_clash(cur: dict, sug: dict) -> str | None:
    """Return a reason string if the suggestion's profile clashes with the current track's."""
    if not isinstance(cur, dict) or not isinstance(sug, dict):
        return None  # model skipped profiles: can't judge, audio vibe gate still applies
    def _e(v) -> float:
        v = float(v)
        return v * 10 if v <= 1.0 else v  # small models often answer on a 0-1 scale

    try:
        gap = abs(_e(sug.get("energy")) - _e(cur.get("energy")))
        if gap > _MAX_ENERGY_GAP:
            return f"energy gap {gap:.0f}"
    except (TypeError, ValueError):
        pass
    tf = {str(cur.get("tempo_feel", "")).lower(), str(sug.get("tempo_feel", "")).lower()}
    if tf == {"driving", "laid-back"}:
        return "tempo feel flip"
    moods = frozenset({str(cur.get("mood", "")).lower(), str(sug.get("mood", "")).lower()})
    if moods in _MOOD_CLASH:
        return "mood flip"
    return None


def _filter_suggestions(data: dict, history: list[str]) -> list[dict]:
    """Drop sets/interviews, exact repeats and profile clashes. Never returns empty if the
    model gave at least one allowed song: the closest clash is kept as a last resort."""
    from app.ui.download_service import _is_mix, _is_non_music

    played = {h.lower() for h in history}
    cur = data.get("current_profile")
    ok, clashes = [], []
    for s in data.get("suggestions", []) or []:
        if not isinstance(s, dict) or not s.get("title"):
            continue
        label = f"{s.get('artist', '')} {s['title']}"
        if _is_mix(label) or _is_non_music(label):
            continue
        if any(s["title"].lower() in p for p in played):
            continue
        # Steering toward the occasion's music is a deliberate genre/mood move:
        # continuity clashes with the CURRENT song are expected, not errors.
        steer = str(data.get("steering", "")).lower().startswith("move")
        reason = None if steer else _profile_clash(cur, s.get("track_profile"))
        (clashes if reason else ok).append(s)
        if reason:
            s["rejected_reason"] = reason
    return ok or clashes[:1]


SET_MODES = ("long", "quick", "hybrid")
SET_MODE_LINES = {
    "long": "LONG (each song plays 3-6 min): pick deep, rolling songs with long intros/outros and "
            "patient builds that reward a long ride; energy changes slowly (maintain or gentle up).",
    "quick": "QUICK (songs switch every 1-2 min, high energy): pick instantly recognisable, "
             "high-energy songs with early hooks and big drops; energy stays high (up or maintain, never down).",
    "hybrid": "HYBRID: mix both, long rides on deep grooves, quick switches on peak-energy songs.",
}


def _extract_json(text: str) -> dict:
    """Robustly pull the first {...} block from LLM output."""
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text.strip())
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON in response: {text[:200]}")
    return _loads_repaired(text[start:end])


def _loads_repaired(body: str, max_fixes: int = 20) -> dict:
    """json.loads, fixing the small-model slips seen live at the exact spot
    the decoder stops: a missing comma ("Expecting ',' delimiter") and a
    trailing comma before } or ]. Anything else re-raises (caller retries)."""
    for _ in range(max_fixes):
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            pos = exc.pos
            prev = body[:pos].rstrip()
            if body[pos:pos + 1] == "," and body[pos + 1:].lstrip()[:1] in ("}", "]"):
                body = body[:pos] + body[pos + 1:]  # py3.13+: points at the comma
            elif exc.msg.startswith("Expecting ',' delimiter"):
                body = body[:pos] + "," + body[pos:]
            elif prev.endswith(",") and body[pos:pos + 1] in ("}", "]"):
                body = prev[:-1] + body[pos:]
            elif exc.msg.startswith("Expecting property name") and prev.endswith(","):
                body = prev[:-1] + body[pos:]
            else:
                raise
    return json.loads(body)


GATE_WAIT_S = 240.0  # suggest / look-ahead give up after this long in the queue


def chat_raw(
    system: str,
    user: str,
    temperature: float = 0.5,
    timeout: float | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    priority: int = llm_gate.SUGGEST,
) -> str:
    """One JSON-mode chat call to the local model; returns the raw text.

    Every call passes the priority gate (app/ui/llm_gate.py): one LLM call at
    a time, PLAN before SUGGEST before LOOKAHEAD. A plan's `timeout` covers
    its wait in the queue plus the call itself.
    """
    wait = timeout if timeout is not None else GATE_WAIT_S
    with llm_gate.gate.slot(priority, wait_timeout=wait) as waited:
        left = None if timeout is None else max(5.0, timeout - waited)
        return _chat_call(system, user, temperature, left, model, max_tokens)


def _chat_call(system, user, temperature, timeout, model, max_tokens) -> str:
    """The HTTP call itself. Supports openai v0.x/3.x (ChatCompletion.create)
    and v1.x/v2.x (OpenAI client). response_format may be ignored by the
    server (mlx_lm.server): callers always parse with _extract_json.
    max_tokens stops a server default from truncating the JSON.
    """
    try:
        import openai
    except ImportError:
        raise RuntimeError("openai package not installed — run: pip install openai")

    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    model = model or os.environ.get("AUTOPILOT_MODEL", "gemma3:4b")
    api_key = os.environ.get("OPENAI_API_KEY", "ollama")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    if hasattr(openai, "OpenAI"):
        client = openai.OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
            **({"max_tokens": max_tokens} if max_tokens else {}),
        )
        return resp.choices[0].message.content or "{}"
    openai.api_key = api_key
    openai.api_base = base_url
    kwargs = {"request_timeout": timeout} if timeout else {}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    resp = openai.ChatCompletion.create(
        model=model, messages=messages, temperature=temperature, **kwargs,
    )
    return resp["choices"][0]["message"]["content"] or "{}"


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
    set_mode: str = "hybrid",
    meta: dict | None = None,
    genre: str = "",
    history_display: list[str] | None = None,
    lookahead: bool = False,
) -> list[dict]:
    """
    Call local Ollama (gemma3:4b) to suggest next n tracks.
    Returns list of dicts with keys:
      artist, title, reason, genre, expected_bpm, expected_key,
      mix_moment, energy_delta, search_query.

    set_position: 0.0 = start of set, 1.0 = end of set.
    genre: this track's genre from an earlier suggestion ("" = generic DJ rules).
    history_display: cleaned history names for the prompt; `history` (raw names)
    still drives the repeat filter.
    lookahead: songs for AFTER the booked next one; lowest LLM priority.
    Compatible with both openai v0.x/3.x (ChatCompletion.create) and v1.x/v2.x (OpenAI client).
    """
    user_msg = _USER_TEMPLATE.format(
        tempo_window=tempo_window(bpm),
        title=title,
        artist=artist,
        bpm=bpm,
        camelot=camelot,
        duration=duration,
        energy=avg_energy,
        occasion=occasion or "general DJ set",
        set_mode_line=SET_MODE_LINES.get(set_mode, SET_MODE_LINES["hybrid"]),
        history=", ".join((history_display or history)[-6:]) if history else "none",
        set_pos_pct=round(set_position * 100),
        arc_phase=_set_arc_phase(set_position),
        n=n,
    )

    try:
        from app.music_brain.dj_knowledge import selection_brief
        brief = selection_brief(genre or "")
    except Exception:  # grounding is best-effort
        brief = ""
    if brief:
        user_msg += _KNOWLEDGE_TEMPLATE.format(brief=brief)

    prio = llm_gate.LOOKAHEAD if lookahead else llm_gate.SUGGEST
    data = None
    for attempt in range(2):  # one retry when the JSON is past repair
        raw = chat_raw(_SYSTEM, user_msg, temperature=0.5, max_tokens=700, priority=prio)
        try:
            data = _extract_json(raw)
            break
        except ValueError as exc:  # JSONDecodeError is a ValueError
            if attempt:
                raise
            print(f"[suggest] bad JSON, retrying once: {exc}", flush=True)
    suggestions = _filter_suggestions(data, history)[:n]
    if meta is not None:  # caller wants the model's read of the CURRENT track too
        meta["current_profile"] = data.get("current_profile") or {}
        meta["current_genre"] = data.get("current_genre") or ""
        meta["steering"] = "move" if str(data.get("steering", "")).lower().startswith("move") else "stay"
        try:
            meta["occasion_fit"] = max(0.0, min(10.0, float(data.get("occasion_fit"))))
        except (TypeError, ValueError):
            meta["occasion_fit"] = None

    for s in suggestions:
        artist_s = s.get("artist", "")
        title_s = s.get("title", "")
        # YouTube Music songs search with strict song matching (see
        # download_service): no live recordings, sets, remixes or covers.
        s["search_query"] = f"ytmsearch:{artist_s} - {title_s}"
        # Ensure all expected fields present with defaults
        s.setdefault("genre", "")
        s.setdefault("mix_moment", "")
        s.setdefault("energy_delta", "maintain")
        s.setdefault("vibe_link", "")
        s.setdefault("track_profile", {})

    return suggestions
