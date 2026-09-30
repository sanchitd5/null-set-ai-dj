"""Fixer D (batch 2): cumulative energy fall, the prompt's energy window, the real
Fred again.. trace (session 2026-09-29_005748), and cross-session repeats."""
from app.music_brain.analysis import energy
from app.ui.services import autopilot_service as svc
from app.ui.services.set_memory import MAX_SONGS, PROMPT_LIMIT, SetMemory


# --- (1) cumulative fall -------------------------------------------------------------
def test_slow_drain_9_7_4_2_is_stopped():
    step = lambda cur, nxt, played, **o: energy.next_ok(cur, nxt, songs=8, recent=played, **o)
    assert step(9, 7, [9])["ok"]
    assert not step(7, 6, [9, 7])["ok"]                    # 3 under the peak of 9
    assert not step(7, 5, [9, 7])["ok"]
    assert "drains the set" in step(7, 5, [9, 7])["why"]
    assert step(7, 8, [9, 7])["ok"] and step(7, 7, [9, 7])["ok"]   # rises and holds are never blocked


def test_drain_rule_exemptions():
    kw = dict(songs=8, recent=[9, 7])
    assert energy.next_ok(7, 5, reset=True, **kw)["ok"]                  # a deliberate dip was asked for
    assert energy.next_ok(4, 2, songs=8, recent=[4, 4])["ok"]            # a low set has no high peak to drain from
    assert energy.next_ok(7, 5, set_pos=0.9, recent=[9, 7])["ok"]        # cool-down at the end
    assert not energy.next_ok(7, 5, force=True, **kw)["ok"]              # the last-round force never widens falls
    assert energy.next_ok(6, 4, songs=8, recent=[9, 6, 6, 6, 6, 6, 6])["ok"]   # the peak left the 6-song window
    assert energy.next_ok(7, 5, songs=8)["ok"]                           # no history: the old rule only


def test_allowed_window_follows_the_peak():
    assert energy.allowed_window(7, songs=8) == (5, 9)
    assert energy.allowed_window(7, songs=8, recent=[9, 7]) == (7, 9)
    assert energy.allowed_window(7, songs=8, recent=[9, 7], reset=True) == (5, 9)


# --- (2) the prompt tells the window ----------------------------------------------------
def test_energy_line_states_the_rule_window():
    assert "MUST be 7-9" in svc.energy_line(0.5, 7, window=(7, 9))
    assert "MEASURED ENERGY: 7/10" in svc.energy_line(0.5, 7, window=(7, 9))
    assert "MUST be 5-9" in svc.energy_line(0.5, 7)


def test_profile_clash_uses_the_window_floor():
    cur = {"energy": 7, "_measured": True, "_lo": 7}
    assert svc._profile_clash(cur, {"energy": 5}) == "energy below the set floor 7"
    assert svc._profile_clash(cur, {"energy": 8}) is None


# --- (3) the real trace: 7 Fred again.. songs in a row ----------------------------------------
TRACE = [
    "Daft Punk - Get Lucky (Official Audio) ft. Pharrell Williams, Nile Rodgers",
    "fred again.. - chanel x a new error x i am a party",
    "Fred again.. x I. Jordan - Admit (U Don't Want 2) (10 February 2022)",
    "Fred again.. & Baby Keem - leavemealone",
    "Atlantic Records - CA7RIEL, Paco Amoroso, PinkPantheress, Fred again..–Sexy Magic (From Grand Theft Auto VI: The Album)",
    "Fred again.. - Delilah (pull me out of this)",
    "Fred again.. - Jungle",
    "Fred again.. feat. The Blessed Madonna - Marea (We’ve Lost Dancing) (Official Audio)",
    "Lane 8, Kasbo & BJOERN - World Is Mine (Official Music Video)",
    "Fred again.., Brian Eno - Cmon (LATIN MAFIA, KatzPascale, Fred edit)",
]
ALT = {"artist": "Bicep", "title": "Glue"}


def _pick(name):
    a, t = name.split(" - ", 1)
    return {"artist": a, "title": t}


def test_trace_fred_run_is_blocked_by_artist_spacing():
    blocked = []
    for i in range(2, len(TRACE)):
        got = svc.artist_spacing([_pick(TRACE[i]), ALT], TRACE[:i])
        if "fred" in TRACE[i].lower():
            blocked.append(i)
            assert got == [ALT], (i, TRACE[i])       # a Fred pick never wins over an alternative
    assert blocked == [2, 3, 4, 5, 6, 7, 9]           # incl. the credit that runs into the title (i=4)


def test_trace_only_fred_offered_is_not_kept():
    for i in (2, 3, 5, 6, 7, 9):
        assert svc.artist_spacing([_pick(TRACE[i])], TRACE[:i]) == [], TRACE[i]   # [] -> suggest re-asks


# --- (4) repeat songs across sessions ---------------------------------------------------------
def test_prompt_shows_40_but_the_filter_gets_every_remembered_song(tmp_path, monkeypatch):
    import itertools, time, types
    clock = itertools.count(time.time() - 1000, 1.0)
    monkeypatch.setattr("app.ui.services.set_memory.time", types.SimpleNamespace(time=lambda: next(clock)))
    mem = SetMemory(tmp_path / "m.json")
    for i in range(80):                                # song0 is the oldest
        mem.record([f"Artist{i} - Song{i}"], "old-set")
    every = mem.earlier_sets(["Now - Playing"], set_id="new-set", limit=MAX_SONGS)
    assert len(every) == 80 and len(mem.earlier_sets([], set_id="new-set")) == PROMPT_LIMIT
    bare = lambda xs: {svc._bare_title(str(x).split(" - ", 1)[-1]) for x in xs}
    assert "song0" in bare(every)                     # oldest of 80: beyond the prompt's 40, still filtered
    assert "song0" not in bare(every[:PROMPT_LIMIT])
