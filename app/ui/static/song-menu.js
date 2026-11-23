// SONG MENU (OD-2): the two on-demand moves "Lyric remix" and "Vocal swap" for any song in a song-pick list, relative to the song
// playing now. THIS file decides nothing: POST /api/song_menu/actions (dj-core fn "song_menu_actions") says for each candidate whether
// each move is available / preparing / not possible, why, and the plan to book. A pick loads the song on the idle deck (the next song)
// and books the move on the existing paths: lyric remix = lyric-remix.js request (its own phrase clock, readiness retry), vocal swap =
// learned-moves.js on-demand "vocal_swap" once the new song's stems are live. Pure helpers are node-checked
// (app/tests/js/song_menu_check.js).
"use strict";

const SongMenu = {
  TTL_MS: 8000,
  MOVES: [{ key: "lyricRemix", label: "Lyric remix" }, { key: "vocalSwap", label: "Vocal swap" }],
  esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
  // The words under a move: state chip + a plain sentence. eta_s is shown only when the server knows it.
  describe(action) {
    const a = action || {};
    const reason = String(a.reason || "").trim();
    if (a.state === "available") return { cls: "sm-available", chip: "Ready", text: reason, enabled: true };
    if (a.state === "preparing") {
      const eta = Number.isFinite(a.eta_s) && a.eta_s > 0 ? ` (about ${Math.round(a.eta_s)} s)` : "";
      return { cls: "sm-preparing", chip: "Getting ready", text: (reason || "preparing") + eta, enabled: true };
    }
    return { cls: "sm-no", chip: "Not possible", text: reason || "not possible for these two songs", enabled: false };
  },
  // Ids still needing an answer (not cached fresh), at most `max`.
  missing(ids, cache, aId, now, max = 50) {
    const out = [];
    for (const id of ids) {
      const hit = cache.get(`${aId}|${id}`);
      if (id && id !== aId && !(hit && now - hit.at < SongMenu.TTL_MS) && !out.includes(id) && out.length < max) out.push(id);
    }
    return out;
  },
  // What to do for a pick. st = {a: id on deck a|null, b: id on deck b|null, playing: "a"|"b"|null}. -> {steps} | {error}
  // steps: {load: deck, id} then {lyric: {plan, a, b}} | {swap: {a, b, deck}}
  steps(kind, bId, action, st) {
    const aId = st.playing ? st[st.playing] : null;
    if (!aId) return { error: "Start a song first, then choose what comes next." };
    if (bId === aId) return { error: "This song is already playing." };
    const d = SongMenu.describe(action);
    if (!d.enabled) return { error: `${kind === "lyricRemix" ? "Lyric remix" : "Vocal swap"} is not possible: ${d.text}.` };
    const idle = st.playing === "a" ? "b" : "a", steps = [];
    if (st[idle] !== bId) steps.push({ load: idle, id: bId });
    if (kind === "lyricRemix") {
      const plan = action && action.plan, id = plan && (plan.plan_id || plan.id);
      if (!id) return { error: "The lyric remix for these two songs is not ready yet." };
      steps.push({ lyric: { plan: String(id), a: aId, b: bId } });
    } else {
      steps.push({ swap: { a: aId, b: bId, deck: idle } });
    }
    return { steps };
  },
  rows(items) { return items; },
  // Popover markup for one candidate's answer.
  html(bId, item) {
    const body = SongMenu.MOVES.map((m) => {
      const d = SongMenu.describe(item && item[m.key]);
      return `<button class="song-menu-act ${d.cls}" data-songmenu-act="${m.key}" data-track="${SongMenu.esc(bId)}"${d.enabled ? "" : " aria-disabled=\"true\""}>
        <span class="sm-name">${m.label}</span><span class="sm-chip">${d.chip}</span><span class="sm-why">${SongMenu.esc(d.text)}</span></button>`;
    }).join("");
    return body;
  },
};
if (typeof module !== "undefined" && module.exports) module.exports = SongMenu;

(function () {
  if (typeof document === "undefined") return;
  const cache = new Map();                       // "a|b" -> {at, item}
  let pop = null, popFor = null;
  const note = (t) => { if (typeof setStatus === "function") setStatus(t); };
  const deckState = () => {
    const d = window.decks || {}, st = (typeof state !== "undefined" ? state : window.state) || {};
    return { a: st.trackA || null, b: st.trackB || null, playing: d.a && d.a.playing ? "a" : d.b && d.b.playing ? "b" : null };
  };
  const mod = (n) => (window.Engine && window.Engine.host && window.Engine.host.mod && window.Engine.host.mod[n]) || null;

  // One batched request for the ids not cached yet (visible lists call this with every row on screen).
  async function fetchActions(ids) {
    const st = deckState(), aId = st.playing ? st[st.playing] : null;
    if (!aId) return null;
    const need = SongMenu.missing(ids, cache, aId, Date.now());
    if (need.length) {
      const d = window.decks && window.decks[st.playing];
      const pos = d && typeof d._currentPosition === "function" ? d._currentPosition() : 0;
      const res = await fetch("/api/song_menu/actions", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ a: aId, bs: need, pos_s: Number.isFinite(pos) && pos > 0 ? pos : 0, relaxed: !!(window.Engine && window.Engine.host && window.Engine.host.session && window.Engine.host.session.relaxed) }) });
      if (!res.ok) throw new Error(res.statusText);
      const j = await res.json(), now = Date.now();
      if (cache.size > 400) cache.clear();
      for (const [id, item] of Object.entries(j.items || {})) cache.set(`${aId}|${id}`, { at: now, item });
    }
    return aId;
  }
  const itemFor = (aId, id) => { const h = cache.get(`${aId}|${id}`); return h ? h.item : null; };

  function close() { if (pop) { pop.remove(); pop = null; popFor = null; } }
  async function open(bId, anchor) {
    if (popFor === bId) return close();
    close();
    pop = document.createElement("div");
    pop.className = "song-menu-pop";
    pop.setAttribute("role", "menu");
    pop.innerHTML = `<div class="sm-head">Mix this song in next</div><div class="sm-body"><span class="sm-load">Checking…</span></div>`;
    document.body.appendChild(pop);
    popFor = bId;
    const r = anchor.getBoundingClientRect();
    pop.style.top = `${Math.min(window.innerHeight - 220, Math.max(8, r.bottom + 4))}px`;
    pop.style.left = `${Math.max(8, Math.min(window.innerWidth - 330, r.right - 320))}px`;
    const body = pop.querySelector(".sm-body");
    try {
      const aId = await fetchActions([bId]);
      if (popFor !== bId) return;
      if (!aId) { body.innerHTML = `<span class="sm-load">Start a song first, then choose what comes next.</span>`; return; }
      body.innerHTML = SongMenu.html(bId, itemFor(aId, bId));
    } catch (e) {
      if (popFor === bId) body.innerHTML = `<span class="sm-load">Could not check: ${SongMenu.esc(e.message)}</span>`;
    }
  }

  async function loadSong(deckLetter, id) {
    const res = await fetch(typeof StemFetch !== "undefined" ? StemFetch.trackAudioUrl(id) : `/api/audio/tracks/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error(res.statusText);
    const name = (window.trackBrowser && window.trackBrowser.nameOf(id)) || id;
    await loadIntoDeck(deckLetter, id, name, await res.blob());
  }
  const askStems = (id) => { if (id) fetch(`/api/tracks/${encodeURIComponent(id)}/separate?stems=4`, { method: "POST" }).catch(() => {}); };
  // The vocal swap runs on the existing artist move once the new song's stems are live; never earlier (it cannot work without them).
  async function swapWhenReady(s) {
    const lm = mod("learnedMoves"), deck = window.decks && window.decks[s.deck];
    if (!lm || !lm.onDemand || !lm.onDemand.vocal_swap) throw new Error("the vocal swap engine is not loaded");
    for (let i = 0; i < 240 && !(deck && deck.stemsReady); i++) {          // up to 2 min
      if (i === 0) note("Vocal swap: getting the stems of the next song ready…");
      await new Promise((r) => setTimeout(r, 500));
      const st = deckState();
      if (st.playing && st[st.playing] !== s.a) return note("Vocal swap dropped: the song playing changed.");
    }
    if (!(deck && deck.stemsReady)) return note("Vocal swap not played: the stems of the next song did not get ready in time.");
    const r = lm.onDemand.vocal_swap();
    note(r && r.ok === false ? `Vocal swap not played: ${r.why}` : "Vocal swap booked at the next phrase.");
  }

  async function pick(kind, bId) {
    close();
    try {
      const aId = await fetchActions([bId]);
      const plan = SongMenu.steps(kind, bId, aId ? itemFor(aId, bId) && itemFor(aId, bId)[kind] : null, deckState());
      if (plan.error) return note(plan.error);
      askStems(aId); askStems(bId);
      note("Loading the next song, getting stems…");
      for (const s of plan.steps) {
        if (s.load) { await loadSong(s.load, s.id); continue; }
        if (s.lyric) {
          const lr = mod("lyricRemix");
          if (!lr || !lr.request) throw new Error("the lyric remix engine is not loaded");
          lr.request(s.lyric);
          note("Lyric remix booked for the next phrase.");
        } else if (s.swap) {
          swapWhenReady(s).catch((e) => note(`Vocal swap failed: ${e.message}`));
        }
      }
    } catch (e) { note(`Could not book it: ${e.message}`); }
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-songmenu]");
    if (btn) { e.preventDefault(); e.stopPropagation(); return void open(btn.dataset.songmenu, btn); }
    const act = e.target.closest("[data-songmenu-act]");
    if (act) { if (act.getAttribute("aria-disabled") === "true") return; return void pick(act.dataset.songmenuAct, act.dataset.track); }
    if (pop && !e.target.closest(".song-menu-pop")) close();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  // lists warm the answers of the rows on screen in one request (browser.js / autopilot queue call this)
  window.songMenu = { warm: (ids) => fetchActions(ids).catch(() => {}), close, open };
})();
