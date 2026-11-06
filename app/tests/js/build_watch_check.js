// Node check for the reload prompt (app/ui/static/build-watch.js). Run by test_build_watch.py.
const assert = require("assert");
const bw = require("../../ui/static/build-watch.js");

assert.strictEqual(bw.isNewer("abc", "def"), true);
assert.strictEqual(bw.isNewer("abc", "abc"), false);
assert.strictEqual(bw.isNewer(null, "abc"), false);
assert.strictEqual(bw.isNewer("abc", ""), false);
assert.strictEqual(bw.isNewer("abc", undefined), false);
assert.strictEqual(bw.TEXT, "New version - reload");
assert.strictEqual(bw.POLL_MS, 60000);

assert.strictEqual(bw.setPlaying({}), false);
assert.strictEqual(bw.setPlaying({ decks: { a: { playing: false }, b: null } }), false);
assert.strictEqual(bw.setPlaying({ decks: { a: { playing: true } } }), true);
assert.strictEqual(bw.setPlaying({ autopilotState: { active: true }, decks: {} }), true);

// a tiny DOM: enough for the banner
function fakeDoc() {
  const listeners = {};
  const mk = (tag) => {
    const n = { tag, children: [], attrs: {}, hidden: false, on: {},
      setAttribute(k, v) { this.attrs[k] = v; },
      addEventListener(t, f) { this.on[t] = f; },
      append(...c) { this.children.push(...c); } };
    return n;
  };
  const body = mk("body");
  return { body, visibilityState: "visible", createElement: mk,
    addEventListener(t, f) { listeners[t] = f; }, fire(t) { listeners[t] && listeners[t](); } };
}

(async () => {
  const builds = ["aaa", "aaa", null, "bbb", "bbb", "ccc"];
  let i = 0, reloads = 0, asked = 0, answer = false, playing = false, ticks = 0, tickMs = 0;
  const doc = fakeDoc();
  const win = {
    document: doc,
    location: { reload() { reloads++; } },
    confirm(msg) { asked++; assert.strictEqual(msg, bw.ASK); return answer; },
    setInterval(f, ms) { ticks++; tickMs = ms; },
  };
  const fetch = async (url, init) => {
    assert.strictEqual(url, "/api/build");
    assert.strictEqual(init.cache, "no-store");
    const b = builds[i++];
    if (b === null) throw new Error("server restarting");
    return { ok: true, json: async () => ({ build: b }) };
  };
  const w = bw.createBuildWatch({ win, doc, fetch, isPlaying: () => playing });
  assert.strictEqual(ticks, 1);
  assert.strictEqual(tickMs, 60000);
  await new Promise((r) => setTimeout(r, 0));              // the first check at start
  assert.strictEqual(w.known, "aaa");
  assert.strictEqual(w.stale, false);
  assert.strictEqual(doc.body.children.length, 0);

  assert.strictEqual(await w.check(), false);              // same build
  assert.strictEqual(await w.check(), false);              // network error: no prompt, no change
  doc.fire("visibilitychange");                            // back to the tab: checks at once
  await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(w.stale, true);
  assert.strictEqual(w.latest, "bbb");
  assert.strictEqual(doc.body.children.length, 1);
  const banner = doc.body.children[0];
  assert.strictEqual(banner.id, "build-banner");
  assert.strictEqual(banner.children[0].textContent, "New version - reload");
  assert.strictEqual(reloads, 0);                          // never by itself

  await w.check();                                         // still bbb: one banner
  assert.strictEqual(doc.body.children.length, 1);

  // during a playing set RELOAD asks first; "no" keeps the set
  playing = true;
  banner.children[0].on.click();
  assert.strictEqual(asked, 1);
  assert.strictEqual(reloads, 0);
  answer = true;
  assert.strictEqual(w.reload(), true);
  assert.strictEqual(reloads, 1);
  playing = false;
  assert.strictEqual(w.reload(), true);                    // nothing playing: no question
  assert.strictEqual(asked, 2);

  // dismissed, then an even newer build shows it again
  banner.children[1].on.click();
  assert.strictEqual(banner.hidden, true);
  await w.check();
  assert.strictEqual(w.latest, "ccc");
  assert.strictEqual(banner.hidden, false);
  assert.strictEqual(doc.body.children.length, 1);

  // a 401 (signed out) or junk answer never prompts
  const w2 = bw.createBuildWatch({ win, doc: fakeDoc(), autostart: false,
    fetch: async () => ({ ok: false, json: async () => ({}) }) });
  assert.strictEqual(await w2.check(), false);
  assert.strictEqual(w2.known, null);
  const w3 = bw.createBuildWatch({ win, doc: fakeDoc(), autostart: false,
    fetch: async () => ({ ok: true, json: async () => ({ build: 42 }) }) });
  assert.strictEqual(await w3.check(), false);
  assert.strictEqual(w3.known, null);
  console.log("build_watch_check ok");
})().catch((e) => { console.error(e); process.exit(1); });
