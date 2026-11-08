// Node check: library-row album art loads only near the viewport, at most 4 at a time, and a re-render of the
// list never refetches a cover that was already shown (the HOT1 fix for 700 parallel /art requests).
const assert = require("assert");
const path = require("path");

let decodes = 0, maxInFlight = 0, inFlight = 0;
const pending = [];
class FakeImage { set src(u) { this._src = u; } decode() { decodes++; inFlight++; maxInFlight = Math.max(maxInFlight, inFlight);
  return new Promise((res) => pending.push(() => { inFlight--; res(); })); } }
let observer = null;
class FakeIO { constructor(cb) { this.cb = cb; this.watched = new Set(); observer = this; }
  observe(el) { this.watched.add(el); } unobserve(el) { this.watched.delete(el); }
  fire(els) { this.cb(els.map((t) => ({ target: t, isIntersecting: true }))); } }

function row(id) {
  const img = { src: "" };
  return { dataset: { art: `/api/tracks/${id}/art?size=160`, ph: "1", track: id }, isConnected: true,
    querySelector: () => img, img };
}
const doc = { readyState: "complete", hidden: true, addEventListener() {}, querySelector: () => null,
  getElementById: () => null, createElement: () => ({}) };
global.window = global;
Object.assign(global, { document: doc, Image: FakeImage, IntersectionObserver: FakeIO,
  requestAnimationFrame: () => 0, setTimeout: () => 0, addEventListener() {}, fetch: async () => ({ ok: false }) });
require(path.join(__dirname, "../../ui/static/album-art.js"));
const flush = async () => { for (let i = 0; i < 50; i++) await Promise.resolve(); };

(async () => {
  const rows = Array.from({ length: 700 }, (_, i) => row("t" + i));
  const list = { querySelectorAll: () => rows };
  window.AlbumArt.fillRows(list);
  assert.strictEqual(decodes, 0, "nothing fetched before a row is visible");
  assert.strictEqual(observer.watched.size, 700);

  observer.fire(rows.slice(0, 10));                       // 10 rows scroll into view
  assert.strictEqual(decodes, 4, "at most 4 covers in flight");
  while (pending.length) { pending.shift()(); await flush(); }
  assert.strictEqual(decodes, 10, "the 10 visible covers load, nothing else");
  assert.ok(maxInFlight <= 4, "never more than 4 in flight");
  assert.strictEqual(rows[3].img.src, rows[3].dataset.art);
  assert.strictEqual(rows[50].img.src, "", "an off-screen row stays empty");

  const again = Array.from({ length: 700 }, (_, i) => row("t" + i));   // browser.js re-renders the list
  window.AlbumArt.fillRows({ querySelectorAll: () => again });
  assert.strictEqual(decodes, 10, "a re-render refetches nothing");
  assert.strictEqual(again[3].img.src, again[3].dataset.art, "shown covers come back straight from cache");
  console.log("album_art_rows_check OK");
})().catch((e) => { console.error(e); process.exit(1); });
