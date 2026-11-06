// One set at a time (docs/engineering/SET_LEASE.md). START SET takes the server's set lease; another
// client holding it -> "A set is running on <device> since <time>" with TAKE OVER / CANCEL. While held,
// every same-origin /api/ request carries X-Set-Lease (the set-scoped routes answer 409 without it), a
// heartbeat runs every beat_s, and STOP SET / closing the page releases it (sendBeacon on pagehide).
// A 409 on a beat = the set was taken over elsewhere: onLost() stops this console's set.
// The phone web Lite view is this same page, so it uses the same code.
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.setLease = api.createSetLease({ win: root, deviceName: () => {
    const nav = root.navigator || {};
    const kind = root.phoneUi && root.phoneUi.phone ? "phone web console" : "desktop console";
    return nav.platform ? `${kind} (${nav.platform})` : kind;
  } });
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";
  const BASE = "/api/set/lease";

  function fmtSince(sec, now) {
    if (!sec) return "a moment ago";
    const d = new Date(sec * 1000);
    const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    const mins = Math.max(0, Math.round((now - sec * 1000) / 60000));
    return mins < 1 ? `${hm} (just now)` : `${hm} (${mins} min ago)`;
  }

  // "A set is running on <device> since <time>" from a 409 body
  function conflictText(body, now) {
    const h = (body && body.holder) || {};
    const dev = h.device_name || h.name || "another device";
    const where = h.client === "phone" ? `${dev} (phone)` : dev;
    const mine = body && body.same_holder ? " (this device, maybe another tab)" : "";
    return `A set is running on ${where}${mine} since ${fmtSince(body && body.since, now)}`;
  }

  // Same-origin /api/ URL? (relative "/api/..." or absolute on this origin)
  function isApi(url, origin) {
    if (typeof url !== "string") url = url && typeof url.href === "string" ? url.href : "";
    if (url.startsWith("/api/")) return true;
    return !!origin && url.startsWith(origin + "/api/");
  }

  function createSetLease(o = {}) {
    const win = o.win || {};
    const doc = o.doc || win.document;
    const now = o.now || (() => Date.now());
    const setIv = o.setInterval || ((f, ms) => setInterval(f, ms));
    const clearIv = o.clearInterval || ((h) => clearInterval(h));
    const rawFetch = o.fetch || (win.fetch ? win.fetch.bind(win) : null);
    const origin = win.location ? win.location.origin : "";
    let lease = null, timer = null;
    const lost = [];

    const s = {
      get id() { return lease ? lease.id : null; },
      get held() { return !!lease; },
      onLost(fn) { lost.push(fn); },
      // headers to add for [url] (exposed for tests and for code that builds requests itself)
      headersFor(url) { return lease && isApi(url, origin) ? { "X-Set-Lease": lease.id } : {}; },
      conflictText,
    };

    async function call(method, path, body, extra = {}) {
      const r = await rawFetch(path, { method, headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin", ...extra });
      let j = null;
      try { j = await r.json(); } catch (e) { j = null; }
      return { status: r.status, body: j };
    }

    function hold(l) {
      lease = l;
      if (timer) clearIv(timer);
      timer = setIv(beat, Math.max(5, Number(l.beat_s) || 15) * 1000);
    }

    function drop() {
      lease = null;
      if (timer) { clearIv(timer); timer = null; }
    }

    async function beat() {
      if (!lease) return;
      const id = lease.id;
      let r;
      try { r = await call("POST", `${BASE}/${encodeURIComponent(id)}/beat`); } catch (e) { return; } // offline: keep beating
      if (!lease || lease.id !== id) return;
      if (r.status === 409) {
        drop();
        const why = r.body && r.body.detail === "taken over" && r.body.holder
          ? `Set taken over by ${r.body.holder.device_name || "another device"}` : "The server ended this set's lease";
        lost.forEach((fn) => { try { fn(why, r.body); } catch (e) { /* a listener never breaks the beat */ } });
      } else if (r.status === 200 && r.body && r.body.lease) {
        lease = r.body.lease;
      }
    }
    s.beat = beat;

    // The TAKE OVER / CANCEL dialog. Resolves true for TAKE OVER.
    function ask(body) {
      if (o.confirmTakeover) return Promise.resolve(o.confirmTakeover(body));
      if (!doc || !doc.body) return Promise.resolve(false);
      return new Promise((resolve) => {
        const wrap = doc.createElement("div");
        wrap.className = "set-lease-dialog";
        wrap.setAttribute("role", "alertdialog");
        wrap.setAttribute("aria-modal", "true");
        wrap.style.cssText = "position:fixed;inset:0;z-index:99999;display:grid;place-items:center;background:rgba(0,0,0,.6)";
        const box = doc.createElement("div");
        box.style.cssText = "background:#111;color:#eee;border:1px solid #444;border-radius:8px;padding:1.2rem;" +
          "max-width:min(26rem,92vw);font:15px system-ui,sans-serif;display:grid;gap:.9rem";
        const msg = doc.createElement("div");
        msg.textContent = conflictText(body, now());
        const note = doc.createElement("div");
        note.style.cssText = "color:#aaa;font-size:13px";
        note.textContent = "The server runs one set at a time. TAKE OVER ends the set there.";
        const row = doc.createElement("div");
        row.style.cssText = "display:flex;gap:.6rem;justify-content:flex-end";
        const btn = (label, val) => {
          const b = doc.createElement("button");
          b.type = "button";
          b.textContent = label;
          b.style.cssText = "min-height:44px;padding:.4rem 1rem;font:inherit;cursor:pointer";
          b.addEventListener("click", () => { wrap.remove(); resolve(val); });
          return b;
        };
        const take = btn("TAKE OVER", true);
        row.append(btn("CANCEL", false), take);
        box.append(msg, note, row);
        wrap.append(box);
        doc.body.append(wrap);
        try { take.focus(); } catch (e) { /* no focus */ }
      });
    }
    s.ask = ask;

    // START SET. True: go ahead (lease held, or the server has no lease route / is unreachable).
    s.acquire = async function acquire() {
      if (!rawFetch) return true;
      const req = { client: "console", device_name: o.deviceName ? o.deviceName() : "" };
      let r;
      try { r = await call("POST", BASE, req); } catch (e) { return true; }   // server down: nothing to share
      if (r.status === 200 && r.body && r.body.lease) { hold(r.body.lease); return true; }
      if (r.status !== 409) return r.status === 404 || r.status >= 500;      // an old server: no lease
      if (!(await ask(r.body))) return false;
      try { r = await call("POST", `${BASE}/takeover`, req); } catch (e) { return false; }
      if (r.status === 200 && r.body && r.body.lease) { hold(r.body.lease); return true; }
      return false;
    };

    // STOP SET
    s.release = function release() {
      if (!lease) return;
      const id = lease.id;
      drop();
      if (rawFetch) rawFetch(`${BASE}/${encodeURIComponent(id)}`, { method: "DELETE", keepalive: true, credentials: "same-origin" })
        .catch(() => {});
    };

    // page closing: a beacon (POST) survives the unload where a fetch may not
    s.releaseOnUnload = function () {
      if (!lease) return;
      const id = lease.id;
      drop();
      const nav = win.navigator;
      if (nav && typeof nav.sendBeacon === "function") nav.sendBeacon(`${BASE}/${encodeURIComponent(id)}/release`);
    };

    // every same-origin /api/ request carries the lease while held
    s.wrapFetch = function (f) {
      return function (input, init) {
        const h = s.headersFor(typeof input === "string" || (input && input.href) ? input : "");
        if (!h["X-Set-Lease"]) return f(input, init);
        const i = { ...(init || {}) };
        const H = typeof Headers !== "undefined" ? new Headers(i.headers || {}) : null;
        if (H) { H.set("X-Set-Lease", h["X-Set-Lease"]); i.headers = H; } else i.headers = { ...(i.headers || {}), ...h };
        return f(input, i);
      };
    };

    if (win.fetch && !o.fetch) {
      win.fetch = s.wrapFetch(win.fetch.bind(win));
      if (win.addEventListener) win.addEventListener("pagehide", s.releaseOnUnload);
    }
    return s;
  }

  return { createSetLease, conflictText, isApi };
});
