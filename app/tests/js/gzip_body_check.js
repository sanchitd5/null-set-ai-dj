// Node check for app/ui/static/gzip-body.js: threshold, header, gzip round trip, 415 fallback, no CompressionStream.
const assert = require("assert");
const zlib = require("zlib");
const { create, eligible, MIN_BYTES } = require("../../ui/static/gzip-body.js");

const win = { location: { origin: "https://dj.example" }, CompressionStream, Blob, Response, Headers };
const JSONH = { "Content-Type": "application/json" };
const big = JSON.stringify({ steps: Array.from({ length: 300 }, (_, i) => ({ i, kind: "blend", note: "x".repeat(40) })) });
const small = JSON.stringify({ ok: true });

(async () => {
  assert.ok(big.length > MIN_BYTES && small.length < MIN_BYTES);

  // eligibility: only big same-origin JSON writes
  assert.strictEqual(eligible("/api/set-logs", { method: "POST", headers: JSONH, body: big }, win.location.origin), true);
  assert.strictEqual(eligible("https://dj.example/api/x", { method: "PUT", headers: JSONH, body: big }, win.location.origin), true);
  assert.strictEqual(eligible("/api/x", { method: "POST", headers: JSONH, body: small }, ""), false);
  assert.strictEqual(eligible("/api/x", { method: "POST", headers: JSONH, body: "a".repeat(MIN_BYTES) }, ""), false);   // exactly at it: plain
  assert.strictEqual(eligible("/api/x", { method: "GET", headers: JSONH, body: big }, ""), false);
  assert.strictEqual(eligible("/api/x", { method: "POST", headers: { "Content-Type": "text/plain" }, body: big }, ""), false);
  assert.strictEqual(eligible("/api/x", { method: "POST", headers: { ...JSONH, "Content-Encoding": "br" }, body: big }, ""), false);
  assert.strictEqual(eligible("https://other.example/api/x", { method: "POST", headers: JSONH, body: big }, win.location.origin), false);
  assert.strictEqual(eligible("/static/x.json", { method: "POST", headers: JSONH, body: big }, ""), false);
  assert.strictEqual(eligible("/api/x", { method: "POST", headers: JSONH, body: new Uint8Array(9000) }, ""), false);    // already binary
  assert.strictEqual(eligible("/api/x", { method: "POST", headers: new Headers(JSONH), body: big }, ""), true);

  // a big JSON body: gzip + header, inflates back to the same text
  const seen = [];
  const ok = async (input, init) => { seen.push({ input, init }); return { status: 200 }; };
  let g = create(win, ok);
  await g.fetch("/api/set-logs", { method: "POST", headers: JSONH, body: big });
  assert.strictEqual(seen[0].init.headers.get("content-encoding"), "gzip");
  assert.strictEqual(zlib.gunzipSync(Buffer.from(seen[0].init.body)).toString(), big);
  assert.ok(seen[0].init.body.length < big.length / 3);
  assert.strictEqual(g.state.saved, big.length - seen[0].init.body.length);

  // small body: untouched
  await g.fetch("/api/x", { method: "POST", headers: JSONH, body: small });
  assert.strictEqual(seen[1].init.body, small);

  // 415: resent plain, then plain for the session
  const calls = [];
  g = create(win, async (input, init) => { calls.push(init); return { status: init.headers.get && init.headers.get("content-encoding") ? 415 : 200 }; });
  const r = await g.fetch("/api/set-logs", { method: "POST", headers: JSONH, body: big });
  assert.strictEqual(r.status, 200);
  assert.strictEqual(calls.length, 2);
  assert.strictEqual(calls[1].body, big);
  assert.strictEqual(g.state.saved, 0);
  await g.fetch("/api/set-logs", { method: "POST", headers: JSONH, body: big });
  assert.strictEqual(calls.length, 3);
  assert.strictEqual(calls[2].body, big);

  // another error is the answer, not a retry
  let n = 0;
  g = create(win, async () => { n++; return { status: 413 }; });
  assert.strictEqual((await g.fetch("/api/x", { method: "POST", headers: JSONH, body: big })).status, 413);
  assert.strictEqual(n, 1);

  // no CompressionStream: plain
  const plainSeen = [];
  g = create({ location: win.location }, async (i, init) => { plainSeen.push(init); return { status: 200 }; });
  await g.fetch("/api/x", { method: "POST", headers: JSONH, body: big });
  assert.strictEqual(plainSeen[0].body, big);

  console.log("gzip_body_check ok");
})().catch((e) => { console.error(e); process.exit(1); });
