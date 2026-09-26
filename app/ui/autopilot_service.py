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
  ±1 hour opposite letter (e.g. 8A→9B or 8A→7B): diagonal move, score 0.75
  -2 hours same letter (e.g. 8A→6A): energy drop, score 0.6
  3+ hours apart: harmonic clash — avoid unless using Echo Out or Breakdown recipe.
  The ALLOWED KEYS line below lists the clean moves already worked out for you.

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
]}

EXAMPLE 3 - hip-hop / trap, occasion "birthday party", mid-set (set_position 0.5). Half-time counts: 72 BPM x2 = 144, inside the 141-159 window:
Current: "HUMBLE." by Kendrick Lamar | 150 BPM | 4A | Energy 0.70 | Genre: hip-hop / trap
{"steering":"stay","occasion_fit":8,"current_genre":"hip-hop / trap","current_profile":{"energy":8,"tempo_feel":"mid","drums":"sparse","vocals":"sung","mood":"dark","texture":"polished"},"suggestions":[
  {"artist":"Future","title":"Mask Off","reason":"4A identical key, 150 BPM seamless; flute loop over sparse 808s keeps the dark, head-nod bounce.","genre":"trap","expected_bpm":150,"expected_key":"4A","mix_moment":"exit at hook end ~bar 32","energy_delta":"maintain","vibe_link":"sparse 808 bounce under a looped melodic hook","occasion_fit":8,"track_profile":{"energy":7,"tempo_feel":"mid","drums":"sparse","vocals":"sung","mood":"dark","texture":"polished"}},
  {"artist":"Lil Uzi Vert","title":"XO Tour Llif3","reason":"5A (+1 hour), 155 BPM small push; same minor-key trap bounce with a sung melodic hook.","genre":"trap","expected_bpm":155,"expected_key":"5A","mix_moment":"exit at second hook ~bar 48","energy_delta":"up","vibe_link":"minor-key synth lead over rattling hi-hats","occasion_fit":8,"track_profile":{"energy":8,"tempo_feel":"mid","drums":"busy","vocals":"sung","mood":"dark","texture":"polished"}},
  {"artist":"Kendrick Lamar","title":"Money Trees","reason":"4B relative major, 72 BPM locks at double time (144); a laid-back same-artist breather.","genre":"hip-hop","expected_bpm":72,"expected_key":"4B","mix_moment":"exit at verse end ~bar 40","energy_delta":"down","vibe_link":"hazy sampled loop and dry storytelling vocal","occasion_fit":7,"track_profile":{"energy":6,"tempo_feel":"mid","drums":"sparse","vocals":"sung","mood":"dark","texture":"raw"}}
]}

EXAMPLE 4 - drum & bass, no occasion, peak (set_position 0.8). 174 BPM DnB also locks with 87 BPM half-time tracks:
Current: "Tarantula" by Pendulum | 174 BPM | 9A | Energy 0.85 | Genre: drum & bass
{"steering":"stay","occasion_fit":0,"current_genre":"drum & bass","current_profile":{"energy":9,"tempo_feel":"driving","drums":"busy","vocals":"sung","mood":"euphoric","texture":"polished"},"suggestions":[
  {"artist":"Sub Focus","title":"Solar System","reason":"9A identical key, 174 BPM seamless; rolling breaks and big synth stabs hold the peak.","genre":"drum & bass","expected_bpm":174,"expected_key":"9A","mix_moment":"exit at second drop ~bar 96","energy_delta":"maintain","vibe_link":"rolling two-step breaks under wide synth stabs","occasion_fit":0,"track_profile":{"energy":9,"tempo_feel":"driving","drums":"busy","vocals":"none","mood":"euphoric","texture":"polished"}},
  {"artist":"Chase & Status","title":"Baddadan","reason":"10A (+1 hour), 174 BPM; heavier jump-up bass keeps the peak but darkens slightly.","genre":"drum & bass","expected_bpm":174,"expected_key":"10A","mix_moment":"exit at drop 2 end ~bar 80","energy_delta":"maintain","vibe_link":"punchy DnB kick with MC vocal hooks","occasion_fit":0,"track_profile":{"energy":9,"tempo_feel":"driving","drums":"busy","vocals":"chopped","mood":"euphoric","texture":"raw"}},
  {"artist":"Wilkinson","title":"Afterglow","reason":"9B relative major, 174 BPM; its half-time 87 intro lets the floor breathe before the liquid drop.","genre":"liquid drum & bass","expected_bpm":174,"expected_key":"9B","mix_moment":"exit at outro ~bar 128","energy_delta":"down","vibe_link":"liquid breaks with a soaring sung chorus","occasion_fit":0,"track_profile":{"energy":8,"tempo_feel":"driving","drums":"busy","vocals":"sung","mood":"euphoric","texture":"polished"}}
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
  THEME LOCK: once inside the occasion's world, EVERY suggestion must itself fit the occasion
  (its own "occasion_fit" >= 7). Variety / subgenre changes happen WITHIN the theme - for a
  Punjabi wedding: bhangra, Punjabi pop, Punjabi hip-hop, bhangra-house, Punjabi Bollywood
  dance numbers; NOT generic Indian hip-hop, chill Indian electronica or unrelated EDM.
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
{{"steering":"stay|move","occasion_fit":0,"current_genre":"","current_profile":{{"energy":0,"tempo_feel":"","drums":"","vocals":"","mood":"","texture":""}},"suggestions":[{{"artist":"","title":"","reason":"2-sentence reason referencing harmonic move, energy arc, and how THIS song's sound matches","genre":"inferred genre of suggested track","expected_bpm":0,"expected_key":"","mix_moment":"exit at [section] ~bar N","energy_delta":"up|down|maintain","vibe_link":"specific sonic characteristic shared — NOT a genre label","genre_hop":0,"occasion_fit":0,"track_profile":{{"energy":0,"tempo_feel":"","drums":"","vocals":"","mood":"","texture":""}}}}]}}

{_FEW_SHOT}"""

TEMPO_LOCK_PCT = 0.06  # the autopilot pitch-locks the next song within +/-8%; aim inside 6%


MIN_THEME_FIT = 6.0
SUGGEST_TEMPERATURE = 0.75


def _num(v):
    try:
        return max(0.0, min(10.0, float(v)))
    except (TypeError, ValueError):
        return None


def _bare_title(title: str) -> str:
    """Song identity for repeat checks: no (...)/[...] tags, feat. credits, punctuation."""
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", str(title).lower())
    t = re.split(r"\s+[-–—|]\s+", t, maxsplit=1)[0]   # "Excuses (Remix) - DJ Cut" -> "excuses"
    t = re.sub(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", "", t)
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", t).split())


def _recent_artists(names: list, n: int = 4) -> str:
    """Artists of the last n played songs ("Artist - Title" names)."""
    out = []
    for name in list(names or [])[-n:]:
        artist = str(name).split(" - ", 1)[0].strip()
        if artist and artist.lower() not in (a.lower() for a in out):
            out.append(artist)
    return ", ".join(out) or "none"


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


def _camelot_at(hour: int, letter: str) -> str:
    return f"{(hour - 1) % 12 + 1}{letter}"


def allowed_keys(camelot: str) -> str:
    """The clean key moves from `camelot`, pre-computed for the prompt:
    "8A, 7A, 9A, 8B, 10A (+2 boost), 7B/9B (diagonal)"."""
    m = re.match(r"^\s*(\d{1,2})([AB])\s*$", str(camelot or ""), re.IGNORECASE)
    if not m or not 1 <= int(m.group(1)) <= 12:
        return "unknown - stay harmonically close"
    h, letter = int(m.group(1)), m.group(2).upper()
    other = "B" if letter == "A" else "A"
    return (
        f"{_camelot_at(h, letter)}, {_camelot_at(h - 1, letter)}, {_camelot_at(h + 1, letter)}, "
        f"{_camelot_at(h, other)}, {_camelot_at(h + 2, letter)} (+2 boost), "
        f"{_camelot_at(h - 1, other)}/{_camelot_at(h + 1, other)} (diagonal)"
    )


def loudness_label(dbfs: float | None) -> str:
    """Absolute whole-track RMS loudness, comparable across songs (Avg Energy is
    peak-normalised per song, so a quiet ballad and a club banger both read ~0.5)."""
    if dbfs is None:
        return "unknown"
    try:
        db = float(dbfs)
    except (TypeError, ValueError):
        return "unknown"
    label = "loud" if db > -10 else "medium" if db > -14 else "quiet"
    return f"{db:.0f} dBFS ({label})"


_LEAD_SYSTEM = (
    "You are a professional open-format DJ taking the dance floor on a journey the DJ asked for. "
    "You know music across cultures and languages (Western electronic, pop, hip-hop, Punjabi / "
    "Bollywood, Latin, Afrobeats...). Suggest REAL released songs only. Reply with JSON only."
)

_LEAD_TEMPLATE = (
    'NOW PLAYING: "{title}" by {artist} | {bpm:.0f} BPM | key {camelot}\n'
    "{lead}\n"
    "{tempo_line}\n"
    "Already played (never again): {history}\n\n"
    "Think of the path: what connects the current song's world to the destination's world "
    "(shared producers, crossover collabs, fusion remixes, similar rhythm), then give {n} songs "
    "for THIS step.\n"
    'JSON: {{"steering":"move","occasion_fit":0,"current_genre":"","current_profile":{{"energy":0,'
    '"tempo_feel":"","drums":"","vocals":"","mood":"","texture":""}},"suggestions":[{{"artist":"",'
    '"title":"","reason":"why it is this step of the journey","genre":"","expected_bpm":0,'
    '"expected_key":"","mix_moment":"","energy_delta":"up|down|maintain","vibe_link":"",'
    '"occasion_fit":0,"track_profile":{{"energy":0,"tempo_feel":"","drums":"","vocals":"",'
    '"mood":"","texture":""}}}}]}}'
)


def lead_line(lead_to: str, step: int, steps: int, bpm: float | None = None) -> str:
    """DESTINATION block for the user's LEAD TO request (top of the prompt).

    Tucked into the occasion text, the destination lost to vibe continuity /
    ALLOWED KEYS / TEMPO WINDOW and the model kept suggesting the current world
    (Marea -> Anyma, Yotto when asked to lead to Diljit Dosanjh - Lover). So it
    gets its own highest-priority line with an explicit per-step ratio.
    """
    target = " ".join(str(lead_to or "").split())[:120]
    if not target or steps <= 0:
        return ""
    step = max(1, min(int(step or 1), int(steps)))
    is_song = " - " in target
    tempo = f" (~{float(bpm):.0f} BPM)" if bpm else ""
    frac = step / steps
    how = ("a crossover / fusion that still has the current song's feel but clearly adds the "
           "destination's sound" if frac < 0.45 else
           "mostly the destination's world, keeping one thread (tempo, energy or texture) to the "
           "current song" if frac < 0.9 else
           "squarely inside the destination's own world")
    tail = ("The destination SONG itself plays right after the last step - do NOT suggest it; "
            "pick songs that lead naturally into it."
            if is_song else "After the last step the set stays in that world.")
    return (f"DESTINATION (the DJ's explicit instruction - HIGHEST priority, overrides VIBE "
            f"CONTINUITY, SAME-ARTIST, ALLOWED KEYS and occasion rules; tempo still blends): "
            f"lead the set to \"{target}\"{tempo} in {steps} songs. This is step {step} of {steps}: "
            f"every suggestion must be {how}. Genre / language / culture of each suggestion must be "
            f"visibly closer to the destination than the current song. {tail}\n")


def tempo_bridge_line(tempo_target: float, tempo_note: str = "") -> str:
    """A bridge ladder step: the next song aims at a new tempo, not the current one."""
    lo, hi = tempo_target * 0.96, tempo_target * 1.04
    note = f"; {tempo_note}" if tempo_note else ""
    return (
        f"TEMPO BRIDGE: pick songs natively near {tempo_target:.0f} BPM (±4%: {lo:.0f}-{hi:.0f}), "
        f"or half/double time{note}\n"
    )


_TEMPO_WINDOW_LINE = (
    "TEMPO WINDOW (required, so the next song can be beat-matched - also while steering; "
    "choose a remix / edit that fits rather than leaving it): {tempo_window}\n"
)

_USER_TEMPLATE = (
    'NOW PLAYING: "{title}" by {artist}\n'
    "BPM: {bpm:.1f} | Camelot Key: {camelot} | Duration: {duration:.0f}s | "
    "Avg Energy: {energy:.2f}/1.0 (relative to this song's own peak) | Loudness: {loudness} | "
    "Set position: {set_pos_pct}% through set\n"
    "{lead_line}"
    "Occasion: {occasion}\n"
    "Set mode: {set_mode_line}\n"
    "{tempo_line}"
    "ALLOWED KEYS (expected_key must be one of these unless steering): {allowed_keys}\n"
    "First fill current_genre and current_profile for THIS song, then pick songs whose own "
    "track_profile stays close to it. Stay in this genre neighbourhood unless the occasion demands a shift.\n"
    "Already played this set - NEVER suggest these again: {history}\n"
    "Artists heard in the last few songs (pick someone else unless it is a deliberate "
    "same-artist moment early in the set): {recent_artists}\n"
    "Played in the listener's EARLIER sets - they have heard these recently, so prefer fresh "
    "songs over them (only reuse one if it is clearly the perfect fit): {earlier_sets}\n"
    "At least ONE of your suggestions must be a less obvious pick (a deep cut, a newer release "
    "or a lesser-played gem) FROM THE SAME GENRE as the current song, not the genre's most famous anthem.\n"
    "genre_hop for each suggestion: 0 = same subgenre, 1 = neighbouring subgenre (e.g. melodic house -> "
    "progressive house), 2 = a different genre, 3 = unrelated (e.g. melodic house -> soft rock). "
    "Unless the occasion or a DESTINATION asks for a change, every suggestion must be genre_hop 0 or 1.\n\n"
    "Suggest {n} tracks. Prioritise: vibe continuity → harmonic compatibility → energy arc for {arc_phase} → diversity.\n"
    "Reply ONLY with the JSON object, compact (no line breaks or indentation). Keep every "
    "reason under 20 words, vibe_link under 8 words, mix_moment under 6 words."
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


def _key_clash_reason(current_key: str | None, expected_key) -> str | None:
    """A reason when expected_key is clearly outside ALLOWED KEYS (3+ hours on the
    wheel). Unknown / unparseable keys are kept: the check is soft."""
    from app.music_brain.recipe_matcher import is_key_clash

    if not current_key or not expected_key:
        return None
    try:
        if is_key_clash(str(current_key), str(expected_key)):
            return f"key clash {current_key} -> {str(expected_key).strip().upper()}"
    except ValueError:
        return None
    return None


MAX_GENRE_HOP = 1  # 0 same subgenre, 1 neighbour; 2+ only on a deliberate move

# Broad genre families, matched as substrings of the model's genre labels. The
# model's own genre_hop is not trusted alone: it rated Afusic "Pal Pal" (Urdu
# pop) -> Fred again.. "Delilah" (UK electronic) as a small hop.
GENRE_FAMILIES = {
    "south_asian": ("punjabi", "bhangra", "desi", "bollywood", "hindi", "urdu", "pakistani",
                    "indian", "filmi", "sufi", "qawwali", "haryanvi", "tamil", "telugu"),
    "electronic": ("house", "techno", "garage", "trance", "edm", "electronic", "electronica",
                   "dubstep", "drum & bass", "drum and bass", "dnb", "bass music", "breakbeat",
                   "downtempo", "ambient", "future bass", "electro", "idm", "jungle"),
    "hiphop": ("hip-hop", "hip hop", "rap", "trap", "drill", "grime"),
    "rnb": ("r&b", "rnb", "soul"),
    "latin": ("reggaeton", "latin", "dembow", "cumbia", "bachata", "salsa", "urbano"),
    "afro": ("afrobeat", "afro", "amapiano", "afropop"),
    "rock": ("rock", "punk", "metal", "grunge"),
    "pop": ("pop",),
    "country": ("country", "folk", "americana"),
    "jazz": ("jazz", "funk", "disco"),
}


def _genre_families(label) -> set:
    g = str(label or "").lower()
    return {fam for fam, keys in GENRE_FAMILIES.items() if any(k in g for k in keys)}


def _family_jump(current_genre, genre) -> bool:
    """True when both labels are known and share no family (e.g. Urdu pop -> UK garage)."""
    a, b = _genre_families(current_genre), _genre_families(genre)
    return bool(a and b) and not (a & b)


def _filter_suggestions(
    data: dict, history: list[str], occasion_set: bool = False, current_key: str | None = None,
    allow_genre_change: bool = False,
) -> list[dict]:
    """Drop sets/interviews, exact repeats, profile clashes and (unless steering)
    suggestions whose expected_key clashes with `current_key`. Never returns empty
    if the model gave at least one allowed song: the closest clash is kept as a
    last resort."""
    from app.ui.download_service import _is_mix, _is_non_music

    played = {h.lower() for h in history}
    # THEME LOCK (occasion): inside the occasion's world drop suggestions whose
    # own occasion_fit < MIN_THEME_FIT; while steering in, a bridge may not fit
    # worse than the playing song. Unrated suggestions are kept.
    steering_move = str(data.get("steering", "")).lower().startswith("move")
    cur_fit = _num(data.get("occasion_fit"))
    theme_floor = (cur_fit if steering_move else MIN_THEME_FIT) if occasion_set else None
    off_theme = []
    played_bare = {_bare_title(h.split(" - ", 1)[-1]) for h in history}
    cur = data.get("current_profile")
    ok, clashes, key_clashes, genre_jumps = [], [], [], []
    # "move" only licenses a genre jump inside an occasion: with no occasion the
    # model says "move" freely (it let Pal Pal -> Delilah through).
    steering_any = occasion_set and steering_move
    cur_genre = data.get("current_genre")
    for s in data.get("suggestions", []) or []:
        if not isinstance(s, dict) or not s.get("title"):
            continue
        label = f"{s.get('artist', '')} {s['title']}"
        if _is_mix(label) or _is_non_music(label):
            continue
        if _bare_title(s["title"]) in played_bare or any(s["title"].lower() in p for p in played):
            continue  # same song again, incl. a remix / feat. variant of a played one
        # Genre continuity (user: "it's changing genre a lot": Lane 8 -> Elton John
        # -> A Boogie -> The Weeknd). The prompt rule alone wasn't followed.
        hop = _num(s.get("genre_hop"))
        if _family_jump(cur_genre, s.get("genre")):
            hop = max(hop or 0.0, 2.0)
        print(f"[suggest] {label[:60]!r} genre={s.get('genre')!r} hop={hop} (cur={cur_genre!r})", flush=True)
        if hop is not None and hop > MAX_GENRE_HOP and not (steering_any or allow_genre_change):
            s["rejected_reason"] = f"genre jump ({hop:.0f})"
            genre_jumps.append((hop, s))
            continue
        fit = _num(s.get("occasion_fit"))
        if theme_floor is not None and fit is not None and fit < theme_floor:
            off_theme.append((fit, s))
            continue
        # Steering toward the occasion's music is a deliberate genre/mood move:
        # continuity clashes with the CURRENT song are expected, not errors.
        steer = str(data.get("steering", "")).lower().startswith("move")
        key_reason = None if steer else _key_clash_reason(current_key, s.get("expected_key"))
        if key_reason:
            s["rejected_reason"] = key_reason
            key_clashes.append(s)
            continue
        reason = None if steer else _profile_clash(cur, s.get("track_profile"))
        (clashes if reason else ok).append(s)
        if reason:
            s["rejected_reason"] = reason
    if ok or clashes or key_clashes:
        return ok or clashes[:1] or key_clashes[:1]
    if genre_jumps:  # only genre jumps left: the smallest one, rather than nothing
        return [min(genre_jumps, key=lambda t: t[0])[1]]
    # everything was off-theme: keep only the best-fitting one rather than nothing
    return [max(off_theme, key=lambda t: t[0])[1]] if off_theme else []


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
    if start == -1:
        raise ValueError(f"No JSON in response: {text[:200]}")
    # First COMPLETE object only: models that don't stop (gemma-4 on mlx kept
    # writing "ASSISTANT: {...}" turns) repeat the JSON, and first-{ to last-}
    # spanned several copies -> invalid.
    end = _balanced_end(text, start)
    if end is None:
        end = text.rfind("}") + 1
        if end <= start:
            raise ValueError(f"No JSON in response: {text[:200]}")
    return _loads_repaired(text[start:end])


def _balanced_end(text: str, start: int) -> int | None:
    """Index just past the } that closes the { at `start` (string-aware)."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


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


CHAT_STOPS = ["\nUSER:", "\nASSISTANT", "ASSISTANT's RULE", "<end_of_turn>"]


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
            # models without a working chat template run on into fake turns
            stop=CHAT_STOPS,
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
    earlier_sets: list[str] | None = None,
    loudness_dbfs: float | None = None,
    tempo_target: float | None = None,
    tempo_note: str = "",
    lead_to: str = "",
    lead_step: int = 0,
    lead_steps: int = 0,
    lead_bpm: float | None = None,
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
    loudness_dbfs: whole-track RMS loudness (vibe.analyze_vibe), comparable across songs.
    tempo_target / tempo_note: a bridge-ladder step; replaces the TEMPO WINDOW line.
      Tempo only: never sets occasion_set, theme lock or steering.
    Compatible with both openai v0.x/3.x (ChatCompletion.create) and v1.x/v2.x (OpenAI client).
    """
    try:
        target = float(tempo_target) if tempo_target is not None else 0.0
    except (TypeError, ValueError):
        target = 0.0
    tempo_line = (
        tempo_bridge_line(target, str(tempo_note or "").strip()) if target > 0
        else _TEMPO_WINDOW_LINE.format(tempo_window=tempo_window(bpm))
    )
    user_msg = _USER_TEMPLATE.format(
        lead_line=lead_line(lead_to, lead_step, lead_steps, lead_bpm),
        tempo_line=tempo_line,
        allowed_keys=allowed_keys(camelot),
        loudness=loudness_label(loudness_dbfs),
        title=title,
        artist=artist,
        bpm=bpm,
        camelot=camelot,
        duration=duration,
        energy=avg_energy,
        occasion=occasion or "general DJ set",
        set_mode_line=SET_MODE_LINES.get(set_mode, SET_MODE_LINES["hybrid"]),
        history=", ".join((history_display or history)[-30:]) if history else "none",
        recent_artists=_recent_artists(history_display or history),
        earlier_sets=", ".join(earlier_sets or []) or "none",
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

    system_msg = _SYSTEM
    if lead_to and lead_steps:
        # LEAD TO: a focused prompt. With the full prompt the model saw the
        # DESTINATION line but its continuity rules / house few-shot won
        # (Marea -> Anyma, Lane 8, Ben Böhmer when asked for Diljit Dosanjh).
        system_msg = _LEAD_SYSTEM
        user_msg = _LEAD_TEMPLATE.format(
            title=title, artist=artist, bpm=bpm, camelot=camelot,
            lead=lead_line(lead_to, lead_step, lead_steps, lead_bpm).strip(),
            tempo_line=tempo_line.strip(),
            history=", ".join((history_display or history)[-30:]) if history else "none",
            n=n,
        )

    prio = llm_gate.LOOKAHEAD if lookahead else llm_gate.SUGGEST
    data = None
    for attempt in range(3):  # two retries when the JSON is past repair (gemma-4 slips now and then)
        # 0.75: song picks should vary between runs (0.5 replayed the same set from
        # the same seed); the transition PLAN stays at a low temperature.
        raw = chat_raw(system_msg, user_msg, temperature=SUGGEST_TEMPERATURE if attempt == 0 else 0.4,
                       max_tokens=1100 if lead_to else 700, priority=prio)  # lead JSON is longer
        try:
            data = _extract_json(raw)
            break
        except ValueError as exc:  # JSONDecodeError is a ValueError
            if attempt >= 2:
                raise
            print(f"[suggest] bad JSON (attempt {attempt + 1}/3), retrying: {exc}", flush=True)
    if lead_to:
        data["steering"] = "move"  # the user's destination: no continuity / key filters against it
    suggestions = _filter_suggestions(
        data, history, occasion_set=bool((occasion or "").strip()) and not lead_to, current_key=camelot,
        allow_genre_change=bool(lead_to) or "NEIGHBOURING subgenre" in (occasion or ""),
    )
    # Songs from EARLIER sets are dropped whenever a fresh alternative exists:
    # the soft prompt hint alone let "Lane 8 - Little By Little" follow Fred
    # again.. in every set.
    heard = {_bare_title(str(x).split(" - ", 1)[-1]) for x in (earlier_sets or [])}
    fresh = [x for x in suggestions if _bare_title(x.get("title", "")) not in heard]
    suggestions = (fresh or suggestions)[:n]
    if meta is not None:  # caller wants the model's read of the CURRENT track too
        meta["current_profile"] = data.get("current_profile") or {}
        meta["current_genre"] = data.get("current_genre") or ""
        try:
            meta["occasion_fit"] = max(0.0, min(10.0, float(data.get("occasion_fit"))))
        except (TypeError, ValueError):
            meta["occasion_fit"] = None
        # "move" only counts when the playing song does NOT fit the occasion: the
        # model also said "move" for Diljit's "Lover" at a Punjabi wedding
        # (meaning more energy, not another genre).
        wants_move = str(data.get("steering", "")).lower().startswith("move")
        fit = meta["occasion_fit"]
        meta["steering"] = "move" if wants_move and (fit is None or fit < 6) else "stay"

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
        s["expected_bpm"] = _bpm_or_none(s.get("expected_bpm"))

    return suggestions


def _bpm_or_none(v) -> float | None:
    """expected_bpm as a float: small models send 124, "124", "124 BPM" or "~124"."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    m = re.search(r"\d+(?:\.\d+)?", str(v or ""))
    return float(m.group()) if m and float(m.group()) > 0 else None
