// Per-song AI step log, browser half.
//
// User: "you need to log each step that ai took for song, save waveform alongside,
// for future analysis to improve our algorithm". The server (app/ui/song_log.py) files
// every step under its song and draws the waveform; this module only collects:
//   - song start / end: which deck is audible with which track (polled once a second)
//   - existing signals: "ai-activity" (stem moves, dj-mind decisions, learned picks ...),
//     "ai-cue" (drops, transitions, lines), "ai-supermove", "seek-refused", "deck-track-loaded"
//   - window.aiStep(kind, data) for the few places with no event (autopilot.js calls it)
// Steps are queued and POSTed in batches (/api/session/steps, keepalive). Never throws,
// never awaits on the audio path: logging must not break or slow a live set.
(function (root) {
  const MAX_BATCH = 50;          // steps per POST (server takes up to 200)
  const MAX_BYTES = 60000;       // keepalive bodies must stay under 64 KB
  const MAX_QUEUE = 500;         // offline / slow server: oldest steps go first
  const FLUSH_MS = 2000;
  const POLL_MS = 1000;
  const OFF_POLLS = 3;           // a deck silent this many polls in a row = its song ended
  const AUDIBLE = 0.05;          // crossfader gain above this = on air
  const MAX_STR = 300, MAX_BLOB = 2000;

  function small(v) {
    if (v === undefined) return undefined;
    try {
      const s = JSON.stringify(v);
      if (s === undefined) return undefined;
      return s.length <= MAX_BLOB ? JSON.parse(s) : s.slice(0, MAX_BLOB) + "...";
    } catch (e) { return String(v).slice(0, MAX_BLOB); }
  }

  // Pure: one step in the server's shape.
  function payload(kind, data, now) {
    const d = data || {};
    const out = { kind: String(kind || "step").slice(0, 40), t: now };
    for (const k of ["track_id", "deck", "decision", "why", "phase", "name"]) {
      if (d[k] !== undefined && d[k] !== null && d[k] !== "") out[k] = String(d[k]).slice(0, MAX_STR);
    }
    if (Number.isFinite(d.at_song)) out.at_song = Math.round(d.at_song * 100) / 100;
    const inputs = small(d.inputs), result = small(d.result);
    if (inputs !== undefined) out.inputs = inputs;
    if (result !== undefined) out.result = result;
    return out;
  }

  // Pure: bounded queue.
  function push(q, item, max = MAX_QUEUE) {
    q.items.push(item);
    if (q.items.length > max) { const n = q.items.length - max; q.items.splice(0, n); q.dropped = (q.dropped || 0) + n; }
    return q;
  }

  // Pure: the next batch, bounded by count and by body size.
  function takeBatch(q, maxN = MAX_BATCH, maxBytes = MAX_BYTES) {
    const out = [];
    let bytes = 12;
    while (q.items.length && out.length < maxN) {
      const n = JSON.stringify(q.items[0]).length + 1;
      if (out.length && bytes + n > maxBytes) break;
      out.push(q.items.shift());
      bytes += n;
    }
    return out;
  }

  // Pure: song start / end from one poll. snap: [{deck, track_id, on, pos, name}].
  function songTick(state, snap, offPolls = OFF_POLLS) {
    const ev = [];
    for (const s of snap) {
      const cur = state[s.deck] || { id: null, off: 0, pos: null };
      const on = s.on && s.track_id ? s.track_id : null;
      if (on && on === cur.id) {
        cur.off = 0;
      } else if (on) {
        if (cur.id) ev.push({ kind: "song_end", deck: s.deck, track_id: cur.id, at_song: cur.pos });
        ev.push({ kind: "song_start", deck: s.deck, track_id: on, at_song: s.pos, name: s.name });
        cur.id = on; cur.off = 0;
      } else if (cur.id) {
        cur.off += 1;
        if (cur.off >= offPolls || (s.track_id && s.track_id !== cur.id)) {
          ev.push({ kind: "song_end", deck: s.deck, track_id: cur.id, at_song: cur.pos });
          cur.id = null; cur.off = 0;
        }
      }
      if (on) cur.pos = s.pos;
      state[s.deck] = cur;
    }
    return ev;
  }

  const core = { payload, push, takeBatch, songTick, small, MAX_BATCH, MAX_BYTES, MAX_QUEUE, OFF_POLLS };

  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (!root || typeof root.addEventListener !== "function" || typeof fetch !== "function") return;

  // ------------------------------------------------------------------ browser
  const q = { items: [], dropped: 0 };
  const deckTrack = {};             // deck letter -> track id loaded there
  const songState = {};

  function deckOf(id) { return (root.decks && root.decks[id]) || null; }
  function posOf(id) {
    const d = deckOf(id);
    try { return d && d._currentPosition ? +d._currentPosition() : undefined; } catch (e) { return undefined; }
  }

  function aiStep(kind, data) {
    try {
      const d = Object.assign({}, data || {});
      if (d.deck && !d.track_id && deckTrack[d.deck]) d.track_id = deckTrack[d.deck];
      if (d.deck && !Number.isFinite(d.at_song)) d.at_song = posOf(d.deck);
      push(q, payload(kind, d, Date.now() / 1000));
      if (q.items.length >= MAX_BATCH) flush();
    } catch (e) { /* logging never breaks the set */ }
  }

  function flush() {
    try {
      while (q.items.length) {
        const steps = takeBatch(q);
        if (!steps.length) break;
        fetch("/api/session/steps", {
          method: "POST", headers: { "Content-Type": "application/json" }, keepalive: true,
          body: JSON.stringify({ steps }),
        }).catch(() => {});
      }
    } catch (e) { /* never */ }
  }

  function poll() {
    try {
      const snap = [];
      for (const d of Object.values(root.decks || {})) {
        if (!d || !d.id) continue;
        const gain = d.crossfaderGain ? d.crossfaderGain.gain.value : 1;
        const el = root.document && root.document.getElementById(`title-${d.id}`);
        snap.push({ deck: d.id, track_id: deckTrack[d.id] || null, on: !!d.playing && gain > AUDIBLE,
                    pos: posOf(d.id), name: el ? el.textContent.trim() : undefined });
      }
      for (const e of songTick(songState, snap)) aiStep(e.kind, e);
    } catch (e) { /* never */ }
  }

  const rest = (o, drop) => { const r = {}; for (const k of Object.keys(o || {})) if (!drop.includes(k)) r[k] = o[k]; return r; };

  if (root.document) {
    root.document.addEventListener("deck-track-loaded", (e) => {
      const d = e.detail || {};
      if (!d.deck || !d.trackId) return;
      deckTrack[d.deck] = String(d.trackId);
      aiStep("load", { deck: d.deck, track_id: d.trackId, phase: "planning", decision: `loaded on deck ${String(d.deck).toUpperCase()}` });
    });
  }
  root.addEventListener("ai-activity", (e) => {
    const d = e.detail || {};
    aiStep(d.kind === "decision" ? "mind" : (d.kind || "activity"), {
      deck: d.deck, decision: d.label || d.action, why: d.why, inputs: rest(d, ["kind", "deck", "label", "action", "why"]),
    });
  });
  root.addEventListener("ai-cue", (e) => {
    const d = e.detail || {};
    const ctx = root.audioCtx;
    const ahead = ctx && Number.isFinite(d.at) ? d.at - ctx.currentTime : 0;
    const p = d.deck ? posOf(d.deck) : undefined;
    aiStep(`cue_${d.kind || "cue"}`, { deck: d.deck, decision: d.kind, why: d.why,
      at_song: Number.isFinite(p) ? p + Math.max(0, ahead) : undefined, inputs: rest(d, ["kind", "deck", "why", "at"]) });
  });
  root.addEventListener("ai-supermove", (e) => {
    const d = e.detail || {};
    aiStep("supermove", { deck: d.deck, decision: d.name });
  });
  root.addEventListener("seek-refused", (e) => {
    const d = e.detail || {};
    aiStep("seek_refused", { deck: d.deck, decision: `seek ${d.from} -> ${d.to} refused`, why: d.why, at_song: d.from });
  });
  root.addEventListener("pagehide", flush);

  setInterval(poll, POLL_MS);
  setInterval(flush, FLUSH_MS);
  root.aiStep = aiStep;
  root.stepLog = { core, flush, queue: q };
})(typeof window !== "undefined" ? window : globalThis);
