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

  // ---- "vis-moment": the one visual signal every move emits (NULL-BOT and the SHOW) ----
  // detail {at (audio s, the hit), name (short caption), tier, deck, bar?, until?, source?}, emitted by the
  // move's own planner when it books. Tiers:
  //   super   fly-in takeover, gated (AI driving, VFX on) and de-duped (SUPERMOVE_WINDOW_S)
  //   accent  in-place reaction at NULL's corner (a nod / pop), rate-limited to one per ACCENT_BARS bars
  //   dance   the SHOW's Anyma-drop dance: NULL dances beside the figure on the same beat grid until `until`
  // Legacy signals still count as super: "ai-supermove" and the CUE_MOVES "ai-cue" whys.
  const TIERS = ["super", "accent", "dance"];
  const ACCENT_BARS = 2, DANCE_BARS = 16, DANCE_MAX_S = 90;
  // evt: {type, detail} -> {name, at, deck, tier, bar?, until?} | null
  function momentFor(evt) {
    const d = evt && evt.detail;
    if (!d || !Number.isFinite(d.at)) return null;
    if (evt.type !== "vis-moment") {
      const sm = supermoveFor(evt);
      if (sm && d.bar > 0) sm.bar = d.bar;
      return sm ? Object.assign(sm, { tier: "super" }) : null;
    }
    const name = String(d.name || "").trim().toUpperCase().slice(0, 24);
    if (!name) return null;
    const m = { name, at: d.at, deck: deckOf(d.deck), tier: TIERS.includes(d.tier) ? d.tier : "super" };
    if (d.bar > 0) m.bar = d.bar;
    if (m.tier === "dance") {
      const u = Number.isFinite(d.until) && d.until > d.at ? d.until : d.at + DANCE_BARS * (m.bar || 2);
      m.until = Math.min(u, d.at + DANCE_MAX_S);
    }
    return m;
  }
  // The next Anyma drop (anyma-show.js core.anymaDrops, song time) within lookS of song position pos, not yet
  // announced (done: Set of drop times). -> drop | null
  function nextAnymaDrop(drops, pos, lookS, done) {
    if (!Array.isArray(drops) || !Number.isFinite(pos)) return null;
    for (const d of drops) if (d && d.at > pos && d.at - pos <= lookS && !(done && done.has(d.at))) return d;
    return null;
  }
  // A legacy supermove signal ("ai-supermove", a CUE_MOVES "ai-cue") as the vis-moment it re-announces. -> detail | null
  function legacyMoment(evt) {
    if (!evt || evt.type === "vis-moment") return null;
    const sm = supermoveFor(evt);
    if (!sm) return null;
    const vm = { at: sm.at, name: sm.name, tier: "super", deck: sm.deck, source: "cue" };
    if (evt.detail.bar > 0) vm.bar = evt.detail.bar;
    return vm;
  }
  // A vis-moment on the hit of a takeover already booked from a legacy cue (the MERGE cue, then HOLD->DROP
  // from its planner) renames that takeover instead of being de-duped away. pending: [{hitS, sm}]. -> bool
  const SAME_HIT_S = 0.25;
  function renameSameHit(pending, hitS, name) {
    const p = (pending || []).find((x) => Math.abs(x.hitS - hitS) < SAME_HIT_S);
    if (!p || !name) return false;
    p.sm.name = name;
    return true;
  }
  // May an accent at hitS react? g: {aiActive, vfxOn, last (hit s of the last accent), hitS, barS, booked
  // (super hit times)}. -> "" or the reason not. Never on top of a takeover (half its de-dup window).
  function accentGate(g) {
    if (!g.aiActive) return "AI not driving";
    if (!g.vfxOn) return "VFX off";
    if ((g.booked || []).some((h) => Math.abs(h - g.hitS) < SUPERMOVE_WINDOW_S / 2)) return "super";
    const bar = g.barS > 0 ? g.barS : 2;
    if (Number.isFinite(g.last) && Math.abs(g.hitS - g.last) < ACCENT_BARS * bar) return "rate";
    return "";
  }
  // Where NULL stands while the SHOW is up (translate from the viewport centre + scale of the big bot).
  // o: {mode ("off" | "full" | "embed" / any window mode), rect (the stage's box, window modes), vw, vh, size (bot px),
  // dancing}. full: always, bottom-left corner in front of the stage. A window stage: only while dancing,
  // beside the stage (above it when there is no room on the right). -> {x, y, s} | null
  function dockPlace(o) {
    if (!o || !o.mode || o.mode === "off" || !(o.vw > 0) || !(o.vh > 0) || !(o.size > 0)) return null;
    const vmin = Math.min(o.vw, o.vh);
    let T, cx, cy;
    if (o.mode === "full") {
      T = 0.2 * vmin; cx = 16 + T / 2; cy = o.vh - 16 - T / 2;
    } else {
      const r = o.rect;
      if (!o.dancing || !r || !(r.width > 0) || !(r.height > 0)) return null;   // embedded band not laid out / no stage: top bar
      T = Math.max(48, Math.min(0.16 * vmin, r.height * 0.8));
      if (r.right + 8 + T <= o.vw) { cx = r.right + 8 + T / 2; cy = r.bottom - T / 2; }
      else { cx = r.left + T / 2; cy = r.top - 8 - T / 2; }
    }
    const f = (v) => Math.round(v * 10) / 10;
    return { x: f(cx - o.vw / 2), y: f(cy - o.vh / 2), s: Math.round((T / o.size) * 1000) / 1000 };
  }
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
  const core = { mood, LABEL, supermoveFor, isSupermove, vfxOn, supermoveGate, hitPerfMs, beatSeconds,
                 takeoverPlan, beatPhaseMs, SUPERMOVE_WINDOW_S, CUE_MOVES,
                 TIERS, ACCENT_BARS, DANCE_BARS, DANCE_MAX_S, momentFor, accentGate, dockPlace, renameSameHit, SAME_HIT_S, legacyMoment, nextAnymaDrop };
  root.nullBot = { core };          // anyma-show.js may reuse momentFor: one reading of the signal
  if (typeof module !== "undefined" && module.exports) module.exports = core;
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

  // ---- NULL-BOT on the moves (browser) ----------------------------------------
  // One class on #nul-super runs each state (null-bot.css); JS only sets a few
  // CSS variables once per moment: no per-frame DOM writes.
  //   nul-sm-on     SUPER takeover (fly in, dance, the hit on the move's audio time, fly back)
  //   nul-sm-dock   the small NULL in front of the SHOW (full stage, or beside a window stage while it dances)
  //   nul-sm-dance  the dock NULL dances on the Anyma drop's beat grid
  //   nul-sm-react  ACCENT pop of the dock NULL; the top-bar NULL gets .nul-react
  const sup = document.getElementById("nul-super");
  if (sup) {
    const svg = el.querySelector("svg");
    sup.innerHTML = '<div class="nul-sm-scrim"></div><div class="nul-sm-bot"><div class="nul-sm-halo"></div>' +
      '<div class="nul-sm-ring"></div><div class="nul-sm-pop"></div><div class="nul-sm-cap"></div></div>';
    if (svg) sup.querySelector(".nul-sm-pop").appendChild(svg.cloneNode(true));
    const cap = sup.querySelector(".nul-sm-cap"), bot = sup.querySelector(".nul-sm-bot");
    const booked = [];               // hit times (perf s) of booked takeovers, for the de-dup window
    let endTimer = 0, lastAccent = -Infinity, reactTimer = 0, chipTimer = 0, danceTimer = 0;
    let dock = null, dockKey = "", dance = null;   // dance: {untilMs} while the dock NULL dances
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
    const showMode = () => (root.anymaShow && typeof root.anymaShow.mode === "string" ? root.anymaShow.mode : "off");

    // The SHOW in browser full screen owns the top layer: NULL has to live inside the full-screen element
    // to be seen at all (z-index cannot reach above it). Back to <body> when full screen ends.
    document.addEventListener("fullscreenchange", () => {
      const fs = document.fullscreenElement;
      const home = fs && fs !== sup && !fs.contains(sup) ? fs : !fs && sup.parentNode !== document.body ? document.body : null;
      if (home) home.appendChild(sup);
      syncDock();
    });

    // dock: where NULL stands in front of the SHOW. Called on the 150 ms tick; writes only on change.
    function syncDock() {
      const mode = showMode(), dancing = !!dance && performance.now() < dance.untilMs;
      if (dance && !dancing) { dance = null; sup.classList.remove("nul-sm-dance"); }
      // idle (no dance, no full SHOW): nothing to place, and no layout read every 150 ms
      if (!dancing && mode !== "full") {
        dock = null;
        if (dockKey !== "") { dockKey = ""; sup.classList.remove("nul-sm-dock"); }
        return;
      }
      let rect = null;
      if (mode !== "off" && mode !== "full" && dancing) {
        const stg = document.querySelector(".anyma-stage");
        rect = stg && !stg.hidden ? stg.getBoundingClientRect() : null;
      }
      dock = dockPlace({ mode, rect, vw: root.innerWidth, vh: root.innerHeight, size: bot.offsetWidth || 380, dancing });
      const key = dock ? `${dock.x},${dock.y},${dock.s}` : "";
      if (key === dockKey) return;
      dockKey = key;
      if (dock) {
        sup.style.setProperty("--sm-dx", `${dock.x}px`);
        sup.style.setProperty("--sm-dy", `${dock.y}px`);
        sup.style.setProperty("--sm-ds", String(dock.s));
      }
      sup.classList.toggle("nul-sm-dock", !!dock);
    }
    root.setInterval(syncDock, 150);

    // Anyma drops, AHEAD: while the SHOW is up, find the on-air deck's next Anyma drop with the SHOW's own
    // detector and announce it (vis-moment tier "dance") a few seconds early, once per drop. NULL dances on it;
    // the SHOW may take the same signal. No SHOW code needed: anymaShow.core is pure.
    const drops = { a: { an: null, list: [], done: new Set() }, b: { an: null, list: [], done: new Set() } };
    function watchDrops() {
      const C = root.anymaShow && root.anymaShow.core;
      if (!C || !C.prepTrack || !C.anymaDrops || showMode() === "off" || !root.decks || typeof audioCtx === "undefined") return;
      const gain = (d) => (d && d.playing ? ((d.crossfaderGain && d.crossfaderGain.gain.value) || 0) * ((d.volumeGain && d.volumeGain.gain.value) || 1) : 0);
      const id = gain(root.decks.a) >= gain(root.decks.b) ? "a" : "b", d = root.decks[id], w = drops[id];
      if (!d || !d.playing || !d.analysis || typeof d._currentPosition !== "function") return;
      if (w.an !== d.analysis) {
        const an = d.analysis, hint = [an.genre, an.artist, an.title, d.trackName].filter((x) => typeof x === "string").join(" ");
        w.an = an; w.done.clear();
        try { const pt = C.prepTrack(an); w.list = pt ? C.anymaDrops(pt, C.anymaHint ? C.anymaHint(hint) : "") : []; } catch (_) { w.list = []; }
      }
      const pos = d._currentPosition(), rate = (typeof d._playbackRate === "function" && d._playbackRate()) || 1;
      const dr = nextAnymaDrop(w.list, pos, 4 * rate, w.done);
      if (!dr) return;
      w.done.add(dr.at);
      const bar = 240 / (d.bpm || 128) / rate, at = audioCtx.currentTime + (dr.at - pos) / rate;
      root.dispatchEvent(new CustomEvent("vis-moment", { detail: { at, name: "ANYMA DROP", tier: "dance", deck: id, bar, until: at + DANCE_BARS * bar, source: "anyma" } }));
    }
    root.setInterval(watchDrops, 500);

    function show(sm) {
      // re-check at the start: the AI or the VFX may have been switched off since booking
      if (!aiActive() || !vfx()) return;
      const now = performance.now();
      const p = takeoverPlan(hitNow(sm.at), now, beatFor(sm));
      if (!p) return;
      const s = sup.style;
      if (dock) {                    // fly out of the dock (the top bar sits under the full SHOW)
        s.setProperty("--sm-fx", `${dock.x}px`); s.setProperty("--sm-fy", `${dock.y}px`); s.setProperty("--sm-fs", String(dock.s));
      } else {
        const r = el.getBoundingClientRect(), size = bot.offsetWidth || 1;      // one read per takeover
        s.setProperty("--sm-fx", `${(r.left + r.width / 2 - root.innerWidth / 2).toFixed(1)}px`);
        s.setProperty("--sm-fy", `${(r.top + r.height / 2 - root.innerHeight / 2).toFixed(1)}px`);
        s.setProperty("--sm-fs", (r.width / size).toFixed(3));
      }
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
    const pending = [];              // {hitS, sm} of booked takeovers: a named moment on the same hit renames it
    function bookSuper(sm, named) {
      const now = performance.now(), hitS = hitNow(sm.at) / 1000;
      while (booked.length && booked[0] < now / 1000 - SUPERMOVE_WINDOW_S) booked.shift();
      while (pending.length && pending[0].hitS < now / 1000 - SUPERMOVE_WINDOW_S) pending.shift();
      if (named && renameSameHit(pending, hitS, sm.name)) return;
      if (supermoveGate({ aiActive: aiActive(), vfxOn: vfx(), booked, hitS })) return;
      const p = takeoverPlan(hitS * 1000, now, beatFor(sm));
      if (!p) return;
      booked.push(hitS);
      booked.sort((x, y) => x - y);
      pending.push({ hitS, sm });
      setTimeout(() => show(sm), Math.max(0, p.start - now));
    }
    // ACCENT: a quick pop / nod where NULL stands (top bar, and the dock when the SHOW is up), on the hit.
    function react(sm, beatMs) {
      if (!aiActive() || !vfx() || sup.classList.contains("nul-sm-on")) return;
      const dur = `${Math.round(Math.max(250, Math.min(700, beatMs)))}ms`;
      el.style.setProperty("--nul-react", dur);
      sup.style.setProperty("--nul-react", dur);
      el.classList.remove("nul-react"); sup.classList.remove("nul-sm-react");
      void el.offsetWidth;           // restart the pop (at most one per 2 bars)
      el.classList.add("nul-react"); sup.classList.add("nul-sm-react");
      clearTimeout(reactTimer);
      reactTimer = setTimeout(() => { el.classList.remove("nul-react"); sup.classList.remove("nul-sm-react"); }, Math.max(250, beatMs) + 80);
      if (chip) {
        chip.textContent = sm.name;
        clearTimeout(chipTimer);
        chipTimer = setTimeout(() => { chip.textContent = LABEL[cur] || LABEL.idle; }, 1500);
      }
    }
    function bookAccent(sm) {
      const now = performance.now(), hitMs = hitNow(sm.at), beatS = beatFor(sm);
      const no = accentGate({ aiActive: aiActive(), vfxOn: vfx(), last: lastAccent, hitS: hitMs / 1000, barS: 4 * beatS, booked });
      if (no || hitMs < now - LATE_MS || hitMs - now > MAX_LEAD_MS) return;
      lastAccent = hitMs / 1000;
      setTimeout(() => react(sm, beatS * 1000), Math.max(0, hitMs - now));
    }
    // DANCE: the SHOW's Anyma drop. The dock NULL dances on the drop's beat grid (phase from its hit) until `until`.
    function bookDance(sm) {
      if (!aiActive() || !vfx()) return;
      const now = performance.now(), hitMs = hitNow(sm.at), untilMs = hitNow(sm.until), beatMs = beatFor(sm) * 1000;
      if (!(untilMs > now) || hitMs - now > MAX_LEAD_MS) return;
      clearTimeout(danceTimer);
      danceTimer = setTimeout(() => {
        const t = performance.now();
        sup.style.setProperty("--sm-beat", `${beatMs.toFixed(0)}ms`);
        sup.style.setProperty("--sm-phase", `${beatPhaseMs(t, hitMs, beatMs).toFixed(0)}ms`);
        dance = { untilMs };
        sup.classList.remove("nul-sm-dance");
        void sup.offsetWidth;
        sup.classList.add("nul-sm-dance");
        st.grooveUntil = untilMs / 1000;       // the top-bar NULL grooves too (seen when the SHOW is a window)
        syncDock();
      }, Math.max(0, hitMs - now));
    }
    function book(evt) {
      const sm = momentFor(evt);
      if (!sm || typeof audioCtx === "undefined") return;
      if (sm.tier === "accent") bookAccent(sm);
      else if (sm.tier === "dance") bookDance(sm);
      else bookSuper(sm, !(evt.detail && evt.detail.source === "cue"));
    }
    // One source of truth: a legacy supermove signal is re-announced as a vis-moment (source "cue"), so the
    // SHOW and NULL book the same {at, name}; NULL itself books only from vis-moment.
    for (const t of ["ai-cue", "ai-supermove"]) root.addEventListener(t, (e) => {
      const vm = legacyMoment({ type: t, detail: e.detail });
      if (vm) root.dispatchEvent(new CustomEvent("vis-moment", { detail: vm }));
    });
    root.addEventListener("vis-moment", (e) => book({ type: "vis-moment", detail: e.detail }));
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
