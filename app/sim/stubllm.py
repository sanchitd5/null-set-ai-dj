"""Deterministic stand-in for the DJ's LLM: seeded picks from a catalog of known songs.

This is NOT the sim's default. The virtual set records and replays the real model's
replies (world.py). The stub exists for (a) pytest, (b) building the first fixtures offline
when no model server is reachable, and (c) the replay fallback when a code change makes the
set diverge from the recorded one (a recorded reply for another situation would be wrong;
the stub answers from the catalog instead and the report counts it as `replay_misses`).

It reads the situation from the prompt the engine built (song playing, its tempo / key /
measured energy, the songs around it) exactly as a model would, and behaves like a competent,
slightly noisy selector: it knows each catalog song's tempo, key and energy and prefers a
lockable tempo, a compatible key and a close energy, plus seeded noise so it is not a perfect
optimiser. Swap it for the real model with `--record`. Every random draw is seeded from
(seed, kind, the song playing, call number): the answer to a call never depends on which other
calls happened.
"""
from __future__ import annotations

import json
import random
import re
from typing import Protocol

_SPLIT = re.compile(r"\s+[-–—]\s+")
_NOW = re.compile(r'NOW PLAYING: "(.*?)" by (.*?)\n')
_BPM = re.compile(r"BPM: ([\d.]+) \| Camelot Key: (\w+)")
_ENERGY = re.compile(r"MEASURED ENERGY: (\d+)/10")
_AROUND = re.compile(r"Around this song[^:]*: (.*?)\n")


class LLM(Protocol):
    """What world.py needs from a model. `reply` returns the raw text a chat call would."""

    def reply(self, kind: str, system: str, user: str, ctx: dict) -> str: ...


def split_name(name: str) -> tuple:
    parts = _SPLIT.split(name, maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ("", name.strip())


def _feel(bpm: float) -> str:
    return "driving" if bpm >= 118 else "laid-back" if bpm < 95 else "mid"


def _tempo_gap(a_bpm: float, b_bpm: float) -> float:
    if not a_bpm or not b_bpm:
        return 0.3
    return min(abs(a_bpm / (b_bpm * m) - 1) for m in (1, 2, 0.5))


class StubLLM:
    def __init__(self, seed: int, catalog: list, noise: float = 0.35):
        """catalog: [{name, bpm, key, level}] every song the "model" may suggest."""
        self.seed = seed
        self.catalog = sorted(catalog, key=lambda c: (c["name"].lower(), c.get("id", "")))
        self.noise = noise
        self.calls = 0
        self.said: set = set()             # songs this model has already suggested in this run

    def reply(self, kind: str, system: str, user: str, ctx: dict) -> str:
        self.calls += 1
        if kind in ("suggest", "lookahead"):
            return json.dumps(self._suggest(kind, user, ctx))
        return "{}"            # a plan: no model opinion, the rules decide (validate_plan copes)

    def _rng(self, kind: str, ctx: dict) -> random.Random:
        return random.Random(f"{self.seed}:{kind}:{ctx.get('current', '')}:{ctx.get('n_call', 0)}")

    def _suggest(self, kind: str, user: str, ctx: dict) -> dict:
        from app.music_brain.recipe_matcher import camelot_distance_score   # lazy: config must see AIDJ_CACHE_DIR first

        rng = self._rng(kind, ctx)
        now = _NOW.search(user)
        bm, em, am = _BPM.search(user), _ENERGY.search(user), _AROUND.search(user)
        title = now.group(1) if now else ctx.get("current", "")
        cur_bpm = float(bm.group(1)) if bm else 0.0
        cur_key = bm.group(2) if bm else ""
        cur_lvl = int(em.group(1)) if em else 5
        around = {s.strip().lower() for s in (am.group(1).split(",") if am else [])}
        n = int(ctx.get("n_picks") or 5)
        scored = []
        for c in self.catalog:
            low = c["name"].lower()
            if title and title.lower() in low or any(a and a in low for a in around) or low in self.said:
                continue
            gap = _tempo_gap(cur_bpm, float(c.get("bpm") or 0))
            try:
                key = camelot_distance_score(cur_key, c.get("key") or "")[0] if cur_key and c.get("key") else 0.6
            except ValueError:
                key = 0.6
            lvl = abs(int(c.get("level") or 5) - cur_lvl)
            s = (1.0 if gap <= 0.06 else max(0.0, 1.0 - (gap - 0.06) * 6)) + key + (0.6 if lvl <= 2 else -0.3 * (lvl - 2))
            scored.append((s + rng.gauss(0, self.noise), c))
        scored.sort(key=lambda x: (-x[0], x[1]["name"].lower()))
        picks = []
        for _, c in scored[:n]:
            self.said.add(c["name"].lower())
            artist, ttl = split_name(c["name"])
            picks.append({
                "artist": artist or "Unknown", "title": ttl, "reason": "similar tempo and key",
                "genre": "", "era": "", "expected_bpm": round(float(c.get("bpm") or 0)),
                "expected_key": c.get("key") or "", "energy_delta": "maintain", "genre_hop": 0, "occasion_fit": 0,
                "track_profile": {"energy": int(c.get("level") or 5), "tempo_feel": _feel(float(c.get("bpm") or 0)),
                                  "mood": "euphoric"},
            })
        return {"steering": "stay", "occasion_fit": 0, "current_genre": "", "current_era": "",
                "current_profile": {"energy": cur_lvl, "tempo_feel": _feel(cur_bpm), "mood": "euphoric"},
                "suggestions": picks}
