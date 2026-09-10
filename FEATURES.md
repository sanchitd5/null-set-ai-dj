# DJ Console — Feature Spec

What's actually implemented in `app/ui` (the console) and `app/music_brain` (the AI engine) today,
versus what's stubbed, disabled, or missing. Written so you can tell "real" from "placeholder" at a
glance. Last verified against the codebase on 2026-09-10 (124/124 tests passing, 0 skipped).

Legend: ✅ implemented and wired end-to-end · ⚠️ implemented but with a caveat · ❌ not implemented
(UI may exist but is disabled/absent, or there's no UI for it at all).

---

## 1. Playback engine (per deck)

| Feature | Status | Notes |
|---|---|---|
| Load track (file picker or browser) | ✅ | Web Audio decode, drives waveform + deck engine |
| Play / Pause / Cue | ✅ | |
| Reverse | ✅ | |
| Brake (turntable stop ramp) | ✅ | |
| Pitch fader (±8%) | ✅ | Also shifts pitch (see Keylock below) |
| Pitch bend (hold, ±2%) | ✅ | |
| Tap tempo (override analyzer BPM) | ✅ | Drives loop length, beatjump, tempo-synced FX, jog spin |
| Sync (match tempo to other deck) | ✅ | |
| Beatjump (±1/4/8/16 beats) | ✅ | |
| Loop (on/off, halve/double length) | ✅ | |
| Loop Roll pads (8 fixed lengths) | ✅ | |
| Hot cues (4 per deck + clear) | ✅ | |
| Hot cue markers drawn on waveform | ❌ | `#cues-a`/`#cues-b` containers exist in markup but nothing populates them for real hot cues (only the new AI ghost-markers use this container, see §3) |
| Slicer pads | ❌ | Tab is present but disabled — no beat-slicing DSP exists |
| **Keylock** | ❌ | Explicitly flagged N/A in the header. Raw `playbackRate` always shifts pitch with tempo; true keylock needs a phase-vocoder (e.g. SoundTouchJS), not implemented |
| Jog wheel | ⚠️ | Visual/seek only — a circular skeuomorphic turntable, not a scratch/XY control |

## 2. Mixer

| Feature | Status | Notes |
|---|---|---|
| 3-band EQ (Hi/Mid/Low) per channel | ✅ | |
| Trim (gain) per channel | ✅ | |
| Channel faders + VU meters | ✅ | |
| Crossfader (equal-power) | ✅ | |
| Master fader | ✅ | |
| Dedicated filter knob (resonant HP/LP sweep) | ❌ | Only reachable via the FX row's "FILTER" effect, not a dedicated per-channel knob |
| Mix recording → downloadable file | ✅ | MediaRecorder tap on the master bus, `.webm`, no backend round-trip |

## 3. AI Transition Brain (`app/music_brain`, exposed via `app/ui/server.py`)

| Feature | Status | Notes |
|---|---|---|
| Track analysis (BPM, beatgrid, Camelot key, phrases, sections, vocal presence) | ✅ | `POST /api/tracks`, `GET /api/tracks/{id}/analysis` |
| Auto-suggest transitions once both decks are loaded | ✅ | No button press — fires automatically on `deck-track-loaded`, scores all 28 recipes (`POST /api/match`), top 3 shown as cards |
| Manual point picking (click waveform to set exit/entry) | ✅ | Snaps to nearest 8-bar phrase boundary server-side |
| Manual recipe override (dropdown) | ✅ | |
| Ghost markers — hover a suggestion to preview where it would land | ✅ | Draws into the same `#cues-a`/`#cues-b` overlay containers hot cues leave unused (see §1) |
| Instant preview (15–30s audition) | ✅ | `POST /api/preview`, plays inline in the AI panel |
| **Render full mix (offline export)** | ✅ | `POST /api/render` → `render_full_mix()` in `transition_renderer.py`; was previously a dead button with no backend route — now wired end-to-end with a download link |
| Stem separation (Demucs, 4-stem/2-stem) | ✅ | `POST /api/tracks/{id}/separate`; now exposed as a **STEMS A** / **STEMS B** toggle in the AI panel (previously backend-only, no UI trigger at all) |
| Stem-based recipes actually consuming separated stems | ⚠️ | The stems endpoint runs and caches correctly, but `transition_renderer.py` only has dedicated DSP chains for Bass Swap / Drop Swap / Echo Out / Quick Cut / Hard Cut / Filter Transition — every other recipe (including the stem-based ones like Acapella Overlay, Stems Transition) falls back to `_generic_eq_blend`, which does **not** actually use the separated stem files. Separating stems today doesn't yet change how those recipes render |
| "Commit" — an AI suggestion drives the live crossfader/EQ in real time | ❌ | Discussed as a UX direction, not built. Today "preview" plays a rendered clip through a hidden `<audio>` element, not through the actual deck/crossfader/EQ chain |
| Live automation of console controls (any kind) | ❌ | Nothing in the console currently automates fader/EQ/crossfader movement over time — every transition is either a rendered clip (preview/render) or a manual human action |

## 4. Sampler & FX

| Feature | Status | Notes |
|---|---|---|
| 8-slot sampler bank (synthesized one-shots) | ✅ | Oscillator/noise-based, not sample playback — there is no sample library in this repo |
| Custom sample upload | ✅ | `POST /api/samples`, content-hashed |
| Per-deck insert FX (Filter/Echo/Reverb/Flanger/Phaser/Bitcrusher/Ping-Pong) | ✅ | Real Web Audio graph, one wet/dry knob per deck |
| Per-effect parameters beyond wet/dry | ❌ | Unlike some reference apps' per-effect param1/param2, there's a single shared wet knob only |

## 5. Track browser

| Feature | Status | Notes |
|---|---|---|
| List/search/sort your own uploaded tracks | ✅ | By name, BPM, key, energy |
| Genre browsing, streaming catalog | ❌ | Not a goal — this is a local-file tool, not a streaming product (see §7) |
| Favorites / playlists / AutoMix | ❌ | Not implemented |

## 6. Keyboard shortcuts

| Feature | Status | Notes |
|---|---|---|
| Mirrored per-deck keys (Q/A/Z/S/D... for A, P/;//... for B) | ✅ | Legacy scheme, still the default for every per-deck action |
| **Combo keys — same key, Shift picks the deck** | ✅ | New: `Space` = Play/Pause, `Enter` = Cue, `\` = FX on/off, `'` = Loop on/off. Plain = Deck A, Shift = Deck B. Lets you drive playback/live-FX one-handed without memorizing a second key per deck |
| **Editable/rebindable shortcuts** | ✅ | Click any key chip in the shortcut legend (`?` or `` ` ``), press a new key. Persists per-browser in `localStorage`. "RESET ALL TO DEFAULTS" reverts everything. Binding collisions are allowed but flagged in the status line (last-bound wins) |
| Sampler pad keys (T/Y/U/G/H/V/B/N) | ✅ | Also rebindable via the same system |

## 7. Deliberately out of scope

Per explicit direction: this is a local, personal/educational tool, not a monetized product. These are
not gaps to fill:

- Login / account system
- "Pro" paywall / unlock gating
- App-download CTA
- Streaming/Beatport-style catalog browsing

## 8. Known architectural gaps worth knowing about

- **No live automation engine.** Every "apply a transition" action today produces a rendered audio
  clip (preview or full render); nothing drives the actual crossfader/EQ/fader controls over time.
  Building that ("Commit" from the earlier UX discussion) is real DSP + UI work, not yet started.
- **Stem separation and stem-based recipe rendering are not yet connected.** You can separate a deck's
  stems and it caches correctly, but no renderer currently reads those stem files back in — every
  non-dedicated recipe (including the nominally stem-based ones) renders through the generic EQ-blend
  fallback instead.
- **Hot cues aren't drawn on the waveform.** The DOM containers exist and are now used by the AI ghost
  markers, but real hot cues remain pad-only with no on-waveform visualization.

## 9. In-progress live-console foundation (2026-09-10)

- **Waveform seek and AI point selection:** ✅ A normal main-waveform click seeks the deck. Shift-click
  pins the outgoing/incoming AI point, which the backend phrase-snaps when rendering or matching.
- **Live Transition Maker:** ⚠️ Candidate cards can arm a phrase-timed live console run. Bass Swap,
  Drop Swap, Filter Transition, Echo Out, Quick Cut, and Hard Cut schedule against the Web Audio clock;
  a manual trusted control move cancels remaining automation. It still needs browser/audio QA, a visual
  mix corridor, emergency recovery, and broader recipe coverage.
- **Set-log download and archive:** ⚠️ Stopping a browser recording offers a `djset-v1` JSON download with
  recording metadata and captured control/automation events. The backend validates and caches submitted logs
  with an Obsidian Markdown export; browser-side replay and richer journey capture are not implemented.
- **Configured local library scan:** ✅ `DJ_LIBRARY_DIRS` is a server-side semicolon-separated allowlist;
  `POST /api/library/scan` discovers supported audio files safely and makes them available to the browser.
- **Render-loop optimization:** ✅ Overview nodes and readouts are cached/dirty-checked, and overview
  updates use compositor transforms. The AudioContext requests interactive latency.
