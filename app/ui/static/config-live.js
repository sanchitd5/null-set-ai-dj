// Applies the server's live dj-config values (dj-config "Live-override contract"). The server serves GET /config-live.js as
// `window.DJ_CONFIG_SERVER = {...};` followed by this file, so the values arrive with the script: no XHR / fetch at load.
// Loaded after generated/dj_config.js and before every script that reads DJ_CONFIG. Only names in the bundled
// DJ_CONFIG_LIVE table are taken, each checked against the BUNDLED min..max; no / bad data keeps the bundled defaults.
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
  if (root.DJ_CONFIG && root.DJ_CONFIG_LIVE && root.DJ_CONFIG_SERVER) {
    root.DJ_CONFIG = applyLive(root.DJ_CONFIG, root.DJ_CONFIG_LIVE, root.DJ_CONFIG_SERVER);
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { applyLive };
})(typeof window !== "undefined" ? window : globalThis);
