"""
BPM Lookup Module.
Provides OpenAI-based BPM refinement for songs using text prompts.
Falls back to librosa beat tracking if OpenAI lookup fails.
Standalone for metadata lookup without audio processing.
"""

import os
import json
import re
from dotenv import load_dotenv
try:
    from openai import OpenAI
except Exception:
    OpenAI = None
OpenAI = OpenAI if OpenAI else None

try:
    import librosa
    import numpy as np
except ImportError:
    librosa = None
    np = None

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
    """Generate cache file path for a song."""
    # Use filename without extension as cache key
    base_name = os.path.splitext(filename)[0]
    # Sanitize filename for use as cache file
    safe_name = re.sub(r'[^\w\s-]', '_', base_name)
    return os.path.join(NOTES_DIR, f"{safe_name}_metadata.json")


def load_cached_metadata(filename):
    """Load cached metadata if available."""
    cache_path = get_cache_path(filename)
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                print(f"  ✓ Loaded cached metadata from notes/")
                return data
        except Exception as e:
            print(f"  ⚠ Cache read failed: {e}")
    return None


def save_cached_metadata(filename, metadata):
    """Save metadata to cache for future use."""
    cache_path = get_cache_path(filename)
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
        print(f"  ✓ Cached metadata to notes/")
    except Exception as e:
        print(f"  ⚠ Cache write failed: {e}")


def estimate_bpm_with_librosa(audio_path):
    """
    Fallback BPM estimation using librosa beat tracking.
    Also calculates energy level for professional DJ mixing.
    Returns dict with bpm and energy, or None if failed.
    """
    if librosa is None:
        print("⚠ librosa not available for BPM estimation")
        return None
    
    try:
        print(f"  → Using librosa beat tracking for '{os.path.basename(audio_path)}'...")
        
        # Load audio
        y, sr = librosa.load(audio_path, sr=22050, duration=120)  # Load first 2 minutes
        
        # Use beat tracking to estimate tempo
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
        
        # tempo is returned as float, convert to int
        bpm = int(round(tempo))
        
        # Calculate energy level (RMS)
        rms = librosa.feature.rms(y=y)[0]
        energy = float(np.mean(rms))
        
        # Normalize energy to 0-1 scale
        # Typical RMS values: quiet=0.01, normal=0.05-0.15, loud=0.3+
        energy_normalized = min(1.0, energy / 0.25)
        
        # Validate BPM is in reasonable range
        if 60 <= bpm <= 220:
            print(f"  ✓ Librosa: BPM={bpm}, Energy={energy_normalized:.2f}")
            return {"bpm": bpm, "energy": energy_normalized}
        else:
            print(f"  ⚠ Librosa BPM out of range: {bpm}, using default")
            return None
            
    except Exception as e:
        print(f"  ❌ Librosa BPM estimation failed: {e}")
        return None


def refine_bpm(title, artist, audio_file=None):
    """
    BPM detection using librosa beat tracking only.
    OpenAI BPM lookup disabled - using local librosa analysis for accuracy.
    """
    # Use librosa beat tracking for BPM detection
    if audio_file and os.path.exists(audio_file):
        print(f"  → Using librosa beat tracking for BPM...")
        librosa_result = estimate_bpm_with_librosa(audio_file)
        if librosa_result and isinstance(librosa_result, dict):
            return librosa_result.get("bpm", 120)
        elif librosa_result:
            return librosa_result
    
    # Final fallback to default
    print(f"  → Using default BPM: 120")
    return 120


def get_genre(title, artist):
    """
    Lookup for song genre using Gemini (fallback to OpenAI/Unknown).
    """
    try:
        import gemini_client
        if gemini_client.API_KEYS:
            g, _, _ = gemini_client.get_metadata_with_gemini(title, artist)
            if g and g != "Unknown":
                print(f"Genre for '{title}' by '{artist}': {g}")
                return g
    except Exception as e:
        pass

    try:
        if client is not None:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a music metadata expert. Respond ONLY with the primary genre (e.g., R&B) from reliable sources. No explanation, no extra text."},
                    {"role": "user", "content": f"What is the primary genre of '{title}' by '{artist}'?"}
                ],
                temperature=0.0,
                max_tokens=10
            )
            genre = response.choices[0].message.content.strip()
            if genre:
                print(f"Genre for '{title}' by '{artist}': {genre}")
                return genre
    except Exception as e:
        print(f"Genre lookup failed for '{title}' by '{artist}': {e}")
    return "Unknown"


def estimate_key(title, artist):
    """
    Lookup for song key and scale using Gemini (fallback to OpenAI/C major).
    """
    try:
        import gemini_client
        if gemini_client.API_KEYS:
            _, k, s = gemini_client.get_metadata_with_gemini(title, artist)
            if k:
                print(f"Key for '{title}' by '{artist}': {k} {s}")
                return k, s
    except Exception as e:
        pass

    try:
        if client is not None:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a music metadata expert. Respond ONLY with 'Key: C Scale: major' format from reliable sources like Tunebat. No extra text."},
                    {"role": "user", "content": f"What is the key and scale of '{title}' by '{artist}'?"}
                ],
                temperature=0.0,
                max_tokens=20
            )
            text = response.choices[0].message.content.strip()
            match = re.search(r'Key: (\w+) Scale: (\w+)', text)
            if match:
                key, scale = match.groups()
                print(f"Key for '{title}' by '{artist}': {key} {scale}")
                return key, scale
    except Exception as e:
        print(f"Key lookup failed for '{title}' by '{artist}': {e}")
    return "C", "major"


def _key_to_semitone(key, scale):
    keys = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    idx = keys.index(key)
    if scale == 'minor':
        idx += 12
    return idx


def process_bpm_lookup(input_json: str = "analyzed_setlist.json", output_json: str = "basic_setlist.json"):
    """
    Reads analyzed_setlist.json, enriches each song with BPM/genre/key, outputs basic_setlist.json.
    """
    if client is None:
        print("[WARN] OpenAI client not configured. Using default values.")
    
    try:
        with open(input_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        enriched_setlist = {"setlist": []}
        
        for segment in data.get("analyzed_setlist", []):
            time_slot = segment.get("time", "00:00")
            enriched_tracks = []
            
            for track in segment.get("analyzed_tracks", []):
                title = track.get("title", "Unknown")
                artist = track.get("artist", "Unknown")
                file = track.get("file", "")
                
                print(f"Processing: {title} by {artist}")
                
                # Check cache first
                cached = load_cached_metadata(file)
                if cached and "bpm" in cached:
                    print(f"  ✓ Using cached data")
                    enriched_tracks.append({
                        "title": title,
                        "artist": artist,
                        "file": file,
                        "bpm": cached.get("bpm", 120),
                        "genre": cached.get("genre", "Unknown"),
                        "key": cached.get("key", "C"),
                        "key_semitone": cached.get("key_semitone", 0),
                        "scale": cached.get("scale", "major"),
                        "energy": cached.get("energy", 0.5)  # Energy level for DJ mixing
                    })
                    continue
                
                # Build full path to audio file
                audio_path = os.path.join(SONGS_DIR, file) if file else None
                
                # Lookup metadata with fallback to librosa
                bpm_result = refine_bpm(title, artist, audio_path) if client else (estimate_bpm_with_librosa(audio_path) if audio_path and os.path.exists(audio_path) else 120)
                
                # Extract BPM and energy
                if isinstance(bpm_result, dict):
                    bpm = bpm_result.get("bpm", 120)
                    energy = bpm_result.get("energy", 0.5)
                else:
                    bpm = bpm_result
                    energy = 0.5  # Default energy if not calculated
                
                genre = get_genre(title, artist) if client else "Unknown"
                key, scale = estimate_key(title, artist) if client else ("C", "major")
                key_semitone = _key_to_semitone(key, scale)
                
                metadata = {
                    "title": title,
                    "artist": artist,
                    "file": file,
                    "bpm": bpm,
                    "genre": genre,
                    "key": f"{key}m" if scale == "minor" else key,
                    "key_semitone": key_semitone,
                    "scale": scale,
                    "energy": energy  # Add energy to metadata
                }
                
                # Save to cache
                save_cached_metadata(file, metadata)
                
                enriched_tracks.append(metadata)
            
            enriched_setlist["setlist"].append({
                "time": time_slot,
                "tracks": enriched_tracks
            })
        
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(enriched_setlist, f, indent=2)
        
        print(f"\nBPM lookup complete. Saved to '{output_json}'.")
        return enriched_setlist
    
    except Exception as e:
        print(f"[ERROR] BPM lookup failed: {e}")
        raise


if __name__ == "__main__":
    # Example usage
    process_bpm_lookup("analyzed_setlist.json", "basic_setlist.json")