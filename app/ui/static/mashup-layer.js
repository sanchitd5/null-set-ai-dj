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
  if (typeof audioCtx === "undefined") return;

  const LEVEL = 0.9;
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
    hp.frequency.value = 120;
    const env = audioCtx.createGain();
    env.gain.setValueAtTime(0.0001, when);
    env.gain.exponentialRampToValueAtTime(LEVEL, when + beat);
    env.gain.setValueAtTime(LEVEL, when + Math.max(beat, clipSecs - bar));
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
