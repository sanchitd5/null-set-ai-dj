// Node check for the riff-over-rap schedule (app/ui/static/riff-over-rap.js). Run by test_keylock.py.
const assert = require("assert");
const { schedule } = require("../../ui/static/riff-over-rap.js");
const TL = (m) => ({ break: 16, rap: 24, mashup: 24, blend: 24 + m, swap: 28 + m, end: 32 + m, mashup_bars: m });
for (const m of [16, 32]) {
  const { events, totalBars } = schedule({ gains: { a_gain: 1, b_vocals: 0.4 }, timeline: TL(m) });
  assert.strictEqual(totalBars, 32 + m);
  const lines = events.filter((e) => !e.bHold && e.bar % 4 === 0).map((e) => e.bar);
  assert.deepStrictEqual(lines, [0, 16, 24, 24 + m / 2, 24 + m, 28 + m, 32 + m]);
  // stem remix inside the mashup: a rap "hold on" in the last 4 bars of every 16
  const holds = events.filter((e) => e.bHold);
  assert.strictEqual(holds.length, m / 16);
  holds.forEach((h, k) => { assert.strictEqual(h.bar, 24 + 16 * k + 12); assert.strictEqual(h.bHold.untilBar, 24 + 16 * (k + 1)); });
  if (m >= 32) {   // A drops out under the held rap for the last 2 bars, back on the line
    const drop = events.find((e) => e.bar === 24 + m - 2);
    assert.ok(Object.values(drop.aGain).every((v) => v === 0));
    assert.ok(Object.values(events.find((e) => e.bar === 24 + m - 0.25).aGain).every((v) => v === 1));
  }
  // second half of the mashup: rap x1.5, still rap only
  const lift = events.find((e) => e.bar === 24 + m / 2);
  assert.ok(Math.abs(lift.bRamp.stems.vocals - 0.6) < 1e-9); assert.strictEqual(lift.bRamp.stems.drums, 0);
  { // measured rap lines: the hold on loops the given bar for 4 bars, A drops out where the server said
    const holds = Array.from({ length: m / 16 }, (_, k) => 24 + 16 * k + 9);
    const t2 = schedule({ gains: { a_gain: 1, b_vocals: 0.4 }, timeline: { ...TL(m), holds, dropout: m >= 32 ? 24 + 27 : undefined } }).events;
    const hs = t2.filter((e) => e.bHold);
    hs.forEach((h, k) => { assert.strictEqual(h.bHold.fromBar, holds[k]); assert.strictEqual(h.bar, holds[k] + 1); assert.strictEqual(h.bHold.untilBar, holds[k] + 5); });
    if (m >= 32) {
      assert.ok(Object.values(t2.find((e) => e.bar === 51).aGain).every((v) => v === 0));
      assert.ok(Object.values(t2.find((e) => e.bar === 52.75).aGain).every((v) => v === 1));
      assert.ok(!t2.some((e) => e.bar === 24 + m - 2 && e.aGain));      // not at the old fixed spot
    }
  }
  { // the lift is the measured one when the plan carries it (server: rap_lift), x1.5 otherwise
    const measured = schedule({ gains: { a_gain: 1, b_vocals: 0.3, rap_lift: 2 }, timeline: TL(m) }).events.find((e) => e.bar === 24 + m / 2);
    assert.ok(Math.abs(measured.bRamp.stems.vocals - 0.6) < 1e-9);
  }
  // A's drop plays once untouched: nothing of B until the mashup (user)
  const start = events.find((e) => e.b === "start");
  assert.strictEqual(start.bar, 24);
  assert.ok(events.filter((e) => e.bar < 24).every((e) => e.bStems === undefined && !e.b));
  assert.strictEqual(events.find((e) => e.bar === 16).a, "drop");
  assert.strictEqual(events.find((e) => e.bar === 24).a, "drop-half");  // only the drop's first half loops (the set)
  // then only B's rap on top, under A
  assert.deepStrictEqual(start.bStems, { drums: 0, bass: 0, vocals: 0.4, other: 0 });
  // B's backing only in the crossfade, as ramps
  assert.ok(events.filter((e) => e.bar < 24 + m && e.bStems).every((e) => !e.bStems.drums && !e.bStems.bass && !e.bStems.other));
  assert.ok(events.filter((e) => e.bar < 24 + m && e.bRamp).every((e) => !e.bRamp.stems.drums && !e.bRamp.stems.other));
  const blend = events.find((e) => e.bar === 24 + m);
  assert.ok(blend.bRamp && blend.bRamp.bars === 4 && blend.aRamp.drums === 0);
  // one bass owner, swapped on the line
  const swap = events.find((e) => e.bar === 28 + m);
  assert.strictEqual(swap.aGain.bass, 0); assert.strictEqual(swap.bStems.bass, 1);
  assert.strictEqual(events[events.length - 1].bStems, null);
  assert.ok(events.every((e) => e.bar % 4 === 0 || e.bar % 1 !== 0 || e.bar === 24 + m - 2));
}
console.log("riff schedule ok");

{ // prepare: one request per pair in flight, successes and deterministic failures cached
  const { memoPrepare } = require("../../ui/static/riff-over-rap.js");
  (async () => {
    let calls = 0, t = 0;
    const now = () => t;
    const store = new Map();
    const fail = () => { calls++; return Promise.resolve({ ok: false, reasons: ["tempo gap 41.6 %"], deterministic: true }); };
    const [x, y] = await Promise.all([memoPrepare(store, "a>b", fail, now), memoPrepare(store, "a>b", fail, now)]);
    assert.strictEqual(calls, 1); assert.strictEqual(x, y);                // in-flight shared
    await memoPrepare(store, "a>b", fail, now); assert.strictEqual(calls, 1);   // failure cached
    t = 11 * 60 * 1000; await memoPrepare(store, "a>b", fail, now); assert.strictEqual(calls, 2);   // ttl over
    const transient = () => { calls++; return Promise.resolve({ ok: false, reasons: ["HTTP 503"] }); };
    await memoPrepare(store, "c>d", transient, now); await memoPrepare(store, "c>d", transient, now);
    assert.strictEqual(calls, 4);                                            // transient: retried
    const good = () => { calls++; return Promise.resolve({ ok: true }); };
    await memoPrepare(store, "e>f", good, now); await memoPrepare(store, "e>f", good, now);
    assert.strictEqual(calls, 5);                                            // success cached
    const boom = () => { calls++; return Promise.reject(new Error("x")); };
    await assert.rejects(memoPrepare(store, "g>h", boom, now));
    await assert.rejects(memoPrepare(store, "g>h", boom, now)); assert.strictEqual(calls, 7);
    console.log("riff prepare memo ok");
  })().catch((e) => { console.error(e); process.exit(1); });
}
