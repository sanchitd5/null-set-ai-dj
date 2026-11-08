// Live dj-config overrides (dj-config/README "Live-override contract"). Loaded right after
// generated/dj_config.js and before every script that reads DJ_CONFIG, so the constants those scripts copy
// at load already carry the server's values. One synchronous GET /api/config/live (same origin, session
// cookie); any failure keeps the bundled defaults. Only names in the bundled DJ_CONFIG_LIVE table are taken,
// and each value is checked against the BUNDLED min..max (the server is not trusted for bounds).
(function (root) {
  "use strict";
  function applyLive(base, bounds, payload) {
    const out = Object.assign({}, base);
    const vals = payload && typeof payload === "object" ? payload.values : null;
    if (!vals || typeof vals !== "object") return base;
    for (const name of Object.keys(bounds)) {
      const e = vals[name];
      const v = e && typeof e === "object" ? e.value : e;
      if (typeof v === "number" && Number.isFinite(v)) out[name] = Math.min(Math.max(v, bounds[name].min), bounds[name].max);
    }
    return Object.freeze(out);
  }
  function fetchLive() {
    try {
      const x = new XMLHttpRequest();
      x.open("GET", "/api/config/live", false);
      x.send();
      return x.status === 200 ? JSON.parse(x.responseText) : null;
    } catch (e) { return null; }
  }
  if (typeof XMLHttpRequest !== "undefined" && root.DJ_CONFIG && root.DJ_CONFIG_LIVE) {
    root.DJ_CONFIG = applyLive(root.DJ_CONFIG, root.DJ_CONFIG_LIVE, fetchLive());
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { applyLive };
})(typeof window !== "undefined" ? window : globalThis);
