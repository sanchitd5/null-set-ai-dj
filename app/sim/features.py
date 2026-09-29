"""Feature coverage: which live features a run exercised, which it refused (and why), and how each
one that ran held up against its own checks.

The console explains itself: every stem move, mashup, riff, remix, hook drop, sampler hit ...
announces what it did (ai-activity label), what it refused (a console line with the reason) and
what the DJ mind decided. The table below is those words, counted, plus the audible log of the
transitions the feature ran in. Nothing is re-derived: a feature is "executed" when the console
said it did it.

Per feature: triggered (executed + refused), executed, refused with reasons, audible-risk flags
(from the graph sampler: dead air, two sub-bass owners, two singers, a tempo clash in a transition that ran
the feature) and its own checks (pass / fail), e.g. riff over rap: tempo gate honoured, keys ok, no
vocal clash; mashup / merge / blend: one sub-bass owner, entry on the 8-bar grid.

`never_triggered` lists the features a run did not touch; the suite unions runs and fails when a
feature that used to be triggered somewhere is not any more (suite.py).
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Optional

# feature -> (category, one-line what it is)
CATALOG = {
    "mashup_transition": ("transition", "Mashup -> Transition: A's instrumental under B's vocal, then B's beat takes over"),
    "mashup_layer": ("transition", "live A x B: B's vocal over A's instrumental phrase before the transition"),
    "stem_merge": ("transition", "Stem Merge: each stem from the deck the combo names, then B takes all"),
    "stem_bridge": ("transition", "Stem Bridge: beatless bridge across a tempo gap"),
    "stem_blend": ("transition", "stem blend / double drop: one owner per layer, kick + bass swapped on the line"),
    "stem_intro": ("transition", "Stem Intro: B enters on one measured intro stem"),
    "vocal_handoff": ("transition", "one singer: A's vocal rides B's beat"),
    "riff_over_rap": ("transition", "RIFF x RAP: A's riff under B's rap, tempo gate 3-8 %"),
    "layer": ("transition", "LAYER: both records held, bass handed over on a line"),
    "eq_blend": ("transition", "the EQ path (a stem move refused, or no stems)"),
    "echo_out": ("transition", "Echo Out"),
    "long_blend": ("transition", "Long Blend (one song, key-locked stems)"),
    "bass_swap": ("transition", "Bass Swap"),
    "learned_technique": ("brain", "a move learned from studied sets replaces the recipe"),
    "cookbook_recipes": ("brain", "the cookbook recipes the matcher can rank (probed on every played pair)"),
    "remix_synth_hold": ("in-song", "stem remix: synth hold"),
    "remix_acapella": ("in-song", "stem remix: acapella"),
    "remix_vocal_hold": ("in-song", "stem remix: vocal hold (HOLD ON)"),
    "remix_drum_break": ("in-song", "stem remix: drum break"),
    "remix_bass_out": ("in-song", "stem remix: bass out"),
    "strip_rebuild": ("in-song", "strip & rebuild (breakdown)"),
    "hook_drop": ("in-song", "hook drop: acapella on the emotional line, then the drop"),
    "mashup_break": ("in-song", "remix inside a mashup"),
    "auto_sampler": ("in-song", "sampler / one-shot hits on drops"),
    "dj_mind": ("in-song", "DJ mind decisions (fakeout, beat boost, stutter, filter build, hold loop ...)"),
    "hold_loop": ("in-song", "hold loop while the next song is not ready"),
    "tempo_stems": ("tempo", "key-locked tempo stems rendered at the playing tempo"),
    "tempo_home": ("tempo", "the new song glides back to its own tempo (homePlan)"),
    "bridge_path": ("tempo", "BRIDGE PATH: a tempo ladder toward a far tempo"),
    "library_fallback": ("brain", "a library song that locks, when the model gave nothing usable"),
    "live_ear": ("ear", "live ear: hold-loop seam checks and decisions"),
    "silent_ear": ("ear", "silent ear: pre-planned merge / audition"),
    "vibe_strip": ("ui", "VIBE strip states (idle / thinking / planning / mixing / listening)"),
    "null_bot_supermove": ("ui", "NULL-BOT supermoves"),
    "set_mode_windows": ("set", "play windows by set mode (LONG / MID / QUICK / bail / BRIDGE / famous)"),
    "fame": ("set", "famous-song window and fame lookups"),
    "set_memory": ("set", "earlier sets' songs offered to the model as 'heard recently'"),
    "llm_gate": ("set", "model calls by gate priority (suggest / lookahead / plan / ear)"),
    "energy_note": ("set", "energy arc hints (dip / callback / reprise) sent with the suggestion"),
}

_REMIX_LABELS = {"SYNTH HOLD": "remix_synth_hold", "ACAPELLA": "remix_acapella", "HOLD ON": "remix_vocal_hold",
                 "DRUM BREAK": "remix_drum_break", "BASS OUT": "remix_bass_out"}
_SIGNAL_RE = re.compile(r"[a-z]+")


def _bucket(reason: str) -> str:
    """A short stable key for a refusal reason (the numbers in it vary)."""
    r = reason.lower()
    for key, name in (("tempo gap", "tempo gap"), ("tempo too far", "tempo gap"), ("keys clash", "keys clash"), ("key clash", "keys clash"),
                      ("vocal", "vocals"), ("no stems", "no stems"), ("not separated", "no stems"), ("master ", "loudness floor"),
                      ("loudness", "loudness floor"), ("no stem audible", "silent stem"), ("no audible intro", "silent intro stem"),
                      ("breakdown", "no breakdown"), ("groove", "no groove"), ("rap", "no rap"), ("unavailable", "unavailable"),
                      ("cap", "cap"), ("since", "cooldown"), ("steering", "steering"), ("peak", "peak"), ("nowhere", "no room"),
                      ("stems not", "stems not ready"), ("plan", "no plan")):
        if key in r:
            return name
    return re.sub(r"[\d.]+", "#", reason)[:48].strip()


class _F:
    def __init__(self):
        self.executed = 0
        self.refused = Counter()
        self.risks: Counter = Counter()
        self.checks: dict = {}
        self.details: list = []
        self.instances: list = []       # transition indices the feature ran in

    def check(self, name: str, ok: bool) -> None:
        c = self.checks.setdefault(name, {"pass": 0, "fail": 0})
        c["pass" if ok else "fail"] += 1

    def out(self) -> dict:
        refused = sum(self.refused.values())
        d = {"triggered": self.executed + refused, "executed": self.executed, "refused": refused,
             "reasons": dict(sorted(self.refused.items())), "risks": dict(sorted(self.risks.items())),
             "checks": {k: v for k, v in sorted(self.checks.items())}}
        if self.details:
            d["details"] = self.details[:12]
        return d


def _phrase_err(entry: Optional[dict], pos: Optional[float]) -> Optional[float]:
    """Distance (s) from a song position to the nearest 8-bar phrase line of that song."""
    if not entry or pos is None:
        return None
    ph = (entry.get("analysis") or {}).get("phrase_boundaries_8bar") or []
    return min((abs(p - pos) for p in ph), default=None)


def feature_table(js: dict, world, run: dict) -> dict:
    F = {name: _F() for name in CATALOG}
    cons = js["console"]
    events = js["events"]
    trans = run["transitions"]
    wins = {w["i"]: w for w in js["audible"]["transitions"]}
    by_name = {}
    from app.ui import server

    for tid, nm in server._track_names.items():
        if tid in world._tracks:
            by_name[nm] = world._tracks[tid]

    def txt(c):
        return c["text"]

    # ---- console lines: refusals and small confirmations ----------------------------------------
    seen_riff = set()
    for c in cons:
        t = txt(c)
        m = re.match(r"^Mashup skipped: (.*)", t)
        if m:
            F["mashup_layer"].refused[_bucket(m.group(1))] += 1
        m = re.match(r"^mashup .*? refused: (.*)", t)
        if m:
            F["mashup_transition"].refused[_bucket(m.group(1))] += 1
        m = re.match(r"^merge .*? refused: (.*)", t)
        if m:
            F["stem_merge"].refused[_bucket(m.group(1))] += 1
        m = re.match(r"^stem bridge .*? refused: (.*)", t)
        if m:
            F["stem_bridge"].refused[_bucket(m.group(1))] += 1
        m = re.match(r"^stem blend .*? refused: (.*)", t)
        if m:
            F["stem_blend"].refused[_bucket(m.group(1))] += 1
        m = re.match(r"^stem intro .*? skipped: (.*)", t)
        if m:
            F["stem_intro"].refused[_bucket(m.group(1))] += 1
        m = re.match(r"^riff over rap: no - (.*)", t)
        if m and (c["t"], m.group(1)) not in seen_riff:        # the console logs it from two callers
            seen_riff.add((c["t"], m.group(1)))
            reasons = [r.strip() for r in m.group(1).split("; ") if r.strip()]
            F["riff_over_rap"].refused[_bucket(reasons[0].replace("no: ", ""))] += 1
        m = re.match(r"^(?:LAYER off for .*?|Layer plan unavailable|LAYER \(AI\) vetoed): (.*)", t)
        if m:
            F["layer"].refused[_bucket(m.group(1))] += 1
        m = re.match(r"^learned pick: none - (.*)", t)
        if m:
            F["learned_technique"].refused[_bucket(m.group(1))] += 1
        m = re.match(r"^remix (\w): (\w+) skipped", t)
        if m:
            F.get(f"remix_{m.group(2)}", F["remix_synth_hold"]).refused["skipped"] += 1
        m = re.match(r"^breakdown .*? skipped: (.*)", t)
        if m:
            F["strip_rebuild"].refused[_bucket(m.group(1))] += 1
        m = re.match(r"^tempo home ([AB]): (\w+) - (.*)", t)
        if m:
            F["tempo_home"].executed += 1
            F["tempo_home"].details.append(m.group(2))
        if t.startswith("BRIDGE"):
            F["bridge_path"].executed += 1
        if t.startswith("live ear"):
            F["live_ear"].refused[_bucket(t)] += 1
        if t.startswith("merge (silent ear)"):
            F["silent_ear"].executed += 1
        if t.startswith("transition recipe (learned)"):
            F["learned_technique"].executed += 1
        if t.startswith("library fallback"):
            F["library_fallback"].refused["error"] += 1

    # ---- the engine's events: what it announced doing --------------------------------------------------
    actions = Counter()
    for e in events:
        d = e.get("detail") or {}
        if e["type"] != "ai-activity":
            continue
        label = str(d.get("label") or "")
        kind = d.get("kind")
        if kind == "decision":
            act = str(d.get("action") or "")
            if act and act != "ride":
                actions[act] += 1
                F["dj_mind"].executed += 1
                if act == "holdloop":
                    F["hold_loop"].executed += 1
                if act == "hook_drop":
                    F["hook_drop"].executed += 1
        if label.startswith("MASHUP →"):
            F["mashup_transition"].executed += 1
        elif label.startswith("FULL MASHUP"):
            F["mashup_layer"].executed += 1
        elif label.startswith("MERGE →"):
            F["stem_merge"].executed += 1
        elif label.startswith("STEM BRIDGE"):
            F["stem_bridge"].executed += 1
        elif label.startswith("STEM BLEND") or label.startswith("STEM DOUBLE DROP"):
            F["stem_blend"].executed += 1
        elif label.startswith("STEM INTRO"):
            F["stem_intro"].executed += 1
        elif label.startswith("VOCAL HANDOFF") or label.startswith("VOCAL OUT"):
            F["vocal_handoff"].executed += 1
        elif label.startswith("STRIP & REBUILD"):
            F["strip_rebuild"].executed += 1
        elif label.startswith("HOOK DROP"):
            F["hook_drop"].executed += 1
        elif label.startswith("REMIX · MASHUP BREAK"):
            F["mashup_break"].executed += 1
        elif label.startswith("REMIX ·"):
            key = next((v for k, v in _REMIX_LABELS.items() if k in label), None)
            if key:
                F[key].executed += 1
        elif label.startswith("AUTO SAMPLER"):
            F["auto_sampler"].executed += 1
        elif label.startswith("TEMPO STEMS"):
            F["tempo_stems"].executed += 1
        elif label.startswith("RIFF"):
            F["riff_over_rap"].executed += 1
        elif label.startswith("LEARNED"):
            pass                                   # counted from the console line
    for act, n in actions.items():
        F["dj_mind"].details.append(f"{act} x{n}")

    # ---- transitions: which move ran, and how it held up ----------------------------------------------------
    for tr in trans:
        w = wins.get(tr["i"] - 1, {})
        move = str(tr["recipe_executed"] or "")
        feats = []
        if move == "Stem Bridge":
            feats.append("stem_bridge")
        elif move.startswith("LAYER"):
            feats.append("layer")
        elif move == "Stem Merge":
            feats.append("stem_merge")
        elif move == "RIFF OVER RAP":
            feats.append("riff_over_rap")
        elif move.startswith("Mashup"):
            feats.append("mashup_transition")
        elif move == "EQ blend":
            feats.append("eq_blend")
        elif move == "Echo Out":
            feats.append("echo_out")
        elif move == "Long Blend":
            feats.append("long_blend")
        elif move == "Bass Swap":
            feats.append("bass_swap")
        for name in feats:
            f = F[name]
            f.instances.append(tr["i"])
            if name in ("eq_blend", "echo_out", "long_blend", "bass_swap"):
                f.executed += 1
            # audible risks of the transition this feature ran in
            if (tr["dead_air_s"] or 0) > 1.0:
                f.risks["dead_air"] += 1
            if (tr["bass_overlap_s"] or 0) > 2.0:
                f.risks["two_sub_bass_owners"] += 1
            if (tr["vocal_clash_s"] or 0) > 2.0:
                f.risks["two_singers"] += 1
            if (tr["unlocked_overlap_s"] or 0) > 2.0:
                f.risks["tempo_clash"] += 1
            # checks: the rules each move has to keep
            f.check("one_sub_bass_owner", (tr["bass_overlap_s"] or 0) <= 1.0)
            f.check("no_dead_air", (tr["dead_air_s"] or 0) <= 1.0)
            if name in ("stem_merge", "mashup_transition", "riff_over_rap", "layer", "long_blend", "bass_swap"):
                f.check("one_singer", (tr["vocal_clash_s"] or 0) <= 1.0)
            st = (w.get("start") or {}).get("deck") or {}
            out_id = w.get("out") or "a"
            in_id = w.get("in") or "b"
            err_a = _phrase_err(by_name.get(tr["from"]), (st.get(out_id) or {}).get("pos"))
            if err_a is not None and name not in ("echo_out", "eq_blend"):
                f.check("exit_on_8bar_line", err_a <= 0.45)
            err_b = _phrase_err(by_name.get(tr["to"]), (st.get(in_id) or {}).get("pos"))
            if err_b is not None and name in ("stem_merge", "mashup_transition", "riff_over_rap", "long_blend", "bass_swap"):
                f.check("entry_on_8bar_line", err_b <= 0.45)
            if name in ("riff_over_rap", "mashup_transition", "long_blend", "bass_swap", "stem_merge"):
                f.check("tempo_gate_8pct", (tr["tempo_pct"] or 0) <= 8.5)
                f.check("keys_not_clashing", tr["key_kb"] is None or tr["key_kb"] > 0)
            if name == "stem_bridge":
                f.check("beatless", (tr["unlocked_overlap_s"] or 0) <= 1.0)
    if any(t["path"] == "eq" and t["degraded"] for t in trans):
        pass

    # ---- tempo / brain / ear / ui / set, from API traffic and the presentation layer ---------------------------
    net = js["net"]
    F["bridge_path"].executed += sum(1 for n in net if n["path"].startswith("/api/bridge/plan"))
    F["library_fallback"].executed += sum(1 for n in net if n["path"].startswith("/api/library/lockable"))
    F["live_ear"].executed += sum(1 for n in net if n["path"].startswith("/api/live/ear"))
    F["silent_ear"].executed += sum(1 for n in net if n["path"].startswith("/api/transition/preplan") or n["path"].startswith("/api/merge/audition"))
    F["fame"].executed += sum(1 for n in net if n["path"].endswith("/fame"))
    F["mashup_layer"].executed += sum(1 for n in net if n["path"].startswith("/api/mashup/plan") and n.get("status") == 200)

    status = js.get("status_log") or []
    labels, last_label = Counter(), None
    for st in status:
        m = re.search(r"\| (LONG|MID|QUICK·bail|QUICK|BRIDGE|FULL·famous) ", st["text"])
        if m and m.group(1) != last_label:          # count each window once (the status line repeats every second)
            labels[m.group(1)] += 1
        if m:
            last_label = m.group(1)
        elif st["text"].startswith("Next:"):
            last_label = None
    for lab, n in labels.items():
        F["set_mode_windows"].executed += n
        F["set_mode_windows"].details.append(f"{lab} x{n}")
    if labels.get("FULL·famous"):
        F["fame"].executed += labels["FULL·famous"]

    orb = Counter()
    for u in js.get("ui_states") or []:
        v = (u.get("vibe") or "").upper()
        for s in ("IDLE", "THINKING", "PLANNING", "MIXING", "LISTENING"):
            if s in v:
                orb[s] += 1
    F["vibe_strip"].executed = sum(orb.values())
    F["vibe_strip"].details = [f"{k} x{v}" for k, v in sorted(orb.items())]
    sm = js.get("supermoves") or []
    F["null_bot_supermove"].executed = len(sm)
    F["null_bot_supermove"].details = sorted({s["name"] for s in sm})

    prios = Counter(e.get("priority") for e in world.events if e.get("kind") == "llm")
    F["llm_gate"].executed = sum(prios.values())
    F["llm_gate"].details = [f"{k} x{v}" for k, v in sorted(prios.items(), key=lambda kv: str(kv[0]))]
    F["set_memory"].executed = world.prompt_flags.get("earlier_sets", 0)
    F["energy_note"].executed = world.prompt_flags.get("energy_note", 0)

    # ---- the cookbook recipes: probed on every played pair (real matcher) ------------------------------------------
    probe = recipe_probe(run, by_name)
    F["cookbook_recipes"].executed = len(probe["viable"])
    F["cookbook_recipes"].details = [f"viable {len(probe['viable'])}/{probe['total']}", f"top-1: {', '.join(sorted(probe['top1']))}"]
    for r, why in probe["never"].items():
        F["cookbook_recipes"].refused[why] += 1
    for name, n in probe["snap"].items():
        F["cookbook_recipes"].check(name, n[0] == n[1])

    table = {k: F[k].out() for k in CATALOG}
    never = sorted(k for k, v in table.items() if v["triggered"] == 0)
    return {"table": table, "never_triggered": never, "triggered": sorted(k for k in table if table[k]["triggered"]),
            "cookbook": {"viable": sorted(probe["viable"]), "top1": sorted(probe["top1"]), "total": probe["total"]}}


def recipe_probe(run: dict, by_name: dict) -> dict:
    """Every cookbook recipe, scored by the real matcher on every played pair: which are viable
    (score > 0), which the matcher ranks first, and whether its a_time / b_time sit on the 8-bar grid."""
    from app.music_brain.analyzer import _from_dict
    from app.music_brain.knowledge_parser import KnowledgeParser
    from app.music_brain.recipe_matcher import RecipeMatcher, nearest_phrase_boundary

    km = RecipeMatcher(KnowledgeParser())
    names = sorted(r.name for r in km.knowledge.get_all()) if hasattr(km, "knowledge") else []
    viable, top1, snap, never = set(), set(), {}, {}
    seen = set()
    for tr in run["transitions"]:
        a, b = by_name.get(tr["from"]), by_name.get(tr["to"])
        if not a or not b or (tr["from"], tr["to"]) in seen:
            continue
        seen.add((tr["from"], tr["to"]))
        ta, tb = _from_dict(dict(a["analysis"])), _from_dict(dict(b["analysis"]))
        try:
            cands = km.match(ta, tb, top_n=40)
        except Exception:
            continue
        if cands:
            top1.add(cands[0].recipe.name)
        for c in cands:
            if c.score > 0:
                viable.add(c.recipe.name)
            ok_a = abs(nearest_phrase_boundary(ta, c.a_time) - c.a_time) < 0.15
            s = snap.setdefault("a_time_on_8bar_line", [0, 0])
            s[1] += 1
            s[0] += 1 if ok_a else 0
    for n in names:
        if n not in viable:
            never[n] = "never scored above 0 on a played pair"
    return {"viable": viable, "top1": top1, "total": len(names), "never": never, "snap": snap}
