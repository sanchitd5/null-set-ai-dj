"""Transition techniques learned from real sets, each with the conditions it needs.

Not every technique mixes every pair: each one states what it needs, and
rank() returns every technique with the reasons it fits or does not, so the
planner (and the user) can see why a move was or was not chosen.

Sources (measured with 4-stem Demucs; see research/notes/set-study-gfF8jzBVWvM.md):
  USB002 = Fred again.. & Thomas Bangalter, Alexandra Palace, 27 Feb 2026.

  strip_rebuild   USB002 74:22, leavemealone (Nia Archives Remix), 174 BPM.
                  Vocal never stops; drums out -> bass out (voice alone) -> bass
                  back, no kick -> bass out + drums creep (build) -> drop on the
                  line, ~40 bars. How a famous song plays 7 minutes.
  riff_over_rap   USB002 66:00-68:48, Aerodynamic (122.9, 10A) x Victory Lap Five
                  (140, 5B). Outgoing KEY-LOCKED +13.9 % to the incoming tempo
                  (pitch 0.00 st); its 16-bar full groove LOOPED; incoming bass
                  swapped in under it; loop released into the outgoing song's
                  OWN drumless breakdown (= the 8-bar break); incoming drops in
                  under the breakdown's riff, looped on top, riff fades after
                  ~8 bars. Keys 5 hours apart: fine, the top layer is rap.
                  ONE TONAL OWNER: while A's riff plays, B brings drums, bass and
                  rap only; B's own synths enter as the riff fades (the set's
                  `other` layer is Aerodynamic's alone at 67:36-68:24).
  vocal_handoff   House rule (user): one singer through a crossfade; outgoing
                  vocal rides the incoming instrumental, incoming vocal returns
                  as it fades.
  full_mashup     Incoming vocal phrase over the outgoing instrumental (stems).
  eq_blend        The recipe engine's default (recipe_matcher.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

STEMS = ("drums", "bass", "vocals", "other")
SILENT_DB = -45.0          # a stem this quiet over a phrase is "out"
# Rap vs sung (measured 2026-09-27; voiced fraction does NOT separate them:
# rap is voiced speech). Sung notes hold a pitch and sit on the scale; rap
# glides. VLF rap: held 0.074 s, 25.8 c off-note (random = 25); Starboy 0.184 s
# / 12.5 c, Apocalypse 0.380 / 13.9, Teardrop 0.184 / 22.2. Preliminary: 5 songs.
RAP_MAX_HOLD_S = 0.10
RAP_MIN_OFFNOTE_C = 20.0
MAX_KEYLOCK_STRETCH = 0.08  # key-locked stretch smears past ~8 % (CLAUDE.md s4: big gaps go via Echo Out)
MAX_RATE_STRETCH = 0.06    # without key-lock (pitch moves): the recipe engine's limit


# ----------------------------------------------------------------- features
def camelot_score(a: Optional[str], b: Optional[str]) -> float:
    """Same table as CLAUDE.md section 4 (and dj-mind.js camelotScore)."""
    import re

    pa = re.match(r"^(\d{1,2})([AB])$", str(a or "").strip(), re.I)
    pb = re.match(r"^(\d{1,2})([AB])$", str(b or "").strip(), re.I)
    if not pa or not pb:
        return 0.0
    d = min((int(pa[1]) - int(pb[1])) % 12, (int(pb[1]) - int(pa[1])) % 12)
    if pa[2].upper() != pb[2].upper():
        return 0.85 if d == 0 else 0.0
    return {0: 1.0, 1: 0.9, 2: 0.8}.get(d, 0.0)


def vocal_style(y: np.ndarray, sr: int) -> dict:
    """{'rap': bool, 'hold_s', 'offnote_c'} from an isolated vocal (pyin)."""
    import librosa

    hop = 256
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    f0, flag, _ = librosa.pyin(y, fmin=70, fmax=900, sr=sr, frame_length=1024, hop_length=hop)
    loud = rms[: len(flag)] > np.percentile(rms, 40)
    cents = 1200 * np.log2(np.where(flag & loud, f0, np.nan) / 440.0)
    d = np.abs(np.diff(cents))
    runs, cur = [], 0
    for x in d:
        if np.isfinite(x) and x < 25:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    held = [r for r in runs if r >= 3]
    hold = float(np.mean(held)) * hop / sr if held else 0.0
    off = float(np.nanmean(np.abs(cents - np.round(cents / 100) * 100))) if np.isfinite(cents).any() else 25.0
    return {"rap": hold < RAP_MAX_HOLD_S and off > RAP_MIN_OFFNOTE_C, "hold_s": round(hold, 3), "offnote_c": round(off, 1)}


# A hook chanted bar after bar: each bar's vocal RHYTHM repeats an earlier bar
# (onset-envelope r >= 0.75 against the previous 8). Timbre can't tell hook from
# verse (same voice: MFCC r 0.75-0.86 everywhere); rhythm can. Victory Lap:
# 0:16.8-0:25.3 repeats at r 0.76-0.94, the verse breaks it at 0:27 (0.50).
REPEAT_R = 0.75
REPEAT_RUN = 3


def repetitive_bars(vocals: np.ndarray, sr: int, downbeats: Sequence[float], upto: int = 48) -> List[bool]:
    """Per bar (from downbeats[0]): does its vocal rhythm repeat a bar within the previous 8?"""
    import librosa

    hop = 128
    env = librosa.onset.onset_strength(y=vocals, sr=sr, hop_length=hop)
    fr = sr / hop
    db = list(downbeats)[: upto + 1]
    if len(db) < 3:
        return []
    n = int(round((db[1] - db[0]) * fr))

    def bar(i):
        a = int(db[i] * fr)
        x = env[a:a + n]
        return x - x.mean() if len(x) == n else None

    def r(x, y):
        if x is None or y is None:
            return 0.0
        d = np.linalg.norm(x) * np.linalg.norm(y)
        return float(np.dot(x, y) / d) if d > 1e-9 else 0.0

    bars = [bar(i) for i in range(len(db) - 1)]
    return [i > 0 and max(r(bars[i], bars[j]) for j in range(max(0, i - 8), i)) >= REPEAT_R for i in range(len(bars))]


def skip_repetitive_intro(rep: List[bool], downbeats: Sequence[float], phrases: Sequence[float], rap_at: float) -> float:
    """Entry after the opening hook (the first run of >= REPEAT_RUN repeating
    bars), snapped forward to B's next 8-bar phrase line."""
    # the FIRST run is the opening hook; later repeats are the verse's own
    # rhyme patterns (Victory Lap 0:32-0:36) and stay in
    end_bar, run = None, 0
    for i, x in enumerate(rep):
        run = run + 1 if x else 0
        if run >= REPEAT_RUN:
            end_bar = i
        elif end_bar is not None:
            break
    if end_bar is None:
        return rap_at
    after = downbeats[end_bar + 1] if end_bar + 1 < len(downbeats) else rap_at
    nxt = [p for p in phrases if p >= after - 0.05]
    return max(rap_at, nxt[0] if nxt else after)


def stem_map(stem_audio: Dict[str, np.ndarray], sr: int, phrases: Sequence[float]) -> List[Dict[str, float]]:
    """Per 8-bar phrase: RMS dB of each stem. [{start, end, drums, bass, vocals, other}]"""
    out = []
    for a, b in zip(phrases, phrases[1:]):
        row = {"start": float(a), "end": float(b)}
        for n in STEMS:
            y = stem_audio.get(n)
            seg = y[int(a * sr): int(b * sr)] if y is not None else np.zeros(1)
            row[n] = float(10 * np.log10(np.mean(np.square(seg)) + 1e-12)) if len(seg) else -120.0
        out.append(row)
    return out


def full_groove_runs(smap: List[Dict[str, float]], min_phrases: int = 2) -> List[Tuple[float, float]]:
    """Stretches where drums, bass and riff all play: loopable groove."""
    runs, cur = [], None
    for r in smap:
        on = all(r[n] > SILENT_DB for n in ("drums", "bass", "other"))
        if on and cur is None:
            cur = [r["start"], r["end"]]
        elif on:
            cur[1] = r["end"]
        elif cur is not None:
            runs.append(tuple(cur)); cur = None
    if cur is not None:
        runs.append(tuple(cur))
    return [x for x in runs if x[1] - x[0] >= min_phrases * (smap[0]["end"] - smap[0]["start"] if smap else 0) * 0.95]


def breakdowns(smap: List[Dict[str, float]]) -> List[Tuple[float, float]]:
    """Phrases with the riff but no drums and no bass: a natural break."""
    return [(r["start"], r["end"]) for r in smap
            if r["drums"] <= SILENT_DB and r["bass"] <= SILENT_DB and r["other"] > SILENT_DB]


@dataclass
class PairFeatures:
    bpm_a: float
    bpm_b: float
    key_a: Optional[str] = None
    key_b: Optional[str] = None
    stems_a: bool = False
    stems_b: bool = False
    famous_a: bool = False
    vocal_a_exit: float = 0.0          # share of the exit window where A sings
    vocal_b_entry: float = 0.0         # share of B's entry window with vocals
    b_rap: Optional[bool] = None       # B has a rap section (vocal_style), None = unknown
    b_rap_at: List[float] = field(default_factory=list)   # where B raps (s): enter there
    a_grooves: List[Tuple[float, float]] = field(default_factory=list)
    a_breakdowns: List[Tuple[float, float]] = field(default_factory=list)
    keylock: bool = False              # engine can stretch without moving pitch
    a_hook_drops: List[dict] = field(default_factory=list)   # hook_drop.plan() for A: where to go acapella + drop
    exit_window: Tuple[float, float] = (0.0, 0.0)

    @property
    def tempo_gap(self) -> float:
        return _fold_gap(self.bpm_a, self.bpm_b)

    @property
    def riff_tempo_gap(self) -> float:
        """Like tempo_gap, but also folds 3-over-4 / 2-over-3 feel: riff_over_rap
        only needs A's groove bars to line up with B's, which a felt 3:4 or 2:3
        relationship gives even when the raw BPM ratio doesn't. Other techniques
        (learned moves, vocal/rate stretch) keep the tighter tempo_gap."""
        return _fold_gap(self.bpm_a, self.bpm_b, extra=True)

    @property
    def key(self) -> float:
        return camelot_score(self.key_a, self.key_b)


# --------------------------------------------------------------- techniques
Check = Tuple[bool, str]


@dataclass
class Technique:
    name: str
    source: str
    what: str
    checks: Callable[[PairFeatures], List[Check]]
    live: bool = True                  # False: needs something the live engine lacks

    def assess(self, f: PairFeatures) -> dict:
        res = self.checks(f)
        return {"name": self.name, "source": self.source, "what": self.what,
                "fits": all(ok for ok, _ in res),
                "reasons": [("ok: " if ok else "no: ") + why for ok, why in res]}


def _riff_over_rap(f: PairFeatures) -> List[Check]:
    gap = f.riff_tempo_gap
    lo, hi = f.exit_window
    brk = [b for b in f.a_breakdowns if lo - 60 <= b[0] <= hi + 30]
    rap = bool(f.b_rap) and f.vocal_b_entry >= 0.3
    return [
        (f.stems_a and f.stems_b, "stems on both songs" if f.stems_a and f.stems_b else "needs 4 stems on both songs"),
        (0.03 <= gap <= MAX_KEYLOCK_STRETCH, f"tempo gap {gap:.1%} (3-8 %: stretch A onto B's tempo)"),
        (f.keylock or gap <= MAX_RATE_STRETCH,
         "key-locked stretch available" if f.keylock else f"no key-lock: {gap:.1%} would detune A's riff by {12 * np.log2(1 + gap):.1f} st"),
        (bool(f.a_grooves), "A has a loopable full groove" if f.a_grooves else "A has no drums+bass+riff stretch to loop"),
        (bool(brk), "A has its own drumless breakdown near the exit" if brk else "A has no drumless breakdown near the exit (the break comes from the song)"),
        (rap or f.key >= 0.8, (f"B raps at {', '.join(f'{int(t // 60)}:{int(t % 60):02d}' for t in f.b_rap_at[:3]) or '?'}: enter there, the key clash doesn't matter") if rap else
         ("keys compatible" if f.key >= 0.8 else f"B's vocal is sung and keys clash ({f.key_a}->{f.key_b})")),
    ]


def _strip_rebuild(f: PairFeatures) -> List[Check]:
    return [
        (f.famous_a, "A is famous: worth playing in full" if f.famous_a else "A isn't famous: no need to stretch it"),
        (f.stems_a, "A has live stems" if f.stems_a else "needs A's 4 stems"),
        (f.vocal_a_exit >= 0.5, f"A's vocal carries {f.vocal_a_exit:.0%} of the section (needs 50 %)"),
    ]


def _vocal_handoff(f: PairFeatures) -> List[Check]:
    return [
        (f.stems_a and f.stems_b, "stems on both" if f.stems_a and f.stems_b else "needs stems on both"),
        (f.key >= 0.8, f"keys {f.key_a}->{f.key_b} score {f.key:.2f} (A's voice sits over B's harmony)"),
        (f.vocal_a_exit >= 0.3, f"A sings {f.vocal_a_exit:.0%} of the blend"),
        (f.tempo_gap <= MAX_RATE_STRETCH, f"tempo gap {f.tempo_gap:.1%} (<= 6 % pitch-lock)"),
    ]


def _full_mashup(f: PairFeatures) -> List[Check]:
    return [
        (f.stems_a, "A can drop its vocal (stems)" if f.stems_a else "needs A's stems"),
        (f.vocal_b_entry >= 0.5, f"B has a vocal phrase ({f.vocal_b_entry:.0%})"),
        (f.key >= 0.8 or bool(f.b_rap),
         "harmony fits (or B raps)" if f.key >= 0.8 else f"keys {f.key_a}->{f.key_b} clash under a sung vocal"),
        (f.tempo_gap <= 0.04, f"tempo gap {f.tempo_gap:.1%} (<= 4 % for a vocal)"),
    ]


def _eq_blend(f: PairFeatures) -> List[Check]:
    return [(f.tempo_gap <= MAX_RATE_STRETCH or True, "always available (recipe engine picks the recipe)")]


TECHNIQUES = [
    Technique("riff_over_rap", "USB002 1:06:00 Aerodynamic x Victory Lap Five",
              "A key-locked to B's tempo, A's groove looped, bass to B, release into A's own breakdown, "
              "B drops in under A's looped riff", _riff_over_rap, live=False),
    Technique("strip_rebuild", "USB002 1:14:22 leavemealone (Nia Archives Remix)",
              "drums out, bass out, voice alone, bass back, build, drop on the line (40 bars)", _strip_rebuild),
    Technique("vocal_handoff", "house rule", "one singer: A's vocal rides B's instrumental", _vocal_handoff),
    Technique("full_mashup", "stems", "B's vocal phrase over A's instrumental", _full_mashup),
    Technique("eq_blend", "recipe engine", "EQ-first recipe blend (bass swap, echo out, ...)", _eq_blend),
]


NEAR_GAP = 0.03             # a learned move fits pairs within this tempo gap of a pair it was seen on
NEAR_KEY = 0.15             # ... and within this key score


FOLD_RATIOS = (1.0, 2.0, 0.5)                          # straight, half/double time
FOLD_RATIOS_EXTRA = FOLD_RATIOS + (2 / 3, 3 / 2, 3 / 4, 4 / 3)  # + 3-over-4 / 2-over-3 feel


def _fold_gap(bpm_a: float, bpm_b: float, extra: bool = False) -> float:
    """Tempo gap with half/double time folded, as the set learner records it.
    extra=True also folds 3-over-4 / 2-over-3 feel (bar grids that line up at
    those ratios even when the raw BPM ratio doesn't) -- used only where that
    looser feel is actually musically valid (riff_over_rap's groove-over-groove
    loop), not for tight learned-move matching."""
    if bpm_a <= 0 or bpm_b <= 0:
        return 1.0
    ratios = FOLD_RATIOS_EXTRA if extra else FOLD_RATIOS
    return min(abs(bpm_b * k / bpm_a - 1) for k in ratios)


TEMPO_GAP_SLACK = 0.02      # a learned move fits pairs up to this much further apart than seen
KEY_SCORE_SLACK = 0.05


def scene_store(store: Dict[str, dict], scene: Optional[str] = None) -> Dict[str, dict]:
    """The store as one scene sees it. A sighting from a scene-tagged set
    (scene_profile.SET_SCENES) counts toward that scene only: scene=None keeps the
    global sets' sightings, scene="punjabi" adds the Punjabi sets' ones. count,
    tempo_gap_max and key_score_min are recomputed from what is kept, so a desi key
    clash never loosens the global rules. A store with no tagged set comes back as is.
    Each entry also gets scene_clash: the kept scene-tagged sightings on a key clash
    (key_score < LEARNED_MIN_KEY), and scene_sets: those sightings' set labels."""
    from app.music_brain.analysis import scene_profile as sp
    out = {}
    for kind, e in (store or {}).items():
        obs = e.get("observations") if isinstance(e, dict) else None
        if not isinstance(obs, list) or not any(isinstance(o, dict) and sp.set_scene(o.get("set_id")) for o in obs):
            out[kind] = e
            continue
        keep = [o for o in obs if isinstance(o, dict) and sp.set_scene(o.get("set_id")) in (None, scene)]
        tagged = [o for o in keep if sp.set_scene(o.get("set_id"))]
        clash = [o for o in tagged if o.get("key_score") is not None and o["key_score"] < LEARNED_MIN_KEY]
        e = dict(e, observations=keep, count=len(keep),
                 tempo_gap_max=max([o["tempo_gap"] for o in keep if o.get("tempo_gap") is not None], default=None),
                 key_score_min=min([o["key_score"] for o in keep if o.get("key_score") is not None], default=None))
        if scene:
            e["scene_clash"] = len(clash)
            e["scene_sets"] = sorted({sp.set_label(o.get("set_id")) for o in clash})
        out[kind] = e
    return out


def learned_techniques(store: Optional[Dict[str, dict]] = None, scene: Optional[str] = None) -> List[Technique]:
    """Techniques observed in studied sets (app.music_brain.set_learner), each
    fitting pairs inside the tempo gap / key score range it was seen at.
    scene: whose scene-tagged sightings count too (scene_store); None = global only."""
    if store is None:
        from app.music_brain.set_learner import load_learned
        store = load_learned()
    store = scene_store(store, scene)
    out = []
    for kind, e in sorted(store.items()):
        obs = e.get("observations") or []
        if not obs or e.get("disabled"):
            continue
        rules = [r.get("text", "") for r in e.get("user_rules") or []]
        ai_rules = list(e.get("ai_rules") or [])[:3]
        gap_max, key_min, stems = e.get("tempo_gap_max"), e.get("key_score_min"), bool(e.get("stems"))
        vocal_from = {o.get("detail", {}).get("vocal_from") for o in obs} - {None}

        def checks(f: PairFeatures, obs_all=obs, gap_max=gap_max, key_min=key_min, stems=stems, vocal_from=vocal_from, n=len(obs), rules=rules, kind=kind, ai_rules=ai_rules):
            res: List[Check] = ([(True, f"seen {n}x in studied sets")] + [(True, f"user rule: {r}") for r in rules]
                                + [(True, f"ai rule: {r}") for r in ai_rules])
            if stems:
                res.append((f.stems_a and f.stems_b, "stems on both" if f.stems_a and f.stems_b else "needs stems on both"))
            # "pairs like this one": the nearest pair the move was seen on must be close
            # in tempo gap and key. A range (min..max over every sighting) ends up
            # covering every pair and says nothing about this one.
            pairs = [(o["tempo_gap"], o.get("key_score")) for o in obs_all if o.get("tempo_gap") is not None]
            if pairs:
                gap = _fold_gap(f.bpm_a, f.bpm_b)
                near = min(pairs, key=lambda p: abs(p[0] - gap) / NEAR_GAP + (abs((p[1] if p[1] is not None else f.key) - f.key) / NEAR_KEY))
                ok = abs(near[0] - gap) <= NEAR_GAP and (near[1] is None or abs(near[1] - f.key) <= NEAR_KEY)
                res.append((ok, f"like a pair seen at tempo gap {near[0]:.1%}" + (f", key {near[1]:.2f}" if near[1] is not None else "")
                            + f" (this pair: {gap:.1%}, key {f.key:.2f})"))
            if vocal_from == {"A"}:
                res.append((f.vocal_a_exit >= 0.3, f"A sings {f.vocal_a_exit:.0%} of the exit (needs 30 %)"))
            elif vocal_from == {"B"}:
                res.append((f.vocal_b_entry >= 0.3, f"B sings {f.vocal_b_entry:.0%} of the entry (needs 30 %)"))
            if kind == "acapella_drop":
                d = f.a_hook_drops[0] if f.a_hook_drops else None
                res.append((d is not None, f'A\'s hook "{d["text"]}": beat out at {d["cut_at"]:.0f}s, drop at {d["drop_at"]:.0f}s'
                            if d else "no hook line on a phrase boundary in A (needs synced lyrics)"))
            return res

        src = obs[0]
        out.append(Technique(f"learned:{kind}", f"{src.get('set_id')} {int(src.get('at', 0)) // 60}:{int(src.get('at', 0)) % 60:02d} "
                             f"{src.get('track_a', '')}{' -> ' + src['track_b'] if src.get('track_b') else ''}",
                             e.get("what", kind), checks, live=bool(e.get("live"))))
    return out


def rank(f: PairFeatures, learned: Optional[Dict[str, dict]] = None, scene: Optional[str] = None) -> List[dict]:
    """Every technique with fits + reasons; fitting ones first, in library order
    (built-ins, then techniques learned from studied sets). scene: see scene_store."""
    lib = TECHNIQUES + learned_techniques(learned, scene)
    order = {t.name: i for i, t in enumerate(lib)}
    out = [t.assess(f) | {"live": t.live} for t in lib]
    return sorted(out, key=lambda x: (not x["fits"], order[x["name"]]))


# ------------------------------------------------------- learned -> console
# Learned kinds the live console can already perform, as the console recipe that
# performs them. Everything else learned (vocal re-cuts, chops, loops) is studied,
# not played: the console has no move for it yet.
LEARNED_RECIPE = {
    "learned:bass_swap": "Bass Swap",
    "learned:stem_intro": "Long Blend",        # stem-moves eqIntro: B's intro stem under A, bass on the swap line
    "learned:acapella_over": "Mashup → Transition",
}

# The owner's rule: a hard cut is worse than any blend and is never preferred. It has no console
# recipe on purpose, and learned_pick refuses it even if a mapping is added later.
NEVER_PLAY = {"learned:hard_cut"}


LEARNED_MIN_KEY = 0.6   # = autopilot.js KEY_SAFE_MIN: the KB's worst legal move is -2 hours (0.6); 0.3 and 0 are rewritten
_KEY_SENSITIVE = {"learned:bass_swap", "learned:stem_intro"}   # both layer tonal stems / full mixes


def learned_pick(ranked: List[dict], store: Optional[Dict[str, dict]] = None,
                 key_score: Optional[float] = None, level: Optional[str] = None,
                 tempo_gap: Optional[float] = None) -> Optional[dict]:
    """The learned move to play for this pair, or None: live, fits, has a console recipe;
    among those, the one seen most often in studied sets. key_score (Camelot, 0-1) of
    the pair: below LEARNED_MIN_KEY the tonal blends (bass swap, stem intro) are skipped,
    whatever clashing pairs they were once seen on (7 of 11 stem_intro sightings clash).
    level: the Punjabi scene profile level (scene_profile.level), None = no profile, the
    rule above as it was. rank() must have been given scene_profile.learned_scene(level).
    Under "full": a tonal blend on a clash plays when the scene's own sets show it on
    clashing pairs often enough (learned_clash_ok), and a pair past the keylock cap
    (tempo_gap, octave-folded) gets the profile's fallback_recipe instead of the blend."""
    from app.music_brain.analysis import scene_profile as sp
    if store is None:
        from app.music_brain.set_learner import load_learned
        store = load_learned()
    store = scene_store(store, sp.learned_scene(level))
    best = None
    for r in ranked:
        rec = LEARNED_RECIPE.get(r["name"])
        if r["name"] in NEVER_PLAY or not (rec and r["fits"] and r.get("live")):
            continue
        kind = r["name"].split(":", 1)[1]
        e = store.get(kind) or {}
        clash = key_score is not None and key_score < LEARNED_MIN_KEY and r["name"] in _KEY_SENSITIVE
        if clash and not sp.learned_clash_ok(level, e.get("scene_clash")):
            continue
        if kind == "acapella_over":           # only B's voice over A's beat is the console's mashup
            froms = {o.get("detail", {}).get("vocal_from") for o in e.get("observations") or []}
            if "B" not in froms:
                continue
        seen = int(e.get("count") or 0)
        if best is None or seen > best["seen"]:
            best = {"kind": kind, "recipe": rec, "seen": seen, "source": r["source"], "reasons": r["reasons"],
                    "rules": [x for x in r["reasons"] if "rule:" in x]}
            if level:                       # profile fields only: no profile = today's pick, byte for byte
                n = int(e.get("scene_clash") or 0)
                best.update(level=level, scene_clash=n, tempo_gap=None if tempo_gap is None else round(tempo_gap, 4),
                            clash=(f"learned from {', '.join(e.get('scene_sets') or [])}: {n} key-clash "
                                   f"{kind.replace('_', ' ')}s") if clash else None, planned=None, degraded=None)
    if best and level and not sp.learned_tempo_ok(level, tempo_gap):
        # never stretch past the keylock cap: the scene's cut-style handover on the downbeat instead
        best.update(planned=best["recipe"], recipe=sp.PUNJABI_PROFILE["fallback_recipe"],
                    degraded=f"tempo gap {tempo_gap:.1%} past the {sp.PUNJABI_PROFILE['learned_tempo_cap']:.0%} keylock cap")
    return best


# ------------------------------------------------------- learned -> in-song moves
# The four learned kinds that are not transition recipes: the console plays them INSIDE a song
# (app/ui/static/learned-moves.js). This is what the console reads: per kind, whether it may run
# (seen in a studied set, not disabled) and the parameters the sightings show. The console clamps
# them by what the playing song's audio allows; a value that is None here is a logged fallback there.
MOVE_KINDS = ("vocal_loop", "vocal_resequence", "vocal_chop", "loop_extend")


def _median(xs: Sequence[float]) -> Optional[float]:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return float(xs[m]) if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def _spans(obs: List[dict], key: str) -> List[float]:
    """Lengths (s) of every [start, end] pair under detail[key]."""
    out = []
    for o in obs:
        det = o.get("detail")
        for p in (det.get(key) if isinstance(det, dict) else None) or []:
            try:
                out.append(float(p[1]) - float(p[0]))
            except (TypeError, ValueError, IndexError):
                continue
    return out


def _move_params(kind: str, obs: List[dict]) -> Dict[str, Optional[float]]:
    det = [o.get("detail") if isinstance(o.get("detail"), dict) else {} for o in obs]
    spans = []
    for d in det:
        try:
            spans.append(float(d["set_span"][1]) - float(d["set_span"][0]))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    span = _median(spans)
    if kind == "vocal_loop":
        return {"repeats": _median([d.get("repeats") for d in det]),
                "repeats_max": max([d.get("repeats") or 0 for d in det], default=0) or None,
                "line_s": _median(_spans(obs, "source_lines")), "span_s": span}
    if kind == "vocal_resequence":
        return {"lines": _median([len(d.get("source_lines") or []) for d in det]) or None,
                "line_s": _median(_spans(obs, "source_lines")), "span_s": span}
    if kind == "vocal_chop":
        return {"frag_s": _median(_spans(obs, "fragments")),
                "frags": _median([len(d.get("fragments") or []) for d in det]) or None,
                "jumps": _median([d.get("jumps") for d in det]), "span_s": span}
    gaps = sorted(o["tempo_gap"] for o in obs if o.get("tempo_gap") is not None)
    return {"tempo_gap_max": gaps[-1] if gaps else None, "tempo_gap_median": _median(gaps)}


def learned_moves(store: Optional[Dict[str, dict]] = None) -> Dict[str, dict]:
    """{kind: {kind, enabled, seen, stems, rules, params}} for the in-song learned moves.
    enabled = sighted at least once and not disabled by the user; rules = the user's words."""
    if store is None:
        from app.music_brain.set_learner import load_learned
        store = load_learned()
    out = {}
    for kind in MOVE_KINDS:
        e = (store or {}).get(kind) or {}
        obs = [o for o in e.get("observations") or [] if isinstance(o, dict)]
        rules = [str(r.get("text", "")) for r in e.get("user_rules") or [] if isinstance(r, dict)]
        out[kind] = {"kind": kind, "enabled": bool(obs) and not e.get("disabled"), "disabled": bool(e.get("disabled")),
                     "seen": len(obs), "stems": bool(e.get("stems")), "rules": rules,
                     "ai_rules": list(e.get("ai_rules") or [])[:2], "params": _move_params(kind, obs)}
    return out
