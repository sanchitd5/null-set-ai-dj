"""dedup_songs: name identity, audio confirmation, canonical pick, quarantine apply / restore, prevention.
All on tmp_path fixtures; no network, no audio decoding (the tool works from the cached analysis)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from app.ui.services import dedup_songs as ds


# ------------------------------------------------------------------------------------ names


def same(a: str, b: str) -> str:
    return ds.name_match(ds.identity(a), ds.identity(b))


@pytest.mark.parametrize("a,b", [
    ("Fred again.. - Marea (We’ve Lost Dancing)", "Fred again.. feat. The Blessed Madonna - Marea (We’ve Lost Dancing) (Official Audio)"),
    ("Tinlicker & Helsloot - Because You Move Me", "Tinlicker & Helsloot - Because You Move Me (Official Music Video)"),
    ("Charli xcx - Vroom Vroom (Audio)", "Charli XCX - Vroom Vroom [Official Lyric Video]"),
    ("Artist - Song (Official HD Video)", "Artist - Song [4K] (Visualizer)"),
    ("Aqua - Barbie Girl (Lyrical Video)", "AQUA - Barbie Girl (Official Audio) HQ"),
    ("BICEP - BICEP | GLUE (Official Video)", "BICEP - Glue"),
    ("Rosalía - Despechá (Official Audio)", "ROSALIA - DESPECHA (Letra/Lyrics)"),
    ("The Weeknd - Starboy (Audio) ft. Daft Punk", "The Weeknd - Starboy ft. Daft Punk (Official Video) ft. Daft Punk"),
    ("Flume & Kai - Never Be Like You", "Flume - Never Be Like You feat. Kai"),
])
def test_upload_noise_variants_are_the_same_song(a, b):
    assert same(a, b) == "strong"


@pytest.mark.parametrize("marker", [
    "(Kordhell Remix)", "(Live at Red Rocks)", "(Acoustic)", "(Slowed + Reverb)", "(Instrumental)", "(Sped Up)",
    "(Nightcore)", "(Radio Edit)", "(Fred V Bootleg)", "(Extended Mix)", "(Karaoke Version)", "(Remastered 2011)",
    "(Cover)", "(VIP)", "(Flip)", "(Rework)", "(Stripped)", "(8D Audio)",
])
def test_remix_like_versions_stay_separate(marker):
    plain = "Skrillex - Bangarang (Official Audio)"
    assert same(plain, f"Skrillex - Bangarang {marker}") == "no"
    assert same(f"Skrillex - Bangarang {marker}", f"Skrillex - Bangarang {marker} [Official Audio]") == "strong"


def test_different_remixers_stay_separate_and_bare_remix_word_counts():
    assert same("A - B (Kordhell Remix)", "A - B (HUM3N Remix)") == "no"
    assert same("Daddy Yankee - Con Calma Remix", "Daddy Yankee - Con Calma") == "no"
    assert same("Daddy Yankee - Con Calma (Rakurs Remix)", "Daddy Yankee - Con Calma") == "no"


def test_different_artist_or_title_is_not_a_match_and_missing_artist_is_weak():
    assert same("Fred again.. - Jungle", "Skrillex - Jungle") == "no"
    assert same("Fred again.. - Jungle", "Fred again.. - Delilah") == "no"
    assert same("So High", "Doja Cat - So High (Official Audio)") == "weak"


# ------------------------------------------------------------------------------------ fixtures


def curve(seed: int, n: int = 240) -> list:
    rng = np.random.default_rng(seed)
    base = np.cumsum(rng.normal(size=n))
    return [round(float(v), 4) for v in base]


def make_track(cache: Path, tid: str, name: str, *, seed: int, dur: float = 240.0, bpm: float = 120.0,
               key: str = "8A", stems: bool = False, keylock: bool = False, size: int = 100,
               shift: int = 0, mtime: float = 1000.0) -> str:
    """One library track with the files the app derives from it.  Returns the full hash."""
    full = tid + "0" * 48
    up = cache / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    f = up / f"{tid}.flac"
    f.write_bytes(b"x" * size)
    os.utime(f, (mtime, mtime))
    names = json.loads((up / "_names.json").read_text()) if (up / "_names.json").exists() else {}
    names[tid] = name
    (up / "_names.json").write_text(json.dumps(names))
    c = curve(seed)
    if shift:
        c = curve(seed, 240 + shift)[shift:]        # the same song with `shift` seconds cut off the start
    (cache / "analysis").mkdir(exist_ok=True)
    (cache / "analysis" / f"{full}.v5.json").write_text(json.dumps({
        "duration": dur, "bpm": bpm, "key": {"camelot": key}, "energy_curve": c,
        "energy_times": [float(i) for i in range(len(c))]}))
    (cache / "analysis" / f"{full}.vibe.json").write_text("{}")
    if stems:
        d = cache / "stems" / f"{full}_htdemucs_ft"
        d.mkdir(parents=True, exist_ok=True)
        (d / "vocals.wav").write_bytes(b"s" * 1000)
    if keylock:
        d = cache / "keylock" / f"t{full[:24]}_120p0"
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text("{}")
        (d / "bass.wav").write_bytes(b"k" * 500)
    (cache / "waveforms").mkdir(exist_ok=True)
    (cache / "waveforms" / f"{tid}-v1-4.json").write_text("{}")
    return full


def snapshot(root: Path) -> dict:
    def body(p: Path):                   # json compares as data (a rewrite may change the indentation)
        if not p.is_file():
            return None
        raw = p.read_bytes()
        return json.loads(raw) if p.suffix == ".json" else raw

    return {str(p.relative_to(root)): body(p) for p in sorted(root.rglob("*")) if QUAR not in p.parts}


QUAR = ds.QUARANTINE
A, B, C = "a" * 16, "b" * 16, "c" * 16


@pytest.fixture
def lib(tmp_path):
    cache = tmp_path / "cache"
    make_track(cache, A, "Artist - Song (Official Audio)", seed=1, stems=True, keylock=True, size=300)
    make_track(cache, B, "Artist - Song (Official Lyric Video)", seed=1, stems=True, size=200)
    make_track(cache, C, "Other - Thing", seed=9, stems=True, bpm=99.0, key="3B")
    (cache / "fame.json").write_text(json.dumps({A: {"views": 5}, B: {"views": 6}, C: {"views": 1}}))
    (cache / "set_memory.json").write_text(json.dumps({"artist - song": {"name": "Artist - Song"}}))
    return cache


# ------------------------------------------------------------------------------------ audio + groups


def test_name_and_audio_match_makes_a_group_and_keeps_the_worked_on_copy(lib):
    groups, reviews = ds.find_groups(ds.scan(lib))
    assert len(groups) == 1 and not reviews
    g = groups[0]
    assert g.canonical.id == A and [d.id for d in g.duplicates] == [B]
    assert "stems=y" in g.why and "keylock=1" in g.why
    assert g.duplicates[0].sizes["stems"] == 1000 and g.duplicates[0].sizes["songs"] == 200


def test_canonical_prefers_work_done_then_clean_source_then_bitrate_then_oldest(tmp_path):
    cache = tmp_path / "c"
    x, y = "1" * 16, "2" * 16
    make_track(cache, x, "Artist - Song (Official Music Video)", seed=4, stems=True)
    make_track(cache, y, "Artist - Song (Official Audio)", seed=4)          # cleaner source but no stems
    t = ds.scan(cache)
    assert ds.pick_canonical([t[x], t[y]])[0].id == x
    make_track(cache, y, "Artist - Song (Official Audio)", seed=4, stems=True)
    t = ds.scan(cache)
    assert ds.pick_canonical([t[x], t[y]])[0].id == y                       # equal work: official audio wins
    make_track(cache, x, "Artist - Song (Official Audio)", seed=4, stems=True, mtime=1.0)
    t = ds.scan(cache)
    assert ds.pick_canonical([t[x], t[y]])[0].id == x                       # all equal: oldest wins


def test_name_match_but_audio_mismatch_goes_to_review_not_a_group(tmp_path):
    cache = tmp_path / "c"
    make_track(cache, A, "Artist - Song", seed=1)
    make_track(cache, B, "Artist - Song (Official Audio)", seed=2)          # other energy curve
    make_track(cache, C, "Artist - Song (Official Video)", seed=1, bpm=140.0)
    groups, reviews = ds.find_groups(ds.scan(cache))
    assert groups == []
    assert {r.kind for r in reviews} == {"name match, audio mismatch"}
    assert len(reviews) == 3


def test_intro_offset_and_duration_gap_are_tolerated_only_with_a_fingerprint_match(tmp_path):
    cache = tmp_path / "c"
    make_track(cache, A, "Artist - Song", seed=3, dur=240.0)
    make_track(cache, B, "Artist - Song (Lyric Video)", seed=3, dur=250.0, shift=0)
    make_track(cache, C, "Artist - Song (Official Video)", seed=7, dur=250.0)   # same length, other audio
    groups, reviews = ds.find_groups(ds.scan(cache))
    assert [len(g.duplicates) for g in groups] == [1]
    assert {r.b.id for r in reviews} | {r.a.id for r in reviews} >= {C}


def test_weak_name_with_matching_audio_and_audio_only_matches_are_review_only(tmp_path):
    cache = tmp_path / "c"
    make_track(cache, A, "Doja Cat - So High", seed=5)
    make_track(cache, B, "So High", seed=5)
    make_track(cache, C, "Someone - Different Name", seed=5)
    groups, reviews = ds.find_groups(ds.scan(cache))
    assert groups == []
    assert {r.kind for r in reviews} == {"weak name (artist unknown), audio match", "audio match, names differ"}


def test_remix_pair_with_identical_audio_is_never_grouped(tmp_path):
    cache = tmp_path / "c"
    make_track(cache, A, "Artist - Song", seed=5)
    make_track(cache, B, "Artist - Song (Kordhell Remix)", seed=5)
    groups, _ = ds.find_groups(ds.scan(cache))
    assert groups == []


def test_chain_match_does_not_merge_two_copies_that_only_match_through_a_third(tmp_path, monkeypatch):
    cache = tmp_path / "c"
    make_track(cache, A, "Artist - Song", seed=1, stems=True)
    make_track(cache, B, "Artist - Song (Audio)", seed=1)
    make_track(cache, C, "Artist - Song (Lyrics)", seed=1)
    real = ds.audio_compare

    def fake(a, b):
        if {a.id, b.id} == {B, C}:
            return ds.AudioResult(False, "forced")
        return real(a, b)

    monkeypatch.setattr(ds, "audio_compare", fake)
    groups, reviews = ds.find_groups(ds.scan(cache))
    assert len(groups) == 1 and len(groups[0].duplicates) == 2            # both match the kept copy directly

    def fake2(a, b):                      # B and C match the kept copy A only through each other
        if A in (a.id, b.id) and {a.id, b.id} & {C}:
            return ds.AudioResult(False, "forced")
        return real(a, b)

    monkeypatch.setattr(ds, "audio_compare", fake2)
    groups, reviews = ds.find_groups(ds.scan(cache))
    assert [d.id for d in groups[0].duplicates] == [B] and any(r.kind.startswith("chain") or C in (r.a.id, r.b.id) for r in reviews)


def test_groove_keylock_sets_are_attributed_through_the_key_formula(tmp_path):
    import hashlib

    cache = tmp_path / "c"
    full = make_track(cache, A, "Artist - Song", seed=1)
    meta = {"ratio": 0.9, "a_groove": [31.5, 62.0], "a_solo": [62.0, 78.0]}
    key = hashlib.sha256(f"{full}|0.900000|31.500|78.000".encode()).hexdigest()[:20]
    d = cache / "keylock" / key
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(meta))
    assert ds.scan(cache)[A].files["keylock"] == [d]


# ------------------------------------------------------------------------------------ apply / restore


def run_apply(cache, **kw):
    groups, _ = ds.find_groups(ds.scan(cache))
    return ds.apply(cache, groups, is_running=lambda: False, stamp="T1", **kw)


def test_apply_moves_the_duplicate_to_quarantine_with_manifest_and_alias_map(lib):
    m = run_apply(lib)
    q = lib / QUAR / "T1"
    assert not (lib / "uploads" / f"{B}.flac").exists() and (q / "files" / "uploads" / f"{B}.flac").exists()
    assert not list((lib / "stems").glob(f"{B}*")) and list((lib / "stems").glob(f"{A}*"))
    assert not list((lib / "analysis").glob(f"{B}*")) and not (lib / "waveforms" / f"{B}-v1-4.json").exists()
    assert (lib / "uploads" / f"{A}.flac").exists() and (lib / "uploads" / f"{C}.flac").exists()
    assert json.loads((lib / ds.ALIASES_FILE).read_text()) == {B: A}
    assert ds.resolve_alias(B, lib) == A and ds.resolve_alias(C, lib) == C
    man = json.loads((q / "manifest.json").read_text())
    assert man == json.loads(json.dumps(m)) and man["groups"][0]["duplicates"] == [B]
    kinds = {x["kind"] for x in man["groups"][0]["moved"]}
    assert {"songs", "stems", "analysis", "waveforms"} <= kinds
    assert all({"original", "quarantined", "size", "id"} <= set(x) for x in man["groups"][0]["moved"])
    assert B not in json.loads((lib / "uploads" / "_names.json").read_text())
    fame = json.loads((lib / "fame.json").read_text())
    assert B not in fame and A in fame and C in fame
    assert json.loads((lib / "set_memory.json").read_text())      # name-keyed memory untouched


def test_apply_is_idempotent_and_a_second_scan_finds_nothing(lib):
    run_apply(lib)
    after = snapshot(lib)
    groups, _ = ds.find_groups(ds.scan(lib))
    assert groups == []
    m2 = ds.apply(lib, groups, is_running=lambda: False, stamp="T2")
    assert m2["groups"] == [] and snapshot(lib) == after


def test_restore_round_trips_files_names_fame_and_aliases(lib):
    before = snapshot(lib)
    run_apply(lib)
    assert snapshot(lib) != before
    out = ds.restore(lib, "T1", is_running=lambda: False)
    assert out["restored"] > 0 and out["skipped_occupied"] == []
    after = snapshot(lib)
    after.pop(ds.ALIASES_FILE, None)
    assert after == before and ds.load_aliases(lib) == {}
    assert ds.restore(lib, "T1", is_running=lambda: False)["restored"] == 0      # repeating is safe
    groups, _ = ds.find_groups(ds.scan(lib))
    assert len(groups) == 1                                                        # the duplicate is back


def test_restore_never_overwrites_an_occupied_path(lib):
    run_apply(lib)
    (lib / "uploads" / f"{B}.flac").write_bytes(b"newer")
    out = ds.restore(lib, "T1", is_running=lambda: False)
    assert str(lib / "uploads" / f"{B}.flac") in out["skipped_occupied"]
    assert (lib / "uploads" / f"{B}.flac").read_bytes() == b"newer"


def test_apply_tolerates_files_vanishing_and_rolls_back_a_failing_group(lib, monkeypatch):
    groups, _ = ds.find_groups(ds.scan(lib))
    next(iter((lib / "stems").glob(f"{B}*"))).joinpath("vocals.wav").unlink()      # vanished after the scan
    (lib / "waveforms" / f"{B}-v1-4.json").unlink()
    m = ds.apply(lib, groups, is_running=lambda: False, stamp="T1")
    assert m["groups"][0]["duplicates"] == [B] and m["groups"][0]["missing"] == [f"waveforms/{B}-v1-4.json"]

    lib2 = lib.parent / "cache2"
    import shutil
    shutil.copytree(lib, lib2)
    ds.restore(lib2, "T1", is_running=lambda: False)
    snap = snapshot(lib2)
    groups2, _ = ds.find_groups(ds.scan(lib2))
    monkeypatch.setattr(ds, "_remap_state", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        ds.apply(lib2, groups2, is_running=lambda: False, stamp="T9")
    assert snapshot(lib2) == snap and ds.load_aliases(lib2) == {}       # group put back, nothing half-applied


def test_apply_refuses_while_the_app_runs_and_group_flag_limits_scope(lib, tmp_path):
    groups, _ = ds.find_groups(ds.scan(lib))
    with pytest.raises(ds.ApplyRefused):
        ds.apply(lib, groups, is_running=lambda: True)
    assert (lib / "uploads" / f"{B}.flac").exists()
    with pytest.raises(ds.ApplyRefused):
        ds.apply(lib, groups, only="gdeadbeef", is_running=lambda: False)
    ds.apply(lib, groups, only=B, is_running=lambda: False, stamp="T1")           # any member id names the group
    assert not (lib / "uploads" / f"{B}.flac").exists()


def test_purge_deletes_the_quarantine_only_when_asked(lib):
    run_apply(lib)
    assert (lib / QUAR / "T1").is_dir()
    freed = ds.purge(lib, "T1")
    assert freed > 0 and not (lib / QUAR / "T1").exists() and (lib / "uploads" / f"{A}.flac").exists()
    with pytest.raises(ds.ApplyRefused):
        ds.purge(lib, "T1")


def test_cli_dry_run_writes_report_and_deletes_nothing(lib, tmp_path, capsys):
    before = snapshot(lib)
    rc = ds.main(["--cache", str(lib), "--report", str(tmp_path / "r.md")])
    assert rc == 0 and snapshot(lib) == before
    text = (tmp_path / "r.md").read_text()
    assert "dry run" in text and B in text and "REVIEW" in text and "—" not in text
    assert ds.main(["--cache", str(lib), "--purge", "T1"]) == 2                   # needs --yes


# ------------------------------------------------------------------------------------ prevention


def test_wanted_from_search_urls():
    assert ds.wanted_from_url("ytmsearch:Fred again.. - Delilah") == "Fred again.. - Delilah"
    assert ds.wanted_from_url("ytsearch5:Fred again.. - Delilah audio") == "Fred again.. - Delilah"
    assert ds.wanted_from_url("https://www.youtube.com/watch?v=x") is None
    assert ds.wanted_from_url("ytmsearch:just words") is None


def test_find_existing_reuses_same_recording_but_not_a_remix_and_resolves_aliases(tmp_path):
    names = {A: "Artist - Song (Official Video)", B: "Artist - Song (Audio)", C: "Artist - Song (Kordhell Remix)"}
    assert ds.find_existing("Artist - Song", names) == B                             # cleanest upload
    assert ds.find_existing("Artist - Song (Kordhell Remix)", names) == C
    assert ds.find_existing("Artist - Song (HUM3N Remix)", names) is None
    assert ds.find_existing("Artist - Other", names) is None
    assert ds.find_existing("Song", names) is None                                    # no artist: never reuse
    (tmp_path / ds.ALIASES_FILE).write_text(json.dumps({B: A}))
    assert ds.find_existing("Artist - Song", {B: "Artist - Song (Audio)"}, cache=tmp_path) == A
    assert ds.find_existing("Artist - Song", names, exists=lambda i: i != B, cache=None) == A


def test_download_job_reuses_the_library_copy_instead_of_downloading(tmp_path):
    from app.tests.py.test_engine_injection import FakeAI, FakeHost
    from app.ui.services import download_jobs, engine

    def boom(*a, **k):
        raise AssertionError("must not download")

    with engine.using(engine.Engine(FakeHost(), FakeAI())):
        jid = download_jobs.start_job(
            "ytmsearch:Artist - Song", "l", tmp_path, download_fn=boom, register_fn=boom,
            analyze_fn=lambda t: type("A", (), {"duration": 200.0, "bpm": 120.0})(),
            reuse_fn=lambda url: [{"track_id": A, "filename": f"{A}.flac", "display_name": "Artist - Song"}])
        job = download_jobs.get_job(jid)
    assert job["state"] == "done" and job["tracks"][0]["track_id"] == A and job["tracks"][0]["duration"] == 200.0


def test_official_audio_is_preferred_over_lyric_uploads_on_the_first_search_pass():
    from app.ui.services import download_service as dl

    song = (["artist"], ["song"], "Song")
    strict, lenient = dl._search_match_filter(["artist", "song"], song, True), dl._search_match_filter(["artist", "song"], song)
    lyric = {"title": "Artist - Song (Official Lyric Video)", "duration": 200, "channel": "Artist"}
    audio = {"title": "Artist - Song (Official Audio)", "duration": 200, "channel": "Artist"}
    assert strict(lyric) and strict(audio) is None
    assert lenient(lyric) is None and lenient(audio) is None


# ------------------------------------------------------------------------------------ --include-review


@pytest.fixture
def rev(tmp_path):
    """A, B, C: same name, three different energy curves -> three REVIEW pairs, one component."""
    cache = tmp_path / "cache"
    make_track(cache, A, "Artist - Song", seed=1, stems=True)
    make_track(cache, B, "Artist - Song (Official Audio)", seed=2)
    make_track(cache, C, "Artist - Song (Official Video)", seed=3)
    make_track(cache, "d" * 16, "Other - Thing", seed=9, bpm=99.0, key="3B")
    return cache


def rgroups(cache):
    groups, reviews = ds.find_groups(ds.scan(cache))
    return groups, ds.review_groups(groups, reviews, cache)


def test_review_chain_component_keeps_one_copy_by_the_canonical_rule(rev):
    groups, rg = rgroups(rev)
    assert groups == [] and len(rg) == 1
    assert rg[0].canonical.id == A and {d.id for d in rg[0].duplicates} == {B, C}     # stems win
    assert all(v.startswith("REVIEW: name match, audio mismatch") for v in rg[0].evidence.values())


def test_review_pair_keeps_exactly_one(tmp_path):
    cache = tmp_path / "c"
    make_track(cache, A, "Artist - Song", seed=1)
    make_track(cache, B, "Artist - Song (Official Audio)", seed=2)
    _, rg = rgroups(cache)
    assert len(rg) == 1 and len(rg[0].duplicates) == 1 and rg[0].canonical.id == A    # all equal: id order


def test_review_never_drops_alias_target_or_confirmed_canonical(rev):
    (rev / ds.ALIASES_FILE).write_text(json.dumps({"z" * 16: C}))
    _, rg = rgroups(rev)
    assert rg[0].canonical.id == C and {d.id for d in rg[0].duplicates} == {A, B}     # alias target wins the pick
    (rev / ds.ALIASES_FILE).write_text(json.dumps({"y" * 16: A, "z" * 16: C}))
    _, rg = rgroups(rev)
    assert {d.id for d in rg[0].duplicates} == {B}                                    # both protected stay


def test_review_dry_run_deletes_nothing_and_lists_keep_drop(rev, tmp_path):
    before = snapshot(rev)
    assert ds.main(["--cache", str(rev), "--include-review", "--report", str(tmp_path / "r.md")]) == 0
    assert snapshot(rev) == before
    text = (tmp_path / "r.md").read_text()
    assert f"KEEP `{A}`" in text and f"DUP  `{B}`" in text and "REVIEW component" in text
    assert "REVIEW: name match" in text and "—" not in text


def test_review_apply_restore_alias_and_idempotent(rev):
    before = snapshot(rev)
    _, rg = rgroups(rev)
    m = ds.apply(rev, rg, is_running=lambda: False, stamp="T1")
    assert not (rev / "uploads" / f"{B}.flac").exists() and not (rev / "uploads" / f"{C}.flac").exists()
    assert (rev / "uploads" / f"{A}.flac").exists()
    assert ds.resolve_alias(B, rev) == A and ds.resolve_alias(C, rev) == A
    assert len(m["groups"]) == 1
    groups, reviews = ds.find_groups(ds.scan(rev))
    assert reviews == [] and ds.review_groups(groups, reviews, rev) == []             # idempotent
    ds.restore(rev, "T1", is_running=lambda: False)
    after = snapshot(rev)
    after.pop(ds.ALIASES_FILE, None)
    assert after == before


def test_review_apply_refuses_while_app_runs_and_flag_off_is_unchanged(rev, capsys):
    _, rg = rgroups(rev)
    with pytest.raises(ds.ApplyRefused):
        ds.apply(rev, rg, is_running=lambda: True)
    assert (rev / "uploads" / f"{B}.flac").exists()
    ds.main(["--cache", str(rev)])
    assert "REVIEW component" not in capsys.readouterr().out
    assert ds.main(["--cache", str(rev), "--apply", "--app-port", "1"]) == 0          # no groups: nothing moves
    assert (rev / "uploads" / f"{B}.flac").exists()
