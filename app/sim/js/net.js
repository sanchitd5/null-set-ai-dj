// fetch() for the headless console: real HTTP to the private API port (the real FastAPI app),
// serialised in issue order, answered to the page after a MODELLED latency on the virtual
// clock. The real round trip takes however long it takes; the page never sees that time, so a
// run is the same on a fast or a slow machine and the order of events never depends on which
// request finished first.
"use strict";
const http = require("http");
const { performance: realPerf } = require("perf_hooks");

// virtual seconds a reply takes to reach the page, by endpoint. The values are typical of the
// live app (LLM ~9 s, a download ~25 s, a match ~1.5 s); they matter only to timing races
// (deadlines, the exit window an in-flight plan has to beat).
const LATENCY = [
  [/^\/api\/autopilot\/suggest/, 9], [/^\/api\/autopilot\/plan/, 8], [/^\/api\/download(?!\/jobs)/, 25],
  [/^\/api\/download\/jobs/, 0.2], [/^\/api\/match/, 1.5], [/^\/api\/blend\/plan/, 1], [/^\/api\/layer\/plan/, 1],
  [/^\/api\/riff\/plan/, 2], [/^\/api\/mashup\/plan/, 2], [/^\/api\/transition\/preplan/, 0.5],
  [/^\/api\/merge\/audition/, 0.5], [/^\/api\/audio\/tracks/, 1.0], [/^\/api\/tracks\/[^/]+\/stems\/[a-z]+/, 0.4],
  [/^\/api\/tracks\/[^/]+\/analysis/, 0.2], [/^\/api\/live\/ear/, 1.5], [/^\/api\/search/, 1],
];
const DOWNLOAD_S = 25;      // a download job's modelled duration (yt-dlp + analysis), seconds
function latencyFor(path) { for (const [re, s] of LATENCY) if (re.test(path)) return s; return 0.05; }

class Net {
  constructor({ host, port, clock, headers }) {
    this.host = host; this.port = port; this.clock = clock; this.headers = headers || {};
    this._chain = Promise.resolve();
    this.inflight = 0;
    this.log = [];
    this.marks = { transitionEnd: 0 };
    this.sessionEvents = [];              // what the console told the server about the set, virtual time
    this.jobStart = {};
    clock.pendingNet = () => this.inflight;
  }
  // Background jobs run to completion inside the request in the sim (so their result never depends on
  // how fast a thread ran). The console polls them as it does live: a download job that the server
  // finished at once is reported "running" until its modelled download time has passed.
  _model(method, path, r, issuedAt) {
    try {
      if (method === "POST" && path.startsWith("/api/download/jobs")) {
        const j = JSON.parse(r.body.toString("utf8"));
        if (j.job_id) this.jobStart[j.job_id] = issuedAt;
      } else if (method === "GET") {
        const m = /^\/api\/download\/jobs\/([0-9a-f]+)/.exec(path);
        const start = m && this.jobStart[m[1]];
        if (start !== undefined && this.clock.now - start < DOWNLOAD_S) {
          const j = JSON.parse(r.body.toString("utf8"));
          if (j.state === "done" || j.state === "error") {
            r.body = Buffer.from(JSON.stringify({ ...j, state: "running", stage: "downloading", percent: Math.round(Math.min(99, ((this.clock.now - start) / DOWNLOAD_S) * 100)), tracks: [], error: null }));
          }
        }
      }
    } catch (e) { /* not a job body */ }
  }
  _request(method, path, headers, body) {
    return new Promise((resolve, reject) => {
      const t0 = realPerf.now();
      const req = http.request({ host: this.host, port: this.port, method, path, headers: { ...this.headers, ...headers } }, (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => resolve({ status: res.statusCode, statusText: res.statusMessage, headers: res.headers, body: Buffer.concat(chunks), real: realPerf.now() - t0 }));
        res.on("error", reject);
      });
      req.on("error", reject);
      if (body) req.write(body);
      req.end();
    });
  }
  fetch(input, opts = {}) {
    const url = typeof input === "string" ? input : (input && input.url) || String(input);
    const path = url.startsWith("http") ? new URL(url).pathname + new URL(url).search : url;
    const method = (opts.method || "GET").toUpperCase();
    const signal = opts.signal;
    if (signal && signal.aborted) return Promise.reject(abortError());
    const headers = {};
    for (const [k, v] of Object.entries(opts.headers || {})) headers[k.toLowerCase()] = v;
    let body = opts.body;
    if (body && typeof body !== "string" && !Buffer.isBuffer(body)) {
      if (typeof body.arrayBuffer === "function") { /* Blob / FormData not needed by the autopilot */ }
      body = String(body);
    }
    const issuedAt = this.clock.now;
    headers["x-sim-time"] = issuedAt.toFixed(3);                 // the sim world's clock is the console's
    if (path.startsWith("/api/session/event") && typeof body === "string") {
      try { const ev = JSON.parse(body); this.sessionEvents.push({ t: +issuedAt.toFixed(3), kind: ev.kind, data: ev.data }); } catch (e) { /* not JSON */ }
      if (body.includes("transition_end")) this.marks.transitionEnd++;
    }
    if (typeof body === "string" && body.length && !Buffer.isBuffer(body) && !headers["content-length"]) headers["content-length"] = String(Buffer.byteLength(body));
    this.inflight++;
    let done;
    const result = new Promise((resolve, reject) => { done = { resolve, reject }; });
    // serialise: one real request at a time, in the order the page issued them
    this._chain = this._chain.then(() => this._request(method, path, headers, body)).then((r) => {
      this.inflight--;
      const lat = latencyFor(path.split("?")[0]);
      this.log.push({ t: +issuedAt.toFixed(3), method, path: path.slice(0, 160), status: r.status, latency: lat, bytes: r.body.length });
      const respond = () => {
        if (signal && signal.aborted) return done.reject(abortError());
        this._model(method, path, r, issuedAt);
        done.resolve(makeResponse(r, url));
      };
      if (lat > 0) this.clock.setTimeout(respond, lat * 1000); else respond();
    }, (e) => {
      this.inflight--;
      this.log.push({ t: +issuedAt.toFixed(3), method, path: path.slice(0, 160), status: 0, error: String(e.message || e) });
      done.reject(new TypeError("Failed to fetch"));
    });
    if (signal) signal.addEventListener("abort", () => done.reject(abortError()), { once: true });
    return result;
  }
}
function abortError() { const e = new Error("The operation was aborted."); e.name = "AbortError"; return e; }
function makeResponse(r, url) {
  const buf = r.body;
  const ab = () => buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
  const res = {
    ok: r.status >= 200 && r.status < 300, status: r.status, statusText: r.statusText || "", url, redirected: false, type: "basic",
    headers: { get: (k) => { const v = r.headers[String(k).toLowerCase()]; return v === undefined ? null : String(v); }, has: (k) => String(k).toLowerCase() in r.headers, forEach: (f) => Object.entries(r.headers).forEach(([k, v]) => f(v, k)) },
    json: () => Promise.resolve().then(() => JSON.parse(buf.toString("utf8"))),
    text: () => Promise.resolve(buf.toString("utf8")),
    arrayBuffer: () => Promise.resolve(ab()),
    blob: () => Promise.resolve(new Blob([buf], { type: r.headers["content-type"] || "" })),
    clone: () => makeResponse(r, url),
    body: null,
  };
  return res;
}

module.exports = { Net, latencyFor };
