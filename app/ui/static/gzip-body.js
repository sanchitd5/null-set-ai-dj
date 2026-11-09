// Big JSON request bodies go up gzip-compressed (server side: auth.GzipRequestMiddleware): a same-origin /api/
// POST / PUT / PATCH with a JSON string body over MIN_BYTES is run through the browser's own
// CompressionStream("gzip") and sent with `Content-Encoding: gzip` (the server inflates it, with its
// size limits applied to the inflated body). No CompressionStream -> plain, as before. A 415 (an older
// server) resends the same request plain, once, and the rest of the page's session sends plain.
// Wraps window.fetch like set-lease.js does. Responses need nothing: the browser inflates those itself.
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else api.install(root);
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";
  const MIN_BYTES = 4096;
  const METHODS = ["POST", "PUT", "PATCH"];

  function urlOf(input) {
    return typeof input === "string" ? input : input && typeof input.href === "string" ? input.href : "";
  }

  function isApi(url, origin) {
    return url.startsWith("/api/") || (!!origin && url.startsWith(origin + "/api/"));
  }

  function headerOf(headers, name) {
    if (!headers) return "";
    if (typeof headers.get === "function") return headers.get(name) || "";
    const want = name.toLowerCase();
    const list = Array.isArray(headers) ? headers : Object.entries(headers);
    for (const [k, v] of list) if (String(k).toLowerCase() === want) return String(v);
    return "";
  }

  // Worth compressing? (string JSON body, over the threshold, nothing already encoded it)
  function eligible(input, init, origin) {
    if (!init || typeof init.body !== "string" || init.body.length <= MIN_BYTES) return false;
    if (typeof input !== "string" && !(input && typeof input.href === "string")) return false;   // a Request object: leave it
    if (!isApi(urlOf(input), origin)) return false;
    if (!METHODS.includes(String(init.method || "GET").toUpperCase())) return false;
    if (headerOf(init.headers, "content-encoding")) return false;
    return /^application\/json/i.test(headerOf(init.headers, "content-type"));
  }

  async function gzipText(text, win) {
    const stream = new win.Blob([text]).stream().pipeThrough(new win.CompressionStream("gzip"));
    return new Uint8Array(await new win.Response(stream).arrayBuffer());
  }

  function create(win, fetchFn) {
    const origin = win.location ? win.location.origin : "";
    const state = { plain: !win.CompressionStream || !win.Blob || !win.Response, saved: 0 };

    async function wrapped(input, init) {
      if (state.plain || !eligible(input, init, origin)) return fetchFn(input, init);
      let packed;
      try { packed = await gzipText(init.body, win); } catch (e) { return fetchFn(input, init); }
      if (packed.length >= init.body.length) return fetchFn(input, init);
      const H = new win.Headers(init.headers || {});
      H.set("Content-Encoding", "gzip");
      const res = await fetchFn(input, { ...init, headers: H, body: packed });
      if (res.status !== 415) { state.saved += init.body.length - packed.length; return res; }
      state.plain = true;                                  // an older server: plain from now on
      return fetchFn(input, init);
    }
    return { fetch: wrapped, state };
  }

  function install(win) {
    if (!win.fetch || win.__gzipBody) return null;
    const g = create(win, win.fetch.bind(win));
    win.fetch = g.fetch;
    win.__gzipBody = g.state;
    return g;
  }

  return { create, install, eligible, MIN_BYTES };
});
