// VIBE strip: shows, at a glance, what the AI hears, plans and does.
//
//   orb        idle / listening / thinking (which LLM job, queue) / planning / mixing
//   next       countdown to the booked transition, the recipe, and for a Stem Merge
//              the per-stem ownership lanes (who owns drums/bass/vox/synth), the
//              handover line and B-full, with the silent ear's score when it heard it
//   phrase     bar within the 8-bar phrase + phrase index on the on-air deck, beat dots
//   energy     measured 1-10 level of this song and the next, trail of recent songs
//   key        Camelot keys of both decks and whether they agree
//   stems      per-deck stem meters (stemState), flashing when the AI moves one
//   hook       next hook drop / booked drop countdown with the lyric line
//   badges     YouTube paused, stem backlog, hold loop, last live-ear decision
//   feed       short auto-fading entries for every AI event, glitches in warn style
//
// Pure presentation: reads globals / events / cheap GET status routes, never
// touches audio or playback. Pure core is exported for node checks.
(function (root) {
  "use strict";

  const STEMS = ["drums", "bass", "vocals", "other"];
  const STEM_TAG = { drums: "DRM", bass: "BAS", vocals: "VOX", other: "SYN" };
  const FEED_TTL_MS = 12000, FEED_MAX = 6, FEED_DEDUPE_MS = 4000;
  const TRAIL_MAX = 10;

  const JOB_TXT = {
    plan: "planning the transition", suggest: "picking next songs", lookahead: "looking ahead",
    ear: "silent ear auditioning", live: "live ear listening",
  };

  // -- pure core -------------------------------------------------------------

  // s: {transitioning, preplanning (song name|null), recipe, gate {in_flight, queued[]},
  //     model {ready, detail}, holdLoop (bool), earRecent (bool)} -> {state, label, detail, queue}
  function aiState(s) {
    s = s || {};
    const gate = s.gate || {};
    const queue = Array.isArray(gate.queued) ? gate.queued.length : 0;
    const job = gate.in_flight || null;
    const out = (state, label, detail) => ({ state, label, detail, queue, job });
    if (s.transitioning) return out("mixing", "MIXING", s.recipe ? `playing ${s.recipe}` : "transition running");
    if (s.preplanning) return out("planning", "PLANNING", `pre-planning the mix into ${s.preplanning}`);
    if (job === "ear" || job === "live") return out("listening", "LISTENING", JOB_TXT[job]);
    if (job) return out("thinking", "THINKING", JOB_TXT[job] || job);
    if (s.holdLoop || s.earRecent) return out("listening", "LISTENING", s.holdLoop ? "hold loop: ear on the seam" : "live ear listening");
    // only a local runtime that exists but is not up yet (no backend = Ollama / none: nothing to warm)
    if (s.model && s.model.backend && s.model.ready === false) return out("idle", "IDLE", `model ${s.model.detail || "not ready"}`);
    return out("idle", "IDLE", queue ? "waiting on the queue" : "riding the mix");
  }

  // seconds of real time until the deck position reaches fireAt (null when unknown)
  function countdown(fireAt, pos, rate) {
    if (!Number.isFinite(fireAt) || !Number.isFinite(pos)) return null;
    return (fireAt - pos) / (rate > 0 ? rate : 1);
  }

  function fmtSecs(s) {
    if (!Number.isFinite(s)) return "--";
    const neg = s < 0, a = Math.abs(s);
    if (a < 10) return `${neg ? "-" : ""}${a.toFixed(1)}s`;
    const m = Math.floor(a / 60), r = Math.floor(a % 60);
    return `${neg ? "-" : ""}${m}:${String(r).padStart(2, "0")}`;
  }

  // Stem Merge ownership lanes from a deck's _mergePlan {M, pick {combo, label, ear}}.
  // Bars 0..M: each stem from the deck the combo names; the handover line at M;
  // B owns everything from M (A's tones fade over 8 bars, B full at M + 8).
  function mergeLanes(plan) {
    if (!plan || !plan.pick || !plan.pick.combo || !(plan.M > 0)) return null;
    const M = plan.M, total = M + 8, c = plan.pick.combo;
    const lanes = STEMS.map((n) => {
      const segs = [{ from: 0, to: M, owner: c[n] === "a" ? "a" : "b" }, { from: M, to: total, owner: "b" }];
      const merged = [];
      for (const g of segs) {
        const last = merged[merged.length - 1];
        if (last && last.owner === g.owner) last.to = g.to; else merged.push({ ...g });
      }
      return { stem: n, tag: STEM_TAG[n], segs: merged };
    });
    const ear = plan.pick.ear;
    return {
      total, handover: M, lanes, label: plan.pick.label || "",
      heard: !!plan.heard, preplanned: !!plan.preplanned,
      earScore: ear && Number.isFinite(ear.score) ? ear.score : null,
      why: (ear && (ear.why || ear.reason)) || (Array.isArray(plan.pick.reasons) ? plan.pick.reasons.join(", ") : ""),
    };
  }

  // playhead as a 0-1 fraction of the merge (bars elapsed since A reached aT)
  function mergePlayhead(plan, posA, barA) {
    if (!plan || !(plan.M > 0) || !Number.isFinite(plan.aT) || !Number.isFinite(posA) || !(barA > 0)) return null;
    const bars = (posA - plan.aT) / barA, total = plan.M + 8;
    return { bars, frac: Math.max(0, Math.min(1, bars / total)), live: bars >= 0 && bars <= total, phase: mergePhase(plan, bars) };
  }
  // MERGE -> HOLD -> TRANSITION phase at `bars` since the merge start (plan.phases from
  // stem-moves holdPlan; a classic fixed merge has the same 2 / M / M + 8 lines).
  function mergePhase(plan, bars) {
    if (!plan || !(plan.M > 0) || !Number.isFinite(bars) || bars < 0 || bars > plan.M + 8) return null;
    return bars < 2 ? "merge" : bars < plan.M ? "hold" : "handover";
  }

  function lastLE(arr, t) {           // index of the last value <= t, -1 when none
    let lo = 0, hi = arr.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (arr[mid] <= t + 1e-3) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }

  // bar within the 8-bar phrase (1-8), phrase index (1-based), beat (1-4)
  function phraseAt(downbeats, phrases, pos) {
    if (!Array.isArray(downbeats) || !downbeats.length || !Number.isFinite(pos)) return null;
    const i = lastLE(downbeats, pos);
    if (i < 0) return null;
    let start = 0, phrase = Math.floor(i / 8) + 1;
    if (Array.isArray(phrases) && phrases.length) {
      const p = lastLE(phrases, pos);
      const offset = phrases[0] <= downbeats[0] + 0.1 ? 0 : 1;
      phrase = Math.max(1, p + 1 + offset);
      if (p >= 0) {
        const j = lastLE(downbeats, phrases[p] + 0.05);
        start = j < 0 ? 0 : j;
      }
    }
    const barLen = i + 1 < downbeats.length ? downbeats[i + 1] - downbeats[i]
      : i > 0 ? downbeats[i] - downbeats[i - 1] : 2;
    const beat = Math.max(1, Math.min(4, Math.floor(((pos - downbeats[i]) / (barLen || 2)) * 4) + 1));
    const rel = i - start;
    return { bar: (((rel % 8) + 8) % 8) + 1, phrase, beat };
  }

  // camelot agreement of two decks; scoreFn = djMind.core.camelotScore
  function harmony(keyA, keyB, scoreFn) {
    if (!keyA || !keyB || typeof scoreFn !== "function") return null;
    const score = scoreFn(keyA, keyB);
    const label = score >= 1 ? "SAME KEY" : score >= 0.9 ? "±1 HOUR" : score >= 0.85 ? "REL MAJ/MIN"
      : score >= 0.8 ? "+2 BOOST" : "CLASH";
    return { score, label, ok: score >= 0.8 };
  }

  function energyChips(level) {
    return Number.isFinite(level) ? Math.max(0, Math.min(10, Math.round(level))) : 0;
  }

  function trailPush(trail, level, max) {
    if (!Number.isFinite(level)) return trail.slice();
    return trail.concat([level]).slice(-(max || TRAIL_MAX));
  }

  // stemState (null = full mix) -> {drums, bass, vocals, other} 0-1
  function stemLevels(stemState) {
    const out = {};
    for (const n of STEMS) {
      const v = stemState ? stemState[n] : 1;
      out[n] = Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 1;
    }
    return out;
  }

  // next hook the on-air deck could drop (cut_at ahead within `within` s)
  function nextHook(hooks, pos, within) {
    if (!Array.isArray(hooks) || !Number.isFinite(pos)) return null;
    let best = null;
    for (const h of hooks) {
      if (!h || !Number.isFinite(h.cut_at)) continue;
      const dt = h.cut_at - pos;
      if (dt > 0 && dt <= (within || 45) && (!best || dt < best.in)) best = { in: dt, text: h.text || "", drop_at: h.drop_at };
    }
    return best;
  }

  // earliest booked cue still ahead (at: AudioContext seconds); cues older than 1 s drop out
  function nextCue(cues, now, kind) {
    let best = null;
    for (const c of cues || []) {
      if (!c || !Number.isFinite(c.at) || (kind && c.kind !== kind)) continue;
      if (c.at > now - 0.3 && (!best || c.at < best.at)) best = c;
    }
    return best;
  }
  function pruneCues(cues, now) { return (cues || []).filter((c) => c && Number.isFinite(c.at) && c.at > now - 1); }

  // window event -> feed entry {kind, label, why, deck, warn} (null = not worth a line)
  function feedEntry(type, d) {
    d = d || {};
    const deck = d.deck ? String(d.deck).toUpperCase() : "";
    if (type === "ai-activity") {
      if (d.kind === "stems") return null;                        // meters show these
      if (d.kind === "decision") {
        if (!d.action || d.action === "ride") return null;
        return { kind: "decision", label: `${deck} ${String(d.action).replace(/_/g, " ").toUpperCase()}`, why: d.why || "", deck };
      }
      if (d.kind === "precheck") return { kind: "precheck", label: `${deck} SILENT CHECK`, why: `${(d.tried || []).length || ""} loop spans`, deck };
      if (d.kind === "silent-ear") {
        const r = d.result || {};
        return { kind: "ear", label: `EAR ${String(r.verdict || "heard").toUpperCase()}`, why: r.reason || "", deck };
      }
      return { kind: d.kind || "ai", label: d.label || String(d.kind || "AI").toUpperCase(), why: d.why || "", deck };
    }
    if (type === "ai-cue") return { kind: `cue-${d.kind || "cue"}`, label: `${deck} ${String(d.kind || "cue").toUpperCase()} BOOKED`, why: d.why || "", deck };
    if (type === "ear-flush") return { kind: "ear-flush", label: "EAR FLUSHED", why: d.reason || "", deck };
    if (type === "seek-refused") return { kind: "seek-refused", label: `${deck} SEEK REFUSED`, why: d.why || "", deck, warn: true };
    if (type === "glitch") {
      const good = d.kind === "recover" || d.kind === "silence_end";
      return { kind: `glitch-${d.kind}`, label: good ? `RECOVERED (${d.kind})` : `GLITCH · ${String(d.kind || "?").toUpperCase()}`,
        why: `${d.where || ""}${d.move && d.move.label ? ` · during ${d.move.label}` : ""}`, deck: "", warn: !good };
    }
    return null;
  }

  function feedPrune(list, now) {
    return list.filter((x) => now - x.t < (x.warn ? FEED_TTL_MS * 2 : FEED_TTL_MS));
  }

  // newest first; the same kind+label within FEED_DEDUPE_MS bumps a counter instead of a new row
  let feedSeq = 0;
  function feedPush(list, e, now) {
    const out = feedPrune(list || [], now);
    if (!e) return out;
    const key = `${e.kind}|${e.label}`;
    const i = out.findIndex((x) => x.key === key && now - x.t < FEED_DEDUPE_MS);
    if (i >= 0) {
      const dup = { ...out[i], count: out[i].count + 1, t: now, why: e.why || out[i].why };
      return [dup].concat(out.filter((_, k) => k !== i));
    }
    return [{ ...e, key, t: now, count: 1, id: ++feedSeq }].concat(out).slice(0, FEED_MAX);
  }

  const core = {
    STEMS, aiState, countdown, fmtSecs, mergeLanes, mergePlayhead, mergePhase, phraseAt, harmony, energyChips,
    trailPush, stemLevels, nextHook, nextCue, pruneCues, feedEntry, feedPush, feedPrune,
    FEED_TTL_MS, FEED_MAX, FEED_DEDUPE_MS,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof document === "undefined") return;

  // -- browser ----------------------------------------------------------------
  const bar = document.getElementById("vibe-bar");
  if (!bar) return;
  const HIDE_KEY = "vibeUiHidden";
  const reduced = root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const clock = () => (typeof audioCtx !== "undefined" ? audioCtx.currentTime : performance.now() / 1000);

  const stemCol = (id) => `<div class="vb-stems" data-deck="${id}"><span class="vb-k vb-${id}">${id.toUpperCase()}</span>` +
    STEMS.map((n) => `<span class="vb-meter" data-stem="${n}" title="${n}"><i></i><b>${STEM_TAG[n]}</b></span>`).join("") + "</div>";
  bar.innerHTML = `
    <button type="button" class="hw-btn vb-toggle" aria-pressed="true" title="Show / hide the AI vibe strip">VIBE</button>
    <div class="vb-body">
      <div class="vb-cell vb-orb" data-state="idle" role="status" aria-live="polite">
        <span class="vb-dot" aria-hidden="true"></span>
        <div class="vb-col"><span class="vb-state">IDLE</span><span class="vb-sub vb-detail">riding the mix</span></div>
        <span class="vb-q" hidden></span>
      </div>
      <div class="vb-cell vb-next">
        <div class="vb-row"><span class="vb-k">NEXT</span><span class="vb-cd">--</span><span class="vb-recipe vb-sub">nothing booked</span><span class="vb-badge vb-planned" hidden>EAR-PLANNED</span></div>
        <div class="vb-track"><div class="vb-lanes"></div><i class="vb-fill"></i><i class="vb-head"></i></div>
        <div class="vb-sub vb-why"></div>
      </div>
      <div class="vb-cell vb-phrase" title="Bar within the 8-bar phrase on the on-air deck">
        <span class="vb-k">BAR</span><span class="vb-num vb-barn">-/8</span>
        <span class="vb-beats" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
        <span class="vb-sub vb-phr">PHR -</span>
      </div>
      <div class="vb-cell vb-energy" title="Measured energy (1-10 vs the library)">
        <div class="vb-row"><span class="vb-k">NRG</span><span class="vb-chips vb-cur" aria-hidden="true">${"<i></i>".repeat(10)}</span><span class="vb-num vb-curn">-</span></div>
        <div class="vb-row"><span class="vb-k">NXT</span><span class="vb-chips vb-nxt" aria-hidden="true">${"<i></i>".repeat(10)}</span><span class="vb-num vb-nxtn">-</span></div>
        <span class="vb-trail" aria-label="Energy of recent songs"></span>
      </div>
      <div class="vb-cell vb-key" title="Camelot keys of both decks">
        <span class="vb-k">KEY</span><span class="vb-num vb-keys">-- · --</span><span class="vb-sub vb-harm"></span>
      </div>
      <div class="vb-cell vb-stemcell">${stemCol("a")}${stemCol("b")}</div>
      <div class="vb-cell vb-hook" hidden><span class="vb-k">DROP</span><span class="vb-num vb-hookcd"></span><span class="vb-sub vb-hooktxt"></span></div>
      <div class="vb-cell vb-badges"></div>
      <ol class="vb-feed" aria-live="polite" aria-label="AI events"></ol>
    </div>`;
  const $ = (sel) => bar.querySelector(sel);
  const el = {
    toggle: $(".vb-toggle"), orb: $(".vb-orb"), state: $(".vb-state"), detail: $(".vb-detail"), q: $(".vb-q"),
    cd: $(".vb-cd"), recipe: $(".vb-recipe"), planned: $(".vb-planned"), lanes: $(".vb-lanes"),
    track: $(".vb-track"), fill: $(".vb-fill"), head: $(".vb-head"), why: $(".vb-why"),
    phrase: $(".vb-phrase"), barn: $(".vb-barn"), beats: [...bar.querySelectorAll(".vb-beats i")], phr: $(".vb-phr"),
    cur: [...bar.querySelectorAll(".vb-cur i")], nxt: [...bar.querySelectorAll(".vb-nxt i")],
    curn: $(".vb-curn"), nxtn: $(".vb-nxtn"), trail: $(".vb-trail"),
    keyCell: $(".vb-key"), keys: $(".vb-keys"), harm: $(".vb-harm"),
    stems: { a: $('.vb-stems[data-deck="a"]'), b: $('.vb-stems[data-deck="b"]') },
    hook: $(".vb-hook"), hookcd: $(".vb-hookcd"), hooktxt: $(".vb-hooktxt"),
    badges: $(".vb-badges"), feed: $(".vb-feed"),
  };

  // write-through caches: the DOM is only touched when a value really changes
  const last = new Map();
  function put(node, prop, v) {
    if (!node) return;
    const k = last.get(node) || {};
    if (k[prop] === v) return;
    k[prop] = v; last.set(node, k);
    if (prop === "text") node.textContent = v;
    else if (prop === "hidden") node.hidden = v;
    else if (prop.startsWith("--")) node.style.setProperty(prop, v);
    else if (prop.startsWith("data-")) node.setAttribute(prop, v);
    else if (prop.startsWith("class:")) node.classList.toggle(prop.slice(6), !!v);
  }

  let hidden = false;
  try { hidden = localStorage.getItem(HIDE_KEY) === "1"; } catch (e) { /* storage off */ }
  function applyHidden() {
    bar.classList.toggle("vb-hidden", hidden);
    el.toggle.setAttribute("aria-pressed", String(!hidden));
  }
  el.toggle.addEventListener("click", () => {
    hidden = !hidden;
    try { localStorage.setItem(HIDE_KEY, hidden ? "1" : "0"); } catch (e) { /* storage off */ }
    applyHidden();
    dirty = true;
  });
  applyHidden();

  // -- signals ---------------------------------------------------------------
  const sig = { gate: null, model: null, yt: null, stems: null, energy: { a: null, b: null, next: null }, trail: [] };
  let feed = [], cues = [], dirty = true, feedDirty = true, lastTrack = null;

  function onEvent(type) {
    return (e) => {
      const d = (e && e.detail) || {};
      if (type === "ai-cue" && Number.isFinite(d.at)) cues = pruneCues(cues, clock()).concat([{ ...d }]);
      if (type === "ai-activity" && d.kind === "stems" && d.deck) flash(d.deck);
      if (type === "ai-activity" && d.kind === "stem-move" && d.deck) flash(d.deck);
      const entry = feedEntry(type, d);
      if (entry) { feed = feedPush(feed, entry, Date.now()); feedDirty = true; }
      dirty = true;
    };
  }
  for (const t of ["ai-activity", "ai-cue", "ear-flush", "seek-refused", "glitch"]) root.addEventListener(t, onEvent(t));
  root.addEventListener("ai-energy", (e) => {
    const d = (e && e.detail) || {};
    if (Number.isFinite(d.a)) {
      if (!sig.trail.length) sig.trail = trailPush(sig.trail, d.a);
      sig.energy.a = d.a;
    }
    if (Number.isFinite(d.b)) { sig.energy.b = d.b; sig.energy.next = d.next || null; }
    dirty = true;
  });

  const flashing = {};
  function flash(id) {
    const g = el.stems[String(id).toLowerCase()];
    if (!g || reduced) return;
    // alternate two animation classes so a fresh flash restarts without a layout read
    const n = flashing[id] = ((flashing[id] || 0) + 1) % 2;
    g.classList.remove("vb-flash-0", "vb-flash-1");
    g.classList.add(`vb-flash-${n}`);
  }

  async function poll(url, key, ms) {
    let busy = false;
    const run = async () => {
      if (busy || hidden || document.hidden) return;
      busy = true;
      const ctl = new AbortController(), to = setTimeout(() => ctl.abort(), 3000);
      try {
        const r = await fetch(url, { signal: ctl.signal });
        if (r.ok) { sig[key] = await r.json(); dirty = true; }
      } catch (e) { /* server busy or restarting: keep the last snapshot */ }
      finally { clearTimeout(to); busy = false; }
    };
    run();
    setInterval(run, ms);
  }
  poll("/api/llm/status", "llm", 2500);
  poll("/api/youtube/status", "yt", 5000);
  poll("/api/stems/status", "stems", 5000);

  // -- render (<= 10 Hz, one rAF per tick) --------------------------------------
  const decks = () => root.decks || {};
  const rateOf = (d) => (d && d._playbackRate && d._playbackRate()) || 1;
  const posOf = (d) => (d && d._currentPosition ? d._currentPosition() : NaN);
  const gainOf = (d) => (d && d.crossfaderGain ? d.crossfaderGain.gain.value : 0);
  function onAirDeck() {
    const ap = root.autopilotState, ds = decks();
    if (ap && ap.active && ds[ap.activeDeck] && ds[ap.activeDeck].playing) return ds[ap.activeDeck];
    let best = null;
    for (const d of Object.values(ds)) if (d && d.playing && (!best || gainOf(d) > gainOf(best))) best = d;
    return best;
  }

  let laneKey = "";
  function renderLanes(lanes) {
    const key = lanes ? JSON.stringify([lanes.total, lanes.lanes.map((l) => l.segs.map((s) => s.owner).join())]) : "";
    if (key === laneKey) return;
    laneKey = key;
    el.lanes.textContent = "";
    el.track.classList.toggle("vb-merge", !!lanes);
    if (!lanes) return;
    for (const l of lanes.lanes) {
      const row = document.createElement("div");
      row.className = "vb-lane";
      const tag = document.createElement("b");
      tag.textContent = l.tag;
      row.appendChild(tag);
      for (const s of l.segs) {
        const seg = document.createElement("i");
        seg.className = `vb-seg vb-own-${s.owner}`;
        seg.style.left = `${(s.from / lanes.total) * 100}%`;
        seg.style.width = `${((s.to - s.from) / lanes.total) * 100}%`;
        seg.title = `${l.stem}: deck ${s.owner.toUpperCase()} (bars ${s.from}-${s.to})`;
        row.appendChild(seg);
      }
      el.lanes.appendChild(row);
    }
    const line = document.createElement("i");
    line.className = "vb-handover";
    line.style.left = `${(lanes.handover / lanes.total) * 100}%`;
    line.title = `handover line (bar ${lanes.handover}), B full at bar ${lanes.total}`;
    el.lanes.appendChild(line);
  }

  function renderNext(now) {
    const ap = root.autopilotState, ds = decks(), mind = root.djMind;
    const out = ap && ap.active ? ds[ap.activeDeck] : null;
    const inn = out ? Object.values(ds).find((d) => d && d !== out) : null;
    const nx = ap && ap.next;
    // a deck keeps its _mergePlan after its merge: only trust it while a Stem Merge is booked
    const plan = inn && nx && /stem merge/i.test(nx.recipe || "") ? inn._mergePlan : null;
    const transitioning = !!(mind && mind.transitioning);
    const cd = out && ap ? countdown(ap.fireAt, posOf(out), rateOf(out)) : null;
    const lanes = mergeLanes(plan);
    renderLanes(lanes);
    if (!nx && !transitioning) {
      put(el.cd, "text", "--"); put(el.recipe, "text", ap && ap.active ? "choosing the next song" : "nothing booked");
      put(el.planned, "hidden", true); put(el.why, "text", ""); put(el.fill, "--vb-p", "0");
      put(el.head, "hidden", true);
      return;
    }
    put(el.cd, "text", transitioning ? "NOW" : cd == null ? "--" : cd > 0 ? `in ${fmtSecs(cd)}` : "NOW");
    const recipe = (nx && nx.recipe) || "";
    put(el.recipe, "text", `${nx && nx.name ? nx.name : "next song"}${recipe ? ` · ${recipe}` : ""}${lanes && lanes.label ? ` · ${lanes.label}` : ""}`);
    put(el.planned, "hidden", !(lanes && lanes.preplanned));
    const whyText = lanes ? `${lanes.heard ? `ear ${lanes.earScore != null ? `${lanes.earScore}/10` : "heard"}` : "unheard"}${lanes.why ? ` · ${lanes.why}` : ""}` : "";
    put(el.why, "text", whyText);
    if (lanes && out) {
      const ph = mergePlayhead(plan, posOf(out), 240 / (out.bpm || 128));
      // MERGE -> HOLD -> HANDOVER: the phase the merge is in, with the planned hold length
      if (ph && ph.phase) put(el.why, "text", `${ph.phase.toUpperCase()}${plan.phases ? ` (hold ${plan.phases.hold.bars} bars)` : ""} · ${whyText}`);
      put(el.head, "hidden", !ph);
      if (ph) put(el.head, "--vb-p", ph.frac.toFixed(3));
      put(el.fill, "--vb-p", ph ? ph.frac.toFixed(3) : "0");
    } else {
      // no lanes: the bar fills over the last 60 s before the fire line
      const f = transitioning ? 1 : cd == null ? 0 : Math.max(0, Math.min(1, 1 - cd / 60));
      put(el.fill, "--vb-p", f.toFixed(3));
      put(el.head, "hidden", true);
    }
  }

  let lastOne = -1, oneFlip = 0;
  function renderPhrase(d) {
    const a = d && d.analysis;
    const p = a ? phraseAt(a.downbeat_times, a.phrase_boundaries_8bar, posOf(d)) : null;
    put(el.barn, "text", p ? `${p.bar}/8` : "-/8");
    put(el.phr, "text", p ? `PHR ${p.phrase} · ${String(d.id).toUpperCase()}` : "PHR -");
    el.beats.forEach((b, i) => put(b, "class:on", !!p && i < p.beat));
    const one = p && p.bar === 1 ? p.phrase : -1;
    if (one !== lastOne) {
      lastOne = one;
      if (one > 0 && !reduced) { oneFlip ^= 1; el.phrase.classList.remove("vb-one-0", "vb-one-1"); el.phrase.classList.add(`vb-one-${oneFlip}`); }
    }
  }

  function renderEnergy() {
    const ap = root.autopilotState;
    if (ap && ap.trackId !== lastTrack) {
      if (lastTrack != null && Number.isFinite(sig.energy.b)) {
        sig.energy.a = sig.energy.b; sig.energy.b = null; sig.energy.next = null;
        sig.trail = trailPush(sig.trail, sig.energy.a);
      }
      lastTrack = ap.trackId;
    }
    const cur = Number.isFinite(sig.energy.a) ? sig.energy.a : ap && Number.isFinite(ap.energy) ? ap.energy : null;
    const nc = energyChips(cur), nn = energyChips(sig.energy.b);
    el.cur.forEach((c, i) => put(c, "class:on", i < nc));
    el.nxt.forEach((c, i) => put(c, "class:on", i < nn));
    put(el.curn, "text", cur == null ? "-" : String(Math.round(cur * 10) / 10));
    put(el.nxtn, "text", Number.isFinite(sig.energy.b) ? String(Math.round(sig.energy.b * 10) / 10) : "-");
    const tk = sig.trail.join();
    if (last.get(el.trail) !== tk) {
      last.set(el.trail, tk);
      el.trail.textContent = "";
      for (const v of sig.trail) {
        const i = document.createElement("i");
        i.style.setProperty("--vb-e", String(energyChips(v) / 10));
        i.title = `energy ${v}`;
        el.trail.appendChild(i);
      }
    }
  }

  function renderKey() {
    const ds = decks();
    const ka = ds.a && ds.a.analysis && ds.a.analysis.key && ds.a.analysis.key.camelot;
    const kb = ds.b && ds.b.analysis && ds.b.analysis.key && ds.b.analysis.key.camelot;
    const fn = root.djMind && root.djMind.core && root.djMind.core.camelotScore;
    const h = harmony(ka, kb, fn);
    put(el.keys, "text", `${ka || "--"} · ${kb || "--"}`);
    put(el.harm, "text", h ? `${h.label} ${h.score.toFixed(2)}` : "");
    put(el.keyCell, "data-harm", h ? (h.ok ? "ok" : "clash") : "none");
  }

  function renderStems() {
    const ds = decks();
    for (const id of ["a", "b"]) {
      const d = ds[id], g = el.stems[id];
      if (!g) continue;
      const lv = stemLevels(d && d.stemState);
      put(g, "class:vb-off", !(d && d.playing));
      put(g, "class:vb-split", !!(d && d.stemState));
      for (const n of STEMS) put(g.querySelector(`[data-stem="${n}"]`), "--vb-l", lv[n].toFixed(2));
    }
  }

  function renderHook(d, now) {
    const cue = nextCue(cues, now, "drop");
    const hook = d && d.stemsReady ? nextHook(d.hookDrops, posOf(d)) : null;
    if (cue) {
      put(el.hook, "hidden", false);
      put(el.hookcd, "text", cue.at > now ? `in ${fmtSecs(cue.at - now)}` : "NOW");
      put(el.hooktxt, "text", `${String(cue.deck || "").toUpperCase()} ${cue.why || ""}`.trim());
    } else if (hook) {
      put(el.hook, "hidden", false);
      put(el.hookcd, "text", `hook ${fmtSecs(hook.in / rateOf(d))}`);
      put(el.hooktxt, "text", `"${hook.text}"`);
    } else put(el.hook, "hidden", true);
  }

  function renderBadges() {
    const b = [];
    const yt = sig.yt;
    if (yt && yt.cooling) {
      const t = yt.until ? new Date(yt.until * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
      b.push(["warn", `YOUTUBE PAUSED${t ? ` until ${t}` : ""}`]);
    }
    const st = sig.stems;
    if (st) {
      const left = (st.backlog || 0) + ((st.urgent && st.urgent.length) || 0) + (st.busy ? 1 : 0);
      if (left) b.push(["ai", `STEMS ${left} left${st.llm_busy ? " (paused: LLM)" : ""}`]);
    }
    const hl = root.djMind && root.djMind.holdLoopInfo && root.djMind.holdLoopInfo();
    if (hl) b.push(["ai", `HOLD LOOP ${hl.bars} bars · ${fmtSecs(hl.secs)}`]);
    const ear = root.liveEar && root.liveEar.last;
    if (ear && ear.decision && Date.now() - ear.at < 30000) {
      const dec = ear.decision;
      b.push(["ai", `EAR ${String(dec.action || "keep").toUpperCase()}${dec.source ? ` (${dec.source})` : ""}`]);
    }
    const key = JSON.stringify(b);
    if (last.get(el.badges) === key) return;
    last.set(el.badges, key);
    el.badges.textContent = "";
    for (const [tone, txt] of b) {
      const s = document.createElement("span");
      s.className = `vb-badge vb-${tone}`;
      s.textContent = txt;
      el.badges.appendChild(s);
    }
  }

  function renderOrb() {
    const ap = root.autopilotState, mind = root.djMind, llm = sig.llm || {};
    const ear = root.liveEar && root.liveEar.last;
    const s = aiState({
      transitioning: !!(mind && mind.transitioning),
      preplanning: ap && ap.preplanning,
      recipe: ap && ap.next && ap.next.recipe,
      gate: llm.gate, model: llm,
      holdLoop: !!(mind && mind.holdLoopInfo && mind.holdLoopInfo()),
      earRecent: !!(ear && Date.now() - ear.at < 4000),
    });
    put(el.orb, "data-state", s.state);
    put(el.state, "text", s.label);
    put(el.detail, "text", s.detail);
    put(el.q, "hidden", !s.queue);
    put(el.q, "text", s.queue ? `+${s.queue} queued` : "");
  }

  const rendered = new Map();       // feed id -> li
  function renderFeed(now) {
    const pruned = feedPrune(feed, now);
    if (pruned.length !== feed.length) { feed = pruned; feedDirty = true; }
    if (!feedDirty) return;
    feedDirty = false;
    const keep = new Set(feed.map((x) => x.id));
    for (const [id, li] of rendered) if (!keep.has(id)) { li.remove(); rendered.delete(id); }
    let prev = null;
    for (const x of feed) {
      let li = rendered.get(x.id);
      if (!li) {
        li = document.createElement("li");
        li.className = `vb-ev${x.warn ? " vb-warn" : ""}`;
        li.innerHTML = '<b></b><span class="vb-sub"></span><em></em>';
        rendered.set(x.id, li);
      }
      li.children[0].textContent = x.label;
      li.children[1].textContent = x.why ? String(x.why).slice(0, 90) : "";
      li.children[2].textContent = x.count > 1 ? `×${x.count}` : "";
      const ref = prev ? prev.nextSibling : el.feed.firstChild;
      if (li !== ref) el.feed.insertBefore(li, ref);
      prev = li;
    }
  }

  let queued = false;
  function frame() {
    queued = false;
    if (hidden) return;
    const now = clock();
    cues = pruneCues(cues, now);
    const d = onAirDeck();
    renderOrb();
    renderNext(now);
    renderPhrase(d);
    renderEnergy();
    renderKey();
    renderStems();
    renderHook(d, now);
    renderBadges();
    renderFeed(Date.now());
    dirty = false;
  }
  // 10 Hz: live values (countdown, bar, meters) move every tick while a deck plays;
  // with nothing playing only a real signal (dirty) or feed expiry triggers work
  setInterval(() => {
    if (queued || hidden || document.hidden) return;
    const playing = Object.values(decks()).some((x) => x && x.playing);
    if (!playing && !dirty && !feed.length) return;
    queued = true;
    requestAnimationFrame(frame);
  }, 100);

  root.vibeUi = { core, get hidden() { return hidden; } };
})(typeof window !== "undefined" ? window : globalThis);
