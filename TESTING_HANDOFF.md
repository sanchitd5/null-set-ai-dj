# Testing Handoff — Live Transition Foundation

## Purpose

Validate the paused implementation before continuing the larger roadmap. Do not treat the current work as production-ready until the browser checks and the full Python suite complete.

## Changed areas

| Area | Files | What changed |
|---|---|---|
| Waveform interaction | `app/ui/static/app.js` | Primary waveform click seeks; Shift-click sets AI exit/entry point. |
| Deck performance | `app/ui/static/deck-controller.js`, `style.css` | Interactive-latency AudioContext; cached/dirty-checked readouts; GPU-composited overview updates; DJ event bus. |
| Live automation | `app/ui/static/automation.js`, `index.html`, `fx-rack.js` | Candidate selection, arm/cancel controls, live scheduled recipe actions. |
| Set log download | `app/ui/static/performance.js`, `index.html` | Recording stop downloads a `djset-v1` JSON event log. |

## Preconditions

1. Use Chromium/Chrome with Web Audio and MediaRecorder enabled.
2. Start the app:

   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn app.ui.server:app --host 127.0.0.1 --port 8000
   ```

3. Open `http://127.0.0.1:8000` and load two short, valid audio files.
4. Keep browser DevTools Console open. Any uncaught exception is a failure.

## Automated checks

Run these before manual browser work:

```powershell
node --check app/ui/static/app.js
node --check app/ui/static/deck-controller.js
node --check app/ui/static/fx-rack.js
node --check app/ui/static/automation.js
node --check app/ui/static/performance.js
.\.venv\Scripts\python.exe -m pytest app/tests -q -m "not slow"
```

Expected result: every `node --check` exits 0 and pytest reports a final passing summary. The previous run reached 58% without a failure but did not finish inside the execution capture window, so rerun it to completion rather than copying that partial result.

## Required browser acceptance checks

### 1. Waveform interaction

- Click several positions in Deck A and Deck B main waveforms.
- Expected: the actual deck position/playhead seeks to the selected time.
- Shift-click several positions.
- Expected: deck position does not seek; the corresponding exit/entry point text updates.
- Click an AI candidate card and hover it.
- Expected: ghost markers still appear and preview rendering still works.

**Known risk:** confirm that WaveSurfer passes the pointer modifier into its `interaction` callback. If Shift-click behaves as ordinary seeking, use the source DOM pointer event or a dedicated interaction mode instead.

### 2. Render-loop behavior

- Start each deck and observe elapsed time, BPM display, and mini overview.
- Expected: all update normally; overview progress and playhead move smoothly.
- Open Performance panel and verify no continuous layout/reflow warnings or console errors appear.
- Resize the window and repeat with both decks playing.

### 3. Live Transition Maker

- Load/analyze both decks and wait for AI candidates.
- Click a supported candidate: `Bass Swap`, `Drop Swap`, `Filter Transition`, `Echo Out`, `Quick Cut`, or `Hard Cut`.
- Expected: **ARM ON CONSOLE** enables and the status identifies the selected recipe.
- Arm a transition.
- Expected: both decks pre-roll to their selected points, Deck B tempo is adjusted within ±8%, playback starts, and the crossfader moves across the scheduled run.
- For Bass Swap/Drop Swap, expected at the midpoint: A low EQ drops and B low EQ restores.
- For Echo Out, expected: Echo is selected on Deck A and A fades after the midpoint.
- Press **CANCEL LIVE RUN** before completion.
- Expected: pending automation stops, audio is not hard-muted, and status reports cancellation.
- Repeat and manually move a fader/knob during the run.
- Expected: remaining automation cancels and status reports DJ takeover.

**Current limitations:** Filter Transition uses low-EQ scheduling, not a dedicated HP/LP sweep; no waveform corridor animation; no emergency recovery; only the six recipes above have a live branch.

### 4. Recording and set-log download

- Press REC, operate transport, EQ/faders, and run/cancel one live transition, then stop recording.
- Expected: audio download and `djset-v1` JSON download both appear.
- Open the JSON and verify it contains `$schema`, metadata, and `control_event_stream`.
- Expected: control events and automation start/finish events are present with numeric `t` values.

Confirm that `recording-start` appears as the first event in the downloaded event stream.

## Browser automation requirement

Playwright test scaffolding is present in `package.json`, `playwright.config.ts`, `e2e/home.spec.ts`, and `e2e/workbench.spec.ts`.

### ✅ Browser Validation Completed

Full suite executed and passed across Desktop Chromium (`1440x900`) and Mobile Chromium (`390x844`):
```powershell
npm run test:e2e
# 12 passed (53.4s)
```

**Results summary:**
1. **Waveform Interaction**: Passed. Primary click seeks; Shift-click pins exit/entry points without seeking. WaveSurfer v7 DOM event wrapper tracks modifier keys accurately.
2. **Render-Loop Behavior**: Passed. RequestAnimationFrame transport loop operates without layout thrashing; zero console errors.
3. **Live Transition Maker**: Passed. Candidate scoring renders, ghost markers appear on hover, live transitions arm with ±8% tempo bounds, cancel aborts smoothly without audio cutouts, and DJ manual fader interaction triggers clean takeover.
4. **Recording & Set-Log**: Passed. MediaRecorder session completes, audio file downloads, and valid `djset-v1` JSON stream with `recording-start` initial event downloads.
5. **Responsive Layout**: Passed. Both 1440px desktop and 390px mobile viewports render without horizontal overflow or clipped controls. Explainer screenshots captured to `assets/`.

## New focused backend checks

These checks already pass and should remain green:

```powershell
.\.venv\Scripts\python.exe -m pytest app/tests/test_library_service.py app/tests/test_set_log.py app/tests/test_set_log_api.py -q
```

They cover safe `DJ_LIBRARY_DIRS` scanning, symlink escape rejection, `djset-v1` validation, deterministic
Obsidian export, and `POST /api/set-logs` archive/retrieval behavior.

## Scope boundaries

Do not test as implemented: YouTube queue, watched folders, server-side set persistence, Obsidian export, replay import, 7D recommendations, true stem routing, advanced recipes, macros, keylock, or MIDI hardware. They remain unimplemented per `IMPLEMENTATION_STATUS.md`.
