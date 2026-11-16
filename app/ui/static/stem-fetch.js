// Stem download for the decks: AAC (.m4a, ?q=phone, the server's phone_audio encoder: ~2.5x smaller than
// FLAC/WAV, MP4 edit list drops the encoder priming so decoded length matches) fetched in parallel with
// byte progress. Pure of DOM; node-checked (app/tests/js/stem_fetch_check.js).
var StemFetch = (function () {
  const NAMES = ["drums", "bass", "vocals", "other"];

  // The AAC variant of a stem URL (same route, same identity checks).
  function aacUrl(url) {
    return url + (url.includes("?") ? "&" : "?") + "q=phone";
  }

  // One body as bytes, reporting (loaded) as chunks arrive. Falls back to arrayBuffer() without a stream.
  async function fetchBytes(url, onBytes, fetchFn) {
    const res = await (fetchFn || fetch)(url);
    if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
    const total = Number(res.headers && res.headers.get && res.headers.get("content-length")) || 0;
    if (!res.body || !res.body.getReader) {
      const buf = await res.arrayBuffer();
      onBytes(buf.byteLength, total || buf.byteLength);
      return buf;
    }
    const reader = res.body.getReader();
    const chunks = [];
    let loaded = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      loaded += value.byteLength;
      onBytes(loaded, total);
    }
    const out = new Uint8Array(loaded);
    let at = 0;
    for (const c of chunks) { out.set(c, at); at += c.byteLength; }
    return out.buffer;
  }

  // All stems in parallel -> {name: decoded}. onProgress({loaded, total, pct}) sums every stream;
  // pct is null until every stream has reported its length.
  async function fetchStems(urls, decode, onProgress, fetchFn) {
    const loaded = {}, total = {};
    const report = () => {
      if (!onProgress) return;
      const l = NAMES.reduce((s, n) => s + (loaded[n] || 0), 0);
      const known = NAMES.every((n) => total[n] > 0);
      const t = NAMES.reduce((s, n) => s + (total[n] || 0), 0);
      onProgress({ loaded: l, total: known ? t : 0, pct: known && t > 0 ? Math.min(100, Math.floor((100 * l) / t)) : null });
    };
    const out = {};
    await Promise.all(NAMES.map(async (n) => {
      const buf = await fetchBytes(aacUrl(urls[n]), (l, t) => { loaded[n] = l; total[n] = t; report(); }, fetchFn);
      out[n] = await decode(buf);
    }));
    return out;
  }

  // The deck's stem status words. phase: "separating" (a split job runs) | "downloading" (bytes coming) | null.
  function label(phase, pct) {
    if (phase === "downloading") return pct == null ? "DOWNLOADING STEMS…" : `DOWNLOADING STEMS ${pct}%`;
    if (phase === "separating") return "SEPARATING…";
    return null;
  }

  return { NAMES, aacUrl, fetchBytes, fetchStems, label };
})();
if (typeof module !== "undefined" && module.exports) module.exports = StemFetch;
