// AI Music Brain - live ear: listens to the master bus while a HOLD LOOP runs.
//
// Two layers (see app/ui/live_ear.py):
//  1. DSP watchdog, every 250 ms while the hold loop plays: loop start vs the
//     analysed downbeat, loop length vs the real 8-bar span, level jump at each
//     loop seam (click), master peak / clipping, two decks owning the low end,
//     and how long the loop has run. Cheap: deck state + a 16 kHz mono ring.
//  2. One decision per 8-bar phrase (sooner when the watchdog flags a new
//     problem): numbers + the last 8 s of master audio go to /api/live/ear, where
//     Qwen3-Omni listens and proposes one move; the server's rules answer when
//     the model is off or wrong. The move lands through djMind.holdLoopAct on
//     the next loop wrap.
//
// Capture runs in an AudioWorklet (off the main thread, PERFORMANCE_AUDIT.md);
// the main thread only copies 4096-sample chunks into the ring.
// Depends on globals: audioCtx, masterGain, decks (deck-controller.js), djMind.
(function (root) {
  "use strict";

  const RATE = 16000;             // capture rate: Qwen3-Omni takes 16 kHz audio
  const RING_S = 12;
  const CLIP_S = 8;               // seconds sent per decision
  const TICK_MS = 250;
  const REQ_TIMEOUT_MS = 12000;
  // Same thresholds as app/ui/live_ear.py flags().
  const SEAM_SHIFT_MS = 20, GRID_ERR_MS = 35, SEAM_CLICK_RATIO = 4, FATIGUE_S = 60;
  const LOW_OPEN_DB = -10;        // a low EQ above this still carries sub-bass

  // ---------------------------------------------------------- pure core --
  // ms between `t` and the nearest downbeat (null without a grid).
  function gridOffsetMs(downbeats, t) {
    if (!downbeats || !downbeats.length) return null;
    let best = Infinity;
    for (const x of downbeats) { const d = Math.abs(x - t); if (d < best) best = d; else if (x > t) break; }
    return best * 1000;
  }
  // Loop length vs the real span of `bars` downbeats from the loop start (ms).
  function loopLengthErrMs(downbeats, start, bars, barSecs) {
    if (!downbeats || downbeats.length < 2) return null;
    let i = 0, best = Infinity;
    downbeats.forEach((x, k) => { const d = Math.abs(x - start); if (d < best) { best = d; i = k; } });
    const j = i + bars;
    if (j >= downbeats.length) return null;
    return Math.abs(downbeats[j] - downbeats[i] - bars * barSecs) * 1000;
  }
  // RMS per `frame` samples.
  function frameRms(samples, frame) {
    const n = Math.floor(samples.length / frame), out = new Float32Array(n);
    for (let f = 0; f < n; f++) {
      let s = 0;
      for (let k = f * frame; k < (f + 1) * frame; k++) s += samples[k] * samples[k];
      out[f] = Math.sqrt(s / frame);
    }
    return out;
  }
  // Level jump at the loop seams vs the clip's median frame-to-frame jump.
  // seamIdx: sample indices of each wrap inside `samples`. 1 = seam is typical.
  function seamClickRatio(samples, seamIdx, frame) {
    const rms = frameRms(samples, frame);
    if (rms.length < 8 || !seamIdx.length) return null;
    const jumps = [];
    for (let f = 1; f < rms.length; f++) jumps.push(Math.abs(rms[f] - rms[f - 1]));
    const sorted = jumps.slice().sort((a, b) => a - b);
    const median = sorted[Math.floor(sorted.length / 2)] || 1e-6;
    let worst = 0;
    for (const i of seamIdx) {
      const f = Math.round(i / frame);
      if (f < 1 || f >= rms.length) continue;
      worst = Math.max(worst, Math.abs(rms[f] - rms[f - 1]), f + 1 < rms.length ? Math.abs(rms[f + 1] - rms[f]) : 0);
    }
    return worst / Math.max(median, 1e-6);
  }
  function flags(m) {
    const out = [];
    if ((m.seam_shift_ms || 0) > SEAM_SHIFT_MS) out.push("seam_phase");
    if ((m.grid_err_ms || 0) > GRID_ERR_MS) out.push("loop_length");
    if ((m.seam_click_ratio || 0) > SEAM_CLICK_RATIO) out.push("seam_click");
    if ((m.peak_dbfs != null && m.peak_dbfs > -0.3) || (m.clip_events || 0) > 0) out.push("clipping");
    if (m.low_clash) out.push("low_clash");
    if ((m.secs_looping || 0) > FATIGUE_S) out.push("fatigue");
    return out;
  }
  // 16-bit PCM mono WAV.
  function wavBytes(samples, rate) {
    const buf = new ArrayBuffer(44 + samples.length * 2), v = new DataView(buf);
    const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    str(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); str(8, "WAVE");
    str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    str(36, "data"); v.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
      const x = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true);
    }
    return new Uint8Array(buf);
  }
  // ---- silent pre-check: score a loop seam from the buffer, never played --
  // At the wrap the listener hears x[start..] where the song would have gone
  // on with x[end..]. Seamless = those two stretches sound alike: same
  // envelope, same brightness, same level, no sample jump. 1 = identical.
  function envelopes(x, sr, from, to) {
    const frame = Math.max(1, Math.round(sr / 100));         // 10 ms
    const a = Math.max(0, Math.floor(from * sr)), b = Math.min(x.length, Math.floor(to * sr));
    const n = Math.floor((b - a) / frame), env = new Float32Array(n), hp = new Float32Array(n);
    for (let f = 0; f < n; f++) {
      let s = 0, h = 0;
      for (let k = a + f * frame; k < a + (f + 1) * frame; k++) {
        const v = x[k], d = k > 0 ? v - x[k - 1] : 0;         // first difference ~ high-pass
        s += v * v; h += d * d;
      }
      env[f] = Math.sqrt(s / frame); hp[f] = Math.sqrt(h / frame);
    }
    return { env, hp };
  }
  function corr(a, b) {
    const n = Math.min(a.length, b.length);
    if (n < 8) return 0;
    let ma = 0, mb = 0;
    for (let i = 0; i < n; i++) { ma += a[i]; mb += b[i]; }
    ma /= n; mb /= n;
    let sab = 0, saa = 0, sbb = 0;
    for (let i = 0; i < n; i++) { const p = a[i] - ma, q = b[i] - mb; sab += p * q; saa += p * p; sbb += q * q; }
    return saa > 0 && sbb > 0 ? sab / Math.sqrt(saa * sbb) : 0;
  }
  function meanDb(e) { let s = 0; for (const v of e) s += v * v; return 10 * Math.log10(Math.max(s / Math.max(1, e.length), 1e-12)); }
  // x: mono samples, sr: rate, start/end: loop span (s), win: compare window (s).
  function seamScore(x, sr, start, end, win) {
    const dur = x.length / sr;
    // Compare what follows each edge; near the track end compare what precedes them.
    const after = end + win <= dur;
    const A = after ? envelopes(x, sr, end, end + win) : envelopes(x, sr, end - win, end);
    const B = after ? envelopes(x, sr, start, start + win) : envelopes(x, sr, start - win, start);
    const shape = Math.max(0, (corr(A.env, B.env) + corr(A.hp, B.hp)) / 2);
    const levelDb = Math.abs(meanDb(A.env) - meanDb(B.env));
    const iE = Math.min(x.length - 1, Math.max(1, Math.round(end * sr))), iS = Math.min(x.length - 1, Math.round(start * sr));
    const localRms = Math.max(1e-4, Math.sqrt(A.env.reduce((s, v) => s + v * v, 0) / Math.max(1, A.env.length)));
    const jump = Math.abs(x[iS] - x[iE - 1]) / localRms;      // sample step at the join
    const score = shape * Math.exp(-levelDb / 6) * (jump > 3 ? 0.8 : 1);
    return { score: Math.round(score * 1000) / 1000, shape: Math.round(shape * 1000) / 1000,
             level_db: Math.round(levelDb * 10) / 10, jump: Math.round(jump * 10) / 10 };
  }
  // Mono mixdown of an AudioBuffer-like {numberOfChannels, getChannelData}.
  function mono(buffer) {
    if (buffer._earMono) return buffer._earMono;
    const L = buffer.getChannelData(0), R = buffer.numberOfChannels > 1 ? buffer.getChannelData(1) : L;
    const m = new Float32Array(L.length);
    for (let i = 0; i < L.length; i++) m[i] = (L[i] + R[i]) * 0.5;
    try { buffer._earMono = m; } catch (e) { /* frozen buffer: recompute next time */ }
    return m;
  }
  // The wrap as the crowd would hear it: 4 s before `end`, then 4 s from `start`, at 16 kHz.
  function seamClip(x, sr, start, end, secs) {
    const n = Math.floor(secs * RATE), out = new Float32Array(2 * n), step = sr / RATE;
    for (let i = 0; i < n; i++) {
      out[i] = x[Math.min(x.length - 1, Math.max(0, Math.floor((end - secs) * sr + i * step)))] || 0;
      out[n + i] = x[Math.min(x.length - 1, Math.floor(start * sr + i * step))] || 0;
    }
    return out;
  }

  const core = { gridOffsetMs, loopLengthErrMs, frameRms, seamClickRatio, flags, wavBytes,
                 seamScore, seamClip, mono };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof root.document === "undefined" || typeof audioCtx === "undefined") return;

  // ----------------------------------------------------------- capture --
  const WORKLET = `
  class EarTap extends AudioWorkletProcessor {
    constructor() { super(); this.step = sampleRate / ${RATE}; this.acc = 0; this.n = 0; this.pos = 0;
      this.out = new Float32Array(4096); this.o = 0; this.peak = 0; this.clips = 0; }
    process(inputs) {
      const ch = inputs[0];
      if (!ch || !ch.length) return true;
      const L = ch[0], R = ch[1] || ch[0];
      for (let i = 0; i < L.length; i++) {
        const x = (L[i] + R[i]) * 0.5, a = Math.max(Math.abs(L[i]), Math.abs(R[i]));
        if (a > this.peak) this.peak = a;
        if (a >= 0.999) this.clips++;
        this.acc += x; this.n++; this.pos += 1;
        if (this.pos >= this.step) {
          this.pos -= this.step;
          this.out[this.o++] = this.acc / this.n; this.acc = 0; this.n = 0;
          if (this.o === this.out.length) {
            this.port.postMessage({ s: this.out, t: currentTime, peak: this.peak, clips: this.clips }, [this.out.buffer]);
            this.out = new Float32Array(4096); this.o = 0; this.peak = 0; this.clips = 0;
          }
        }
      }
      return true;
    }
  }
  registerProcessor("ear-tap", EarTap);`;

  const ring = new Float32Array(RATE * RING_S);
  let w = 0, filled = 0, ringEnd = 0;           // write index, samples held, audio time at ring end
  const peaks = [];                            // [{t, peak, clips}] per chunk
  let tapReady = null;

  function startTap() {
    if (tapReady) return tapReady;
    tapReady = (async () => {
      const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
      await audioCtx.audioWorklet.addModule(url);
      const node = new AudioWorkletNode(audioCtx, "ear-tap", { numberOfInputs: 1, numberOfOutputs: 1 });
      const mute = audioCtx.createGain(); mute.gain.value = 0;
      masterOut.connect(node); node.connect(mute); mute.connect(audioCtx.destination);
      node.port.onmessage = (e) => {
        const s = e.data.s;
        for (let i = 0; i < s.length; i++) { ring[w] = s[i]; w = (w + 1) % ring.length; }
        filled = Math.min(ring.length, filled + s.length);
        ringEnd = e.data.t;
        peaks.push({ t: e.data.t, peak: e.data.peak, clips: e.data.clips });
        while (peaks.length && peaks[0].t < ringEnd - RING_S) peaks.shift();
      };
    })().catch((err) => { console.warn("live ear: capture unavailable", err); tapReady = null; });
    return tapReady;
  }
  // Last `secs` of captured audio, oldest first, and the audio time it starts at.
  function lastSamples(secs) {
    const n = Math.min(filled, Math.floor(secs * RATE)), out = new Float32Array(n);
    for (let i = 0; i < n; i++) out[i] = ring[(w - n + i + ring.length) % ring.length];
    return { samples: out, t0: ringEnd - n / RATE };
  }

  // ---------------------------------------------------------- watchdog --
  function enabled() { const t = document.getElementById("ap-ear-toggle"); return !t || t.checked; }
  function eqDb(id, band) {
    const el = document.querySelector(`.eq-knob[data-deck="${id}"][data-band="${band}"]`);
    return el ? parseFloat(el.value) || 0 : 0;
  }
  function audible(d) {
    return d && d.playing && d.volumeGain && d.crossfaderGain &&
      d.volumeGain.gain.value > 0.1 && d.crossfaderGain.gain.value > 0.1;
  }
  function metrics(info) {
    const d = root.decks[info.deck], a = d.analysis || {};
    const otherId = info.deck === "a" ? "b" : "a", other = root.decks[otherId];
    const bpm = d.bpm || 128;
    const m = {
      deck: info.deck, bpm: Math.round(bpm * 10) / 10, rate: Math.round(info.rate * 1000) / 1000,
      loop_bars: info.bars, secs_looping: Math.round(info.secs),
      passes: Math.floor((info.secs * info.rate) / (info.bars * info.bar)),
      washed: info.washed, moved: info.moved, can_move: info.canMove,
      seam_shift_ms: round1(gridOffsetMs(a.downbeat_times, info.start)),
      grid_err_ms: round1(loopLengthErrMs(a.downbeat_times, info.start, info.bars, info.bar)),
      low_clash: !!(audible(other) && eqDb(info.deck, "low") > LOW_OPEN_DB && eqDb(otherId, "low") > LOW_OPEN_DB),
    };
    const recent = peaks.filter((p) => p.t >= ringEnd - 2);
    if (recent.length) {
      const pk = Math.max(...recent.map((p) => p.peak));
      m.peak_dbfs = round1(20 * Math.log10(Math.max(pk, 1e-6)));
      m.clip_events = peaks.reduce((s, p) => s + p.clips, 0);
    }
    return m;
  }
  function round1(x) { return x == null ? null : Math.round(x * 10) / 10; }
  // Seam level jump over the clip: wrap k happens at startedAt + k * loopLen / rate.
  function addSeamClick(m, info, clip) {
    const d = root.decks[info.deck];
    const len = (info.bars * info.bar) / info.rate;
    if (!d.startedAt || !(len > 0)) return;
    const idx = [];
    const first = Math.ceil((clip.t0 - d.startedAt) / len);
    for (let k = Math.max(1, first); ; k++) {
      const t = d.startedAt + k * len;
      if (t > ringEnd) break;
      idx.push(Math.round((t - clip.t0) * RATE));
    }
    m.seam_click_ratio = round1(seamClickRatio(clip.samples, idx, RATE / 200)); // 5 ms frames
  }

  let loopKey = null, lastAsk = 0, lastFlags = "", inFlight = null;
  async function ask(info) {
    const m = metrics(info);
    const clip = lastSamples(CLIP_S);
    addSeamClick(m, info, clip);
    const fd = new FormData();
    fd.append("metrics", JSON.stringify(m));
    if (clip.samples.length >= RATE) fd.append("clip", new Blob([wavBytes(clip.samples, RATE)], { type: "audio/wav" }), "ear.wav");
    const ctl = new AbortController(), key = loopKey;
    const to = setTimeout(() => ctl.abort(), REQ_TIMEOUT_MS);
    try {
      const res = await fetch("/api/live/ear", { method: "POST", body: fd, signal: ctl.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const dec = await res.json();
      root.liveEar.last = { at: Date.now(), metrics: m, decision: dec };
      if (key !== loopKey || !root.djMind.holdLoopInfo()) return;   // loop released meanwhile
      if (dec.action && dec.action !== "keep") {
        const who = dec.source === "AI" ? "EAR·AI" : "EAR";
        root.djMind.holdLoopAct(dec.action, dec.reason, who);
      }
    } catch (err) {
      if (err.name !== "AbortError") console.warn("live ear:", err.message);
    } finally { clearTimeout(to); }
  }
  function tick() {
    if (!enabled() || !root.djMind || !root.djMind.holdLoopInfo) return;
    const info = root.djMind.holdLoopInfo();
    if (!info) { loopKey = null; return; }
    startTap();
    const key = `${info.deck}:${info.start.toFixed(2)}:${info.bars}`;
    const now = performance.now() / 1000;
    const phraseS = (8 * info.bar) / info.rate;
    if (key !== loopKey) { loopKey = key; lastAsk = now; lastFlags = ""; return; }  // let one pass play first
    if (inFlight) return;
    const f = flags(metrics(info)).join(",");
    const due = now - lastAsk >= phraseS || (f && f !== lastFlags && now - lastAsk >= info.bar / info.rate);
    if (!due) return;
    lastAsk = now; lastFlags = f;
    inFlight = ask(info).finally(() => { inFlight = null; });
  }
  setInterval(tick, TICK_MS);
  // Silent pre-check: score candidate spans on the deck's buffer (nothing
  // plays). cands: [{start, bars}] in preference order. Returns them scored.
  function precheck(d, cands, bar) {
    if (!d || !d.buffer) return cands.map((c) => ({ ...c, seam: null }));
    const x = mono(d.buffer), sr = d.buffer.sampleRate;
    return cands.map((c) => ({ ...c, seam: seamScore(x, sr, c.start, c.start + c.bars * bar, bar) }));
  }
  // Omni hears the chosen seam offline (a rendered clip, not the master) and
  // reports; advisory only, its seam judgement measured unreliable.
  async function silentEar(d, span, bar) {
    if (!d || !d.buffer || !enabled()) return null;
    const x = mono(d.buffer), sr = d.buffer.sampleRate, end = span.start + span.bars * bar;
    const fd = new FormData();
    fd.append("metrics", JSON.stringify({ deck: d.id, precheck: true, bpm: d.bpm, loop_bars: span.bars,
      seam_score: span.seam && span.seam.score, can_move: false, secs_looping: 0 }));
    fd.append("clip", new Blob([wavBytes(seamClip(x, sr, span.start, end, 4), RATE)], { type: "audio/wav" }), "seam.wav");
    const ctl = new AbortController(), to = setTimeout(() => ctl.abort(), REQ_TIMEOUT_MS);
    try {
      const res = await fetch("/api/live/ear", { method: "POST", body: fd, signal: ctl.signal });
      return res.ok ? await res.json() : null;
    } catch (e) { return null; } finally { clearTimeout(to); }
  }

  root.liveEar = { core, last: null, lastSamples, precheck, silentEar };
})(typeof window !== "undefined" ? window : globalThis);
