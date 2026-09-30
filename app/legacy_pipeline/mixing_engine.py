# mixing_engine.py
"""
DJ Mixing Engine: Chorus Beatmatch + Crossfade + Minor Time-Stretch

- Reads mixing_plan.json + structure_data.json
- Applies chorus beatmatch: second song starts at first chorus start of first
- Fade out first song at first chorus end
- Minor time-stretching on incoming song to match BPM of outgoing (±2%)
- Every 5th song: full-song + crossfade
- Outputs normalized mix.mp3
"""

import os
import json
import numpy as np
from pydub import AudioSegment
from pydub.effects import normalize
import librosa
import scipy.signal
from scipy.signal import butter, sosfilt

# Import waveform visualization module
try:
    from waveform_visualizer import (
        plot_waveform_alignment,
        plot_beat_alignment,
        plot_phase_cancellation_check,
        plot_mix_overview
    )
    VISUALIZATION_ENABLED = True
except ImportError as e:
    print(f"Warning: Waveform visualization disabled - {e}")
    VISUALIZATION_ENABLED = False

SONGS_DIR = "./data/songs"

# ================= GENRE-SPECIFIC DJ MIXING RULES =================
# ================= SAFE CONVERSIONS =================
def ms(seconds) -> int:
    if seconds is None or seconds == "":
        return 0
    try:
        return int(round(float(seconds) * 1000))
    except (ValueError, TypeError):
        return 0

def safe_float(val, default=0.0):
    if val is None:
        return default
    try:
        return float(val)
    except:
        return default

# ================= AUDIO UTILITIES =================
def audio_segment_to_np(seg: AudioSegment):
    samples = np.array(seg.get_array_of_samples())
    if seg.channels == 2:
        samples = samples.reshape((-1,2)).mean(axis=1)
    return samples.astype(np.float32) / 32768.0

def np_to_audio_segment(y: np.ndarray, sr: int = 44100):
    y = np.clip(y, -1.0, 1.0)
    y_int16 = (y * 32767).astype(np.int16)
    return AudioSegment(y_int16.tobytes(), frame_rate=sr, sample_width=2, channels=1)

# ================= EQ FILTERING (PROFESSIONAL DJ MIXING) =================
def apply_lowpass_filter(audio: AudioSegment, cutoff_hz: float = 8000):
    """
    Apply low-pass filter to audio (cuts high frequencies).
    Used on OUTGOING track during fadeout to prevent frequency clash.
    """
    y = audio_segment_to_np(audio)
    sr = audio.frame_rate
    
    # Ensure cutoff is below Nyquist frequency (sr/2)
    nyquist = sr / 2.0
    cutoff_hz = min(cutoff_hz, nyquist * 0.95)  # 95% of Nyquist for safety
    
    # Design Butterworth low-pass filter
    sos = butter(4, cutoff_hz, btype='lowpass', fs=sr, output='sos')
    y_filtered = sosfilt(sos, y)
    
    return np_to_audio_segment(y_filtered, sr=sr)

def apply_highpass_filter(audio: AudioSegment, cutoff_hz: float = 200):
    """
    Apply high-pass filter to audio (cuts low frequencies/bass).
    Used on INCOMING track during intro to prevent bass clash.
    """
    y = audio_segment_to_np(audio)
    sr = audio.frame_rate
    
    # Ensure cutoff is valid (must be > 0 and < Nyquist)
    nyquist = sr / 2.0
    cutoff_hz = max(20, min(cutoff_hz, nyquist * 0.95))  # Between 20Hz and 95% Nyquist
    
    # Design Butterworth high-pass filter
    sos = butter(4, cutoff_hz, btype='highpass', fs=sr, output='sos')
    y_filtered = sosfilt(sos, y)
    
    return np_to_audio_segment(y_filtered, sr=sr)

def apply_progressive_eq(audio: AudioSegment, filter_type: str = "lowpass"):
    """
    Apply EQ filter that gradually increases in strength.
    Creates smooth transition instead of sudden frequency cut.
    """
    duration_ms = len(audio)
    if duration_ms < 100:
        return audio
    
    sr = audio.frame_rate
    nyquist = sr / 2.0
    num_chunks = 10
    chunk_size = duration_ms // num_chunks
    
    result = AudioSegment.empty()
    
    for i in range(num_chunks):
        start = i * chunk_size
        end = min((i + 1) * chunk_size, duration_ms)
        chunk = audio[start:end]
        
        # Gradually increase filter strength
        progress = (i + 1) / num_chunks
        
        if filter_type == "lowpass":
            # Start at 12kHz (or safe limit), end at 4kHz (or safe limit)
            max_cutoff = min(12000, nyquist * 0.95)
            min_cutoff = min(4000, nyquist * 0.5)
            cutoff = max_cutoff - ((max_cutoff - min_cutoff) * progress)
            filtered = apply_lowpass_filter(chunk, cutoff)
        else:  # highpass
            # Start at 100Hz, end at 300Hz
            cutoff = 100 + (200 * progress)
            filtered = apply_highpass_filter(chunk, cutoff)
        
        result += filtered
    
    return result


# ================= WAVEFORM PHASE ALIGNMENT (PROFESSIONAL DJ TECHNIQUE) =================
def align_waveform_phase(outgoing: AudioSegment, incoming: AudioSegment, max_shift_ms=50):
    """
    Align waveforms at the sample level using cross-correlation.
    This prevents phase cancellation when overlaying tracks.
    
    Professional DJs use this technique to ensure waveforms constructively interfere
    rather than cancel each other out (which causes volume drops/hollow sound).
    
    Process:
    1. Convert both audio segments to numpy arrays
    2. Use cross-correlation to find optimal phase alignment
    3. Shift incoming waveform by optimal offset
    4. Check for phase coherence after alignment
    
    Returns: (phase_aligned_incoming, shift_samples, coherence_score)
    """
    # Convert to numpy arrays for processing
    y_out = audio_segment_to_np(outgoing)
    y_in = audio_segment_to_np(incoming)
    sr = outgoing.frame_rate
    
    # Limit analysis to first few seconds for speed
    analysis_samples = min(sr * 5, len(y_out), len(y_in))  # 5 seconds max
    y_out_segment = y_out[:analysis_samples]
    y_in_segment = y_in[:analysis_samples]
    
    # Calculate max shift in samples
    max_shift_samples = int(sr * max_shift_ms / 1000)
    
    # Use cross-correlation to find optimal alignment
    # This finds where the two waveforms match best
    correlation = np.correlate(y_out_segment, y_in_segment, mode='same')
    
    # Find the lag that gives maximum correlation
    center = len(correlation) // 2
    search_range = min(max_shift_samples, center)
    search_start = center - search_range
    search_end = center + search_range
    
    search_correlation = correlation[search_start:search_end]
    optimal_lag_idx = np.argmax(np.abs(search_correlation))
    optimal_lag_samples = optimal_lag_idx - search_range
    
    # Apply shift to incoming audio
    if optimal_lag_samples > 0:
        # Add silence to beginning
        silence_duration_ms = int(optimal_lag_samples * 1000 / sr)
        aligned_incoming = AudioSegment.silent(silence_duration_ms) + incoming
    elif optimal_lag_samples < 0:
        # Trim from beginning
        trim_duration_ms = int(abs(optimal_lag_samples) * 1000 / sr)
        aligned_incoming = incoming[trim_duration_ms:]
    else:
        aligned_incoming = incoming
    
    # Calculate phase coherence score (0-1, higher is better)
    # This measures how well the waveforms align
    y_in_aligned = audio_segment_to_np(aligned_incoming)[:analysis_samples]
    if len(y_in_aligned) >= analysis_samples:
        # Normalize both signals for comparison
        y_out_norm = y_out_segment / (np.max(np.abs(y_out_segment)) + 1e-8)
        y_in_norm = y_in_aligned / (np.max(np.abs(y_in_aligned)) + 1e-8)
        
        # Calculate correlation coefficient (coherence)
        coherence = np.abs(np.corrcoef(y_out_norm, y_in_norm)[0, 1])
    else:
        coherence = 0.0
    
    shift_ms = optimal_lag_samples * 1000 / sr
    
    return aligned_incoming, shift_ms, coherence

def check_phase_cancellation(outgoing: AudioSegment, incoming: AudioSegment, overlap_start_ms=0):
    """
    Check if overlaying two audio segments will cause phase cancellation.
    Phase cancellation occurs when two waveforms are out of phase and cancel each other.
    
    Returns: (has_cancellation: bool, cancellation_severity: float)
    """
    # Extract overlap sections
    overlap_duration_ms = min(len(outgoing) - overlap_start_ms, len(incoming), 5000)  # Max 5s check
    if overlap_duration_ms <= 0:
        return False, 0.0
    
    y_out = audio_segment_to_np(outgoing[overlap_start_ms:overlap_start_ms + overlap_duration_ms])
    y_in = audio_segment_to_np(incoming[:overlap_duration_ms])
    
    # Normalize for fair comparison
    y_out = y_out / (np.max(np.abs(y_out)) + 1e-8)
    y_in = y_in / (np.max(np.abs(y_in)) + 1e-8)
    
    # Check if signals are approximately opposite (phase cancellation)
    # Negative correlation indicates phase opposition
    min_len = min(len(y_out), len(y_in))
    correlation = np.corrcoef(y_out[:min_len], y_in[:min_len])[0, 1]
    
    # Cancellation severity: -1 = complete opposition, 0 = no correlation, 1 = perfect alignment
    has_cancellation = correlation < -0.3  # Threshold for significant cancellation
    severity = abs(min(correlation, 0))  # Only negative correlations matter
    
    return has_cancellation, severity


def generate_synthetic_beat_grid(audio_seg: AudioSegment, bpm: float, first_beat_ms: float = None):
    """
    Generate a synthetic beat grid from known BPM starting at first beat position.
    
    This is MORE RELIABLE than librosa's beat detection which often:
    - Detects double-time (198 instead of 99 BPM)
    - Misses beats
    - Has inconsistent downbeat detection
    
    Args:
        audio_seg: Audio segment to analyze
        bpm: Known BPM of the track
        first_beat_ms: Where first beat starts (None = auto-detect)
    
    Returns: (beat_times_ms, downbeat_times_ms)
    """
    duration_ms = len(audio_seg)
    beat_interval_ms = 60000 / bpm  # milliseconds per beat
    
    # If first_beat_ms not provided, find it using onset detection
    if first_beat_ms is None:
        first_beat_ms = find_first_beat_onset(audio_seg, bpm)
    
    # Generate beat grid from first beat
    beats = []
    current_time = first_beat_ms
    while current_time < duration_ms:
        beats.append(current_time)
        current_time += beat_interval_ms
    
    beat_times_ms = np.array(beats)
    
    # Downbeats are every 4th beat (assuming 4/4 time)
    downbeat_times_ms = beat_times_ms[::4]
    
    return beat_times_ms, downbeat_times_ms


def find_first_beat_onset(audio_seg: AudioSegment, expected_bpm: float):
    """
    Find the first beat (musical content start) using onset detection.
    More reliable than librosa.beat.beat_track for finding the first beat.
    
    Returns: first_beat_ms
    """
    y = audio_segment_to_np(audio_seg)
    sr = audio_seg.frame_rate
    
    # Get onset envelope with bass emphasis (kicks are on beats)
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, aggregate=np.median)
    
    # Also check RMS energy for loud content
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    
    # Find first strong onset
    onset_threshold = np.percentile(onset_env, 70)  # Top 30% of onsets
    strong_onsets = np.where(onset_env > onset_threshold)[0]
    
    if len(strong_onsets) == 0:
        return 0  # No strong onsets found
    
    # First strong onset frame
    first_onset_frame = strong_onsets[0]
    
    # Also check RMS to make sure it's actual music, not a click
    rms_threshold = np.max(rms) * 0.1  # At least 10% of peak loudness
    loud_frames = np.where(rms > rms_threshold)[0]
    
    if len(loud_frames) > 0:
        # Use the earlier of: first strong onset or first loud frame
        first_loud_frame = loud_frames[0]
        first_frame = min(first_onset_frame, first_loud_frame)
    else:
        first_frame = first_onset_frame
    
    # Convert to time
    first_beat_ms = librosa.frames_to_time(first_frame, sr=sr, hop_length=512) * 1000
    
    # Round to nearest beat interval for cleaner alignment
    beat_interval_ms = 60000 / expected_bpm
    first_beat_ms = round(first_beat_ms / beat_interval_ms) * beat_interval_ms
    
    return max(0, first_beat_ms)

# ================= ADVANCED BEAT ALIGNMENT SYSTEM =================
def detect_beat_grid(audio_seg: AudioSegment, bpm=None):
    """
    Detect precise beat grid with downbeat detection using librosa.
    
    IMPROVED FOR ISSUES #1, #4:
    - Uses multiple methods to find true downbeats (beat 1 of each bar)
    - Ensures consistent bar-phase detection for alignment
    
    Returns beat times in milliseconds and downbeat positions.
    """
    y = audio_segment_to_np(audio_seg)
    sr = audio_seg.frame_rate
    
    # Use librosa's advanced beat tracking
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units='frames')
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    
    if len(beat_times) < 4:
        beat_times_ms = beat_times * 1000
        return beat_times_ms, beat_times_ms[:1] if len(beat_times) > 0 else np.array([]), tempo
    
    # IMPROVED DOWNBEAT DETECTION (FIX ISSUE #4)
    # Method 1: Use onset strength to find stronger beats
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    
    # Method 2: Use low-frequency (bass) content - kicks are on downbeats
    # Apply low-pass filter to isolate bass
    try:
        y_bass = librosa.effects.preemphasis(y, coef=-0.95)  # Emphasize low frequencies
        onset_bass = librosa.onset.onset_strength(y=y_bass, sr=sr)
    except:
        onset_bass = onset_env
    
    # Calculate beat strengths using combined methods
    beat_strengths = []
    bass_strengths = []
    for bt in beat_times:
        frame_idx = int(librosa.time_to_frames(bt, sr=sr))
        if frame_idx < len(onset_env):
            beat_strengths.append(onset_env[frame_idx])
            bass_strengths.append(onset_bass[frame_idx] if frame_idx < len(onset_bass) else onset_env[frame_idx])
        else:
            beat_strengths.append(0)
            bass_strengths.append(0)
    
    beat_strengths = np.array(beat_strengths)
    bass_strengths = np.array(bass_strengths)
    
    # Combined score: weight bass content higher (downbeats typically have stronger bass/kick)
    combined_strengths = 0.4 * beat_strengths + 0.6 * bass_strengths
    
    # CRITICAL FIX: Find the true "beat 1" position
    # Look at first 8 beats and find which position has highest average strength across bars
    bar_position_scores = [0.0, 0.0, 0.0, 0.0]  # Score for each beat position (1,2,3,4)
    
    for i in range(min(32, len(combined_strengths))):  # First 8 bars
        position = i % 4
        bar_position_scores[position] += combined_strengths[i]
    
    # The beat position with highest total score is likely "beat 1"
    true_beat_one_offset = np.argmax(bar_position_scores)
    
    # Identify downbeats (every 4th beat, starting from true beat 1)
    downbeat_indices = []
    for i in range(true_beat_one_offset, len(beat_times), 4):
        downbeat_indices.append(i)
    
    downbeat_times = beat_times[downbeat_indices] if len(downbeat_indices) > 0 else beat_times[::4]
    
    # Convert to milliseconds
    beat_times_ms = beat_times * 1000
    downbeat_times_ms = downbeat_times * 1000
    
    return beat_times_ms, downbeat_times_ms, tempo


def align_beats_perfect(outgoing: AudioSegment, incoming: AudioSegment, 
                        overlap_duration_ms: int, bpm_from: float, bpm_to: float,
                        track_names: tuple = ("Track A", "Track B")):
    """
    Perfect beat-to-beat alignment during overlap.
    
    Process (CORRECTED for Issue #1):
    1. Calculate ACTUAL BPM from beat intervals (not metadata)
    2. Time-stretch incoming to match outgoing FIRST
    3. THEN detect beats on the stretched audio
    4. Align downbeats in overlap region
    5. Generate waveform visualizations for DJ analysis
    
    Args:
        outgoing: Outgoing track audio
        incoming: Incoming track audio
        overlap_duration_ms: Duration of overlap
        bpm_from: BPM of outgoing track (metadata - will be verified)
        bpm_to: BPM of incoming track (metadata - will be verified)
        track_names: Tuple of (outgoing_name, incoming_name) for visualization
    
    Returns: (aligned_incoming, shift_ms)
    """
    print("   🎯 Applying perfect beat alignment...")
    
    # STEP 1: Detect beat grids to calculate ACTUAL BPM (not metadata)
    print("   → Detecting beats to calculate actual BPM...")
    try:
        outgoing_beats_initial, outgoing_downbeats_initial, _ = detect_beat_grid(outgoing, bpm_from)
        incoming_beats_initial, incoming_downbeats_initial, _ = detect_beat_grid(incoming, bpm_to)
    except Exception as e:
        print(f"   ⚠️  Beat detection failed: {e}, using basic alignment")
        return incoming, 0
    
    if len(outgoing_beats_initial) < 2 or len(incoming_beats_initial) < 2:
        print("   ⚠️  Insufficient beats detected, using basic alignment")
        return incoming, 0
    
    # STEP 2: Calculate detected BPM from beat intervals
    # IMPORTANT: librosa detection is often wrong - trust metadata more
    outgoing_intervals = np.diff(outgoing_beats_initial / 1000)  # Convert to seconds
    incoming_intervals = np.diff(incoming_beats_initial / 1000)
    
    period_out = np.median(outgoing_intervals)
    period_in = np.median(incoming_intervals)
    
    bpm_out_detected = 60 / period_out
    bpm_in_detected = 60 / period_in
    
    # IMPROVED BPM CORRECTION: Check multiple common librosa detection errors
    # Handles: 2x (double-time), 0.5x (half-time), 1.5x (triplet), 1.33x (4/3 ratio), etc.
    def correct_detected_bpm(detected, metadata, beats, downbeats, name):
        """Correct detected BPM, handling common librosa detection errors."""
        # Check common error ratios
        ratios = [1.0, 2.0, 0.5, 1.5, 0.75, 1.333, 0.666]
        
        best_ratio = 1.0
        best_diff = abs(detected - metadata)
        
        for ratio in ratios:
            corrected = detected / ratio
            diff = abs(corrected - metadata)
            if diff < best_diff:
                best_diff = diff
                best_ratio = ratio
        
        corrected_bpm = detected / best_ratio
        corrected_beats = beats
        corrected_downbeats = downbeats
        
        if best_ratio == 2.0:
            print(f"   ⚠️  {name}: Double-time detected ({detected:.1f} → {corrected_bpm:.1f} BPM)")
            corrected_beats = beats[::2]
            corrected_downbeats = downbeats[::2] if len(downbeats) > 0 else downbeats
        elif best_ratio != 1.0:
            print(f"   ⚠️  {name}: BPM corrected by {best_ratio}x ({detected:.1f} → {corrected_bpm:.1f} BPM)")
        
        # If still >10% off from metadata, trust metadata instead
        if abs(corrected_bpm - metadata) / metadata > 0.10:
            print(f"   ⚠️  {name}: Using metadata BPM {metadata:.1f} (detection {detected:.1f} unreliable)")
            return metadata, beats, downbeats
        
        return corrected_bpm, corrected_beats, corrected_downbeats
    
    bpm_out_actual, outgoing_beats_initial, outgoing_downbeats_initial = correct_detected_bpm(
        bpm_out_detected, bpm_from, outgoing_beats_initial, outgoing_downbeats_initial, "Outgoing")
    
    bpm_in_actual, incoming_beats_initial, incoming_downbeats_initial = correct_detected_bpm(
        bpm_in_detected, bpm_to, incoming_beats_initial, incoming_downbeats_initial, "Incoming")
    
    print(f"   → Final BPM values:")
    print(f"     Outgoing: {bpm_out_actual:.2f} BPM (metadata: {bpm_from:.1f}, detected: {bpm_out_detected:.1f})")
    print(f"     Incoming: {bpm_in_actual:.2f} BPM (metadata: {bpm_to:.1f}, detected: {bpm_in_detected:.1f})")
    
    # STEP 3: Time-stretch incoming to match outgoing (CRITICAL FIX)
    # This MUST happen BEFORE beat detection for alignment
    stretch_ratio = bpm_in_actual / bpm_out_actual
    
    if abs(stretch_ratio - 1.0) > 0.001:  # More than 0.1% difference
        print(f"   🎛️  Time-stretching incoming: {stretch_ratio:.4f}x ({bpm_in_actual:.2f} → {bpm_out_actual:.2f} BPM)")
        
        try:
            # Time-stretch the ENTIRE incoming track
            incoming = time_stretch_audio(incoming, stretch_ratio)
            print(f"   ✅ Incoming track stretched to match outgoing tempo")
        except Exception as e:
            print(f"   ⚠️  Time-stretch failed: {e}, proceeding without stretch")
            stretch_ratio = 1.0
    else:
        print(f"   → BPMs already matched (difference < 0.1%), no stretch needed")
    
    # STEP 4: Generate SYNTHETIC beat grids using known BPM (MORE RELIABLE than librosa)
    # librosa's beat detection is unreliable - it often detects double-time and has inconsistent results
    # Using synthetic grids from known BPM ensures beat counts match perfectly
    print(f"   → Generating synthetic beat grids at {bpm_out_actual:.1f} BPM...")
    try:
        # Generate beat grids from known BPM (finds first beat via onset detection)
        outgoing_beats, outgoing_downbeats = generate_synthetic_beat_grid(outgoing, bpm_out_actual)
        incoming_beats, incoming_downbeats = generate_synthetic_beat_grid(incoming, bpm_out_actual)
        
        print(f"   → Outgoing: {len(outgoing_beats)} beats, first beat at {outgoing_beats[0]:.1f}ms")
        print(f"   → Incoming: {len(incoming_beats)} beats, first beat at {incoming_beats[0]:.1f}ms")
                
    except Exception as e:
        print(f"   ⚠️  Beat grid generation failed: {e}")
        return incoming, 0
    
    # Verify beat counts now match
    overlap_beats_out = outgoing_beats[outgoing_beats < overlap_duration_ms]
    overlap_beats_in = incoming_beats[incoming_beats < overlap_duration_ms]
    
    print(f"   → Beat count in overlap region: Outgoing={len(overlap_beats_out)}, Incoming={len(overlap_beats_in)}")
    
    beat_count_diff = abs(len(overlap_beats_out) - len(overlap_beats_in))
    if beat_count_diff == 0:
        print(f"   ✅ Beat counts match perfectly - tempo sync successful!")
    elif beat_count_diff <= 2:
        print(f"   ✅ Beat counts match (±{beat_count_diff} beat tolerance) - tempo sync successful!")
    else:
        print(f"   ⚠️  WARNING: Beat counts differ by {beat_count_diff}!")
        # Additional diagnostics
        if len(overlap_beats_in) == 0:
            print(f"   → Incoming has no beats in overlap - first beat at {incoming_beats[0]:.0f}ms" if len(incoming_beats) > 0 else "   → No incoming beats detected at all!")
    
    if len(outgoing_downbeats) == 0 and len(outgoing_beats) > 0:
        # Use every 4th beat as downbeat fallback
        outgoing_downbeats = outgoing_beats[::4]
        print(f"   → No outgoing downbeats, using every 4th beat")
    
    if len(incoming_downbeats) == 0 and len(incoming_beats) > 0:
        # Use every 4th beat as downbeat fallback
        incoming_downbeats = incoming_beats[::4]
        print(f"   → No incoming downbeats, using every 4th beat")
    
    # NOTE: Visualization moved to AFTER alignment is applied (see Step 5b)
    
    # STEP 5: BAR-LEVEL ALIGNMENT (FIX ISSUE #4)
    # ==========================================================
    # CRITICAL: Align downbeat to downbeat DIRECTLY
    # Don't round to whole bars - just align the actual downbeat positions
    # ==========================================================
    
    # Calculate beat interval (should be the same for both tracks now)
    beat_interval_ms = 60000 / bpm_out_actual  # ms per beat
    bar_interval_ms = beat_interval_ms * 4     # ms per bar (4 beats)
    
    # Find the first downbeat in the overlap region for outgoing track
    outgoing_overlap_downbeats = outgoing_downbeats[outgoing_downbeats < overlap_duration_ms]
    
    # CRITICAL FIX: For incoming, we need a downbeat that exists (not necessarily in overlap)
    # If incoming's first downbeat is very late (long intro), we still use it
    if len(outgoing_overlap_downbeats) == 0:
        print("   ⚠️  No outgoing downbeats in overlap region, using first available")
        target_downbeat = outgoing_downbeats[0] if len(outgoing_downbeats) > 0 else outgoing_beats[0] if len(outgoing_beats) > 0 else 0
    else:
        target_downbeat = outgoing_overlap_downbeats[0]
    
    if len(incoming_downbeats) == 0:
        print("   ⚠️  No incoming downbeats detected, using first beat")
        incoming_first_downbeat = incoming_beats[0] if len(incoming_beats) > 0 else 0
    else:
        incoming_first_downbeat = incoming_downbeats[0]
    
    # SIMPLE DIRECT ALIGNMENT: Shift so incoming's first downbeat lands on target
    # This is the CORRECT approach - align downbeat to downbeat directly
    direct_shift = target_downbeat - incoming_first_downbeat
    
    # If incoming's first downbeat is very late (e.g., 10+ seconds into track),
    # we might want to use a later downbeat from outgoing instead
    if incoming_first_downbeat > overlap_duration_ms:
        print(f"   ⚠️  Incoming first downbeat at {incoming_first_downbeat:.0f}ms (after overlap region)")
        # Find an outgoing downbeat that would align with incoming starting from 0
        # We want: outgoing_downbeat = incoming_first_downbeat + shift
        # So we should pick an outgoing downbeat and calculate the required shift
        
        # Option: Use the incoming track from its start, align to nearest outgoing downbeat
        # Find which outgoing downbeat is closest to where incoming would be
        possible_targets = outgoing_downbeats[outgoing_downbeats < overlap_duration_ms + incoming_first_downbeat]
        if len(possible_targets) > 0:
            # Pick one that gives a clean bar alignment
            best_target = possible_targets[-1]  # Use latest one in range
            direct_shift = best_target - incoming_first_downbeat
            print(f"   → Using later target downbeat: {best_target:.1f}ms")
    
    # Apply the direct shift (no rounding to bars - we want exact downbeat alignment)
    shift_ms = direct_shift
    
    print(f"   → Downbeat alignment: {shift_ms:.1f}ms shift")
    print(f"     (Target: {target_downbeat:.1f}ms, Incoming first downbeat: {incoming_first_downbeat:.1f}ms)")
    print(f"     Bar interval: {bar_interval_ms:.1f}ms ({bpm_out_actual:.1f} BPM)")
    
    # Apply the shift
    if shift_ms > 0:
        incoming = AudioSegment.silent(int(shift_ms)) + incoming
        # Update beat positions for shifted audio
        incoming_beats = incoming_beats + shift_ms
        incoming_downbeats = incoming_downbeats + shift_ms
    elif shift_ms < 0:
        # Trim from beginning - but ensure we don't cut into actual music
        trim_amount = min(int(-shift_ms), max(0, len(incoming) - overlap_duration_ms - 1000))
        if trim_amount > 0:
            incoming = incoming[trim_amount:]
            # Update beat positions (they shift left)
            incoming_beats = np.array([b + shift_ms for b in incoming_beats if b + shift_ms >= 0])
            incoming_downbeats = np.array([b + shift_ms for b in incoming_downbeats if b + shift_ms >= 0])
    
    # VERIFY alignment - calculate actual positions after shift
    if len(outgoing_downbeats) > 0 and len(incoming_downbeats) > 0:
        out_first = outgoing_downbeats[0]
        # incoming_downbeats are already updated with the shift
        in_first_after_shift = incoming_downbeats[0] if len(incoming_downbeats) > 0 else 0
        
        alignment_error = abs(out_first - in_first_after_shift)
        
        # Check if error is within 1 beat
        if alignment_error < beat_interval_ms / 2:
            print(f"   ✅ Downbeat alignment verified: {alignment_error:.1f}ms error (excellent)")
        elif alignment_error < beat_interval_ms:
            print(f"   ✅ Downbeat alignment verified: {alignment_error:.1f}ms error (good)")
        else:
            # Check if it's off by whole bars (which might be acceptable)
            error_in_beats = alignment_error / beat_interval_ms
            print(f"   ⚠️  Alignment check: {alignment_error:.1f}ms error ({error_in_beats:.1f} beats)")
    
    # === STEP 5b: VISUALIZATION (AFTER alignment is applied) ===
    # This shows the ALIGNED beat positions, not the original positions
    if VISUALIZATION_ENABLED and len(outgoing_beats) > 0 and len(incoming_beats) > 0:
        try:
            plot_beat_alignment(outgoing, incoming, 
                              outgoing_beats, incoming_beats,
                              outgoing_downbeats, incoming_downbeats,
                              track_names)
        except Exception as e:
            print(f"   ⚠️  Beat visualization failed: {e}")
    
    # STEP 6: Beat grid warping for continuous sync
    # Only warp beats within the overlap duration
    overlap_beats_out = outgoing_beats[outgoing_beats < overlap_duration_ms]
    overlap_beats_in = incoming_beats[(incoming_beats >= 0) & (incoming_beats < overlap_duration_ms)]
    
    if len(overlap_beats_out) < 2 or len(overlap_beats_in) < 2:
        print(f"   → Basic alignment applied (shift: {shift_ms:.1f}ms)")
        return incoming, shift_ms
    
    # Match the number of beats to process
    num_beats = min(len(overlap_beats_out), len(overlap_beats_in))
    
    # Calculate beat-by-beat drift and apply micro corrections
    total_corrections = 0
    corrected_audio = incoming
    
    for i in range(1, num_beats):
        target_beat_time = overlap_beats_out[i]
        actual_beat_time = overlap_beats_in[i]
        drift_ms = actual_beat_time - target_beat_time
        
        # Apply micro time-stretch if drift exceeds threshold (10ms)
        if abs(drift_ms) > 10:
            # Calculate the segment to correct (from previous beat to this beat)
            segment_start = int(overlap_beats_in[i-1])
            segment_end = int(overlap_beats_in[i])
            
            if segment_end > segment_start and segment_end <= len(corrected_audio):
                segment = corrected_audio[segment_start:segment_end]
                
                # Calculate correction ratio
                target_duration = overlap_beats_out[i] - overlap_beats_out[i-1]
                actual_duration = segment_end - segment_start
                correction_ratio = target_duration / actual_duration
                
                # Limit correction to prevent artifacts (±5%)
                correction_ratio = np.clip(correction_ratio, 0.95, 1.05)
                
                # Apply micro time-stretch
                if abs(correction_ratio - 1.0) > 0.01:
                    try:
                        stretched_segment = time_stretch_audio(segment, correction_ratio)
                        
                        # Reconstruct audio with corrected segment
                        before = corrected_audio[:segment_start]
                        after = corrected_audio[segment_end:]
                        corrected_audio = before + stretched_segment + after
                        
                        total_corrections += 1
                    except:
                        pass  # Skip if stretch fails
    
    if total_corrections > 0:
        print(f"   → Beat grid warping: {total_corrections} micro-corrections applied")
    else:
        print(f"   → Beats already aligned (drift < 10ms)")
    
    # STEP 7: FINAL VERIFICATION
    # Quick check that the alignment is reasonable
    try:
        # Get beats from corrected audio
        check_length = min(overlap_duration_ms + 2000, len(corrected_audio))
        if check_length > 1000:
            corrected_beats_check, corrected_downbeats_check, _ = detect_beat_grid(
                corrected_audio[:check_length], 
                bpm_out_actual
            )
            
            # Fix double-time if detected
            if len(corrected_beats_check) > 1:
                detected_bpm = 60000 / np.median(np.diff(corrected_beats_check))
                if detected_bpm > bpm_out_actual * 1.8:
                    corrected_beats_check = corrected_beats_check[::2]
                    corrected_downbeats_check = corrected_downbeats_check[::2] if len(corrected_downbeats_check) > 0 else corrected_downbeats_check
            
            if len(corrected_downbeats_check) > 0 and len(outgoing_downbeats) > 0:
                # Simple check: is first corrected downbeat close to an outgoing downbeat?
                first_corrected = corrected_downbeats_check[0]
                closest_outgoing = outgoing_downbeats[np.argmin(np.abs(outgoing_downbeats - first_corrected))]
                error = abs(first_corrected - closest_outgoing)
                
                if error < beat_interval_ms / 2:
                    print(f"   ✅ Final alignment verified: {error:.1f}ms error (excellent)")
                elif error < beat_interval_ms:
                    print(f"   ✅ Final alignment verified: {error:.1f}ms error (good)")
                else:
                    print(f"   ⚠️  Final alignment: {error:.1f}ms to nearest outgoing downbeat")
    except Exception as e:
        print(f"   ⚠️  Final verification skipped: {e}")
    
    # STEP 3: WAVEFORM PHASE ALIGNMENT (Professional DJ technique)
    # After beat alignment, align waveforms at sample level to prevent phase cancellation
    print(f"   🌊 Applying waveform phase alignment...")
    
    # Use a section from both tracks for phase analysis
    phase_analysis_duration = min(overlap_duration_ms, 3000, len(outgoing), len(corrected_audio))
    outgoing_phase_section = outgoing[:phase_analysis_duration]
    incoming_phase_section = corrected_audio[:phase_analysis_duration]
    
    # Apply phase alignment
    phase_aligned, phase_shift_ms, coherence = align_waveform_phase(
        outgoing_phase_section,
        corrected_audio,
        max_shift_ms=20  # Max 20ms micro-adjustment for phase
    )
    
    # Check for phase cancellation
    has_cancellation, severity = check_phase_cancellation(
        outgoing,
        phase_aligned,
        overlap_start_ms=0
    )
    
    if has_cancellation:
        print(f"   ⚠️  Phase cancellation detected (severity: {severity:.2f})")
        print(f"   → Inverting phase to fix cancellation...")
        # Invert the phase of incoming track to fix cancellation
        y_inverted = audio_segment_to_np(phase_aligned) * -1
        phase_aligned = np_to_audio_segment(y_inverted, sr=phase_aligned.frame_rate)
        # Re-check
        has_cancellation, severity = check_phase_cancellation(outgoing, phase_aligned)
        if not has_cancellation:
            print(f"   ✅ Phase cancellation fixed!")
    
    print(f"   → Phase alignment: {phase_shift_ms:.2f}ms shift, coherence: {coherence:.3f}")
    
    # === VISUALIZATION: Waveform Phase Alignment ===
    if VISUALIZATION_ENABLED:
        try:
            plot_waveform_alignment(
                outgoing_phase_section, 
                phase_aligned[:phase_analysis_duration] if len(phase_aligned) >= phase_analysis_duration else phase_aligned,
                phase_analysis_duration, 
                track_names,
                phase_shift_ms, 
                coherence
            )
        except Exception as e:
            print(f"   ⚠️  Waveform visualization failed: {e}")
    
    # === VISUALIZATION: Phase Cancellation Check ===
    if VISUALIZATION_ENABLED and (has_cancellation or severity > 0.2):
        try:
            plot_phase_cancellation_check(
                outgoing,
                phase_aligned,
                has_cancellation, 
                severity,
                track_names
            )
        except Exception as e:
            print(f"   ⚠️  Phase check visualization failed: {e}")
    
    # Use phase-aligned version if coherence improved significantly
    if abs(phase_shift_ms) > 1.0:  # Only apply if shift is meaningful
        corrected_audio = phase_aligned
    
    return corrected_audio, shift_ms

# ================= TIME-STRETCH =================
def time_stretch_audio(audio: AudioSegment, factor: float):
    """
    Time-stretch AudioSegment without changing pitch using librosa.
    
    IMPORTANT: 'factor' represents the tempo ratio (incoming_bpm / outgoing_bpm):
    - factor > 1: incoming is FASTER, so we need to SLOW IT DOWN (stretch longer)
    - factor < 1: incoming is SLOWER, so we need to SPEED IT UP (compress shorter)
    
    librosa.effects.time_stretch uses 'rate' which works OPPOSITE:
    - rate > 1: audio gets FASTER (shorter duration)
    - rate < 1: audio gets SLOWER (longer duration)
    
    So we INVERT the factor: rate = 1.0 / factor
    """
    y = audio_segment_to_np(audio)
    # CRITICAL FIX: Invert the factor for librosa
    # If incoming is 1.0071x faster, we need rate=0.993 to slow it down
    rate = 1.0 / factor
    y_stretch = librosa.effects.time_stretch(y, rate=rate)
    return np_to_audio_segment(y_stretch, sr=audio.frame_rate)


# ================= ECHO EFFECT =================
def apply_echo_effect(audio: AudioSegment, echo_duration_ms: int = 3000, 
                      num_echoes: int = 4, decay_factor: float = 0.6):
    """
    Apply a decaying echo effect to audio - creates repeating fading echoes.
    
    Args:
        audio: AudioSegment to apply echo to
        echo_duration_ms: Total duration of the echo tail (default 3 seconds)
        num_echoes: Number of echo repeats (default 4)
        decay_factor: Volume multiplier per echo (0.6 = each echo is 60% of previous)
    
    Returns: AudioSegment with source audio + echo tail
    """
    if len(audio) == 0:
        return audio
    
    # Total output: original audio + echo tail
    total_duration = len(audio) + echo_duration_ms
    
    # Start with original audio fading out, plus silence for echo tail
    source_fading = audio.fade_out(len(audio))
    result = source_fading + AudioSegment.silent(echo_duration_ms)
    
    # Calculate delay between echoes (spread across echo duration)
    delay_ms = echo_duration_ms // (num_echoes + 1)
    
    # Add each echo - they start AFTER the source ends
    for i in range(1, num_echoes + 1):
        # Volume reduction: each echo is decay_factor^i of original
        volume_multiplier = decay_factor ** i
        volume_db = 10 * np.log10(volume_multiplier + 1e-10)  # Convert to dB (negative)
        
        # Create quieter copy of source
        echo_audio = audio + volume_db  # pydub uses + for dB adjustment
        echo_audio = echo_audio.fade_out(len(echo_audio))
        
        # Echo starts after source ends, with increasing delay
        echo_start = len(audio) + (delay_ms * (i - 1))
        
        # Create padded echo
        padded_echo = AudioSegment.silent(echo_start) + echo_audio
        
        # Trim or pad to match total duration
        if len(padded_echo) > total_duration:
            padded_echo = padded_echo[:total_duration]
        else:
            padded_echo = padded_echo + AudioSegment.silent(total_duration - len(padded_echo))
        
        # Overlay onto result
        result = result.overlay(padded_echo)
    
    print(f"   🔊 Echo effect: {num_echoes} echoes, {decay_factor:.0%} decay, {echo_duration_ms/1000:.1f}s tail")
    return result


def apply_echo_transition(outgoing_track: AudioSegment, incoming_track: AudioSegment,
                          first_chorus_end_ms: int, echo_duration_ms: int = 3000,
                          bpm_from: float = 120, bpm_to: float = 120,
                          track_names: tuple = ("Outgoing", "Incoming")):
    """
    Apply echo transition at the end of first chorus.
    
    TRANSITION FLOW:
    1. Outgoing track plays FULLY until first_chorus_end_ms
    2. Echo effect plays for 3 seconds (outgoing fades out with echo)
    3. THEN incoming track starts playing (AFTER the echo, not during)
    """
    print(f"   🎵 ECHO TRANSITION at {first_chorus_end_ms/1000:.1f}s")
    print(f"      Outgoing track length: {len(outgoing_track)/1000:.1f}s")
    print(f"      Incoming track length: {len(incoming_track)/1000:.1f}s")
    
    # Ensure chorus end point is valid
    if first_chorus_end_ms <= 0 or first_chorus_end_ms > len(outgoing_track):
        print(f"   ⚠️  Invalid chorus end point, using 60s default")
        first_chorus_end_ms = min(60000, len(outgoing_track) - echo_duration_ms)
    
    # ===== STEP 1: BPM MATCHING =====
    if bpm_from > 0 and bpm_to > 0 and abs(bpm_from - bpm_to) > 0.5:
        stretch_factor = bpm_to / bpm_from
        stretch_factor = np.clip(stretch_factor, 0.95, 1.05)
        if abs(stretch_factor - 1.0) > 0.005:
            print(f"   🎛️  BPM match: {bpm_to:.0f} → {bpm_from:.0f} (stretch {stretch_factor:.4f}x)")
            incoming_track = time_stretch_audio(incoming_track, stretch_factor)
    
    # ===== STEP 2: OUTGOING PLAYS FULLY UNTIL CHORUS END =====
    before_transition = outgoing_track[:first_chorus_end_ms]
    print(f"      Before transition: {len(before_transition)/1000:.1f}s")
    
    # ===== STEP 3: CREATE ECHO SECTION (3 seconds) =====
    # Take the last 1.5 seconds of the chorus as echo source
    echo_source_len = min(1500, first_chorus_end_ms)
    echo_source = outgoing_track[first_chorus_end_ms - echo_source_len : first_chorus_end_ms]
    
    # Apply echo effect - creates source + decaying echo tail
    echo_with_tail = apply_echo_effect(echo_source, echo_duration_ms=echo_duration_ms, num_echoes=4, decay_factor=0.5)
    
    # Get just the echo tail (the part after the source audio ends)
    echo_tail = echo_with_tail[echo_source_len:]
    
    # Apply low-pass filter for muffled/distant echo sound
    echo_tail_filtered = apply_progressive_eq(echo_tail, filter_type="lowpass")
    
    # Ensure echo is exactly the right length
    if len(echo_tail_filtered) > echo_duration_ms:
        echo_tail_filtered = echo_tail_filtered[:echo_duration_ms]
    elif len(echo_tail_filtered) < echo_duration_ms:
        echo_tail_filtered = echo_tail_filtered + AudioSegment.silent(echo_duration_ms - len(echo_tail_filtered))
    
    # Add extra fadeout to make it smooth
    echo_tail_filtered = echo_tail_filtered.fade_out(len(echo_tail_filtered))
    
    print(f"      Echo section: {len(echo_tail_filtered)/1000:.1f}s")
    
    # ===== STEP 4: INCOMING TRACK STARTS AFTER ECHO =====
    # Small fade in on incoming for smoothness
    incoming_with_fade = incoming_track.fade_in(min(500, len(incoming_track)))
    
    print(f"      Incoming track: {len(incoming_with_fade)/1000:.1f}s")
    
    # ===== STEP 5: FINAL ASSEMBLY =====
    # Outgoing → Echo (3s) → Incoming (sequential, no overlap)
    result = before_transition + echo_tail_filtered + incoming_with_fade
    
    print(f"   ✅ Echo transition done:")
    print(f"      Total: {len(result)/1000:.1f}s")
    print(f"      Outgoing → {first_chorus_end_ms/1000:.1f}s | Echo {echo_duration_ms/1000:.1f}s | THEN Incoming starts")
    
    return normalize(result)


# ================= TRANSITIONS =================
# ================= MAIN MIX =================
def generate_mix(mixing_plan_json: str = "data/output/mixing_plan.json", 
                structure_json: str = "data/output/structure_data.json",
                output_path: str = "data/output/mix.mp3"):
    print("Loading data...")
    with open(mixing_plan_json, "r", encoding="utf-8") as f:
        plan = json.load(f).get("mixing_plan", [])
    with open(structure_json, "r", encoding="utf-8") as f:
        structure_data = json.load(f)

    tracks_db = {}
    for section in structure_data.get("analyzed_setlist", []):
        for track in section.get("analyzed_tracks", []):
            tracks_db[track["title"]] = track

    if not plan:
        print("No mixing plan found!")
        return

    mix = AudioSegment.empty()
    previous_track_audio = None
    previous_track_bpm = 120
    
    # Track mix overview data for visualization
    mix_overview_data = []

    for idx, entry in enumerate(plan):
        to_title = entry.get("to_track")
        if not to_title or to_title not in tracks_db:
            print(f"  [SKIP] Missing track: {to_title}")
            continue

        to_meta = tracks_db[to_title]
        to_path = os.path.join(SONGS_DIR, to_meta.get("file",""))
        if not os.path.exists(to_path):
            print(f"  [ERROR] File not found: {to_path}")
            continue

        to_audio = AudioSegment.from_file(to_path)
        bpm_to = safe_float(to_meta.get("bpm", 120))

        # First track: just add it with fade in
        if idx == 0:
            print(f"\n1. {to_title} [Intro]")
            print(f"   Track duration: {len(to_audio)/1000:.1f}s")
            mix = to_audio.fade_in(ms(2))
            previous_track_audio = to_audio
            previous_track_bpm = bpm_to
            
            # Add to mix overview
            mix_overview_data.append({
                'name': to_title,
                'start_ms': 0,
                'duration_ms': len(to_audio),
                'bpm': bpm_to
            })
            continue

        # Get transition parameters from mixing plan
        from_title = entry.get("from_track")
        transition_type = entry.get("transition_type", "echo-transition")
        
        if not from_title or from_title not in tracks_db:
            print(f"\n  [WARN] from_track missing: {from_title}. Appending with crossfade.")
            mix = mix.append(to_audio, crossfade=2000)
            previous_track_audio = to_audio
            previous_track_bpm = bpm_to
            continue

        from_meta = tracks_db[from_title]
        bpm_from = previous_track_bpm
        
        print(f"\n{idx+1}. → {to_title}")
        print(f"   Transition type: {transition_type}")
        print(f"   Mix length before: {len(mix)/1000:.1f}s")
        
        # === ECHO TRANSITION (Boss's requirement) ===
        if transition_type == "echo-transition":
            # Get first chorus end from mixing plan
            first_chorus_end_sec = safe_float(entry.get("first_chorus_end_sec"), 
                                              entry.get("transition_point", 60.0))
            echo_duration_sec = safe_float(entry.get("echo_duration_sec"), 3.0)
            
            first_chorus_end_ms = ms(first_chorus_end_sec)
            echo_duration_ms = ms(echo_duration_sec)
            
            print(f"   First chorus end: {first_chorus_end_sec:.1f}s")
            print(f"   Echo duration: {echo_duration_sec:.1f}s")
            print(f"   BPM: {bpm_from:.0f} → {bpm_to:.0f}")
            
            # Apply echo transition using previous_track_audio
            if previous_track_audio:
                # Use apply_echo_transition function with track names for logging
                track_names = (from_title, to_title)
                transition_result = apply_echo_transition(
                    outgoing_track=previous_track_audio,
                    incoming_track=to_audio,
                    first_chorus_end_ms=first_chorus_end_ms,
                    echo_duration_ms=echo_duration_ms,
                    bpm_from=bpm_from,
                    bpm_to=bpm_to,
                    track_names=track_names
                )
                
                # The mix is built incrementally:
                # - Previous mix content (before this track's outgoing portion)
                # - New transition result replaces the outgoing track with transition
                
                # Find where previous track started in the mix
                # and replace from there with the new transition
                previous_track_start = len(mix) - len(previous_track_audio)
                
                # Keep everything before the previous track
                mix_before_previous = mix[:max(0, previous_track_start)]
                
                # Add the transition result (which includes outgoing->echo->incoming)
                mix = mix_before_previous + transition_result
                
                # Track where incoming starts for next iteration
                incoming_start_in_mix = previous_track_start + first_chorus_end_ms
                
                # Add to mix overview
                mix_overview_data.append({
                    'name': to_title,
                    'start_ms': incoming_start_in_mix,
                    'duration_ms': len(to_audio),
                    'bpm': bpm_to
                })
            else:
                # Fallback: just append with crossfade
                print("   [WARN] No previous track audio, using crossfade fallback")
                mix = mix.append(to_audio, crossfade=2000)
        else:
            # Fallback for other transition types (legacy support)
            print(f"   [INFO] Using crossfade for transition type: {transition_type}")
            mix = mix.append(to_audio, crossfade=3000)
        
        # Update tracking variables
        previous_track_audio = to_audio
        previous_track_bpm = bpm_to
        
        print(f"   Mix length after: {len(mix)/1000:.1f}s")

    # === GENERATE MIX OVERVIEW VISUALIZATION ===
    if VISUALIZATION_ENABLED and len(mix_overview_data) > 0:
        try:
            print("\n📊 Generating mix overview visualization...")
            plot_mix_overview(mix_overview_data)
        except Exception as e:
            print(f"⚠️  Mix overview visualization failed: {e}")

    print("\n" + "="*60)
    print("Normalizing & exporting final mix...")
    final_mix = normalize(mix)
    final_mix.export(output_path, format="mp3", bitrate="320k",
                     tags={"artist":"AI DJ","title":"Echo Transition Mix"})
    print(f"✅ MIX READY → {output_path} ({len(final_mix)/60000:.1f} minutes)")
    print("="*60)

if __name__ == "__main__":
    generate_mix()
