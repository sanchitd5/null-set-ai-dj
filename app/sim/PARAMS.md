# Layered-move parameters: constants versus the waveform

Inventory of the hard-coded numbers that shape RIFF x RAP, the mashup layer, merge, bridge, LAYER and hook drops
(line numbers at the commit that added this file). KB = a rule from `DJ/` or CLAUDE.md section 4, kept. GUESS = a
stand-in for a property of the two tracks, to be measured. "Done" rows read the audio through
`app/music_brain/analysis/waveform_params.py`, which measures each stem once (hash keyed, cached in
`CACHE_DIR/wf_profile`, 0.5 s hops, one FFT per hop) and logs `derived_params` (measured or fallback) so the sim counts them.
Every derived value has a clamp and falls back to the old constant only when the audio could not be read.

## Done

| Was (file:line) | Value | Stood in for | Now |
|---|---|---|---|
| `keylock.py:195` RAP_UNDER_RIFF_DB | rap 9 dB under the riff, full band | how much the riff masks the voice | `waveform_params.rap_offsets`: the rap sits 6 dB under the riff IN THE 300-3400 Hz BAND (the 9 dB stays exact for a riff with 3 dB of voice-band loss), clamped 5-13 dB. `a_riff_voice_db` measured from the rendered riff stem (`keylock.stretched_levels`, backfilled for old renders) |
| `riff-over-rap.js:63` RAP_LIFT | x1.5 in the mashup's 2nd half | how much the rap needs to come up | `rap_lift` from the same offset (0.4 x offset dB, clamped 2-5 dB); x1.5 only when unmeasured |
| `riff-over-rap.js` hold on at bar +11 of each 16 | last bar | a bar of rap worth looping | `waveform_params.pick_hold_bar`: bar 8-11 of the segment that is full of rap (>= 90 % of hops voiced, no breath) and loudest in the voice band |
| `riff-over-rap.js` A drops out at blend - 2 | last 2 bars | where the rap is alone and continuous | `pick_dropout_bar`: the 2-bar span of the last 8 bars with the most continuous, loudest rap |
| `mashup-layer.js:18` LEVEL | guest vocal at 0.9 | guest loudness against the host | `waveform_params.guest_level` per host entry: guest voice band level with the host's voice band at that entry, clamp 0.25-1.0 |
| `mashup-layer.js` high-pass 120 Hz | 120 Hz | who owns the low end | `sub_corner_hz`: where 90 % of the host's 20-300 Hz energy ends, floor 120 Hz (KB), cap 200 Hz |

## Kept (KB rules)

| File:line | Value | Rule |
|---|---|---|
| `config.py:28` SUB_BASS_CROSSOVER_HZ | 120 | one sub owner below 120 Hz (CLAUDE.md s4 item 2); every measured corner is >= this |
| `mashup.py:30`, `layer.py:41`, `merge.py:43`, `stem-moves.js:568` | key score >= 0.8 | Camelot table (s4 item 3) |
| `keylock.py:38-42` GROOVE/SOLO/MASHUP/BLEND bars, `layer.py:42-43`, `blend.py:34`, `preplan.py:35` | 8 / 16 / 32 bar lengths | 8-bar phrase snap (s4 item 1) |
| `tempo-rule.js:22`, `techniques.py:45-46` | 8 % key-lock, 6 % pitch | BPM policy (s4 item 4); another agent owns the cap |
| `mashup.py:29`, `blend.py:29` | 4 % / 8 % rate deviation | BPM policy |

## Still a guess, not converted (UNVERIFIED which would help)

| File:line | Value | Stands in for | Why not done here |
|---|---|---|---|
| `merge.py:42`, `stem-moves.js:77` MIN_RMS / INTRO_MIN_RMS | 0.01 absolute (-40 dBFS) | "this stem really plays" | JS and Python must agree (rule vectors); a relative floor changes selection rules another agent owns. `REMIX_REL_FLOOR` 0.15 (`stem-moves.js:503`) is already relative |
| `stem-moves.js:105-106` LEVEL_FLOOR_DB 8, AUDIBLE_GAIN 0.126 | 8 dB / -18 dB | audibility of the master | dead-air gates (silence rules of the loop agent) |
| `stem-moves.js:449` LIFT 1.25, `:529` HOOK_OTHER 0.35, `hook_drop.py:111` OTHER_DUCK 0.35 | +2 dB, duck to 0.35 | how far synths must duck for the voice | needs the voice vs synth band levels at the line; next candidate: `voice_db` of the vocals stem against `other` at the hook |
| `hook_drop.py:26-27` DEFAULT_HOLD_BARS 2, MAX 8 | 2 bars | how long the voice stays alone | lyric timing already drives it; the fallback is the guess |
| `layer.py:44` LAYER_MAX_VOCAL_CLASH 0.03 | 3 % overlap | vocal clash risk | time overlap only; a spectral clash (centroid / formant range of the two vocal stems, `centroid_hz` is already in the profile) is the next step |
| `mashup.py:31-32` MIN_GUEST 0.5, MAX_HOST 0.2 vocal coverage | 50 % / 20 % | usable phrase | gates, not levels |
| `blend.py:30` MAX_VOCAL_COVERAGE 0.15, `:32` ENTRY_SEARCH_FRACTION 0.45, `:33` ENERGY_MATCH_WEIGHT 1.5, `:36-38` drop line 0.2 / top quartile | see file | instrumental, entry depth, drop line | selection rules |
| `layer.py:45-46` GROOVE_MAX_CV 0.08, GROOVE_MIN_BEATS 16 | 8 % beat spread | steady groove | swing / onset stats (3-over-4, 2-over-3) would replace this and the riff tempo gate; needs onset stats in the analyzer, not built |
| `keylock.py:196` BASS_UNDER_RIFF_DB 9 | 9 dB | B's bass under the riff | `b_bass` is not used by the console schedule today |
| `keylock.py:197` MAX_A_BOOST_DB 8 | 8 dB | level match ceiling | headroom clamp (A peak -3 dB) is already measured |
| `keylock.py:86` SNAP_S 0.6, `:85` MIN_B_AFTER_S 60 | seconds | grid tolerance, room after | structure |
| `preplan.py:36-42` MAX_HANDOVERS, EAR_TOP, MIN_LEAD_S 20, PLAN_BUDGET_S 20 | see file | scheduling | timing, not audio |
| `dsp_rack.py:106` high_cut_hz 5000 | 5 kHz | mid / high split of the EQ preset | preset curve; a masking-driven EQ (which bands to cut from measured band energies of both tracks) is the next step |
| `tempo-rule.js:20-26` | ramp limits | tempo change speed | tempo policy |

## Sim metrics

`derived_params` events (server side, `waveform_params.note`) -> `report.json` metrics `wf_params_measured`,
`wf_params_fallback`, `wf_measured_share`, `wf_hold_fit`, `wf_hold_fit_fixed`, `wf_dropout_fit(_fixed)`;
`bass_overlap_seconds` and `vocal_clash_seconds` from the audible log. The played set rarely reaches an ok mashup plan
or RIFF x RAP (key, tempo and rap-section gates), so `wf_probe.py` also runs the same derivations over every track the
run loaded: `wfp_hold_fit` against `wfp_hold_fit_fixed` (share of the rap voiced under the bar the move picked versus
the old fixed bar), `wfp_dropout_fit(_fixed)`, `wfp_level_spread` (the old guest gain is one value; the derived ones
differ per pair), `wfp_measured` / `wfp_fallback`. Synthetic stems carry the real songs' per-second stem energy but
white-noise spectra, so gap and loudness numbers are real, band and low-end numbers only prove plumbing.
All are informational: none enters the score, none is a `HARD` gate.
