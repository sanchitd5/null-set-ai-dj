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
  if (typeof module !== "undefined" && module.exports) module.exports = { mood, LABEL };
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
