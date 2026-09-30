"""Learned moves under the Punjabi scene profile: scene-tagged sets (scene_profile.SET_SCENES)
count toward their scene only; at the full level a learned stem intro may play on a key clash
with enough Punjabi evidence, and never stretches past the 8 % keylock cap (Quick Cut)."""
import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.music_brain import scene_profile as sp
from app.music_brain import set_learner as sl
from app.music_brain import techniques as tq
from app.tests.py import learned_off_vectors as vectors

HERE = Path(__file__).parents[1]  # app/tests
GOLDEN = json.loads((HERE / "fixtures" / "learned_off_golden.json").read_text())
JS = HERE.parent / "ui" / "static" / "scene-profile.js"
CLASH = ("8A", "2A")                      # camelot 0.0


def _pick(bpm_b, level, store=None, keys=CLASH, stems=True):
    store = copy.deepcopy(vectors.SCENE_STORE if store is None else store)
    f = tq.PairFeatures(100.0, bpm_b, *keys, stems_a=stems, stems_b=stems)
    ranked = tq.rank(f, learned=copy.deepcopy(store), scene=sp.learned_scene(level))
    return tq.learned_pick(ranked, store, key_score=tq.camelot_score(*keys), level=level,
                           tempo_gap=f.tempo_gap if level else None)


def test_scene_tag():
    assert sp.set_scene("aLWCv6MGyho") == "punjabi"
    assert sp.set_scene("mDtud5fLgFQ") is None and sp.set_scene(None) is None
    assert sp.set_label("aLWCv6MGyho") == "DJ Timeless NYC Live Sessions 1"
    assert sp.learned_scene("full") == "punjabi"
    assert sp.learned_scene("handover") is None and sp.learned_scene(None) is None


def test_profile_off_is_main_byte_for_byte():
    """Golden made on main before scene tags; a Punjabi-tagged set's sightings change nothing."""
    for store in (vectors.GLOBAL_STORE, vectors.SCENE_STORE):
        now = json.loads(json.dumps(vectors.compute(store)))
        assert len(now) == GOLDEN["n"]
        assert hashlib.sha256(json.dumps(now, sort_keys=True).encode()).hexdigest() == GOLDEN["sha256"]


def test_global_ranges_ignore_tagged_sets():
    g = tq.scene_store(vectors.SCENE_STORE, None)
    for kind, e in vectors.GLOBAL_STORE.items():
        for k in ("observations", "count", "tempo_gap_max", "key_score_min"):
            assert g[kind][k] == e[k], (kind, k)
    assert g["loop_extend"]["observations"] == []                         # tagged only: nothing global
    p = tq.scene_store(vectors.SCENE_STORE, "punjabi")["stem_intro"]
    assert p["count"] == 8 and p["tempo_gap_max"] == 0.3245 and p["scene_clash"] == 4
    assert tq.scene_store(vectors.GLOBAL_STORE, "punjabi") == vectors.GLOBAL_STORE  # no tagged set: as is


def test_merge_writes_global_ranges_only(tmp_path):
    path = tmp_path / "learned.json"
    obs = [sl.Observation("stem_intro", set_id="s1", at=1.0, track_a="A", track_b="B", tempo_gap=0.02, key_score=0.9),
           sl.Observation("stem_intro", set_id="aLWCv6MGyho", at=2.0, track_a="C", track_b="D", tempo_gap=0.33, key_score=0.0)]
    e = sl.merge(obs, path=path)["stem_intro"]
    assert e["count"] == 2 and e["tempo_gap_max"] == 0.02 and e["key_score_min"] == 0.9


def test_clash_stem_intro_allowed_only_under_full():
    full = _pick(101.0, "full")
    assert full["recipe"] == "Long Blend" and full["kind"] == "stem_intro" and full["degraded"] is None
    assert full["clash"] == "learned from DJ Timeless NYC Live Sessions 1: 4 key-clash stem intros"
    assert full["seen"] == 8                                               # global 3 + Punjabi 5
    assert _pick(101.0, None) is None                                      # off: today's gate
    assert _pick(101.0, "handover") is None                                # one side Punjabi: no exemption


def test_clash_needs_enough_punjabi_evidence():
    thin = copy.deepcopy(vectors.SCENE_STORE)
    obs = thin["stem_intro"]["observations"]
    tagged_clash = [o for o in obs if o["set_id"] == vectors.TAGGED and o["key_score"] < tq.LEARNED_MIN_KEY]
    for o in tagged_clash[2:]:
        obs.remove(o)                                                      # 2 clashing sightings < 3
    assert _pick(101.0, "full", store=thin) is None
    assert sp.PUNJABI_PROFILE["learned_clash_min_obs"] == 3
    assert not sp.learned_clash_ok("full", 2) and sp.learned_clash_ok("full", 3)


def test_tempo_never_past_keylock_cap():
    wide = _pick(100.0 * (1 - 0.3245), "full")                             # the 32 % sighting (folded)
    assert wide["recipe"] == "Quick Cut" and wide["planned"] == "Long Blend"
    assert wide["degraded"].startswith("tempo gap 32.5% past the 8% keylock cap")
    mid = _pick(117.8, "full", keys=("8A", "9A"))                           # 17.8 %, keys fine
    assert mid["recipe"] == "Quick Cut" and mid["planned"] == "Long Blend"
    assert _pick(117.8, None, keys=("8A", "9A")) is None                   # off: tagged sighting unseen
    # sweep: under full, nothing but the cut past the cap
    for bpm_b in [100.0 + 0.5 * i for i in range(0, 120)]:
        p = _pick(bpm_b, "full")
        if p and tq._fold_gap(100.0, bpm_b) > tq.MAX_KEYLOCK_STRETCH:
            assert p["recipe"] == "Quick Cut", bpm_b
    assert sp.PUNJABI_PROFILE["learned_tempo_cap"] == tq.MAX_KEYLOCK_STRETCH


def test_the_three_32pct_sightings_do_not_octave_fold():
    """set-study-aLWCv6MGyho s4: Big Dawgs 137.14 -> Pretty Girls Walk 90.01, Candy Shop 98.02 ->
    Dilbar 131.81, I Like It 150.01 -> Headlines 101.33 (data/cache/analysis v5 BPMs). x2 / x0.5
    leaves them at 31-33 %: they get the Quick Cut. A 3:4 / 2:3 fold would put them under 2 %."""
    for a, b, want in ((137.14, 90.01, 0.3127), (98.02, 131.81, 0.3276), (150.01, 101.33, 0.3245)):
        assert sl.tempo_gap(a, b) == pytest.approx(want, abs=1e-4)
        assert not sp.learned_tempo_ok("full", tq._fold_gap(a, b))
        assert tq._fold_gap(a, b, extra=True) < 0.02


def test_endpoint_passes_profile(monkeypatch):
    import app.ui.server as srv
    monkeypatch.setattr(sl, "load_learned", lambda *a, **k: copy.deepcopy(vectors.SCENE_STORE))
    monkeypatch.setattr(srv, "_pair_features_cached", lambda a, b, keylock: tq.PairFeatures(
        100.0, 101.0, *CLASH, stems_a=True, stems_b=True))
    steps = []
    monkeypatch.setattr(srv, "_song_step", lambda *a, **k: steps.append(k))
    assert srv.get_learned_pick("a", "b")["pick"] is None
    assert srv.get_learned_pick("a", "b", profile="bogus")["pick"] is None
    assert srv.get_learned_pick("a", "b", profile="handover")["pick"] is None
    pick = srv.get_learned_pick("a", "b", profile="full")["pick"]
    assert pick["recipe"] == "Long Blend" and pick["level"] == "full"
    assert steps[-1]["why"].startswith("learned from DJ Timeless NYC Live Sessions 1: 4 key-clash stem intros")
    assert steps[-1]["inputs"]["profile"] == "full" and "profile" not in steps[0]["inputs"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_rule_matches_python():
    probe = (
        f"const S = require({json.dumps(str(JS))});"
        "const c = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        "process.stdout.write(JSON.stringify({"
        " scene: c.lvls.map((l) => S.learnedScene(l)),"
        " clash: c.clash.map(([l, n]) => S.learnedClashOk(l, n)),"
        " tempo: c.tempo.map(([l, g]) => S.learnedTempoOk(l, g))}));"
    )
    lvls = ["full", "handover", None, "x"]
    cases = {"lvls": lvls,
             "clash": [[l, n] for l in lvls for n in (None, 0, 2, 3, 9)],
             "tempo": [[l, g] for l in lvls for g in (None, 0.0, 0.0499, 0.08, 0.0801, 0.3245)]}
    out = subprocess.run(["node", "-e", probe], input=json.dumps(cases), capture_output=True, text=True, check=True)
    js = json.loads(out.stdout)
    assert js["scene"] == [sp.learned_scene(l) for l in lvls]
    assert js["clash"] == [sp.learned_clash_ok(l, n) for l, n in cases["clash"]]
    assert js["tempo"] == [sp.learned_tempo_ok(l, g) for l, g in cases["tempo"]]
