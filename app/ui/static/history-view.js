// HISTORY: past sessions, REPLAY as is, TIME TRAVEL and LIKED, in the MACROS panel.
//
// Owner: "add feature to replay previously played track as is as well, and time travel to exact
// point"; "i really liked neverland -> nocturnal ..."; "i also like vocal throw a lot".
// Rows come from GET /api/sessions/{id}/timeline. "Replay from here" / "Replay this transition" /
// TIME TRAVEL ask POST /api/replay for a replay macro (source replay:<session>) and hand it to
// macro-mode.js startReplay: the song loads at its logged position and PLAY MACRO performs every
// step forced (stored recipe, exit, entry, merge), with every live gate. LIKE keeps the row's
// transition as stored (POST /api/liked).
// While a replay / liked step plays, its stored in-transition moves that have an on-demand entry
// point (artist-moves.js / fx-moves.js runNow) are fired at their logged time from the transition
// start (replaySchedule); the vocal throw rides the Echo Out recipe itself with its stored echo.
// Pure core (node-checked: app/tests/js/history_view_check.js) + runtime through the Host port.
(function (root) {
  // moves with an on-demand entry point, and the bars of lead each needs (it fires on the next line after that)
  const ARTIST_LEAD_BARS = { slip_loop: 4, pad_lead: 4, chant_gate: 2, dhol_drop: 2, cue_tease: 1, roll: 1, perc_bridge: 1 };
  const FX_LEAD_BARS = { mid_blend: 1, wet_band: 1, kick_roll: 1 };
  // a recipe that performs a move itself (autopilot.js executeTransition): not fired again
  const RECIPE_OWNS = { vocal_throw: /echo/i };

  const fmt = (t) => (Number.isFinite(t) ? `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}` : "?");
  const short = (n) => String(n || "").replace(/\s*[([].*$/, "").split(" - ").pop().slice(0, 28);

  // Pure: a timeline (GET /api/sessions/{id}/timeline) -> rows for the HISTORY list.
  function historyRows(tl) {
    return ((tl && tl.transitions) || []).map((t) => ({
      n: t.n, at: t.at || "", a: t.a, b: t.b, liked: !!t.liked,
      label: `${t.at || "?"} ${short(t.a_name)} > ${short(t.b_name)}`,
      detail: `${t.recipe || "?"}${t.planned && t.planned !== t.recipe ? ` (booked ${t.planned})` : ""} · exit ${fmt(t.a_time)} / entry ${fmt(t.b_time)}` +
        ((t.moves || []).length ? ` · ${(t.moves || []).map((m) => m.move).join(", ")}` : ""),
    }));
  }

  // Pure: the stored moves of a step to fire during its transition. o: {recipe (the one that runs),
  // barS (A's bar, s)} -> {fire: [{move, via, deckSide, callDt}], left: [{move, why}]}. callDt is
  // seconds after the transition start to call runNow (its lead taken off, never before 0).
  function replaySchedule(step, o = {}) {
    const fire = [], left = [];
    const bar = o.barS > 0 ? o.barS : 1.875;
    for (const m of (step && step.moves) || []) {
      const mv = m && m.move;
      if (!mv) continue;
      if (RECIPE_OWNS[mv] && RECIPE_OWNS[mv].test(o.recipe || "")) { left.push({ move: mv, why: "performed by the recipe (stored params)" }); continue; }
      const lead = ARTIST_LEAD_BARS[mv] != null ? ARTIST_LEAD_BARS[mv] : FX_LEAD_BARS[mv];
      if (lead == null) { left.push({ move: mv, why: "no on-demand entry point: left to the live rules" }); continue; }
      if (!Number.isFinite(m.dt)) { left.push({ move: mv, why: "no logged time" }); continue; }
      fire.push({ move: mv, via: ARTIST_LEAD_BARS[mv] != null ? "artist" : "fx", side: m.side || "a",
                  callDt: Math.max(0, +(m.dt - lead * bar).toFixed(3)) });
    }
    return { fire, left };
  }

  // Pure: a POST /api/replay answer -> what the console does, or {error}.
  function travelAction(res) {
    if (!res || typeof res !== "object") return { error: "no answer" };
    const m = res.replay && res.replay.macro;
    if (!m || !Array.isArray(m.steps) || !m.steps.length) return { error: "nothing to replay from there (the set had ended)" };
    const l = res.load;
    if (!l || !/^[0-9a-f]{16}$/.test(String(l.track_id || ""))) return { error: "no song to load" };
    const gaps = (res.replay.gaps || []).reduce((k, g) => k + ((g.gaps || []).length ? 1 : 0), 0);
    return { macro: m, load: { track_id: l.track_id, name: l.name || l.track_id, pos: Number.isFinite(l.pos) ? l.pos : 0 },
             line: `REPLAY ${m.steps.length} step(s) from ${short(l.name)} at ${fmt(l.pos)}${res.restarted_transition ? " (the transition restarts whole)" : ""}` +
                   `${gaps ? `, ${gaps} with log gaps` : ""}${res.replay.cut ? `; ${res.replay.cut}` : ""}` };
  }

  const core = { ARTIST_LEAD_BARS, FX_LEAD_BARS, historyRows, replaySchedule, travelAction, fmt };
  root.historyViewCore = core;
  if (typeof module !== "undefined" && module.exports) module.exports = core;

  // ---- runtime: the world only through the Host port (engine.js) ----
  function create({ host }) {
    const ui = host.ui;
    const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    let session = null;
    const say = (msg, ok = true) => { if (ui.status) ui.status(msg); if (!ok) console.warn(msg); else console.info(msg); };
    async function getJSON(url, opts) {
      const r = await host.api.fetch(url, opts);
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || `${url}: ${r.status}`);
      return d;
    }
    const post = (url, body) => getJSON(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

    async function listSessions() {
      const sel = ui.el("hist-session");
      if (!sel) return;
      try {
        const list = ((await getJSON("/api/sessions")).sessions || []).filter((s) => s.transitions > 0).slice(0, 40);
        sel.innerHTML = `<option value="">SESSIONS…</option>` + list.map((s) =>
          `<option value="${esc(s.id)}">${esc(s.id)} · ${s.transitions} transitions</option>`).join("");
      } catch (e) { say(`history: ${e.message}`, false); }
    }
    async function showSession(id) {
      session = id || null;
      const el = ui.el("hist-rows");
      if (!el) return;
      if (!id) { el.innerHTML = ""; return; }
      const rows = historyRows(await getJSON(`/api/sessions/${encodeURIComponent(id)}/timeline`));
      el.innerHTML = rows.map((r) => `<li title="${esc(r.detail)}"><span class="hist-label">${esc(r.label)}</span> <span class="hist-detail">${esc(r.detail)}</span>` +
        ` <button class="hw-btn" data-hist="from" data-n="${r.n}" title="Load A at this transition and replay the set from here">FROM HERE</button>` +
        ` <button class="hw-btn" data-hist="one" data-n="${r.n}" title="Replay this transition only, as it played">THIS ONE</button>` +
        ` <button class="hw-btn${r.liked ? " is-on" : ""}" data-hist="like" data-n="${r.n}" title="Keep this transition as it played (LIKED)">${r.liked ? "LIKED" : "LIKE"}</button></li>`).join("");
    }
    async function replay(body) {
      const mm = host.mod.macroMode;
      if (!mm || !mm.startReplay) return say("REPLAY: MACROS not loaded", false);
      try {
        const a = travelAction(await post("/api/replay", body));
        if (a.error) return say(`REPLAY: ${a.error}`, false);
        say(a.line);
        host.log.step("replay", { phase: "user", decision: "time travel", why: a.line });
        return mm.startReplay(a.macro, a.load);
      } catch (e) { return say(`REPLAY: ${e.message}`, false); }
    }
    async function like(n) {
      try {
        const d = await post("/api/liked", { session, n });
        say(`LIKED: ${short(d.liked.a_name)} > ${short(d.liked.b_name)} kept as played (${d.liked.step.recipe})`);
        if (host.mod.macroMode && host.mod.macroMode.refreshList) host.mod.macroMode.refreshList();
        showSession(session);
      } catch (e) { say(`LIKE: ${e.message}`, false); }
    }

    // stored in-transition moves of a replay / liked step, fired at their logged time
    function onCue(e) {
      const d = e && e.detail;
      const mm = host.mod.macroMode;
      if (!d || d.kind !== "transition" || !mm || !mm.storedMove) return;
      const st = host.state || {}, inn = d.deck, out = inn === "a" ? "b" : "a";
      const aId = out === "a" ? st.trackA : st.trackB, bId = inn === "a" ? st.trackA : st.trackB;
      const step = mm.storedStep ? mm.storedStep(aId, bId) : null;
      if (!step) return;
      const od = (host.decks || {})[out];
      const plan = replaySchedule(step, { recipe: (host.mod.autopilotState && host.mod.autopilotState.next && host.mod.autopilotState.next.recipe) || step.recipe,
                                          barS: 240 / ((od && od.bpm) || 128) });
      const t0 = Number.isFinite(d.at) ? d.at : host.clock.audioNow();
      for (const f of plan.fire) {
        const ms = Math.max(0, (t0 - host.clock.audioNow() + f.callDt) * 1000);
        host.clock.setTimeout(() => {
          const deck = (host.decks || {})[f.side === "b" ? inn : out];
          const r = f.via === "artist" ? host.mod.artistMoves && host.mod.artistMoves.runNow(f.move, { deck, inTransition: true })
            : host.mod.fxMoves && host.mod.fxMoves.runNow(f.move);
          host.log.step("replay_move", { deck: deck && deck.id, decision: f.move, why: r && r.ok ? "stored move replayed" : `refused: ${(r && r.why) || "not loaded"}` });
        }, ms);
      }
      for (const l of plan.left) host.log.step("replay_move", { decision: l.move, why: l.why });
    }
    if (host.bus && host.bus.on) host.bus.on("ai-cue", onCue);

    const on = (id, ev, fn) => { const el = ui.el(id); if (el) el.addEventListener(ev, fn); };
    on("hist-session", "change", (e) => showSession(e.target.value).catch((x) => say(x.message, false)));
    on("hist-refresh", "click", listSessions);
    on("hist-go", "click", () => {
      const at = ui.el("hist-at");
      if (!session) return say("TIME TRAVEL: pick a session", false);
      if (!at || !at.value.trim()) return say("TIME TRAVEL: give HH:MM:SS (the step log's clock) or seconds into the set", false);
      return replay({ session, at: at.value.trim() });
    });
    on("hist-rows", "click", (e) => {
      const b = e.target && e.target.closest && e.target.closest("[data-hist]");
      if (!b || !session) return;
      const n = Number(b.dataset.n);
      if (b.dataset.hist === "from") replay({ session, step: n });
      else if (b.dataset.hist === "one") replay({ session, step: n, to: n });
      else like(n);
    });
    listSessions();
    return { core, listSessions, showSession, replay, like, onCue };
  }
  if (root.Engine) root.Engine.mount("historyView", create);
})(typeof window !== "undefined" ? window : globalThis);
