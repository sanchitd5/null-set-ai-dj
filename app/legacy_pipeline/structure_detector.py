"""
Structure Timestamp Detector Module - OPTIMIZED VERSION
Analyzes songs for DJ transitions using OpenAI Whisper API only (no local model).
Detects transition_point and intro_duration for mixing.

SPEED OPTIMIZATIONS:
1. Uses OpenAI Whisper API (no local model loading)
2. Skips word-level timestamps (not needed for transition detection)
3. Reduces audio analysis to first 90 seconds only
4. Parallel processing ready (can be added if needed)
"""

import os
import json
import numpy as np
import scipy.signal
from dotenv import load_dotenv

try:
    from openai import OpenAI
    import librosa
except Exception:
    OpenAI = None
    librosa = None

load_dotenv()

client = None
if OpenAI is not None:
    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception:
        client = None

SONGS_DIR = "./data/songs"
NOTES_DIR = "./notes"

# Create notes directory if it doesn't exist
if not os.path.exists(NOTES_DIR):
    os.makedirs(NOTES_DIR)


def get_cache_path(filename):
    """Generate cache file path for a song's structure data."""
    # Use filename without extension as cache key
    base_name = os.path.splitext(filename)[0]
    # Sanitize filename for use as cache file
    import re
    safe_name = re.sub(r'[^\w\s-]', '_', base_name)
    return os.path.join(NOTES_DIR, f"{safe_name}_structure.json")


def load_cached_structure(filename):
    """Load cached structure analysis if available."""
    cache_path = get_cache_path(filename)
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                print(f"  ✓ Loaded cached structure from notes/")
                return data
        except Exception as e:
            print(f"  ⚠ Cache read failed: {e}")
    return None


def save_cached_structure(filename, structure_data):
    """Save structure analysis to cache for future use."""
    cache_path = get_cache_path(filename)
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(structure_data, f, indent=2)
        print(f"  ✓ Cached structure to notes/")
    except Exception as e:
        print(f"  ⚠ Cache write failed: {e}")


def clean_json_output(text: str) -> str:
    """Strip code fences from GPT output."""
    return text.replace("```json", "").replace("```", "").strip()


def transcribe_song_fast(client, audio_path):
    """Transcribe song using OpenAI Whisper API (FAST - no local model)."""
    print(f"🔊 Transcribing: {os.path.basename(audio_path)}")
    with open(audio_path, "rb") as audio_file:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            response_format="verbose_json",
            timestamp_granularities=["segment"]  # Only segment-level, not word-level
        )
    return result.model_dump()


def extract_beat_times_fast(audio_path, max_duration=90):
    """
    Extract beat timestamps and phrase boundaries - OPTIMIZED: only first 90 seconds.
    Returns beat times, tempo, and phrase boundaries (every 8 bars).
    """
    if librosa is None:
        return np.array([]), 120.0, np.array([])
    
    try:
        # Only load first 90 seconds for speed
        y, sr = librosa.load(audio_path, sr=22050, duration=max_duration)  # Lower sample rate = faster
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
        beat_times = librosa.frames_to_time(beats, sr=sr)
        
        # Calculate phrase boundaries (every 8 bars = 32 beats)
        beats_per_phrase = 32  # 8 bars × 4 beats
        phrase_boundaries = []
        for i in range(0, len(beat_times), beats_per_phrase):
            if i < len(beat_times):
                phrase_boundaries.append(beat_times[i])
        
        return beat_times, tempo, np.array(phrase_boundaries)
    except Exception as e:
        print(f"Beat detection failed: {e}")
        return np.array([]), 120.0, np.array([])


def analyze_energy_curve(audio_path, max_duration=120):
    """
    Analyze energy curve to detect buildups, drops, and transitions.
    Returns energy characteristics for intelligent transition point selection.
    """
    if librosa is None:
        return {"has_buildup": False, "has_drop": False, "buildups": [], "drops": []}
    
    try:
        y, sr = librosa.load(audio_path, sr=22050, duration=max_duration)
        
        # Calculate RMS energy in 2-second windows
        hop_length = sr * 2  # 2-second windows
        rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
        
        # Smooth energy curve
        from scipy.signal import savgol_filter
        if len(rms) > 5:
            rms_smooth = savgol_filter(rms, min(11, len(rms) if len(rms) % 2 == 1 else len(rms) - 1), 3)
        else:
            rms_smooth = rms
        
        # Find energy changes (first derivative)
        energy_diff = np.diff(rms_smooth)
        
        # Detect buildups (sustained energy increases)
        buildups = []
        for i in range(len(energy_diff) - 3):
            # 3+ consecutive increases = buildup
            if all(energy_diff[i:i+3] > 0.005):
                time = i * 2  # Convert to seconds
                if 45 <= time <= 115:
                    buildups.append({
                        "time": time,
                        "peak_time": (i + 3) * 2,
                        "intensity": float(energy_diff[i:i+3].sum())
                    })
        
        # Detect drops (sudden energy decreases)
        drops = []
        for i in range(len(energy_diff)):
            if energy_diff[i] < -0.03:  # Significant drop
                time = i * 2
                if 45 <= time <= 115:
                    drops.append({
                        "time": time,
                        "depth": float(abs(energy_diff[i]))
                    })
        
        # Calculate energy at key points
        def get_energy_at_time(target_time):
            idx = min(int(target_time / 2), len(rms_smooth) - 1)
            return float(rms_smooth[idx])
        
        return {
            "has_buildup": len(buildups) > 0,
            "has_drop": len(drops) > 0,
            "buildups": buildups,
            "drops": drops,
            "energy_curve": [float(x) for x in rms_smooth]
            # Removed "get_energy_at" - it's a function, not JSON serializable
        }
    except Exception as e:
        print(f"  Energy analysis failed: {e}")
        return {"has_buildup": False, "has_drop": False, "buildups": [], "drops": []}


def snap_to_phrase_boundary(target_time, phrase_boundaries, direction="nearest"):
    """
    Snap a target time to the nearest musical phrase boundary.
    This ensures transitions happen at natural musical breaks (every 8 bars).
    
    Args:
        target_time: Desired transition time in seconds
        phrase_boundaries: Array of phrase boundary times
        direction: "nearest", "before", or "after"
    
    Returns:
        Snapped time at phrase boundary
    """
    if len(phrase_boundaries) == 0:
        return target_time
    
    phrase_boundaries = np.array(phrase_boundaries)
    
    if direction == "nearest":
        idx = np.argmin(np.abs(phrase_boundaries - target_time))
        snapped_time = phrase_boundaries[idx]
    elif direction == "before":
        # Get all boundaries at or before target
        valid = phrase_boundaries[phrase_boundaries <= target_time]
        if len(valid) == 0:
            snapped_time = phrase_boundaries[0]
        else:
            snapped_time = valid[-1]  # Last (closest) boundary before target
    else:  # after
        # Get all boundaries at or after target
        valid = phrase_boundaries[phrase_boundaries >= target_time]
        if len(valid) == 0:
            snapped_time = phrase_boundaries[-1]
        else:
            snapped_time = valid[0]  # First (closest) boundary after target
    
    if abs(snapped_time - target_time) > 2.0:  # Only log if significant change
        print(f"  → Snapped {target_time:.1f}s to phrase boundary at {snapped_time:.1f}s")
    return float(snapped_time)


def snap_to_bar_boundary(target_time, beats, beats_per_bar=4):
    """
    FIX FOR ISSUE #2: Snap timestamp to nearest BAR boundary (not just beat).
    
    Professional DJs always transition on bar boundaries (beat 1 of a bar),
    not in the middle of a bar.
    
    Args:
        target_time: Desired time in seconds
        beats: Array of beat times in seconds
        beats_per_bar: Number of beats per bar (usually 4 for 4/4 time)
    
    Returns:
        Time snapped to nearest bar boundary (downbeat)
    """
    if len(beats) < beats_per_bar:
        return target_time
    
    # Calculate bar boundaries (every 4th beat = downbeat)
    bar_boundaries = beats[::beats_per_bar]
    
    if len(bar_boundaries) == 0:
        return target_time
    
    # Find nearest bar boundary
    idx = np.argmin(np.abs(bar_boundaries - target_time))
    snapped_time = bar_boundaries[idx]
    
    if abs(snapped_time - target_time) > 0.5:
        print(f"  → Quantized {target_time:.2f}s to bar boundary at {snapped_time:.2f}s")
    
    return float(snapped_time)


def snap_to_beat(target_time, beats):
    """
    Snap timestamp to nearest beat (for fine-grained alignment).
    
    Args:
        target_time: Desired time in seconds
        beats: Array of beat times in seconds
    
    Returns:
        Time snapped to nearest beat
    """
    if len(beats) == 0:
        return target_time
    
    idx = np.argmin(np.abs(beats - target_time))
    return float(beats[idx])


def ask_gpt4o_for_transition_point_fast(segments, beats_str, title, artist, duration):
    """
    Ask GPT to detect transition point and check for vocals in first 8 seconds.
    Returns transition point, intro duration, and whether vocals exist in first 8s.
    """
    # Format segments with timestamps
    lyrics_formatted = []
    has_early_vocals = False
    first_meaningful_audio_time = None
    
    for seg in segments[:20]:  # Only first 20 segments (~first 2 minutes)
        start = seg.get("start", 0)
        end = seg.get("end", start + 2)
        text = seg.get("text", "").strip()
        lyrics_formatted.append(f"[{start:.1f}s - {end:.1f}s] {text}")
        
        # Check if there are vocals in first 8 seconds
        if start < 8.0 and text and len(text) > 10:  # Meaningful lyrics
            has_early_vocals = True
        
        # Find FIRST meaningful audio (not silence/noise)
        if first_meaningful_audio_time is None and text and len(text) > 5:
            first_meaningful_audio_time = start
    
    # If no meaningful audio found, default to 0.5s (avoid complete silence)
    if first_meaningful_audio_time is None:
        first_meaningful_audio_time = 0.5
    
    lyrics_text = "\n".join(lyrics_formatted)
    
    prompt = f"""Song: "{title}" by {artist}

Here are the lyrics with timestamps:
{lyrics_text}

TASK: Find the FIRST CHORUS of this song.

The chorus is the catchy, repeated hook part that comes after the intro and first verse. It's usually the most memorable part with repeated lyrics.

STEP 1: Identify which lines are the FIRST CHORUS
STEP 2: Find the LAST LINE of that first chorus  
STEP 3: Give me the END timestamp of that last line

Return JSON:
{{
  "first_chorus_lines": ["line 1", "line 2", ...],
  "last_chorus_line": "<the final line of the first chorus>",
  "chorus_end_time": <END timestamp of the last chorus line - this is where we echo out>,
  "has_vocals_in_first_8s": <true if singing starts before 8 seconds>,
  "intro_duration_sec": <when does first vocal start>
}}"""
    
    client_local = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client_local.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1
    )
    
    result = json.loads(clean_json_output(response.choices[0].message.content))
    
    # Add Python-detected early vocals as backup
    if "has_vocals_in_first_8s" not in result:
        result["has_vocals_in_first_8s"] = has_early_vocals
    
    # CRITICAL FIX: Ensure intro_duration reflects actual audio start
    if "intro_duration_sec" not in result or result["intro_duration_sec"] < 0.3:
        # Use detected audio start time, with minimum of 0.5s
        result["intro_duration_sec"] = max(first_meaningful_audio_time, 0.5)
        print(f"  → Corrected intro_duration to {result['intro_duration_sec']:.2f}s (actual audio start)")
    
    # NEW SIMPLE FORMAT: Use chorus_end_time directly
    if "chorus_end_time" in result:
        chorus_end = float(result["chorus_end_time"])
        print(f"  → First chorus ends at {chorus_end:.1f}s")
        print(f"  → Last chorus line: {result.get('last_chorus_line', 'unknown')}")
        
        # Create transition candidates for backward compatibility
        result["transition_candidates"] = [{
            "time": chorus_end,
            "type": "chorus_end",
            "has_vocals_after": True,
            "energy": "medium",
            "reasoning": f"End of first chorus: {result.get('last_chorus_line', '')}"
        }]
        result["recommended_transition"] = chorus_end
        result["transition_point"] = chorus_end
    elif "transition_candidates" not in result:
        # Fallback: create single candidate from old format
        result["transition_candidates"] = [{
            "time": result.get("transition_point_sec", 70.0),
            "type": "verse_end",
            "has_vocals_after": result.get("has_vocals_in_first_8s", False),
            "energy": "medium",
            "reasoning": "Fallback transition point"
        }]
        result["recommended_transition"] = result.get("transition_point_sec", 70.0)
        result["transition_point"] = result.get("recommended_transition", 70.0)
    else:
        # Maintain backward compatibility for old format
        result["transition_point"] = result.get("recommended_transition", 70.0)
    
    result["intro_duration"] = result.get("intro_duration_sec", 8.0)
    
    return result


def analyze_structure_fast(title, artist, filename, bpm, SONGS_DIR="./data/songs"):
    """
    OPTIMIZED structure analysis:
    - Uses OpenAI Whisper API (no local model loading)
    - Only processes first 90s of audio
    - Uses segment-level timestamps (not word-level)
    - Uses gpt-4o-mini for speed
    - Caches results in notes/ folder for reuse
    """
    # Check cache first
    cached = load_cached_structure(filename)
    if cached and "transition_point" in cached:
        print(f"  ✓ Using cached structure data")
        return cached
    
    file_path = os.path.join(SONGS_DIR, filename)
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}. Using fallback.")
        return {
            "has_vocals": False, 
            "transition_point": 70.0,
            "intro_duration": 8.0
        }

    try:
        # Step 2: Fast beat extraction with phrase boundaries (only first 90s)
        beats, tempo, phrase_boundaries = extract_beat_times_fast(file_path, max_duration=90)
        beats_str = ", ".join(f"{t:.1f}" for t in beats[:100])  # Only first 100 beats

        duration = 180.0
        try:
            import soundfile as sf
            f_info = sf.info(file_path)
            duration = f_info.duration
        except Exception:
            pass

        # Check if Gemini is available
        import gemini_client
        if gemini_client.API_KEYS:
            result = gemini_client.detect_chorus_with_gemini(file_path, title, artist, duration=duration)
            has_vocals = result.get("has_vocals", True)
        else:
            # Fallback to OpenAI Whisper
            transcript = transcribe_song_fast(client, file_path)
            has_vocals = bool(transcript.get("text", "").strip())
            duration = transcript.get("duration", 180.0)
            segments = transcript.get("segments", [])
            result = ask_gpt4o_for_transition_point_fast(segments, beats_str, title, artist, duration)

        # Step 3.5: Add energy curve analysis
        print("  Analyzing energy curve...")
        energy_data = analyze_energy_curve(file_path, max_duration=120)
        result["energy_analysis"] = energy_data
        
        transition_point = float(result.get("transition_point_sec", result.get("recommended_transition", 70.0)))
        intro_duration = float(result.get("intro_duration_sec", 8.0))
        has_vocals_in_first_8s = result.get("has_vocals_in_first_8s", False)
        transition_is_line_end = result.get("transition_is_line_end", True)
        
        # ==========================================================
        # FIX ISSUE #2: MUSICAL QUANTIZATION OF ALL TIMESTAMPS
        # ==========================================================
        # All timestamps MUST be snapped to musical boundaries:
        # - Transitions: snap to BAR boundaries (every 4 beats / downbeats)
        # - Intro points: snap to beats at minimum
        # ==========================================================
        
        if len(beats) > 0:
            # STEP 1: Snap transition point to BAR boundary (most important!)
            # Professional DJs ALWAYS transition on beat 1 of a bar
            original_transition = transition_point
            transition_point = snap_to_bar_boundary(transition_point, beats, beats_per_bar=4)
            
            # If phrase boundaries exist, prefer those (every 8 bars = 32 beats)
            if len(phrase_boundaries) > 0:
                phrase_snapped = snap_to_phrase_boundary(transition_point, phrase_boundaries, direction="nearest")
                # Use phrase boundary if it's within 4 seconds of bar-snapped time
                if abs(phrase_snapped - transition_point) < 4.0:
                    transition_point = phrase_snapped
            
            # STEP 2: Snap intro duration to beat boundary
            # This ensures incoming track enters on a beat
            intro_duration = snap_to_beat(intro_duration, beats)
            
            # STEP 3: Quantize ALL transition candidates in the result
            if "transition_candidates" in result:
                for candidate in result["transition_candidates"]:
                    if "time" in candidate:
                        candidate["time"] = snap_to_bar_boundary(candidate["time"], beats, beats_per_bar=4)
            
            print(f"  ✓ Quantized transition: {original_transition:.1f}s → {transition_point:.1f}s (bar-aligned)")
        else:
            print(f"  ⚠️  No beats detected - timestamps not quantized!")
        
        # Step 5: Enforce constraints
        transition_point = float(np.clip(transition_point, 50.0, min(120.0, duration - 10.0)))
        intro_duration = float(np.clip(intro_duration, 0.0, 20.0))
        
        print(f"  ✓ Transition: {transition_point:.1f}s (phrase-aligned), Intro: {intro_duration:.1f}s")
        print(f"    Vocals in first 8s: {has_vocals_in_first_8s}, Line end: {transition_is_line_end}")
        
        # Prepare complete structure data with all new fields
        structure_data = {
            "has_vocals": has_vocals, 
            "transition_point": transition_point,
            "intro_duration": intro_duration,
            "has_vocals_in_first_8s": has_vocals_in_first_8s,
            "transition_is_line_end": transition_is_line_end,
            # NEW: Include all transition candidates and energy analysis
            "transition_candidates": result.get("transition_candidates", []),
            "recommended_transition": result.get("recommended_transition", transition_point),
            "energy_analysis": energy_data
        }
        
        # Save to cache
        save_cached_structure(filename, structure_data)
        
        return structure_data
        
    except Exception as e:
        print(f"Error analyzing '{title}': {e}")
        return {
            "has_vocals": False, 
            "transition_point": 70.0,
            "intro_duration": 8.0
        }


def process_structure_detection(input_json: str = "basic_setlist.json", output_json: str = "structure_data.json"):
    """
    Process all songs with OPTIMIZED structure detection.
    """
    if client is None:
        print("[WARN] OpenAI client not configured. Using fallback times.")
    
    try:
        with open(input_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        structure_data = {"analyzed_setlist": []}
        
        for segment in data.get("setlist", []):
            time_slot = segment.get("time", "00:00")
            analyzed_tracks = []
            
            for track in segment.get("tracks", []):
                title = track.get("title", "Unknown")
                artist = track.get("artist", "Unknown")
                filename = track.get("file", "")
                bpm = track.get("bpm", 120)
                
                print(f"🎵 Analyzing: {title} by {artist}")
                
                # FAST structure detection
                structure = analyze_structure_fast(title, artist, filename, bpm, SONGS_DIR)
                
                # Merge data
                analyzed_track = track.copy()
                analyzed_track.update(structure)
                analyzed_tracks.append(analyzed_track)
            
            structure_data["analyzed_setlist"].append({
                "time": time_slot,
                "analyzed_tracks": analyzed_tracks
            })
        
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(structure_data, f, indent=2)
        
        print(f"\n✅ Structure detection complete. Saved to '{output_json}'.")
        return structure_data
    
    except Exception as e:
        print(f"[ERROR] Structure detection failed: {e}")
        raise


if __name__ == "__main__":
    process_structure_detection("basic_setlist.json", "structure_data.json")