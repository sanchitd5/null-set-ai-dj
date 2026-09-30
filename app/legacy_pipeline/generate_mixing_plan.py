# generate_mixing_plan.py

"""
DJ Mixing Plan Generator (Ready for Mixing Engine)

- Reads basic_setlist.json + structure_data.json
- Uses harmonic mixing (Camelot Wheel) for key compatibility
- Dynamic overlap duration based on energy difference
- Chorus Beatmatch for normal tracks
- Outputs mixing_plan.json with exact timings for fade-in/out
"""

import os
import json
from datetime import datetime, timedelta
import librosa

SONGS_DIR = "./data/songs"

# ================= GENRE-SPECIFIC TRANSITION RULES =================
# ================= CAMELOT WHEEL (HARMONIC MIXING) =================
# Musical key compatibility for smooth DJ transitions
CAMELOT_WHEEL = {
    # Major keys
    "C": {"compatible": ["G", "F", "Am"], "energy_boost": ["G"], "energy_drop": ["Am"]},
    "C#": {"compatible": ["G#", "F#", "A#m"], "energy_boost": ["G#"], "energy_drop": ["A#m"]},
    "D": {"compatible": ["A", "G", "Bm"], "energy_boost": ["A"], "energy_drop": ["Bm"]},
    "D#": {"compatible": ["A#", "G#", "Cm"], "energy_boost": ["A#"], "energy_drop": ["Cm"]},
    "E": {"compatible": ["B", "A", "C#m"], "energy_boost": ["B"], "energy_drop": ["C#m"]},
    "F": {"compatible": ["C", "A#", "Dm"], "energy_boost": ["C"], "energy_drop": ["Dm"]},
    "F#": {"compatible": ["C#", "B", "D#m"], "energy_boost": ["C#"], "energy_drop": ["D#m"]},
    "G": {"compatible": ["D", "C", "Em"], "energy_boost": ["D"], "energy_drop": ["Em"]},
    "G#": {"compatible": ["D#", "C#", "Fm"], "energy_boost": ["D#"], "energy_drop": ["Fm"]},
    "A": {"compatible": ["E", "D", "F#m"], "energy_boost": ["E"], "energy_drop": ["F#m"]},
    "A#": {"compatible": ["F", "D#", "Gm"], "energy_boost": ["F"], "energy_drop": ["Gm"]},
    "B": {"compatible": ["F#", "E", "G#m"], "energy_boost": ["F#"], "energy_drop": ["G#m"]},
    
    # Minor keys
    "Am": {"compatible": ["Em", "Dm", "C"], "energy_boost": ["C"], "energy_drop": ["Dm"]},
    "A#m": {"compatible": ["Fm", "D#m", "C#"], "energy_boost": ["C#"], "energy_drop": ["D#m"]},
    "Bm": {"compatible": ["F#m", "Em", "D"], "energy_boost": ["D"], "energy_drop": ["Em"]},
    "Cm": {"compatible": ["Gm", "Fm", "D#"], "energy_boost": ["D#"], "energy_drop": ["Fm"]},
    "C#m": {"compatible": ["G#m", "F#m", "E"], "energy_boost": ["E"], "energy_drop": ["F#m"]},
    "Dm": {"compatible": ["Am", "Gm", "F"], "energy_boost": ["F"], "energy_drop": ["Gm"]},
    "D#m": {"compatible": ["A#m", "G#m", "F#"], "energy_boost": ["F#"], "energy_drop": ["G#m"]},
    "Em": {"compatible": ["Bm", "Am", "G"], "energy_boost": ["G"], "energy_drop": ["Am"]},
    "Fm": {"compatible": ["Cm", "A#m", "G#"], "energy_boost": ["G#"], "energy_drop": ["A#m"]},
    "F#m": {"compatible": ["C#m", "Bm", "A"], "energy_boost": ["A"], "energy_drop": ["Bm"]},
    "Gm": {"compatible": ["Dm", "Cm", "A#"], "energy_boost": ["A#"], "energy_drop": ["Cm"]},
    "G#m": {"compatible": ["D#m", "C#m", "B"], "energy_boost": ["B"], "energy_drop": ["C#m"]},
}

def calculate_key_compatibility(key1: str, key2: str) -> float:
    """
    Calculate harmonic compatibility score between two musical keys.
    Returns: 1.0 (perfect), 0.5 (acceptable), -1.0 (clash)
    """
    if not key1 or not key2 or key1 not in CAMELOT_WHEEL:
        return 0.5  # Unknown, assume neutral
    
    if key1 == key2:
        return 1.0  # Same key = perfect
    
    compatible_keys = CAMELOT_WHEEL[key1].get("compatible", [])
    if key2 in compatible_keys:
        return 1.0  # Harmonically compatible
    
    # Check if relative minor/major
    if key1.endswith("m") and key2 == key1[:-1]:  # Am -> A
        return 0.8
    if not key1.endswith("m") and key2 == key1 + "m":  # A -> Am
        return 0.8
    
    return -1.0  # Key clash - will sound bad


def find_first_chorus_end(track: dict) -> float:
    """
    Find the END of the FIRST CHORUS - where to echo out.
    
    Simple approach: GPT identifies the first chorus and gives us chorus_end_time.
    
    Args:
        track: Track dictionary with structure data
    
    Returns: Time in seconds where first chorus ends (for echo transition)
    """
    # Check both locations: track["structure"] and track directly (backward compatibility)
    structure = track.get("structure", track)
    
    # NEW SIMPLE APPROACH: Use chorus_end_time directly from GPT
    chorus_end = structure.get("chorus_end_time")
    if chorus_end:
        print(f"    → First chorus ends at {chorus_end:.1f}s")
        last_line = structure.get("last_chorus_line", "")
        if last_line:
            print(f"    → Last line: \"{last_line[:50]}...\"" if len(last_line) > 50 else f"    → Last line: \"{last_line}\"")
        return float(chorus_end)
    
    # FALLBACK: Check transition_candidates for chorus_end type
    candidates = structure.get("transition_candidates", [])
    
    # Look for chorus_end type first
    for candidate in candidates:
        if candidate.get("type") == "chorus_end":
            print(f"    → Found chorus_end at {candidate['time']:.1f}s")
            return candidate["time"]
    
    # Look for any point with has_vocals_after=False
    for candidate in candidates:
        if candidate.get("has_vocals_after") == False:
            print(f"    → Found vocal-free point at {candidate['time']:.1f}s")
            return candidate["time"]
    
    # Fallback to recommended_transition
    recommended = structure.get("recommended_transition")
    if recommended:
        print(f"    → Using recommended transition: {recommended:.1f}s")
        return recommended
    
    # Final fallback: Use transition_point
    transition_pt = structure.get("transition_point")
    if transition_pt:
        print(f"    → Using transition_point: {transition_pt:.1f}s")
        return transition_pt
    
    # Last resort fallback
    print(f"    → No chorus end found, using default 60s")
    return 60.0


def load_json(path: str):
    with open(path, "r") as f:
        return json.load(f)


def format_time(seconds: float) -> str:
    return (datetime.min + timedelta(seconds=seconds)).strftime("%H:%M:%S")


def select_tracks_in_order(basic_setlist: dict, structure_data: dict) -> list[dict]:
    """
    Merge basic setlist and analyzed structure data, then SORT BY BPM (lowest to highest).
    This creates a smooth energy progression for DJ mixing.
    """
    all_tracks = []
    analyzed_dict = {}
    for segment in structure_data.get("analyzed_setlist", []):
        for track in segment.get("analyzed_tracks", []):
            analyzed_dict[track["title"]] = track

    for segment in basic_setlist.get("setlist", []):
        for track in segment.get("tracks", []):
            title = track["title"]
            analyzed_track = analyzed_dict.get(title)
            if analyzed_track:
                track_copy = analyzed_track.copy()
                track_copy["original_segment_time"] = segment.get("time", "Unknown")
                all_tracks.append(track_copy)
            else:
                print(f"Warning: '{title}' not found in structure data; skipping.")

    # SORT BY BPM - lowest to highest for smooth energy progression
    all_tracks.sort(key=lambda t: t.get("bpm", 120))
    
    print(f"\n🎵 Song order (sorted by BPM):")
    for i, track in enumerate(all_tracks, 1):
        print(f"  {i}. {track['title']} - {track.get('bpm', 'N/A')} BPM")
    print()
    
    return all_tracks


def generate_mixing_plan(
    basic_setlist_path: str = "basic_setlist.json",
    structure_json_path: str = "structure_data.json",
    output_path: str = "mixing_plan.json",
    overlap_duration: float = 8.0,  # 8 seconds overlap at transition
    fade_duration: float = 1.0  # 1 second fade out
):
    try:
        basic_setlist = load_json(basic_setlist_path)
        structure_data = load_json(structure_json_path)

        all_tracks = select_tracks_in_order(basic_setlist, structure_data)

        mixing_plan = []
        last_start_sec = 0.0
        last_track = None

        for idx, track in enumerate(all_tracks):
            file_path = os.path.join(SONGS_DIR, track["file"])
            if not os.path.exists(file_path):
                print(f"Missing: {file_path}. Skipping.")
                continue

            try:
                duration_sec = librosa.get_duration(filename=file_path)
            except Exception as e:
                print(f"Error loading {track['file']}: {e}. Skipping.")
                continue

            if last_track is None:
                # First track - play from beginning
                start_sec = 0.0
                incoming_start_sec = 0.0
                transition_type = "Fade In"
                transition_point = None
                first_chorus_end = None
                incoming_intro = None
                bpm_change_point = None
                comment = f"Start with {track['title']} (BPM {track.get('bpm', 'N/A')})"
            else:
                # === ECHO TRANSITION: Find first chorus end ===
                print(f"  Analyzing echo transition: {last_track['title']} → {track['title']}")
                
                # Find first_chorus_end from transition_candidates
                first_chorus_end = find_first_chorus_end(last_track)
                
                to_intro_duration = track.get("intro_duration", 8.0)
                
                # Check key compatibility
                from_key = last_track.get("key", "")
                to_key = track.get("key", "")
                key_score = calculate_key_compatibility(from_key, to_key)
                
                # ECHO TRANSITION: Incoming starts at first chorus end
                # 3-second echo plays while incoming starts
                echo_duration = 3.0  # 3 seconds echo
                incoming_start_sec = last_start_sec + first_chorus_end
                
                # BPM change happens at echo start
                bpm_change_point = incoming_start_sec
                
                transition_type = "echo-transition"
                transition_point = first_chorus_end
                incoming_intro = to_intro_duration
                
                # Enhanced comment with key info
                key_info = ""
                if key_score >= 1.0:
                    key_info = f"✓ Keys match ({from_key}→{to_key})"
                elif key_score < 0:
                    key_info = f"⚠ Key clash ({from_key}→{to_key})"
                
                comment = (
                    f"ECHO TRANSITION: {last_track['title']} (BPM {last_track.get('bpm', 120)}) -> {track['title']} "
                    f"(BPM {track.get('bpm', 120)}). Echo at first chorus end ({first_chorus_end:.1f}s). "
                    f"{key_info}. 3s echo + incoming starts simultaneously."
                )

            start_str = format_time(incoming_start_sec)

            mixing_plan.append(
                {
                    "from_track": last_track["title"] if last_track else None,
                    "to_track": track["title"],
                    "incoming_start_sec": incoming_start_sec,
                    "start_time": start_str,
                    "transition_point": transition_point,
                    "first_chorus_end_sec": first_chorus_end if last_track else None,
                    "echo_duration_sec": 3.0 if last_track else None,
                    "incoming_intro_duration": incoming_intro,
                    "bpm_change_point_sec": bpm_change_point,
                    "overlap_duration": overlap_duration,
                    "fade_duration": fade_duration,
                    "transition_type": transition_type,
                    "to_bpm": track.get("bpm", 120),
                    "from_bpm": last_track.get("bpm", 120) if last_track else None,
                    "comment": comment,
                }
            )

            last_start_sec = incoming_start_sec
            last_track = track

        with open(output_path, "w") as f:
            json.dump({"mixing_plan": mixing_plan}, f, indent=2)

        print(f"Mixing plan saved to '{output_path}' with {len(mixing_plan)} tracks.")

    except Exception as e:
        print(f"Error generating mixing plan: {e}")
        raise


if __name__ == "__main__":
    generate_mixing_plan()
