// The gradient rule (user: "tempo change should be gradual like a gradient,
// every move on master should follow a gradient"). One place decides how the
// AI may change a deck's tempo, and whether two tempos lock at all.
//
//  - A deck that reaches the master (playing, crossfader side open > 0.05,
//    volume > 0.05) only changes tempo by a ramp, at most MAX_TEMPO_PCT_PER_BAR.
//    0.25 %/bar = 1 % per 4-bar group: a DJ nudging the pitch fader through a
//    phrase, well under what the ear hears as "the tempo moved" (a 2 % step is
//    obvious; 2 % over 8 bars is not). Bigger changes simply take longer.
//  - A deck that is not heard may be set instantly (pre-start tempo lock).
//  - A beat-to-beat recipe needs the two tempos to lock inside the incoming
//    deck's range: PITCH_RANGE_PCT on the pitched mix (the console fader), or
//    KEYLOCK_RANGE_PCT when key-locked tempo stems rendered at exactly that
//    tempo are attached. Otherwise the transition is beatless (Stem Bridge /
//    Echo Out): [[Beatmatching & Tempo]], [[Genre Bridge Playbook]].
// Pure functions, node-testable (module.exports), browser global `tempoRule`.
(function (root) {
  "use strict";

  const MAX_TEMPO_PCT_PER_BAR = 0.25;   // 1 % per 4 bars
  const PITCH_RANGE_PCT = 8;            // console pitch fader (+-8 %)
  const KEYLOCK_RANGE_PCT = 8;          // key-locked stems: past ~8 % the stretch smears; bigger gaps go Echo Out / Stem Bridge
  const AUDIBLE_MIN = 0.05;             // crossfader side / volume gain floor
  const STILL_PCT = 0.05;               // below this a tempo change is no change
  const MIN_LEVEL_RAMP_BEATS = 1;       // EQ / volume / filter / crossfader
  const MIN_STEM_RAMP_BARS = 0.25;      // stem gains: a quarter bar

  // Does this deck reach the master right now?
  function isAudible(s) {
    if (!s || !s.playing) return false;
    const side = s.side == null ? 1 : s.side, vol = s.volume == null ? 1 : s.volume;
    return side > AUDIBLE_MIN && vol > AUDIBLE_MIN;
  }

  // Bars a glide from one pitch-% to another takes at the max rate.
  function glideBars(fromPct, toPct) {
    const d = Math.abs((toPct || 0) - (fromPct || 0));
    return d < STILL_PCT ? 0 : d / MAX_TEMPO_PCT_PER_BAR;
  }

  // Seconds for that glide, bars counted at the deck's heard tempo.
  function glideSeconds(fromPct, toPct, bpm, rate) {
    const bar = 240 / Math.max(1, (bpm || 128) * Math.max(0.05, rate || 1));
    return glideBars(fromPct, toPct) * bar;
  }

  // How the AI may move a deck's tempo: { instant: true } or { seconds }.
  function tempoMove(o) {
    const g = glideSeconds(o.fromPct, o.toPct, o.bpm, o.rate);
    if (!isAudible(o) || g === 0) return { instant: true, seconds: 0 };
    return { instant: false, seconds: g };
  }

  // Half / double time counts as a match. aEff: outgoing heard BPM.
  function lockRate(aEff, bBpm) {
    if (!(aEff > 0) || !(bBpm > 0)) return null;
    return [1, 2, 0.5].map((m) => aEff / (bBpm * m))
      .reduce((best, r) => (Math.abs(r - 1) < Math.abs(best - 1) ? r : best));
  }

  // Can B lock beat-to-beat to A right now? keyLocked: B has tempo stems
  // rendered for exactly this tempo (within 1 %), so its key does not move.
  function beatLock(o) {
    const r = lockRate(o.aEff, o.bBpm);
    if (r == null) return { ok: false, rate: 1, pct: 0, keyLocked: false, why: "no tempo" };
    const pct = (r - 1) * 100;
    const keyLocked = !!(o.tempoStemsBpm > 0) && Math.abs(o.tempoStemsBpm / (o.bBpm * r) - 1) < 0.01;
    const range = keyLocked ? KEYLOCK_RANGE_PCT : PITCH_RANGE_PCT;
    const ok = Math.abs(pct) <= range + 1e-9;
    return { ok, rate: r, pct, keyLocked, range,
      why: ok ? `${pct.toFixed(1)}% inside +-${range}%${keyLocked ? " (key-locked stems)" : ""}`
        : `${pct.toFixed(1)}% outside +-${range}%${keyLocked ? " (key-locked stems)" : ""}` };
  }

  // The recipe gate (ADDENDUM): beat-to-beat recipes (Long Blend, Bass Swap,
  // LAYER, PEAK, merge...) only when the tempos lock right now; else beatless.
  function beatRecipe(o) {
    const lock = beatLock(o);
    return { lock, beat: lock.ok, oneSong: !!o.stemsBoth && lock.ok,
      fallback: lock.ok ? null : (o.stemsBoth ? "Stem Bridge" : "Echo Out") };
  }

  // The pick-time / play-time shared gate: is a transition to B actually
  // smooth right now, or would it fall back to a hard/abrupt Echo Out?
  // Smooth = beat-matched lock (in range, or key-locked stems), or a proper
  // beatless bridge (Stem Bridge, stems on both decks). Same pure fn used
  // by evaluateCandidate (pick time) and scheduleTransition (play time) so
  // the two can never disagree: same inputs, same verdict.
  // o: { aEff, bBpm, stemsBoth, tempoStemsBpm }
  function planFit(o) {
    const lock = beatRecipe(o);
    const smooth = lock.beat || lock.fallback === "Stem Bridge";
    const plan = lock.beat ? (lock.oneSong ? "beat-matched (stems)" : "beat-matched") : lock.fallback;
    const why = lock.beat ? lock.lock.why
      : lock.fallback === "Stem Bridge" ? "tempo gap, stems on both decks: beatless Stem Bridge"
      : `tempo gap (${lock.lock.why}), no stems on both decks: only a hard Echo Out`;
    return Object.assign({ smooth, plan, why }, lock);
  }

  // A tempo lock that could still become a beat-to-beat blend once B's
  // key-locked stems render (the gap the stem render is worth waiting for).
  function stemLockable(aEff, bBpm) {
    const r = lockRate(aEff, bBpm);
    return r != null && Math.abs(r - 1) * 100 <= KEYLOCK_RANGE_PCT;
  }

  // Never push a deck past its range.
  function clampPitch(pct, keyLocked) {
    const range = keyLocked ? KEYLOCK_RANGE_PCT : PITCH_RANGE_PCT;
    return Math.max(-range, Math.min(range, pct || 0));
  }

  // Decide whether to wait for B's key-locked tempo stems to render before
  // deciding recipe. Renders take ~2s. Wait if: render in flight AND enough
  // time until transition (>= phraseS or can defer to next phrase boundary)
  // AND deferred transition doesn't outlive the outgoing track.
  // -> { wait: bool, why: string }
  function shouldWaitForTempoStems(o) {
    // o: { renderInFlight, transitionSeconds, phraseSeconds, songLeftSeconds }
    if (!o || !o.renderInFlight) return { wait: false, why: "no render in flight" };
    if (!(o.transitionSeconds > 0) || !o.phraseSeconds) return { wait: false, why: "no time info" };
    const renderTimeS = 2.5;     // conservative: ~2 s for Demucs stems, network varies
    const needS = renderTimeS;
    const deferableS = o.phraseSeconds;  // defer to next phrase boundary
    if (o.transitionSeconds >= needS) return { wait: true, why: `${needS.toFixed(1)}s render fits before transition` };
    const deferredS = o.transitionSeconds + deferableS;
    if (deferredS <= (o.songLeftSeconds || Infinity)) {
      return { wait: true, why: `defer to next phrase (${deferableS.toFixed(1)}s), render done before then, outgoing track OK` };
    }
    return { wait: false, why: `defer would outlive outgoing track (needs ${deferredS.toFixed(1)}s, have ${(o.songLeftSeconds || 0).toFixed(1)}s)` };
  }

  // Minimum ramp for any other AI move on an audible deck. A drop landing on
  // its downbeat (the slam is the musical hit) and silent decks are exempt.
  function minRampSeconds(kind, barS, o) {
    o = o || {};
    if (o.drop || !isAudible(o.deck || { playing: true })) return 0.005;
    return kind === "stem" ? barS * MIN_STEM_RAMP_BARS : barS / 4 * MIN_LEVEL_RAMP_BEATS;
  }

  const core = { MAX_TEMPO_PCT_PER_BAR, PITCH_RANGE_PCT, KEYLOCK_RANGE_PCT, AUDIBLE_MIN, STILL_PCT,
    MIN_LEVEL_RAMP_BEATS, MIN_STEM_RAMP_BARS,
    isAudible, glideBars, glideSeconds, tempoMove, lockRate, beatLock, beatRecipe, planFit, stemLockable, clampPitch, minRampSeconds, shouldWaitForTempoStems };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (root.Engine) root.Engine.mount("tempoRule", () => core);      // the Host port hands it to whoever asks (host.mod.tempoRule)
})(typeof window !== "undefined" ? window : globalThis);
