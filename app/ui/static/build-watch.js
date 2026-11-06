// Reload prompt: the console notices a newer server build and offers "New version - reload".
// GET /api/build ({build}: the server's git short sha, app/ui/services/build_info.py) is polled every
// 60 s and whenever the page becomes visible again. The first answer is the build this page loaded
// with; a different one later shows the banner (desktop and phone Lite: it is the same page).
// It never reloads by itself. RELOAD during a playing set asks first, since a reload stops the set.
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else if (root.document) root.buildWatch = api.createBuildWatch({ win: root, isPlaying: () => api.setPlaying(root) });
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";
  const URL = "/api/build";
  const POLL_MS = 60000;
  const TEXT = "New version - reload";
  const ASK = "A set is playing. Reloading the page stops it. Reload now?";

  // a different, non-empty build id than the one this page loaded with
  function isNewer(known, latest) {
    return typeof known === "string" && typeof latest === "string" && !!known && !!latest && known !== latest;
  }

  // anything on air: a deck playing or the autopilot running a set
  function setPlaying(win) {
    const ap = win.autopilotState;
    if (ap && ap.active) return true;
    return Object.values(win.decks || {}).some((d) => d && d.playing);
  }

  function createBuildWatch(o) {
    const win = o.win;
    const doc = o.doc || win.document;
    const get = o.fetch || ((u, i) => win.fetch(u, i));
    const isPlaying = o.isPlaying || (() => false);
    const s = { known: null, latest: null, stale: false, banner: null };

    function show() {
      if (s.banner || !doc || !doc.body) { if (s.banner) s.banner.hidden = false; return; }
      const b = doc.createElement("div");
      b.id = "build-banner";
      b.className = "build-banner";
      b.setAttribute("role", "status");
      const btn = doc.createElement("button");
      btn.type = "button";
      btn.className = "build-reload";
      btn.textContent = TEXT;
      btn.addEventListener("click", () => s.reload());
      const later = doc.createElement("button");
      later.type = "button";
      later.className = "build-later";
      later.setAttribute("aria-label", "Later");
      later.textContent = "×";
      later.addEventListener("click", () => { b.hidden = true; });
      b.append(btn, later);
      doc.body.append(b);
      s.banner = b;
    }

    s.reload = function reload() {
      if (isPlaying() && !(win.confirm && win.confirm(ASK))) return false;
      win.location.reload();
      return true;
    };

    s.check = async function check() {
      let build;
      try {
        const r = await get(URL, { cache: "no-store", credentials: "same-origin" });
        if (!r || !r.ok) return s.stale;               // restarting / signed out: ask again next time
        build = (await r.json()).build;
      } catch (e) {
        return s.stale;
      }
      if (typeof build !== "string" || !build) return s.stale;
      if (s.known === null) s.known = build;
      const newer = isNewer(s.known, build);
      if (newer && build !== s.latest) { s.latest = build; s.stale = true; show(); }
      return s.stale;
    };

    if (o.autostart !== false) {
      s.check();
      (o.setInterval || win.setInterval).call(win, s.check, o.pollMs || POLL_MS);
      if (doc && doc.addEventListener) {
        doc.addEventListener("visibilitychange", () => { if (doc.visibilityState === "visible") s.check(); });
      }
    }
    return s;
  }

  return { createBuildWatch, isNewer, setPlaying, TEXT, ASK, POLL_MS };
});
