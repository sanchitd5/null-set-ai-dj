// Node check for album art (app/ui/static/album-art.js): the crossfade follows the mix, placeholder, late art.
const assert = require("assert");
const { artUrl, shares, ArtSlot, Mixer } = require("../../ui/static/album-art.js");

// ---- size variants
assert.strictEqual(artUrl("/api/tracks/x/art?size=600&v=9", 160), "/api/tracks/x/art?size=160&v=9");
assert.strictEqual(artUrl(null, 160), null);

// ---- opacity follows the crossfader gains (the console's equal-power curve, deck-controller.js applyCrossfader)
const gains = (v) => { const x = (v + 1) / 2; return [Math.cos(x * 0.5 * Math.PI), Math.cos((1 - x) * 0.5 * Math.PI)]; };
const layer = (deck, mode) => ({ deck, mode, el: { style: {} } });
const L = [layer("a", "now"), layer("b", "now"), layer("a", "next"), layer("b", "next")];
const mx = new Mixer(L);
const op = (i) => parseFloat(L[i].el.style.opacity);

// start: A on air
mx.apply(shares(...gains(-1), true, true));
assert.deepStrictEqual([op(0), op(1), op(2), op(3)], [1, 0, 0, 1]);          // NOW = A, NEXT = B

// a transition: 0 -> 1 across the move, B's NOW opacity only ever rises, A's only falls, they sum to 1
let prevB = 0, writes = 0;
for (let k = 0; k <= 40; k++) {
  const v = -1 + 2 * (k / 40);
  if (mx.apply(shares(...gains(v), true, true))) writes++;
  assert.ok(op(1) >= prevB - 1e-9, "B never goes back");
  assert.ok(Math.abs(op(0) + op(1) - 1) < 0.011, "the two layers sum to 1");
  assert.ok(Math.abs(op(2) - op(1)) < 1e-9, "NEXT is the mirror of NOW");
  prevB = op(1);
}
assert.ok(writes <= 41);
// the handover gives the exact final state
assert.deepStrictEqual([op(0), op(1), op(2), op(3)], [0, 1, 1, 0]);
assert.strictEqual(L[1].el.style.transform, "scale(1.000)");
assert.strictEqual(L[0].el.style.transform, "scale(0.940)");

// the centre is half and half; a hand move drives it the same way (it is just the gains)
mx.apply(shares(...gains(0), true, true));
assert.ok(Math.abs(op(0) - 0.5) < 0.011 && Math.abs(op(1) - 0.5) < 0.011);
mx.apply(shares(...gains(-0.5), true, true));
assert.ok(op(0) > 0.6 && op(1) < 0.4);

// not a fixed-time fade: the same instants with the crossfader parked give the same opacity forever
mx.apply(shares(...gains(0.5), true, true));
const parked = op(1);
for (let i = 0; i < 5; i++) assert.strictEqual(mx.apply(shares(...gains(0.5), true, true)), false);   // no write when unchanged
assert.strictEqual(op(1), parked);

// one deck only: it owns the art; nothing decidable keeps what is shown
assert.deepStrictEqual(shares(0, 1, false, true), { a: 0, b: 1 });
assert.deepStrictEqual(shares(1, 0, true, false), { a: 1, b: 0 });
assert.strictEqual(shares(0, 0, true, true), null);
assert.strictEqual(mx.apply(null), false);

// ---- placeholder + late art + decode
function slot() {
  const el = { dataset: {} }, img = { src: "" }, calls = [];
  const pending = [];
  const decode = (u) => { calls.push(u); return new Promise((res, rej) => pending.push({ u, res, rej })); };
  return { s: new ArtSlot(el, img, decode), el, img, calls, pending };
}
(async () => {
  const t = slot();
  assert.strictEqual(t.el.dataset.ph, "1", "art_url null -> placeholder");
  assert.strictEqual(t.s.set(null), false, "null again: no work");
  assert.strictEqual(t.calls.length, 0);

  // the url arrives later (backfill)
  assert.strictEqual(t.s.set("/api/tracks/a/art?size=160&v=1"), true);
  assert.strictEqual(t.el.dataset.ph, "1", "placeholder until decoded");
  t.pending[0].res();
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(t.img.src, "/api/tracks/a/art?size=160&v=1");
  assert.strictEqual(t.el.dataset.ph, undefined, "art shown after decode");

  // unchanged url: no new decode
  assert.strictEqual(t.s.set("/api/tracks/a/art?size=160&v=1"), false);
  assert.strictEqual(t.calls.length, 1);

  // a new song while the old decode is pending: the stale one must not paint
  const u = slot();
  u.s.set("old"); u.s.set("new");
  u.pending[1].res(); u.pending[0].res();
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(u.img.src, "new");

  // a decode failure keeps the placeholder
  const f = slot();
  f.s.set("bad"); f.pending[0].rej(new Error("404"));
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(f.el.dataset.ph, "1");

  // back to no art: placeholder returns
  t.s.set(null);
  assert.strictEqual(t.el.dataset.ph, "1");
  console.log("album_art_check ok");
})().catch((e) => { console.error(e); process.exit(1); });
