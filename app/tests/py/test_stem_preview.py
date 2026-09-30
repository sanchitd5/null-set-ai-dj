"""stem-preview: the console's own transition captured headless (app/sim/stem_capture.py) and
rendered on the real audio (app/music_brain/render/graph_render.py).

* the renderer applies a known automation to synthetic stems sample-accurately, its AudioParam
  timelines match the sim's recording Web Audio API (webaudio.js valueAt) exactly, output
  length and peak normalisation;
* a forced Bass Swap / Long Blend captured on a synthetic fixture pair has the expected shape
  (Bass Swap: bass ownership flips on one downbeat, drums keep running; Long Blend: gradual
  crossfade). Needs node + ffmpeg; no network, nothing written outside tmp_path.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.music_brain.render import graph_render as gr

REPO = Path(__file__).resolve().parents[3]
HAVE_TOOLS = bool(shutil.which("node") and shutil.which("ffmpeg"))
needs_tools = pytest.mark.skipif(not HAVE_TOOLS, reason="needs node and ffmpeg")


# ------------------------------------------------------------------ renderer
def _src_node(nid, key, w0, w1, song_start, rate=1.0):
    k0, k1 = int(np.ceil(w0 * 1000)), int(np.floor(w1 * 1000))
    pos = [round(song_start + (k / 1000 - w0) * rate, 6) for k in range(k0, k1 + 1)]
    return {"id": nid, "kind": "source", "type": None, "params": {}, "file": key, "start": w0, "stop": None,
            "positions": {"k0": k0, "pos": pos}}


def _gain(nid, base, ev):
    return {"id": nid, "kind": "gain", "type": None, "params": {"gain": {"base": base, "ev": ev}}}


def test_renderer_applies_known_automation_sample_accurately(tmp_path):
    sr = gr.SR
    n = sr * 4
    rng = np.random.RandomState(1)
    drums = (rng.standard_normal((n, 2)) * 0.2).astype(np.float32)
    bass = (0.3 * np.sin(2 * np.pi * 55 * np.arange(n) / sr)).astype(np.float32)[:, None].repeat(2, 1)
    sf.write(tmp_path / "drums.wav", drums, sr, subtype="FLOAT")
    sf.write(tmp_path / "bass.wav", bass, sr, subtype="FLOAT")
    w0, w1 = 10.0, 12.0                          # audio clock window; song seconds 1..3
    cap = {"window": [w0, w1], "t0": 10.5, "t_end": 11.5, "pos_hz": 1000, "destination": 1, "labels": {},
           "nodes": [{"id": 1, "kind": "destination", "params": {}},
                     _src_node(2, "x:drums", w0, w1, 1.0), _src_node(3, "x:bass", w0, w1, 1.0),
                     _gain(4, 1.0, [{"type": "set", "time": 10.5, "value": 1.0}, {"type": "lin", "time": 11.5, "value": 0.0}]),
                     _gain(5, 0.0, [{"type": "set", "time": 11.0, "value": 1.0}])],
           "edges": [{"from": 2, "to": 4, "on": 0, "off": None}, {"from": 3, "to": 5, "on": 0, "off": None},
                     {"from": 4, "to": 1, "on": 0, "off": None}, {"from": 5, "to": 1, "on": 0, "off": None}]}
    res = gr.render(cap, {"x:drums": tmp_path / "drums.wav", "x:bass": tmp_path / "bass.wav"})
    y = res["audio"]
    assert y.shape == (2, 2 * sr)                                  # output length = window
    assert res["not_rendered"] == []
    t = w0 + np.arange(2 * sr) / sr
    ramp = np.clip((11.5 - t) / 1.0, 0, 1)
    step = (t >= 11.0).astype(np.float64)
    want = drums[sr:3 * sr].T * ramp + bass[sr:3 * sr].T * step
    assert np.max(np.abs(y - want)) < 1e-5                        # sample-accurate
    k = int(round((11.0 - w0) * sr))                               # the bass lands on its exact sample
    assert np.allclose(y[:, k - 1], want[:, k - 1], atol=1e-6) and np.allclose(y[:, k], want[:, k], atol=1e-6)


def test_param_curve_matches_the_sims_recording_api():
    """graph_render.param_curve == webaudio.js FakeAudioParam.valueAt for every ramp type."""
    ev = [{"type": "set", "time": 0.0, "value": 1.0}, {"type": "lin", "time": 1.0, "value": 0.2},
          {"type": "exp", "time": 2.0, "value": 0.8}, {"type": "target", "time": 2.5, "value": 0.0, "tc": 0.3},
          {"type": "set", "time": 3.5, "value": 0.5}, {"type": "curve", "time": 4.0, "dur": 1.0, "curve": [0.5, 1.0, 0.0]},
          {"type": "lin", "time": 6.0, "value": 1.0}]
    times = [round(x, 4) for x in np.linspace(-0.5, 7.0, 151)]
    if not shutil.which("node"):
        pytest.skip("needs node")
    js = (
        "const { FakeAudioContext } = require(process.argv[1]);"
        "const [ev, ts] = JSON.parse(process.argv[2]);"
        "const c = new FakeAudioContext({ now: 0 });"
        "const g = c.createGain(); const p = g.gain; p._ev = ev.map((e) => Object.assign({}, e)); p._base = 1;"
        "process.stdout.write(JSON.stringify(ts.map((t) => p.valueAt(t))));"
    )
    r = subprocess.run(["node", "-e", js, str(REPO / "app/sim/js/webaudio.js"), json.dumps([ev, times])],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    want = np.array(json.loads(r.stdout))
    got = gr.param_curve({"base": 1.0, "ev": ev}, np.array(times))
    assert np.allclose(got, want, atol=1e-9), np.c_[times, got, want][np.abs(got - want) > 1e-9][:5]


def _impulse_cap(d_s: float, g: float, w: float = 1.2):
    """impulse -> dest, and impulse -> delay(d) -> dest with delay -> gain(g) -> delay (an echo)."""
    sr = gr.SR
    imp = [0.0] * sr
    imp[0] = 1.0
    src = _src_node(2, None, 0.0, w, 0.0)
    src["buf"] = 99
    return {"window": [0.0, w], "t0": 0.0, "t_end": w, "pos_hz": 1000, "destination": 1, "labels": {},
            "buffers": {"99": {"sr": sr, "channels": [imp, imp]}},
            "nodes": [{"id": 1, "kind": "destination", "params": {}}, src,
                      {"id": 3, "kind": "delay", "params": {"delayTime": {"base": d_s, "ev": []}}},
                      _gain(4, g, [])],
            "edges": [{"from": 2, "to": 1, "on": 0, "off": None}, {"from": 2, "to": 3, "on": 0, "off": None},
                      {"from": 3, "to": 4, "on": 0, "off": None}, {"from": 4, "to": 3, "on": 0, "off": None},
                      {"from": 3, "to": 1, "on": 0, "off": None}]}


def test_feedback_delay_gives_the_geometric_echo_tail():
    """An impulse through a delay d with feedback g: repeats at k*d with amplitude g^(k-1),
    rendered through chunk boundaries (a small chunk forces many)."""
    d, g = 0.1, 0.6
    res = gr.render(_impulse_cap(d, g), {}, chunk=128 * 100)
    y = res["audio"][0]
    assert res["not_rendered"] == []
    D = int(round(d * gr.SR))
    want = np.zeros_like(y)
    want[0] = 1.0
    for k in range(1, len(y) // D + 1):
        if k * D < len(y):
            want[k * D] = g ** (k - 1)
    assert np.max(np.abs(y - want)) < 1e-4, np.flatnonzero(np.abs(y - want) > 1e-4)[:5]


def test_delay_in_a_cycle_is_at_least_one_render_quantum():
    res = gr.render(_impulse_cap(0.0005, 0.5, w=0.05), {})         # 22 samples asked, 128 in a cycle
    y = res["audio"][0]
    nz = np.flatnonzero(np.abs(y) > 1e-6)
    assert list(nz[:3]) == [0, 128, 256] and np.isclose(y[256], 0.5, atol=1e-4)


def test_param_eval_in_blocks_equals_one_pass():
    ev = [{"type": "set", "time": 0.0, "value": 1.0}, {"type": "lin", "time": 1.0, "value": 0.2},
          {"type": "exp", "time": 2.0, "value": 0.8}, {"type": "target", "time": 2.5, "value": 0.0, "tc": 0.3},
          {"type": "curve", "time": 4.0, "dur": 1.0, "curve": [0.5, 1.0, 0.0]}, {"type": "lin", "time": 6.0, "value": 1.0}]
    t = np.linspace(-0.5, 7.0, 7501)
    whole = gr.param_curve({"base": 1.0, "ev": ev}, t)
    pe = gr.ParamEval({"base": 1.0, "ev": ev})
    parts = np.concatenate([pe(t[i:i + 333]) for i in range(0, len(t), 333)])
    assert np.allclose(whole, parts, atol=1e-12)


def test_normalise_gives_both_files_the_same_peak():
    a = np.random.RandomState(0).standard_normal((2, 1000)) * 0.1
    b = a * 7.0
    na, nb = gr.normalise(a, -1.0), gr.normalise(b, -1.0)
    assert np.isclose(np.abs(na).max(), 10 ** (-1 / 20)) and np.isclose(np.abs(nb).max(), 10 ** (-1 / 20))
    assert np.allclose(na, nb)


def test_lowshelf_cut_matches_web_audio_response():
    """A -26 dB low shelf at 120 Hz (the console's low EQ kill): ~-26 dB at 30 Hz, ~0 dB at 5 kHz."""
    sr = gr.SR
    t = np.arange(sr) / sr
    for f, lo, hi in ((30.0, -27.0, -24.0), (5000.0, -0.3, 0.3)):
        x = np.sin(2 * np.pi * f * t)[None, :].repeat(2, 0)
        y = gr.run_biquad(x, "lowshelf", np.full(sr, 120.0), np.full(sr, 1.0), np.full(sr, -26.0))
        db = 20 * np.log10(np.sqrt(np.mean(y[0, sr // 2:] ** 2)) / np.sqrt(0.5))
        assert lo < db < hi, (f, db)


# ------------------------------------------------------------------ capture on a fixture pair
BPM = 128.0
BEAT = 60.0 / BPM
BAR = 4 * BEAT
DUR = 96.0
SR_FIX = 22050


def _song(dirp: Path, seed: int, bass_hz: float) -> dict:
    """A 128 BPM song with 4 stems (kick + hats, sub bass, pad, quiet voice) and its analysis."""
    n = int(DUR * SR_FIX)
    t = np.arange(n) / SR_FIX
    rng = np.random.RandomState(seed)
    ph = np.mod(t, BEAT)
    kick = np.sin(2 * np.pi * 60 * ph) * np.exp(-ph / 0.08)
    hat_ph = np.mod(t + BEAT / 2, BEAT)
    hats = rng.standard_normal(n) * np.exp(-hat_ph / 0.02) * 0.3
    stems = {"drums": 0.5 * kick + hats, "bass": 0.35 * np.sin(2 * np.pi * bass_hz * t),
             "other": 0.12 * np.sin(2 * np.pi * 440 * t) + 0.05 * rng.standard_normal(n),
             "vocals": 0.02 * np.sin(2 * np.pi * 660 * t)}
    dirp.mkdir(parents=True, exist_ok=True)
    mix = sum(stems.values())
    song = dirp / "mix.wav"
    sf.write(song, np.stack([mix, mix], 1).astype(np.float32) * 0.8, SR_FIX, subtype="PCM_16")
    digest = hashlib.sha256(song.read_bytes()).hexdigest()
    return {"path": song, "digest": digest, "stems": {k: v * 0.8 for k, v in stems.items()}}


def _fixture_cache(root: Path) -> tuple:
    cache = root / "src_cache"
    out = []
    for i, (seed, hz) in enumerate(((1, 55.0), (2, 49.0))):
        s = _song(root / f"song{i}", seed, hz)
        d = cache / "stems" / f"{s['digest']}_htdemucs_ft"
        d.mkdir(parents=True)
        paths = {}
        for k, v in s["stems"].items():
            sf.write(d / f"{k}.wav", np.stack([v, v], 1).astype(np.float32), SR_FIX, subtype="PCM_16")
            paths[k] = str(d / f"{k}.wav")
        (d / "manifest.json").write_text(json.dumps({"version": 2, "format": "wav", "stems": paths}))
        beats = list(np.arange(0, DUR - 0.5, BEAT).round(4))
        an = {"path": str(s["path"]), "duration": DUR, "bpm": BPM, "beat_times": beats, "downbeat_times": beats[::4],
              "phrase_boundaries_8bar": beats[::32], "phrase_boundaries_16bar": beats[::64],
              "key": {"camelot": "8A", "confidence": 0.9, "is_major": False, "key_name": "A Minor"},
              "energy_curve": [0.7] * int(DUR), "energy_times": [float(x) for x in range(int(DUR))],
              "sections": [], "vocal_active_regions": []}
        (cache / "analysis").mkdir(parents=True, exist_ok=True)
        (cache / "analysis" / f"{s['digest']}.v5.json").write_text(json.dumps(an))
        out.append(s["path"])
    ida = hashlib.sha256(out[0].read_bytes()).hexdigest()[:16]
    (cache / "fame.json").write_text(json.dumps({ida: {"views": 5, "famous": False, "name": "a"}, "ffff": {"views": 1}}))
    (cache / "liked.json").write_text(json.dumps({"schema": 1, "liked": {}}))
    return cache, out[0], out[1]


def _curves(cap: dict, deck: str, t: np.ndarray) -> dict:
    by = {v: int(k) for k, v in cap["labels"].items()}
    nodes = {n["id"]: n for n in cap["nodes"]}
    def c(label, param="gain", dflt=1.0):
        nid = by.get(f"{deck}.{label}")
        p = nid is not None and nodes[nid]["params"].get(param)
        return gr.param_curve(p, t) if p else np.full(len(t), dflt)
    return {"xf": c("crossfaderGain") * c("volumeGain"), "mix": c("mixGain"), "low": c("lowFilter"),
            **{s: c(f"stemGain.{s}", dflt=0.0) for s in ("drums", "bass", "vocals", "other")}}


def _presence(cv: dict, stem: str) -> np.ndarray:
    """How much of a deck's `stem` reaches its channel: (full mix or that stem) x fader."""
    return np.maximum(cv["mix"], cv[stem]) * cv["xf"]


@pytest.fixture(scope="module")
def captures(tmp_path_factory):
    if not HAVE_TOOLS:
        pytest.skip("needs node and ffmpeg")
    from app.sim.stem_capture import capture
    root = tmp_path_factory.mktemp("stem_capture")
    cache, a, b = _fixture_cache(root)
    a_time = 32 * BEAT * 5                        # a phrase line (75 s)
    return {r: capture(a, b, r, a_time=a_time, b_time=0.0, pre=6.0, post=4.0, src_cache=cache)
            for r in ("Bass Swap", "Long Blend")}


@needs_tools
def test_bass_swap_capture_flips_bass_on_one_downbeat_drums_keep_running(captures):
    cap = captures["Bass Swap"]
    assert cap["ran"] == "Bass Swap" and cap["end_marked"]
    t = np.arange(cap["t0"], cap["t_end"], 0.005)
    A, B = _curves(cap, "a", t), _curves(cap, "b", t)
    # bass owner: the low band open (EQ above -6 dB) and the bass reaching the channel
    own_a = (A["low"] > -6) & (_presence(A, "bass") > 0.3)
    own_b = (B["low"] > -6) & (_presence(B, "bass") > 0.3)
    assert own_a[0] and not own_b[0] and own_b[-1] and not own_a[-1]
    flips_a, flips_b = np.flatnonzero(np.diff(own_a.astype(int))), np.flatnonzero(np.diff(own_b.astype(int)))
    assert len(flips_a) == 1 and len(flips_b) == 1                 # one hand-off, no ping-pong
    ta, tb = t[flips_a[0]], t[flips_b[0]]
    assert abs(ta - tb) <= BEAT                                     # both sides move together
    both = own_a & own_b
    assert both.sum() * 0.005 <= BEAT                               # never two bass owners for more than a beat
    # the hand-off sits on one downbeat D (B plays from 0: its downbeats are t0 + k bars):
    # A lets go of the bass by D, B takes it from D, each within a beat of it
    D = cap["t0"] + BAR * round((0.5 * (ta + tb) - cap["t0"]) / BAR)
    assert ta <= D + 0.03 and tb >= D - 0.03 and abs(ta - D) <= BEAT and abs(tb - D) <= BEAT, (ta - D, tb - D)
    drums = np.maximum(_presence(A, "drums"), _presence(B, "drums"))
    per_beat = [drums[(t >= s) & (t < s + BEAT)].max() for s in np.arange(cap["t0"], cap["t_end"] - BEAT, BEAT)]
    assert min(per_beat) > 0.5, per_beat                            # every beat of the swap has a kick up


@needs_tools
def test_long_blend_capture_is_a_gradual_crossfade(captures):
    cap = captures["Long Blend"]
    assert cap["ran"] == "Long Blend" and cap["end_marked"]
    t = np.arange(cap["t0"], cap["t_end"] + 1e-9, 0.25)
    A, B = _curves(cap, "a", t), _curves(cap, "b", t)
    fb = np.maximum(B["mix"], np.maximum.reduce([B[s] for s in ("drums", "bass", "vocals", "other")])) * B["xf"]
    fa = np.maximum(A["mix"], np.maximum.reduce([A[s] for s in ("drums", "bass", "vocals", "other")])) * A["xf"]
    assert fb[0] < 0.1 and fb[-1] > 0.9 and fa[0] > 0.9 and fa[-1] < 0.1
    assert cap["t_end"] - cap["t0"] >= 8 * BAR - 0.5                # at least 8 bars long
    assert np.max(np.abs(np.diff(fb))) < 0.25 and np.max(np.abs(np.diff(fa))) < 0.25   # no step, a ramp
    rise = t[np.argmax(fb > 0.9)] - t[np.argmax(fb > 0.1)]
    assert rise >= BAR, rise                                        # B comes up over a bar or more
    mid = (t > cap["t0"] + 0.3 * (cap["t_end"] - cap["t0"])) & (t < cap["t0"] + 0.7 * (cap["t_end"] - cap["t0"]))
    assert (np.minimum(fa, fb)[mid] > 0.2).all()                    # both play together through the middle


@needs_tools
def test_capture_renders_to_window_length(captures, tmp_path):
    cap = captures["Bass Swap"]
    files = {}
    for side in ("a", "b"):
        s = cap["songs"][side]
        files[f"{s['id']}:mix"] = Path(s["path"])
        for n in ("drums", "bass", "vocals", "other"):
            files[f"{s['id']}:{n}"] = Path(s["stem_dir"]) / f"{n}.wav"
    res = gr.render(cap, files)
    w0, w1 = cap["window"]
    assert res["audio"].shape == (2, int(round((w1 - w0) * gr.SR)))
    assert np.isclose(cap["t0"] - w0, 6.0) and np.isclose(w1 - cap["t_end"], 4.0)
    y = gr.normalise(res["audio"], -1.0)
    assert np.isclose(np.abs(y).max(), 10 ** (-1 / 20))
    one_s = gr.SR
    assert np.sqrt(np.mean(res["audio"][:, :one_s] ** 2)) > 1e-3   # A audible before the move
    assert np.sqrt(np.mean(res["audio"][:, -one_s:] ** 2)) > 1e-3  # B audible after it


def _static_digest() -> str:
    h = hashlib.sha256()
    for p in sorted((REPO / "app/ui/static").rglob("*")):
        if p.is_file():
            h.update(p.name.encode() + p.read_bytes())
    return h.hexdigest()


@needs_tools
def test_preview_overrides_touch_only_the_captures_copy(tmp_path):
    """allow_stem_path / xf are applied to the capture's throwaway copy of the console, listed in
    sim_overrides, and never written to app/ui/static. xf=8 halves the booked bar counts."""
    from app.sim.stem_capture import capture
    cache, a, b = _fixture_cache(tmp_path)
    before = _static_digest()
    base = capture(a, b, "Bass Swap", a_time=32 * BEAT * 5, b_time=0.0, pre=2.0, post=1.0, src_cache=cache)
    half = capture(a, b, "Bass Swap", a_time=32 * BEAT * 5, b_time=0.0, pre=2.0, post=1.0, src_cache=cache,
                   allow_stem_path=True, xf=8)
    assert _static_digest() == before
    assert base["sim_overrides"] == []
    assert {o["override"] for o in half["sim_overrides"]} == {"allowStemPath", "xf"}
    L0, L1 = base["t_end"] - base["t0"], half["t_end"] - half["t0"]
    assert abs(L1 - L0 / 2) < 0.1, (L0, L1)
    with pytest.raises(ValueError):
        capture(a, b, "Bass Swap", a_time=75.0, b_time=0.0, src_cache=cache, xf=0)


@needs_tools
def test_capture_serves_what_the_live_server_reads_from_its_cache(captures):
    """B's vocal entry is asked like the running set's staging does (a mashup reads it), and the
    console-wide cache reads (liked, macros, learned moves, recipes, cached fame) are answered."""
    cap = captures["Bass Swap"]
    assert cap["vocal_entry_b"] is not None and "entry" in cap["vocal_entry_b"]
    served_never = {"GET /api/liked", "GET /api/macros", "GET /api/learned/moves", "GET /api/recipes"}
    assert not served_never & set(cap["unserved"]), cap["unserved"]
    ida, idb = cap["songs"]["a"]["id"], cap["songs"]["b"]["id"]
    assert f"GET /api/tracks/{ida}/fame" not in cap["unserved"]          # cached: answered
    assert f"GET /api/tracks/{idb}/fame" in cap["unserved"]              # not cached: a miss would ask YouTube


@needs_tools
def test_full_capture_spans_a_from_zero_to_bs_end(tmp_path):
    from app.sim.stem_capture import capture
    cache, a, b = _fixture_cache(tmp_path)
    # 45 s: past the default 30 s pre-roll, so A starting at a_time - pre (not 0:00) would fail
    a_time = 32 * BEAT * 3
    cap = capture(a, b, "Bass Swap", a_time=a_time, b_time=0.0, src_cache=cache, full=True)
    w0, w1 = cap["window"]
    assert abs((cap["t0"] - w0) - a_time) < 0.01                        # A from its 0:00
    b_at_end = cap["t_end"] - cap["t0"]                                 # B entered at 0, rate 1
    assert abs((w1 - cap["t_end"]) - (DUR - b_at_end)) < 0.1            # B to its own end
    nd = {n["id"]: n for n in cap["nodes"]}
    mixes = [n for n in nd.values() if n["kind"] == "source" and (n.get("file") or "").endswith(":mix")]
    a_mix = min(mixes, key=lambda n: n["start"])                        # A's mix is the first source to start
    assert abs(a_mix["start"] - w0) < 0.05                              # it sounds from the window's first sample
    assert a_mix["offset"] < 0.05                                       # and from A's own 0:00, not a_time - pre


def test_bridge_parser_has_stem_preview():
    from app.music_brain.agent_bridge import _build_parser
    a = _build_parser().parse_args(["stem-preview", "a.mp3", "b.mp3", "--recipe", "Bass Swap", "--a-time", "195",
                                    "--b-time", "3.3", "--out", "x.wav"])
    assert (a.command, a.recipe, a.a_time, a.b_time, a.pre, a.post) == ("stem-preview", "Bass Swap", 195.0, 3.3, 30.0, 30.0)
    assert a.allow_stem_path is False and a.xf is None
    a = _build_parser().parse_args(["stem-preview", "a", "b", "--recipe", "Long Blend", "--a-time", "1", "--b-time", "0",
                                    "--out", "x.wav", "--allow-stem-path", "--xf", "8"])
    assert a.allow_stem_path is True and a.xf == 8.0


def test_capture_refuses_a_song_without_cached_stems(tmp_path):
    from app.sim.stem_capture import seed_cache
    song = tmp_path / "s.wav"
    sf.write(song, np.zeros((100, 2), np.float32), 22050)
    with pytest.raises(ValueError, match="no cached 4-stem set"):
        seed_cache(tmp_path / "dst", tmp_path / "src", [song])
