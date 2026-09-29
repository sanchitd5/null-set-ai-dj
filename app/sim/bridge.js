// Node side of the virtual set: a JSON-lines RPC over stdin / stdout that drives the REAL
// console modules (autopilot.js core, tempo-rule.js, dj-mind.js core, stem-moves.js core).
// No logic is forked here: every answer is a call into those files, this file only marshals
// arguments and, for the stem moves, feeds them the measured stem energy the way
// stem-moves.js does in the browser (stemBlend / stemBridge, minus the audio graph).
//
//   request  {"id": n, "fn": "autopilot.decideRecipe", "args": [...]}
//   response {"id": n, "ok": true, "result": ...}  |  {"id": n, "ok": false, "error": "..."}
"use strict";
const path = require("path");
const readline = require("readline");

const STATIC = path.join(__dirname, "..", "ui", "static");
const tempoRule = require(path.join(STATIC, "tempo-rule.js"));
const autopilot = require(path.join(STATIC, "autopilot.js"));
const djMind = require(path.join(STATIC, "dj-mind.js"));
const stem = require(path.join(STATIC, "stem-moves.js"));

const STEMS = ["drums", "bass", "vocals", "other"];

// ---- measured stem energy -> the bins stem-moves.js expects -------------------------
// curve: {hop, rms: {stem: [..]}, aud: {stem: [..]}}, one value per `hop` seconds.
// Bin i of `barS` seconds starting at `t0` = mean of the hop values it covers.
function bins(arr, t0, barS, hop, n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const a = Math.max(0, Math.floor((t0 + i * barS) / hop));
    const b = Math.max(a + 1, Math.floor((t0 + (i + 1) * barS) / hop));
    const seg = arr.slice(a, Math.min(arr.length, b));
    out.push(seg.length ? seg.reduce((x, y) => x + y, 0) / seg.length : 0);
  }
  return out;
}
function stemBins(curve, band, t0, barS, n) {
  if (!curve || !curve[band]) return null;
  const o = {};
  for (const s of STEMS) {
    if (!curve[band][s]) return null;
    o[s] = bins(curve[band][s], t0, barS, curve.hop || 1, n);
  }
  return o;
}
const meanOver = (e, a, b) => {
  if (!e) return null;
  const m = {};
  for (const n of STEMS) { const v = e[n].slice(a, Math.max(a + 1, b)); m[n] = v.reduce((x, y) => x + y, 0) / (v.length || 1); }
  return m;
};
const share = (regions, a, b) => stem.vocalShare(regions, a, b);

// ---- stem blend, as stemMoves.stemBlend decides it (no audio graph) ---------------------
// o: {kind, bars, barS, dir, pA, pB, barSongA, barSongB, aVocals, bVocals, keyScore,
//     aCurve, bCurve}   pA / pB: song seconds where the blend starts on each deck
function simStemBlend(o) {
  const aSings = share(o.aVocals, o.pA, o.pA + o.bars * o.barS) >= 0.3;
  const bSings = share(o.bVocals, o.pB, o.pB + o.bars * o.barS) >= 0.3;
  const keyClash = o.keyScore != null && o.keyScore < 0.8;
  let eOut = stemBins(o.aCurve, "rms", o.pA, o.barSongA, o.bars + 1);
  let eIn = stemBins(o.bCurve, "rms", o.pB, o.barSongB, o.bars + 1);
  if (!eOut || !eIn) eOut = eIn = null;
  const P = stem.introBars(o.bars);
  const fader = autopilot.stemBlendFader(o.kind, o.bars, o.dir || 1);
  const fit = stem.fitStemBlend(o.kind, o.bars, {
    aSings, bSings, keyClash, eOut, eIn, fader, dir: o.dir || 1,
    aSingsIntro: share(o.aVocals, o.pA, o.pA + P * o.barSongA) >= 0.3,
    introEnergy: meanOver(eIn, 0, P),
  });
  return { refused: !!fit.refused, intro: fit.intro, fix: fit.fix, minDb: fit.check.minDb, reason: fit.check.reason || "",
    measured: !!eOut, aSings, bSings, keyClash,
    introAudible: fit.intro ? stem.introAudible(fit.intro, meanOver(eIn, 0, P)) : true,
    introRms: fit.intro && eIn ? meanOver(eIn, 0, P)[fit.intro] : null };
}

// ---- stem bridge, as stemMoves.stemBridge decides it ------------------------------------
// o: {barA, barB, keyScore, pA, bFrom, aVocals, aCurve, bCurve}
function simStemBridge(o) {
  const keyClash = o.keyScore != null && o.keyScore < 0.8;
  const aSings = share(o.aVocals, o.pA, o.pA + 4 * o.barA) >= 0.3;
  const span = 8 * Math.max(o.barA, o.barB) + 4 * o.barA;
  const nA = Math.ceil(span / o.barA) + 1, nB = Math.ceil(span / o.barB) + 1;
  const eA = stemBins(o.aCurve, "rms", o.pA, o.barA, nA), eB = stemBins(o.bCurve, "rms", o.bFrom, o.barB, nB);
  const aA = stemBins(o.aCurve, "aud", o.pA, o.barA, nA), aB = stemBins(o.bCurve, "aud", o.bFrom, o.barB, nB);
  const both = eA && eB && aA && aB;
  const outFn = (E) => (both ? (n, t) => E[n][Math.max(0, Math.min(E[n].length - 1, Math.floor(t / o.barA)))] : null);
  const inFn = (E, bStart) => (both ? (n, t) => E[n][Math.max(0, Math.min(E[n].length - 1, Math.floor((t - bStart) / o.barB)))] : null);
  const intro0 = keyClash ? "drums" : stem.pickIntro({ keyClash, aSings: true, bSings: false, energy: meanOver(eB, 0, 2) });
  let plan = null, check = null;
  for (const fix of [{}, { introLevel: 1 }]) {
    const p = stem.stemBridgePlan(o.barA, o.barB, keyClash, aSings, Object.assign({ intro: intro0 === "vocals" ? "other" : intro0 }, fix));
    check = stem.gates(p, p.fader, p.total, { eOut: outFn(eA), eIn: inFn(eB, p.bStart), aOut: outFn(aA), aIn: inFn(aB, p.bStart) }, 1,
      { inStart: p.bStart, step: o.barA / 16, win: o.barA / 4 });
    if (check.ok) { plan = p; break; }
  }
  return { refused: !plan, intro: plan ? plan.intro : intro0, total: plan ? plan.total : null,
    minDb: check.minDb, reason: check.reason || "", measured: !!both, aSings, keyClash,
    silentRun: check.run || 0 };
}

// ---- how long a booked move keeps both records in the mix, seconds ----------------------
// Mirrors executeTransition's bar counts: stem blends via autopilotCore.stemBlendBars, the
// EQ kinds are 8 bars (bass / echo / filter / loop), 16 (blend / default), double 8.5.
function moveSeconds(kind, barS, aLeftBars, scale) {
  if (autopilot.stemBlendBars) {
    if (["blend", "bass", "filter", "loop", "double"].includes(kind)) {
      return autopilot.stemBlendBars(kind, barS, aLeftBars, scale) * barS;
    }
  }
  return 8 * barS * scale;
}

const FNS = {
  "autopilot.recipeKind": (r) => autopilot.recipeKind(r),
  "autopilot.energyStepOk": (cur, nxt, o) => autopilot.energyStepOk(cur, nxt, o),
  "autopilot.tempoLockableAt": (a, b, lim) => autopilot.tempoLockableAt(a, b, lim),
  "autopilot.decideRecipe": (o) => {
    const d = autopilot.decideRecipe(Object.assign({}, o, { mashupFits: () => !!o.mashupFits }), tempoRule);
    return d;
  },
  "autopilot.learnedRecipe": (pick, o) => autopilot.learnedRecipe(pick, o),
  "autopilot.playWindowFor": (o) => autopilot.playWindowFor(o),
  "autopilot.exitBounds": (o) => autopilot.exitBounds(Object.assign({}, o, { trackDur: o.trackDur == null ? Infinity : o.trackDur })),
  "autopilot.exitPick": (o) => autopilot.exitPick(o),
  "autopilot.exitTiming": (o) => autopilot.exitTiming(o),
  "autopilot.exitHighPush": (o) => autopilot.exitHighPush(o),
  "autopilot.stemBlendBars": (...a) => autopilot.stemBlendBars(...a),
  "autopilot.hybridWindowKey": (e) => autopilot.hybridWindowKey(e),
  "autopilot.homePlan": (o) => autopilot.homePlan(o),
  "autopilot.constants": () => ({ KEY_SAFE_MIN: autopilot.KEY_SAFE_MIN, HOME_DROP_PCT: autopilot.HOME_DROP_PCT }),
  "tempoRule.planFit": (o) => tempoRule.planFit(o),
  "tempoRule.beatLock": (o) => tempoRule.beatLock(o),
  "tempoRule.constants": () => ({ PITCH_RANGE_PCT: tempoRule.PITCH_RANGE_PCT, KEYLOCK_RANGE_PCT: tempoRule.KEYLOCK_RANGE_PCT,
    MAX_TEMPO_PCT_PER_BAR: tempoRule.MAX_TEMPO_PCT_PER_BAR }),
  "djMind.camelotScore": (a, b) => djMind.camelotScore(a, b),
  "djMind.layerBars": (m) => djMind.layerBars(m),
  "djMind.layerDecision": (ctx) => djMind.layerDecision(ctx),
  // dj-mind.js nextEnergyNote() keeps its state in the browser closure; the same three calls
  // (energyNote, motifHook) with the state passed in and back: {note, hook, callbackDone, repriseAsked}
  "sim.nextEnergyNote": (energies, setPos, callbackDone, history, repriseAsked) => {
    const n = djMind.energyNote(energies, setPos, callbackDone, { history, repriseAsked });
    let hook = null, asked = repriseAsked;
    if (n === "reprise") { const m = djMind.motifHook(history); asked = `${m.key}:${m.plays}`; hook = m.title; }
    return { note: n, hook, callbackDone: callbackDone || n === "callback", repriseAsked: asked };
  },
  "autopilot.useLibraryFallback": (n) => autopilot.useLibraryFallback(n),
  "autopilot.emptyRetryMs": (n) => autopilot.emptyRetryMs(n),
  "stem.pickIntro": (c) => stem.pickIntro(c),
  "sim.stemBlend": simStemBlend,
  "sim.stemBridge": simStemBridge,
  "sim.moveSeconds": moveSeconds,
  "sim.ping": () => "pong",
};

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on("line", (line) => {
  if (!line.trim()) return;
  let req;
  try { req = JSON.parse(line); } catch (e) { process.stdout.write(JSON.stringify({ id: null, ok: false, error: "bad json" }) + "\n"); return; }
  try {
    const f = FNS[req.fn];
    if (!f) throw new Error(`unknown fn ${req.fn}`);
    const result = f(...(req.args || []));
    process.stdout.write(JSON.stringify({ id: req.id, ok: true, result: result === undefined ? null : result }) + "\n");
  } catch (e) {
    process.stdout.write(JSON.stringify({ id: req.id, ok: false, error: String(e && e.stack || e) }) + "\n");
  }
});
