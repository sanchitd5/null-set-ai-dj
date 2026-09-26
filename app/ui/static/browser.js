// AI Music Brain — track browser.
//
// Real data only, straight off the existing REST surface:
//   GET /api/tracks                     -> every track that has been uploaded
//   GET /api/tracks/{id}/analysis       -> bpm, camelot key, duration, energy curve
//   GET /api/audio/tracks/{id}          -> the stored audio, streamed back
//
// Analyses are cached in-memory per track_id (the first analysis of a track can
// take seconds on the server; after that it is served from the server's own
// JSON cache, and from this map within a session).
//
// Deliberately NOT rendered: crates/playlists, star ratings, an automix queue.
// None of them exist in this product, and inventing them here would be a lie
// about what the tool can do. "AVG NRG" is the mean of the analyzer's own
// normalized RMS energy curve (0-1, relative to each track's own peak) — it is
// labelled as that, not as a 0-10 "energy score".
//
// Depends on `state` + `loadIntoDeck` (app.js) and `decks` (deck-controller.js).

(function () {
  const rowsEl = document.getElementById("browser-rows");
  const searchEl = document.getElementById("browser-search");
  const refreshEl = document.getElementById("browser-refresh");
  const libraryScanEl = document.getElementById("browser-library-scan");
  if (!rowsEl) return;

  const analysisCache = new Map(); // track_id -> analysis | "pending" | "error"
  let tracks = [];
  let sortKey = "name";
  let sortDir = 1;

  const baseName = (p) => String(p).replace(/\\/g, "/").split("/").pop();

  function fmtTime(seconds) {
    if (!isFinite(seconds) || seconds <= 0) return "--:--";
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60).toString().padStart(2, "0");
    return `${m}:${s}`;
  }

  function avgEnergy(analysis) {
    const curve = analysis && analysis.energy_curve;
    if (!curve || !curve.length) return null;
    return curve.reduce((a, b) => a + b, 0) / curve.length;
  }

  async function fetchAnalysis(trackId) {
    if (analysisCache.has(trackId)) return analysisCache.get(trackId);
    analysisCache.set(trackId, "pending");
    try {
      const res = await fetch(`/api/tracks/${trackId}/analysis`);
      if (!res.ok) throw new Error(res.statusText);
      const data = await res.json();
      analysisCache.set(trackId, data);
      return data;
    } catch (e) {
      analysisCache.set(trackId, "error");
      return "error";
    }
  }

  // Analyse a few at a time so a big library does not fire 50 parallel
  // librosa requests at the backend.
  async function warmAnalyses(list) {
    const queue = list.slice();
    const workers = new Array(2).fill(0).map(async () => {
      while (queue.length) {
        const t = queue.shift();
        await fetchAnalysis(t.track_id);
        render();
      }
    });
    await Promise.all(workers);
  }

  function deckStatus(trackId) {
    if (state.trackA === trackId) return "a";
    if (state.trackB === trackId) return "b";
    return null;
  }

  function matches(track, query) {
    if (!query) return true;
    const a = analysisCache.get(track.track_id);
    const parts = [track.name, track.track_id];
    if (a && a !== "pending" && a !== "error") {
      parts.push(a.bpm ? a.bpm.toFixed(1) : "");
      parts.push(a.key ? `${a.key.camelot} ${a.key.key_name}` : "");
    }
    return parts.join(" ").toLowerCase().includes(query.toLowerCase());
  }

  function sortValue(track) {
    const a = analysisCache.get(track.track_id);
    const ready = a && a !== "pending" && a !== "error";
    switch (sortKey) {
      case "bpm": return ready ? a.bpm : -1;
      case "key": return ready && a.key ? a.key.camelot : "";
      case "energy": return ready ? (avgEnergy(a) || 0) : -1;
      default: return track.name.toLowerCase();
    }
  }

  function render() {
    const query = searchEl ? searchEl.value.trim() : "";
    const visible = tracks.filter((t) => matches(t, query));
    visible.sort((x, y) => {
      const vx = sortValue(x);
      const vy = sortValue(y);
      if (vx < vy) return -1 * sortDir;
      if (vx > vy) return 1 * sortDir;
      return 0;
    });

    if (!visible.length) {
      rowsEl.innerHTML = `<div class="browser-empty">${
        tracks.length ? "No uploaded track matches that search." : "No tracks uploaded yet — load a file on either deck first."
      }</div>`;
      return;
    }

    rowsEl.innerHTML = visible.map((t) => {
      const a = analysisCache.get(t.track_id);
      const ready = a && a !== "pending" && a !== "error";
      const on = deckStatus(t.track_id);
      const nrg = ready ? avgEnergy(a) : null;
      const bpm = ready && a.bpm ? a.bpm.toFixed(1) : (a === "error" ? "n/a" : "…");
      const camelot = ready && a.key ? a.key.camelot : "";
      const keyName = ready && a.key ? a.key.key_name : "";
      return `
        <div class="browser-row ${on ? "on-" + on : ""}" data-track="${t.track_id}">
          <div class="row-load">
            ${on
              ? `<span class="chip">ON ${on.toUpperCase()}</span>`
              : `<button class="hw-btn load-a" data-load="a" data-track="${t.track_id}">A</button>
                 <button class="hw-btn load-b" data-load="b" data-track="${t.track_id}">B</button>`}
          </div>
          <div class="row-title">
            <span class="row-name" title="${t.name}">${t.name}</span>
            <span class="row-id">${t.track_id}</span>
          </div>
          <div class="num">${bpm}</div>
          <div>${camelot ? `<span class="key-chip" title="${keyName}">${camelot}</span>` : `<span class="row-status">--</span>`}</div>
          <div class="num">${ready ? fmtTime(a.duration) : "--:--"}</div>
          <div>${nrg === null
            ? `<span class="row-status">--</span>`
            : `<div class="nrg-bar" title="Mean normalized RMS energy: ${nrg.toFixed(2)} of 1.00"><i style="width:${(nrg * 100).toFixed(0)}%"></i></div><span class="row-status">${nrg.toFixed(2)}</span>`}</div>
          <div class="row-status right ${on ? "playing-" + on : ""}">${on ? "LOADED " + on.toUpperCase() : (ready ? "READY" : "ANALYSING")}</div>
        </div>`;
    }).join("");
  }

  async function loadToDeck(trackId, deckLetter) {
    const track = tracks.find((t) => t.track_id === trackId);
    const name = track ? track.name : trackId;
    setStatus(`Loading ${name} into deck ${deckLetter.toUpperCase()}…`);
    try {
      const res = await fetch(`/api/audio/tracks/${trackId}`);
      if (!res.ok) throw new Error(res.statusText);
      const blob = await res.blob();
      // Same load path as a fresh upload: the deck rebuilds its waveform,
      // analysis, BPM, key, beat grid and trim region exactly as it would from
      // the file picker — no re-upload needed, the track is already stored.
      await loadIntoDeck(deckLetter, trackId, name, blob);
      setStatus(`${name} loaded into deck ${deckLetter.toUpperCase()}.`);
    } catch (e) {
      setStatus(`Could not load ${name}: ${e.message}`);
    }
    render();
  }

  rowsEl.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-load]");
    if (!btn) return;
    loadToDeck(btn.dataset.track, btn.dataset.load);
  });

  if (searchEl) searchEl.addEventListener("input", render);

  document.querySelectorAll(".sort-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (sortKey === btn.dataset.sort) sortDir *= -1;
      else { sortKey = btn.dataset.sort; sortDir = 1; }
      document.querySelectorAll(".sort-btn").forEach((b) => b.classList.toggle("is-on", b === btn));
      btn.textContent = `${btn.dataset.sort.toUpperCase()} ${sortDir > 0 ? "↑" : "↓"}`;
      render();
    });
  });

  async function refresh() {
    try {
      const res = await fetch("/api/tracks");
      const data = await res.json();
      tracks = (data.tracks || []).map((t) => ({ track_id: t.track_id, name: baseName(t.path) }));
      render();
      warmAnalyses(tracks.filter((t) => !analysisCache.has(t.track_id)));
    } catch (e) {
      rowsEl.innerHTML = `<div class="browser-empty">Could not reach /api/tracks: ${e.message}</div>`;
    }
  }

  if (refreshEl) refreshEl.addEventListener("click", refresh);
  if (libraryScanEl) libraryScanEl.addEventListener("click", async () => {
    libraryScanEl.disabled = true;
    const original = libraryScanEl.textContent;
    libraryScanEl.textContent = "SCANNING…";
    try {
      const res = await fetch("/api/library/scan", { method: "POST" });
      if (!res.ok) throw new Error(res.statusText);
      const data = await res.json();
      setStatus(`${data.count || 0} configured library track${data.count === 1 ? "" : "s"} available.`);
      await refresh();
    } catch (e) {
      setStatus(`Could not scan configured library: ${e.message}`);
    } finally {
      libraryScanEl.disabled = false;
      libraryScanEl.textContent = original;
    }
  });
  // A freshly uploaded track becomes a browser row straight away.
  document.addEventListener("deck-track-loaded", () => { refresh(); });

  // --- URL import (YouTube / YouTube Music) ---
  const urlInput = document.getElementById("url-import-input");
  const urlBtn = document.getElementById("url-import-btn");
  const urlStatusEl = document.getElementById("url-import-status");

  async function importUrl() {
    const url = urlInput ? urlInput.value.trim() : "";
    if (!url) return;
    if (urlBtn) { urlBtn.disabled = true; urlBtn.textContent = "IMPORTING…"; }
    if (urlStatusEl) urlStatusEl.textContent = "Downloading…";
    try {
      const res = await fetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      const count = data.tracks ? data.tracks.length : 0;
      if (urlStatusEl) urlStatusEl.textContent = `✓ ${count} track${count === 1 ? "" : "s"} imported from ${data.source}.`;
      if (urlInput) urlInput.value = "";
      await refresh();
    } catch (e) {
      if (urlStatusEl) urlStatusEl.textContent = `✗ ${e.message}`;
      setStatus(`Import failed: ${e.message}`);
    } finally {
      if (urlBtn) { urlBtn.disabled = false; urlBtn.textContent = "IMPORT"; }
    }
  }

  if (urlBtn) urlBtn.addEventListener("click", importUrl);
  if (urlInput) urlInput.addEventListener("keydown", (e) => { if (e.key === "Enter") importUrl(); });

  refresh();
})();
