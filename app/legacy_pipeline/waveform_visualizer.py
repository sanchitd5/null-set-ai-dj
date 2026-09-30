# waveform_visualizer.py
"""
Waveform Visualization System for DJ Mixing Analysis

Creates visual plots showing:
- Waveform alignment between tracks
- Beat grid synchronization
- Phase cancellation detection
- Mix quality metrics

Perfect for DJs who want to see the wave alignment during mixing.
"""

import os
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend for server environments
import matplotlib.pyplot as plt
from pydub import AudioSegment

# Output directory for waveform visualizations
WAVEFORM_OUTPUT_DIR = "./data/output/waveforms"

# Create output directory if it doesn't exist
if not os.path.exists(WAVEFORM_OUTPUT_DIR):
    os.makedirs(WAVEFORM_OUTPUT_DIR)

# ================= UTILITY FUNCTIONS =================
def audio_segment_to_np(seg: AudioSegment):
    """Convert AudioSegment to numpy array."""
    samples = np.array(seg.get_array_of_samples())
    if seg.channels == 2:
        samples = samples.reshape((-1, 2)).mean(axis=1)
    return samples.astype(np.float32) / 32768.0

# ================= WAVEFORM VISUALIZATION FUNCTIONS =================
def plot_waveform_alignment(outgoing: AudioSegment, incoming: AudioSegment, 
                           overlap_duration_ms: int, track_names: tuple,
                           phase_shift_ms: float = 0, coherence: float = 0,
                           output_filename: str = None):
    """
    Visualize waveform alignment between two tracks during overlap.
    Shows how the waves align at sample level - crucial for DJ mixing quality.
    
    Creates a plot showing:
    1. Outgoing track waveform (blue)
    2. Incoming track waveform (orange)
    3. Overlap region highlighting
    4. Phase alignment metrics
    
    Args:
        outgoing: Outgoing track audio
        incoming: Incoming track audio
        overlap_duration_ms: Duration of overlap in ms
        track_names: Tuple of (outgoing_name, incoming_name)
        phase_shift_ms: Phase alignment shift applied
        coherence: Waveform coherence score (0-1)
        output_filename: Custom filename (optional)
    
    Returns:
        Path to saved visualization
    """
    print(f"   📊 Generating waveform visualization...")
    
    # Convert to numpy arrays
    y_out = audio_segment_to_np(outgoing)
    y_in = audio_segment_to_np(incoming)
    sr = outgoing.frame_rate
    
    # Limit to overlap section + context (for clarity)
    context_duration_ms = 2000  # 2 seconds before/after
    total_duration_ms = overlap_duration_ms + (2 * context_duration_ms)
    total_samples = int(sr * total_duration_ms / 1000)
    
    # Extract sections
    y_out_section = y_out[:min(total_samples, len(y_out))]
    y_in_section = y_in[:min(total_samples, len(y_in))]
    
    # Time axis in seconds
    time_out = np.arange(len(y_out_section)) / sr
    time_in = np.arange(len(y_in_section)) / sr
    
    # Create figure with 3 subplots
    fig, axes = plt.subplots(3, 1, figsize=(16, 10))
    fig.suptitle(f'Waveform Alignment Analysis\n{track_names[0]} → {track_names[1]}', 
                 fontsize=16, fontweight='bold')
    
    # === SUBPLOT 1: Outgoing Track ===
    ax1 = axes[0]
    ax1.plot(time_out, y_out_section, color='#1f77b4', linewidth=0.5, alpha=0.8)
    ax1.fill_between(time_out, y_out_section, alpha=0.3, color='#1f77b4')
    ax1.set_ylabel('Amplitude', fontsize=11, fontweight='bold')
    ax1.set_title(f'Outgoing: {track_names[0]}', fontsize=12, fontweight='bold', loc='left')
    ax1.grid(True, alpha=0.3, linestyle='--')
    max_time = max(time_out[-1] if len(time_out) > 0 else 0, time_in[-1] if len(time_in) > 0 else 0)
    ax1.set_xlim(0, max_time if max_time > 0 else 10)
    ax1.set_ylim(-1.0, 1.0)
    
    # Highlight overlap region
    overlap_start_sec = context_duration_ms / 1000
    overlap_end_sec = (context_duration_ms + overlap_duration_ms) / 1000
    ax1.axvspan(overlap_start_sec, overlap_end_sec, alpha=0.2, color='green', 
                label=f'Overlap Zone ({overlap_duration_ms/1000:.1f}s)')
    ax1.legend(loc='upper right', fontsize=10)
    
    # === SUBPLOT 2: Incoming Track ===
    ax2 = axes[1]
    ax2.plot(time_in, y_in_section, color='#ff7f0e', linewidth=0.5, alpha=0.8)
    ax2.fill_between(time_in, y_in_section, alpha=0.3, color='#ff7f0e')
    ax2.set_ylabel('Amplitude', fontsize=11, fontweight='bold')
    ax2.set_title(f'Incoming: {track_names[1]}', fontsize=12, fontweight='bold', loc='left')
    ax2.grid(True, alpha=0.3, linestyle='--')
    ax2.set_xlim(0, max_time if max_time > 0 else 10)
    ax2.set_ylim(-1.0, 1.0)
    
    # Highlight overlap region
    ax2.axvspan(overlap_start_sec, overlap_end_sec, alpha=0.2, color='green',
                label=f'Overlap Zone ({overlap_duration_ms/1000:.1f}s)')
    ax2.legend(loc='upper right', fontsize=10)
    
    # === SUBPLOT 3: Overlaid Waveforms (Critical for DJ Analysis) ===
    ax3 = axes[2]
    
    # Plot both waveforms overlaid
    ax3.plot(time_out, y_out_section, color='#1f77b4', linewidth=0.7, alpha=0.7, 
             label=f'{track_names[0]} (Outgoing)')
    ax3.plot(time_in, y_in_section, color='#ff7f0e', linewidth=0.7, alpha=0.7,
             label=f'{track_names[1]} (Incoming)')
    
    ax3.set_xlabel('Time (seconds)', fontsize=11, fontweight='bold')
    ax3.set_ylabel('Amplitude', fontsize=11, fontweight='bold')
    ax3.set_title('Waveform Overlay - Phase Alignment Check', fontsize=12, fontweight='bold', loc='left')
    ax3.grid(True, alpha=0.3, linestyle='--')
    ax3.set_xlim(0, max_time if max_time > 0 else 10)
    ax3.set_ylim(-1.0, 1.0)
    
    # Highlight overlap region
    ax3.axvspan(overlap_start_sec, overlap_end_sec, alpha=0.15, color='green')
    
    # Add phase alignment metrics as text box
    metrics_text = f'Phase Alignment Metrics:\n'
    metrics_text += f'• Shift Applied: {phase_shift_ms:.2f} ms\n'
    metrics_text += f'• Coherence Score: {coherence:.3f}\n'
    
    if coherence >= 0.8:
        quality = "Excellent ✓"
        color = 'green'
    elif coherence >= 0.6:
        quality = "Good"
        color = 'orange'
    else:
        quality = "Poor - May Need Adjustment"
        color = 'red'
    
    metrics_text += f'• Alignment Quality: {quality}'
    
    props = dict(boxstyle='round', facecolor='wheat', alpha=0.8, edgecolor=color, linewidth=2)
    ax3.text(0.02, 0.97, metrics_text, transform=ax3.transAxes, fontsize=10,
             verticalalignment='top', bbox=props, family='monospace')
    
    ax3.legend(loc='upper right', fontsize=10)
    
    # Generate output filename
    if output_filename is None:
        safe_name1 = "".join(c for c in track_names[0][:30] if c.isalnum() or c in (' ', '_')).strip()
        safe_name2 = "".join(c for c in track_names[1][:30] if c.isalnum() or c in (' ', '_')).strip()
        output_filename = f"waveform_alignment_{safe_name1}_to_{safe_name2}.png"
    
    output_path = os.path.join(WAVEFORM_OUTPUT_DIR, output_filename)
    
    # Adjust layout and save
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    
    print(f"   ✅ Waveform visualization saved: {output_path}")
    return output_path

def plot_beat_alignment(outgoing: AudioSegment, incoming: AudioSegment,
                       outgoing_beats: np.ndarray, incoming_beats: np.ndarray,
                       outgoing_downbeats: np.ndarray, incoming_downbeats: np.ndarray,
                       track_names: tuple, output_filename: str = None):
    """
    Visualize beat grid alignment between two tracks.
    Shows beat markers overlaid on waveforms to verify beat-to-beat sync.
    
    Args:
        outgoing: Outgoing track audio
        incoming: Incoming track audio
        outgoing_beats: Beat times in ms for outgoing track
        incoming_beats: Beat times in ms for incoming track
        outgoing_downbeats: Downbeat times in ms for outgoing track
        incoming_downbeats: Downbeat times in ms for incoming track
        track_names: Tuple of (outgoing_name, incoming_name)
        output_filename: Custom filename (optional)
    
    Returns:
        Path to saved visualization
    """
    print(f"   📊 Generating beat alignment visualization...")
    
    # Convert to numpy arrays
    y_out = audio_segment_to_np(outgoing)
    y_in = audio_segment_to_np(incoming)
    sr = outgoing.frame_rate
    
    # Limit to first 15 seconds for clarity
    max_duration_sec = 15
    max_samples = int(sr * max_duration_sec)
    y_out_section = y_out[:min(max_samples, len(y_out))]
    y_in_section = y_in[:min(max_samples, len(y_in))]
    
    # Time axis
    time_out = np.arange(len(y_out_section)) / sr
    time_in = np.arange(len(y_in_section)) / sr
    
    # Create figure
    fig, axes = plt.subplots(2, 1, figsize=(16, 8))
    fig.suptitle(f'Beat Grid Alignment\n{track_names[0]} → {track_names[1]}', 
                 fontsize=16, fontweight='bold')
    
    # === SUBPLOT 1: Outgoing Track with Beat Markers ===
    ax1 = axes[0]
    ax1.plot(time_out, y_out_section, color='#1f77b4', linewidth=0.5, alpha=0.7)
    ax1.fill_between(time_out, y_out_section, alpha=0.2, color='#1f77b4')
    
    # Plot beat markers
    for beat_ms in outgoing_beats:
        beat_sec = beat_ms / 1000
        if beat_sec <= max_duration_sec:
            ax1.axvline(beat_sec, color='red', linewidth=1, alpha=0.5, linestyle='--')
    
    # Plot downbeat markers (stronger)
    for downbeat_ms in outgoing_downbeats:
        downbeat_sec = downbeat_ms / 1000
        if downbeat_sec <= max_duration_sec:
            ax1.axvline(downbeat_sec, color='darkred', linewidth=2, alpha=0.8)
    
    ax1.set_ylabel('Amplitude', fontsize=11, fontweight='bold')
    ax1.set_title(f'Outgoing: {track_names[0]} (Red lines = beats, Dark red = downbeats)', 
                  fontsize=12, fontweight='bold', loc='left')
    ax1.grid(True, alpha=0.3, linestyle='--')
    ax1.set_xlim(0, max_duration_sec)
    ax1.set_ylim(-1.0, 1.0)
    
    # === SUBPLOT 2: Incoming Track with Beat Markers ===
    ax2 = axes[1]
    ax2.plot(time_in, y_in_section, color='#ff7f0e', linewidth=0.5, alpha=0.7)
    ax2.fill_between(time_in, y_in_section, alpha=0.2, color='#ff7f0e')
    
    # Plot beat markers
    for beat_ms in incoming_beats:
        beat_sec = beat_ms / 1000
        if beat_sec <= max_duration_sec:
            ax2.axvline(beat_sec, color='red', linewidth=1, alpha=0.5, linestyle='--')
    
    # Plot downbeat markers (stronger)
    for downbeat_ms in incoming_downbeats:
        downbeat_sec = downbeat_ms / 1000
        if downbeat_sec <= max_duration_sec:
            ax2.axvline(downbeat_sec, color='darkred', linewidth=2, alpha=0.8)
    
    ax2.set_xlabel('Time (seconds)', fontsize=11, fontweight='bold')
    ax2.set_ylabel('Amplitude', fontsize=11, fontweight='bold')
    ax2.set_title(f'Incoming: {track_names[1]} (Red lines = beats, Dark red = downbeats)', 
                  fontsize=12, fontweight='bold', loc='left')
    ax2.grid(True, alpha=0.3, linestyle='--')
    ax2.set_xlim(0, max_duration_sec)
    ax2.set_ylim(-1.0, 1.0)
    
    # Add info text - COUNT ONLY BEATS WITHIN THE 15-SECOND DISPLAY WINDOW
    max_ms = max_duration_sec * 1000
    out_beats_in_view = len([b for b in outgoing_beats if 0 <= b <= max_ms])
    in_beats_in_view = len([b for b in incoming_beats if 0 <= b <= max_ms])
    
    info_text = f'Beat Alignment Analysis:\n'
    info_text += f'• Outgoing: {out_beats_in_view} beats in view\n'
    info_text += f'• Incoming: {in_beats_in_view} beats in view\n'
    info_text += f'• Downbeats should align vertically'
    
    props = dict(boxstyle='round', facecolor='lightblue', alpha=0.8)
    ax2.text(0.02, 0.97, info_text, transform=ax2.transAxes, fontsize=10,
             verticalalignment='top', bbox=props, family='monospace')
    
    # Generate output filename
    if output_filename is None:
        safe_name1 = "".join(c for c in track_names[0][:30] if c.isalnum() or c in (' ', '_')).strip()
        safe_name2 = "".join(c for c in track_names[1][:30] if c.isalnum() or c in (' ', '_')).strip()
        output_filename = f"beat_alignment_{safe_name1}_to_{safe_name2}.png"
    
    output_path = os.path.join(WAVEFORM_OUTPUT_DIR, output_filename)
    
    # Adjust layout and save
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    
    print(f"   ✅ Beat alignment visualization saved: {output_path}")
    return output_path

def plot_phase_cancellation_check(outgoing: AudioSegment, incoming: AudioSegment,
                                  has_cancellation: bool, severity: float,
                                  track_names: tuple, output_filename: str = None):
    """
    Visualize phase cancellation analysis between two tracks.
    Shows whether waveforms will constructively or destructively interfere.
    
    Args:
        outgoing: Outgoing track audio
        incoming: Incoming track audio
        has_cancellation: Whether phase cancellation was detected
        severity: Cancellation severity (0-1)
        track_names: Tuple of (outgoing_name, incoming_name)
        output_filename: Custom filename (optional)
    
    Returns:
        Path to saved visualization
    """
    print(f"   📊 Generating phase cancellation check...")
    
    # Convert to numpy arrays (first 5 seconds only)
    y_out = audio_segment_to_np(outgoing)[:int(outgoing.frame_rate * 5)]
    y_in = audio_segment_to_np(incoming)[:int(incoming.frame_rate * 5)]
    sr = outgoing.frame_rate
    
    # Normalize for comparison
    y_out_norm = y_out / (np.max(np.abs(y_out)) + 1e-8)
    y_in_norm = y_in / (np.max(np.abs(y_in)) + 1e-8)
    
    # Calculate sum (what happens when overlaid)
    min_len = min(len(y_out_norm), len(y_in_norm))
    y_sum = y_out_norm[:min_len] + y_in_norm[:min_len]
    
    # Time axis
    time = np.arange(min_len) / sr
    
    # Create figure
    fig, axes = plt.subplots(4, 1, figsize=(16, 10))
    fig.suptitle(f'Phase Cancellation Analysis\n{track_names[0]} + {track_names[1]}', 
                 fontsize=16, fontweight='bold')
    
    # === SUBPLOT 1: Outgoing Track ===
    ax1 = axes[0]
    ax1.plot(time, y_out_norm[:min_len], color='#1f77b4', linewidth=0.5, alpha=0.8)
    ax1.fill_between(time, y_out_norm[:min_len], alpha=0.3, color='#1f77b4')
    ax1.set_ylabel('Amplitude', fontsize=10, fontweight='bold')
    ax1.set_title(f'Track 1: {track_names[0]}', fontsize=11, fontweight='bold', loc='left')
    ax1.grid(True, alpha=0.3)
    ax1.set_xlim(0, 5)
    ax1.set_ylim(-1.2, 1.2)
    
    # === SUBPLOT 2: Incoming Track ===
    ax2 = axes[1]
    ax2.plot(time, y_in_norm[:min_len], color='#ff7f0e', linewidth=0.5, alpha=0.8)
    ax2.fill_between(time, y_in_norm[:min_len], alpha=0.3, color='#ff7f0e')
    ax2.set_ylabel('Amplitude', fontsize=10, fontweight='bold')
    ax2.set_title(f'Track 2: {track_names[1]}', fontsize=11, fontweight='bold', loc='left')
    ax2.grid(True, alpha=0.3)
    ax2.set_xlim(0, 5)
    ax2.set_ylim(-1.2, 1.2)
    
    # === SUBPLOT 3: Combined (Sum) ===
    ax3 = axes[2]
    color = 'green' if not has_cancellation else 'red'
    ax3.plot(time, y_sum, color=color, linewidth=0.5, alpha=0.8)
    ax3.fill_between(time, y_sum, alpha=0.3, color=color)
    ax3.set_ylabel('Amplitude', fontsize=10, fontweight='bold')
    ax3.set_title('Combined Signal (Track 1 + Track 2)', fontsize=11, fontweight='bold', loc='left')
    ax3.grid(True, alpha=0.3)
    ax3.set_xlim(0, 5)
    ax3.set_ylim(-2.0, 2.0)
    
    # Add reference lines for expected range
    ax3.axhline(1.0, color='gray', linestyle='--', linewidth=0.5, alpha=0.5, label='Expected max (no cancellation)')
    ax3.axhline(-1.0, color='gray', linestyle='--', linewidth=0.5, alpha=0.5)
    ax3.legend(loc='upper right', fontsize=9)
    
    # === SUBPLOT 4: Correlation Analysis ===
    ax4 = axes[3]
    
    # Calculate correlation coefficient over time (windowed)
    window_size = sr // 10  # 0.1 second windows
    correlations = []
    times_corr = []
    
    for i in range(0, min_len - window_size, window_size // 2):
        window_out = y_out_norm[i:i+window_size]
        window_in = y_in_norm[i:i+window_size]
        if len(window_out) == len(window_in) and len(window_out) > 0:
            corr = np.corrcoef(window_out, window_in)[0, 1]
            correlations.append(corr)
            times_corr.append((i + window_size/2) / sr)
    
    ax4.plot(times_corr, correlations, color='purple', linewidth=2, marker='o', markersize=3)
    ax4.axhline(0, color='black', linewidth=1, linestyle='-', alpha=0.3)
    ax4.axhline(-0.3, color='red', linewidth=1, linestyle='--', alpha=0.5, label='Cancellation threshold')
    ax4.fill_between(times_corr, correlations, 0, where=np.array(correlations) >= 0, 
                     alpha=0.3, color='green', label='Constructive')
    ax4.fill_between(times_corr, correlations, 0, where=np.array(correlations) < 0, 
                     alpha=0.3, color='red', label='Destructive')
    
    ax4.set_xlabel('Time (seconds)', fontsize=10, fontweight='bold')
    ax4.set_ylabel('Correlation', fontsize=10, fontweight='bold')
    ax4.set_title('Phase Correlation Over Time (+1 = perfect alignment, -1 = complete cancellation)', 
                  fontsize=11, fontweight='bold', loc='left')
    ax4.grid(True, alpha=0.3)
    ax4.set_xlim(0, 5)
    ax4.set_ylim(-1.1, 1.1)
    ax4.legend(loc='upper right', fontsize=9)
    
    # Add result text
    result_text = f'Phase Cancellation Check:\n'
    result_text += f'• Cancellation Detected: {"YES ⚠" if has_cancellation else "NO ✓"}\n'
    result_text += f'• Severity: {severity:.3f}\n'
    
    if has_cancellation:
        result_text += f'• Action: Phase inverted to fix'
        box_color = 'salmon'
    else:
        result_text += f'• Status: Safe to overlay'
        box_color = 'lightgreen'
    
    props = dict(boxstyle='round', facecolor=box_color, alpha=0.9, edgecolor='black', linewidth=2)
    ax4.text(0.02, 0.97, result_text, transform=ax4.transAxes, fontsize=10,
             verticalalignment='top', bbox=props, family='monospace', fontweight='bold')
    
    # Generate output filename
    if output_filename is None:
        safe_name1 = "".join(c for c in track_names[0][:30] if c.isalnum() or c in (' ', '_')).strip()
        safe_name2 = "".join(c for c in track_names[1][:30] if c.isalnum() or c in (' ', '_')).strip()
        output_filename = f"phase_check_{safe_name1}_to_{safe_name2}.png"
    
    output_path = os.path.join(WAVEFORM_OUTPUT_DIR, output_filename)
    
    # Adjust layout and save
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    
    print(f"   ✅ Phase cancellation check saved: {output_path}")
    return output_path

def plot_mix_overview(track_list: list, output_filename: str = "mix_overview.png"):
    """
    Create a high-level overview of the entire mix showing all tracks and transitions.
    
    Args:
        track_list: List of dicts with track info: {name, start_ms, duration_ms, bpm}
        output_filename: Output filename
    
    Returns:
        Path to saved visualization
    """
    print(f"   📊 Generating mix overview visualization...")
    
    if not track_list:
        print("   ⚠️  No tracks to visualize")
        return None
    
    # Create figure
    fig, ax = plt.subplots(figsize=(20, 8))
    fig.suptitle('Complete Mix Overview - Timeline', fontsize=16, fontweight='bold')
    
    # Color palette for tracks
    colors = plt.cm.tab20(np.linspace(0, 1, len(track_list)))
    
    # Plot each track as a horizontal bar
    for idx, track in enumerate(track_list):
        start_sec = track.get('start_ms', 0) / 1000
        duration_sec = track.get('duration_ms', 0) / 1000
        end_sec = start_sec + duration_sec
        
        # Draw track bar
        ax.barh(idx, duration_sec, left=start_sec, height=0.8, 
                color=colors[idx], alpha=0.7, edgecolor='black', linewidth=1)
        
        # Add track name
        track_name = track.get('name', f'Track {idx+1}')
        bpm = track.get('bpm', 0)
        label = f"{track_name}\n({bpm:.0f} BPM)" if bpm > 0 else track_name
        
        # Position label in middle of bar
        label_pos = start_sec + duration_sec / 2
        ax.text(label_pos, idx, label, ha='center', va='center', 
                fontsize=9, fontweight='bold', color='black',
                bbox=dict(boxstyle='round', facecolor='white', alpha=0.8, edgecolor='none'))
    
    # Formatting
    ax.set_xlabel('Time (seconds)', fontsize=12, fontweight='bold')
    ax.set_ylabel('Track Number', fontsize=12, fontweight='bold')
    ax.set_yticks(range(len(track_list)))
    ax.set_yticklabels([f"Track {i+1}" for i in range(len(track_list))])
    ax.grid(True, alpha=0.3, axis='x', linestyle='--')
    ax.set_xlim(0, max([t.get('start_ms', 0)/1000 + t.get('duration_ms', 0)/1000 for t in track_list]) + 10)
    
    # Add total duration info
    total_duration_sec = max([t.get('start_ms', 0) + t.get('duration_ms', 0) for t in track_list]) / 1000
    info_text = f'Total Mix Duration: {total_duration_sec/60:.1f} minutes ({total_duration_sec:.0f} seconds)\n'
    info_text += f'Number of Tracks: {len(track_list)}'
    
    props = dict(boxstyle='round', facecolor='lightblue', alpha=0.9, edgecolor='black', linewidth=2)
    ax.text(0.98, 0.98, info_text, transform=ax.transAxes, fontsize=11,
            verticalalignment='top', horizontalalignment='right', bbox=props, family='monospace')
    
    output_path = os.path.join(WAVEFORM_OUTPUT_DIR, output_filename)
    
    # Save
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    
    print(f"   ✅ Mix overview saved: {output_path}")
    return output_path
