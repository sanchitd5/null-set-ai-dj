// AI Music Brain - pair atlas COMBOS and MACROS in the live console.
//
// Owner: "pre knowledge of songs lead to good sets", "AI needs to prefer known good combos,
// treat like game combo moves", "i am not able to recreate the moves you play".
//   COMBO   a pair the atlas (app/music_brain/pair_atlas.py) scores as working well AND
//           that fits a combo move (merge -> hold, riff x rap, mashup, double drop, drop swap).
//           The autopilot tries combos for the playing song FIRST, before asking the LLM; a
//           song already loaded on the other deck that forms a combo is tried before them.
//           Combos chain: COMBO x3: MERGE -> RIFF x RAP -> MASHUP in the VIBE strip.
//   MACRO   a saved, replayable set (CACHE_DIR/macros/<name>.json). When the playing song is
//           in a macro and its next step is still valid, the autopilot takes it with
//           probability MACRO_PREFERENCE (0.8; the rest explores). MACRO MODE plays the loaded
//           macro deterministically. The user plays it step by step from the MACRO panel.
// Every choice still goes through the autopilot's own gates (evaluateCandidate,
// decideRecipe, planHold ...): a stored plan is a default, never a bypass.
// Pure core (node-testable: app/tests/macro_mode_check.js) + a runtime mounted on the Host port.
(function (root) {
  "use strict";

  const MACRO_PREFERENCE = 0.8;       // share of valid macro steps the autopilot takes (owner)
  const COMBO_MIN_WORKS = 65;         // = pair_atlas.COMBO_MIN_WORKS
  const ARTIST_SPACING = 3;           // no artist twice within this many songs
  const COMBO_LABEL = { merge: "MERGE", riff: "RIFF x RAP", mashup: "MASHUP", double_drop: "DOUBLE DROP", drop_swap: "DROP SWAP" };
  const STEM_RECIPE = /merge|mashup|stem|riff/i;

  function artistOf(name) {
    let n = String(name || "").toLowerCase();
    for (const sep of [" - ", " – ", ": "]) { const i = n.indexOf(sep); if (i > 0) { n = n.slice(0, i); break; } }
    for (const sep of [" & ", " x ", ", ", " feat", " ft.", " ft "]) { const i = n.indexOf(sep); if (i > 0) n = n.slice(0, i); }
    return n.trim();
  }

  // Combos for the playing song A, in the order the autopilot tries them.
  // o: {aId, loadedId (the other deck's song, or null), partners (atlas rows for A),
  //     played (ids), recent (names of the last songs), minWorks}
  // -> {list: [{track_id, name, combo, label, works, plan, loaded}], skipped: [{b, why}]}
  function comboCandidates(o) {
    const played = new Set(o.played || []), minWorks = o.minWorks == null ? COMBO_MIN_WORKS : o.minWorks;
    const recentArt = new Set((o.recent || []).slice(-ARTIST_SPACING).map(artistOf).filter(Boolean));
    const list = [], skipped = [];
    for (const p of o.partners || []) {
      if (!p || !p.combo || p.b === o.aId) continue;
      const why = played.has(p.b) ? "already played this set"
        : p.works < minWorks ? `works ${p.works} < ${minWorks}`
        : recentArt.has(artistOf(p.b_name)) ? `artist spacing (${artistOf(p.b_name)})`
        : (p.played_bad || 0) > (p.played_good || 0) ? `bad played evidence (-${p.played_bad})` : null;
      if (why) { skipped.push({ b: p.b, name: p.b_name, why }); continue; }
      list.push({ track_id: p.b, name: p.b_name, bpm: p.b_bpm, duration: p.b_duration, combo: p.combo, label: COMBO_LABEL[p.combo] || String(p.combo).toUpperCase(),
                  works: p.works, plan: p.plan || null, loaded: p.b === o.loadedId });
    }
    // the user loaded it on purpose: first; then by works (the atlas order), stable
    list.sort((x, y) => (y.loaded - x.loaded) || (y.works - x.works));
    return { list, skipped };
  }

  // The next step of a macro the playing song is in. macros: [{name, steps}] (loaded macro
  // first), valid(step) -> {ok, gate}. -> {found: {macro, step} | null, invalid: [{name, n, gate}]}
  function macroCandidate(o) {
    const played = new Set(o.played || []), invalid = [];
    for (const m of o.macros || []) {
      const step = (m.steps || []).find((s) => s.a === o.aId);
      if (!step) continue;
      const v = played.has(step.b) ? { ok: false, gate: "already played this set" } : (o.valid ? o.valid(step) : { ok: true });
      if (!v.ok) { invalid.push({ name: m.name, n: step.n, gate: v.gate }); continue; }
      return { found: { macro: m.name, step }, invalid };
    }
    return { found: null, invalid };
  }

  // Take the macro step? An invalid step is never taken; a valid one with probability pref,
  // drawn from rng (host.random.next: seeded in the sim). -> {take, draw, line}
  function macroPrefer(c, rng, pref = MACRO_PREFERENCE) {
    if (!c || !c.found) {
      const bad = c && c.invalid && c.invalid[0];
      return { take: false, draw: null, line: bad ? `macro: skipped (invalid: ${bad.gate})` : null };
    }
    const draw = rng();
    const p = Math.max(0, Math.min(1, Number.isFinite(pref) ? pref : MACRO_PREFERENCE));
    if (draw < p) return { take: true, draw, line: `macro: preferred ${c.found.macro} step ${c.found.step.n}` };
    return { take: false, draw, line: `macro: skipped (${Math.round((1 - p) * 100)}% explore)` };
  }

  // Combo streak after a transition landed (combo: the combo move key, or null).
  function streakAfter(s, combo) {
    if (!combo) return { n: 0, names: [] };
    const names = (s && s.names || []).concat([COMBO_LABEL[combo] || String(combo).toUpperCase()]);
    return { n: names.length, names };
  }
  function streakLabel(s) {
    if (!s || !s.n) return "";
    return `COMBO x${s.n}: ${s.names.slice(-4).join(" -> ")}`;
  }

  // The atlas's plan as the default for this pair (candidate from /api/match). The live
  // gates (decideRecipe, the merge gates, exitTiming) still decide what actually plays.
  function applyPlan(cand, plan) {
    if (!plan || !cand) return { cand, used: false };
    const out = Object.assign({}, cand);
    if (plan.recipe) out.recipe = plan.recipe;
    if (Number.isFinite(plan.a_time)) out.a_time = plan.a_time;
    if (Number.isFinite(plan.b_time)) out.b_time = plan.b_time;
    out.atlasPlan = plan;
    return { cand: out, used: out.recipe !== cand.recipe || out.a_time !== cand.a_time || out.b_time !== cand.b_time };
  }

  // When a user's PLAY STEP fires: at the stored exit when it is still ahead, else on the
  // next 8-bar phrase line after now. o: {nowPos, aTime, phrases, bar} -> song seconds
  function fireAt(o) {
    const lead = 0.25;
    if (Number.isFinite(o.aTime) && o.aTime > o.nowPos + lead) return o.aTime;
    const line = (o.phrases || []).find((p) => p > o.nowPos + lead);
    if (line != null) return line;
    const L = 8 * (o.bar || 2);
    return o.nowPos + L - (((o.nowPos - (o.phrases && o.phrases[0] || 0)) % L) + L) % L;
  }

  // The safety gates a stored step must pass right now (same rules as the autopilot:
  // tempo-rule planFit, autopilot keySafeRecipe, stems for stem moves).
  // o: {step, aId, bId (other deck), aStems, bStems, aEff, bBpm, keyScore, tempoRule, keySafe}
  // -> {ok, recipe, why}
  function stepGate(o) {
    const s = o.step;
    if (!s) return { ok: false, why: "no step" };
    if (o.aId !== s.a) return { ok: false, why: `the playing song is not step ${s.n}'s A (${s.a_name || s.a})` };
    if (o.bId && o.bId !== s.b) return { ok: false, why: `the other deck holds a different song than ${s.b_name || s.b}` };
    let recipe = s.recipe || "Echo Out", why = s.why || "";
    if (STEM_RECIPE.test(recipe) && !(o.aStems && o.bStems)) {
      recipe = "Bass Swap";
      why = `stems missing: ${s.recipe} falls back to a Bass Swap`;
    }
    if (o.tempoRule && o.aEff > 0 && o.bBpm > 0) {
      const fit = o.tempoRule.planFit({ aEff: o.aEff, bBpm: o.bBpm, stemsBoth: !!(o.aStems && o.bStems), tempoStemsBpm: null });
      if (!fit.beat && !/echo|bridge|filter/i.test(recipe)) {
        const fb = fit.fallback || "Echo Out";
        why = `tempo gap (${fit.why}): ${recipe} falls back to ${fb}`;
        recipe = fb;
      }
    }
    if (o.keySafe) {
      const safe = o.keySafe(recipe, o.keyScore);
      if (safe !== recipe) { why = `keys clash (camelot ${o.keyScore}): ${recipe} becomes ${safe}`; recipe = safe; }
    }
    return { ok: true, recipe, why };
  }

  // Edit one step (recipe and / or points); the server saves the result as a new version.
  function editStep(macro, n, patch) {
    const m = JSON.parse(JSON.stringify(macro));
    const s = m.steps.find((x) => x.n === n);
    if (!s) throw new Error(`no step ${n}`);
    for (const k of ["recipe", "a_time", "b_time"]) if (patch[k] !== undefined) s[k] = patch[k];
    s.why = `edited by the user${patch.why ? `: ${patch.why}` : ""}`;
    m.parent = macro.name;
    return m;
  }

  // The console's current set as a macro: the played transitions [{a, b, a_name, b_name,
  // recipe, a_time, b_time}] in order. Chained A->B->C, else throws.
  function setToMacro(name, transitions) {
    const steps = (transitions || []).filter((t) => t && t.a && t.b).map((t, i) => Object.assign({ n: i + 1 }, t));
    if (!steps.length) throw new Error("no transitions in this set yet");
    for (let i = 1; i < steps.length; i++) if (steps[i].a !== steps[i - 1].b) throw new Error(`step ${i + 1} does not start where step ${i} ended`);
    return { name, source: "console", steps };
  }

  // AI ACTIONS run-now entries (owner add-on contract): refusal on unsafe state, else ok.
  // id: "macro-step" | "macro-transition" | "plan-picks"; c: the state the runtime reads
  function runNowCheck(id, c) {
    if (id === "plan-picks") {
      const n = (c.picks || []).length;
      return n >= 2 ? { ok: true, why: `${n} songs picked` } : { ok: false, why: "pick at least 2 songs (tick them in the COMPATIBLE list)" };
    }
    if (!c.playing) return { ok: false, why: "no deck is playing" };
    const step = id === "macro-step" ? c.step : c.pairStep;
    if (!step) return { ok: false, why: id === "macro-step" ? "load a macro first (MACROS)" : "the atlas has no stored transition for the loaded A/B pair" };
    const g = stepGate(Object.assign({}, c, { step }));
    return g.ok ? { ok: true, why: g.why || `step ${step.n}: ${g.recipe}`, recipe: g.recipe } : g;
  }

  const core = { MACRO_PREFERENCE, COMBO_MIN_WORKS, ARTIST_SPACING, COMBO_LABEL, artistOf, comboCandidates, macroCandidate,
                 macroPrefer, streakAfter, streakLabel, applyPlan, fireAt, stepGate, editStep, setToMacro, runNowCheck,
                 createRuntime: create };   // node checks drive the runtime over a fake Host
  if (typeof module !== "undefined" && module.exports) module.exports = core;

  // ---- runtime: reaches the world only through the Host port (engine.js) ---------------------------------
  function create({ host }) {
    const ui = host.ui;
    const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const partnersCache = new Map();     // aId -> {at, rows}
    let macros = [];                     // full macros known to this tab (loaded one first)
    let loaded = null;                   // the macro in the MACRO panel
    let cursor = 0;                      // its next step index
    let streak = { n: 0, names: [] };
    let armed = null;                    // a step the user armed for the autopilot's next booking
    const played = [];                   // transitions this set: [{a, b, a_name, b_name, recipe, a_time, b_time}]
    const stats = { macroSeen: 0, macroTaken: 0, comboTried: 0, comboPicked: 0, atlasPlan: 0, maxStreak: 0 };
    const flag = (id, d) => ui.flag(id, d);
    const say = (msg, ok = true) => { ui.status(`${ok ? "" : "✗ "}${msg}`); const el = ui.el("macro-status"); if (el) el.textContent = msg; };
    const step = (kind, o) => host.log.step(kind, Object.assign({ phase: "selection" }, o));

    async function getJSON(url, opts) {
      const r = await host.api.fetch(url, opts);
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      return d;
    }
    async function partners(aId, move = null, n = 30) {
      const key = `${aId}|${move || ""}|${n}`, hit = partnersCache.get(key);
      if (hit && host.clock.now() - hit.at < 5 * 60 * 1000) return hit.rows;
      let rows = [];
      try { rows = (await getJSON(`/api/atlas/partners?a=${encodeURIComponent(aId)}&n=${n}${move ? `&move=${encodeURIComponent(move)}` : ""}`)).partners || []; }
      catch (e) { rows = []; }
      partnersCache.set(key, { at: host.clock.now(), rows });
      return rows;
    }
    function planFor(aId, bId) {
      for (const [k, v] of partnersCache) if (k.startsWith(`${aId}|`)) { const r = v.rows.find((x) => x.b === bId); if (r && r.plan) return r.plan; }
      return null;
    }
    function pref() {
      const el = ui.el("ap-macro-pref"), raw = el ? String(el.value == null ? "" : el.value).trim() : "";
      const v = raw === "" ? NaN : Number(raw);
      return Number.isFinite(v) ? Math.max(0, Math.min(1, v > 1 ? v / 100 : v)) : MACRO_PREFERENCE;
    }

    // The autopilot asks before its normal pool / LLM: -> [{track_id, name, keep, _combo, _macro}]
    // o: {played: ids, recent: names, loadedId}
    async function firstCandidates(aId, o = {}) {
      const out = [];
      const mk = (id, name, extra) => Object.assign({ track_id: id, name, keep: false, fromLibrary: true }, extra);
      // 0) a step the user armed (PLAY STEP while the autopilot runs)
      if (armed && armed.a === aId) {
        out.push(mk(armed.b, armed.b_name, { _macro: { name: loaded && loaded.name, step: armed, byUser: true } }));
        step("macro", { decision: `step ${armed.n} armed by user`, why: `${armed.recipe} into ${armed.b_name}` });
        armed = null;
        return out;
      }
      // 1) a known macro step (MACRO MODE: always; else with probability MACRO_PREFERENCE)
      const pool = (loaded ? [loaded] : []).concat(macros.filter((m) => !loaded || m.name !== loaded.name));
      // offline part of "still valid"; the autopilot's evaluateCandidate re-runs every live gate
      // (tempo, key, energy step, stems) on the step and falls through when one refuses it
      const recentArt = (o.recent || []).slice(-ARTIST_SPACING).map(artistOf).filter(Boolean);
      const mc = macroCandidate({ macros: pool, aId, played: o.played,
        valid: (s) => (recentArt.includes(artistOf(s.b_name)) ? { ok: false, gate: `artist spacing (${artistOf(s.b_name)})` } : { ok: true }) });
      const macroMode = flag("ap-macro-mode", false) && loaded;
      const pick = macroMode && mc.found && mc.found.macro === loaded.name ? { take: true, line: `macro: MACRO MODE ${loaded.name} step ${mc.found.step.n}` }
        : macroPrefer(mc, () => host.random.next(), pref());
      if (mc.found) stats.macroSeen++;
      if (pick.line) { console.info(pick.line); step("macro", { decision: pick.take ? "preferred" : "skipped", why: pick.line }); }
      if (pick.take) {
        stats.macroTaken++;
        const s = mc.found.step;
        out.push(mk(s.b, s.b_name, { _macro: { name: mc.found.macro, step: s } }));
      }
      // 2) combos for A (loaded partner first)
      if (flag("ap-atlas-combo", true)) {
        const rows = await partners(aId, null, 40);
        const cc = comboCandidates({ aId, loadedId: o.loadedId, partners: rows, played: o.played, recent: o.recent });
        for (const c of cc.list) {
          if (out.some((x) => x.track_id === c.track_id)) continue;
          out.push(mk(c.track_id, c.name, { _combo: c, bpm: c.bpm, duration: c.duration }));
        }
        stats.comboTried += cc.list.length ? 1 : 0;
        const line = cc.list.length ? `combo: picked: ${cc.list.slice(0, 3).map((c) => `${c.name} (${c.label}, works ${c.works}${c.loaded ? ", loaded" : ""})`).join(", ")}`
          : `combo: skipped: ${cc.skipped.length ? cc.skipped.slice(0, 2).map((s) => `${s.name}: ${s.why}`).join("; ") : "no combo for this song in the atlas"}`;
        console.info(line);
        step("combo", { decision: cc.list.length ? "picked" : "skipped", why: line });
      }
      return out;
    }
    // evaluateCandidate: the atlas / macro plan as the default plan (gates re-validate it)
    function defaultPlan(aId, cand, match) {
      const st = cand && cand._macro && cand._macro.step;
      const plan = st ? { recipe: st.recipe, a_time: st.a_time, b_time: st.b_time, merge: st.merge || null } : planFor(aId, cand && cand.track_id);
      const r = applyPlan(match, plan);
      if (plan) {
        stats.atlasPlan++;
        const line = `atlas: plan ${plan.recipe} exit ${Math.round(plan.a_time || 0)} s entry ${Math.round(plan.b_time || 0)} s${st ? ` (macro ${cand._macro.name} step ${st.n})` : ""}`;
        console.info(line);
        step("atlas", { decision: "plan", why: line });
      }
      return r.cand;
    }
    // a transition landed: streak, set record, VIBE strip
    function landed(aId, bId, info = {}) {
      const rows = [...partnersCache.entries()].filter(([k]) => k.startsWith(`${aId}|`)).flatMap(([, v]) => v.rows);
      const p = rows.find((x) => x.b === bId);
      const combo = info.combo || (p && p.combo) || null;
      streak = streakAfter(streak, combo);
      stats.maxStreak = Math.max(stats.maxStreak, streak.n);
      if (combo) stats.comboPicked++;
      played.push({ a: aId, b: bId, a_name: info.aName || (p && p.a_name) || "", b_name: info.bName || (p && p.b_name) || "",
                    recipe: info.recipe || "Echo Out", a_time: info.aTime, b_time: info.bTime, combo });
      if (loaded) { const i = loaded.steps.findIndex((s) => s.a === aId && s.b === bId); if (i >= 0) cursor = i + 1; renderMacro(); }
      const label = streakLabel(streak);
      const el = ui.el("combo-streak");
      if (el) { el.textContent = label; el.hidden = !label; }
      if (label) console.info("combo:", label);
    }

    // ---- MACRO panel -------------------------------------------------------------------------------------------------
    function renderMacro() {
      const el = ui.el("macro-steps");
      if (!el) return;
      if (!loaded) { el.innerHTML = `<li class="macro-empty">no macro loaded</li>`; return; }
      el.innerHTML = loaded.steps.map((s, i) => `<li data-n="${s.n}" class="${i === cursor ? "macro-next" : ""}">` +
        `<b>${s.n}.</b> ${esc(s.b_name || s.b)} <span class="macro-rec">${esc(s.recipe)}</span>` +
        ` <span class="macro-pts">out ${fmt(s.a_time)} / in ${fmt(s.b_time)}</span>` +
        (s.merge ? ` <span class="macro-merge">hold ${esc(s.merge.hold_bars)} bars, ${esc((s.merge.phases || []).length)} phases</span>` : "") +
        `</li>`).join("");
    }
    const fmt = (t) => (Number.isFinite(t) ? `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}` : "?");
    async function refreshList() {
      const sel = ui.el("macro-select");
      try {
        const list = (await getJSON("/api/macros")).macros || [];
        if (sel) sel.innerHTML = `<option value="">MACROS…</option>` + list.map((m) => `<option value="${esc(m.name)}">${esc(m.name)} · ${m.songs} songs</option>`).join("");
        macros = [];
        for (const m of list.slice(0, 20)) { try { macros.push((await getJSON(`/api/macros/${encodeURIComponent(m.name)}`)).macro); } catch (e) { /* skip */ } }
      } catch (e) { say(`macros: ${e.message}`, false); }
    }
    async function loadMacro(name) {
      if (!name) { loaded = null; renderMacro(); return; }
      const d = await getJSON(`/api/macros/${encodeURIComponent(name)}`);
      loaded = d.macro; cursor = 0;
      const bad = (d.validation || []).filter((v) => !v.ok);
      renderMacro();
      say(`macro ${loaded.name}: ${loaded.steps.length} steps${bad.length ? `; ${bad.length} need a fallback (${bad[0].issues[0]})` : ""}`, !bad.length);
    }
    function deckState() {
      const ap = host.mod.autopilotState, decks = host.decks || {}, st = host.state || {};
      const aDeck = ap && ap.activeDeck || "a", bDeck = aDeck === "a" ? "b" : "a";
      const d = decks[aDeck], o = decks[bDeck];
      const aId = aDeck === "a" ? st.trackA : st.trackB, bId = aDeck === "a" ? st.trackB : st.trackA;
      const cs = host.mod.djMind && host.mod.djMind.core && host.mod.djMind.core.camelotScore;
      const ka = d && d.analysis && d.analysis.key && d.analysis.key.camelot, kb = o && o.analysis && o.analysis.key && o.analysis.key.camelot;
      return { aDeck, bDeck, d, o, aId, bId, playing: !!(d && d.isPlaying !== false && d.buffer),
               aStems: !!(d && d.stemsReady), bStems: !!(o && o.stems), aEff: d && d.bpm ? d.bpm * (d._playbackRate ? d._playbackRate() : 1) : 0,
               bBpm: o && o.bpm, keyScore: cs && ka && kb ? cs(ka, kb) : null,
               tempoRule: host.mod.tempoRule, keySafe: host.mod.autopilot && host.mod.autopilot.core && host.mod.autopilot.core.keySafeRecipe };
    }
    async function ensureLoaded(deck, id, name) {
      const st = host.state || {};
      if ((deck === "a" ? st.trackA : st.trackB) === id) return;
      const r = await host.api.fetch(`/api/audio/tracks/${encodeURIComponent(id)}`);
      if (!r.ok) throw new Error(`cannot load ${name || id}`);
      await host.loadIntoDeck(deck, id, name || id, await r.blob());
    }
    // PLAY STEP / PLAY THIS TRANSITION: the same gates as the autopilot. With the autopilot
    // running the step is armed for its next booking; else it fires on the console's own
    // move at the stored point (quantised to the next phrase line when that passed).
    async function playStep(s, label) {
      const c0 = deckState();
      if (!s) return say(`${label}: no step`, false);
      if (c0.bId !== s.b) {
        try { await ensureLoaded(c0.bDeck, s.b, s.b_name); } catch (e) { return say(`${label}: ${e.message}`, false); }
      }
      const c = deckState();
      const g = runNowCheck("macro-step", Object.assign({}, c, { step: s }));
      host.log.step("macro", { phase: "user", decision: g.ok ? `step ${s.n} played by user` : "refused", why: g.why });
      console.info(`macro: step ${s.n} ${g.ok ? "played by user" : `refused: ${g.why}`}`);
      if (!g.ok) return say(`${label}: ${g.why}`, false);
      const ap = host.mod.autopilotState;
      if (ap && ap.active) { armed = s; return say(`${label}: step ${s.n} armed, ${g.recipe} into ${s.b_name} at the next booking`); }
      const d = c.d, pos = d._currentPosition ? d._currentPosition() : 0;
      const at = fireAt({ nowPos: pos, aTime: s.a_time, phrases: d.analysis && d.analysis.phrase_boundaries_8bar, bar: 240 / (d.bpm || 128) });
      if (c.o && Number.isFinite(s.b_time) && c.o.seek) c.o.seek(s.b_time);
      const act = /mashup/i.test(g.recipe) ? "mashup" : /riff/i.test(g.recipe) ? "riff" : "mix";
      const lead = Math.max(0, (at - pos) / (d._playbackRate ? d._playbackRate() : 1) - 240 / (d.bpm || 128) * 8);
      host.clock.setTimeout(() => { const aa = host.mod.aiActions; if (aa && aa[act]) aa[act](); }, lead * 1000);
      say(`${label}: step ${s.n} ${g.recipe} into ${s.b_name} at ${fmt(at)}${g.why ? ` (${g.why})` : ""}`);
      cursor = Math.max(cursor, (loaded ? loaded.steps.indexOf(s) : -1) + 1);
      renderMacro();
    }
    async function pairStep() {
      const c = deckState();
      if (!c.aId || !c.bId) return null;
      await partners(c.aId, null, 40);
      const p = planFor(c.aId, c.bId);
      return p ? { n: 1, a: c.aId, b: c.bId, recipe: p.recipe, a_time: p.a_time, b_time: p.b_time, merge: p.merge, b_name: "" } : null;
    }
    async function saveSet() {
      const nameEl = ui.el("macro-name");
      const name = (nameEl && nameEl.value.trim()) || `set-${new Date(host.clock.now()).toISOString().slice(0, 16).replace(/[:T]/g, "-")}`;
      try {
        const m = setToMacro(name, played);
        const d = await getJSON("/api/macros", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ macro: m }) });
        say(`saved macro ${d.macro.name} (${d.macro.steps.length} transitions)`);
        refreshList();
      } catch (e) { say(`SAVE MACRO: ${e.message}`, false); }
    }
    async function saveEdited(n, patch) {
      if (!loaded) return;
      try {
        const m = editStep(loaded, n, patch);
        const d = await getJSON("/api/macros", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ macro: m }) });
        loaded = d.macro; renderMacro(); refreshList();
        say(`step ${n} edited: saved as ${d.macro.name}`);
      } catch (e) { say(`edit: ${e.message}`, false); }
    }
    // ---- COMPATIBLE panel: partners of the playing song grouped by move ------------------------------------------------
    const GROUPS = [["merge", "MERGE"], ["riff", "RIFF x RAP"], ["mashup", "MASHUP"], ["supermove", "SUPERMOVE"], [null, "OTHER MOVES"]];
    const picks = new Set();
    async function renderCompatible() {
      const el = ui.el("compat-list");
      const c = deckState();
      if (!el) return;
      if (!c.aId) { el.innerHTML = `<p class="compat-empty">play a song to see its partners</p>`; return; }
      const parts = [];
      for (const [move, title] of GROUPS) {
        const rows = await partners(c.aId, move, 6);
        parts.push(`<h4>${title}</h4><ul>` + (rows.length ? rows.map((r) => `<li><label><input type="checkbox" data-pick="${esc(r.b)}" ${picks.has(r.b) ? "checked" : ""}/> ${esc(r.b_name)}</label>` +
          ` <span class="compat-works">${r.works}</span> <span class="compat-rec">${esc(r.best)}</span>` +
          ` <button class="hw-btn compat-arm" data-arm="${esc(r.b)}" data-move="${esc(move || "")}" title="Load on the other deck and arm this move">LOAD + ARM</button></li>`).join("")
          : `<li class="compat-empty">none</li>`) + `</ul>`);
      }
      el.innerHTML = parts.join("");
    }
    async function arm(bId, move) {
      const c = deckState();
      const rows = await partners(c.aId, move || null, 40);
      const r = rows.find((x) => x.b === bId);
      if (!r) return say("LOAD + ARM: not in the atlas", false);
      const s = { n: 1, a: c.aId, b: bId, a_name: r.a_name, b_name: r.b_name, recipe: r.plan ? r.plan.recipe : r.recipe,
                  a_time: r.plan && r.plan.a_time, b_time: r.plan && r.plan.b_time, merge: r.plan && r.plan.merge };
      await playStep(s, `${(move || "move").toUpperCase()}`);
    }
    async function planFromPicks() {
      const c = deckState();
      const ids = [c.aId, ...picks].filter(Boolean);
      const g = runNowCheck("plan-picks", { picks: ids });
      if (!g.ok) return say(`PLAN FROM PICKS: ${g.why}`, false);
      const lockEl = ui.el("macro-lock-order");
      try {
        const d = await getJSON("/api/macros/plan-from-picks", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids: [...new Set(ids)], locked: !!(lockEl && lockEl.checked), name: "picks" }) });
        await refreshList();
        await loadMacro(d.macro.name);
        host.log.step("macro", { phase: "user", decision: "plan from picks", why: `${d.macro.name}: ${d.macro.tracks.length} songs` });
      } catch (e) { say(`PLAN FROM PICKS: ${e.message}`, false); }
    }

    const ACTIONS = {
      "macro-step": () => playStep(loaded && loaded.steps[cursor], "PLAY STEP"),
      "macro-transition": async () => { const s = await pairStep(); return s ? playStep(s, "PLAY THIS TRANSITION") : say("PLAY THIS TRANSITION: the atlas has no stored transition for the loaded pair", false); },
      "plan-picks": () => planFromPicks(),
    };
    const run = (id) => { if (ACTIONS[id]) return ACTIONS[id](); return undefined; };
    if (root.aiActions && typeof root.aiActions.register === "function") for (const id of Object.keys(ACTIONS)) root.aiActions.register(id, ACTIONS[id]);
    if (root.djEvents && root.djEvents.addEventListener) root.djEvents.addEventListener("ai-action", (e) => run(e.detail && e.detail.id));

    const on = (id, ev, fn) => { const el = ui.el(id); if (el) el.addEventListener(ev, fn); };
    on("macro-select", "change", (e) => loadMacro(e.target.value).catch((x) => say(x.message, false)));
    on("macro-save", "click", saveSet);
    on("macro-next", "click", () => ACTIONS["macro-step"]());
    on("macro-skip", "click", () => { if (loaded) { cursor = Math.min(loaded.steps.length, cursor + 1); renderMacro(); } });
    on("macro-repeat", "click", () => { if (loaded) { cursor = Math.max(0, cursor - 1); renderMacro(); } });
    on("macro-edit", "click", () => {
      if (!loaded || !loaded.steps[cursor]) return;
      const s = loaded.steps[cursor];
      const rec = ui.el("macro-edit-recipe"), a = ui.el("macro-edit-a"), b = ui.el("macro-edit-b");
      saveEdited(s.n, { recipe: rec && rec.value || undefined, a_time: a && a.value !== "" ? Number(a.value) : undefined, b_time: b && b.value !== "" ? Number(b.value) : undefined });
    });
    on("macro-picks", "click", planFromPicks);
    on("compat-refresh", "click", renderCompatible);
    on("compat-list", "change", (e) => { const id = e.target && e.target.dataset && e.target.dataset.pick; if (id) { if (e.target.checked) picks.add(id); else picks.delete(id); } });
    on("compat-list", "click", (e) => { const b = e.target && e.target.closest && e.target.closest("[data-arm]"); if (b) arm(b.dataset.arm, b.dataset.move || null); });
    if (host.bus && host.bus.on) host.bus.on("keydown", (e) => {
      if (e && e.key === "M" && e.shiftKey && !(e.target && /input|select|textarea/i.test(e.target.tagName || ""))) ACTIONS["macro-step"]();
    });
    refreshList();
    renderMacro();

    return { core, firstCandidates, defaultPlan, landed, partners, planFor, loadMacro, playStep, saveSet, run, ACTIONS,
             get stats() { return Object.assign({ streak: streak.n }, stats); }, get streak() { return streak; }, get loaded() { return loaded; } };
  }
  if (root.Engine) root.Engine.mount("macroMode", create);
})(typeof window !== "undefined" ? window : globalThis);
