// Entry of the headless console: boots the console's own scripts on the virtual clock, starts a
// set exactly as a user does (seed URL in the START box, click START), lets it play until the
// requested number of songs has played, stops it, and writes what happened to cfg.out.
//
//   node app/sim/js/run-set.js cfg.json
//   cfg = { port, seedUrl, mode, tracks, occasion, seed, maxSeconds, out, echo, toggles: {id: bool} }
"use strict";
const fs = require("fs");
const path = require("path");
const { createEnv, safe } = require("./env");
const { Sampler, analyseWindow } = require("./graph");

// the console's scripts; SIM_STATIC_DIR points at another copy (used to try a change before it lands)
const STATIC = process.env.SIM_STATIC_DIR || path.resolve(__dirname, "..", "..", "ui", "static");

// NULL-BOT supermoves: the mascot's own pure rule (mascot.js supermoveFor) over the engine's events
function loadPure(file) {       // a console file's node exports, in a sandbox with no window / document
  const vm = require("vm");
  const sb = { module: { exports: {} }, console };
  vm.createContext(sb);
  vm.runInContext(fs.readFileSync(file, "utf8"), sb, { filename: file });
  return sb.module.exports;
}
function superMoves(events) {
  const mascot = loadPure(path.join(STATIC, "mascot.js"));
  const out = [];
  for (const e of events) {
    const sm = mascot.supermoveFor({ type: e.type, detail: e.detail });
    if (sm) out.push({ t: e.t, ...sm });
  }
  return out;
}

async function main() {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  // paint-only scripts (waveforms, canvas visuals, marquee) are not part of the engine: skipped
  const skip = cfg.skip || ["stem-wave.js", "visuals.js", "marquee.js"];
  const env = await createEnv({ staticDir: STATIC, port: cfg.port, seed: cfg.seed || 1, echo: !!cfg.echo, skip });
  env.loadScripts();
  if (cfg.profile) env.clock.profile = new Map();
  const $ = (id) => document.getElementById(id);
  $("ap-mode").value = cfg.mode || "long";
  $("ap-seed-input").value = cfg.seedUrl;
  $("ap-occasion-input").value = cfg.occasion || "";
  for (const [id, on] of Object.entries(cfg.toggles || {})) { const el = $(id); if (el) el.checked = !!on; }

  if (cfg.watchdog) {           // debugging aid: real-time heartbeat of the virtual run
    require("timers").setInterval(() => {
      const c = env.logs.console;
      const line = `[watchdog] virtual ${env.clock.now.toFixed(1)}s firings ${env.clock.firings} pendingNet ${env.net.inflight} last: ${c.length ? c[c.length - 1].text.slice(0, 160) : ""}\n`;
      fs.appendFileSync(cfg.watchlog || "/dev/stderr", line);
    }, 5000).unref();
  }
  await env.clock.run(0.5);
  const sampler = new Sampler(env);       // what the audio graph would have played, every 250 ms
  sampler.start();
  // the presentation layer's own state (VIBE strip, NULL-BOT), every 5 virtual seconds
  const uiStates = [];
  env.clock.setInterval(() => {
    const bar = document.getElementById("vibe-bar"), bot = document.getElementById("nul-mascot");
    const snap = { t: +env.clock.now.toFixed(1), vibe: bar ? bar.textContent.replace(/\s+/g, " ").trim().slice(0, 240) : null,
      vibe_classes: bar ? bar.className : null, mascot: bot ? { cls: bot.className, mood: bot.dataset.mood || null, text: bot.textContent.trim().slice(0, 40) } : null };
    const last = uiStates[uiStates.length - 1];
    if (!last || last.vibe !== snap.vibe || JSON.stringify(last.mascot) !== JSON.stringify(snap.mascot)) uiStates.push(snap);
  }, 5000);
  $("ap-start-btn").click();
  const want = Math.max(1, (cfg.tracks || 4) - 1);
  let why = "";
  const stop = () => {
    if (env.net.marks.transitionEnd >= want) { why = "songs"; return true; }
    return false;
  };
  const res = await env.clock.run(cfg.maxSeconds || 4000, stop);
  if (!why) why = res === "idle" ? "idle" : "time";
  // let the last transition's tail play out, then stop the set like the STOP button
  await env.clock.run(env.clock.now + 30);
  $("ap-stop-btn").click();
  await env.clock.run(env.clock.now + 2);

  // audible-effect windows: each transition (start .. end) and the whole set
  const ev = env.net.sessionEvents.filter((e) => e.kind === "track" && e.data);
  const starts = ev.filter((e) => e.data.event === "transition_start");
  const ends = ev.filter((e) => e.data.event === "transition_end");
  const windows = starts.map((s, i) => {
    const t1 = (ends[i] && ends[i].t) || s.t + (s.data.seconds || 30);
    return { i, from: s.data.from, to: s.data.to, recipe: s.data.recipe, planned: s.data.planned, out: s.data.out, in: s.data.in, seconds: s.data.seconds, t0: s.t, t1,
      ...analyseWindow(sampler.series, s.t - 1, t1, { inDeck: s.data.in }) };
  });
  const whole = sampler.series.length ? analyseWindow(sampler.series, sampler.series[0].t + 5, sampler.series[sampler.series.length - 1].t) : null;
  const series1hz = sampler.series.filter((s, k) => k % 4 === 0).map((s) => [s.t, +(10 * Math.log10(Math.max(1e-18, s.P))).toFixed(1),
    +(10 * Math.log10(Math.max(1e-18, (s.decks.a || { P: 0 }).P))).toFixed(1), +(10 * Math.log10(Math.max(1e-18, (s.decks.b || { P: 0 }).P))).toFixed(1)]);
  const profile = env.clock.profile ? [...env.clock.profile.entries()].sort((a, b) => b[1].ms - a[1].ms).slice(0, 20).map(([k, v]) => `${v.ms.toFixed(0)}ms ${v.n}x ${k}`) : undefined;
  const out = {
    profile,
    ended: why, virtual_seconds: +env.clock.now.toFixed(2), timer_firings: env.clock.firings,
    scripts: env.scripts, errors: env.logs.errors, console: env.logs.console, events: env.logs.events,
    ui_states: uiStates, supermoves: superMoves(env.logs.events),
    status_log: document.textLog, play_start_t: (sampler.series.find((s) => s.deck && Object.values(s.deck).some((d) => d.playing)) || {}).t,
    net: env.net.log, session_events: env.net.sessionEvents, audible: { transitions: windows, set: whole, series_1hz: series1hz },
    audio_errors: env.audio ? env.audio._errors : [],
    dom_misses: [...(document.misses || [])].slice(0, 50),
  };
  fs.writeFileSync(cfg.out, JSON.stringify(out));
  process.exit(0);
}
main().catch((e) => { process.stderr.write(`headless run crashed: ${e && e.stack}\n`); process.exit(1); });
