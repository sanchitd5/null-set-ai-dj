"""
Gemini Client Module for AI DJ Mixing System
============================================
Provides multi-key rotation and robust Gemini 3.6 Flash integration for:
1. Track selection and setlist ordering (Stage 1)
2. Song genre, key, and scale lookup (Stage 2)
3. Chorus timestamps, vocals, and transition point detection (Stage 3)
"""

import os
import json
from dotenv import load_dotenv
from google import genai
from google.genai import errors

load_dotenv()

# Gather all available Gemini API keys
_RAW_KEYS = [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
]
API_KEYS = [k.strip() for k in _RAW_KEYS if k and k.strip()]
_current_key_idx = 0

MODEL_NAME = "gemini-3.6-flash"


def get_active_client():
    """Returns a Google GenAI Client with automatic key rotation."""
    global _current_key_idx
    if not API_KEYS:
        return None
    key = API_KEYS[_current_key_idx % len(API_KEYS)]
    return genai.Client(api_key=key)


def rotate_key():
    """Rotates to next available API key in case of quota or rate limits."""
    global _current_key_idx
    if len(API_KEYS) > 1:
        _current_key_idx = (_current_key_idx + 1) % len(API_KEYS)
        print(f"  [KEY ROTATION] Switched to Gemini API Key #{_current_key_idx + 1}")


def call_gemini_with_retry(prompt, contents=None, max_retries=3):
    """Executes a generate_content call on Gemini 3.6 Flash with key rotation on error."""
    global _current_key_idx
    if not API_KEYS:
        return None

    for attempt in range(max_retries * len(API_KEYS)):
        client = get_active_client()
        try:
            input_contents = contents if contents is not None else prompt
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=input_contents,
            )
            return response.text.strip()
        except errors.ClientError as e:
            err_msg = str(e)
            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower():
                print(f"  [GEMINI RATE LIMIT] Quota hit on key #{_current_key_idx + 1}. Rotating key...")
                rotate_key()
            else:
                print(f"  [GEMINI CLIENT ERROR] {e}")
                rotate_key()
        except Exception as e:
            print(f"  [GEMINI ERROR] {e}")
            rotate_key()
    return None


def clean_json_markdown(text: str) -> str:
    """Strip markdown code blocks and whitespace from JSON response."""
    if not text:
        return "{}"
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("\n```", 1)[0] if "```" in t else t
        t = t.replace("```json", "").replace("```", "").strip()
    return t


def select_and_order_songs_with_gemini(available_songs, user_input):
    """
    Stage 1: Let Gemini 3.6 Flash parse user request, select appropriate songs,
    and order them with a DJ energy arc progression.
    """
    if not API_KEYS or not available_songs:
        print("  [WARN] Gemini API keys not available or library empty. Using default order.")
        return available_songs[:10]

    songs_list = []
    for s in available_songs:
        songs_list.append(f'"{s["title"]}" by {s["artist"]} (file: {s["file"]})')
    available_songs_str = "\n".join(songs_list)

    prompt = f"""You are a world-class professional DJ. The user wants a custom DJ mix and submitted this request:

"{user_input}"

Available songs in the local music library:
{available_songs_str}

Your task:
1. Parse the user's request (song count, artists, moods, genres, or "mix all songs").
2. Select the matching songs from the library.
3. Order the songs in a professional DJ set sequence with great musical flow and energy progression (warm-up -> peak energy -> outro).
4. If the user says "all songs" or does not specify a limit, include all available songs.
5. Return strictly a JSON object with this format:
{{
  "selected_songs": [
    {{
      "title": "exact title from library",
      "artist": "exact artist from library",
      "file": "exact filename from library"
    }}
  ]
}}

IMPORTANT:
- Return ONLY valid JSON, no markdown outside JSON blocks.
- Use EXACT filenames from the library.
- Order represents the mix sequence.
"""

    resp_text = call_gemini_with_retry(prompt)
    if resp_text:
        try:
            cleaned = clean_json_markdown(resp_text)
            parsed = json.loads(cleaned)
            selected = parsed.get("selected_songs", [])
            if selected:
                print(f"  [GEMINI] Successfully selected and sequenced {len(selected)} songs in DJ flow.")
                return selected
        except Exception as e:
            print(f"  [GEMINI] JSON parse error: {e}. Raw: {resp_text[:100]}...")

    print("  [GEMINI] Falling back to all available songs.")
    return available_songs


def get_metadata_with_gemini(title, artist):
    """
    Stage 2: Use Gemini 3.6 Flash to identify genre, key, and scale.
    """
    prompt = f"""You are a professional music theorist and DJ music cataloger.
Song: "{title}" by {artist}

Identify the primary genre and musical key of this track.
Return strictly a JSON object:
{{
  "genre": "primary genre (e.g. Afrobeats, R&B, EDM, Hip-Hop, Pop, House)",
  "key": "musical root note (e.g. C, C#, D, D#, E, F, F#, G, G#, A, A#, B)",
  "scale": "major or minor"
}}
"""
    resp_text = call_gemini_with_retry(prompt)
    genre, key, scale = "Unknown", "C", "major"
    if resp_text:
        try:
            data = json.loads(clean_json_markdown(resp_text))
            genre = data.get("genre", "Unknown")
            key = data.get("key", "C")
            scale = data.get("scale", "major")
            if scale not in ["major", "minor"]:
                scale = "major"
        except Exception:
            pass
    return genre, key, scale


def detect_chorus_with_gemini(audio_path, title, artist, duration=180.0):
    """
    Stage 3: Multi-modal or structural chorus detection with Gemini 3.6 Flash.
    Finds the first chorus start/end timestamps and whether vocals exist early.
    """
    client = get_active_client()
    
    # Try audio upload first for direct listening if file is < 15MB
    if client and os.path.exists(audio_path) and os.path.getsize(audio_path) < 15 * 1024 * 1024:
        try:
            print(f"  [GEMINI AUDIO] Uploading {os.path.basename(audio_path)} for direct acoustic analysis...")
            uploaded = client.files.upload(file=audio_path)
            audio_prompt = f"""Listen to this audio track: "{title}" by {artist}.
Analyze the musical structure for a DJ mix transition:
1. Does singing/vocal start in the first 8 seconds? (true/false)
2. At what second does the first meaningful vocal start? (intro_duration_sec)
3. At what second does the FIRST CHORUS start?
4. At what second does the FIRST CHORUS end? (chorus_end_time - this is where the DJ echoes out)
5. What is the last lyric line of the first chorus?

Return strictly JSON:
{{
  "has_vocals_in_first_8s": true,
  "intro_duration_sec": 8.0,
  "first_chorus_start": 35.0,
  "chorus_end_time": 65.0,
  "last_chorus_line": "lyrics line"
}}
"""
            resp = client.models.generate_content(
                model=MODEL_NAME,
                contents=[uploaded, audio_prompt]
            )
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
            
            if resp and resp.text:
                data = json.loads(clean_json_markdown(resp.text))
                chorus_end = float(data.get("chorus_end_time", 60.0))
                intro_dur = float(data.get("intro_duration_sec", 8.0))
                has_vocals = bool(data.get("has_vocals_in_first_8s", False))
                last_line = data.get("last_chorus_line", "")
                
                print(f"  [GEMINI AUDIO] First chorus detected ending at {chorus_end:.1f}s, Intro: {intro_dur:.1f}s")
                return {
                    "has_vocals": True,
                    "has_vocals_in_first_8s": has_vocals,
                    "intro_duration": intro_dur,
                    "intro_duration_sec": intro_dur,
                    "transition_point": chorus_end,
                    "chorus_end_time": chorus_end,
                    "last_chorus_line": last_line,
                    "transition_candidates": [{
                        "time": chorus_end,
                        "type": "chorus_end",
                        "has_vocals_after": True,
                        "energy": "medium",
                        "reasoning": f"End of first chorus: {last_line}"
                    }],
                    "recommended_transition": chorus_end
                }
        except Exception as e:
            print(f"  [GEMINI AUDIO UPLOAD ERROR] {e}. Falling back to text knowledge base...")

    # Fallback to text prompt knowledge base
    prompt = f"""Song: "{title}" by {artist}. Total duration: ~{duration:.0f}s.
As a music expert and DJ:
1. Does the singing/vocal start in the first 8 seconds?
2. At approximately what second does the first vocal start?
3. At what timestamp does the FIRST CHORUS end (exit point for DJ transition)?
4. What is the last line of that first chorus?

Return strictly JSON:
{{
  "has_vocals_in_first_8s": true,
  "intro_duration_sec": 8.0,
  "chorus_end_time": 65.0,
  "last_chorus_line": "last words of chorus"
}}
"""
    resp_text = call_gemini_with_retry(prompt)
    if resp_text:
        try:
            data = json.loads(clean_json_markdown(resp_text))
            chorus_end = float(data.get("chorus_end_time", 60.0))
            intro_dur = float(data.get("intro_duration_sec", 8.0))
            return {
                "has_vocals": True,
                "has_vocals_in_first_8s": bool(data.get("has_vocals_in_first_8s", False)),
                "intro_duration": intro_dur,
                "intro_duration_sec": intro_dur,
                "transition_point": chorus_end,
                "chorus_end_time": chorus_end,
                "last_chorus_line": data.get("last_chorus_line", ""),
                "transition_candidates": [{
                    "time": chorus_end,
                    "type": "chorus_end",
                    "has_vocals_after": True,
                    "energy": "medium",
                    "reasoning": "First chorus exit"
                }],
                "recommended_transition": chorus_end
            }
        except Exception:
            pass

    # Final safe fallback
    return {
        "has_vocals": True,
        "has_vocals_in_first_8s": False,
        "intro_duration": 8.0,
        "intro_duration_sec": 8.0,
        "transition_point": 60.0,
        "chorus_end_time": 60.0,
        "last_chorus_line": "chorus end",
        "transition_candidates": [{
            "time": 60.0,
            "type": "chorus_end",
            "has_vocals_after": True,
            "energy": "medium",
            "reasoning": "Default transition"
        }],
        "recommended_transition": 60.0
    }
