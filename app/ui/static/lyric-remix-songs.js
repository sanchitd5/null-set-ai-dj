// Lyric remixes (web console panel). A remix is performed LIVE, never rendered: tapping a row plays the real song (or the A -> B pair) and
// asks lyric-remix.js to perform the plan from the stems (dj-core decides; same move as the AI DJ's own). Contract:
//   GET     /api/lyric_remix/songs?q=&limit=   list: {plan_id, kind, a, b, title, text, score}
//   GET/PUT /api/lyric_remix/settings          {ai_picks, min_songs}
//   POST    /api/tracks/<id>/separate?stems=4  stems are requested as soon as a row is tapped
// PLAY NOW loads the song on the playing deck (or A) and plays it; BOOK NEXT loads it on the idle deck. A pair also loads B on the other
// deck. Stems late: the plain song plays and the remix is retried every phrase ("Getting stems for the remix"). lyric-remix.js drives a
// requested remix on its own phrase clock (AI DJ on or off); the status here follows it: Remix in N bars / Remix playing / the refusal. Pure helpers are
// node-checked (app/tests/js/lyric_remix_songs_check.js).
"use strict";

const LRS = {
  POLL_MS: 1000,
  // Case-insensitive substring filter on title + text (the server filters too; this keeps typing instant).
  filter(songs, q) {
    const n = String(q || "").trim().toLowerCase();
    if (!n) return songs.slice();
    return songs.filter((s) => `${s.title || ""} ${s.text || ""}`.toLowerCase().includes(n));
  },
  planId(song) { return song && song.plan_id; },
  esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
  fmtScore(score) { return isFinite(score) ? `${Math.round(Math.max(0, Math.min(1, score)) * 100)}` : "--"; },
  kindLabel(song) { return song && song.b ? "PAIR · LIVE BLEND" : song && song.kind === "full" ? "WHOLE SONG · LIVE" : "LIVE IN THE SONG"; },
  // What to do for a tap. st = {a: track id on deck a|null, b: id on deck b|null, playing: "a"|"b"|null}; action "play" | "queue".
  // -> {steps: [{load, id, play?} ... , {request: {plan, a, b}}]} | {error}. The request always comes last: the remix is booked on top of
  // whatever plays now.
  steps(song, action, st) {
    const A = song.a, B = song.b || null, other = (d) => (d === "a" ? "b" : "a");
    const onAir = st.playing ? st[st.playing] : null, steps = [];
    if (onAir === A) {
      if (B) steps.push({ load: other(st.playing), id: B });
    } else if (action === "queue" && st.playing) {
      if (B) return { error: "A is not on air: use PLAY NOW for a pair remix" };
      steps.push({ load: other(st.playing), id: A });
    } else {
      const dA = st.playing || "a";
      steps.push({ load: dA, id: A, play: true });
      if (B) steps.push({ load: other(dA), id: B });
    }
    steps.push({ request: { plan: song.plan_id, a: A, b: B } });
    return { steps };
  },
  row(song) {
    const plan = LRS.esc(LRS.planId(song));
    return `<div class="browser-row lrs-row" data-song="${plan}">
      <div class="row-title"><span class="row-txt"><span class="row-name" title="${LRS.esc(song.title)}">${LRS.esc(song.title)}</span>
        <span class="row-id lrs-text">${LRS.esc(song.text)}</span></span></div>
      <div class="num" title="score">${LRS.fmtScore(song.score)}</div>
      <div class="lrs-state"><span class="chip lrs-badge lrs-none">${LRS.kindLabel(song)}</span></div>
      <div class="lrs-actions"><button class="hw-btn" data-lrs="play" data-plan="${plan}">PLAY NOW</button>
         <button class="hw-btn" data-lrs="queue" data-plan="${plan}">BOOK NEXT</button></div></div>`;
  },
};
if (typeof module !== "undefined" && module.exports) module.exports = LRS;

(function () {
  if (typeof document === "undefined") return;
  const host = document.getElementById("lrs-panel");
  if (!host) return;
  const rowsEl = document.getElementById("lrs-rows"), searchEl = document.getElementById("lrs-search");
  const aiEl = document.getElementById("lrs-ai-picks"), statusEl = document.getElementById("lrs-status");
  let songs = [], loadSeq = 0, statusTimer = null;
  const note = (t) => { if (statusEl) statusEl.textContent = t || ""; if (t && typeof setStatus === "function") setStatus(t); };
  const json = async (url, opts) => {
    const res = await fetch(url, opts);
    if (!res.ok) throw new Error((await res.text().catch(() => "")) || res.statusText);
    return res.json();
  };
  const lyricRemix = () => (window.Engine && window.Engine.host && window.Engine.host.mod && window.Engine.host.mod.lyricRemix) || null;

  function render() {
    const list = LRS.filter(songs, searchEl && searchEl.value);
    rowsEl.innerHTML = list.length ? list.map(LRS.row).join("")
      : `<div class="browser-empty">${songs.length ? "No lyric remix matches that search." : "No lyric remixes yet."}</div>`;
  }
  async function refresh() {
    const seq = ++loadSeq, q = searchEl ? searchEl.value.trim() : "";
    try {
      const d = await json(`/api/lyric_remix/songs?q=${encodeURIComponent(q)}&limit=200`);
      if (seq !== loadSeq) return;
      songs = d.songs || [];
      if (aiEl) aiEl.checked = !!d.ai_picks;
      render();
    } catch (e) {
      rowsEl.innerHTML = `<div class="browser-empty">Could not load lyric remixes: ${LRS.esc(e.message)} <button class="hw-btn" data-lrs="reload">RETRY</button></div>`;
    }
  }
  // Stems are requested at once (stem server); fire and forget, the deck loads them when they are there.
  const askStems = (id) => { if (id) fetch(`/api/tracks/${encodeURIComponent(id)}/separate?stems=4`, { method: "POST" }).catch(() => {}); };
  async function load(deckLetter, id) {
    const res = await fetch((typeof StemFetch !== "undefined" ? StemFetch.trackAudioUrl(id) : `/api/audio/tracks/${encodeURIComponent(id)}`));
    if (!res.ok) throw new Error(res.statusText);
    const name = (songs.find((s) => s.a === id) || {}).title || id;
    await loadIntoDeck(deckLetter, id, String(name).replace(/ - lyric remix$/, ""), await res.blob());
  }
  const deckState = () => {
    const d = window.decks || {}, st = (typeof state !== "undefined" ? state : window.state) || {};
    return { a: st.trackA || null, b: st.trackB || null, playing: d.a && d.a.playing ? "a" : d.b && d.b.playing ? "b" : null };
  };
  // Shows the engine's wait state ("Getting stems for the remix") until the remix plays or is dropped.
  function watchStatus() {
    clearInterval(statusTimer);
    statusTimer = setInterval(() => {
      const lr = lyricRemix();
      if (!lr) return clearInterval(statusTimer);
      if (lr.status) note(lr.status);
      if (!lr.busy) { clearInterval(statusTimer); note(lr.status || ""); }
    }, LRS.POLL_MS);
  }

  async function act(action, planId) {
    const song = songs.find((s) => LRS.planId(s) === planId);
    if (!song) return;
    try {
      const lr = lyricRemix();
      if (!lr || !lr.request) throw new Error("the lyric remix engine is not loaded");
      const plan = LRS.steps(song, action, deckState());
      if (plan.error) return note(plan.error);
      askStems(song.a); askStems(song.b);
      note("Loading the song(s), getting stems…");
      for (const st of plan.steps) {
        if (st.request) { lr.request(st.request); continue; }
        await load(st.load, st.id);
        const deck = window.decks && window.decks[st.load];
        if (st.play && deck && typeof deck.play === "function") deck.play();
      }
      note(`Lyric remix booked: ${song.text}`);
      watchStatus();
    } catch (e) { note(`Lyric remix failed: ${e.message}`); }
  }

  rowsEl.addEventListener("click", (e) => {
    const b = e.target.closest("[data-lrs]");
    if (!b) return;
    if (b.dataset.lrs === "reload") return refresh();
    act(b.dataset.lrs, b.dataset.plan);
  });
  let searchTimer = null;
  if (searchEl) searchEl.addEventListener("input", () => { render(); clearTimeout(searchTimer); searchTimer = setTimeout(refresh, 300); });
  if (aiEl) {
    json("/api/lyric_remix/settings").then((s) => { aiEl.checked = !!(s && s.ai_picks); }).catch(() => {});
    aiEl.addEventListener("change", async () => {
      const want = aiEl.checked;
      try { await json("/api/lyric_remix/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ai_picks: want }) }); }
      catch (e) { aiEl.checked = !want; note(`Could not save the setting: ${e.message}`); }
    });
  }
  const autoEl = document.getElementById("lrs-auto-remix");
  if (autoEl) {                                           // AUTO-REMIX (default on): remixable songs perform their remix by themselves
    json("/api/lyric_remix/settings").then((s) => { autoEl.checked = !s || s.auto_remix !== false; }).catch(() => {});
    autoEl.addEventListener("change", async () => {
      const want = autoEl.checked;
      try {
        await json("/api/lyric_remix/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ auto_remix: want }) });
        const lr = lyricRemix(); if (lr && lr.setAuto) lr.setAuto(want);
      } catch (e) { autoEl.checked = !want; note(`Could not save the setting: ${e.message}`); }
    });
  }
  const btn = document.getElementById("lrs-refresh"); if (btn) btn.addEventListener("click", refresh);
  refresh();
})();
