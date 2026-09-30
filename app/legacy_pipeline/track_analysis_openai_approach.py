"""
DJ Mixing Pipeline: Simplified song selection using OpenAI.
Handles MP3 scanning and lets OpenAI parse any user request format and select songs directly.
Outputs 'analyzed_setlist.json' for the next pipeline stage (BPM lookup).

Features:
- Flexible input: works with specific counts, artists, moods, or "make a mix with all songs"
- OpenAI handles ALL parsing, selection, and ordering logic
- Single-pass selection for simplicity and reliability
"""

import os
import json
from dotenv import load_dotenv
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

load_dotenv()

client = None
if OpenAI is not None:
    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception:
        client = None

SONGS_DIR = "./data/songs"
def select_and_order_songs_with_openai(available_songs, user_input):
    """
    Let OpenAI parse the user input and directly select + order songs from available list.
    This is more flexible - handles any request format (specific counts, artists, moods, or just "make a mix").
    Returns ordered list of selected songs.
    """
    if client is None:
        print("⚠️ OpenAI not available, using all available songs")
        return available_songs[:10]
    
    # Format available songs for OpenAI
    songs_list = []
    for song in available_songs:
        songs_list.append(f'"{song["title"]}" by {song["artist"]} (file: {song["file"]})')
    
    available_songs_str = "\n".join(songs_list)
    
    prompt = f"""You are a professional DJ. The user wants a DJ mix and has given this request:

"{user_input}"

Available songs in the library:
{available_songs_str}

Your task:
1. Parse the user's request (they may specify: number of songs, artists, specific song names, duration, mood, or just say "make a mix")
2. Select the appropriate songs from the available library that match their request
3. Order the songs in a DJ-friendly sequence (good flow, energy progression)
4. If user doesn't specify a count, choose 10-15 songs for a good mix
5. If user says "all songs" or similar, include ALL available songs

Return ONLY a JSON object in this format:
{{
  "selected_songs": [
    {{
      "title": "exact song title from library",
      "artist": "exact artist name from library",
      "file": "exact filename from library"
    }},
    ...
  ]
}}

IMPORTANT: 
- Return songs in the ORDER they should be mixed
- Use EXACT titles, artists, and filenames from the available library
- Prioritize any specific songs mentioned by the user
- Match artist names flexibly (e.g., "Anirudh" matches "Anirudh Ravichander")
"""
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        result = response.choices[0].message.content.strip()
        
        # Clean markdown
        if result.startswith("```"):
            result = result.split("\n", 1)[1] if "\n" in result else result
            result = result.rsplit("\n```", 1)[0] if "```" in result else result
            result = result.replace("```json", "").strip()
        
        parsed = json.loads(result)
        selected_songs = parsed.get("selected_songs", [])
        
        print(f"\n✅ OpenAI selected {len(selected_songs)} songs in DJ order")
        return selected_songs
        
    except Exception as e:
        print(f"⚠️ OpenAI selection failed: {e}")
        print(f"   Falling back to first 10 songs")
        return available_songs[:10]


def get_available_songs():
    """Scan songs directory and return list of available songs."""
    songs = []
    if not os.path.exists(SONGS_DIR):
        return songs
        
    for filename in os.listdir(SONGS_DIR):
        if not filename.lower().endswith(".mp3"):
            continue
            
        # Clean filename
        clean_name = filename[:-4]
        if clean_name.startswith("[iSongs.info] "):
            clean_name = clean_name.replace("[iSongs.info] ", "")
        
        # Parse artist - title
        parts = clean_name.split(" - ", 1)
        if len(parts) == 2:
            artist = parts[0].strip()
            title = parts[1].strip()
        else:
            artist = "Unknown"
            title = clean_name.strip()
        
        songs.append({
            "title": title,
            "artist": artist,
            "file": filename
        })
    
    return songs


def combined_engine(user_input, output_path="data/output/analyzed_setlist.json"):
    """
    Main entry point: Get available songs, let OpenAI select and order them based on user request.
    Works with ANY request format - specific requirements or just "make a mix with all songs".
    """
    try:
        print("="*60)
        print("STAGE 1: SONG SELECTION")
        print("="*60)
        
        # Step 1: Get all available songs
        all_songs = get_available_songs()
        if not all_songs:
            print("❌ No songs found in ./songs directory")
            return None
        
        print(f"📁 Found {len(all_songs)} songs in library")
        print(f"📝 User request: \"{user_input}\"\n")
        
        # Step 2: Let Gemini (or fallback to OpenAI) parse request and select songs directly
        try:
            import gemini_client
            if gemini_client.API_KEYS:
                selected_songs = gemini_client.select_and_order_songs_with_gemini(all_songs, user_input)
            else:
                selected_songs = select_and_order_songs_with_openai(all_songs, user_input)
        except Exception as e:
            print(f"  [WARN] Gemini selection failed: {e}. Falling back to OpenAI/default.")
            selected_songs = select_and_order_songs_with_openai(all_songs, user_input)
        
        if not selected_songs:
            print("❌ No songs selected")
            return None
        
        # Step 3: Create output structure
        output_data = {
            "analyzed_setlist": [{
                "time": "00:00",
                "analyzed_tracks": selected_songs
            }]
        }
        
        # Step 4: Save to file
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        
        print(f"\n✅ Selected {len(selected_songs)} songs:")
        for idx, song in enumerate(selected_songs, 1):
            print(f"   {idx}. {song['title']} by {song['artist']}")
        
        print(f"\n💾 Saved to: {output_path}")
        return output_data
        
    except Exception as e:
        print(f"❌ Selection failed: {e}")
        import traceback
        traceback.print_exc()
        return None


if __name__ == "__main__":
    test_input = "Generate a mix for 5 anirudh ravichandar songs and 5 ar rahman songs. Must include bloody sweet and OMG Pilla"
    combined_engine(test_input)