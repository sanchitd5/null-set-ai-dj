// AI Music Brain - LEARNING panel: progress of the set studies (agent_bridge learn-set) running on this machine.
//
// core   pure and node-testable (app/tests/learn_progress_check.js): which studies to show, how to format them,
//        when to poll next, and the poller itself over injected clock / fetch / visibility.
// glue   thin: mounts through the Host port (host.api.fetch, host.clock, host.ui) and only touches
//        textContent / style.width of a few nodes, so the poll never costs audio or main-thread time
//        (docs/engineering/PERFORMANCE_AUDIT.md). It is skipped by the sim (sim/js/env.js), like host-browser.js.
//
// Server side: GET /api/learn/progress -> {studies: [{id, title, state, stage, counts, tracks_done, tracks_total,
// current, elapsed_s, eta_s, techniques_found, warnings, error, updated_at, finished_at, ...}]} (learn_progress.py).
(function (root) {
  "use strict";

  const SHOW_AFTER_END_MS = 10 * 60 * 1000;   // a finished (or dead) study stays visible this long
  const POLL_BUSY_MS = 5000;                  // something running
  const POLL_IDLE_MS = 30000;                 // nothing to show
  const MAX_SHOWN = 3;
  const STAGE_LABEL = { fetch: "fetching songs", cut: "cutting clips", separate: "separating songs", analyze: "locating stems",
    detect: "detecting moves", lyrics: "lyrics", ai_review: "AI review", merge: "merging", cleanup: "cleaning up", done: "done", error: "failed" };

  // ---- core ------------------------------------------------------------------------------------------------------------------
  // when the study last mattered: its end for done/error, its last heartbeat for stale
  const endedAtMs = (s) => 1000 * Number(s.state === "stale" ? s.updated_at : (s.finished_at || s.updated_at) || 0);

  function isShown(s, nowMs) {
    if (!s) return false;
    if (s.state === "running") return true;
    return nowMs - endedAtMs(s) <= SHOW_AFTER_END_MS;
  }

  // running first, then newest; at most MAX_SHOWN
  function pick(studies, nowMs) {
    const rank = (s) => (s.state === "running" ? 0 : 1);
    return (Array.isArray(studies) ? studies : []).filter((s) => isShown(s, nowMs))
      .sort((a, b) => rank(a) - rank(b) || (b.updated_at || 0) - (a.updated_at || 0)).slice(0, MAX_SHOWN);
  }

  function fmtDuration(sec) {
    if (sec === null || sec === undefined || !isFinite(sec)) return "";
    const s = Math.max(0, Math.round(sec));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m`;
    return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
  }

  // what the panel prints for one study
  function view(s) {
    const c = (s.counts && s.counts[s.stage]) || null;
    const total = c ? c.total : 0;
    const finished = s.state === "done";
    const pct = finished ? 100 : total > 0 ? Math.min(100, Math.round((100 * c.done) / total)) : null;   // null: indeterminate
    const running = s.state === "running";
    return {
      id: s.id,
      state: s.state,
      title: s.title || s.id,
      stage: s.state === "stale" ? "stopped (no heartbeat)" : (STAGE_LABEL[s.stage] || s.stage || ""),
      count: c && total > 0 ? `${c.done}/${total}` : "",
      part: s.parts > 1 ? `part ${s.part}/${s.parts}` : "",
      pct,
      current: running && s.current ? String(s.current) : "",
      elapsed: fmtDuration(s.elapsed_s),
      eta: running && s.eta_s ? `~${fmtDuration(s.eta_s)} left` : "",
      chips: Object.entries(s.techniques_found || {}).sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)).map(([kind, n]) => ({ kind, n })),
      warnings: (s.warnings || []).slice(-3),
      warningsMore: Math.max(0, (s.warnings || []).length - 3),
      error: s.error ? String(s.error).slice(0, 200) : "",
    };
  }

  // ms until the next poll; null = do not poll (page hidden)
  function nextDelay(studies, nowMs, pageVisible, failed) {
    if (!pageVisible) return null;
    if (failed) return POLL_IDLE_MS;
    return pick(studies, nowMs).some((s) => s.state === "running") ? POLL_BUSY_MS : POLL_IDLE_MS;
  }

  // The poller over injected deps: {fetchJson(url) -> Promise<obj>, setTimeout, clearTimeout, now(), visible(), onData(studies)}.
  // One request in flight at most; a hidden page pauses it (kick() resumes); an error backs off to the idle rate.
  function createPoller(d) {
    let timer = null, busy = false, last = [], failed = false, stopped = true;
    const schedule = () => {
      if (stopped) return;
      const ms = nextDelay(last, d.now(), d.visible(), failed);
      if (ms === null) { timer = null; return; }
      timer = d.setTimeout(tick, ms);
    };
    async function tick() {
      timer = null;
      if (stopped || busy) return;
      if (!d.visible()) return schedule();
      busy = true;
      try {
        const data = await d.fetchJson("/api/learn/progress");
        last = Array.isArray(data && data.studies) ? data.studies : [];
        failed = false;
        d.onData(last);
      } catch (e) { failed = true; }
      busy = false;
      schedule();
    }
    return {
      start() { stopped = false; if (timer === null) tick(); },
      stop() { stopped = true; if (timer !== null) d.clearTimeout(timer); timer = null; },
      kick() { if (!stopped && timer === null && !busy) tick(); },   // page became visible again
      get last() { return last; },
    };
  }

  const core = { SHOW_AFTER_END_MS, POLL_BUSY_MS, POLL_IDLE_MS, MAX_SHOWN, isShown, pick, fmtDuration, view, nextDelay, createPoller };

  // ---- glue ------------------------------------------------------------------------------------------------------------------
  function create({ host }) {
    const panel = host.ui.el("learn-panel");
    const body = host.ui.el("learn-body");
    const head = host.ui.el("learn-summary");
    if (!panel || !body) return null;
    const rows = new Map();                                   // study id -> its nodes (updated in place)

    const el = (tag, cls) => { const n = host.ui.create(tag); if (cls) n.className = cls; return n; };
    const setText = (n, t) => { if (n.textContent !== t) n.textContent = t; };

    function makeRow() {
      const r = { root: el("div", "lp-row") };
      r.top = el("div", "lp-top"); r.title = el("span", "lp-title"); r.stage = el("span", "lp-stage");
      r.top.append(r.title, r.stage);
      r.bar = el("div", "lp-bar"); r.bar.setAttribute("role", "progressbar");
      r.fill = el("div", "lp-fill"); r.bar.appendChild(r.fill);
      r.meta = el("div", "lp-meta");
      r.chips = el("div", "lp-chips");
      r.note = el("div", "lp-note");
      r.root.append(r.top, r.bar, r.meta, r.chips, r.note);
      return r;
    }

    function paint(r, v) {
      r.root.className = `lp-row lp-${v.state}`;
      setText(r.title, v.title);
      setText(r.stage, [v.stage, v.count, v.part && `(${v.part})`].filter(Boolean).join(" "));
      if (v.pct === null) { r.fill.classList.add("indeterminate"); r.fill.style.width = ""; r.bar.removeAttribute("aria-valuenow"); }
      else { r.fill.classList.remove("indeterminate"); r.fill.style.width = `${v.pct}%`; r.bar.setAttribute("aria-valuenow", String(v.pct)); }
      setText(r.meta, [v.current, v.elapsed && `${v.elapsed} elapsed`, v.eta].filter(Boolean).join(" · "));
      const sig = v.chips.map((c) => `${c.kind}:${c.n}`).join(",");
      if (r.sig !== sig) {                                    // rebuild the chips only when a count changed
        r.sig = sig;
        r.chips.textContent = "";
        for (const c of v.chips) { const s = el("span", "lp-chip"); s.textContent = `${c.kind} ${c.n}`; r.chips.appendChild(s); }
      }
      const note = v.error ? `error: ${v.error}` : v.warnings.length ? v.warnings.join(" | ") + (v.warningsMore ? ` (+${v.warningsMore} more)` : "") : "";
      setText(r.note, note);
      r.note.classList.toggle("lp-err", !!v.error);
    }

    function render(studies) {
      const shown = pick(studies, host.clock.now());
      panel.hidden = shown.length === 0;
      const keep = new Set(shown.map((s) => s.id));
      for (const [id, r] of rows) if (!keep.has(id)) { r.root.remove(); rows.delete(id); }
      for (const s of shown) {
        let r = rows.get(s.id);
        if (!r) { r = makeRow(); rows.set(s.id, r); body.appendChild(r.root); }
        paint(r, view(s));
      }
      if (head) setText(head, shown.length ? `(${shown.filter((s) => s.state === "running").length} running)` : "");
    }

    const visible = () => typeof document === "undefined" || document.visibilityState !== "hidden";
    const poller = createPoller({
      fetchJson: async (url) => { const res = await host.api.fetch(url); if (!res.ok) throw new Error(`learn progress ${res.status}`); return res.json(); },
      setTimeout: host.clock.setTimeout, clearTimeout: host.clock.clearTimeout, now: host.clock.now, visible, onData: render,
    });
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", () => { if (visible()) poller.kick(); });
    poller.start();
    return { poller, render };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (root.Engine && typeof root.Engine.mount === "function") root.Engine.mount("learnProgress", create);
})(typeof window !== "undefined" ? window : globalThis);
