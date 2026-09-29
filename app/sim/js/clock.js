// Virtual clock for the headless console: Date.now, performance.now, setTimeout / setInterval,
// requestAnimationFrame. Nothing here reads the wall clock: a 10 song set is a few thousand
// timer firings, run back to back.
//
// run(until): fire due timers in (time, sequence) order. Between two timers it lets every
// promise settle (setImmediate), and while the network layer still has a request in flight it
// keeps waiting in REAL time without moving the virtual clock, so the order of events never
// depends on how fast the server answered.
"use strict";

const EPOCH_MS = Date.UTC(2026, 8, 29, 20, 0, 0);       // the set "starts" 2026-09-29 20:00 UTC

class VirtualClock {
  constructor() {
    this.now = 0;                    // virtual seconds since the set started
    this._seq = 0;
    this._timers = new Map();        // id -> {at, seq, fn, every, args}
    this._next = 1;
    this.pendingNet = () => 0;       // net.js replaces it
    this.firings = 0;
  }
  ms() { return this.now * 1000; }
  dateNow() { return EPOCH_MS + this.now * 1000; }

  setTimeout(fn, ms, ...args) {
    if (typeof fn !== "function") return 0;
    const id = this._next++;
    this._timers.set(id, { at: this.now + Math.max(0, Number(ms) || 0) / 1000, seq: this._seq++, fn, every: 0, args });
    return id;
  }
  setInterval(fn, ms, ...args) {
    if (typeof fn !== "function") return 0;
    const id = this._next++;
    const every = Math.max(1, Number(ms) || 0) / 1000;
    this._timers.set(id, { at: this.now + every, seq: this._seq++, fn, every, args });
    return id;
  }
  clear(id) { this._timers.delete(id); }

  _nextTimer() {
    let best = null, bestId = null;
    for (const [id, t] of this._timers) {
      if (best === null || t.at < best.at || (t.at === best.at && t.seq < best.seq)) { best = t; bestId = id; }
    }
    return best ? [bestId, best] : null;
  }

  async settle() {
    // promises first, then real IO (in-flight requests) until the network layer is idle
    for (;;) {
      await new Promise((r) => setImmediate(r));
      if (this.pendingNet() === 0) {
        await new Promise((r) => setImmediate(r));
        if (this.pendingNet() === 0) return;
      }
    }
  }

  // Advance to virtual time `until` (seconds), or until stop() says so.
  async run(until, stop = () => false) {
    await this.settle();
    for (;;) {
      if (stop()) return "stopped";
      const nx = this._nextTimer();
      if (!nx || nx[1].at > until) { this.now = Math.max(this.now, Math.min(until, nx ? nx[1].at : until)); return nx ? "until" : "idle"; }
      const [id, t] = nx;
      this.now = Math.max(this.now, t.at);
      if (t.every) { t.at = this.now + t.every; t.seq = this._seq++; } else this._timers.delete(id);
      this.firings++;
      const t0 = this.profile ? process.hrtime.bigint() : 0n;
      try { t.fn(...t.args); } catch (e) { this.onError && this.onError(e, t); }
      if (this.profile) {
        const k = `${t.every ? "every " + Math.round(t.every * 1000) + "ms " : ""}${String(t.fn).replace(/\s+/g, " ").slice(0, 90)}`;
        const cur = this.profile.get(k) || { n: 0, ms: 0 };
        cur.n++; cur.ms += Number(process.hrtime.bigint() - t0) / 1e6; this.profile.set(k, cur);
      }
      await this.settle();
    }
  }
}

module.exports = { VirtualClock, EPOCH_MS };
