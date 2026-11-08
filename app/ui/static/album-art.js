// Album art for the console: both decks, NOW / NEXT, the VIBE strip and the library rows.
// Art comes from the server (album_art.py): `art_url` on the track objects of /api/tracks and
// /api/tracks/{id}/analysis, null until the cover is cached; thumbs are `?size=600|160`.
//
// core  pure and node-testable (app/tests/js/album_art_check.js):
//   artUrl      the size variant of an art_url
//   shares      how much of the audible mix each deck owns -> the crossfade opacity
//   ArtSlot     one art square: placeholder until a url arrives, decode off the main thread, no work when unchanged
//   Mixer       writes opacity / transform on the stacked layers, only when the quantised value moved
// glue  browser only: builds the squares, one rAF loop, the late-art poll.
//
// The crossfade is NOT a timer. The share comes from the decks' real crossfaderGain, the value the
// transition schedules on the audio clock (autopilot.js scheduleAudio ~L1380 via rampParam ~L1423; the
// slider is animated alongside it; a hand move reaches the same gain through deck-controller.js
// applyCrossfader ~L1863). So the art is where the mix is, and it lands exactly when the crossfader does.
(function (root) {
  "use strict";

  const EPS = 0.004;

  // "/api/tracks/x/art?size=600&v=1" -> same url at another size (null stays null)
  function artUrl(url, size) {
    if (!url) return null;
    return /[?&]size=\d+/.test(url) ? url.replace(/([?&]size=)\d+/, "$1" + size) : url;
  }

  // Share of the mix per deck, 0..1, a + b = 1. gA/gB are the crossfader gains; hasA/hasB whether a track is loaded.
  // null when nothing is decidable (no deck, or both silent): the caller keeps what is shown.
  function shares(gA, gB, hasA, hasB) {
    const a = hasA ? Math.max(0, +gA || 0) : 0, b = hasB ? Math.max(0, +gB || 0) : 0;
    if (hasA && !hasB) return { a: 1, b: 0 };
    if (hasB && !hasA) return { a: 0, b: 1 };
    const s = a + b;
    if (!(s > 1e-6)) return null;
    let x = a / s;
    if (x > 1 - EPS) x = 1; else if (x < EPS) x = 0;          // the handover lands on the exact end state
    return { a: x, b: 1 - x };
  }

  // One art square. el: {dataset}, img: {src}. decode(url) -> Promise (img.decode off the main thread).
  class ArtSlot {
    constructor(el, img, decode) {
      this.el = el; this.img = img; this.decode = decode; this.url = undefined; this.tok = 0;
      this.loads = 0;
      this.set(null);
    }
    set(url) {
      url = url || null;
      if (url === this.url) return false;                      // unchanged: no work
      this.url = url;
      const tok = ++this.tok;
      if (!url) { this.el.dataset.ph = "1"; this.img.src = ""; return true; }
      this.el.dataset.ph = "1";                                 // placeholder until the image is decoded
      this.loads++;
      Promise.resolve(this.decode(url)).then(() => {
        if (tok !== this.tok) return;                           // a newer url (or null) won the race
        this.img.src = url;
        delete this.el.dataset.ph;
      }, () => { /* keep the placeholder; the next url change retries */ });
      return true;
    }
  }

  // Layers: [{ deck: "a"|"b", el: {style}, mode: "now"|"next" }]. now shows the deck's own share,
  // next shows the other deck's (the one coming in). Writes only opacity and transform.
  class Mixer {
    constructor(layers) { this.layers = layers; this.last = null; }
    apply(sh) {
      if (!sh) return false;
      const key = Math.round(sh.a * 100);
      if (key === this.last) return false;                      // quantised to 1%: no work when unchanged
      this.last = key;
      for (const l of this.layers) {
        const own = l.deck === "a" ? sh.a : sh.b;
        const o = l.mode === "next" ? 1 - own : own;
        l.el.style.opacity = String(Math.round(o * 100) / 100);
        l.el.style.transform = "scale(" + (0.94 + 0.06 * o).toFixed(3) + ")";
      }
      return true;
    }
  }

  const api = { artUrl, shares, ArtSlot, Mixer };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (!root || !root.document) return;
  root.AlbumArt = api;

  // ---------------------------------------------------------------- glue
  const doc = root.document;
  const decode = (url) => {
    const im = new root.Image();
    im.decoding = "async";
    im.src = url;
    return im.decode();                                         // fetched + decoded off the main thread; the <img> then paints from cache
  };

  function square(cls) {
    const el = doc.createElement("span");
    el.className = "aa " + (cls || "");
    const img = doc.createElement("img");
    img.alt = ""; img.decoding = "async"; img.draggable = false;
    const ph = doc.createElement("i");
    ph.className = "aa-ph"; ph.setAttribute("aria-hidden", "true");
    el.append(img, ph);
    return { el, slot: new ArtSlot(el, img, decode) };
  }

  const deckSlots = {};                                         // a / b -> the deck-head square
  const stackSlots = [];                                        // layer squares that follow a deck
  const layers = [];                                            // Mixer layers
  function stack(cls, mode) {
    const wrap = doc.createElement("span");
    wrap.className = "aa-stack " + cls;
    for (const d of ["a", "b"]) {
      const s = square("aa-layer aa-" + d);
      wrap.append(s.el);
      stackSlots.push({ deck: d, slot: s.slot });
      layers.push({ deck: d, el: s.el, mode });
    }
    return wrap;
  }

  function mount() {
    for (const d of ["a", "b"]) {
      const col = doc.querySelector("#deck-" + d + " .deck-title-col");
      if (!col || !col.parentNode) continue;
      const s = square("aa-deck");
      deckSlots[d] = s.slot;
      col.parentNode.insertBefore(s.el, d === "a" ? col : col.nextSibling);   // B's head is mirrored: art sits beside the id
    }
    const now = doc.getElementById("tid-now"), next = doc.getElementById("tid-next");
    const col = (el) => el && el.closest(".hud-col");
    if (col(now)) col(now).before(stack("aa-now", "now"));
    if (col(next)) col(next).before(stack("aa-next", "next"));
    const vb = doc.querySelector("#vibe-bar .vb-body");
    if (vb) {
      const cell = doc.createElement("div");
      cell.className = "vb-cell vb-art";
      cell.append(stack("aa-vibe", "now"));
      vb.prepend(cell);
    }
  }

  const loaded = { a: { id: null, url: null }, b: { id: null, url: null } };
  function deckInfo(d) {
    const dk = root.decks && root.decks[d], st = root.state || {};
    const id = d === "a" ? st.trackA : st.trackB;
    const an = dk && dk.analysis;
    return { id: id || null, url: id && an && an.art_url ? an.art_url : null, has: !!(dk && dk.buffer && id) };
  }
  const urlsById = new Map();                                   // track_id -> art_url, from the last poll

  function syncSlots() {
    for (const d of ["a", "b"]) {
      const i = deckInfo(d);
      if (i.id !== loaded[d].id) loaded[d] = { id: i.id, url: null };
      const url = i.url || (i.id && urlsById.get(i.id)) || null;
      loaded[d].url = url;
      if (deckSlots[d]) deckSlots[d].set(artUrl(url, 160));
    }
    for (const s of stackSlots) s.slot.set(artUrl(loaded[s.deck].url, 160));
  }

  const mixer = new Mixer(layers);
  let frame = 0, lastKey = "";
  function tick() {
    frame = 0;
    if (!doc.hidden) {
      const A = root.decks && root.decks.a, B = root.decks && root.decks.b;
      const st = root.state || {};
      const k2 = st.trackA + "|" + st.trackB + "|" + (A && A.analysis ? A.analysis.art_url : "") + "|" + (B && B.analysis ? B.analysis.art_url : "");
      if (k2 !== lastKey) { lastKey = k2; syncSlots(); }
      if (A && B && A.crossfaderGain && B.crossfaderGain) {
        mixer.apply(shares(A.crossfaderGain.gain.value, B.crossfaderGain.gain.value, deckInfo("a").has, deckInfo("b").has));
      }
    }
    frame = root.requestAnimationFrame(tick);
  }

  // Late art (the server backfills covers in the background): re-read /api/tracks while a loaded deck or a library row
  // still shows the placeholder. Backs off to 5 min; stops asking once nothing is missing.
  let pollMs = 20000, pollTimer = 0;
  function missing() {
    return doc.querySelector(".row-art[data-ph]") !== null ||
      ["a", "b"].some((d) => loaded[d].id && !loaded[d].url);
  }
  async function poll() {
    pollTimer = 0;
    if (!doc.hidden && missing()) {
      try {
        const res = await root.fetch("/api/tracks");
        if (res.ok) {
          const data = await res.json();
          for (const t of data.tracks || []) if (t.art_url) urlsById.set(t.track_id, t.art_url);
          lastKey = "";                                          // force a sync on the next frame
          root.dispatchEvent(new root.CustomEvent("art-urls", { detail: urlsById }));
        }
      } catch (e) { /* offline: try again later */ }
      pollMs = Math.min(300000, Math.round(pollMs * 1.5));
    }
    pollTimer = root.setTimeout(poll, pollMs);
  }

  // library rows: browser.js renders `<span class="aa row-art" data-ph="1" data-art="url|''">`; fill the VISIBLE ones only.
  // browser.js re-renders the whole list often (every analysis that lands), so: covers already decoded are set straight
  // from `shown` (browser cache, no request); new ones load only when their row nears the viewport, at most ROW_FETCHES
  // at a time, so 700 rows never become 700 parallel requests that starve the deck's audio fetch.
  const ROW_FETCHES = 4;
  const shown = new Set();                                      // urls decoded once (served from the HTTP cache after)
  const queue = [];
  let inFlight = 0;
  function show(el, url) {
    if (el.dataset.art !== url) return;                         // the row moved on to another url
    el.querySelector("img").src = url;
    delete el.dataset.ph;
  }
  function pump() {
    while (inFlight < ROW_FETCHES && queue.length) {
      const { el, url } = queue.shift();
      if (!el.isConnected || el.dataset.art !== url) continue;  // re-rendered away before its turn
      if (shown.has(url)) { show(el, url); continue; }
      inFlight++;
      decode(url).then(() => { shown.add(url); show(el, url); }, () => {})
        .finally(() => { inFlight--; pump(); });
    }
  }
  const seen = root.IntersectionObserver ? new root.IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      seen.unobserve(e.target);
      const url = e.target.dataset.art;
      if (url) queue.push({ el: e.target, url });
    }
    pump();
  }, { rootMargin: "300px 0px" }) : null;
  function fillRows(rowsEl) {
    for (const el of rowsEl.querySelectorAll(".row-art[data-art]")) {
      const url = el.dataset.art;
      if (!url || el.dataset.done === url) continue;
      el.dataset.done = url;
      if (shown.has(url)) show(el, url);
      else if (seen) seen.observe(el);
    }
  }
  root.AlbumArt.fillRows = fillRows;
  root.addEventListener("art-urls", () => {
    const rows = doc.getElementById("browser-rows");
    if (!rows) return;
    for (const el of rows.querySelectorAll(".row-art")) {
      const u = urlsById.get(el.dataset.track);
      if (u) el.dataset.art = artUrl(u, 160);
    }
    fillRows(rows);
  });

  function start() {
    mount();
    doc.addEventListener("deck-track-loaded", () => { lastKey = ""; pollMs = 20000; });
    frame = root.requestAnimationFrame(tick);
    pollTimer = root.setTimeout(poll, pollMs);
  }
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", start); else start();
})(typeof window !== "undefined" ? window : globalThis);
