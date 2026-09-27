"""Seed corpus for live_ear.py's Qwen-Omni prompt.

live_ear has no history of real past judgments yet (cold start), so this
module seeds it with synthesized example calls. They are invented, not logged:
each one is a worked application of a specific ./DJ/ passage (cited in its
"source") to the exact watchdog numbers live_ear.flags() reads, using the same
thresholds (SEAM_SHIFT_MS 20, GRID_ERR_MS 35, SEAM_CLICK_RATIO 4, peak >
-0.3 dBFS or clip events, low_clash, FATIGUE_S 60) and only the moves
live_ear.allowed() can offer (keep / move_loop / filter_wash / restore).

Every flag the watchdog can raise has 2-3 examples, plus the common
combinations, the washed / moved states and clean baselines. retrieve() is
plain tag overlap against the watchdog's own flags plus the washed / moved
state - no vector DB, no model call, fully local.

The examples are advisory prompt text only: they never change
rule_decision() or validate(); the watchdog's numbers stay the authority.
"""
from __future__ import annotations

_LOOPS = "04 - Core Techniques/Loops & Beat Jumps.md"
_EQ = "04 - Core Techniques/EQ & Frequency Management.md"
_ENERGY = "06 - Energy & Crowd/Energy Management & Dynamics.md"
_PHRASE = "02 - Music Theory/Phrasing & Structure.md"
_FX = "04 - Core Techniques/Effects (FX) Mastery.md"
_CRISIS = "06 - Energy & Crowd/Reading the Room & Crowd Psychology.md"

EXAMPLES: list[dict] = [
    # --- seam_phase: kicks after the wrap land off the grid (> 20 ms)
    {"tags": {"seam_phase"}, "source": _PHRASE,
     "text": "Seam 30 ms off the beat grid, kicks stumble on the wrap: OFF, move_loop - re-anchor "
             "one phrase earlier rather than patch a bad seam with EQ; a beat that lands late on "
             "every repeat is phrase clashing in miniature."},
    {"tags": {"seam_phase"}, "source": _LOOPS,
     "text": "Seam 22 ms late, just over the limit, groove still mostly locked: OFF, move_loop - "
             "a quantized loop must start on a real downbeat; a small shift is still heard as a "
             "flam every pass."},
    {"tags": {"seam_phase", "moved"}, "source": _LOOPS,
     "text": "Seam still 25 ms off after the loop was already re-anchored once: OFF, filter_wash "
             "if allowed - moving again would chase the grid; pull the lows so the stumble is "
             "masked until the next song drops on Beat 1."},
    # --- loop_length: loop drifts against the analysed downbeats (> 35 ms)
    {"tags": {"loop_length"}, "source": _LOOPS,
     "text": "Loop length runs 40 ms long against the analysed downbeats, drums drift audibly by "
             "the third pass: OFF, move_loop - loops must respect power-of-two beat lengths; "
             "grid error compounds every repeat."},
    {"tags": {"loop_length"}, "source": _LOOPS,
     "text": "Loop 60 ms short of the bar, audible as a skipped half-beat: OFF, move_loop - an "
             "uneven loop shifts the downbeat onto beat 2 or 4 and breaks the 8-bar phrasing."},
    {"tags": {"loop_length", "moved"}, "source": _LOOPS,
     "text": "Loop length still 38 ms off after one re-anchor: OFF, filter_wash - the grid itself "
             "is the problem, another move lands on the same error; hide the drift until the "
             "handoff."},
    # --- seam_click: level jump at the wrap (> 4x the loop's normal transient)
    {"tags": {"seam_click"}, "source": _LOOPS,
     "text": "Sharp click at the wrap, level jump 5x the loop's normal transient: OFF, move_loop "
             "- a click at the seam means the cut point itself is wrong, not a mix problem a wash "
             "can hide."},
    {"tags": {"seam_click"}, "source": _LOOPS,
     "text": "Faint tick at the seam, ratio 4.5x, on a hi-hat heavy loop: OFF, move_loop - the "
             "cut lands mid-transient; seamless loops should sound indistinguishable from the "
             "original groove."},
    {"tags": {"seam_click", "moved"}, "source": _EQ,
     "text": "Click at the wrap persists after a re-anchor, mostly in the highs: OFF, filter_wash "
             "if allowed - the wash pulls the mids and lows so the tick sits under the groove "
             "until the next track."},
    # --- clipping: peak > -0.3 dBFS or clip events
    {"tags": {"clipping"}, "source": _EQ,
     "text": "Peaks at -0.1 dBFS with occasional clip events on the snare: OFF, filter_wash - "
             "pulling energy down under a wash also pulls the clipping headroom back; never let "
             "the master sit in the red."},
    {"tags": {"clipping"}, "source": _EQ,
     "text": "Peak 0.0 dBFS, three clip events per pass, loop otherwise clean: OFF, filter_wash - "
             "redlining triggers limiting and distortion on the PA; cut the loud band rather "
             "than keep a distorted repeat."},
    {"tags": {"clipping", "washed"}, "source": _EQ,
     "text": "Still clipping at -0.2 dBFS after a wash is already on: OFF, keep - the wash is "
             "the only allowed fix and it is in; restoring the EQ now would bring the clipping "
             "straight back."},
    # --- low_clash: two basslines fighting under 120 Hz
    {"tags": {"low_clash"}, "source": _EQ,
     "text": "Two basslines audibly fighting under 120 Hz, low end sounds muddy and loses punch: "
             "OFF, filter_wash - the wash strips low end from the held loop so only one deck "
             "owns the sub-bass."},
    {"tags": {"low_clash"}, "source": "05 - Transition Cookbook/Bass Swap.md",
     "text": "Held loop's kick and the incoming track's sub both present, low end phasing and "
             "pumping: OFF, filter_wash - zero sub-bass overlap; one track's low end must be "
             "dialed down until the downbeat swap."},
    {"tags": {"low_clash", "washed"}, "source": _EQ,
     "text": "Low clash flag still set while the wash is on, residual rumble: OFF, keep - the "
             "wash already hands the low end to one deck; restore only once the flag clears."},
    # --- fatigue: hold loop older than 60 s
    {"tags": {"fatigue"}, "source": _ENERGY,
     "text": "Loop has been running 75 s, the floor has clearly heard the 8 bars repeat 9 times: "
             "OFF, move_loop first (fresh material), filter_wash once already moved - a hold that "
             "long reads as a flat line, not a deliberate energy arc."},
    {"tags": {"fatigue"}, "source": _ENERGY,
     "text": "Loop running 65 s but the section is a driving instrumental groove with no vocal "
             "hook: CLEAN, keep - a groove loop tolerates more repeats than a vocal hook before "
             "it bores the floor."},
    {"tags": {"fatigue"}, "source": _LOOPS,
     "text": "Vocal hook looped for 70 s, the same line over and over: OFF, move_loop - looping "
             "a repetitive vocal phrase for 48+ bars drives the crowd insane; shift to fresh "
             "material a phrase earlier."},
    {"tags": {"fatigue", "moved"}, "source": _ENERGY,
     "text": "Loop moved once and still running 90 s: OFF, filter_wash - new material is spent, "
             "a wash pulls density down so the repeat reads as a valley before the next drop."},
    {"tags": {"fatigue", "washed", "moved"}, "source": _ENERGY,
     "text": "Moved and washed, running 110 s: OFF, keep - every allowed move is used; hold "
             "steady, the next song is the fix. Contrast returns when it lands."},
    # --- combinations
    {"tags": {"seam_phase", "fatigue"}, "source": _LOOPS,
     "text": "Seam 25 ms off AND the loop has run 90 s: OFF, move_loop - fix the seam first (a "
             "bad seam is heard on every single repeat, worse than the fatigue of a clean one)."},
    {"tags": {"low_clash", "clipping"}, "source": _EQ,
     "text": "Low-end clash plus a couple of clip events: OFF, filter_wash - pulling lows and mids "
             "down over 4 bars resolves both at once (headroom returns, one deck owns sub-bass)."},
    {"tags": {"seam_click", "clipping"}, "source": _LOOPS,
     "text": "Click at the seam plus clipping on the same transient: OFF, move_loop - the click "
             "is the seam problem; treat the clipping as a symptom of it, not a separate issue to "
             "wash over."},
    {"tags": {"loop_length", "low_clash"}, "source": _LOOPS,
     "text": "Grid error 45 ms plus a low-end clash: OFF, move_loop - re-anchoring on the grid "
             "usually also lines the bassline back up; wash only if a retry still clashes."},
    {"tags": {"seam_phase", "seam_click"}, "source": _LOOPS,
     "text": "Seam 28 ms late with a thump at the wrap: OFF, move_loop - both point at the same "
             "wrong cut point; one re-anchor fixes both."},
    {"tags": {"fatigue", "low_clash"}, "source": _ENERGY,
     "text": "Running 80 s with two basslines rumbling: OFF, filter_wash - stripping the lows "
             "cures the clash and doubles as the breath before the next drop."},
    # --- washed state / clean baselines
    {"tags": {"clean_wash"}, "source": _EQ,
     "text": "Loop was washed two phrases ago to hide fatigue, no new flags have appeared since: "
             "CLEAN under the wash, restore - bring the EQ back once the problem it was hiding "
             "is gone."},
    {"tags": {"clean_wash"}, "source": _FX,
     "text": "Wash on, seam and levels now clean, loop 40 s old: CLEAN, restore - never leave a "
             "filter engaged over the groove unless it is deliberate; let the track breathe."},
    {"tags": set(), "source": _LOOPS,
     "text": "Loop wraps clean, kicks land exactly on the grid, no clipping, no low clash, "
             "running under a minute: CLEAN, keep - nothing here needs fixing."},
    {"tags": set(), "source": _CRISIS,
     "text": "All numbers inside limits, loop 30 s old on a steady groove: CLEAN, keep - never "
             "fiddle with a working loop; decisive and calm beats a panicked move."},
]


def retrieve(flags: list[str], washed: bool = False, n: int = 3,
             moved: bool = False) -> list[str]:
    """Up to n example judgments whose tags overlap the current watchdog
    flags (+ washed / moved state), most-overlapping first (ties keep corpus
    order). A clean loop with no flags falls back to the untagged "clean,
    keep" examples, or the clean_wash ones if a wash is on."""
    fset = set(flags or [])
    state = set()
    if washed:
        state.add("washed")
    if moved:
        state.add("moved")
    if washed and not fset:
        fset.add("clean_wash")
    scored = []
    for i, e in enumerate(EXAMPLES):
        hit = len(fset & e["tags"])
        if hit == 0:
            continue
        # state tags only refine the order, never admit an example on their own;
        # examples carrying a state the loop is not in rank below exact ones.
        extra = e["tags"] - fset - {"washed", "moved"}
        s_match = len(state & e["tags"])
        s_miss = len((e["tags"] & {"washed", "moved"}) - state)
        scored.append((-(hit * 4 + s_match * 2 - s_miss * 2 - len(extra)), i, e["text"]))
    if not scored:
        scored = [(0, i, e["text"]) for i, e in enumerate(EXAMPLES) if not e["tags"]]
    scored.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in scored[:n]]
