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
import time
from concurrent.futures import ThreadPoolExecutor, wait

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

# ── Few-shot example ──────────────────────────────────────────────────────────
# Shape only, with placeholder names. Real songs here were copied back verbatim
# (gemma-4: Marea / Delilah for any seed) and pulled every set toward the
# examples' artists (Fred again.. bias); a placeholder can't be played.
_FEW_SHOT = """\
EXAMPLE (output shape only - placeholder names, never suggest these):
Current: "Song Zero" by Artist Zero | 124 BPM | 8A | Energy 0.6 | Genre: melodic house
{"steering":"stay","occasion_fit":0,"current_genre":"melodic house","current_era":"2020s","current_profile":{"energy":6,"tempo_feel":"driving","mood":"bittersweet"},"suggestions":[
  {"artist":"Artist One","title":"Song One","reason":"same chopped vocal over rolling bass","genre":"melodic house","era":"2020s","expected_bpm":122,"expected_key":"9A","energy_delta":"maintain","genre_hop":0,"occasion_fit":0,"track_profile":{"energy":6,"tempo_feel":"driving","mood":"bittersweet"}}
]}"""

_SYSTEM = f"""\
You are a professional DJ selector with deep music theory knowledge. Your job is to suggest the next tracks for a live DJ set.

{_CAMELOT_COMPAT}

ENERGY ARC RULES:
  set_position 0.0–0.3 (early/warm-up): prefer maintaining or gently lifting energy, stay within 5 BPM.
    EXCEPTION: a mashup/layering move (riff_over_rap, full_mashup - see techniques.py) is fine from
    the very first transition when the pair actually earns it (stems on both, a real breakdown to
    release into, a loopable groove) - it is a technique choice, not an energy-arc violation, so
    don't suppress a good mashup candidate just because set_position is low. A wide tempo/key gap
    is normal for these techniques (riff_over_rap wants 3-15% and tolerates any key clash under rap
    vocals); don't reject it as "too early" if the transition itself checks out.
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
  "reason" MUST name the specific sonic characteristic both songs share, NOT a genre label:
    Good: "same granular vocal chops, melodic drops"
    Good: "punchy UK bass kick, pitched vocal chops"
    Bad: "same genre", "similar style", "UK bass DNA"

ERA CONTINUITY (as important as genre):
  Also infer the current song's era (the decade it was released, e.g. "1990s") and give every
  suggestion its own release "era". A set holds its era the way it holds its genre: stay within
  ONE decade of the current song (1990s -> 1990s or 2000s is fine; 1990s -> 2010s is a jump).
  Eurodance/90s pop (Aqua "Barbie Girl") continues with other 90s/early-2000s dance-pop, NOT a 2010s
  UK breakbeat track, even at the same tempo. A remix counts at the remix's own release year.
  To change era, bridge through a modern remix or rework of an older song.

TRACK PROFILE (judge the SONG, not the artist):
  Artist reputation is NOT enough. The same artist has chill tracks and peak tracks.
  First describe the CURRENT track in "current_profile", then give every suggestion its own "track_profile":
    energy: integer 1-10 (how hard THIS song hits on a dancefloor; the MEASURED energy above is the truth for the current song)
    tempo_feel: driving | mid | laid-back
    mood: euphoric | bittersweet | dark | chill
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

AVOID TRACKS: The history list contains track names already played. Do NOT suggest any track whose
  title appears in that list. Same artist is fine — only the exact title is banned.

REMIXES: remixes, edits and reworks are welcome when they keep the vibe (a club remix of a
  vocal song is often the better fit on a dancefloor). Give the remixer in the title, e.g.
  "Treat You Better (Purple Disco Machine Remix)". Never the same version of a song twice.

FACTS (critical):
  Suggest only songs that really exist, credited to their real artist. Every pick is
  checked against YouTube; invented titles are thrown away.
  expected_bpm and expected_key are THAT song's own tempo and key as released. NEVER copy
  the current song's BPM or key into a suggestion; if you don't know a song's tempo, skip it.
  reason: at most 8 words (key and BPM are shown beside it: do not repeat them).

OUTPUT FORMAT — return ONLY valid JSON, no markdown, no explanation:
{{"steering":"stay|move","occasion_fit":0,"current_genre":"","current_era":"release decade e.g. 1990s","current_profile":{{"energy":0,"tempo_feel":"","mood":""}},"suggestions":[{{"artist":"","title":"","reason":"the shared sound, max 8 words","genre":"inferred genre of suggested track","era":"its release decade","expected_bpm":0,"expected_key":"","energy_delta":"up|down|maintain","genre_hop":0,"occasion_fit":0,"track_profile":{{"energy":0,"tempo_feel":"","mood":""}}}}]}}

{_FEW_SHOT}"""

REMIX_REPLAY_GAP = 8   # songs between a song and a remix of it
_VERSION_WORDS = re.compile(r"\b(remix|re-?edit|edit|rework|bootleg|vip|flip|refix|dub|mix)\b", re.IGNORECASE)

TEMPO_LOCK_PCT = 0.06  # the autopilot pitch-locks the next song within +/-8%; aim inside 6%


MIN_THEME_FIT = 6.0
SUGGEST_TEMPERATURE = 0.75

# A good DJ picks the next record in 10-15 s. One call on a 3B-active MoE takes
# ~7 s, so a corrective retry (genre / tempo / invented song) is only made while
# it still finishes inside the budget. Bad-JSON retries always run: no JSON, no pick.
SUGGEST_BUDGET_S = float(os.environ.get("SUGGEST_BUDGET_S", "15"))
RETRY_COST_S = 7.0
# ask for n+2: ~1 in 2 local-model picks is invented or off-tempo. Kept at 2 in the
# output diet: the server log still shows calls losing 3 of 5 picks as invented.
EXTRA_CANDIDATES = 2
VERIFY_TIMEOUT_S = 4.0  # parallel YouTube lookups (~1.5 s each); slower = unknown, kept
VERIFY_SONGS = os.environ.get("SUGGEST_VERIFY", "1") != "0"


def _verify_song(artist: str, title: str):
    from app.ui.download_service import verify_song
    return verify_song(artist, title)


def _verify_picks(picks: list[dict]) -> tuple[list[dict], list[dict]]:
    """(real, invented). A pick whose lookup fails or runs past VERIFY_TIMEOUT_S
    counts as real: a slow network must not empty the queue."""
    if not VERIFY_SONGS or not picks:
        return list(picks), []
    pool = ThreadPoolExecutor(max_workers=min(6, len(picks)))
    futs = [pool.submit(_verify_song, p.get("artist", ""), p.get("title", "")) for p in picks]
    wait(futs, timeout=VERIFY_TIMEOUT_S)
    pool.shutdown(wait=False, cancel_futures=True)
    real, fake = [], []
    for p, f in zip(picks, futs):
        ok = f.result() if f.done() and not f.cancelled() and f.exception() is None else None
        (fake if ok is False else real).append(p)
    return real, fake


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
    "Around this song (never again): {history}\n\n"
    "Think of the path: what connects the current song's world to the destination's world "
    "(shared producers, crossover collabs, fusion remixes, similar rhythm), then give {n} songs "
    "for THIS step.\n"
    'JSON: {{"steering":"move","occasion_fit":0,"current_genre":"","current_profile":{{"energy":0,'
    '"tempo_feel":"","mood":""}},"suggestions":[{{"artist":"",'
    '"title":"","reason":"why it is this step, max 8 words","genre":"","expected_bpm":0,'
    '"expected_key":"","energy_delta":"up|down|maintain",'
    '"occasion_fit":0,"track_profile":{{"energy":0,"tempo_feel":"","mood":""}}}}]}}'
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
    "{energy_line} | Loudness: {loudness} | "
    "Set position: {set_pos_pct}% through set\n"
    "{lead_line}"
    "Occasion: {occasion}\n"
    "Set mode: {set_mode_line}\n"
    "{tempo_line}"
    "ALLOWED KEYS (expected_key must be one of these unless steering): {allowed_keys}\n"
    "First fill current_genre, current_era and current_profile for THIS song, then pick songs whose own "
    "track_profile stays close to it. Stay in this genre neighbourhood AND within one decade of this "
    "song's era unless the occasion demands a shift.\n"
    "Around this song (never suggest any of these; nothing already played this set either): {history}\n"
    "Artists heard in the last few songs (pick someone else unless it is a deliberate "
    "same-artist moment early in the set, or one of the listener's favourite artists below): {recent_artists}\n"
    "The listener's FAVOURITE artists (they come back to them set after set): {favourite_artists}. "
    "Their songs are WELCOME whenever they fit the vibe - other tracks, remixes, edits, collaborations - "
    "never avoid them for having been heard before; just don't repeat this set's history.\n"
    "Played in the listener's EARLIER sets - they have heard these recently, so prefer fresh "
    "songs over them (only reuse one if it is clearly the perfect fit; favourite artists' songs "
    "are exempt): {earlier_sets}\n"
    "At least ONE of your suggestions must be a less obvious pick (a deep cut, a newer release "
    "or a lesser-played gem) FROM THE SAME GENRE as the current song, not the genre's most famous anthem.\n"
    "genre_hop for each suggestion: 0 = same subgenre, 1 = neighbouring subgenre (e.g. melodic house -> "
    "progressive house), 2 = a different genre, 3 = unrelated (e.g. melodic house -> soft rock). "
    "Every suggestion must be genre_hop 0 or 1. Genre TRANSITIONS, it never jumps: to change genre, "
    "move ONE step per song through a crossover song that belongs to both worlds (e.g. Urdu pop -> "
    "Punjabi pop -> Punjabi hip-hop -> hip-hop; melodic house -> organic house -> afro house -> afrobeats), "
    "so each song shares its genre with the one before it. This holds even when heading to a DESTINATION.\n\n"
    "Suggest {n} tracks. Prioritise: vibe continuity → harmonic compatibility → energy arc for {arc_phase} → diversity.\n"
    "Reply ONLY with the JSON object, compact (no line breaks or indentation), only the fields "
    "shown. Keep every reason under 8 words."
)
_KNOWLEDGE_TEMPLATE = "\n\nDJ KNOWLEDGE (from the ./DJ wiki):\n{brief}"


# Relaxed session (user: "in relax sessions it should not go upbeat, maintain
# relaxed session"): the arc never builds, and a pick that lifts the energy is
# dropped even if the model offers it.
RELAXED_ARC = "a RELAXED session (hold the calm the whole way: no build, no peak)"
RELAXED_LINE = (
    "\n\nRELAXED SESSION (overrides the ENERGY ARC and SET MODE energy rules): the listener wants "
    "to stay relaxed from start to end. energy_delta must be \"maintain\" or \"down\", never \"up\"; "
    "every track_profile.energy must be at or below the current song's energy; tempo_feel laid-back "
    "or mid, never driving; mood chill or bittersweet, never euphoric; no drops, club bangers, "
    "festival anthems or peak-time remixes. A slightly slower song is fine."
)


def _relaxed_only(suggestions: list, cur_profile) -> list:
    """Drop picks that would lift a relaxed set. Never empty: if every pick lifts,
    keep the calmest one."""
    cur = cur_profile if isinstance(cur_profile, dict) else {}
    cur_e = _num(cur.get("energy"))
    cap = cur_e if cur_e is not None else 5.0
    cur_drive = str(cur.get("tempo_feel", "")).lower() == "driving"

    def lifts(s):
        tp = s.get("track_profile") if isinstance(s.get("track_profile"), dict) else {}
        e = _num(tp.get("energy"))
        return (str(s.get("energy_delta", "")).lower() == "up"
                or (e is not None and e > cap)
                or (str(tp.get("tempo_feel", "")).lower() == "driving" and not cur_drive)
                or str(tp.get("mood", "")).lower() == "euphoric")

    calm = [s for s in suggestions if not lifts(s)]
    if calm or not suggestions:
        return calm
    def energy_of(s):
        tp = s.get("track_profile") if isinstance(s.get("track_profile"), dict) else {}
        e = _num(tp.get("energy"))
        return e if e is not None else 10.0
    keep = min(suggestions, key=energy_of)
    keep["rejected_reason"] = "relaxed: the calmest of picks that all lift the energy"
    return [keep]


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

# Genre-family distance is shared with RecipeMatcher (app/music_brain/genre.py)
# so a manual /api/match call gets the same "no unrelated-genre jump" sense.
# The model's own genre_hop is not trusted alone: it rated Afusic "Pal Pal"
# (Urdu pop) -> Fred again.. "Delilah" (UK electronic) as a small hop.
from app.music_brain.genre import GENRE_FAMILIES  # noqa: E402


def _tempo_locks(target: float, bpm) -> bool | None:
    """True when `bpm` beat-matches `target` (straight, double or half time,
    within TEMPO_LOCK_PCT + 2% slack). None when either tempo is unknown."""
    b = _num_bpm(bpm)
    if not target or target <= 0 or b is None:
        return None
    return any(abs(target / (b * m) - 1) <= TEMPO_LOCK_PCT + 0.02 for m in (1, 2, 0.5))


def _num_bpm(v) -> float | None:
    try:
        b = float(v)
    except (TypeError, ValueError):
        return None
    return b if b > 0 else None


def _few_shot_titles() -> set:
    """Bare titles of the few-shot ANSWERS. gemma-4 sometimes returns an example
    verbatim (Cigarettes After Sex -> "Marea"/"Delilah", current_genre copied too)."""
    return {_bare_title(t) for t in re.findall(r'"title":"([^"]+)"', _FEW_SHOT)}


def _parroted(data: dict, title: str) -> bool:
    """True when every suggestion is a few-shot answer and the current song isn't an example."""
    if f'"{title}"' in _FEW_SHOT:
        return False
    sugg = [x for x in data.get("suggestions", []) or [] if isinstance(x, dict) and x.get("title")]
    ex = _few_shot_titles()
    return bool(sugg) and all(_bare_title(x["title"]) in ex for x in sugg)


from app.music_brain.genre import genre_families as _genre_families  # noqa: E402
from app.music_brain.genre import family_jump as _family_jump  # noqa: E402
from app.music_brain.genre import MAX_ERA_GAP, era_gap  # noqa: E402


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
    # by the song, not the upload: "RÜFÜS DU SOL ●● Treat You Better (Official
    # Single Edit Video)" and "... (Purple Disco Machine Remix)" are the same song
    from app.ui.track_identity import clean_identity
    def _song(h):
        return {_bare_title(h.split(" - ", 1)[-1]), _bare_title(clean_identity(h)[1])}
    played_bare = set().union(*[_song(h) for h in history]) if history else set()
    # Remixes are welcome (user) when they match the vibe; a remix of a song
    # already played only after REMIX_REPLAY_GAP other songs.
    recent_bare = set().union(*[_song(h) for h in history[-REMIX_REPLAY_GAP:]]) if history else set()
    played_versions = {" ".join(str(h).lower().split()) for h in history}
    cur = data.get("current_profile")
    ok, clashes, key_clashes, genre_jumps = [], [], [], []
    # "move" only licenses a genre jump inside an occasion: with no occasion the
    # model says "move" freely (it let Pal Pal -> Delilah through).
    steering_any = occasion_set and steering_move
    cur_genre = data.get("current_genre")
    cur_era = data.get("current_era")
    for s in data.get("suggestions", []) or []:
        if not isinstance(s, dict) or not s.get("title"):
            continue
        label = f"{s.get('artist', '')} {s['title']}"
        if _is_mix(label) or _is_non_music(label):
            continue
        bare = _bare_title(s["title"])
        is_version = bool(_VERSION_WORDS.search(s["title"]))
        if bare in played_bare:
            same_version = any(s["title"].lower() in p for p in played_versions) or not is_version
            if same_version or bare in recent_bare:
                continue  # the same song again, or a remix too soon after its original
        elif any(s["title"].lower() in p for p in played):
            continue
        # Genre continuity (user: "it's changing genre a lot": Lane 8 -> Elton John
        # -> A Boogie -> The Weeknd). The prompt rule alone wasn't followed.
        hop = _num(s.get("genre_hop"))
        if _family_jump(cur_genre, s.get("genre")):
            hop = max(hop or 0.0, 2.0)
        # Era continuity (user: Aqua "Barbie Girl" -> Bicep "Glue" broke the
        # vibe). Two decades apart counts as a jump of that size, in the same
        # bucket as genre jumps, so the closest one survives if nothing else does.
        egap = era_gap(cur_era, s.get("era"))
        era_jump = egap is not None and egap > MAX_ERA_GAP
        print(f"[suggest] {label[:60]!r} genre={s.get('genre')!r} hop={hop} (cur={cur_genre!r})"
              f" era={s.get('era')!r} (cur={cur_era!r})", flush=True)
        if not (steering_any or allow_genre_change):
            if era_jump:
                s["rejected_reason"] = f"era jump ({s.get('era')} after {cur_era})"
                genre_jumps.append((max(hop or 0.0, float(egap)), s))
                continue
            if hop is not None and hop > MAX_GENRE_HOP:
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
    # Qwen3 emits a (possibly empty) <think>...</think> block before the answer
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
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
    a time, PLAN before EAR before SUGGEST before LOOKAHEAD, and none starts
    mid-phrase while the live ear holds the shared model (bounded for SUGGEST;
    see the gate's docstring). A plan's `timeout` covers its wait in the queue
    plus the call itself.
    """
    from app.ui import session_log

    wait = timeout if timeout is not None else GATE_WAIT_S
    waited, t0 = None, None
    try:
        with llm_gate.gate.slot(priority, wait_timeout=wait) as waited:
            left = None if timeout is None else max(5.0, timeout - waited)
            t0 = time.monotonic()
            raw = _chat_call(system, user, temperature, left, model, max_tokens)
    except Exception as exc:
        session_log.log("llm", priority=llm_gate.NAMES.get(priority), max_tokens=max_tokens, ok=False,
                        waited=round(waited, 2) if waited is not None else None,
                        elapsed=round(time.monotonic() - t0, 2) if t0 else None,
                        prompt_chars=len(system) + len(user), error=f"{type(exc).__name__}: {exc}")
        raise
    session_log.log("llm", priority=llm_gate.NAMES.get(priority), max_tokens=max_tokens, ok=True,
                    waited=round(waited, 2), elapsed=round(time.monotonic() - t0, 2),
                    prompt_chars=len(system) + len(user), reply_chars=len(raw or ""), cut_off=cut_off(raw))
    return raw


CHAT_STOPS = ["\nUSER:", "\nASSISTANT", "ASSISTANT's RULE", "<end_of_turn>"]


MAX_SUGGEST_TOKENS = 3000
# Tokens recent suggest replies needed (grows / shrinks with them). Measured on
# Qwen3-Omni (omni server log, 178 suggest calls): the old 13-field picks ran ~150
# tokens each, 164 of 178 replies hit max_tokens=900 and were redone at ~1800. The
# slim schema (_FEW_SHOT: 8-word reason, no vibe_link / mix_moment, 3-field
# profiles) writes ~97 tokens a pick, ~540 for n+2 = 5, so 900 is now a ceiling
# with room rather than a cut.
_suggest_need = 900


def _learn_need(raw: str) -> None:
    """Remember what a complete reply needed: ~3.3 chars per token, +25 % headroom."""
    global _suggest_need
    est = int(len(raw or "") / 3.3 * 1.25)
    _suggest_need = max(900, min(MAX_SUGGEST_TOKENS, est))


def cut_off(raw: str) -> bool:
    """True when a reply opens a JSON object that never closes: the model hit max_tokens."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    start = text.find("{")
    return start != -1 and _balanced_end(text, start) is None


CONTEXT_PLAYED = int(os.environ.get("AUTOPILOT_CONTEXT_PLAYED", "3") or 3)
CONTEXT_QUEUED = int(os.environ.get("AUTOPILOT_CONTEXT_QUEUED", "3") or 3)


def prompt_history(played: list | None, queued: list | None = None, skip: list | None = None,
                   back: int = CONTEXT_PLAYED, ahead: int = CONTEXT_QUEUED) -> str:
    """The songs the model is shown: a rolling window of the last `back` played and
    the next `ahead` queued (default 3 + 3). A long list made the prompt ~4.5k tokens
    and the picks drift; the repeat filters (_filter_suggestions) still see the whole set."""
    played = [x for x in (played or []) if x][-back:] if back > 0 else []
    played_set = {x.lower() for x in played}
    queued = [x for x in (queued or []) if x and x.lower() not in played_set][:ahead] if ahead > 0 else []
    parts = []
    if played:
        parts.append("last played: " + ", ".join(played))
    if queued:
        parts.append("queued next: " + ", ".join(queued))
    shown = played_set | {x.lower() for x in queued}
    skip = [x for x in (skip or []) if x and x.lower() not in shown][-6:]
    if skip:                          # picks just rejected (download failed, off tempo): not again
        parts.append("rejected: " + ", ".join(skip))
    return " | ".join(parts) if parts else "none"


def energy_line(avg: float, measured: int | None, relaxed: bool = False) -> str:
    """The prompt's energy fact: the measured 1-10 level (app.music_brain.energy) with the
    range the next song must stay in, else the old per-song relative number."""
    if measured is None:
        return f"Avg Energy: {avg:.2f}/1.0 (relative to this song's own peak)"
    step = 1 if relaxed else 2
    lo, hi = max(1, measured - step), min(10, measured + step)
    return (f"MEASURED ENERGY: {measured}/10 (vs the library) - every suggestion's track_profile energy "
            f"MUST be {lo}-{hi}: no swings between low and high energy songs")


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
    if "qwen3" in model.lower():
        user += "\n/no_think"  # Qwen3 soft switch: reasoning would eat max_tokens
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
    relaxed: bool = False,
    meta: dict | None = None,
    genre: str = "",
    history_display: list[str] | None = None,
    queue_display: list[str] | None = None,
    avoid_display: list[str] | None = None,
    lookahead: bool = False,
    earlier_sets: list[str] | None = None,
    favourite_artists: list[str] | None = None,
    loudness_dbfs: float | None = None,
    tempo_target: float | None = None,
    tempo_note: str = "",
    lead_to: str = "",
    lead_step: int = 0,
    lead_steps: int = 0,
    lead_bpm: float | None = None,
    measured_energy: int | None = None,
) -> list[dict]:
    """
    Call local Ollama (gemma3:4b) to suggest next n tracks.
    Returns list of dicts with keys:
      artist, title, reason, genre, era, expected_bpm, expected_key,
      energy_delta, track_profile, search_query (+ mix_moment / vibe_link, now
      always "": the model no longer writes display-only fields).

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
        energy_line=energy_line(avg_energy, measured_energy, relaxed),
        occasion=occasion or "general DJ set",
        set_mode_line=SET_MODE_LINES.get(set_mode, SET_MODE_LINES["hybrid"]),
        history=prompt_history(history_display or history, queue_display, avoid_display),
        recent_artists=_recent_artists(history_display or history),
        earlier_sets=", ".join(earlier_sets or []) or "none",
        favourite_artists=", ".join(favourite_artists or []) or "none",
        set_pos_pct=round(set_position * 100),
        arc_phase=RELAXED_ARC if relaxed else _set_arc_phase(set_position),
        n=n + EXTRA_CANDIDATES,  # spares: invented / off-tempo picks are dropped below
    )

    try:
        from app.music_brain.dj_knowledge import selection_brief
        brief = selection_brief(genre or "")
    except Exception:  # grounding is best-effort
        brief = ""
    if brief:
        user_msg += _KNOWLEDGE_TEMPLATE.format(brief=brief)
    if relaxed:
        user_msg += RELAXED_LINE

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
            history=prompt_history(history_display or history, queue_display, avoid_display),
            n=n,
        )

    prio = llm_gate.LOOKAHEAD if lookahead else llm_gate.SUGGEST
    t_start = time.monotonic()

    def can_retry() -> bool:
        """A corrective retry only while it still lands inside the decision budget."""
        return time.monotonic() - t_start < SUGGEST_BUDGET_S - RETRY_COST_S

    data = None
    # Budget: what recent replies needed (a first call cut at 900 then redone at 1800
    # cost ~2x the model time, and starved the live ear that shares the model).
    mt = max(1100 if lead_to else 900, _suggest_need)
    for attempt in range(3):  # two retries when the JSON is past repair (gemma-4 slips now and then)
        # 0.75: song picks should vary between runs (0.5 replayed the same set from
        # the same seed); the transition PLAN stays at a low temperature.
        raw = chat_raw(system_msg, user_msg, temperature=SUGGEST_TEMPERATURE if attempt == 0 else 0.4,
                       max_tokens=mt, priority=prio)
        try:
            data = _extract_json(raw)
            if _parroted(data, title):
                raise ValueError("copied the few-shot example answers")
            _learn_need(raw)
            break
        except ValueError as exc:  # JSONDecodeError is a ValueError
            if attempt >= 2:
                print(f"ERROR [suggest] no usable reply after 3 attempts: {exc}", flush=True)
                raise
            # (the retry below also teaches the next call its budget)
            # Cut off by max_tokens (Qwen3-Omni writes longer reasons): the same limit
            # would cut the retry at the same place, so give it room instead.
            cut = cut_off(raw)
            if cut:
                mt = min(MAX_SUGGEST_TOKENS, mt * 2)
            print(f"[suggest] bad JSON (attempt {attempt + 1}/3), retrying"
                  f"{f' with max_tokens={mt} (reply was cut off)' if cut else ''}: {exc}", flush=True)
    if lead_to:
        data["steering"] = "move"  # the user's destination: no continuity / key filters against it
    suggestions = _filter_suggestions(
        data, history, occasion_set=bool((occasion or "").strip()) and not lead_to, current_key=camelot,
        allow_genre_change=bool(lead_to),
    )
    # Genre transitions, it never jumps (user: Pal Pal -> Delilah). When every pick
    # jumped, ask once more with the rejected picks named, rather than play a jump.
    if (suggestions and str(suggestions[0].get("rejected_reason", "")).startswith(("genre jump", "era jump"))
            and can_retry()):
        cur_genre = data.get("current_genre") or "the current song's genre"
        jumped = "; ".join(f"{x.get('artist', '')} - {x.get('title', '')} ({x.get('genre', '')})"
                           for x in data.get("suggestions", []) if isinstance(x, dict))[:400]
        cur_era = data.get("current_era") or "the current song's era"
        retry_msg = (user_msg + f"\n\nREJECTED - these jumped genre or era away from {cur_genre} "
                     f"({cur_era}): {jumped}. Suggest songs IN {cur_genre} from within one decade of "
                     f"{cur_era}, or a crossover song one step away that still belongs to it.")
        try:
            data2 = _extract_json(chat_raw(system_msg, retry_msg, temperature=0.4,
                                           max_tokens=mt, priority=prio))
            data2.setdefault("current_genre", data.get("current_genre"))
            data2.setdefault("current_era", data.get("current_era"))
            retry = _filter_suggestions(
                data2, history, occasion_set=bool((occasion or "").strip()) and not lead_to,
                current_key=camelot, allow_genre_change=bool(lead_to))
            if retry and not str(retry[0].get("rejected_reason", "")).startswith(("genre jump", "era jump")):
                data, suggestions = data2, retry
        except ValueError as exc:
            print(f"[suggest] genre retry failed: {exc}", flush=True)
    # Tempo holds unless the set is deliberately moving (user: Cigarettes After Sex
    # "Apocalypse" at 96 BPM -> Bonobo "Cirrus" at 117). Every pick sat outside the
    # TEMPO WINDOW, and the client then laddered the set up toward it.
    moving = bool(lead_to) or (bool((occasion or "").strip())
                               and str(data.get("steering", "")).lower().startswith("move"))
    target = tempo_target or bpm
    if suggestions and not moving:
        locked = [x for x in suggestions if _tempo_locks(target, x.get("expected_bpm")) is not False]
        if not locked and not can_retry():
            pass  # no time to ask again: keep them, the client ladders toward the tempo
        elif not locked:
            far = "; ".join(f"{x.get('artist', '')} - {x.get('title', '')} ({x.get('expected_bpm')} BPM)"
                            for x in suggestions)[:400]
            print(f"[suggest] tempo: all picks off {target:.0f} BPM: {far}", flush=True)
            retry_msg = (user_msg + f"\n\nREJECTED - wrong tempo for {target:.0f} BPM: {far}. "
                         f"Every song MUST be inside the TEMPO WINDOW ({tempo_window(target)}). "
                         "Keep the same mood, vocals and energy as the current song.")
            try:
                data2 = _extract_json(chat_raw(system_msg, retry_msg, temperature=0.4,
                                               max_tokens=mt, priority=prio))
                data2.setdefault("current_genre", data.get("current_genre"))
                data2.setdefault("current_era", data.get("current_era"))
                retry = _filter_suggestions(
                    data2, history, occasion_set=bool((occasion or "").strip()) and not lead_to,
                    current_key=camelot, allow_genre_change=bool(lead_to))
                retry = [x for x in retry if _tempo_locks(target, x.get("expected_bpm")) is not False]
                if retry:
                    data, suggestions = data2, retry
            except ValueError as exc:
                print(f"[suggest] tempo retry failed: {exc}", flush=True)
        else:
            suggestions = locked
    # The playing song is never its own next (gemma-4: Vroom Vroom -> Vroom Vroom).
    seed = _bare_title(title)
    suggestions = [x for x in suggestions if _bare_title(x.get("title", "")) != seed]
    # Real songs only: a local model invents plausible titles ("Four Tet - Morrison").
    # Those used to fail at download, one full prepare round later.
    real, fake = _verify_picks(suggestions)
    # Nothing real left: one retry even past the budget, since an empty answer
    # costs the client a whole prepare round (the hold-loop path).
    if fake and not real:
        names = "; ".join(f"{x.get('artist', '')} - {x.get('title', '')}" for x in fake)[:400]
        print(f"[suggest] not real songs: {names}", flush=True)
        retry_msg = (user_msg + f"\n\nREJECTED - these songs do not exist: {names}. "
                     "Suggest real released songs you are sure of, credited to their real artist.")
        try:
            data2 = _extract_json(chat_raw(system_msg, retry_msg, temperature=0.4,
                                           max_tokens=mt, priority=prio))
            data2.setdefault("current_genre", data.get("current_genre"))
            data2.setdefault("current_era", data.get("current_era"))
            retry = _filter_suggestions(
                data2, history, occasion_set=bool((occasion or "").strip()) and not lead_to,
                current_key=camelot, allow_genre_change=bool(lead_to))
            if not moving:
                retry = [x for x in retry if _tempo_locks(target, x.get("expected_bpm")) is not False]
            retry = [x for x in retry if _bare_title(x.get("title", "")) != seed]
            real, _ = _verify_picks(retry)
        except ValueError as exc:
            print(f"[suggest] real-song retry failed: {exc}", flush=True)
    elif fake:
        print(f"[suggest] dropped {len(fake)} invented song(s): "
              + "; ".join(f"{x.get('artist', '')} - {x.get('title', '')}" for x in fake)[:300], flush=True)
    suggestions = real
    # Songs from EARLIER sets are dropped whenever a fresh alternative exists:
    # the soft prompt hint alone let "Lane 8 - Little By Little" follow Fred
    # again.. in every set.
    # Favourite artists are exempt (user: "biased against Fred again.. songs"):
    # a listener who plays an artist in every set wants more of them, and
    # dropping every remembered title of theirs left only other artists.
    from app.ui.set_memory import artist_key
    from app.ui.track_identity import credited_artists
    fav = {artist_key(a) for a in (favourite_artists or [])}
    def _is_fav(x):
        names = [x.get("artist", "")] + credited_artists(f"{x.get('artist', '')} - {x.get('title', '')}")
        return bool(fav) and any(artist_key(n) in fav for n in names if n)
    if relaxed:
        suggestions = _relaxed_only(suggestions, data.get("current_profile"))
    heard = {_bare_title(str(x).split(" - ", 1)[-1]) for x in (earlier_sets or [])}
    fresh = [x for x in suggestions if _is_fav(x) or _bare_title(x.get("title", "")) not in heard]
    suggestions = (fresh or suggestions)[:n]
    if meta is not None:  # caller wants the model's read of the CURRENT track too
        meta["current_profile"] = data.get("current_profile") or {}
        meta["current_genre"] = data.get("current_genre") or ""
        meta["current_era"] = data.get("current_era") or ""
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
