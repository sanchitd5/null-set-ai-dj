// Lyric remix songs: a library panel listing accepted lyric-remix plans as playable songs (contract: wt/REMIXSONGS_CONTRACT.txt).
//   GET  /api/lyric_remix/songs?q=&limit=      list (title, new lyric text, score, state, progress)
//   GET  /api/lyric_remix/songs/<plan_id>      one song (+ beat grid when ready)
//   POST /api/lyric_remix/songs/<plan_id>/render
//   GET  /api/audio/remix/<plan_id>?q=phone    the rendered audio
//   GET/PUT /api/lyric_remix/settings          {ai_picks}
// PLAY NOW loads the song on deck A and starts it; QUEUE loads it as NEXT (deck B), like a library row. Both render first when needed.
// The server decides everything; this file only lists, renders on request and loads. Pure helpers are node-checked
// (app/tests/js/lyric_remix_songs_check.js).
"use strict";

const LRS = {
  POLL_MS: 1000,
  // Case-insensitive substring filter on title + text (the server filters too; this keeps typing instant).
  filter(songs, q) {
    const n = String(q || "").trim().toLowerCase();
    if (!n) return songs.slice();
    return songs.filter((s) => `${s.title || ""} ${s.text || ""}`.toLowerCase().includes(n));
  },
  planId(song) { return song && (song.plan_id || String(song.id || "").replace(/^lr-/, "")); },
  esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
  fmtScore(score) { return isFinite(score) ? `${Math.round(Math.max(0, Math.min(1, score)) * 100)}` : "--"; },
  fmtTime(sec) {
    if (!isFinite(sec) || sec <= 0) return "--:--";
    return `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;
  },
  // Badge label + progress percent (or null) for a song state.
  badge(song) {
    switch (song && song.state) {
      case "ready": return { label: "READY", pct: null, cls: "ready" };
      case "rendering": {
        const p = Math.round(Math.max(0, Math.min(1, +song.progress || 0)) * 100);
        return { label: song.stage ? `RENDERING ${p}% · ${song.stage}` : `RENDERING ${p}%`, pct: p, cls: "rendering" };
      }
      case "error": return { label: "ERROR", pct: null, cls: "error" };
      default: return { label: "NOT RENDERED", pct: null, cls: "none" };
    }
  },
  audioUrl(song) { return `/api/audio/remix/${encodeURIComponent(LRS.planId(song))}?q=phone`; },
  // Deck for an action: PLAY NOW -> a, QUEUE -> b (NEXT).
  deckFor(action) { return action === "queue" ? "b" : "a"; },
  // Analysis-shaped object from the song doc, used when /api/tracks/<id>/analysis has none for an lr- id.
  gridFrom(doc) {
    if (!doc || !Array.isArray(doc.beat_times) || !doc.beat_times.length) return null;
    return { bpm: doc.bpm || 0, duration: doc.duration || 0, beat_times: doc.beat_times, downbeat_times: doc.downbeat_times || [] };
  },
  row(song) {
    const b = LRS.badge(song), id = LRS.esc(song.id);
    const busy = song.state === "rendering";
    const actions = song.state === "error"
      ? `<span class="row-status" title="${LRS.esc(song.error || "")}">${LRS.esc(song.error || "render failed")}</span>
         <button class="hw-btn" data-lrs="retry" data-plan="${LRS.esc(LRS.planId(song))}">RETRY</button>`
      : `<button class="hw-btn" data-lrs="play" data-plan="${LRS.esc(LRS.planId(song))}"${busy ? " disabled" : ""}>PLAY NOW</button>
         <button class="hw-btn" data-lrs="queue" data-plan="${LRS.esc(LRS.planId(song))}"${busy ? " disabled" : ""}>QUEUE</button>`;
    return `<div class="browser-row lrs-row" data-song="${id}">
      <div class="row-title"><span class="row-txt"><span class="row-name" title="${LRS.esc(song.title)}">${LRS.esc(song.title)}</span>
        <span class="row-id lrs-text">${LRS.esc(song.text)}</span></span></div>
      <div class="num" title="score">${LRS.fmtScore(song.score)}</div>
      <div class="lrs-state"><span class="chip lrs-badge lrs-${b.cls}">${LRS.esc(b.label)}</span>${b.pct == null ? "" : `<div class="nrg-bar"><i style="width:${b.pct}%"></i></div>`}</div>
      <div class="lrs-actions">${actions}</div></div>`;
  },
};
if (typeof module !== "undefined" && module.exports) module.exports = LRS;

(function () {
  if (typeof document === "undefined") return;
  const host = document.getElementById("lrs-panel");
  if (!host) return;
  const rowsEl = document.getElementById("lrs-rows"), searchEl = document.getElementById("lrs-search");
  const aiEl = document.getElementById("lrs-ai-picks"), statusEl = document.getElementById("lrs-status");
  let songs = [], timer = null, loadSeq = 0;
  const note = (t) => { if (statusEl) statusEl.textContent = t || ""; if (t && typeof setStatus === "function") setStatus(t); };
  const json = async (url, opts) => {
    const res = await fetch(url, opts);
    if (!res.ok) throw new Error((await res.text().catch(() => "")) || res.statusText);
    return res.json();
  };

  function render() {
    const list = LRS.filter(songs, searchEl && searchEl.value);
    rowsEl.innerHTML = list.length ? list.map(LRS.row).join("")
      : `<div class="browser-empty">${songs.length ? "No lyric remix matches that search." : "No lyric remixes yet."}</div>`;
  }
  function upsert(song) {
    const i = songs.findIndex((s) => s.id === song.id);
    if (i >= 0) songs[i] = Object.assign(songs[i], song); else songs.push(song);
  }
  async function refresh() {
    const seq = ++loadSeq, q = searchEl ? searchEl.value.trim() : "";
    try {
      const d = await json(`/api/lyric_remix/songs?q=${encodeURIComponent(q)}&limit=200`);
      if (seq !== loadSeq) return;
      songs = d.songs || [];
      if (aiEl) aiEl.checked = !!d.ai_picks;
      render();
      if (songs.some((s) => s.state === "rendering")) schedulePoll();
    } catch (e) {
      rowsEl.innerHTML = `<div class="browser-empty">Could not load lyric remixes: ${LRS.esc(e.message)} <button class="hw-btn" data-lrs="reload">RETRY</button></div>`;
    }
  }
  function schedulePoll() { clearTimeout(timer); timer = setTimeout(refresh, LRS.POLL_MS); }

  // Render when needed, poll once a second until ready|error; returns the ready song doc.
  async function ensureReady(planId) {
    let song = songs.find((s) => LRS.planId(s) === planId) || { plan_id: planId, id: `lr-${planId}` };
    if (song.state !== "ready") {
      song = await json(`/api/lyric_remix/songs/${encodeURIComponent(planId)}/render`, { method: "POST" });
      upsert(song); render();
    }
    while (song.state === "rendering") {
      await new Promise((r) => setTimeout(r, LRS.POLL_MS));
      song = await json(`/api/lyric_remix/songs/${encodeURIComponent(planId)}`);
      upsert(song); render();
    }
    if (song.state === "error") throw new Error(song.error || "render failed");
    return song.sha256 ? song : json(`/api/lyric_remix/songs/${encodeURIComponent(planId)}`);
  }

  async function act(action, planId) {
    const deckLetter = LRS.deckFor(action);
    try {
      note("Rendering lyric remix…");
      const doc = await ensureReady(planId);
      note(`Loading ${doc.title} into deck ${deckLetter.toUpperCase()}…`);
      const res = await fetch(LRS.audioUrl(doc));
      if (!res.ok) throw new Error(res.statusText);
      await loadIntoDeck(deckLetter, doc.id, doc.title, await res.blob());
      const deck = window.decks && window.decks[deckLetter], grid = LRS.gridFrom(doc);
      if (deck && grid && !deck.analysis) {   // the track analysis route has nothing for an lr- id: use the song doc's grid
        deck.analysis = grid; if (grid.bpm > 0) deck.bpm = grid.bpm;
        const bpmEl = document.getElementById(`bpm-${deckLetter}`); if (bpmEl) bpmEl.textContent = deck.bpm.toFixed(1);
        if (typeof deck.renderBeatGrid === "function") deck.renderBeatGrid();
      }
      if (action === "play" && deck && typeof deck.play === "function") deck.play();
      note(`${doc.title} ${action === "play" ? "playing on" : "queued as NEXT on"} deck ${deckLetter.toUpperCase()}.`);
    } catch (e) { note(`Lyric remix failed: ${e.message}`); refresh(); }
  }

  rowsEl.addEventListener("click", (e) => {
    const b = e.target.closest("[data-lrs]");
    if (!b) return;
    if (b.dataset.lrs === "reload") return refresh();
    if (b.dataset.lrs === "retry") { const s = songs.find((x) => LRS.planId(x) === b.dataset.plan); if (s) { s.state = "none"; s.error = null; } return act("play", b.dataset.plan); }
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
  const btn = document.getElementById("lrs-refresh"); if (btn) btn.addEventListener("click", refresh);
  refresh();
})();
