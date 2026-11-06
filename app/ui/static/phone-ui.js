// PHONE LITE: the console on a phone (<= 600 px wide, or a landscape phone). Same engine, same
// buttons: this file only builds a single-column view over the desktop DOM and forwards taps to
// the desktop controls (.click() / an "input" event on the real range), so every rule, gate and
// log line is the desktop one. Desktop above the breakpoint is untouched (phone.css loads with the
// same media query and every rule is scoped to html.phone-lite).
//
//   NOW       both decks (title, time left, BPM, key, on air), crossfader, phrase / energy strip,
//             next pick + booked transition with the countdown to its phrase line, big buttons
//             (START / STOP SET, SKIP NEXT or MIX NOW, HOLD LOOP, PLAY STEP, MERGE -> HOLD), volume,
//             the VIBE strip's orb + feed, the running model
//   LIBRARY   the track browser (search, import, load A / B)
//   SET       the autopilot panel (seed, mode, macros, LEAD TO, queue)
//   PADS      the sampler bank (big pads)
//   MORE      account / devices / sign out, audio, screen lock, install, ADVANCED (the full console)
//
// Phone only (the desktop keeps its behaviour): Screen Wake Lock while a set runs or a deck plays
// (re-requested on visibilitychange), Media Session metadata for the lock screen, a TAP TO START
// overlay that resumes the AudioContext, and the iOS keep-the-screen-on banner.
//
// Performance (PERFORMANCE_AUDIT.md): one 5 Hz timer, paused when the tab is hidden or the NOW tab
// is not shown; DOM written only when a value changes; no layout reads per tick. While the Lite
// view hides them, window.djLite tells the waveform / canvas loops to skip their drawing.
(function (root) {
  "use strict";

  const PHONE_QUERY = "(max-width: 600px), (max-height: 500px) and (pointer: coarse)";
  const TABS = ["now", "library", "set", "pads", "more"];

  // -- pure core (node: app/tests/js/phone_ui_check.js) ----------------------------------------
  function fmtClock(s) {
    if (!Number.isFinite(s)) return "--:--";
    const a = Math.max(0, s), m = Math.floor(a / 60), r = Math.floor(a % 60);
    return `${m}:${String(r).padStart(2, "0")}`;
  }
  // seconds of real time left on a deck (track time / playback rate)
  function timeLeft(dur, pos, rate) {
    if (!Number.isFinite(dur) || !Number.isFinite(pos)) return null;
    return Math.max(0, dur - pos) / (rate > 0 ? rate : 1);
  }
  // crossfader -1..1 -> label: "A", "B", "MID", or the side and its share
  function xfLabel(v) {
    if (!Number.isFinite(v)) return "--";
    if (v <= -0.98) return "A";
    if (v >= 0.98) return "B";
    if (Math.abs(v) <= 0.02) return "MID";
    const pct = Math.round(Math.abs(v) * 100);
    return v < 0 ? `A ${pct}` : `B ${pct}`;
  }
  // first 8-bar line strictly after pos (track time), null when none / no data
  function nextLine(phrases, pos) {
    if (!Array.isArray(phrases) || !Number.isFinite(pos)) return null;
    for (const p of phrases) if (p > pos + 0.05) return p;
    return null;
  }
  // a touch drag on a slider: "drag" once it moves sideways past `slop`, "scroll" once it moves
  // more up / down (the page scrolls, the value never changes), null while undecided
  function swipeIntent(dx, dy, slop) {
    const ax = Math.abs(dx), ay = Math.abs(dy), s = slop > 0 ? slop : 8;
    if (ay > s && ay >= ax) return "scroll";
    if (ax > s && ax > ay) return "drag";
    return null;
  }
  // relative drag: start value + the moved fraction of the track, clamped and snapped to step
  function dragValue(v0, dx, width, min, max, step) {
    if (!(width > 0)) return v0;
    let v = v0 + (dx / width) * (max - min);
    v = Math.max(min, Math.min(max, v));
    if (step > 0) v = Math.round((v - min) / step) * step + min;
    return Math.round(v * 1e6) / 1e6;
  }
  function isIOS(ua, platform, touchPoints) {
    return /iP(hone|ad|od)/.test(ua || "") || (platform === "MacIntel" && touchPoints > 1);
  }
  // "Artist - Title" (the downloader's file names) -> {artist, title}
  function splitTitle(name) {
    const s = String(name || "").replace(/\.(mp3|m4a|wav|flac|ogg|webm)$/i, "").trim();
    const m = s.match(/^(.+?)\s+[-–—]\s+(.+)$/);
    return m ? { artist: m[1].trim(), title: m[2].trim() } : { artist: "", title: s };
  }
  // what the wake lock should do now: want it while a set runs or a deck plays, only when visible
  function wakeWanted(setActive, anyPlaying) { return !!(setActive || anyPlaying); }

  // -- phone-sized audio (server: app/ui/services/phone_audio.py, ?q=phone) --------------------
  // the routes the console decodes as deck / stem audio
  const AUDIO_ROUTE = /^\/api\/(audio\/tracks\/[0-9a-f]{16}|tracks\/[0-9a-f]{16}\/stems\/\w+|riff\/[\w-]+\/\w+)$/;
  // url -> the phone variant's url, or null when it is not deck audio / already has a query
  function smallAudioUrl(url) {
    const m = String(url || "").match(/^(https?:\/\/[^/]+)?(\/[^?#]*)(\?[^#]*)?$/);
    if (!m || m[3] || !AUDIO_ROUTE.test(m[2])) return null;
    return `${m[1] || ""}${m[2]}?q=phone`;
  }
  // a small screen, or the user's own Data Saver. Not effectiveType: Chrome's estimate said "3g"
  // for a desktop on localhost, which would have sent a desktop the AAC copy.
  function wantSmallAudio(phoneView, conn) {
    return !!(phoneView || (conn && conn.saveData));
  }
  // total bytes from a 200 (Content-Length) or 206 (Content-Range "bytes a-b/total"); null = unknown
  function totalBytes(status, contentLength, contentRange, already) {
    if (status === 206) {
      const m = String(contentRange || "").match(/\/(\d+)$/);
      return m ? Number(m[1]) : null;
    }
    const n = Number(contentLength);
    return contentLength != null && contentLength !== "" && Number.isFinite(n) ? n + (already || 0) : null;
  }
  // "12.3 / 34.0 MB (36 %)" or "12.3 MB" when the size is unknown
  function fmtProgress(loaded, total) {
    const mb = (b) => (b / 1e6).toFixed(1);
    return total > 0 ? `${mb(loaded)} / ${mb(total)} MB (${Math.min(100, Math.floor((100 * loaded) / total))} %)` : `${mb(loaded)} MB`;
  }

  const core = { PHONE_QUERY, TABS, fmtClock, timeLeft, xfLabel, nextLine, swipeIntent, dragValue, isIOS, splitTitle, wakeWanted,
    smallAudioUrl, wantSmallAudio, totalBytes, fmtProgress };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof document === "undefined") return;

  // -- browser -----------------------------------------------------------------------------------
  const html = document.documentElement;
  const $ = (id) => document.getElementById(id);
  const mq = root.matchMedia ? root.matchMedia(PHONE_QUERY) : null;
  const ctx = () => (typeof audioCtx !== "undefined" ? audioCtx : null);   // deck-controller.js
  const decks = () => root.decks || {};
  const ap = () => root.autopilotState || null;
  const posOf = (d) => (d && d._currentPosition ? d._currentPosition() : NaN);
  const rateOf = (d) => (d && d._playbackRate && d._playbackRate()) || 1;
  const gainOf = (d) => (d && d.crossfaderGain ? d.crossfaderGain.gain.value : 0);
  const store = {
    get: (k) => { try { return root.localStorage.getItem(k); } catch (e) { return null; } },
    set: (k, v) => { try { root.localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };
  const ios = isIOS(navigator.userAgent, navigator.platform, navigator.maxTouchPoints);

  // write-through cache: the DOM is only touched when a value really changes (as vibe-ui.js)
  const last = new Map();
  function put(node, prop, v) {
    if (!node) return;
    const k = last.get(node) || {};
    if (k[prop] === v) return;
    k[prop] = v; last.set(node, k);
    if (prop === "text") node.textContent = v;
    else if (prop === "hidden") node.hidden = v;
    else if (prop === "disabled") node.disabled = v;
    else if (prop.startsWith("--")) node.style.setProperty(prop, v);
    else if (prop.startsWith("data-") || prop.startsWith("aria-")) node.setAttribute(prop, v);
    else if (prop.startsWith("class:")) node.classList.toggle(prop.slice(6), !!v);
  }
  function el(tag, attrs, text) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    if (text != null) n.textContent = text;
    return n;
  }
  const clickEl = (node) => { if (node && !node.disabled) node.click(); return !!node; };

  function onAirDeck() {
    const a = ap(), ds = decks();
    if (a && a.active && ds[a.activeDeck] && ds[a.activeDeck].playing) return ds[a.activeDeck];
    let best = null;
    for (const d of Object.values(ds)) if (d && d.playing && (!best || gainOf(d) > gainOf(best))) best = d;
    return best;
  }
  const anyPlaying = () => Object.values(decks()).some((d) => d && d.playing);

  // ---- swipe-safe slider: drags only sideways, a vertical swipe scrolls the page, a tap never jumps
  function slider({ label, min, max, step, read, write, fmt }) {
    const wrap = el("div", { class: "ls", role: "slider", tabindex: "0", "aria-label": label,
      "aria-valuemin": String(min), "aria-valuemax": String(max) });
    const track = el("div", { class: "ls-track" });
    const fill = el("i", { class: "ls-fill" });
    const run = el("div", { class: "ls-run" });
    const thumb = el("b", { class: "ls-thumb" });
    run.append(thumb);
    track.append(fill, run);
    wrap.append(track);
    let g = null;          // gesture: {id, x0, y0, v0, w, mode}
    const frac = (v) => (v - min) / (max - min);
    function show(v) {
      if (!Number.isFinite(v)) return;
      put(wrap, "--p", frac(v).toFixed(4));
      put(wrap, "aria-valuenow", String(v));
      put(wrap, "aria-valuetext", fmt ? fmt(v) : String(v));
    }
    function set(v) { write(v); show(v); }
    wrap.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      // one layout read per gesture (the track width), never per move
      g = { id: e.pointerId, x0: e.clientX, y0: e.clientY, v0: read(), w: track.getBoundingClientRect().width, mode: null };
      if (e.pointerType === "mouse") { g.mode = "drag"; wrap.setPointerCapture(e.pointerId); }
    });
    wrap.addEventListener("pointermove", (e) => {
      if (!g || e.pointerId !== g.id) return;
      const dx = e.clientX - g.x0, dy = e.clientY - g.y0;
      if (!g.mode) {
        g.mode = swipeIntent(dx, dy, 8);
        if (g.mode === "scroll") { g = null; return; }
        if (g.mode === "drag") { try { wrap.setPointerCapture(e.pointerId); } catch (err) { /* gone */ } wrap.classList.add("ls-on"); }
        else return;
      }
      set(dragValue(g.v0, dx, g.w, min, max, step));
    });
    const end = (e) => { if (g && e.pointerId === g.id) { g = null; wrap.classList.remove("ls-on"); } };
    wrap.addEventListener("pointerup", end);
    wrap.addEventListener("pointercancel", end);
    wrap.addEventListener("keydown", (e) => {
      const big = (max - min) / 10;
      const d = { ArrowLeft: -step, ArrowDown: -step, ArrowRight: step, ArrowUp: step, PageDown: -big, PageUp: big }[e.key];
      if (d == null) return;
      e.preventDefault();
      set(Math.max(min, Math.min(max, read() + d)));
    });
    return { node: wrap, sync: () => { if (!g) show(read()); } };
  }
  // drive a desktop range input the way a hand on it would (its own "input" listeners apply it)
  function rangeIO(id) {
    return {
      read: () => { const n = $(id); return n ? parseFloat(n.value) : NaN; },
      write: (v) => { const n = $(id); if (!n) return; n.value = String(v); n.dispatchEvent(new Event("input", { bubbles: true })); },
    };
  }

  // ---- AudioContext unlock (phones start it suspended; iOS suspends it again on lock / call)
  let unlocked = false;
  function unlockAudio() {
    const c = ctx();
    try { if (navigator.audioSession) navigator.audioSession.type = "playback"; } catch (e) { /* older Safari */ }
    if (!c) return Promise.resolve();
    if (!unlocked) {
      unlocked = true;
      try {   // iOS: a one-sample buffer played inside the gesture opens the output
        const b = c.createBuffer(1, 1, c.sampleRate), s = c.createBufferSource();
        s.buffer = b; s.connect(c.destination); s.start(0);
      } catch (e) { /* not needed elsewhere */ }
    }
    return c.state === "running" ? Promise.resolve() : c.resume().catch(() => {});
  }

  // ---- Screen Wake Lock (phone view only)
  const wake = { lock: null, want: false, busy: false, supported: "wakeLock" in navigator, error: "" };
  async function wakeAcquire() {
    if (!wake.supported || wake.lock || wake.busy || document.hidden) return;
    wake.busy = true;
    try {
      wake.lock = await navigator.wakeLock.request("screen");
      wake.error = "";
      wake.lock.addEventListener("release", () => { wake.lock = null; });
      if (!wake.want) wakeRelease();
    } catch (e) { wake.error = e && e.message ? e.message : "refused"; }
    finally { wake.busy = false; }
  }
  function wakeRelease() {
    const l = wake.lock;
    wake.lock = null;
    if (l) l.release().catch(() => {});
  }
  function wakeUpdate() {
    const a = ap();
    wake.want = wakeWanted(a && a.active, anyPlaying());
    if (wake.want) wakeAcquire(); else wakeRelease();
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && wake.want) wakeAcquire();      // the browser drops the lock when hidden
  });

  // ---- Media Session: what plays, on the lock screen / notification
  let mediaKey = "";
  function mediaUpdate() {
    if (!("mediaSession" in navigator) || typeof root.MediaMetadata !== "function") return;
    const d = onAirDeck();
    const name = d ? (($(`title-${d.id}`) || {}).textContent || "").trim() : "";
    const playing = !!d;
    const key = `${name}|${playing}`;
    if (key === mediaKey) return;
    mediaKey = key;
    try {
      if (name) {
        const t = splitTitle(name);
        navigator.mediaSession.metadata = new root.MediaMetadata({
          title: t.title || name, artist: t.artist || "NULL::SET", album: "NULL::SET AI DJ",
          artwork: [{ src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
                    { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" }],
        });
      }
      navigator.mediaSession.playbackState = playing ? "playing" : "paused";
    } catch (e) { /* partial support */ }
  }

  setInterval(() => {
    if (!phone) { wake.want = false; wakeRelease(); return; }   // desktop: untouched
    wakeUpdate(); mediaUpdate();
  }, 1000);

  // ---- deck / stem audio on a phone: the small variant, resumable, with progress ------------------
  // One wrapper over window.fetch (outermost, so every caller goes through it: browser.js, the
  // autopilot preload, macros, stems). Only GETs of AUDIO_ROUTE without a query, and only when
  // wantSmallAudio: the desktop request path is untouched. The body is read here with a byte count
  // (downloads, painted by tick) and, if the link drops part-way, resumed with a Range request
  // (FileResponse answers 206) instead of starting again; the caller's abort signal still wins.
  const downloads = new Set();          // {label, loaded, total}, one per transfer in flight
  const innerFetch = root.fetch.bind(root);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  function audioLabel(path) {
    const id = (path.match(/[0-9a-f]{16}/) || [""])[0];
    const stem = (path.match(/\/stems\/(\w+)$|\/riff\/[\w-]+\/(\w+)$/) || []).slice(1).find(Boolean);
    const name = (root.trackBrowser && root.trackBrowser.nameOf && root.trackBrowser.nameOf(id)) || id || "audio";
    return stem ? `${name} · ${stem}` : name;
  }
  async function resumableGet(url, init, label) {
    const signal = init && init.signal;
    const chunks = [];
    let got = 0, total = null, type = "", tries = 0;
    const entry = { label, loaded: 0, total: null };
    downloads.add(entry);
    try {
      for (;;) {
        const headers = new Headers((init && init.headers) || undefined);
        if (got) headers.set("Range", `bytes=${got}-`);
        let res;
        try {
          res = await innerFetch(url, Object.assign({}, init, { headers }));
        } catch (e) {
          if ((signal && signal.aborted) || ++tries > 4) throw e;
          await sleep(1000 * tries);
          continue;
        }
        if (!res.ok) {
          if (!got) return res;                            // 404 etc.: the caller handles it as before
          throw new Error(`resume failed: HTTP ${res.status}`);
        }
        if (got && res.status !== 206) { chunks.length = 0; got = 0; }   // Range ignored: whole file again
        if (total == null) total = totalBytes(res.status, res.headers.get("content-length"), res.headers.get("content-range"), got);
        type = type || res.headers.get("content-type") || "";
        entry.total = total;
        const reader = res.body.getReader();
        try {
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            chunks.push(value);
            got += value.length;
            entry.loaded = got;
          }
        } catch (e) {
          if ((signal && signal.aborted) || ++tries > 4) throw e;
          await sleep(1000 * tries);
          continue;                                        // resume from `got`
        }
        if (total == null || got >= total) break;
        if (++tries > 4) throw new Error(`download incomplete: ${got} of ${total} bytes`);
      }
      return new Response(new Blob(chunks, { type }), { status: 200, headers: { "content-type": type, "content-length": String(got) } });
    } finally {
      downloads.delete(entry);
    }
  }
  root.fetch = function (input, init) {
    const method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : null;
    const small = method === "GET" && url && wantSmallAudio(html.classList.contains("phone-lite"), navigator.connection)
      ? smallAudioUrl(url) : null;
    if (!small) return innerFetch(input, init);
    return resumableGet(small, init, audioLabel(small.replace(/^https?:\/\/[^/]+/, "").split("?")[0]));
  };

  // ---- the Lite view ---------------------------------------------------------------------------
  let built = false, phone = false;
  const ui = {};
  let tab = TABS.includes(store.get("phoneTab")) ? store.get("phoneTab") : "now";
  let adv = store.get("phoneAdvanced") === "1";
  const energy = { a: null, b: null };
  root.addEventListener("ai-energy", (e) => {
    const d = (e && e.detail) || {};
    if (Number.isFinite(d.a)) energy.a = d.a;
    if (Number.isFinite(d.b)) energy.b = d.b;
  });

  function deckCard(id) {
    const card = el("div", { class: `lite-deck deck-${id}`, "data-deck": id });
    const top = el("div", { class: "lite-deck-top" });
    const tag = el("span", { class: "lite-id" }, id.toUpperCase());
    const air = el("span", { class: "lite-onair" }, "ON AIR");
    const play = el("button", { type: "button", class: "lite-play", "aria-label": `Play / pause deck ${id.toUpperCase()}` }, "▶");
    play.addEventListener("click", () => { unlockAudio(); clickEl(document.querySelector(`.play-btn[data-deck="${id}"]`)); });
    top.append(tag, air, play);
    const title = el("div", { class: "lite-title" }, "—");
    const meta = el("div", { class: "lite-meta" });
    const left = el("span", { class: "lite-left" }, "--:--");
    const bpm = el("span", { class: "lite-bpm" }, "--.- BPM");
    const key = el("span", { class: "lite-key" }, "--");
    meta.append(left, bpm, key);
    const prog = el("div", { class: "lite-prog", "aria-hidden": "true" }, null);
    prog.append(el("i"));
    card.append(top, title, meta, prog);
    ui[`deck_${id}`] = { card, air, play, title, left, bpm, key, prog };
    return card;
  }

  function bigBtn(cls, text, onTap) {
    const b = el("button", { type: "button", class: `lite-btn ${cls}` }, text);
    b.addEventListener("click", () => { unlockAudio(); onTap(b); });
    return b;
  }

  function say(msg) { ui.toast = { msg, at: Date.now() }; }

  function build() {
    built = true;
    const lite = el("section", { id: "lite", class: "lite", "aria-label": "Now playing" });

    // iOS: Web Audio stops when the screen locks or Safari goes to the background
    const banner = el("div", { class: "lite-banner", id: "lite-ios", role: "note" });
    banner.append(el("span", {}, "iPhone / iPad: keep this screen on and Safari in front during a set. "
      + "iOS stops Web Audio when the screen locks or you switch apps."));
    const ok = el("button", { type: "button", class: "lite-btn lite-small" }, "OK");
    ok.addEventListener("click", () => { banner.hidden = true; store.set("phoneIosBanner", "1"); });
    banner.append(ok);
    banner.hidden = !ios || store.get("phoneIosBanner") === "1";

    const head = el("div", { class: "lite-head" });
    ui.model = el("span", { class: "lite-model" }, "model: checking…");
    ui.wake = el("span", { class: "lite-wake" }, "");
    head.append(el("span", { class: "lite-brand" }, "NULL::SET"), ui.model, ui.wake);

    const ds = el("div", { class: "lite-decks" });
    ds.append(deckCard("a"), deckCard("b"));

    // crossfader + volume: swipe-safe, drive the desktop ranges
    const xfIO = rangeIO("crossfader");
    ui.xf = slider({ label: "Crossfader", min: -1, max: 1, step: 0.01, read: xfIO.read, write: xfIO.write, fmt: xfLabel });
    const xfRow = el("div", { class: "lite-row lite-xf" });
    ui.xfv = el("span", { class: "lite-xfv" }, "--");
    xfRow.append(el("span", { class: "lite-k hud-a" }, "A"), ui.xf.node, el("span", { class: "lite-k hud-b" }, "B"), ui.xfv);

    // phrase / energy strip on the on-air deck
    const strip = el("div", { class: "lite-strip", "aria-label": "Phrase and energy" });
    const bars = el("div", { class: "lite-bars", "aria-hidden": "true" });
    ui.bars = [];
    for (let i = 0; i < 8; i++) { const b = el("i"); ui.bars.push(b); bars.append(b); }
    ui.barTxt = el("span", { class: "lite-bartxt" }, "BAR -/8");
    ui.lineTxt = el("span", { class: "lite-linetxt" }, "");
    const nrg = el("div", { class: "lite-nrg" });
    ui.nrgA = []; ui.nrgB = [];
    const chips = (arr, cls) => { const w = el("span", { class: `lite-chips ${cls}`, "aria-hidden": "true" }); for (let i = 0; i < 10; i++) { const c = el("i"); arr.push(c); w.append(c); } return w; };
    ui.nrgTxt = el("span", { class: "lite-nrgtxt" }, "NRG -");
    nrg.append(el("span", { class: "lite-k" }, "NRG"), chips(ui.nrgA, "lite-cur"), chips(ui.nrgB, "lite-nxt"), ui.nrgTxt);
    const stripTop = el("div", { class: "lite-strip-top" });
    stripTop.append(ui.barTxt, ui.lineTxt);
    strip.append(stripTop, bars, nrg);

    // next pick + booked transition
    const next = el("div", { class: "lite-next" });
    ui.nextName = el("div", { class: "lite-next-name" }, "—");
    ui.nextRecipe = el("div", { class: "lite-next-recipe" }, "nothing booked");
    ui.nextWhen = el("div", { class: "lite-next-when" }, "");
    ui.nextCd = el("span", { class: "lite-next-cd" }, "--");
    const nHead = el("div", { class: "lite-next-head" });
    nHead.append(el("span", { class: "lite-k" }, "NEXT"), ui.nextCd);
    next.append(nHead, ui.nextName, ui.nextRecipe, ui.nextWhen);

    // big buttons: each forwards to the desktop control (same gates, same logs)
    const ctl = el("div", { class: "lite-ctl" });
    ui.start = bigBtn("lite-start", "▶ START SET", () => {
      wakeAcquire();        // inside the gesture: the most reliable moment to get the lock
      clickEl($("ap-start-btn"));
    });
    ui.stop = bigBtn("lite-stop", "■ STOP SET", () => clickEl($("ap-stop-btn")));
    ui.skip = bigBtn("lite-skip", "SKIP NEXT", () => {
      const a = root.autopilot && root.autopilot.superMove;
      if (!a || typeof a.rebook !== "function") return say("SKIP: autopilot not loaded");
      const r = a.rebook("owner skipped the next pick (phone SKIP)");
      say(r.ok ? "SKIP: re-picking the next song" : `SKIP refused: ${r.why}`);
    });
    ui.mix = bigBtn("lite-mix", "MIX NOW", () => clickEl(document.querySelector('[data-ai-action="mix"]')));
    ui.hold = bigBtn("lite-hold", "HOLD LOOP", () => {
      const d = onAirDeck();
      if (!d) return say("HOLD: nothing is playing");
      clickEl(document.querySelector(`.deck-btn[data-deck="${d.id}"][data-action="loop-toggle"]`));
    });
    ui.step = bigBtn("lite-stepb", "PLAY STEP", () => clickEl($("macro-next")));
    ui.merge = bigBtn("lite-merge", "MERGE → HOLD", () => clickEl(document.querySelector('[data-ai-action="merge_hold"]')));
    ctl.append(ui.start, ui.stop, ui.skip, ui.mix, ui.hold, ui.step, ui.merge);

    const volIO = rangeIO("master-fader");
    ui.vol = slider({ label: "Master volume", min: 0, max: 1.2, step: 0.01, read: volIO.read, write: volIO.write,
      fmt: (v) => `${Math.round(v * 100)} %` });
    const volRow = el("div", { class: "lite-row lite-vol" });
    ui.volv = el("span", { class: "lite-xfv" }, "--");
    volRow.append(el("span", { class: "lite-k" }, "VOL"), ui.vol.node, ui.volv);

    ui.status = el("div", { class: "lite-status", role: "status", "aria-live": "polite" }, "");
    lite.append(banner, head, ds, xfRow, strip, next, ctl, volRow, ui.status);

    // the VIBE strip (orb + feed) sits right under the Lite card
    const vibe = $("vibe-bar");
    if (vibe && vibe.parentNode) vibe.parentNode.insertBefore(lite, vibe);
    else document.body.prepend(lite);

    buildMore();
    buildTabs();
    buildTapOverlay();
  }

  function buildMore() {
    const more = el("section", { id: "lite-more", class: "lite lite-more", "aria-label": "Settings" });
    const sec = (title) => { const s = el("div", { class: "lite-sec" }); s.append(el("h2", { class: "lite-k" }, title)); more.append(s); return s; };

    const acct = sec("ACCOUNT");
    const dev = el("button", { type: "button", class: "lite-btn" }, "DEVICES");
    dev.addEventListener("click", () => clickEl($("devices-btn")));
    const out = el("a", { class: "lite-btn", href: "/logout" }, "SIGN OUT");
    acct.append(dev, out);

    const audio = sec("AUDIO");
    ui.audioTxt = el("span", { class: "lite-note" }, "");
    const resume = el("button", { type: "button", class: "lite-btn" }, "RESUME AUDIO");
    resume.addEventListener("click", () => { unlockAudio(); });
    audio.append(ui.audioTxt, resume);

    const screen = sec("SCREEN");
    ui.wakeTxt = el("span", { class: "lite-note" }, "");
    const vfx = el("button", { type: "button", class: "lite-btn", "aria-pressed": "false" }, "VFX");
    vfx.addEventListener("click", () => clickEl($("vfx-toggle")));
    ui.vfx = vfx;
    screen.append(ui.wakeTxt, vfx);

    const app = sec("APP");
    ui.install = el("button", { type: "button", class: "lite-btn", hidden: "" }, "INSTALL APP");
    ui.install.addEventListener("click", async () => {
      const p = ui.installPrompt;
      if (!p) return;
      ui.installPrompt = null; ui.install.hidden = true;
      try { await p.prompt(); } catch (e) { /* dismissed */ }
    });
    ui.installTxt = el("span", { class: "lite-note" },
      ios ? "Add to the home screen: Share, then Add to Home Screen." : "Add to the home screen from the browser menu.");
    app.append(ui.install, ui.installTxt);

    const advSec = sec("ADVANCED");
    ui.adv = el("button", { type: "button", class: "lite-btn", "aria-pressed": String(adv) }, "FULL CONSOLE");
    ui.adv.addEventListener("click", () => { adv = !adv; store.set("phoneAdvanced", adv ? "1" : "0"); apply(); });
    advSec.append(el("span", { class: "lite-note" },
      "The desktop panels (waveforms, EQ, FX, remix, beat grid, AI actions) stacked on this screen. Best in landscape."), ui.adv);

    document.body.append(more);
    root.addEventListener("beforeinstallprompt", (e) => {
      e.preventDefault();
      ui.installPrompt = e;
      ui.install.hidden = false;
    });
  }

  function buildTabs() {
    const bar = el("nav", { id: "lite-tabs", class: "lite-tabs", "aria-label": "Sections" });
    const names = { now: "NOW", library: "LIBRARY", set: "SET", pads: "PADS", more: "MORE" };
    ui.tabs = {};
    for (const t of TABS) {
      const b = el("button", { type: "button", class: "lite-tab", "data-tab": t, "aria-pressed": "false" }, names[t]);
      b.addEventListener("click", () => {
        tab = t; store.set("phoneTab", t); apply();
        root.scrollTo(0, 0);
        // the library warms only the analyses of rows on screen (browser.js)
        if (t === "library" && root.trackBrowser) setTimeout(root.trackBrowser.warmVisible, 0);
      });
      ui.tabs[t] = b;
      bar.append(b);
    }
    // download progress, above the tab bar on every tab (filled by paintDownloads)
    ui.dl = el("div", { id: "lite-dl", class: "lite-dl", role: "status", "aria-live": "polite", hidden: "" });
    ui.dlTxt = el("span", { class: "lite-dl-txt" });
    ui.dlBar = el("i", { class: "lite-dl-bar" });
    ui.dl.append(ui.dlTxt, ui.dlBar);
    document.body.append(bar, ui.dl);
  }

  function paintDownloads() {
    if (!built) return;
    let first = null, more = 0;
    for (const d of downloads) { if (!first) first = d; else more++; }
    put(ui.dl, "hidden", !first);
    if (!first) return;
    // no bytes and no size yet: the server is still making the phone copy (first request per song)
    const what = !first.loaded && first.total == null ? "preparing phone audio…" : fmtProgress(first.loaded, first.total);
    put(ui.dlTxt, "text", `⬇${more ? ` ${more + 1} files ·` : ""} ${first.label}: ${what}`);
    put(ui.dl, "--p", first.total > 0 ? Math.min(1, first.loaded / first.total).toFixed(3) : "0");
  }

  function buildTapOverlay() {
    const o = el("div", { id: "lite-tap", class: "lite-tap", role: "dialog", "aria-label": "Start audio", hidden: "" });
    const b = el("button", { type: "button", class: "lite-tap-btn" }, "TAP TO START AUDIO");
    ui.tapNote = el("p", { class: "lite-note" }, "The browser keeps sound off until you tap.");
    o.append(b, ui.tapNote);
    o.addEventListener("click", () => { unlockAudio().then(paintAudio); });
    document.body.append(o);
    ui.tap = o;
    const c = ctx();
    if (c) c.addEventListener("statechange", paintAudio);
  }

  function paintAudio() {
    if (!built) return;
    const c = ctx();
    const st = c ? c.state : "none";
    // a phone shows the overlay until the context runs; "interrupted" = iOS call / lock
    put(ui.tap, "hidden", !phone || st === "running" || st === "none" || st === "closed");
    put(ui.tapNote, "text", st === "interrupted" || unlocked
      ? "Audio was paused by the phone (screen lock, call or another app). Tap to resume."
      : "The browser keeps sound off until you tap.");
    put(ui.audioTxt, "text", `Audio engine: ${st}${c ? ` · ${(c.sampleRate / 1000).toFixed(1)} kHz` : ""}`);
  }

  // phone on/off, tab, advanced -> classes the CSS reads; the canvas loops read window.djLite
  function apply() {
    phone = !!(mq && mq.matches);
    if (phone && !built) build();
    html.classList.toggle("phone-lite", phone);
    html.classList.toggle("phone-adv", phone && adv);
    if (phone) html.setAttribute("data-ptab", tab); else html.removeAttribute("data-ptab");
    const lite = phone && !adv;
    if (root.djLite !== lite) {
      root.djLite = lite;
      // the waveforms and canvases come back at their real size (they measured 0 while hidden)
      if (!lite) setTimeout(() => root.dispatchEvent(new Event("resize")), 0);
    }
    if (!built) return;
    for (const t of TABS) put(ui.tabs[t], "aria-pressed", String(t === tab));
    put(ui.adv, "aria-pressed", String(adv));
    put(ui.adv, "text", adv ? "FULL CONSOLE: ON" : "FULL CONSOLE: OFF");
    if (phone && adv && tab !== "now" && tab !== "more") {
      // advanced: everything is on the page; a tab jumps to its panel
      const target = { library: ".panel.browser", set: "#autopilot-panel", pads: ".panel.sampler-bank" }[tab];
      const n = target && document.querySelector(target);
      if (n) n.scrollIntoView({ block: "start" });
    }
    if (phone) {
      // the AI orb + feed are part of NOW: a strip hidden on the desktop shows again here
      const vb = $("vibe-bar");
      if (vb && vb.classList.contains("vb-hidden") && !ui.vibeShown) {
        ui.vibeShown = true;
        const t = vb.querySelector(".vb-toggle");
        if (t) t.click();
      }
    }
    paintAudio();
    tick(true);
  }

  // ---- 5 Hz paint of the NOW tab --------------------------------------------------------------
  let lastStatus = { ap: "", st: "", shown: "" };
  function tick(force) {
    if (!built || !phone || document.hidden) return;
    paintDownloads();
    if (!force && tab !== "now" && tab !== "more") return;
    const ds = decks(), a = ap(), air = onAirDeck();
    for (const id of ["a", "b"]) {
      const d = ds[id], u = ui[`deck_${id}`];
      if (!u) continue;
      const loaded = !!(d && d.buffer);
      const t = ($(`title-${id}`) || {}).textContent;
      put(u.title, "text", loaded ? (t || "").trim() || "—" : "empty");
      const pos = posOf(d), rate = rateOf(d);
      const left = loaded ? timeLeft(d.buffer.duration, pos, rate) : null;
      put(u.left, "text", left == null ? "--:--" : `-${fmtClock(left)}`);
      const bpm = ($(`bpm-${id}`) || {}).textContent;
      put(u.bpm, "text", loaded && bpm ? `${bpm.trim()} BPM` : "--.- BPM");
      const key = d && d.analysis && d.analysis.key && d.analysis.key.camelot;
      put(u.key, "text", key || "--");
      put(u.card, "class:is-air", !!air && air === d);
      put(u.air, "text", !!air && air === d ? "ON AIR" : d && d.playing ? "PLAYING" : loaded ? "CUED" : "");
      put(u.card, "class:is-playing", !!(d && d.playing));
      put(u.play, "text", d && d.playing ? "❚❚" : "▶");
      put(u.play, "disabled", !loaded);
      put(u.prog, "--p", loaded ? Math.max(0, Math.min(1, pos / d.buffer.duration)).toFixed(3) : "0");
    }
    ui.xf.sync(); ui.vol.sync();
    const xfv = parseFloat(($("crossfader") || {}).value);
    put(ui.xfv, "text", xfLabel(xfv));
    const vol = parseFloat(($("master-fader") || {}).value);
    put(ui.volv, "text", Number.isFinite(vol) ? `${Math.round(vol * 100)}` : "--");

    // phrase strip on the on-air deck
    const vcore = root.vibeUi && root.vibeUi.core;
    const an = air && air.analysis;
    const ph = an && vcore ? vcore.phraseAt(an.downbeat_times, an.phrase_boundaries_8bar, posOf(air)) : null;
    ui.bars.forEach((b, i) => { put(b, "class:on", !!ph && i < ph.bar); put(b, "class:now", !!ph && i === ph.bar - 1); });
    put(ui.barTxt, "text", ph ? `BAR ${ph.bar}/8 · PHRASE ${ph.phrase} · ${String(air.id).toUpperCase()}` : "BAR -/8");
    const line = an ? nextLine(an.phrase_boundaries_8bar, posOf(air)) : null;
    const lineIn = line == null ? null : (line - posOf(air)) / rateOf(air);
    put(ui.lineTxt, "text", lineIn == null ? "" : `next line in ${vcore ? vcore.fmtSecs(lineIn) : fmtClock(lineIn)}`);
    const eCur = Number.isFinite(energy.a) ? energy.a : a && Number.isFinite(a.energy) ? a.energy : null;
    const nc = Number.isFinite(eCur) ? Math.round(Math.max(0, Math.min(10, eCur))) : 0;
    const nn = Number.isFinite(energy.b) ? Math.round(Math.max(0, Math.min(10, energy.b))) : 0;
    ui.nrgA.forEach((c, i) => put(c, "class:on", i < nc));
    ui.nrgB.forEach((c, i) => put(c, "class:on", i < nn));
    put(ui.nrgTxt, "text", `${eCur == null ? "-" : Math.round(eCur * 10) / 10} → ${Number.isFinite(energy.b) ? Math.round(energy.b * 10) / 10 : "-"}`);

    // next pick + booked transition
    const active = !!(a && a.active);
    const nx = a && a.next;
    const mixing = !!(root.djMind && root.djMind.transitioning);
    if (active && (nx || mixing)) {
      const out = ds[a.activeDeck];
      const cd = Number.isFinite(a.fireAt) && out ? (a.fireAt - posOf(out)) / rateOf(out) : null;
      put(ui.nextName, "text", (nx && nx.name) || "next song");
      put(ui.nextRecipe, "text", (nx && nx.recipe) || "transition");
      put(ui.nextCd, "text", mixing ? "MIXING" : cd == null ? "--" : cd > 0 ? `in ${vcore ? vcore.fmtSecs(cd) : fmtClock(cd)}` : "NOW");
      put(ui.nextWhen, "text", Number.isFinite(a.fireAt) ? `fires on the phrase line at ${fmtClock(a.fireAt)} of deck ${String(a.activeDeck).toUpperCase()}` : "");
    } else {
      const upNext = (($("tid-next") || {}).textContent || "").trim();
      put(ui.nextName, "text", active ? (a.preplanning ? `planning: ${a.preplanning}` : "choosing the next song") : upNext && upNext !== "—" ? upNext : "—");
      put(ui.nextRecipe, "text", active ? (a.preparing ? `waiting for ${a.preparing}` : "nothing booked yet") : "set not running");
      put(ui.nextCd, "text", "--");
      put(ui.nextWhen, "text", "");
    }

    // buttons: shown only when they can act; disabled mirrors the desktop control
    const startBtn = $("ap-start-btn");
    put(ui.start, "hidden", active || !startBtn);
    put(ui.start, "disabled", !!(startBtn && startBtn.disabled));
    put(ui.stop, "hidden", !active);
    put(ui.skip, "hidden", !active);
    put(ui.skip, "disabled", !nx || mixing);
    const other = air && ds[air.id === "a" ? "b" : "a"];
    const pairReady = !!(air && other && other.buffer && other.analysis && !other.playing);
    put(ui.mix, "hidden", active || !document.querySelector('[data-ai-action="mix"]'));
    put(ui.mix, "disabled", !pairReady);
    const loopBtn = air && document.querySelector(`.deck-btn[data-deck="${air.id}"][data-action="loop-toggle"]`);
    const looping = !!(loopBtn && loopBtn.classList.contains("loop-active"));
    put(ui.hold, "disabled", !air);
    put(ui.hold, "class:is-on", looping);
    put(ui.hold, "text", looping ? "HOLD LOOP: ON" : "HOLD LOOP");
    const macroSel = $("macro-select");
    put(ui.step, "hidden", !($("macro-next") && macroSel && macroSel.value));
    put(ui.merge, "hidden", active || !document.querySelector('[data-ai-action="merge_hold"]'));
    put(ui.merge, "disabled", !pairReady);

    // model + status line: the latest of the autopilot's and the console's status, or a SKIP reply
    const model = (($("ap-model-name") || {}).textContent || "").trim();
    const backend = (($("ap-model-backend") || {}).textContent || "").trim();
    put(ui.model, "text", `model: ${model || "-"}${backend && backend !== "-" ? ` (${backend})` : ""}`);
    put(ui.wake, "text", wake.lock ? "SCREEN ON" : "");
    const apS = (($("ap-status") || {}).textContent || "").trim(), st = (($("status") || {}).textContent || "").trim();
    if (apS !== lastStatus.ap) { lastStatus.ap = apS; lastStatus.shown = apS; }
    if (st !== lastStatus.st) { lastStatus.st = st; lastStatus.shown = st; }
    if (ui.toast && Date.now() - ui.toast.at < 6000) put(ui.status, "text", ui.toast.msg);
    else put(ui.status, "text", lastStatus.shown);

    // MORE tab
    put(ui.wakeTxt, "text", !wake.supported ? "Screen lock: this browser cannot keep the screen on; set Auto-Lock to Never during a set."
      : wake.lock ? "Screen kept on while the set runs." : wake.error ? `Screen lock refused: ${wake.error}` : "Screen kept on once a set runs or a deck plays.");
    const vfxBtn = $("vfx-toggle");
    put(ui.vfx, "aria-pressed", vfxBtn ? vfxBtn.getAttribute("aria-pressed") || "false" : "false");
    put(ui.vfx, "text", vfxBtn && vfxBtn.getAttribute("aria-pressed") === "true" ? "VFX: ON" : "VFX: OFF");
  }
  setInterval(() => tick(false), 200);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { paintAudio(); tick(true); } });

  function init() {
    apply();
    if (mq) {
      const onChange = () => apply();
      if (mq.addEventListener) mq.addEventListener("change", onChange); else if (mq.addListener) mq.addListener(onChange);
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
  root.phoneUi = { core, get phone() { return phone; }, unlockAudio };
})(typeof window !== "undefined" ? window : globalThis);
