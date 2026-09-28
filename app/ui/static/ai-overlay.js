// AI Music Brain - overlays that show the AI working on the decks.
//
//  1. Activity HUD (bottom right): every AI job with a live timer. Built by
//     watching fetch() to the AI endpoints, so no call site changes; plus the
//     "ai-activity" events dj-mind.js emits (decisions, silent pre-checks).
//  2. Control glow: the AI moves knobs/faders/buttons by firing synthetic
//     input/click events (isTrusted === false). Those elements glow with a
//     short tag naming the move; the user's own touches never glow.
//  3. Waveform markers on the overview bars: vocal regions, the hold-loop
//     span and the spans the silent pre-check tried, and the planned exit.
//
// Pure presentation: reads state, never changes playback.
(function () {
  "use strict";
  if (typeof document === "undefined") return;

  // ------------------------------------------------------------ 1. HUD --
  const JOBS = [
    [/\/api\/autopilot\/suggest$/, (b) => (b && b.lookahead ? "Looking ahead: songs after next" : "Picking next songs"), "LLM"],
    [/\/api\/autopilot\/plan$/, () => "Planning the transition", "LLM"],
    [/\/api\/match$/, () => "Matching transition recipes", "DSP"],
    [/\/api\/(blend|mashup|layer|bridge)\/plan$/, (b, m) => `Planning ${m[1]}`, "DSP"],
    [/\/api\/tracks\/[^/]+\/analysis$/, () => "Track analysis (library lookup)", "DSP"],
    [/\/api\/tracks\/[^/]+\/vocals/, () => "Vocal map (stems)", "STEMS"],
    [/\/api\/tracks\/[^/]+\/stems$/, () => "Live stems (Demucs 4-stem)", "STEMS"],
    [/\/api\/tracks\/[^/]+\/fame$/, () => "How famous is it (YouTube views)", "NET"],
    [/\/api\/tracks\/[^/]+\/separate/, () => "Separating stems (Demucs)", "STEMS"],
    [/\/api\/live\/ear$/, (b) => (b && b.precheck ? "Listening silently to the loop seam" : "Listening to the master"), "OMNI"],
    [/\/api\/download\/jobs$/, (b) => `Fetching ${(b && (b.query || b.url || "")).replace(/^ytmsearch:/, "") || "song"}`, "NET"],
  ];
  const SHOW_AFTER_MS = 350;       // cached lookups finish faster than this: no flicker
  const KEEP_DONE_MS = 6000;      // finished jobs
  const KEEP_EVENT_MS = 15000;    // decisions stay readable longer
  const MAX_ROWS = 7;

  const hud = document.createElement("div");
  hud.className = "ai-hud";
  hud.innerHTML = `<div class="ai-hud-head"><span class="ai-hud-dot"></span>NULL AT WORK<button class="ai-hud-min" title="Hide / show">–</button></div><ol class="ai-hud-list"></ol>`;
  document.body.appendChild(hud);
  const list = hud.querySelector(".ai-hud-list");
  // Starts collapsed so it never covers deck B's transport (SYNC/REV/BRAKE/CUE/PLAY); choice is remembered.
  const HUD_KEY = "nullset.aiHudOpen";
  let hudOpen = false;
  try { hudOpen = localStorage.getItem(HUD_KEY) === "1"; } catch (e) {}
  hud.classList.toggle("ai-hud-collapsed", !hudOpen);
  hud.querySelector(".ai-hud-min").addEventListener("click", () => {
    const open = hud.classList.toggle("ai-hud-collapsed") === false;
    try { localStorage.setItem(HUD_KEY, open ? "1" : "0"); } catch (e) {}
  });

  const rows = new Map();          // id -> {el, t0, done}
  let seq = 0;
  function row(label, tag) {
    const id = ++seq, el = document.createElement("li");
    el.className = "ai-row ai-running";
    el.innerHTML = `<span class="ai-tag ai-tag-${tag.toLowerCase()}">${tag}</span><span class="ai-label"></span><span class="ai-time"></span>`;
    el.querySelector(".ai-label").textContent = label;
    rows.set(id, { el, t0: performance.now(), done: false, shown: false });
    return id;
  }
  function show(id) {
    const r = rows.get(id);
    if (!r || r.shown) return;
    r.shown = true;
    list.prepend(r.el);
    while (list.children.length > MAX_ROWS) {
      const last = list.lastElementChild;
      for (const [k, v] of rows) if (v.el === last) rows.delete(k);
      last.remove();
    }
  }
  function finish(id, ok, note, keepMs) {
    const r = rows.get(id);
    if (!r) return;
    r.done = true;
    const ms = performance.now() - r.t0;
    if (!r.shown && ms < SHOW_AFTER_MS && ok) { rows.delete(id); return; }
    show(id);
    r.el.classList.remove("ai-running");
    r.el.classList.add(ok ? "ai-ok" : "ai-fail");
    r.el.querySelector(".ai-time").textContent = `${ok ? "✓" : "✗"} ${(ms / 1000).toFixed(1)}s`;
    if (note) r.el.querySelector(".ai-label").textContent += ` · ${note}`;
    setTimeout(() => {
      r.el.classList.add("ai-leaving");
      setTimeout(() => { r.el.remove(); rows.delete(id); }, 380);
    }, keepMs || KEEP_DONE_MS);
  }
  function event(label, tag, note) {       // instant entries (decisions)
    const id = row(label, tag);
    show(id);
    finish(id, true, note, KEEP_EVENT_MS);
    const r = rows.get(id);
    if (r) r.el.querySelector(".ai-time").textContent = new Date().toLocaleTimeString([], { minute: "2-digit", second: "2-digit" });
  }
  setInterval(() => {
    const now = performance.now();
    let busy = false;
    for (const [id, r] of rows) {
      if (r.done) continue;
      busy = true;
      if (now - r.t0 >= SHOW_AFTER_MS) show(id);
      if (r.shown) r.el.querySelector(".ai-time").textContent = `${((now - r.t0) / 1000).toFixed(1)}s`;
    }
    hud.classList.toggle("ai-hud-busy", busy);
    hud.classList.toggle("ai-hud-has", list.children.length > 0);
  }, 100);

  function bodyOf(init) {
    const b = init && init.body;
    if (!b) return null;
    if (typeof b === "string") { try { return JSON.parse(b); } catch (e) { return null; } }
    if (typeof FormData !== "undefined" && b instanceof FormData) {
      try { return JSON.parse(b.get("metrics") || "null"); } catch (e) { return null; }
    }
    return null;
  }
  function noteFor(url, data) {
    if (!data) return "";
    if (/suggest$/.test(url) && Array.isArray(data.suggestions)) {
      return data.suggestions.slice(0, 2).map((s) => `${s.artist} - ${s.title}`).join(", ") || "none";
    }
    if (/live\/ear$/.test(url)) return `${data.source === "AI" ? "Omni" : "rules"}: ${data.action}${data.reason ? " - " + data.reason.slice(0, 60) : ""}`;
    if (/stems$/.test(url)) return data.stems ? "ready: drums, bass, vocals, synths" : data.pending ? "separating in background" : "";
    if (/fame$/.test(url)) return data.views == null ? "" : `${(data.views / 1e6).toFixed(1)}M views${data.famous ? " · FAMOUS: plays in full" : ""}`;
    if (/vocals/.test(url)) return data.pending ? "separating in background" : data.regions ? `${data.regions.length} vocal regions` : "";
    if (/analysis$/.test(url) && data.bpm) return `${data.bpm} BPM ${data.key && data.key.camelot ? data.key.camelot : ""}`;
    if (/match$/.test(url) && data.candidates && data.candidates[0]) return `${data.candidates[0].recipe} ${Math.round(data.candidates[0].score)}`;
    if (/plan$/.test(url) && data.candidate) return data.candidate.recipe || "";
    return "";
  }
  const realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
    const method = ((init && init.method) || "GET").toUpperCase();
    const job = JOBS.find(([re]) => re.test(path));
    // download job polling (GET /api/download/jobs/<id>) and plain GETs of lists are not AI work
    if (!job || (/download\/jobs$/.test(path) && method !== "POST")) return realFetch(input, init);
    const b = bodyOf(init), m = path.match(job[0]);
    const id = row(job[1](b, m), job[2]);
    return realFetch(input, init).then((res) => {
      if (!res.ok) { finish(id, false, `HTTP ${res.status}`); return res; }
      res.clone().json().then((data) => finish(id, true, noteFor(path, data))).catch(() => finish(id, true));
      return res;
    }, (err) => { finish(id, false, err && err.name === "AbortError" ? "cancelled" : "failed"); throw err; });
  };

  let lastMove = "";
  window.addEventListener("ai-activity", (e) => {
    const d = e.detail || {};
    if (d.kind === "decision" && d.action && d.action !== "ride") {
      lastMove = String(d.action).replace(/_/g, " ");
      event(`${(d.deck || "").toUpperCase()} · ${lastMove.toUpperCase()}`, d.tag === "EAR" || String(d.tag).startsWith("EAR") ? "OMNI" : "MIND", (d.why || "").slice(0, 90));
    } else if (d.kind === "precheck") {
      const pct = d.score == null ? "unscored" : `${Math.round(d.score * 100)}%`;
      event(`Silent check: ${d.tried} loop spans`, "DSP", `picked ${d.span.bars} bars, seam ${pct} (crowd hears only this one)`);
    } else if (d.kind === "stem-move") {
      lastMove = String(d.label || "stems").toLowerCase();
      event(`${(d.deck || "").toUpperCase()} · ${d.label}`, "STEMS", d.why || "");
    } else if (d.kind === "silent-ear" && d.result) {
      const r = d.result;
      event("Omni heard the seam (silently)", "OMNI", `${r.verdict}${r.reason ? ": " + r.reason.slice(0, 70) : ""}`);
    }
  });

  // Background stem separation (server queue): one quiet progress row.
  let stemRow = null, stemTotal = 0;
  async function pollStems() {
    try {
      const st = await (await realFetch("/api/stems/status")).json();
      const left = st.backlog + st.urgent.length + (st.busy ? 1 : 0);
      stemTotal = Math.max(stemTotal, left + st.separated);
      if (!left) {
        if (stemRow) { stemRow.el.classList.add("ai-leaving"); setTimeout(() => stemRow && stemRow.el.remove(), 380); stemRow = null; }
        return;
      }
      if (!stemRow) {
        const el = document.createElement("li");
        el.className = "ai-row ai-running ai-row-stems";
        el.innerHTML = `<span class="ai-tag ai-tag-stems">STEMS</span><span class="ai-label"></span><span class="ai-time"></span>`;
        list.appendChild(el);
        stemRow = { el };
      }
      stemRow.el.querySelector(".ai-label").textContent =
        `Separating the library in the background${st.llm_busy && !st.urgent.length ? " (paused: LLM working)" : ""}`;
      stemRow.el.querySelector(".ai-time").textContent = `${left} left`;
    } catch (e) { /* server restarting */ }
  }
  pollStems();
  setInterval(pollStems, 8000);

  // --------------------------------------------------- 2. control glow --
  const CONTROL = ".eq-knob, .gain-knob, .volume-fader, #crossfader, .fx-type-btn, .deck-btn, .fx-knob, .pad, input[type=range], button";
  const glowing = new Map();       // el -> timeout
  let tag = null;
  function glow(el) {
    if (!el || !el.closest || el.closest(".ai-hud")) return;
    const c = el.closest(CONTROL);
    if (!c) return;
    const fresh = !glowing.has(c);
    clearTimeout(glowing.get(c));
    c.classList.add("ai-touch");
    glowing.set(c, setTimeout(() => { c.classList.remove("ai-touch"); glowing.delete(c); }, 1200));
    if (fresh) {                   // one layout read per touch burst, not per ramp step
      const r = c.getBoundingClientRect();
      if (!tag) { tag = document.createElement("div"); tag.className = "ai-touch-tag"; document.body.appendChild(tag); }
      tag.textContent = `AI${lastMove ? ": " + lastMove : ""}`;
      tag.style.transform = `translate(${Math.round(r.left + r.width / 2)}px, ${Math.round(r.top - 18)}px)`;
      tag.classList.remove("ai-touch-tag-on"); void tag.offsetWidth; tag.classList.add("ai-touch-tag-on");
    }
  }
  document.addEventListener("input", (e) => { if (!e.isTrusted) glow(e.target); }, true);
  document.addEventListener("click", (e) => { if (!e.isTrusted) glow(e.target); }, true);

  // ----------------------------------------------- 3. waveform markers --
  const layers = {};
  function layer(id) {
    if (layers[id]) return layers[id];
    const ov = document.getElementById(`overview-${id}`);
    if (!ov) return null;
    const el = document.createElement("div");
    el.className = "ai-ov";
    ov.appendChild(el);
    return (layers[id] = el);
  }
  function pct(t, dur) { return `${Math.max(0, Math.min(100, (t / dur) * 100)).toFixed(3)}%`; }
  let lastKey = {};
  // Update markers in place (same element per slot) so position changes glide.
  function morph(el, html) {
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    const next = [...tmp.children], cur = [...el.children];
    next.forEach((n, i) => {
      const c = cur[i];
      if (c && c.className === n.className) {
        c.style.cssText = n.getAttribute("style") || "";
        if (c.innerHTML !== n.innerHTML) c.innerHTML = n.innerHTML;
        if (n.title) c.title = n.title;
      } else if (c) el.replaceChild(n, c);
      else el.appendChild(n);
    });
    for (let i = next.length; i < cur.length; i++) cur[i].remove();
  }
  function draw() {
    const decks = window.decks || {};
    const st = window.djMind && window.djMind.overlayState ? window.djMind.overlayState() : {};
    for (const id of ["a", "b"]) {
      const d = decks[id], el = layer(id);
      if (!el) continue;
      const dur = d && d.buffer ? d.buffer.duration : 0;
      const voc = (d && d.analysis && d.analysis.vocal_active_regions) || [];
      const mine = st.deck === id;
      const hl = mine ? st.holdLoop : null;
      const key = JSON.stringify([dur, voc.length, mine && st.planFireAt, hl && [hl.start, hl.bars, hl.looping, (hl.tried || []).length]]);
      if (key === lastKey[id]) continue;
      lastKey[id] = key;
      if (!dur) { el.innerHTML = ""; continue; }
      let html = "";
      for (const [s, e] of voc) html += `<i class="ai-voc" style="left:${pct(s, dur)};width:${pct(e - s, dur)}"></i>`;
      if (hl && st.bar) {
        for (const c of hl.tried || []) {
          if (c.start === hl.start && c.bars === hl.bars) continue;
          html += `<i class="ai-try" title="tried: ${c.bars} bars, seam ${c.seam == null ? "?" : Math.round(c.seam * 100) + "%"}" style="left:${pct(c.start, dur)};width:${pct(c.bars * st.bar, dur)}"></i>`;
        }
        const seam = hl.seam == null ? "" : ` ${Math.round(hl.seam * 100)}%`;
        html += `<i class="ai-loop${hl.looping ? " ai-loop-on" : ""}" style="left:${pct(hl.start, dur)};width:${pct(hl.bars * st.bar, dur)}"><b>LOOP ${hl.bars}${seam}</b></i>`;
      }
      if (mine && st.planFireAt != null) html += `<i class="ai-exit" style="left:${pct(st.planFireAt, dur)}"><b>EXIT</b></i>`;
      morph(el, html);
    }
  }
  setInterval(draw, 500);

  // ------------------------------------------------------ 4. TRACK ID --
  // Now playing = the audible deck (playing, louder through the crossfader);
  // next = the other deck once loaded. A hash-only name is flagged ID?.
  const HASH = /^[0-9a-f]{16}$/i;
  function title(id) {
    const el = document.getElementById(`title-${id}`);
    const t = el ? el.textContent.trim() : "";
    return /^Load Track/i.test(t) ? "" : t;
  }
  function line(id) {
    const d = (window.decks || {})[id];
    const name = title(id);
    if (!d || !d.buffer || !name) return null;
    const a = d.analysis || {}, bits = [];
    if (d.bpm) bits.push(`${Math.round(d.bpm * 10) / 10} BPM`);
    if (a.key && a.key.camelot) bits.push(a.key.camelot);
    if (d.fame && d.fame.views) bits.push(`${d.fame.views >= 1e6 ? Math.round(d.fame.views / 1e6) + "M" : Math.round(d.fame.views / 1e3) + "K"} views${d.fame.famous ? " ★" : ""}`);
    return { html: `<b class="tid-deck tid-${id}">${id.toUpperCase()}</b> ${HASH.test(name) ? '<em class="tid-unknown">ID?</em> ' : ""}${esc(name)} <i>${bits.join(" · ")}</i>`,
             loud: d.playing ? (d.crossfaderGain ? d.crossfaderGain.gain.value : 1) * (d.volumeGain ? d.volumeGain.gain.value : 1) : -1 };
  }
  function esc(t) { return String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
  let tidKey = "";
  setInterval(() => {
    const a = line("a"), b = line("b");
    const [now, next] = !a ? [b, null] : !b ? [a, null] : a.loud >= b.loud ? [a, b] : [b, a];
    const key = `${now && now.html}|${next && next.html}`;
    if (key === tidKey) return;
    tidKey = key;
    const n = document.getElementById("tid-now"), x = document.getElementById("tid-next");
    if (n) n.innerHTML = now && now.loud >= 0 ? now.html : now ? now.html : "—";
    if (x) x.innerHTML = next ? next.html : "—";
  }, 700);
})();
