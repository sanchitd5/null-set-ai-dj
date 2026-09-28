"""Synced lyrics, hook detection, LRC alignment, hook-drop planning, acapella-drop learning."""
import numpy as np

from app.music_brain import hook_drop, lyrics
from app.music_brain import set_learner as sl
from app.music_brain import techniques as tq

LRC = """[00:10.00] verse one
[00:14.00] take me higher
[00:18.00] verse two
[00:22.00] take me higher
[00:26.00]
[00:30.00] take me higher
[00:32.40] outro"""


def test_parse_hooks_and_words():
    lines = lyrics.parse_lrc(LRC)
    assert [l["text"] for l in lines][:2] == ["verse one", "take me higher"]
    assert lines[3]["end"] == 26.0                      # blank line ends the previous one
    h = lyrics.hooks(lines)
    assert h[0]["text"] == "take me higher" and h[0]["count"] == 3 and h[0]["times"] == [14.0, 22.0, 30.0]
    assert lyrics.words_between(lines, 15, 19) == "take me higher / verse two"
    assert lyrics.is_hook("Take me higher!", lines) and not lyrics.is_hook("verse one", lines)


def _cand(artist, track, dur, lrc=LRC, **kw):
    return {"artistName": artist, "trackName": track, "duration": dur, "syncedLyrics": lrc} | kw


def test_fetch_only_takes_a_provable_match_and_caches(tmp_path):
    calls = []
    cands = [_cand("Sabrina", "i am a party", 200), _cand("Fred again..", "i am a party (live)", 300),
             _cand("Fred again..", "i am a party", 212)]
    search = lambda a, t: calls.append((a, t)) or cands
    got = lyrics.fetch("Fred again.. - i am a party (Official Audio)", tmp_path, search=search, duration=210)
    assert calls == [("Fred again..", "i am a party")] and len(got) == 6
    assert lyrics.fetch("Fred again.. - i am a party (Official Audio)", tmp_path, search=search, duration=210) == got
    assert len(calls) == 1                                                   # cached
    # wrong artist, or right song at another length: nothing
    assert lyrics.fetch("Fred again.. - x", tmp_path, search=lambda a, t: [_cand("Sabrina", "x", 200)]) == []
    assert lyrics.fetch("Fred again.. - y", tmp_path, search=lambda a, t: [_cand("Fred again..", "y", 150)], duration=210) == []
    assert lyrics.fetch("Nope - z", tmp_path, search=lambda a, t: 1 / 0) == []   # network error: not cached
    assert lyrics.split_title("J Balvin Feat. Jowell - Bonita Audio") == ("J Balvin Feat. Jowell", "Bonita")


def test_credit_and_chinese_lines_are_dropped_and_manual_wins(tmp_path):
    lrc = "[00:00.00] 作词 : Fred again../Jim Legxacy\n[00:00.50] 作曲 : Fred again..\n[00:01.00] Written by: X\n" \
          "[00:02.00] back it up\n[00:04.00] 我爱你 (translation)\n[00:06.00] dump it"
    assert [l["text"] for l in lyrics.parse_lrc(lrc)] == ["back it up", "dump it"]
    zh = "[00:01.00] 我爱你\n[00:03.00] 你好吗"                            # a Chinese song keeps its words
    assert len(lyrics.parse_lrc(zh)) == 2
    lyrics.set_manual("A - B", None, tmp_path)                             # pinned: no lyrics
    assert lyrics.fetch("A - B", tmp_path, search=lambda a, t: [_cand("A", "B", 100)]) == []
    lyrics.set_manual("A - B", "[00:01.00] mine", tmp_path)
    assert [l["text"] for l in lyrics.fetch("A - B", tmp_path, search=lambda a, t: 1 / 0)] == ["mine"]


def test_misaligned_lyrics_are_dropped():
    sr = 1000
    y = np.random.default_rng(0).normal(0, 0.1, 50 * sr)                  # vocal everywhere: nothing lines up
    assert lyrics.align(lyrics.parse_lrc(LRC), y, sr)["lines"] == []


def test_align_finds_the_edit_offset():
    sr = 1000
    lines = lyrics.parse_lrc(LRC)
    y = np.random.default_rng(0).normal(0, 0.001, 50 * sr)
    for l in lines:                                      # local file sings everything 3 s later
        seg = slice(int((l["t"] + 3) * sr), int((l["end"] + 3) * sr))
        y[seg] += np.random.default_rng(1).normal(0, 0.3, seg.stop - seg.start)
    a = lyrics.align(lines, y, sr)
    assert a["trusted"] and abs(a["offset"] - 3.0) < 0.15 and abs(a["lines"][0]["t"] - 13.0) < 0.15


def test_plan_puts_the_drop_on_the_phrase_after_the_hook():
    lines = lyrics.parse_lrc(LRC)
    bpm = 120.0                                          # bar = 2 s
    bounds = [0, 16, 32, 48]
    et = list(np.arange(0, 60, 0.5))
    ec = [0.3 if t < 32 else 0.9 for t in et]            # the song rises at 32
    p = hook_drop.plan(lines, bpm, bounds, et, ec)
    best = p[0]
    assert best["line_t"] == 30.0 and best["drop_at"] == 32.0 and best["energy_rise"] > 0
    assert best["cut_at"] == 30.0 and best["hold_s"] == 2.0            # hold capped at the line start
    learned = {"acapella_drop": {"observations": [{"detail": {"held_s": 8.0, "hook": True}}]}}
    assert hook_drop.plan(lines, bpm, bounds, et, ec, learned=learned)[0]["why"][-1].endswith("(learned from studied sets)")
    assert hook_drop.plan([], bpm, bounds) == [] and hook_drop.plan(lyrics.parse_lrc("[00:01.00] once"), bpm, bounds) == []


def _row(t, db, owner=None):
    return {"t": t, "db": db, "owner": owner, "owners": [owner] if owner else []}


def test_acapella_drop_is_learned_with_its_words():
    song = sl.SongData("A - Song", 0.0, 120.0, "8A", {}, lyrics.parse_lrc(LRC))
    rows = []
    for t in range(0, 40, 4):
        solo = t in (28, 32)
        v = sl.Hit(0, 0.9, float(t) + 2, 1.0)            # the set is 2 s behind the song
        rows += [dict(_row(t, -20, v), stem="vocals"), dict(_row(t, -80 if solo else -20), stem="drums"),
                 dict(_row(t, -80 if solo else -20), stem="bass"), dict(_row(t, -20), stem="other")]
    obs = sl.acapella_drops(rows, [song], "s")
    assert len(obs) == 1 and obs[0].at == 28 and obs[0].detail["drop_at"] == 36
    assert "take me higher" in obs[0].detail["words"] and obs[0].detail["hook"]


def test_rank_learned_acapella_drop_needs_a_hook_drop():
    store = {"acapella_drop": {"kind": "acapella_drop", "what": "x", "stems": False, "live": True, "count": 1,
                               "observations": [{"set_id": "s", "at": 28, "track_a": "A", "detail": {}}]}}
    no = next(x for x in tq.rank(tq.PairFeatures(120, 120), learned=store) if x["name"] == "learned:acapella_drop")
    d = [{"text": "take me higher", "cut_at": 30.0, "drop_at": 32.0}]
    yes = next(x for x in tq.rank(tq.PairFeatures(120, 120, a_hook_drops=d), learned=store) if x["name"] == "learned:acapella_drop")
    assert not no["fits"] and yes["fits"] and any("take me higher" in r for r in yes["reasons"])


def test_pin_from_lrclib_entry(tmp_path):
    get = lambda i: {"artistName": "Fred again..", "trackName": "Sabrina (i am a party)", "duration": 199.1,
                     "syncedLyrics": "[00:00.06] I am a party\n[00:07.44] I know I should want to go"}
    lines = lyrics.set_from_lrclib("Fred again.. - Sabrina (i am a party)", 18606375, tmp_path, get=get)
    assert [l["text"] for l in lines] == ["I am a party", "I know I should want to go"]
    assert lyrics.fetch("Fred again.. - Sabrina (i am a party)", tmp_path, search=lambda a, t: 1 / 0) == lines


def test_plain_lyrics_pin_is_timed_on_the_sung_phrases(tmp_path, monkeypatch):
    get = lambda i: {"artistName": "Fred again..", "trackName": "Halo", "duration": 185.0, "syncedLyrics": None,
                     "plainLyrics": "Boat\n\n作词 : x\nestás perdiendo el foco\nme queda poco"}
    assert lyrics.set_from_lrclib("Fred again.. - Halo", 37601974, tmp_path, get=get) == []
    assert lyrics.load_plain("Fred again.. - Halo", tmp_path) == ["Boat", "estás perdiendo el foco", "me queda poco"]
    sr = 1000
    y = np.zeros(20 * sr)
    for a, b in ((2, 3), (6, 10), (13, 15)):             # three sung phrases: 1 s, 4 s, 2 s
        y[a * sr:b * sr] = np.random.default_rng(a).normal(0, 0.3, (b - a) * sr)
    t = lyrics.time_plain(["Boat", "estás perdiendo el foco", "me queda poco"], y, sr)
    assert [round(l["t"]) for l in t] == [2, 3, 9]              # 8 words over 7 s of singing
    assert all(l["estimated"] for l in t) and t[0]["t"] < t[1]["t"] < t[2]["t"]
    monkeypatch.setattr(lyrics, "LYRICS_DIR", tmp_path)
    monkeypatch.setattr(lyrics, "fetch", lambda title, **kw: [])
    monkeypatch.setattr(lyrics, "load_plain", lambda title: ["Boat", "me queda poco"])
    assert [l["text"] for l in lyrics.for_file("Fred again.. - Halo", y, sr)] == ["Boat", "me queda poco"]


def test_lookup_handles_accents_junk_brackets_plain_and_instrumentals(tmp_path):
    assert lyrics.split_title("Daft Punk - Doin it Right Official Audio ft. Panda Bear") == ("Daft Punk", "Doin it Right")
    assert lyrics.split_title("Fred again.. x Jon Hopkins - Open Eye Signal Feat. LATIN MAFIA under the fabric ID 5")[1] == "Open Eye Signal"
    q = [_cand("Jacob Forever", "Quiéreme", 190)]
    assert lyrics.pick(q, "Jacob Forever", "Quiereme", 191) is q[0]                         # accents
    c = _cand("Fred again.. & LATIN MAFIA", "Cmon (LATIN MAFIA & Fred edit) (feat. Brian Eno)", 256, lrc=None, plainLyrics="words\nmore")
    got = lyrics.pick([c], "Fred again.. Brian Eno LATIN MAFIA", "Cmon LATIN MAFIA Fred edit", 258)
    assert got is c                                                                           # bracketed title, untimed ok
    assert lyrics.pick([c], "Fred again..", "Cmon", 300) is None                              # untimed + wrong length
    far = _cand("Lloyd", "Southside", 318)
    near = _cand("Lloyd", "Southside", 278)
    assert lyrics.pick([far, near], "", "Southside", 280) is near                             # synced, closest length
    assert lyrics.pick([far], "", "Southside", 280) is far                                    # re-aligned later
    dp = [_cand("Daft Punk", "Aerodynamic", 213, lrc=None)]
    assert lyrics.pick(dp, "Daft Punk", "Aerodynamic", 212) == {"instrumental": True}
    lyrics.fetch("Fred again.. & LATIN MAFIA - Cmon (LATIN MAFIA & Fred edit)", tmp_path, search=lambda a, t: [c], duration=258)
    assert lyrics.load_plain("Fred again.. & LATIN MAFIA - Cmon (LATIN MAFIA & Fred edit)", tmp_path) == ["words", "more"]


def test_other_versions_are_not_this_song():
    rmx = _cand("Moderat", "A New Error (Headhunter Remix)", 354, lrc=None, plainLyrics="x")
    mash = _cand("Daft Punk", "Television Rules The Nation x Crescendolls", 211)
    assert lyrics.pick([rmx], "Moderat", "A New Error", 354) is None
    assert lyrics.pick([mash], "Daft Punk", "Crescendolls", 211) is None
    ok = _cand("Nia Archives", "leavemealone (Nia Archives Remix)", 200)
    assert lyrics.pick([ok], "Fred again..", "leavemealone (Nia Archives Remix)", 200) is None   # artist differs
    assert lyrics.pick([ok], "Nia Archives", "leavemealone (Nia Archives Remix)", 200) is ok     # remix asked for


def test_lyrics_need_an_artist_or_a_proven_file(tmp_path, monkeypatch):
    from app.music_brain import set_learner as sl
    monkeypatch.setattr(lyrics, "LYRICS_DIR", tmp_path)
    assert sl.lyric_query("Delilah", "/x/Grayson_Little_-_Delilah.mp3", 0.2) is None
    assert sl.lyric_query("Delilah", "/x/Fred_again.._-_Delilah_pull_me_out_of_this.mp3", 0.8) == \
        "Fred again.. - Delilah pull me out of this"
    assert sl.lyric_query("Fred again.. - Delilah", None, 0.0) == "Fred again.. - Delilah"


def test_drop_never_lands_mid_line():
    lines = lyrics.parse_lrc("[00:10.00] I am a party\n[00:14.00] x\n[00:20.00] I am a party\n[00:24.00] y")
    p = hook_drop.plan(lines, 120.0, [0, 12, 16, 24, 32])            # bar 2 s; 12 is 2 s before the line ends
    assert p and all(x["drop_at"] >= x["line_end"] - hook_drop.EARLY_DROP_BARS * 2.0 for x in p)


def test_render_pulls_the_beat_under_the_line_and_slams_it_back(tmp_path):
    import soundfile as sf
    sr = 8000
    stems = {}
    for i, n in enumerate(("vocals", "drums", "bass", "other")):
        stems[n] = str(tmp_path / f"{n}.wav")
        sf.write(stems[n], np.random.default_rng(i).normal(0, 0.1, (40 * sr, 2)), sr)
    item = {"cut_at": 20.0, "drop_at": 24.0}
    r = hook_drop.render(stems, item, 120.0, tmp_path / "out.wav", pre_s=8, post_s=8)
    y, _ = sf.read(r["path"]); o, _ = sf.read(r["original"])
    d = lambda x, a, b: float(np.sqrt(np.mean(x[int(a * sr):int(b * sr)] ** 2)))
    assert r["cut_in_file"] == 8.0 and r["drop_in_file"] == 12.0
    assert np.allclose(y[:int(7 * sr)], o[:int(7 * sr)]) and np.allclose(y[int(12.1 * sr):], o[int(12.1 * sr):])
    assert d(y, 8.5, 11.5) < 0.7 * d(o, 8.5, 11.5)                         # beat out during the hold
