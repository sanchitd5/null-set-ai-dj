// NULL, the AI DJ mascot (top bar). Pure presentation: it reacts to what the
// AI is doing through the events the engines already emit, never touches audio.
//
//   idle       slow blink
//   think      LLM picking / planning: eyes scan left-right
//   listen     Omni listening, silent seam check: eyes closed
//   groove     stem moves, riff over rap, transitions: head nods on the beat
//   hype       auto-sampler drop cue: "!" flash
//   sweat      hold loop running (waiting for the next song)
// Click: open / close the NULL AT WORK panel.
(function (root) {
  "use strict";

  // Pure: which mood wins right now. busy = {llm, omni, stems}; flags from events.
  function mood(s) {
    if (s.hypeUntil > s.now) return "hype";
    if (s.holdLoop) return "sweat";
    if (s.grooveUntil > s.now) return "groove";
    if (s.omni) return "listen";
    if (s.llm) return "think";
    return "idle";
  }
  const LABEL = { idle: "AI DJ", think: "THINKING", listen: "LISTENING", groove: "MIXING", hype: "DROP!", sweat: "HOLDING" };

  // ---- SUPERMOVE takeover (pure) -------------------------------------------
  // A supermove is a big, audience-facing moment: NULL flies to the centre and
  // dances, its biggest hit on the move's audio time. Only events that carry
  // that time qualify. The "ai-activity" labels (MERGE -> A, HOOK DROP, ...)
  // are announced when the move is booked, bars before its drop, so they never
  // start a takeover: the "ai-cue" booked with them carries the drop time.
  // "ai-supermove" {at, name, deck} is for moves with no cue (LAYER start,
  // PEAK Double Drop / Drop Swap, announced early by autopilot.js).
  const SUPERMOVE_WINDOW_S = 8;
  const CUE_MOVES = [   // [ai-cue kind, test on its why, caption]
    ["drop", /strip & rebuild/i, "STRIP & REBUILD"],
    ["drop", /after the merge/i, "MERGE"],
    ["drop", /after the mashup/i, "MASHUP"],
    ["drop", /^the beat slams back after "/i, "HOOK DROP"],
    ["transition", /^Double Drop:/, "DOUBLE DROP"],
    ["transition", /^Drop Swap:/, "DROP SWAP"],
    ["line", /^B's rap arrives/, "RIFF OVER RAP"],
  ];
  const deckOf = (x) => (x === "a" || x === "b" ? x : "");
  // evt: {type: "ai-cue" | "ai-supermove" | "ai-activity", detail}. -> {name, at, deck} | null
  function supermoveFor(evt) {
    const d = evt && evt.detail;
    if (!d || !Number.isFinite(d.at)) return null;
    if (evt.type === "ai-supermove") {
      const name = String(d.name || "").trim().toUpperCase().slice(0, 24);
      return name ? { name, at: d.at, deck: deckOf(d.deck) } : null;
    }
    if (evt.type !== "ai-cue") return null;
    const why = String(d.why || "");
    for (const [kind, re, name] of CUE_MOVES) if (d.kind === kind && re.test(why)) return { name, at: d.at, deck: deckOf(d.deck) };
    return null;
  }
  const isSupermove = (evt) => supermoveFor(evt) !== null;
  // VFX toggle state: visuals.js stores "on"/"off" under nul.vfx; the button's aria-pressed otherwise.
  function vfxOn(stored, pressed) {
    if (stored === "off") return false;
    if (stored === "on") return true;
    return pressed !== "false";
  }
  // May a supermove whose hit is at hitS (seconds, any one clock) take over?
  // booked: hit times of takeovers already booked. -> "" (yes) or the reason not.
  function supermoveGate(g) {
    if (!g.aiActive) return "AI not driving";
    if (!g.vfxOn) return "VFX off";
    if ((g.booked || []).some((h) => Math.abs(h - g.hitS) < SUPERMOVE_WINDOW_S)) return "de-dup";
    return "";
  }
  // performance.now() ms at which audio time `at` is heard. latencyS: output latency.
  function hitPerfMs(at, audioNow, perfNowMs, latencyS) {
    return perfNowMs + (at - audioNow + (latencyS > 0 ? latencyS : 0)) * 1000;
  }
  // Seconds per beat as heard (bpm * playbackRate), else the cue's bar / 4, else
  // 124 BPM; halved / doubled into 0.3-0.75 s so the nods stay danceable and
  // still land on the beat grid.
  function beatSeconds(bpm, rate, cueBar) {
    let b = bpm > 0 ? 60 / (bpm * (rate > 0 ? rate : 1)) : cueBar > 0 ? cueBar / 4 : 60 / 124;
    while (b < 0.3) b *= 2;
    while (b > 0.75) b /= 2;
    return b;
  }
  const FLY_MS = 550, OUT_MS = 500, PRE_BEATS = 2, POST_BEATS = 3, MAX_MS = 4000, LATE_MS = 250, MAX_LEAD_MS = 120000;
  // The takeover timeline (all perf ms): fly in, dance PRE_BEATS, the HIT on the
  // cue, dance up to POST_BEATS, fly back; never longer than MAX_MS. A cue that
  // gives less lead shortens the pre-dance; one too late or too far out -> null.
  function takeoverPlan(hitMs, nowMs, beatS) {
    if (!(hitMs >= nowMs - LATE_MS) || hitMs - nowMs > MAX_LEAD_MS) return null;
    const b = beatS * 1000;
    let post = POST_BEATS;
    while (post > 1 && FLY_MS + PRE_BEATS * b + post * b + OUT_MS > MAX_MS) post--;
    const hit = Math.max(hitMs, nowMs + FLY_MS);
    const start = Math.max(nowMs, hit - PRE_BEATS * b - FLY_MS);
    const outAt = hit + post * b;
    return { start, hit, outAt, end: outAt + OUT_MS, beatMs: b, flyMs: FLY_MS, outMs: OUT_MS };
  }
  // CSS animation-delay (ms, <= 0) that puts a beat-long loop started at nowMs
  // at progress 0 on the hit (and so on every beat around it).
  function beatPhaseMs(nowMs, hitMs, beatMs) {
    return -((((nowMs - hitMs) % beatMs) + beatMs) % beatMs);
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { mood, LABEL, supermoveFor, isSupermove, vfxOn, supermoveGate, hitPerfMs, beatSeconds,
                       takeoverPlan, beatPhaseMs, SUPERMOVE_WINDOW_S, CUE_MOVES };
  }
  if (typeof document === "undefined") return;

  const el = document.getElementById("nul-mascot");
  const chip = document.getElementById("nul-state");
  if (!el) return;

  const st = { now: 0, hypeUntil: 0, grooveUntil: 0, holdLoop: false, llm: 0, omni: 0 };
  const LLM = /\/api\/autopilot\/(suggest|plan)$/, OMNI = /\/api\/live\/ear$/;

  // Count in-flight AI requests (wraps whatever fetch is installed, incl. the HUD's).
  const prevFetch = root.fetch.bind(root);
  root.fetch = function (input, init) {
    const url = (typeof input === "string" ? input : (input && input.url) || "").replace(/^https?:\/\/[^/]+/, "").split("?")[0];
    const kind = LLM.test(url) ? "llm" : OMNI.test(url) ? "omni" : null;
    if (!kind) return prevFetch(input, init);
    st[kind]++;
    const done = () => { st[kind] = Math.max(0, st[kind] - 1); };
    return prevFetch(input, init).then((r) => { done(); return r; }, (e) => { done(); throw e; });
  };

  const beatS = () => {
    const d = root.decks && (["a", "b"].map((i) => root.decks[i]).find((x) => x && x.playing));
    return 60 / ((d && d.bpm) || 124);
  };
  root.addEventListener("ai-activity", (e) => {
    const d = e.detail || {};
    const now = performance.now() / 1000;
    if (d.kind === "stem-move" || d.kind === "stems" || d.kind === "precheck") st.grooveUntil = now + 8;
    if (d.kind === "stem-move" && /AUTO SAMPLER · drop/.test(d.label || "")) st.hypeUntil = now + 1.6;
    if (d.kind === "decision") st.holdLoop = d.action === "holdloop";
    if (d.kind === "decision" && d.action && d.action !== "ride" && d.action !== "holdloop") st.grooveUntil = now + 6;
  });
  root.addEventListener("ai-cue", (e) => {
    const d = e.detail || {};
    if (d.kind !== "drop" || !Number.isFinite(d.at) || typeof audioCtx === "undefined") return;
    const ms = Math.max(0, (d.at - audioCtx.currentTime) * 1000);
    setTimeout(() => { st.hypeUntil = performance.now() / 1000 + 1.6; }, ms);   // "!" on the drop itself
  });

  // ---- SUPERMOVE takeover (browser) ----------------------------------------
  // One class on #nul-super runs the whole show (null-bot.css); JS only sets a
  // few CSS variables once per takeover: no per-frame DOM writes.
  const sup = document.getElementById("nul-super");
  if (sup) {
    const svg = el.querySelector("svg");
    sup.innerHTML = '<div class="nul-sm-scrim"></div><div class="nul-sm-bot"><div class="nul-sm-halo"></div>' +
      '<div class="nul-sm-ring"></div><div class="nul-sm-pop"></div><div class="nul-sm-cap"></div></div>';
    if (svg) sup.querySelector(".nul-sm-pop").appendChild(svg.cloneNode(true));
    const cap = sup.querySelector(".nul-sm-cap"), bot = sup.querySelector(".nul-sm-bot");
    const booked = [];               // hit times (perf s) of booked takeovers, for the de-dup window
    let endTimer = 0;
    const aiActive = () => !!(root.autopilotState && root.autopilotState.active);
    const vfx = () => {
      let s = null;
      try { s = localStorage.getItem("nul.vfx"); } catch (_) { /* private mode */ }
      const b = document.getElementById("vfx-toggle");
      return vfxOn(s, b && b.getAttribute("aria-pressed"));
    };
    const hitNow = (at) => hitPerfMs(at, audioCtx.currentTime, performance.now(), audioCtx.outputLatency || 0);
    const beatFor = (sm) => {
      const d = root.decks && (root.decks[sm.deck] || ["a", "b"].map((i) => root.decks[i]).find((x) => x && x.playing));
      const rate = d && typeof d._playbackRate === "function" ? d._playbackRate() : 1;
      return beatSeconds(d && d.bpm, rate, sm.bar);
    };
    function show(sm) {
      // re-check at the start: the AI or the VFX may have been switched off since booking
      if (!aiActive() || !vfx()) return;
      const now = performance.now();
      const p = takeoverPlan(hitNow(sm.at), now, beatFor(sm));
      if (!p) return;
      const r = el.getBoundingClientRect(), size = bot.offsetWidth || 1;      // one read per takeover
      const s = sup.style;
      s.setProperty("--sm-fx", `${(r.left + r.width / 2 - root.innerWidth / 2).toFixed(1)}px`);
      s.setProperty("--sm-fy", `${(r.top + r.height / 2 - root.innerHeight / 2).toFixed(1)}px`);
      s.setProperty("--sm-fs", (r.width / size).toFixed(3));
      s.setProperty("--sm-glow", sm.deck ? `var(--${sm.deck})` : "var(--ai)");
      s.setProperty("--sm-beat", `${p.beatMs.toFixed(0)}ms`);
      s.setProperty("--sm-phase", `${beatPhaseMs(now, p.hit, p.beatMs).toFixed(0)}ms`);
      s.setProperty("--sm-fly", `${p.flyMs}ms`);
      s.setProperty("--sm-hit", `${(p.hit - now).toFixed(0)}ms`);
      s.setProperty("--sm-out", `${(p.outAt - now).toFixed(0)}ms`);
      s.setProperty("--sm-outd", `${p.outMs}ms`);
      cap.textContent = sm.name;
      clearTimeout(endTimer);
      sup.classList.remove("nul-sm-on");
      void sup.offsetWidth;          // restart the animations if one was still running
      sup.classList.add("nul-sm-on");
      el.classList.add("nul-away");
      endTimer = setTimeout(() => { sup.classList.remove("nul-sm-on"); el.classList.remove("nul-away"); }, p.end - now);
    }
    function book(evt) {
      const sm = supermoveFor(evt);
      if (!sm || typeof audioCtx === "undefined") return;
      sm.bar = evt.detail.bar;
      const now = performance.now(), hitS = hitNow(sm.at) / 1000;
      while (booked.length && booked[0] < now / 1000 - SUPERMOVE_WINDOW_S) booked.shift();
      const no = supermoveGate({ aiActive: aiActive(), vfxOn: vfx(), booked, hitS });
      if (no) return;
      const p = takeoverPlan(hitS * 1000, now, beatFor(sm));
      if (!p) return;
      booked.push(hitS);
      booked.sort((x, y) => x - y);
      setTimeout(() => show(sm), Math.max(0, p.start - now));
    }
    root.addEventListener("ai-cue", (e) => book({ type: "ai-cue", detail: e.detail }));
    root.addEventListener("ai-supermove", (e) => book({ type: "ai-supermove", detail: e.detail }));
  }

  el.addEventListener("click", () => {
    const hud = document.querySelector(".ai-hud");
    if (hud) hud.classList.toggle("ai-hud-collapsed");
  });

  // ---- musical waves: the master output, live ------------------------------
  // Three layered traces from the real audio (one per band: lows, mids,
  // highs), scrolling like a scope; the mouth opens with the level. Silence
  // still breathes: a slow idle sine so NULL never looks frozen.
  const cv = document.getElementById("nul-waves");
  const cx = cv && cv.getContext("2d");
  let an = null, freq = null, timeBuf = null;
  function tap() {
    if (an || typeof audioCtx === "undefined") return;
    const src = root.masterOut || (typeof masterGain !== "undefined" ? masterGain : null);
    if (!src) return;
    an = audioCtx.createAnalyser();
    an.fftSize = 1024;
    an.smoothingTimeConstant = 0.72;
    src.connect(an);
    freq = new Uint8Array(an.frequencyBinCount);
    timeBuf = new Float32Array(an.fftSize);
  }
  const mouth = el.querySelector(".nul-mouth");
  const COLS = 42, lanes = [new Float32Array(COLS), new Float32Array(COLS), new Float32Array(COLS)];
  let level = 0, phase = 0, lastT = 0;
  const colours = () => {
    const cs = getComputedStyle(document.documentElement);
    return [cs.getPropertyValue("--ai").trim() || "#0ef", cs.getPropertyValue("--a").trim() || "#0f6", cs.getPropertyValue("--b").trim() || "#f2d"];
  };
  let col = null;
  function band(lo, hi) {
    const ny = audioCtx.sampleRate / 2, n = freq.length;
    const a = Math.max(1, Math.floor((lo / ny) * n)), b = Math.min(n - 1, Math.ceil((hi / ny) * n));
    let s = 0;
    for (let i = a; i <= b; i++) s += freq[i];
    return s / ((b - a + 1) * 255);
  }
  function waves(t) {
    requestAnimationFrame(waves);
    if (!cx || t - lastT < 33) return;         // ~30 fps
    lastT = t;
    tap();
    if (!col) col = colours();
    phase += 0.09;
    let bands = [0, 0, 0];
    if (an) {
      an.getByteFrequencyData(freq);
      an.getFloatTimeDomainData(timeBuf);
      let e = 0;
      for (let i = 0; i < timeBuf.length; i++) e += timeBuf[i] * timeBuf[i];
      level = level * 0.6 + Math.min(1, Math.sqrt(e / timeBuf.length) * 5) * 0.4;
      bands = [band(30, 160), band(300, 2500), band(4000, 14000)];
    } else level *= 0.9;
    const idle = level < 0.02;
    for (let k = 0; k < 3; k++) {
      lanes[k].copyWithin(0, 1);
      lanes[k][COLS - 1] = idle ? 0.12 + 0.08 * Math.sin(phase + k * 2.1) : Math.min(1, bands[k] * (k === 0 ? 1.1 : 1.6));
    }
    const W = cv.width, H = cv.height, mid = H / 2;
    cx.clearRect(0, 0, W, H);
    cx.lineWidth = 2.2;
    cx.lineJoin = "round";
    for (let k = 2; k >= 0; k--) {
      const L = lanes[k], amp = H * (k === 0 ? 0.42 : k === 1 ? 0.34 : 0.26);
      cx.strokeStyle = col[k];
      cx.globalAlpha = idle ? 0.45 : 0.55 + 0.45 * (1 - k / 3);
      cx.beginPath();
      for (let i = 0; i < COLS; i++) {
        const x = (i / (COLS - 1)) * W;
        // each lane is a wave whose height follows that band's energy over time
        const y = mid + Math.sin(i * (0.55 + k * 0.35) + phase * (1.4 + k)) * L[i] * amp;
        i ? cx.lineTo(x, y) : cx.moveTo(x, y);
      }
      cx.stroke();
    }
    cx.globalAlpha = 1;
    if (mouth) mouth.setAttribute("height", String(1.6 + level * 4.5)), mouth.setAttribute("y", String(20 - level * 2.2));
  }
  requestAnimationFrame(waves);

  let cur = "";
  setInterval(() => {
    st.now = performance.now() / 1000;
    const hl = root.djMind && root.djMind.holdLoopInfo ? !!root.djMind.holdLoopInfo() : false;
    if (!hl) st.holdLoop = false; else st.holdLoop = true;
    const m = mood(st);
    el.style.setProperty("--nul-beat", `${beatS().toFixed(3)}s`);
    if (m === cur) return;
    el.classList.remove(`nul-${cur}`);
    el.classList.add(`nul-${m}`);
    cur = m;
    if (chip) chip.textContent = LABEL[m];
    el.setAttribute("aria-label", `NULL, the AI DJ: ${LABEL[m].toLowerCase()}`);
  }, 150);
})(typeof window !== "undefined" ? window : globalThis);
