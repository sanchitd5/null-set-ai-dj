// The live console's Host: the Host port (engine.js) built over the real window. The engine modules
// (tempo-rule, stem-moves, riff-over-rap, ai-actions, dj-mind, autopilot) load after this and mount
// themselves with it; nothing of them touches the window directly. The virtual set has its own host
// (app/sim/js/host-sim.js): same port, virtual clock and recording audio.
Engine.use(Engine.createWindowHost(window,
  () => audioCtx,                                        // deck-controller.js: the console's AudioContext
  () => loadIntoDeck,                                    // app.js: the single deck load path
  () => (typeof setStatus === "function" ? setStatus : null)));   // app.js: the status line
