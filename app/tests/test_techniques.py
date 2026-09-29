import numpy as np

from app.music_brain import techniques as tq


def _smap(rows, phrase=15.6):
    """rows: list of (drums, bass, vocals, other) dB per phrase."""
    return [{"start": i * phrase, "end": (i + 1) * phrase, "drums": d, "bass": b, "vocals": v, "other": o}
            for i, (d, b, v, o) in enumerate(rows)]


# Aerodynamic's measured phrase map (aero_map.py, 2026-09-27)
AERO = _smap([(-40, -66, -54, -25), (-24, -29, -24, -22), (-17, -21, -44, -20), (-17, -21, -43, -20),
              (-74, -82, -75, -15), (-43, -57, -50, -15), (-26, -31, -29, -15), (-17, -21, -37, -17),
              (-17, -21, -43, -16), (-78, -90, -90, -22)])


def _usb002(**kw):
    base = dict(bpm_a=122.88, bpm_b=140.02, key_a="10A", key_b="5B", stems_a=True, stems_b=True,
                vocal_b_entry=0.8, b_rap=True, a_grooves=tq.full_groove_runs(AERO),
                a_breakdowns=tq.breakdowns(AERO), exit_window=(40.0, 90.0), keylock=True)
    base.update(kw)
    return tq.PairFeatures(**base)


def _by(name, ranked):
    return next(x for x in ranked if x["name"] == name)


def test_stem_map_finds_the_groove_and_the_breakdown():
    assert tq.breakdowns(AERO)[0] == (4 * 15.6, 5 * 15.6)           # 1:02.4, the guitar solo intro
    runs = tq.full_groove_runs(AERO)
    assert runs[0][0] == 15.6 and runs[0][1] >= 3 * 15.6            # 0:15.6-0:46.8+ loopable


def test_usb002_pair_fits_riff_over_rap_with_keylock():
    # the original 13.9 % gap is past the 8 % key-lock cap now; a 5.8 % one still fits
    assert not _by("riff_over_rap", tq.rank(_usb002()))["fits"]
    r = _by("riff_over_rap", tq.rank(_usb002(bpm_b=130.0)))
    assert r["fits"], r["reasons"]
    assert any("rap" in x for x in r["reasons"])                     # the key clash is excused, and says why


def test_without_keylock_riff_over_rap_says_why_not():
    r = _by("riff_over_rap", tq.rank(_usb002(keylock=False)))
    assert not r["fits"] and any("detune" in x for x in r["reasons"])


def test_sung_vocal_with_clashing_keys_is_not_riff_over_rap():
    r = _by("riff_over_rap", tq.rank(_usb002(b_rap=False)))
    assert not r["fits"] and any("sung" in x for x in r["reasons"])


def test_same_tempo_pair_skips_riff_over_rap_but_can_hand_off():
    f = _usb002(bpm_b=123.5, key_b="10A", vocal_a_exit=0.6)
    ranked = tq.rank(f)
    assert not _by("riff_over_rap", ranked)["fits"]                  # nothing to stretch
    assert _by("vocal_handoff", ranked)["fits"]
    assert ranked[0]["fits"]                                         # fitting ones first


def test_eq_blend_always_available_and_strip_needs_fame():
    ranked = tq.rank(_usb002(famous_a=False, vocal_a_exit=0.9))
    assert _by("eq_blend", ranked)["fits"]
    assert not _by("strip_rebuild", ranked)["fits"]
    assert _by("strip_rebuild", tq.rank(_usb002(famous_a=True, vocal_a_exit=0.9)))["fits"]


def test_camelot_matches_dj_mind():
    assert tq.camelot_score("8A", "8A") == 1.0 and tq.camelot_score("8A", "9A") == 0.9
    assert tq.camelot_score("8A", "8B") == 0.85 and tq.camelot_score("10A", "5B") == 0.0


def test_stem_map_from_audio():
    sr = 1000
    y = {"drums": np.ones(sr * 4) * 0.1, "bass": np.zeros(sr * 4), "vocals": np.zeros(sr * 4), "other": np.ones(sr * 4) * 0.1}
    m = tq.stem_map(y, sr, [0, 2, 4])
    assert len(m) == 2 and m[0]["drums"] > -25 and m[0]["bass"] < -100


def test_repeated_opening_hook_is_skipped():
    sr, bpm = 22050, 140.0
    bar = 240 / bpm
    rng = np.random.default_rng(3)
    y = np.zeros(int(40 * bar * sr), np.float32)
    click = (np.hanning(400) * rng.standard_normal(400)).astype(np.float32)
    hook = sorted(rng.uniform(0, bar, 7))                 # the same 7 syllables every bar
    for b in range(40):
        times = hook if b < 12 else sorted(rng.uniform(0, bar, 7))   # then the verse varies
        for x in times:
            i = int((b * bar + x) * sr)
            y[i:i + 400] += click
    downbeats = [b * bar for b in range(41)]
    phrases = [p * 8 * bar for p in range(6)]
    rep = tq.repetitive_bars(y, sr, downbeats)
    assert all(rep[1:12]) and sum(rep[14:]) <= 2
    entry = tq.skip_repetitive_intro(rep, downbeats, phrases, rap_at=0.0)
    assert entry == phrases[2]                             # next phrase line after the hook (bar 16)


def test_no_hook_keeps_the_rap_entry():
    assert tq.skip_repetitive_intro([False] * 30, [i * 1.7 for i in range(31)], [0, 13.6, 27.2], 13.6) == 13.6


def test_only_the_opening_hook_is_skipped_not_later_rhymes():
    rep = [False] * 40
    for i in range(9, 15):
        rep[i] = True          # opening hook, bars 9-14
    for i in range(18, 21):
        rep[i] = True          # a rhyme pattern later in the verse
    db = [i * 1.7 for i in range(41)]
    assert tq.skip_repetitive_intro(rep, db, [0, 13.6, 27.2, 40.8], 0.0) == 27.2   # after bar 14, not after 20


def test_learned_pick_maps_live_moves_to_console_recipes():
    store = {
        "bass_swap": {"kind": "bass_swap", "what": "x", "stems": False, "live": True, "count": 2, "tempo_gap_max": 0.05,
                      "key_score_min": 0.8, "observations": [{"set_id": "s", "at": 60, "track_a": "A", "track_b": "B", "tempo_gap": 0.02, "key_score": 0.9, "detail": {}}] * 2},
        "stem_intro": {"kind": "stem_intro", "what": "x", "stems": True, "live": True, "count": 5, "tempo_gap_max": 0.05,
                       "key_score_min": 0.8, "observations": [{"set_id": "s", "at": 90, "track_a": "C", "track_b": "D", "tempo_gap": 0.02, "key_score": 0.9, "detail": {}}] * 5},
        "vocal_chop": {"kind": "vocal_chop", "what": "x", "stems": True, "live": False, "count": 9,
                       "observations": [{"set_id": "s", "at": 9, "track_a": "E", "detail": {}}] * 9},
    }
    f = tq.PairFeatures(124, 126, "8A", "9A", stems_a=True, stems_b=True)
    p = tq.learned_pick(tq.rank(f, learned=store), store)
    assert p["kind"] == "stem_intro" and p["recipe"] == "Long Blend" and p["seen"] == 5   # most seen, playable
    no_stems = tq.learned_pick(tq.rank(tq.PairFeatures(124, 126, "8A", "9A"), learned=store), store)
    assert no_stems["kind"] == "bass_swap"                                                 # stem_intro needs stems
    assert tq.learned_pick(tq.rank(tq.PairFeatures(124, 174, "8A", "9A"), learned=store), store) is None  # out of range


def test_learned_move_fits_pairs_like_the_ones_it_was_seen_on():
    obs = [{"set_id": "s", "at": 1, "track_a": "A", "track_b": "B", "tempo_gap": g, "key_score": k, "detail": {}}
           for g, k in ((0.0, 1.0), (0.33, 0.0))]                     # two very different sightings
    store = {"bass_swap": {"kind": "bass_swap", "what": "x", "stems": False, "live": True, "count": 2,
                           "tempo_gap_max": 0.33, "key_score_min": 0.0, "observations": obs}}
    fit = lambda f: next(x for x in tq.rank(f, learned=store) if x["name"] == "learned:bass_swap")["fits"]
    assert fit(tq.PairFeatures(124, 124, "8A", "8A"))                  # like sighting 1
    assert not fit(tq.PairFeatures(124, 140, "8A", "8A"))              # 13 %: like neither (a range would say yes)
    assert fit(tq.PairFeatures(87, 174, "8A", "8A"))                   # half time folds to 0 %


def test_learned_pick_skips_tonal_blends_on_clashing_keys():
    # stem_intro was "seen" on clashing pairs (key_score 0.0) in studied sets: must not license one here
    obs = [{"set_id": "s", "at": 1, "track_a": "A", "track_b": "B", "tempo_gap": 0.02, "key_score": 0.0, "detail": {}}] * 4
    store = {k: {"kind": k, "what": "x", "stems": False, "live": True, "count": 4, "tempo_gap_max": 0.05,
                 "key_score_min": 0.0, "observations": obs} for k in ("stem_intro", "bass_swap")}
    f = tq.PairFeatures(124, 130, "6A", "1A", stems_a=True, stems_b=True)
    ranked = tq.rank(f, learned=store)
    assert tq.learned_pick(ranked, store) is not None                   # no key info: legacy behaviour
    assert tq.learned_pick(ranked, store, key_score=tq.camelot_score("6A", "1A")) is None
    assert tq.learned_pick(ranked, store, key_score=0.9) is not None


def test_learned_hard_cut_plays_as_a_bass_swap_never_a_cut():
    # owner rule: the console never plays a Hard Cut / Quick Cut, it runs a cut as a bass swap
    obs = [{"set_id": "s", "at": 1, "track_a": "A", "track_b": "B", "tempo_gap": 0.02, "key_score": 0.9, "detail": {}}] * 3
    store = {"hard_cut": {"kind": "hard_cut", "what": "x", "stems": False, "live": True, "count": 3,
                          "tempo_gap_max": 0.05, "key_score_min": 0.8, "observations": obs}}
    ranked = tq.rank(tq.PairFeatures(124, 126, "8A", "9A"), learned=store)
    p = tq.learned_pick(ranked, store, key_score=0.9)
    assert p["kind"] == "hard_cut" and p["recipe"] == "Bass Swap"
    assert tq.learned_pick(ranked, store, key_score=0.0) is None      # tonal: no swap across a key clash


def test_keylock_stretch_capped_at_8_percent():
    assert tq.MAX_KEYLOCK_STRETCH == 0.08
