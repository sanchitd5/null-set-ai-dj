// Node check for the console's stem download (app/ui/static/stem-fetch.js): AAC url, parallel fetch,
// byte progress, and the deck's status words. Run by test_stem_fetch.py.
const assert = require("assert");
const SF = require("../../ui/static/stem-fetch.js");

assert.strictEqual(SF.aacUrl("/api/tracks/x/stems/bass"), "/api/tracks/x/stems/bass?q=phone");
assert.strictEqual(SF.aacUrl("/a?b=1"), "/a?b=1&q=phone");

assert.strictEqual(SF.label("separating"), "SEPARATING…");
assert.strictEqual(SF.label("downloading", 43), "DOWNLOADING STEMS 43%");
assert.strictEqual(SF.label("downloading", null), "DOWNLOADING STEMS…");
assert.strictEqual(SF.label(null), null);

// a streamed body of `size` bytes in 4 chunks
function fakeFetch(sizes, seen) {
  return async (url) => {
    seen.push(url);
    const name = url.split("/").pop().split("?")[0];
    const size = sizes[name];
    let sent = 0;
    return {
      ok: true,
      headers: { get: (h) => (h === "content-length" ? String(size) : null) },
      body: { getReader: () => ({ read: async () => {
        if (sent >= size) return { done: true };
        const n = Math.min(size / 4, size - sent); sent += n;
        return { done: false, value: new Uint8Array(n) };
      } }) },
    };
  };
}

(async () => {
  const seen = [], prog = [];
  const urls = Object.fromEntries(SF.NAMES.map((n) => [n, `/api/tracks/t/stems/${n}`]));
  const sizes = { drums: 400, bass: 400, vocals: 400, other: 400 };
  const out = await SF.fetchStems(urls, async (b) => ({ bytes: b.byteLength }), (p) => prog.push(p), fakeFetch(sizes, seen));
  assert.ok(seen.length === 4 && seen.every((u) => u.endsWith("?q=phone")), "all 4 stems asked as AAC");
  for (const n of SF.NAMES) assert.strictEqual(out[n].bytes, 400);
  assert.strictEqual(prog[prog.length - 1].pct, 100);
  assert.strictEqual(prog[prog.length - 1].loaded, 1600);
  const pcts = prog.map((p) => p.pct).filter((p) => p != null);
  assert.ok(pcts.every((p, i) => i === 0 || p >= pcts[i - 1]), "progress never goes back");
  assert.ok(prog[0].pct === null || prog[0].pct <= 100);

  // no stream (sim / old browser): arrayBuffer() path still reports
  const plain = async () => ({ ok: true, headers: { get: () => null }, arrayBuffer: async () => new ArrayBuffer(10) });
  const r = await SF.fetchStems(urls, async (b) => b.byteLength, null, plain);
  assert.strictEqual(r.vocals, 10);

  // a failed stem rejects (the deck keeps the full mix and polls again)
  const bad = async () => ({ ok: false, status: 404, headers: { get: () => null } });
  await assert.rejects(() => SF.fetchStems(urls, async (b) => b, null, bad), /HTTP 404/);
  console.log("stem_fetch ok");
})().catch((e) => { console.error(e); process.exit(1); });
