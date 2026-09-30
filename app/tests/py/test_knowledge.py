"""knowledge/: export (cache -> tracked folder) and seed (tracked folder -> another cache).
Synthetic tmp_path caches only; the atlas is a hand-made doc with the current rules hash."""
import json

import pytest

from app.music_brain.matching import knowledge as kn
from app.music_brain import macros as mc, pair_atlas as pa

A, B, C = "a" * 16, "b" * 16, "c" * 16
NAMES = {A: "Anyma - Eternity", B: "Cassian - SOS", C: "Argy - WIND"}


def _pair(a, b, works, **kw):
    p = {"a": a, "b": b, "works": works, "works_base": works, "best": "Bass Swap", "recipe": "Bass Swap",
         "key": 0.9, "gap": 0.01, "lock": "a", "exit": 120.0, "entry": 8.0, "energy": {"ok": True},
         "merge": {"ok": False, "gate": "vocal"}, "moves": {}, "combo": None, "sig": "deadbeefdeadbeef"}
    p.update(kw)
    return p


def _cache(root, names=NAMES, files=True):
    root.mkdir(parents=True, exist_ok=True)
    (root / "uploads").mkdir()
    (root / "uploads" / "_names.json").write_text(json.dumps(names))
    if files:
        for t in names:
            (root / "uploads" / f"{t}.mp3").write_bytes(t.encode())
    return root


@pytest.fixture
def src(tmp_path):
    cache = _cache(tmp_path / "home")
    tracks = {t: {"name": n, "artist": n.split(" - ")[0], "bpm": 124.0, "key": "8A", "duration": 200.0,
                  "level": 3, "stems": True, "sig": "x", "bars": [0.1] * 50,
                  "vox": [[1.0, 2.0]]} for t, n in NAMES.items()}
    pairs = {f"{A}>{B}": _pair(A, B, 88, combo="studied", studied={"recipe": "Bass Swap", "count": 2},
                               played={"good": 2, "bad": 0, "sessions": ["2026-09-28_212853"]}),
             f"{B}>{C}": _pair(B, C, 70), f"{A}>{C}": _pair(A, C, 10)}
    doc = {"schema": pa.SCHEMA, "rules": pa.rules_hash(), "built_at": 1.5, "cache_dir": str(cache),
           "tracks": tracks, "pairs": pairs, "stats": {}}
    (cache / "pair_atlas.json").write_text(json.dumps(doc))
    mc.write_seed({"name": "studied-s1-1", "source": "atlas:studied", "title": "Anyma @ Atomium #1",
                   "steps": [{"a": A, "b": B, "a_name": NAMES[A], "b_name": NAMES[B], "recipe": "Bass Swap",
                              "a_time": 120.0, "b_time": 8.0}]}, cache)
    mc.write_seed({"name": "chain-1-anyma", "source": "atlas:chain",
                   "steps": [{"a": A, "b": B, "recipe": "Bass Swap"}, {"a": B, "b": C, "recipe": "Echo Out"}]}, cache)
    (cache / "learned_techniques.json").write_text(json.dumps({"bass_swap": {
        "kind": "bass_swap", "what": "x", "stems": True, "live": True, "count": 1,
        "observations": [{"kind": "bass_swap", "set_id": "S1", "at": 10.0, "track_a": NAMES[A],
                          "track_b": NAMES[B], "tempo_gap": 0.01, "key_score": 0.9, "detail": {}}]}}))
    out = tmp_path / "knowledge"
    rep = kn.export(cache, out)
    return {"cache": cache, "out": out, "rep": rep}


def _all_bytes(d):
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def test_export_is_deterministic(src, tmp_path):
    first = _all_bytes(src["out"])
    again = kn.export(src["cache"], src["out"])
    assert again["changed"] == [], "an unchanged cache rewrites nothing"
    other = tmp_path / "k2"
    kn.export(src["cache"], other)
    assert _all_bytes(other) == first
    assert set(first) == {"macros/studied-s1-1.json", "macros/chain-1-anyma.json", "learned_techniques.json",
                          "atlas/meta.json", f"atlas/pairs/{A}.json.gz", f"atlas/pairs/{B}.json.gz", "names.json"}
    assert src["rep"]["macros"] == 2 and src["rep"]["observations"] == 1 and src["rep"]["atlas"]["pairs"] == 3


def test_export_segmented_round_trip_and_small_diffs(src):
    out = src["out"]
    slim = kn.load_atlas(out)
    assert set(slim["pairs"]) == {f"{A}>{B}", f"{B}>{C}", f"{A}>{C}"} and set(slim["tracks"]) == {A, B, C}
    before = _all_bytes(out)
    # a rebuild alone (new built_at, macros re-stamped "created"): no tracked file changes
    atlas = pa.load(src["cache"])
    atlas["built_at"] = 99.0
    pa.write_atlas(atlas, pa.atlas_path(src["cache"]))
    for p in mc.macros_dir(src["cache"]).glob("*.json"):
        m = json.loads(p.read_text())
        m["created"] = 12345.0
        p.write_text(json.dumps(m))
    assert kn.export(src["cache"], out)["changed"] == []
    assert _all_bytes(out) == before
    # one pair changes: its A shard, plus meta.json (its built_at now moves with the content)
    atlas["pairs"][f"{B}>{C}"]["works"] = 71
    pa.write_atlas(atlas, pa.atlas_path(src["cache"]))
    assert kn.export(src["cache"], out)["changed"] == [f"atlas/pairs/{B}.json.gz", "atlas/meta.json"]
    assert kn.load_atlas(out)["pairs"][f"{B}>{C}"]["works"] == 71


def test_rewriting_an_unchanged_seed_macro_keeps_created(src):
    p = mc.macros_dir(src["cache"]) / "chain-1-anyma.json"
    before, stamp = p.read_bytes(), p.stat().st_mtime_ns
    m = mc.write_seed({"name": "chain-1-anyma", "source": "atlas:chain",
                       "steps": [{"a": A, "b": B, "recipe": "Bass Swap"}, {"a": B, "b": C, "recipe": "Echo Out"}]},
                      src["cache"])
    assert m["created"] == json.loads(before)["created"]
    assert p.read_bytes() == before and p.stat().st_mtime_ns == stamp, "an unchanged macro is not rewritten"


def test_seed_still_reads_the_old_single_gz(src, tmp_path):
    import gzip
    old = tmp_path / "oldk"
    old.mkdir()
    slim = kn.load_atlas(src["out"])
    (old / kn.ATLAS).write_bytes(gzip.compress(json.dumps(slim).encode(), mtime=0))
    (old / kn.NAMES).write_bytes((src["out"] / kn.NAMES).read_bytes())
    cache = _cache(tmp_path / "away", dict(NAMES))
    rep = kn.seed(cache, old)
    assert rep["pairs"] == 3 and set(pa.load(cache)["pairs"]) == set(slim["pairs"])
    assert (pa.atlas_path(cache) / pa.META).is_file(), "seeded into the segmented local folder"


def test_export_has_no_private_strings(src):
    slim = kn.load_atlas(src["out"])
    text = json.dumps(slim) + "".join(b.decode() for k, b in _all_bytes(src["out"]).items() if not k.endswith(".gz"))
    assert "/Users/" not in text and str(src["cache"]) not in text and "2026-09-28_212853" not in text
    assert "sig" not in slim["pairs"][f"{A}>{B}"] and "bars" not in slim["tracks"][A]
    assert kn.privacy_hits({"x": ["ok", "/Users/me/song.mp3"]}) and kn.privacy_hits({"m": "me@example.com"})
    assert not kn.privacy_hits({"t": "Anyma @ Live from Atomium", "d": "4/4 at 1:06:30"})


def test_export_refuses_a_path(src):
    lt = src["cache"] / "learned_techniques.json"
    d = json.loads(lt.read_text())
    d["bass_swap"]["observations"][0]["detail"]["file"] = "/Users/me/x.mp3"
    lt.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="private"):
        kn.export(src["cache"], src["out"])


def test_seed_resolves_by_name_and_skips_unresolved(src, tmp_path):
    # another machine: other downloads of Anyma and Cassian (other ids), no Argy
    a2, b2 = "1" * 16, "2" * 16
    cache = _cache(tmp_path / "away", {a2: "Anyma - Eternity", b2: "Cassian - SOS"})
    rep = kn.seed(cache, src["out"])
    assert rep["macros"] == ["studied-s1-1"]
    assert rep["macros_skipped"] == [{"macro": "chain-1-anyma", "missing": ["Argy - WIND"]}]
    m = mc.load("studied-s1-1", cache)
    assert (m["steps"][0]["a"], m["steps"][0]["b"]) == (a2, b2) and m["title"] == "Anyma @ Atomium #1"
    assert rep["observations"] == 1 and rep["pairs"] == 1, "only A>B has both songs here"
    atlas = pa.load(cache)
    assert set(atlas["pairs"]) == {f"{a2}>{b2}"}
    # the slim pair round-trips: plan_of and from_picks give the studied plan
    assert pa.plan_of(atlas["pairs"][f"{a2}>{b2}"])["recipe"] == "Bass Swap"
    pm = mc.from_picks([a2, b2], atlas, locked=True)
    assert pm["steps"][0]["recipe"] == "Bass Swap" and pm["steps"][0]["a_time"] == 120.0
    again = kn.seed(cache, src["out"])
    assert again["macros"] == [] and again["observations"] == 0 and again["pairs"] == 0, "seeding twice adds nothing"


def test_local_files_are_never_overwritten(src, tmp_path):
    cache = _cache(tmp_path / "away", dict(NAMES))
    mine = {"name": "studied-s1-1", "source": "console", "steps": [{"a": B, "b": A, "recipe": "Echo Out"}]}
    mc.save(mine, cache)
    (cache / "learned_techniques.json").write_text(json.dumps({"bass_swap": {
        "kind": "bass_swap", "what": "x", "stems": True, "live": True, "count": 1,
        "observations": [{"kind": "bass_swap", "set_id": "S1", "at": 99.0, "track_a": "mine", "track_b": "",
                          "detail": {}}]}}))
    local_pair = _pair(A, B, 5, recipe="Echo Out", best="Echo Out")
    (cache / "pair_atlas.json").write_text(json.dumps({"schema": pa.SCHEMA, "rules": pa.rules_hash(),
                                                       "tracks": {}, "pairs": {f"{A}>{B}": local_pair}}))
    rep = kn.seed(cache, src["out"])
    assert mc.load("studied-s1-1", cache)["source"] == "console", "the user's macro wins"
    assert rep["observations"] == 0, "set S1 is the local store's"
    atlas = pa.load(cache)
    assert atlas["pairs"][f"{A}>{B}"]["works"] == 5 and rep["pairs"] == 2, "local pair kept, missing ones added"


def test_auto_seed_runs_once_per_change(src, tmp_path, monkeypatch):
    cache = _cache(tmp_path / "away", dict(NAMES))
    calls = []
    real = kn.seed
    monkeypatch.setattr(kn, "seed", lambda c, s, log=None: calls.append(1) or real(c, s))
    assert kn.auto_seed(cache, src["out"])["macros"]
    assert kn.auto_seed(cache, src["out"]) is None
    kn._SEEN.clear()
    assert kn.auto_seed(cache, src["out"]) is None, "the stamp file survives a restart"
    assert calls == [1]
