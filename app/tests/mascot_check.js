// Node check for the mascot mood logic (app/ui/static/mascot.js). Run by test_keylock.py.
const assert = require("assert");
const { mood, LABEL } = require("../ui/static/mascot.js");
const base = { now: 10, hypeUntil: 0, grooveUntil: 0, holdLoop: false, llm: 0, omni: 0 };
assert.strictEqual(mood(base), "idle");
assert.strictEqual(mood({ ...base, llm: 1 }), "think");
assert.strictEqual(mood({ ...base, llm: 1, omni: 1 }), "listen");
assert.strictEqual(mood({ ...base, omni: 1, grooveUntil: 12 }), "groove");
assert.strictEqual(mood({ ...base, grooveUntil: 12, holdLoop: true }), "sweat");
assert.strictEqual(mood({ ...base, holdLoop: true, hypeUntil: 11 }), "hype");
assert.strictEqual(mood({ ...base, grooveUntil: 9 }), "idle");            // expired
assert.ok(Object.keys(LABEL).length === 6);
console.log("mascot ok");
