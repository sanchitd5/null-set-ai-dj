// Node check for app/ui/static/set-lease.js: acquire / 409 dialog / takeover / beat / lost / release and the
// X-Set-Lease header on same-origin /api/ requests.
const assert = require("assert");
const { createSetLease, conflictText, isApi } = require("../../ui/static/set-lease.js");

function fakeServer() {
  const srv = { calls: [], holder: null, next: 1, beacons: [] };
  srv.fetch = async (url, init = {}) => {
    const method = init.method || "GET";
    srv.calls.push([method, url]);
    const body = init.body ? JSON.parse(init.body) : null;
    const res = (status, j) => ({ status, json: async () => j });
    const lease = () => ({ id: `L${srv.next++}`, client: "console", device_name: body.device_name, beat_s: 15 });
    if (srv.down) throw new TypeError("Failed to fetch");
    if (method === "POST" && url === "/api/set/lease") {
      if (srv.holder) return res(409, { code: "set_lease", detail: "a set is running", since: 1700000000,
        holder: { client: "phone", device_name: "pixel" }, same_holder: false });
      srv.holder = lease();
      return res(200, { lease: srv.holder });
    }
    if (method === "POST" && url === "/api/set/lease/takeover") { srv.holder = lease(); return res(200, { lease: srv.holder }); }
    const m = url.match(/^\/api\/set\/lease\/([^/]+)(\/beat)?$/);
    if (m && m[2]) return srv.holder && srv.holder.id === m[1] ? res(200, { lease: srv.holder })
      : res(409, { detail: "taken over", holder: { device_name: "pixel" } });
    if (m && method === "DELETE") { const ok = srv.holder && srv.holder.id === m[1]; if (ok) srv.holder = null; return res(200, { ok: true, released: !!ok }); }
    return res(404, {});
  };
  return srv;
}

function timers() {
  const t = { live: new Set() };
  t.setInterval = (f, ms) => { const h = { f, ms }; t.live.add(h); return h; };
  t.clearInterval = (h) => t.live.delete(h);
  return t;
}

(async () => {
  // pure helpers
  assert.ok(isApi("/api/autopilot/suggest", "https://x"));
  assert.ok(isApi("https://x/api/live/ear", "https://x"));
  assert.ok(!isApi("https://evil/api/live/ear", "https://x"));
  assert.ok(!isApi("/stems/v1/x", "https://x"));
  const txt = conflictText({ holder: { client: "phone", device_name: "pixel" }, since: 1700000000 }, 1700000000 * 1000 + 5 * 60000);
  assert.ok(/^A set is running on pixel \(phone\) since \d\d:\d\d \(5 min ago\)$/.test(txt), txt);

  // grant -> header -> beat -> release
  {
    const srv = fakeServer(), t = timers();
    const L = createSetLease({ fetch: srv.fetch, ...t, win: { location: { origin: "https://x" } }, deviceName: () => "studio" });
    assert.deepStrictEqual(L.headersFor("/api/autopilot/suggest"), {});
    assert.strictEqual(await L.acquire(), true);
    assert.strictEqual(L.id, "L1");
    assert.strictEqual(t.live.size, 1);
    assert.strictEqual([...t.live][0].ms, 15000);
    assert.deepStrictEqual(L.headersFor("/api/autopilot/suggest"), { "X-Set-Lease": "L1" });
    assert.deepStrictEqual(L.headersFor("https://other/api/x"), {});
    // the fetch wrapper adds it
    let seen = null;
    const wrapped = L.wrapFetch((u, i) => { seen = i; return Promise.resolve(); });
    await wrapped("/api/live/ear", { method: "POST", headers: { "Content-Type": "application/json" } });
    assert.strictEqual(seen.headers.get("X-Set-Lease"), "L1");
    assert.strictEqual(seen.headers.get("Content-Type"), "application/json");
    await wrapped("/stems/x", { method: "GET" });
    assert.ok(!seen.headers || !seen.headers.get || !seen.headers.get("X-Set-Lease"));
    await L.beat();
    assert.strictEqual(L.held, true);
    L.release();
    await new Promise((r) => setImmediate(r));
    assert.strictEqual(L.held, false);
    assert.strictEqual(t.live.size, 0);
    assert.strictEqual(srv.holder, null);
    assert.deepStrictEqual(L.headersFor("/api/autopilot/suggest"), {});
  }

  // 409 -> dialog: CANCEL keeps out, TAKE OVER takes the set
  {
    const srv = fakeServer(), t = timers();
    srv.holder = { id: "P", device_name: "pixel" };
    const asked = [];
    let answer = false;
    const L = createSetLease({ fetch: srv.fetch, ...t, confirmTakeover: (b) => { asked.push(b); return answer; } });
    assert.strictEqual(await L.acquire(), false);
    assert.strictEqual(L.held, false);
    assert.strictEqual(asked[0].holder.device_name, "pixel");
    answer = true;
    assert.strictEqual(await L.acquire(), true);
    assert.ok(srv.calls.some(([m, u]) => m === "POST" && u === "/api/set/lease/takeover"));
    assert.strictEqual(L.held, true);
  }

  // taken over elsewhere: the next beat is a 409 -> onLost, header gone, beats stop
  {
    const srv = fakeServer(), t = timers();
    const L = createSetLease({ fetch: srv.fetch, ...t });
    const lost = [];
    L.onLost((why) => lost.push(why));
    await L.acquire();
    srv.holder = { id: "OTHER" };
    await L.beat();
    assert.deepStrictEqual(lost, ["Set taken over by pixel"]);
    assert.strictEqual(L.held, false);
    assert.strictEqual(t.live.size, 0);
  }

  // a network failure on a beat is not a loss; a server that is down at START lets the set run
  {
    const srv = fakeServer(), t = timers();
    const L = createSetLease({ fetch: srv.fetch, ...t });
    await L.acquire();
    srv.down = true;
    await L.beat();
    assert.strictEqual(L.held, true);
    const L2 = createSetLease({ fetch: srv.fetch, ...t });
    assert.strictEqual(await L2.acquire(), true);
    assert.strictEqual(L2.held, false);
  }

  // page closing: sendBeacon to the POST release route
  {
    const srv = fakeServer(), t = timers();
    const beacons = [];
    const win = { navigator: { sendBeacon: (u) => beacons.push(u) } };
    const L = createSetLease({ fetch: srv.fetch, ...t, win });
    await L.acquire();
    L.releaseOnUnload();
    assert.deepStrictEqual(beacons, ["/api/set/lease/L1/release"]);
    assert.strictEqual(L.held, false);
  }

  // the browser bootstrap wraps window.fetch and listens for pagehide
  {
    const srv = fakeServer();
    const ev = {};
    const win = { fetch: srv.fetch, location: { origin: "https://x" }, addEventListener: (k, f) => { ev[k] = f; } };
    const L = createSetLease({ win, setInterval: () => 1, clearInterval: () => {} });
    assert.notStrictEqual(win.fetch, srv.fetch);
    assert.strictEqual(typeof ev.pagehide, "function");
    await L.acquire();
    assert.strictEqual(L.id, "L1");
  }

  console.log("set lease ok");
})().catch((e) => { console.error(e); process.exit(1); });
