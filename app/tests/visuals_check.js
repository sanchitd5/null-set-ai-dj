// Node check for the visuals cue mapping (app/ui/static/visuals.js). Run by test_keylock.py.
const assert = require("assert");
const { effectFor, env, FLASH_GAP_S, aiDriving, energyPeaks, nextPeakIdx, stepPeaks,
        peakAllowed, DROP_PEAK_GAP_S, SEEK_JUMP_S, vfxLayers, flashAllowed, safeBeat, MIN_PULSE_GAP_S,
        bassState, bassFollow, bassAlive, deckBass, bassMix, mixHex, afterPulse, DROP_BURST_S,
        DROP_AFTER_BEATS } = require("../ui/static/visuals.js");

{ // drop: burst + 2 bars of after-pulses at the cue's tempo (default 128 BPM)
  const d = effectFor({ kind: "drop", deck: "b" });
  assert.strictEqual(d.type, "drop"); assert.strictEqual(d.deck, "b");
  assert.strictEqual(d.beat, 60 / 128);
  assert.ok(Math.abs(d.dur - (60 / 128) * (DROP_AFTER_BEATS + 1)) < 1e-9);
  assert.ok(d.dur >= DROP_BURST_S && d.dur <= 6);
  assert.strictEqual(effectFor({ kind: "drop", bar: 100 }).dur, 6);               // capped
  assert.strictEqual(effectFor({ kind: "drop", deck: "a" }, true).dur, DROP_BURST_S); // still: no after-pulses
}
assert.strictEqual(effectFor({ kind: "transition", deck: "a", bar: 1.875 }).type, "sweep");
assert.strictEqual(effectFor({ kind: "transition", bar: 100 }).dur, 3.5);        // capped
assert.strictEqual(effectFor({ kind: "line", deck: "x" }).deck, null);         // unknown deck -> AI colour
assert.strictEqual(effectFor({ kind: "nope" }), null);
assert.strictEqual(effectFor(null), null);
const r = effectFor({ kind: "drop", deck: "a" }, true);
assert.ok(r.still && r.dur >= 1.2);                                              // reduced motion: still glow

assert.strictEqual(env(-0.1, 1), 0);
assert.strictEqual(env(1, 1), 0);
assert.strictEqual(env(0.06, 1), 1);
assert.ok(env(0.5, 1) > 0 && env(0.5, 1) < 1);
assert.ok(FLASH_GAP_S >= 1 / 3);                                                 // <= 3 flashes/s
assert.deepStrictEqual(effectFor({ kind: "peak", deck: "a" }), { type: "peak", deck: "a", dur: 3 });

// ---- AI gate: only a live autopilot counts
assert.strictEqual(aiDriving({ active: true }), true);
assert.strictEqual(aiDriving({ active: false }), false);
assert.strictEqual(aiDriving({ get active() { return true; } }), true);          // autopilotState getter
assert.strictEqual(aiDriving({ active: "yes" }), false);
assert.strictEqual(aiDriving(undefined), false);                                 // autopilot.js not loaded
assert.strictEqual(aiDriving(null), false);

// ---- energy high points. 120 BPM: bar = 2 s, 32 bars = 64 s, 8-bar rise window = 16 s
const BPM = 120;
const song = (n, fn) => { const c = [], t = []; for (let i = 0; i < n; i++) { t.push(i); c.push(fn(i)); } return [c, t]; };
{ // two drops 100 s apart, low elsewhere: both found
  const [c, t] = song(300, (i) => (i === 60 ? 1 : i === 160 ? 0.95 : i === 59 || i === 159 ? 0.5 : 0.2));
  assert.deepStrictEqual(energyPeaks(c, t, BPM), [60, 160]);
}
{ // spacing: a weaker high point 30 s after a stronger one (< 64 s) is dropped
  const [c, t] = song(300, (i) => (i === 60 ? 1 : i === 90 ? 0.95 : 0.2));
  assert.deepStrictEqual(energyPeaks(c, t, BPM), [60]);
  // the stronger one wins even when it comes second
  const [c2, t2] = song(300, (i) => (i === 60 ? 0.95 : i === 90 ? 1 : 0.2));
  assert.deepStrictEqual(energyPeaks(c2, t2, BPM), [90]);
}
{ // top percentile: a local max in the middle of the energy range is not a high point
  const [c, t] = song(200, (i) => (i < 100 ? 0.2 : 0.9) + (i === 50 ? 0.3 : 0));
  assert.ok(!energyPeaks(c, t, BPM).includes(50));
}
{ // local max: a sample still climbing is not the peak, the top of the climb is
  const [c, t] = song(300, (i) => (i >= 95 && i <= 100 ? 0.5 + (i - 95) * 0.1 : 0.2));
  assert.deepStrictEqual(energyPeaks(c, t, BPM), [100]);
}
{ // plateau counts once, at its first sample
  const [c, t] = song(300, (i) => (i >= 100 && i < 110 ? 1 : 0.2));
  assert.deepStrictEqual(energyPeaks(c, t, BPM), [100]);
}
{ // rising edge: a louder wiggle inside a loud section (no climb in the last 8 bars) is not
  // a high point, even though it is stronger; the climb into the section is kept
  const [c, t] = song(400, (i) => (i === 130 ? 0.99 : i >= 100 && i < 140 ? 0.95 : 0.2));
  assert.deepStrictEqual(energyPeaks(c, t, BPM), [100]);
}
{ // bad input never throws
  assert.deepStrictEqual(energyPeaks(null, null, BPM), []);
  assert.deepStrictEqual(energyPeaks([1, 2], [0, 1], BPM), []);
  assert.deepStrictEqual(energyPeaks([0.5, 0.5, 0.5, 0.5], [0, 1, 2, 3], BPM), []);          // flat
  assert.deepStrictEqual(energyPeaks([0, 1, 0, 0], [3, 2, 1, 0], BPM), []);                  // unsorted times
  const [c, t] = song(300, (i) => (i === 60 ? 1 : 0.2));
  c[10] = NaN;
  assert.deepStrictEqual(energyPeaks(c, t, 0), [60]);                                        // bpm fallback
}

// ---- pointer: fires once per crossing, never on seek / loop / load
assert.strictEqual(nextPeakIdx([10, 20, 30], 5), 0);
assert.strictEqual(nextPeakIdx([10, 20, 30], 20), 2);
assert.strictEqual(nextPeakIdx([10, 20, 30], 99), 3);
{
  const tr = { peaks: [10, 20], idx: 0, prev: NaN };
  assert.strictEqual(stepPeaks(tr, 9.9), null);          // first step only finds the pointer
  assert.strictEqual(stepPeaks(tr, 10.01), 10);          // crossed
  assert.strictEqual(stepPeaks(tr, 10.05), null);        // once
  assert.strictEqual(stepPeaks(tr, 25), null);           // jump over 20 (seek): no fire
  assert.strictEqual(tr.idx, 2);
  assert.strictEqual(stepPeaks(tr, 19.9), null);         // back (loop / seek): pointer re-found
  assert.strictEqual(tr.idx, 1);
  assert.strictEqual(stepPeaks(tr, 20 + SEEK_JUMP_S / 2), 20);
  assert.strictEqual(stepPeaks(null, 1), null);
  assert.strictEqual(stepPeaks({ peaks: [5], idx: 0, prev: 4 }, NaN), null);
}

// ---- drop / peak de-dup: nothing within 4 s of a drop, before or after
assert.ok(DROP_PEAK_GAP_S >= 3 && DROP_PEAK_GAP_S <= 5);
assert.strictEqual(peakAllowed(100, []), true);
assert.strictEqual(peakAllowed(100, undefined), true);
assert.strictEqual(peakAllowed(100, [97]), false);        // drop 3 s ago
assert.strictEqual(peakAllowed(100, [103]), false);       // drop booked 3 s ahead
assert.strictEqual(peakAllowed(100, [95.9]), true);
assert.strictEqual(peakAllowed(100, [104]), true);

// ---- layer gate: bass band needs no AI, every other effect does; toggle off = nothing
{
  const on = { enabled: true, hidden: false, playing: true, bassAlive: true };
  assert.deepStrictEqual(vfxLayers({ ...on, autopilot: { active: false } }), { bass: true, ai: false });  // hand mixing
  assert.deepStrictEqual(vfxLayers({ ...on, autopilot: undefined }), { bass: true, ai: false });
  assert.deepStrictEqual(vfxLayers({ ...on, autopilot: { active: true } }), { bass: true, ai: true });
  assert.deepStrictEqual(vfxLayers({ ...on, enabled: false, autopilot: { active: true } }), { bass: false, ai: false });
  assert.deepStrictEqual(vfxLayers({ ...on, hidden: true, autopilot: { active: true } }), { bass: false, ai: false });
  // stopped: the band keeps drawing only while it fades out
  assert.strictEqual(vfxLayers({ ...on, playing: false, bassAlive: true }).bass, true);
  assert.strictEqual(vfxLayers({ ...on, playing: false, bassAlive: false }).bass, false);
  assert.deepStrictEqual(vfxLayers(null), { bass: false, ai: false });
}

// ---- photosensitivity: one flash per second, pulses / kicks under 3 per second
assert.strictEqual(flashAllowed(10, -Infinity), true);
assert.strictEqual(flashAllowed(10.5, 10), false);
assert.strictEqual(flashAllowed(11, 10), true);
assert.strictEqual(flashAllowed(NaN, 0), false);
{ // a drop every 0.25 s for 10 s: at most one flash per second
  let last = -Infinity, n = 0;
  for (let t = 0; t < 10; t += 0.25) if (flashAllowed(t, last)) { last = t; n++; }
  assert.ok(n <= 10, `flashes ${n}`);
}
assert.ok(MIN_PULSE_GAP_S > 1 / 3);
assert.ok(safeBeat(60 / 200) >= MIN_PULSE_GAP_S);                 // 200 BPM -> half-time
assert.strictEqual(safeBeat(60 / 128), 60 / 128);
assert.strictEqual(safeBeat(NaN), 60 / 128);
assert.strictEqual(safeBeat(-1), 60 / 128);

// ---- after-drop pulses: on beats 1..8, decaying, none on the downbeat or after 2 bars
{
  const b = 0.5;
  assert.strictEqual(afterPulse(0.1, b), 0);                           // downbeat is the burst's
  assert.ok(Math.abs(afterPulse(b, b) - 1) < 1e-9);                    // beat 1: full
  assert.ok(afterPulse(b + 0.2, b) < afterPulse(b, b));                // decays inside the beat
  assert.ok(afterPulse(4 * b, b) < afterPulse(b, b));                  // fades over the bars
  assert.ok(afterPulse(8 * b, b) > 0);
  assert.strictEqual(afterPulse(9 * b, b), 0);                         // after 2 bars: over
  assert.strictEqual(afterPulse(-1, b), 0);
  // pulse onsets never faster than 3 per second, even at 200 BPM
  let onsets = 0, prev = 0;
  for (let t = 0; t < 3; t += 0.001) { const p = afterPulse(t, 60 / 200); if (p > prev + 0.5) onsets++; prev = p; }
  assert.ok(onsets <= 9, `onsets ${onsets}`);
}

// ---- bass envelope follower: fast attack, slow release, kick hit, idle decay to zero
{
  const st = bassState();
  const run = (low, secs, t0, fps = 60) => { let t = t0; for (let i = 0; i < secs * fps; i++) { t += 1 / fps; bassFollow(st, low, 1 / fps, t); } return t; };
  let t = run(0.8, 0.1, 0);                                             // 100 ms of bass
  assert.ok(st.level > 0.7, `attack ${st.level}`);                      // attack: up in ~0.1 s
  const top = st.level;
  t = run(0, 0.1, t);
  assert.ok(st.level > 0.5 * top, `release ${st.level}`);               // release slower than attack
  assert.ok(st.level < top);
  t = run(0, 5, t);
  assert.strictEqual(st.level, 0);                                      // idle: exactly zero
  assert.strictEqual(st.hit, 0);
  assert.strictEqual(bassAlive(st), false);
}
{ // kick: a sharp rise into a loud low band hits; a slow climb or a quiet bump does not
  const st = bassState();
  bassFollow(st, 0.2, 1 / 60, 1);
  bassFollow(st, 0.9, 1 / 60, 1.02);
  assert.ok(st.hit > 0.8, `hit ${st.hit}`);
  const h = st.hit;
  bassFollow(st, 0.9, 0.1, 1.12);
  assert.ok(st.hit < h);                                                // decays fast
  bassFollow(st, 0.2, 1 / 60, 1.14);
  bassFollow(st, 0.95, 1 / 60, 1.16);                                   // 0.14 s later: capped
  assert.strictEqual(st.lastKick, 1.02);
  bassFollow(st, 0.2, 1 / 60, 1.5);
  bassFollow(st, 0.95, 1 / 60, 1.52);
  assert.strictEqual(st.lastKick, 1.52);                                // next beat: hits
  const q = bassState();
  bassFollow(q, 0.1, 1 / 60, 1); bassFollow(q, 0.3, 1 / 60, 1.02);     // quiet bump
  assert.strictEqual(q.hit, 0);
  const s = bassState();
  for (let i = 0; i < 60; i++) bassFollow(s, i / 60, 1 / 60, i / 60);   // slow climb
  assert.strictEqual(s.hit, 0);
}
{ // reduced motion: no hits, slow both ways
  const st = bassState();
  bassFollow(st, 0.2, 1 / 60, 1, true); bassFollow(st, 0.95, 1 / 60, 1.02, true);
  assert.strictEqual(st.hit, 0);
  assert.ok(st.level < 0.1);
}
{ // bad input never throws, never goes NaN
  const st = bassState();
  bassFollow(st, NaN, NaN, NaN); bassFollow(st, 5, 1, 1); bassFollow(st, -3, -1, 2);
  assert.ok(Number.isFinite(st.level) && st.level >= 0 && st.level <= 1);
  assert.strictEqual(bassFollow(null, 1, 1, 1), null);
}

// ---- who carries the low end, and the colour mix for two decks
{
  const p = (v) => ({ value: v });
  const deck = (o) => ({ playing: true, crossfaderGain: { gain: p(1) }, volumeGain: { gain: p(1) },
                         lowFilter: { gain: p(0) }, stemState: null, ...o });
  assert.strictEqual(deckBass(deck({})), 1);
  assert.strictEqual(deckBass(deck({ playing: false })), 0);
  assert.strictEqual(deckBass(null), 0);
  assert.strictEqual(deckBass(deck({ crossfaderGain: { gain: p(0) } })), 0);
  assert.ok(deckBass(deck({ lowFilter: { gain: p(-40) } })) < 0.02);           // low EQ killed
  assert.strictEqual(deckBass(deck({ stemState: { bass: 0, drums: 0, vocals: 1, other: 1 } })), 0);
  assert.strictEqual(deckBass(deck({ stemState: { bass: 0, drums: 1, vocals: 1, other: 1 } })), 1); // kick still there
  assert.strictEqual(deckBass({ playing: true }), 1);                          // no nodes: assume full

  assert.strictEqual(bassMix(1, 0), 0);
  assert.strictEqual(bassMix(0, 1), 1);
  assert.strictEqual(bassMix(1, 1), 0.5);
  assert.strictEqual(bassMix(0, 0), null);                                     // nobody: master colour
  assert.strictEqual(bassMix(NaN, undefined), null);
  assert.strictEqual(bassMix(1, 0.3) * 16 % 1, 0);                             // 1/16 steps
  assert.strictEqual(mixHex("#00ff66", "#ff2bd6", 0), "#00ff66");
  assert.strictEqual(mixHex("#00ff66", "#ff2bd6", 1), "#ff2bd6");
  assert.strictEqual(mixHex("#000000", "#ffffff", 0.5), "#808080");
  assert.strictEqual(mixHex("#00ff66", "#ff2bd6", 0.5), "#80959e");
  assert.strictEqual(mixHex("bad", "#ff2bd6", 0.5), "#ff2bd6");
  assert.strictEqual(mixHex("bad", "worse", 0.5), "#00e5ff");
  assert.strictEqual(mixHex("#000000", "#ffffff", 7), "#ffffff");              // clamped
}

// ---- ANYMA look (a VFX style) -------------------------------------------------
{
  const V = require("../ui/static/visuals.js"), A = require("../ui/static/anyma-show.js");
  const TD = require("../ui/static/toggle-drawer.js");
  // style selection + persistence: the drawer's own store and key, default classic
  assert.strictEqual(V.STORE_KEY, TD.STORE_KEY, "the style lives in the drawer's localStorage blob");
  assert.strictEqual(V.styleFromStore(null), "classic");
  assert.strictEqual(V.styleFromStore("{bad json"), "classic");
  assert.strictEqual(V.styleFromStore(JSON.stringify({ "ap-show-toggle": true })), "classic");
  assert.strictEqual(V.styleFromStore(JSON.stringify(TD.withSaved({}, V.STYLE_ID, true))), "anyma");
  assert.strictEqual(V.styleFromStore(JSON.stringify(TD.withSaved({ [V.STYLE_ID]: true }, V.STYLE_ID, false))), "classic");
  assert.strictEqual(V.styleFromStore(JSON.stringify({ [V.STYLE_ID]: "yes" })), "classic", "booleans only");
  // the toggle sits in the VISUALS drawer group, off by default; SHOW AUTO on by default
  const fs = require("fs"), path = require("path");
  const html = fs.readFileSync(path.join(__dirname, "../ui/static/index.html"), "utf8");
  const lab = (id) => { const m = new RegExp(`<label[^>]*>\\s*<input[^>]*id="${id}"[^>]*>`).exec(html); return m ? m[0] : ""; };
  assert.ok(/data-group="VISUALS"/.test(lab(V.STYLE_ID)) && !/checked/.test(lab(V.STYLE_ID)), "ANYMA LOOK: VISUALS, off by default");
  assert.ok(/data-group="VISUALS"/.test(lab("ap-show-auto")) && /checked/.test(lab("ap-show-auto")), "SHOW AUTO: VISUALS, on by default");
  assert.strictEqual(TD.classify(V.STYLE_ID, "VISUALS").group, "VISUALS");
  // trigger mapping reuse: the SHOW core itself, not a fork
  const core = V.anymaCore();
  assert.strictEqual(core.eventTrigger, A.eventTrigger);
  assert.strictEqual(core.stepDirector, A.stepDirector);
  assert.strictEqual(core.musicState, A.musicState);
  const src = fs.readFileSync(path.join(__dirname, "../ui/static/visuals.js"), "utf8");
  assert.ok(!/function (stepDirector|eventTrigger|musicState|pickScene)\b/.test(src), "no forked director in visuals.js");
  assert.ok(/A\.eventTrigger\(/.test(src) && /A\.stepDirector\(/.test(src));
  assert.strictEqual(V.anymaMotif("head"), "eyes");
  assert.strictEqual(V.anymaMotif("figure"), "rings");
  assert.strictEqual(V.anymaMotif("corridor"), "frames");
  assert.strictEqual(V.anymaMotif("monolith"), "scan");
  assert.strictEqual(V.anymaMotif("nope"), "scan");
  // flash cap: two drops 0.5 s apart through the shared director -> one flash; reduced motion -> none
  const ms = A.musicStateNew();
  const flashes = (reduced) => {
    const d = A.createDirector(1);
    let n = 0, prev = 0;
    for (let t = 0; t < 3; t += 1 / 60) {
      if (Math.abs(t - 1) < 1 / 120 || Math.abs(t - 1.5) < 1 / 120) A.queueEvent(d, { type: "supermove", at: t });
      A.stepDirector(d, ms, t, 1 / 60, reduced);
      const a = V.anymaFlash(d.flash, reduced);
      assert.ok(a <= 0.5, "tinted overlay capped at 0.5");
      if (a > prev + 0.2) n++;
      prev = a;
    }
    return n;
  };
  assert.strictEqual(flashes(false), 1, "<= 1 flash per second");
  assert.strictEqual(flashes(true), 0, "reduced motion: no flash");
  assert.strictEqual(V.anymaFlash(1, false), 0.5);
  assert.strictEqual(V.anymaFlash(NaN, false), 0);
  for (const c of Object.values(V.ANYMA_COL)) assert.ok(!/^#f{6}$/i.test(c), "never pure white");
  // SHOW never hides the VFX layer (bass band + classic effects); the manual STAGE only dims it
  const on = { enabled: true, hidden: false, playing: true, bassAlive: true, autopilot: { active: true } };
  for (const show of ["off", "elements", "full", "embed", undefined]) {
    assert.deepStrictEqual(V.vfxLayers({ ...on, show }), { bass: true, ai: true }, `SHOW ${show}`);
  }
  assert.strictEqual(V.showYields("full"), true); assert.strictEqual(V.showYields("off"), false);
  assert.strictEqual(V.showYields("elements"), false, "SHOW in the elements is no stage");
  // SHOW in the elements: the page-wide Anyma motifs stand down (the look lives in the elements)
  assert.strictEqual(V.anymaYields("elements"), true); assert.strictEqual(V.anymaYields("off"), false);
  // the gates hold in ANYMA too: the look only draws inside the ai layer
  assert.ok(/if \(L\.ai && style === "anyma" && !anymaYields\(lay\.show\)\) anymaFrame\(/.test(src));
  // ANYMA adds on top: the classic spawn gate no longer skips the anyma style
  assert.ok(!/style === "anyma"\) return;/.test(src), "classic effects fire in ANYMA LOOK too");
  // one loop: visuals.js has exactly one requestAnimationFrame call site for its frame,
  // and the SHOW's element ticks ride it (anyma-show.js adds a tick, no rAF of its own there)
  assert.strictEqual((src.match(/requestAnimationFrame\(frame\)/g) || []).length, 1);
  assert.ok(/for \(let i = 0; i < ticks\.length; i\+\+\) if \(ticks\[i\]\(now, dt\)\) keep = true;/.test(src));
  assert.ok(/addTick\(fn\)/.test(src) && /removeTick\(fn\)/.test(src));
  // NULL-BOT: the ANYMA tint is CSS only (mascot.js untouched by the look)
  const nb = fs.readFileSync(path.join(__dirname, "../ui/static/null-bot.css"), "utf8");
  assert.ok(/\.anyma-look \.nul-super \{ --sm-glow: #3fd8ff !important; \}/.test(nb));
  const mascot = fs.readFileSync(path.join(__dirname, "../ui/static/mascot.js"), "utf8");
  assert.ok(!/anyma-look|djAiToggles/i.test(mascot));   // the LOOK is CSS only (mascot.js reads the SHOW, never the look)
}
console.log("visuals ok");
