// AI Music Brain - LYRIC REMIX (web executor). The plan (which sung lines, in which order, on which beats) is decided
// by dj-core (fn "lyric_remix_plan", reached through POST /api/lyric_remix/plan) from the precomputed data file that
// ships with the knowledge snapshot. THIS file decides nothing: it builds the request context from the deck, asks, and
// arms the returned plan on the audio clock (vocal stem slices of A, and slices of B's vocal stem into A's vocal chain).
//
// Called once per phrase by learned-moves.js tick when that booked nothing. Never blocks or delays a transition or
// another move: a refusal, a late plan or any exception is a quiet console.info and nothing happens.
(function (root) {
  "use strict";

  const LABEL = "LYRIC REMIX", MOVE = "lyric_remix";
  const MIN_LEAD_S = 0.35;           // the window must start at least this far ahead on the audio clock
  const EXIT_GUARD_BARS = 4;         // a cross-deck window ends this many bars before A's planned exit
  const DEFAULT_RTT_S = 0.35;        // until a round trip was measured
  const EDGE_S = 0.02, HP_HZ = 120;

  // ------------------------------------------------------------------ pure helpers (node-tested)
  const num = (x, d = 0) => (Number.isFinite(x) ? x : d);

  // The request context of the song on air (mode "song"). `i` is plain measured values from the deck / phrase / session.
  // The position is advanced by the estimated round trip (min MIN_LEAD_S) so the plan is for where the deck will be.
  function songCtx(i) {
    const rate = num(i.rate, 1) || 1, adv = Math.max(MIN_LEAD_S, num(i.rttS, DEFAULT_RTT_S));
    const used = Array.isArray(i.used) ? i.used.slice() : [];
    return {
      pos: num(i.pos) + adv * rate, rate, bpm: num(i.bpm, 0), bBpm: null, bar: num(i.bar), lineT: num(i.lineT), exitT: i.exitT == null ? null : num(i.exitT),
      entryT: num(i.entryT), winStart: null, winEnd: null, stemsLive: !!i.stemsLive, guestStems: false, vocalBusy: !!i.vocalBusy,
      drops: i.drops == null ? null : i.drops, vocals: i.vocals == null ? null : i.vocals, barsOnTrack: num(i.barsOnTrack), used,
      count: num(i.count), lastAtBar: i.lastAtBar == null ? null : i.lastAtBar, atBar: num(i.atBar), variant: num(i.variant),
      donePlans: Array.isArray(i.donePlans) ? i.donePlans.slice() : [], mode: "song", relaxed: !!i.relaxed, mashupActive: !!i.mashupActive,
      deferring: !!i.deferring, onDemand: false,
    };
  }
  // The cross-deck variant: B's tempo and vocal stem, and a window that ends EXIT_GUARD_BARS before A's exit (A's exit is
  // untouched). null when there is no window (no exit, B not usable).
  function transitionCtx(c, b) {
    if (!b || !b.id || !(b.bpm > 0) || !b.guestStems || !Number.isFinite(c.exitT)) return null;
    const winEnd = c.exitT - EXIT_GUARD_BARS * c.bar, winStart = c.pos;
    if (!(winEnd > winStart)) return null;
    return Object.assign({}, c, { mode: "transition", bBpm: b.bpm, guestStems: true, winStart, winEnd });
  }
  // The overlap variant (both decks playing inside the transition): `ov` = {winStart, winEnd, vocalSwap} on A's source clock as the host
  // reports them. null when the bounds are unknown / empty or B is not usable. vocalSwap: undefined -> null (no swap planned).
  function overlapCtx(c, b, ov) {
    if (!ov || !b || !b.id || !(b.bpm > 0) || !b.guestStems) return null;
    const winStart = Math.max(num(ov.winStart, NaN), c.pos), winEnd = num(ov.winEnd, NaN);
    if (!(winEnd > winStart)) return null;
    return Object.assign({}, c, { mode: "overlap", bBpm: b.bpm, guestStems: true, guestPlaying: true, winStart, winEnd,
      vocalSwap: ov.vocalSwap == null ? null : ov.vocalSwap, takeOverSwap: true });
  }
  // The body of POST /api/lyric_remix/plan.
  const body = (aId, bId, ctx) => ({ a: aId, b: bId || null, ctx });

  // A plan's slices split by source: self (A's own vocal stem) and guest (B's vocal stem).
  function splitSlices(plan) {
    const all = (plan && Array.isArray(plan.slices)) ? plan.slices : [];
    return { self: all.filter((s) => s && s.src !== "guest"), guest: all.filter((s) => s && s.src === "guest") };
  }
  // The audio-clock timing of one slice: it starts at `at + off / rate` and lasts `dur / rate` (A source seconds on the
  // deck clock). A guest slice plays B's buffer at slice.rate * deckRate from (from + lag) * ratio.
  function sliceTiming(s, at, deckRate, stems) {
    const rate = deckRate > 0 ? deckRate : 1, t0 = at + num(s.off) / rate, len = num(s.dur) / rate;
    const out = { t0, t1: t0 + len, len, gain: s.gain == null ? 1 : s.gain };
    if (s.src === "guest") {
      const k = (stems && stems.ratio) || 1;
      out.rate = num(s.rate, 1) * rate;
      out.offset = Math.max(0, (num(s.from) + ((stems && stems.lag) || 0)) * k);
    }
    return out;
  }
  // the window of a plan on the audio clock: {at, until}; `at` is the audio time of plan.start
  const windowOf = (plan, at, deckRate) => ({ at, until: at + (num(plan.end) - num(plan.start)) / (deckRate > 0 ? deckRate : 1) });
  // an arm is allowed only with at least MIN_LEAD_S of lead; -> null | {gate, reason}
  function leadRefusal(at, now, min = MIN_LEAD_S) {
    const lead = at - now;
    return lead >= min - 1e-9 ? null : { gate: "late", reason: `the window starts in ${lead.toFixed(2)} s (min ${min})` };
  }
  // per-set + per-deck bookkeeping after a plan was armed
  function noteDone(r, donePlans, plan, atBar) {
    r.used.push(MOVE); r.count++; r.lastAtBar = atBar;
    if (plan.plan_id && !donePlans.includes(plan.plan_id)) donePlans.push(plan.plan_id);
  }
  const statusText = (plan) => `Lyric remix: ${plan.text || ""}`.trim();

  // A lyric remix somebody asked for (the Lyric remixes list or an AI pick): `{plan, a, b|null, seen}`. The plan id goes to dj-core as
  // ctx.wantPlan (only that plan, the song cooldowns and the relaxed session do not apply); a within-song plan only on its own song and
  // not in a transition pass, a pair plan only when its B is the booked next song.
  function wantPlanFor(w, aId, bId, transition) {
    if (!w || w.a !== aId) return null;
    if (w.b) return bId === w.b ? w.plan : null;
    return !bId && !transition ? w.plan : null;
  }
  // Gates that can clear by themselves (stems load, the window comes, the other move ends): the request is kept and retried at the next
  // phrase; any other gate (language, quality, tempo gap, no plan ...) drops it. Same list as dj-core retryGates.
  const RETRY_GATES = ["stems_not_live", "guest_stems", "busy", "vocal_layer", "late", "wait", "no_room", "vocal_owner", "no_stem_bus", "unavailable"];
  const retryable = (gate) => RETRY_GATES.includes(gate);
  // The "getting stems" / "waiting" line for a refusal of a requested remix, null when the request was dropped for good.
  function wantStatus(gate, reason) {
    if (gate === "stems_not_live" || gate === "guest_stems" || gate === "deferring") return "Getting stems for the remix";
    if (retryable(gate)) return "Remix waiting for its moment";
    return `Remix not played: ${reason || gate}`;
  }

  const HAS_WAIT_MS = 400;           // the first eligible phrase waits this long (once) for /has before it is skipped
  const core = { songCtx, transitionCtx, overlapCtx, wantPlanFor, retryable, wantStatus, HAS_WAIT_MS, body, splitSlices, sliceTiming, windowOf, leadRefusal, noteDone, statusText, MIN_LEAD_S, EXIT_GUARD_BARS };
  root.lyricRemixCore = core;
  if (typeof module !== "undefined" && module.exports) module.exports = core;

  // ------------------------------------------------------------------ runtime (Host port only)
  function create({ host }) {
    const audioCtx = host.audio;
    const { setTimeout, clearTimeout } = host.clock;
    const timers = { a: [], b: [] };
    const hasCache = new Map();                      // song id -> {within, pair} counts (promise while loading), cleared on song change
    const donePlans = [];                            // plan ids played in this set (sent as ctx.donePlans)
    const live = { a: null, b: null };               // armed guest sources / gains per deck, for stop()
    const waited = new Set();                        // "a|b" pairs whose /has was already awaited once
    const said = new Set();                          // "deck:phrase" refusals already logged
    const seen = { a: null, b: null };                // track id last seen per deck
    let rttS = DEFAULT_RTT_S;
    let want = null;                                 // the requested remix {plan, a, b, seen}, until it plays or its song leaves the air
    let wantStatusText = "";
    const setStatus = (t) => { wantStatusText = t || ""; try { host.bus.emit("lyric-remix-status", { text: wantStatusText }); } catch (e) { /* optional */ } };
    function request(w) {
      if (!w || !w.plan || !w.a) return false;
      want = { plan: String(w.plan), a: String(w.a), b: w.b ? String(w.b) : null, seen: false };
      setStatus("Waiting for the song");
      return true;
    }
    function clearWant() { want = null; setStatus(""); }
    const wantsDeck = (d) => !!(want && d && trackOf(d) === want.a);

    const other = (id) => (id === "a" ? "b" : "a");
    const trackOf = (d) => (host.state && host.state[d.id === "a" ? "trackA" : "trackB"]) || d.trackId || null;
    const cancel = (id) => { (timers[id] || []).forEach(clearTimeout); timers[id] = []; };
    const post = (url, b) => host.api.fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) }).then((r) => r.json());

    // GET /has per (a, b) pair key, cached; entries of other songs are dropped on song change
    function has(aId, bId) {
      const key = `${aId}|${bId || ""}`;
      if (!hasCache.has(key)) {
        if (hasCache.size > 40) hasCache.clear();
        const p = host.api.fetch(`/api/lyric_remix/has?a=${encodeURIComponent(aId)}&b=${encodeURIComponent(bId || "")}`).then((r) => r.json())
          .then((j) => { const v = { within: num(j && j.within), pair: num(j && j.pair) }; hasCache.set(key, v); return v; },
            () => { hasCache.delete(key); return { within: 0, pair: 0 }; });
        hasCache.set(key, p);
      }
      return hasCache.get(key);
    }
    const cached = (aId, bId) => { const v = has(aId, bId); return v && typeof v.then !== "function" ? v : null; };
    // Start /has for the song on air and the booked next song (any deck loading a track may call it). Never throws.
    function prefetch(d) {
      try {
        const aId = d && trackOf(d), nId = d && trackOf(host.decks[other(d.id)] || {});
        if (!aId) return;
        if (seen[d.id] !== aId) { seen[d.id] = aId; hasCache.clear(); }
        has(aId, null); if (nId) has(aId, nId);
      } catch (e) { /* optional */ }
    }
    // The overlap bounds / swap window as the host reports them: djMind.overlapInfo() -> {winStart, winEnd, vocalSwap}. null = unknown.
    let saidNoOverlap = false;
    function overlapInfo(d) {
      const mind = host.mod.djMind;
      if (!(mind && mind.transitioning)) return null;
      const b = host.decks && host.decks[other(d.id)];
      if (!b || !b.playing || !d.playing || !(b.stems && b.stems.vocals)) return null;
      const oi = typeof mind.overlapInfo === "function" ? mind.overlapInfo() : null;
      if (!oi) { if (!saidNoOverlap) { saidNoOverlap = true; console.info("lyric remix overlap skipped: the host exposes no overlap bounds"); } return null; }
      return oi;
    }
    // 20 ms ramps: deck x's live vocal stem is muted across [at, until] and restored after
    function muteVocal(x, at, until) {
      const g = x && x.stemLive && x.stemLive.vocals && x.stemLive.vocals.gain;
      if (!g) return false;
      g.cancelScheduledValues(at);
      g.setValueAtTime(g.value, at); g.linearRampToValueAtTime(0, at + 0.02);
      g.setValueAtTime(0, Math.max(at + 0.02, until - 0.02)); g.linearRampToValueAtTime(1, until);
      return true;
    }
    function cancelSwap() {
      const sm = host.mod.stemMoves, ap = host.mod.autopilot;
      for (const o of [sm, ap]) if (o && typeof o.cancelVocalSwap === "function") { try { o.cancelVocalSwap(); return true; } catch (e) { /* try next */ } }
      console.info("lyric remix: no swap cancel hook in the host");
      return false;
    }

    function deckInputs(d, o) {
      const st = o.st || {}, r = d._learned || (d._learned = { used: [], count: 0, lastAtBar: null, variant: 0 });
      const DL = root.dropLineCore || null;
      const atBar = (o.lineT - (o.entryT || 0)) / o.bar;
      const rate = (d._playbackRate && d._playbackRate()) || 1;
      return {
        pos: o.pos, rate, bpm: d.bpm || (d.analysis && d.analysis.bpm) || 0, bar: o.bar, lineT: o.lineT, exitT: o.exitT, entryT: o.entryT || 0,
        barsOnTrack: st.barsOnTrack != null ? st.barsOnTrack : atBar, atBar, stemsLive: !!(d.stemsReady && d.stemsLiveAt && d.stemsLiveAt(audioCtx.currentTime + 0.5)),
        vocalBusy: !!((d._slices && d._slices.vocals) || (d._holds && d._holds.vocals) || d._artistSwap || d._lyricRemix),
        drops: DL && DL.dropSpans && d.analysis ? DL.dropSpans(d.analysis, d.bpm || d.analysis.bpm) : null,
        vocals: d.analysis && d.analysis.vocal_active_regions, used: r.used, count: r.count, lastAtBar: r.lastAtBar, variant: r.variant,
        donePlans, relaxed: !!(host.session && host.session.relaxed), mashupActive: !!st.mashupActive,
        deferring: !!(host.session && host.session.deferring), rttS,
      };
    }
    function guestInfo(d) {
      const b = host.decks && host.decks[other(d.id)];
      if (!b || !b.stems || !b.stems.vocals || !(b.bpm > 0)) return null;
      const id = trackOf(b);
      return id ? { id, bpm: b.bpm, guestStems: true, deck: b } : null;
    }

    // Arm `plan` on deck d. -> {ok, at, until} | {ok:false, gate, reason}. Nothing is booked on a refusal.
    function arm(d, plan) {
      const sm = host.mod.stemMoves;
      if (!plan || plan.ok !== true || !Array.isArray(plan.slices) || !plan.slices.length) return { ok: false, gate: "plan", reason: "no usable plan" };
      const at = sm.audioAt(d, plan.start), late = leadRefusal(at, audioCtx.currentTime);
      if (late) return Object.assign({ ok: false }, late);
      if (!d.stemsLiveAt || !d.stemsLiveAt(at)) return { ok: false, gate: "stems_not_live", reason: "the deck's stems are not sounding at the window start" };
      if ((d._slices && d._slices.vocals) || (d._holds && d._holds.vocals) || d._artistSwap || d._lyricRemix) return { ok: false, gate: "busy", reason: "the vocal stem is already sliced / held" };
      const { self, guest } = splitSlices(plan);
      const b = host.decks && host.decks[other(d.id)], bufB = b && b.stems && b.stems.vocals;
      if (guest.length && !bufB) return { ok: false, gate: "guest_stems", reason: "B's vocal buffer is missing" };
      if (guest.length && !(d.stemGain && d.stemGain.vocals && d.stemLive && d.stemLive.vocals)) return { ok: false, gate: "no_stem_bus", reason: "the deck's vocal stem chain is missing" };
      const mute = Array.isArray(plan.mute_decks) ? plan.mute_decks.filter((x) => x === "a" || x === "b").map((x) => host.decks && host.decks[x]) : [];
      if (mute.some((x) => !(x && x.stemLive && x.stemLive.vocals && x.stemLive.vocals.gain))) return { ok: false, gate: "stems_not_live", reason: "a deck to mute has no live vocal stem" };
      const rate = (d._playbackRate && d._playbackRate()) || 1, { until } = windowOf(plan, at, rate);
      const rec = { srcs: [], gains: [], until, muted: mute };
      d._lyricRemix = rec;
      cancel(d.id);
      timers[d.id].push(setTimeout(() => {
        if (!d.playing || d._lyricRemix !== rec) return;
        if (!d.stemMix({}, at - 0.01, 0.01)) { d._lyricRemix = null; return void console.info("lyric remix skipped: stem mode refused"); }
        const liveGain = d.stemLive.vocals.gain;
        let selfUntil = null;
        if (self.length) {                                // A's own pieces; stemSlices mutes the live vocal across them
          const res = d.stemSlices("vocals", self.map((s) => ({ from: s.from, dur: s.dur, off: s.off, gain: s.gain })), at);
          if (!res) { d._lyricRemix = null; d.stemMix(null, at, 0.02); return void console.info("lyric remix skipped: slices refused"); }
          selfUntil = res.until;
        }
        const bus = d.stemGain.vocals, ctxB = b && b.stems;
        for (const s of guest) {                          // B's pieces, high-passed, into A's vocal chain
          const t = sliceTiming(s, at, rate, ctxB), src = audioCtx.createBufferSource(), hp = audioCtx.createBiquadFilter(), g = audioCtx.createGain();
          const e = Math.min(EDGE_S, (t.t1 - t.t0) / 3);
          src.buffer = bufB; src.playbackRate.value = t.rate;
          hp.type = "highpass"; hp.frequency.value = HP_HZ;
          g.gain.setValueAtTime(0, t.t0); g.gain.linearRampToValueAtTime(t.gain, t.t0 + e);
          g.gain.setValueAtTime(t.gain, t.t1 - e); g.gain.linearRampToValueAtTime(0, t.t1);
          src.connect(hp); hp.connect(g); g.connect(bus);
          src.start(t.t0, Math.min(t.offset, Math.max(0, bufB.duration - 0.01)));
          src.stop(t.t1 + 0.02);
          rec.srcs.push(src); rec.gains.push(g);
          src.onended = () => { try { g.disconnect(); } catch (err) { /* gone */ } };
        }
        // the live vocal stays muted across the whole window, also where only a guest piece (or the tail of one) plays
        if (selfUntil == null || until > selfUntil + 0.001) {
          const from = selfUntil == null ? at : selfUntil - 0.008;
          liveGain.cancelScheduledValues(from);
          if (selfUntil == null) { liveGain.setValueAtTime(1, at); liveGain.linearRampToValueAtTime(0, at + 0.008); }
          else liveGain.setValueAtTime(0, from);
          liveGain.setValueAtTime(0, until - 0.008); liveGain.linearRampToValueAtTime(1, until);
        }
        for (const x of mute) muteVocal(x, at, until);   // both decks' live vocal off: the pieces are the only lead vocal
        if (plan.replaces_swap) cancelSwap();
        timers[d.id].push(setTimeout(() => {
          if (d._lyricRemix === rec) d._lyricRemix = null;
          if (d.playing) d.stemMix(null, until + 0.03, 0.02);
        }, Math.max(0, (until - audioCtx.currentTime) * 1000 - 200)));
      }, Math.max(0, (at - audioCtx.currentTime) * 1000 - 250)));
      live[d.id] = rec;
      return { ok: true, at, until };
    }

    function say(d, plan, run, atBar) {
      const why = statusText(plan);
      console.info(`lyric remix ${plan.mode}: ${plan.why || why}`);
      host.log.step("lyric_remix", { deck: d.id, decision: "lyric_remix", why, result: { plan_id: plan.plan_id, mode: plan.mode, beats: plan.beats, score: plan.score, t0: run.at, t1: run.until } });
      host.bus.emit("ai-activity", { kind: "learned_move", label: LABEL, why, move: MOVE, deck: d.id, t0: run.at, t1: run.until });
      host.bus.emit("vis-moment", { at: run.at, name: LABEL, tier: "accent", deck: d.id });
      host.ui.status(why);
    }

    // One call per phrase (learned-moves.js tick, only when it booked nothing). -> {kind, why, busyS} when armed, else null.
    // Asynchronous work (the /has check and the POST) is fire-and-forget: the caller never waits on it; the plan is armed
    // from the answer if the window is still ahead by then.
    function tick(d, o) {
      try {
        if (!d || !d.playing || !o) return null;
        const aId = trackOf(d);
        if (!aId) return null;
        if (seen[d.id] !== aId) { seen[d.id] = aId; hasCache.clear(); }          // song change: ask again
        const g = guestInfo(d), bId = g && g.id;
        prefetch(d);                                       // song on air + booked next: /has starts as early as possible
        const nId = trackOf(host.decks[other(d.id)] || {}), wkey = `${aId}|${nId || ""}`;
        const pending = [];
        if (!cached(aId, null)) pending.push(has(aId, null));
        if (bId && !cached(aId, bId)) pending.push(has(aId, bId));
        if (pending.length && !waited.has(wkey)) {          // await once, capped: the first eligible phrase can still plan
          waited.add(wkey); if (waited.size > 200) waited.clear();
          let tm; const cap = new Promise((res) => { tm = setTimeout(res, HAS_WAIT_MS); });
          Promise.race([Promise.all(pending), cap]).then(() => { clearTimeout(tm); if (d.playing && trackOf(d) === aId) plan_(d, o, aId, g, bId); }).catch(() => {});
          return null;
        }
        plan_(d, o, aId, g, bId);
      } catch (e) {
        console.info("lyric remix tick failed -", e && e.message);
      }
      return null;
    }
    function plan_(d, o, aId, g, bId) {
      try {
        if (want && want.a !== aId && want.seen) clearWant();             // its song left the air without it
        const asked = !!(want && want.a === aId);
        if (asked) want.seen = true;
        const within = cached(aId, null), pair = bId ? cached(aId, bId) : null;
        const wpSong = wantPlanFor(want, aId, null, false), wpPair = wantPlanFor(want, aId, bId, true);
        if (!asked && !within && !pair) return;                            // /has not answered within the cap: skip this phrase
        const wantTransition = asked ? !!wpPair : !!(pair && pair.pair > 0), wantSong = asked ? !!wpSong : !!(within && within.within > 0);
        if (!wantTransition && !wantSong) return;
        const inp = deckInputs(d, o), base = songCtx(inp);
        const key = `${d.id}:${o.phrase}`;
        let tries = [];
        const withWant = (c, plan) => (plan ? Object.assign(c, { wantPlan: plan }) : c);
        if (wantTransition || wantSong) { const oc = overlapCtx(base, g, overlapInfo(d)); if (oc) tries.push({ bId, ctx: withWant(oc, wpPair), want: wpPair }); }
        if (wantTransition) { const tc = transitionCtx(base, g); if (tc) tries.push({ bId, ctx: withWant(tc, wpPair), want: wpPair }); }
        if (wantSong) tries.push({ bId: null, ctx: withWant(Object.assign({}, base), wpSong), want: wpSong });
        if (asked) tries = tries.filter((t) => t.want);               // a request only runs its own plan
        let lastRefusal = null;
        const refused = () => { if (asked && lastRefusal) setStatus(wantStatus(lastRefusal.gate, lastRefusal.reason)); if (asked && lastRefusal && !retryable(lastRefusal.gate)) want = null; };
        const run = (i) => {
          if (i >= tries.length) return refused();
          const t = tries[i], t0 = host.clock.now();
          post("/api/lyric_remix/plan", body(aId, t.bId, t.ctx)).then((res) => {
            rttS = Math.max(0.05, Math.min(2, (host.clock.now() - t0) / 1000));
            if (!res || res.ok !== true) {
              lastRefusal = { gate: res && res.gate, reason: res && res.reason };
              if (!said.has(key)) { said.add(key); if (said.size > 200) said.clear(); console.info(`lyric remix skipped: ${res && res.gate}: ${res && res.reason}`); }
              return run(i + 1);
            }
            if (!d.playing || trackOf(d) !== aId) return;
            const r = d._learned || (d._learned = { used: [], count: 0, lastAtBar: null, variant: 0 });
            const armed = arm(d, res);
            if (!armed.ok) { lastRefusal = { gate: armed.gate, reason: armed.reason }; console.info(`lyric remix skipped: ${armed.gate}: ${armed.reason}`); return run(i + 1); }
            noteDone(r, donePlans, res, inp.atBar);
            if (t.want && want && want.plan === t.want) clearWant();
            say(d, res, armed, inp.atBar);
          }).catch((e) => { lastRefusal = { gate: "unavailable", reason: e && e.message }; console.info("lyric remix unavailable -", e && e.message); refused(); });
        };
        run(0);
      } catch (e) {
        console.info("lyric remix tick failed -", e && e.message);
      }
    }

    function stop(d) {
      try {
        if (!d) return;
        cancel(d.id);
        const rec = d._lyricRemix || live[d.id];
        if (rec) {
          const t = audioCtx.currentTime;
          for (const s of rec.srcs) { try { s.stop(t + 0.03); } catch (e) { /* already stopped */ } }
          for (const g of rec.gains) { try { g.disconnect(); } catch (e) { /* gone */ } }
          const lg = d.stemLive && d.stemLive.vocals && d.stemLive.vocals.gain;
          for (const x of rec.muted || []) {
            const g = x && x.stemLive && x.stemLive.vocals && x.stemLive.vocals.gain;
            if (g && x !== d) { g.cancelScheduledValues(t); g.setValueAtTime(g.value, t); g.linearRampToValueAtTime(1, t + 0.03); }
          }
          if (lg && d._lyricRemix === rec) { lg.cancelScheduledValues(t); lg.setValueAtTime(lg.value, t); lg.linearRampToValueAtTime(1, t + 0.03); }
          if (d._lyricRemix === rec) { d._lyricRemix = null; if (d.stemMix) d.stemMix(null, 0, 0.02); }
          live[d.id] = null;
        }
      } catch (e) { console.info("lyric remix stop failed -", e && e.message); }
    }
    const api = { core, tick, arm, stop, cancel, prefetch, request, clear: clearWant, wantsDeck, get want() { return want; }, get status() { return wantStatusText; },
      get donePlans() { return donePlans; } };
    host.mod.lyricRemix = api;
    return api;
  }
  if (root.Engine) root.Engine.mount("lyricRemix", create);
})(typeof window !== "undefined" ? window : globalThis);
