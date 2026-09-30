// Node check for autopilotCore.awaitJob (app/ui/static/autopilot.js): polling a
// background job (app/ui/services/bg_jobs.py) inside the caller's wait budget.
// Exits non-zero on the first failed assertion.
const assert = require("assert");
const { awaitJob } = require("../../ui/static/autopilot.js");

function fakeClock() {
  let t = 0;
  return { now: () => t, sleep: async (ms) => { t += ms; } };
}

(async () => {
  // a body without "pending" IS the result: no polling
  let polls = 0;
  const direct = await awaitJob({ ok: true, plan: 1 }, async () => { polls++; return null; });
  assert.deepStrictEqual(direct, { ok: true, plan: 1 });
  assert.strictEqual(polls, 0);

  // pending, then done on the third poll
  let c = fakeClock(), n = 0;
  const done = await awaitJob({ status: "pending", job: "j1" }, async (job) => {
    assert.strictEqual(job, "j1");
    n++;
    return n < 3 ? { status: "pending", job: "j1" } : { ok: true, plan: "p" };
  }, { everyMs: 1000, budgetMs: 35000, now: c.now, sleep: c.sleep });
  assert.deepStrictEqual(done, { ok: true, plan: "p" });
  assert.strictEqual(n, 3);

  // never finishes: gives up at the budget with null (the caller carries on as before)
  c = fakeClock(); n = 0;
  const slow = await awaitJob({ status: "pending", job: "j2" }, async () => { n++; return { status: "pending", job: "j2" }; },
    { everyMs: 1000, budgetMs: 5000, now: c.now, sleep: c.sleep });
  assert.strictEqual(slow, null);
  assert.ok(n <= 5, `polled ${n} times inside a 5 s budget`);
  assert.ok(c.now() <= 5000);

  // the booking changed (alive false): stop without another poll
  c = fakeClock(); n = 0;
  let alive = true;
  const gone = await awaitJob({ status: "pending", job: "j3" }, async () => { n++; alive = false; return { status: "pending", job: "j3" }; },
    { everyMs: 1000, budgetMs: 35000, now: c.now, sleep: c.sleep, alive: () => alive });
  assert.strictEqual(gone, null);
  assert.strictEqual(n, 1);

  // a failed poll (network error / 404 expired job) answers null, never throws
  c = fakeClock();
  assert.strictEqual(await awaitJob({ status: "pending", job: "j4" }, async () => { throw new Error("down"); },
    { now: c.now, sleep: c.sleep }), null);
  c = fakeClock();
  assert.strictEqual(await awaitJob({ status: "pending", job: "j5" }, async () => null,
    { now: c.now, sleep: c.sleep }), null);
  assert.strictEqual(await awaitJob(null, async () => ({ ok: true })), null);

  console.log("await_job_check: ok");
})().catch((e) => { console.error(e); process.exit(1); });
