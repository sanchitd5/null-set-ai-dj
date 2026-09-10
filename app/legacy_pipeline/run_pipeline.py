# run_pipeline.py
"""
Full AI DJ Pipeline Runner
==========================

Orchestrates the end-to-end AI DJ mix creation process:

1. Setlist + Full Analysis:
   - Parses user input, generates setlist with refined BPMs.
   - Adds metadata (genre, key) and structure (chorus/verse) with beat alignment.
   - Produces both basic_setlist.json and structure_data.json.

2. Mixing Plan Generation:
   - Reads analyzed_setlist.json and structure_data.json.
   - Sorts tracks globally by BPM.
   - Applies sliding 5-song window for BPM normalization.
   - Marks every 5th song for full-song crossfade; others use chorus beatmatch.

3. Mix Generation:
   - Reads mixing_plan.json and structure_data.json.
   - Applies chorus beatmatch transitions (second song starts at first chorus start of first song, first song fades out at first chorus end).
   - Minor time-stretching to align BPMs ±2%.
   - Crossfade applied for every 5th song.

Outputs:
- analyzed_setlist.json, structure_data.json
- mixing_plan.json
- Final mix.mp3
"""

import os
import logging
from dotenv import load_dotenv

# ----------------- IMPORT MODULES -----------------
from track_analysis_openai_approach import combined_engine  # Stage 1: song selection
from bpm_lookup import process_bpm_lookup                   # Stage 2: BPM enrichment
from structure_detector import process_structure_detection  # Stage 3: chorus detection
from generate_mixing_plan import generate_mixing_plan      # Stage 4: mixing plan
from mixing_engine import generate_mix                      # Stage 5: final mix

load_dotenv()  # Load API keys for OpenAI

# ----------------- CONFIG -----------------
SONGS_DIR = "./data/songs"
OUTPUT_DIR = "./data/output"

# Create output directory if it doesn't exist
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)
    
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('AI_DJ_Pipeline')

# Suppress Numba debug output
logging.getLogger('numba').setLevel(logging.WARNING)


# ----------------- PIPELINE -----------------
def run_pipeline(user_input: str):
    """
    Executes the full AI DJ pipeline:
    Stage 1: Song selection → analyzed_setlist.json (user-requested songs only)
    Stage 2: BPM lookup → basic_setlist.json
    Stage 3: Transition point detection → structure_data.json
    Stage 4: Mixing plan → mixing_plan.json
    Stage 5: Final mix → mix.mp3
    
    Args:
        user_input: User's complete request (song count, artists, specific songs, mood, etc.)
    """

    try:
        # Stage 1: Select songs based on user request
        logger.info("Stage 1: Selecting songs based on user request...")
        combined_engine(user_input, output_path=os.path.join(OUTPUT_DIR, "analyzed_setlist.json"))
        if not os.path.exists(os.path.join(OUTPUT_DIR, "analyzed_setlist.json")):
            raise FileNotFoundError("analyzed_setlist.json not created.")
        logger.info("Stage 1 complete: analyzed_setlist.json created.")

        # Stage 2: Add BPM, genre, and key metadata
        logger.info("Stage 2: Looking up BPM and metadata...")
        process_bpm_lookup(os.path.join(OUTPUT_DIR, "analyzed_setlist.json"), 
                          os.path.join(OUTPUT_DIR, "basic_setlist.json"))
        if not os.path.exists(os.path.join(OUTPUT_DIR, "basic_setlist.json")):
            raise FileNotFoundError("basic_setlist.json not created.")
        logger.info("Stage 2 complete: basic_setlist.json with BPM data created.")

        # Stage 3: Detect chorus timestamps
        logger.info("Stage 3: Detecting chorus timestamps...")
        process_structure_detection(os.path.join(OUTPUT_DIR, "basic_setlist.json"), 
                                   os.path.join(OUTPUT_DIR, "structure_data.json"))
        if not os.path.exists(os.path.join(OUTPUT_DIR, "structure_data.json")):
            raise FileNotFoundError("structure_data.json not created.")
        logger.info("Stage 3 complete: structure_data.json with chorus timestamps created.")

        # Stage 4: Generate mixing plan with transition timing
        logger.info("Stage 4: Generating mixing plan with transition points...")
        generate_mixing_plan(basic_setlist_path=os.path.join(OUTPUT_DIR, "basic_setlist.json"),
                             structure_json_path=os.path.join(OUTPUT_DIR, "structure_data.json"),
                             output_path=os.path.join(OUTPUT_DIR, "mixing_plan.json"))
        if not os.path.exists(os.path.join(OUTPUT_DIR, "mixing_plan.json")):
            raise FileNotFoundError("mixing_plan.json not created.")
        logger.info("Stage 4 complete: mixing_plan.json ready.")

        # Stage 5: Generate final mix
        logger.info("Stage 5: Generating final MP3 mix...")
        generate_mix(mixing_plan_json=os.path.join(OUTPUT_DIR, "mixing_plan.json"),
                     structure_json=os.path.join(OUTPUT_DIR, "structure_data.json"),
                     output_path=os.path.join(OUTPUT_DIR, "mix.mp3"))
        if not os.path.exists(os.path.join(OUTPUT_DIR, "mix.mp3")):
            raise FileNotFoundError("mix.mp3 not created.")
        logger.info("Stage 5 complete: mix.mp3 generated successfully.")

        logger.info("AI DJ Pipeline execution completed successfully!")
        logger.info(f"Check outputs in '{OUTPUT_DIR}/' folder: analyzed_setlist.json, basic_setlist.json, structure_data.json, mixing_plan.json, mix.mp3")

    except Exception as e:
        logger.error(f"Pipeline execution failed: {str(e)}")
        raise


# ----------------- MAIN -----------------
if __name__ == "__main__":
    # Example: Event-based user input
    user_input = (
        "Mix all songs"
    )
    
    # Duration will be auto-calculated from time range (7pm-10pm = 180 minutes)
    # Or you can override: run_pipeline(user_input, target_duration_minutes=120)
    run_pipeline(user_input)
