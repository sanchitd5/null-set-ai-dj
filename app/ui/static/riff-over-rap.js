// AI Music Brain - "riff over rap", live (USB002 1:06:00, Aerodynamic x Victory Lap Five).
//
// The server (app/music_brain/keylock.py) renders A's last groove + breakdown
// KEY-LOCKED at B's tempo. At the moment A reaches that groove on its own
// deck, A's normal playback stops and these stems take over, sample-locked,
// through A's channel strip (A's EQ and fader still apply):
//
//    The USB002 structure (measured) with the user's rules:
//    bar 0     A's groove, as is (none of B)
//    bar 16    the FIRST HALF of A's drop, clean: the set's break
//    bar 24    that half looped; B's RAP ONLY on top, well under A: the mashup
//              (B enters after its repeated opening hook; 16 bars, 32 if it keeps rapping)
//    bar 24+M  8-bar crossfade: B's backing fades in as A fades, bass swap at +4
//    bar 32+M  B carries on
//
// The engine reaches the world only through the Host port (engine.js): host.decks, host.audio, host.clock, host.api, host.bus.
(function (root) {
  "use strict";

  // Pure schedule: what happens at each bar. plan = /api/riff/plan answer.
  // User rules (2026-09-27): A's drop plays once AS IS before any mashup (that
  // creates the vibe); none of B before that; only B's rap on top of A, well
  // under it; the mashup runs longer when the vibe holds; no hard cuts.
  function schedule(plan) {
    const tl = plan.timeline || { break: 16, rap: 24, mashup: 24, blend: 40, swap: 44, end: 48, mashup_bars: 16 };
    const gb = plan.gains || { a_gain: 1, b_vocals: 1 };
    const rapOnly = { drums: 0, bass: 0, vocals: gb.b_vocals, other: 0 };
    const half = tl.swap - tl.blend, rest = tl.end - tl.swap;
    return {
      events: [
        { bar: 0, a: "groove", aGain: { drums: 1, bass: 1, vocals: 1, other: 1 },
          why: "A's groove, key-locked to B's tempo" },
        { bar: tl.break, a: "drop", why: "the first half of A's drop, clean: the break (as in the set)" },
        { bar: tl.mashup, a: "drop-half", b: "start", bStems: rapOnly,
          why: `the mashup: that half of A's drop looped, B's rap on top (${db(gb.b_vocals)} dB, ${tl.mashup_bars} bars)` },
        // second half of the mashup: the rap comes up x1.5 so A's music doesn't
        // bury it (user), ramped over 2 bars
        { bar: tl.mashup + tl.mashup_bars / 2, bRamp: { stems: { ...rapOnly, vocals: Math.min(1, gb.b_vocals * RAP_LIFT) }, bars: 2 },
          why: `the rap comes up for the second half of the mashup (${db(Math.min(1, gb.b_vocals * RAP_LIFT))} dB)` },
        // stem remix inside the mashup (user: stem separation remixing in mashups):
        // "hold on" at the end of each 16 bars: the rap's last bar looped over 4
        ...Array.from({ length: Math.floor(tl.mashup_bars / 16) }, (_, k) => {
          const s0 = tl.mashup + 16 * k;
          return { bar: s0 + 12, bHold: { stem: "vocals", fromBar: s0 + 11, bars: 1, untilBar: s0 + 16 },
                   why: "HOLD ON: the rap's last bar looped for 4 bars over the riff" };
        }),
        // a 32-bar mashup's last 2 bars: A drops out under the held rap, slams back on the line
        ...(tl.mashup_bars >= 32 ? [
          { bar: tl.blend - 2, aGain: { drums: 0, bass: 0, vocals: 0, other: 0 }, why: "A drops out: the rap alone for 2 bars" },
          { bar: tl.blend - 0.25, aGain: { drums: 1, bass: 1, vocals: 1, other: 1 }, why: "A slams back on the line" },
        ] : []),
        { bar: tl.blend, aRamp: { drums: 0, bars: half }, aRampOther: { other: 0, bars: half + rest },
          bRamp: { stems: { drums: 1, bass: 0, vocals: 1, other: 0.6 }, bars: half },
          why: "crossfade: A's drums out as B's drums come in, B's rap to full" },
        { bar: tl.swap, aGain: { bass: 0 }, bStems: { drums: 1, bass: 1, vocals: 1, other: 0.6 },
          bRamp: { stems: { drums: 1, bass: 1, vocals: 1, other: 1 }, bars: rest },
          why: "bass to B on the line, A's riff fades as B's synths rise" },
        { bar: tl.end, a: "stop", bStems: null, xfToB: 0, why: "B carries on" },
      ],
      totalBars: tl.end,
    };
  }
  const RAP_LIFT = 1.5;    // user: "raise it by half more" in the mashup's second segment
  function db(g) { return g >= 1 ? "0" : (20 * Math.log10(g)).toFixed(1); }
  // One in-flight request per key, successes kept, deterministic failures (no plan: gap/key)
  // kept for FAIL_TTL_MS so the same pair is not re-planned on every retry.
  const FAIL_TTL_MS = 10 * 60 * 1000;
  function memoPrepare(store, key, fn, now = Date.now) {
    const hit = store.get(key);
    if (hit) {
      if (hit.promise) return hit.promise;
      if (hit.failAt == null || now() - hit.failAt < FAIL_TTL_MS) return Promise.resolve(hit.value);
      store.delete(key);
    }
    const promise = Promise.resolve().then(fn).then((value) => {
      if (value && value.ok) store.set(key, { value });
      else if (value && value.deterministic) store.set(key, { value, failAt: now() });
      else store.delete(key);
      return value;
    }, (e) => { store.delete(key); throw e; });
    store.set(key, { promise });
    return promise;
  }
  const core = { schedule, memoPrepare };
  if (typeof module !== "undefined" && module.exports) module.exports = core;

  function create({ host }) {
  const { setTimeout } = host.clock;
  const audioCtx = host.audio;
  const fetch = (url, opts) => host.api.fetch(url, opts);
  const STEMS = ["drums", "bass", "vocals", "other"];
  const cache = new Map();          // key -> {plan, buffers}
  const note = (label, why) => host.bus.emit("ai-activity", { kind: "stem-move", deck: "", label, why });

  // Plan + render + decode. Resolves {ok:true, plan, buffers} or {ok:false, reasons}
  // — the reasons are always returned to the caller, not just console-logged, so
  // the UI can show the actual "why" instead of a bare "see the console".
  function prepare(aId, bId, notBefore) {
    return memoPrepare(cache, `${aId}>${bId}`, () => prepareUncached(aId, bId, notBefore), host.clock.now);
  }
  async function prepareUncached(aId, bId, notBefore) {
    const res = await fetch("/api/riff/plan", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ a_id: aId, b_id: bId, not_before: notBefore || 0 }) });
    if (!res.ok) return { ok: false, reasons: [`plan request failed: HTTP ${res.status}`] };
    const plan = await res.json();
    if (!plan.ok) {
      const reasons = plan.reasons || ["no reason given"];
      console.info("riff over rap: no -", reasons.join("; "));
      return { ok: false, reasons, deterministic: true };
    }
    for (let i = 0; i < 90 && plan.state !== "done"; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      const st = await (await fetch(`/api/riff/${plan.key}`)).json();
      plan.state = st.state;
      plan.meta = st.meta;
      if (String(st.state).startsWith("error")) {
        console.warn("riff over rap:", st.state);
        return { ok: false, reasons: [`render failed: ${st.state}`] };
      }
    }
    if (plan.state !== "done") return { ok: false, reasons: ["timed out waiting for the render"] };
    if (!plan.meta) plan.meta = (await (await fetch(`/api/riff/${plan.key}`)).json()).meta;
    if (plan.b_levels) {
      const r = await fetch(`/api/riff/${plan.key}/balance`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(plan.b_levels) });
      if (r.ok) plan.gains = await r.json();
    }
    const buffers = {};
    await Promise.all(STEMS.map(async (n) => {
      buffers[n] = await audioCtx.decodeAudioData(await (await fetch(plan.stems[n])).arrayBuffer());
    }));
    const out = { ok: true, plan, buffers };
    return out;
  }

  // Run it. t0 = audio time A reaches the groove start. ui = {xf(v), eq(deck, band, v), pitch(deck, pct)}.
  // Returns the run length in ms.
  function run(prep, outId, innId, t0, ui) {
    const { plan, buffers } = prep;
    const oa = host.decks[outId], ib = host.decks[innId];
    const bar = plan.bar_s, ratio = plan.meta.ratio, ws = plan.meta.window_start;
    const toStretch = (t) => (t - ws) * ratio;
    const gs0 = toStretch(plan.a_groove[0]);
    const ss0 = gs0 + 16 * bar;                       // groove end = breakdown start, on B's grid
    const at = (b) => t0 + b * bar;
    const { events, totalBars } = schedule(plan);
    const TL = plan.timeline || {};
    const BRK = TL.break || 16, MSH = TL.mashup || 24;
    const BLD = TL.blend || MSH + 16;
    const aGain = (plan.gains && plan.gains.a_gain) || 1;

    // A: stop its own playback exactly at t0, the stretched stems take over.
    // Through the deck so its liveness knows: from t0 A's own mix and stems are
    // silent, so no stem move may mute A's mix on the strength of them.
    if (oa.stopSourcesAt) oa.stopSourcesAt(t0);
    else for (const s of oa._allSources()) { try { s.stop(t0); } catch (e) { /* already stopped */ } s._stopAt = t0; }
    // Deck A keeps "playing": its clock follows the stretched stems back to song
    // time (groove loop, then the breakdown loop), so the deck never looks stopped.
    const g0 = plan.a_groove[0], s0 = plan.a_solo[0];   // groove start, drop start
    const extPos = (T) => {
      const e = Math.max(0, T - t0);
      if (e < MSH * bar) return g0 + e / ratio;                         // groove, then the drop's first half
      return s0 + ((e - MSH * bar) % (8 * bar)) / ratio;               // that half, looped
    };
    setTimeout(() => { oa._extPos = extPos; }, Math.max(0, (t0 - audioCtx.currentTime) * 1000));
    const gains = {};
    for (const n of STEMS) {
      const g = audioCtx.createGain();
      g.gain.setValueAtTime(aGain, t0);
      g.gain.__last = aGain;
      g.connect(oa.inputGain);
      gains[n] = g;
    }
    const srcs = [];
    const start = (offset, loopLen, when, until) => {
      for (const n of STEMS) {
        const s = audioCtx.createBufferSource();
        s.buffer = buffers[n];
        if (loopLen > 0) {
          s.loop = true;
          s.loopStart = offset;
          s.loopEnd = offset + loopLen;
        }
        s.connect(gains[n]);
        if (oa.stemMeter) s.connect(oa.stemMeter[n]);          // deck A's mini players show the riff stems
        s.start(when, offset);
        s.stop(until);
        srcs.push(s);
      }
    };
    // Groove and the first half of the drop play straight through (no loop
    // between two different grooves); then only that half loops: one groove.
    start(gs0, 0, t0, at(MSH));
    start(ss0, 8 * bar, at(MSH), at(totalBars) + 0.05);

    // B: native tempo, cued so its rap lands on bar 40.
    ui.pitch(innId, 0);
    // linearRamp from the previous scheduled point: no setValueAtTime(param.value)
    // (read early, it would jump the level back to a stale value)
    const ramp = (param, v, when, len) => { param.linearRampToValueAtTime(param.__last ?? param.value, when); param.linearRampToValueAtTime(v, when + len); param.__last = v; };
    const later = (when, fn) => setTimeout(fn, Math.max(0, (when - audioCtx.currentTime) * 1000 - 150));
    for (const e of events) {
      const T = at(e.bar);
      if (e.aGain) for (const [n, v] of Object.entries(e.aGain)) ramp(gains[n].gain, v * aGain, T, e.bar ? bar / 4 : 0.005);
      if (e.bRamp) later(T + 0.05, () => ib.stemMix(e.bRamp.stems, T + 0.02, e.bRamp.bars * bar));
      if (e.b === "start") {
        later(T - 0.35, () => {
          ib.play(plan.b_start, false, T);
          ib.stemMix(e.bStems, T - 0.01, 0.005);
        });
      } else if (e.bStems !== undefined && e.b !== "start") {
        later(T, () => ib.stemMix(e.bStems, T, e.bStems === null ? 0.02 : bar / 4));
      }
      if (e.bHold) {
        const h = e.bHold, T0 = at(h.fromBar + 1), T1 = at(h.untilBar);
        // B's song time for render bar X: B started at plan.b_start on the mashup line
        const from = plan.b_start + (h.fromBar - MSH) * bar;
        later(T0, () => { if (ib.playing) ib.holdStem(h.stem, from, h.bars, T0, T1); });
      }
      if (e.aFade) for (const n of STEMS) ramp(gains[n].gain, 0, T, e.aFade * bar);
      if (e.aRamp) for (const [n, v] of Object.entries(e.aRamp)) if (n !== "bars") ramp(gains[n].gain, v * aGain, T, e.aRamp.bars * bar);
      if (e.aRampOther) { ramp(gains.other.gain, 0, T, e.aRampOther.bars * bar); ramp(gains.vocals.gain, 0, T, e.aRampOther.bars * bar); }
      if (e.xfToB === 0) { /* the sweep below already landed on B */ }
      if (e.xfToB) later(T, () => {
        const steps = 16;
        for (let i = 1; i <= steps; i++) setTimeout(() => ui.xf(innId, i / steps), (e.xfToB * bar * 1000 * i) / steps);
      });
      later(T, () => note(`RIFF OVER RAP · bar ${e.bar}`, e.why));
    }
    // both channels open, both lows open (bass ownership is done with the stems)
    // EQs flat at the start; the crossfader moves only in smooth sweeps (user):
    // A side -> centre while A's drop-half plays alone (B still silent), then
    // centre -> B across the final crossfade. Never a jump.
    later(t0, () => { ui.eq(outId, "low", 0); ui.eq(innId, "low", 0); ui.eq(innId, "mid", 0); ui.eq(innId, "high", 0); });
    const sweep = (fromF, toF, T0, secs) => {
      const steps = 24;
      for (let i = 1; i <= steps; i++) {
        later(T0 + (secs * i) / steps, () => ui.xf(innId, fromF + (toF - fromF) * (i / steps)));
      }
    };
    sweep(-1, 0, at(BRK), (MSH - BRK) * bar);
    sweep(0, 1, at(BLD), (totalBars - BLD) * bar);
    later(t0, () => { oa._meterGain = gains; });
    later(at(totalBars) + 0.1, () => {
      oa._meterGain = null;
      const endPos = extPos(audioCtx.currentTime);
      oa._extPos = null;
      // drop the stopped sources too: afterBlend's stopNow() is a no-op on a deck
      // that no longer plays, so they'd linger in source/_stemSrc and pass for stems
      if (oa._stopSource) oa._stopSource();
      if (oa.stemState && oa._fallbackToMix) oa._fallbackToMix();
      oa.playing = false;
      oa.startOffset = endPos;
      if (oa.onPlayStateChange) oa.onPlayStateChange(false);
      for (const g of Object.values(gains)) g.disconnect();
    });
    // the auto sampler marks B's rap arriving (never A's drop: the user wants it untouched)
    host.bus.emit("ai-cue", { at: at(MSH), kind: "line", deck: innId, bar: bar, why: "B's rap arrives: open hat on the line" });
        note("RIFF OVER RAP", `${Math.round((1 / ratio - 1) * 1000) / 10}% key-locked: A's groove at B's ${plan.target_bpm} BPM, B's rap at ${Math.floor(plan.b_entry / 60)}:${String(Math.floor(plan.b_entry % 60)).padStart(2, "0")}`);
    return (at(totalBars) - audioCtx.currentTime) * 1000;
  }

  return { core, prepare, run };
  }
  if (root.Engine) root.Engine.mount("riffOverRap", create);
})(typeof window !== "undefined" ? window : globalThis);
