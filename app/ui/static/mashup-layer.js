// AI Music Brain — live mashup layer ("A x B").
//
// Plays a trimmed, separated vocal of one track (guest) over the playing deck
// (host), tempo-locked through playbackRate and started exactly on a host
// 8-bar phrase boundary chosen by POST /api/mashup/plan. The backend only picks
// host phrases without their own vocal, so two vocals never fight.
//
// Signal path: vocal clip -> high-pass 120 Hz (no second sub, see
// [[EQ & Frequency Management]]) -> gain envelope (1-beat fade in, 1-bar fade
// out) -> masterGain.
//
// Depends on globals: audioCtx, masterGain, decks (deck-controller.js).
// Public API: window.mashup.

(function () {
  const LEVEL = 0.9;          // used only when the server sent no measured gain (waveform_params.guest_level)
  const HP_HZ = 120;          // the KB sub crossover: the floor of any measured corner

  // Gain and high-pass corner of the guest vocal for the chosen host entry: measured by the server from the
  // two tracks' audio (level per host entry, corner from the host's own low end), the old constants otherwise.
  function layerParams(plan, hostEntry) {
    let level = LEVEL, hp = HP_HZ, measured = false;
    const es = plan.host_entries, ls = plan.host_levels;
    if (Array.isArray(es) && Array.isArray(ls) && es.length === ls.length && es.length) {
      let bi = 0;
      es.forEach((e, i) => { if (Math.abs(e - hostEntry) < Math.abs(es[bi] - hostEntry)) bi = i; });
      if (ls[bi] > 0 && ls[bi] <= 1) { level = ls[bi]; measured = !(plan.param_sources && plan.param_sources.guest_level === "fallback"); }
    }
    if (plan.hp_hz >= HP_HZ && plan.hp_hz <= 400) hp = plan.hp_hz;
    return { level, hp, measured };
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { layerParams };

  if (typeof audioCtx === "undefined") return;
  let current = null; // { src, env, deckId, endAt }

  function cancel() {
    if (!current) return;
    const { src, env } = current;
    const t = audioCtx.currentTime;
    try {
      env.gain.cancelScheduledValues(t);
      env.gain.setTargetAtTime(0.0001, t, 0.08);
      src.stop(t + 0.5);
    } catch (_) { /* already stopped */ }
    current = null;
  }

  async function loadClip(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`vocal clip ${res.status}`);
    return audioCtx.decodeAudioData(await res.arrayBuffer());
  }

  // Schedule `plan` (from /api/mashup/plan) on deck `deckId` at host time
  // `hostEntry` (seconds into the host track). Resolves true when booked.
  async function play(deckId, plan, hostEntry) {
    const d = window.decks && window.decks[deckId];
    if (!d || !d.playing) return false;
    const buffer = await loadClip(plan.guest_vocal_url);

    const deckRate = d._playbackRate();
    const pos = d._currentPosition();
    const lead = (hostEntry - pos) / deckRate;
    if (lead < 0.25) return false; // phrase already passed while loading

    cancel();
    const when = audioCtx.currentTime + lead;
    const rate = plan.rate * deckRate; // follow the host pitch fader too
    const clipSecs = buffer.duration / rate;
    const beat = 60 / (plan.host_bpm * deckRate);
    const bar = beat * 4;

    const src = audioCtx.createBufferSource();
    src.buffer = buffer;
    src.playbackRate.value = rate;
    const hp = audioCtx.createBiquadFilter();
    hp.type = "highpass";
    const lp = layerParams(plan, hostEntry);
    hp.frequency.value = lp.hp;
    const env = audioCtx.createGain();
    env.gain.setValueAtTime(0.0001, when);
    env.gain.exponentialRampToValueAtTime(lp.level, when + beat);
    env.gain.setValueAtTime(lp.level, when + Math.max(beat, clipSecs - bar));
    env.gain.exponentialRampToValueAtTime(0.0001, when + clipSecs);
    src.connect(hp).connect(env).connect(masterGain);
    src.start(when);
    src.stop(when + clipSecs + 0.05);
    const entry = { src, env, deckId, endAt: when + clipSecs };
    current = entry;
    src.onended = () => { if (current === entry) current = null; };
    return true;
  }

  window.mashup = {
    play,
    cancel,
    get active() { return !!current; },
  };
})();
