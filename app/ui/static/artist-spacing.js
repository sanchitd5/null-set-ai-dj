// AI Music Brain - artist spacing, the ONE rule every selection path obeys.
//
// Owner: "it's going back to playing Fred again". The server's suggest filter
// (autopilot_service.py:artist_spacing) was the only check; the ready pool, combos,
// macros, the atlas and the library fallback reached evaluateCandidate without it.
// This file mirrors the Python rule exactly (parity: app/tests/js/artist_spacing_cases.json,
// checked by app/tests/js/artist_spacing_check.js and app/tests/py/test_artist_spacing_parity.py):
//   * no artist from the last GAP songs (played + queued)
//   * no artist already MAX_IN_WINDOW times in the last WINDOW songs (favourites included)
// Credits hidden in a title ("Atlantic Records - CA7RIEL, Fred again..–Sexy Magic") count:
// an artist key (>= 5 chars) of the candidate OR of a recent song found anywhere in a name.
(function (root) {
  "use strict";

  const GAP = 2, MAX_IN_WINDOW = 2, WINDOW = 6;          // = ARTIST_GAP_SONGS / ARTIST_MAX_IN_WINDOW / ARTIST_WINDOW
  const SEPARATORS = /\s+[-–—|｜•·]\s+|\s[｜|]\s?|\s*●+\s*|\s+[：:]\s+|\s*：\s*/;   // = track_identity._SEPARATORS
  const ARTIST_SPLIT = /\s*(?:,|&|\+|\bx\b|\band\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|\bvs\.?)\s*/i;
  const FEAT = /[([]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^)\]]+)[)\]]/i;

  const artistKey = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "");   // = set_memory.artist_key

  function splitOnce(s, re) {
    const m = re.exec(s);
    return m ? [s.slice(0, m.index), s.slice(m.index + m[0].length)] : [s];
  }
  const splitArtists = (s) => s.split(new RegExp(ARTIST_SPLIT.source, "gi")).map((n) => n.trim()).filter(Boolean);

  // = track_identity.credited_artists
  function creditedArtists(display) {
    const parts = splitOnce(String(display || "").trim(), SEPARATORS);
    let names = [];
    if (parts.length === 2 && parts[0] && parts[1]) {
      names = splitArtists(parts[0]);
      const m = FEAT.exec(parts[1]);
      if (m) names = names.concat(splitArtists(m[1]));
    }
    const seen = new Set(), out = [];
    for (const n of names) if (!seen.has(n.toLowerCase())) { seen.add(n.toLowerCase()); out.push(n); }
    return out;
  }

  // = autopilot_service._artists_of (name string form)
  function artistsOf(name, known) {
    const keys = new Set(creditedArtists(name).map(artistKey).filter(Boolean));
    const flat = artistKey(name);
    for (const k of known || []) if (k.length >= 5 && flat.includes(k)) keys.add(k);
    return keys;
  }

  // Why `name` may not play next after `recent` (display names, oldest first), or null.
  // relax: the last round (forceJump) may exceed the window cap, never the gap (Python keeps
  // the least-repeated pick then too, never one of the last GAP songs' artists).
  // -> {kind: null | "gap" | "window", artist}  (= autopilot_service.spacing_block)
  function spacingKind(name, recent, relax) {
    const r = (recent || []).filter(Boolean).slice(-WINDOW);
    const known = new Set(creditedArtists(name).map(artistKey).filter(Boolean));
    for (const x of r) for (const k of creditedArtists(x).map(artistKey)) if (k) known.add(k);
    const mine = artistsOf(name, known);
    const counts = new Map();
    let near = null;
    r.forEach((x, i) => {
      for (const a of artistsOf(x, known)) {
        counts.set(a, (counts.get(a) || 0) + 1);
        if (i >= r.length - GAP && mine.has(a)) near = a;
      }
    });
    if (near) return { kind: "gap", artist: near };
    if (!relax) for (const a of mine) if ((counts.get(a) || 0) >= MAX_IN_WINDOW) return { kind: "window", artist: a };
    return { kind: null, artist: null };
  }

  function spacingBlock(name, recent, relax) {
    const k = spacingKind(name, recent, relax);
    return k.kind === "gap" ? `artist spacing (${k.artist} in the last ${GAP} songs)`
      : k.kind === "window" ? `artist spacing (${k.artist} ${MAX_IN_WINDOW}x in the last ${WINDOW})` : null;
  }

  const core = { GAP, MAX_IN_WINDOW, WINDOW, artistKey, creditedArtists, artistsOf, spacingKind, spacingBlock };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.artistSpacing = core;
})(typeof window !== "undefined" ? window : globalThis);
