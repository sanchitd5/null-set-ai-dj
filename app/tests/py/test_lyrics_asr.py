"""Lyrics by speech-to-text on the vocal stem: guards, precedence, plain-text placement, cache.
The Whisper model is always mocked here except in the one slow test."""
import json
import os

import numpy as np
import pytest

from app.music_brain.analysis import hook_drop, lyrics, lyrics_asr as asr

SR = 16000


def _vocal(spans, dur=40.0, sr=SR):
    y = np.zeros(int(dur * sr), np.float32)
    for a, b in spans:
        y[int(a * sr):int(b * sr)] = np.random.default_rng(int(a * 10)).normal(0, 0.3, int((b - a) * sr))
    return y


def _w(word, t, end, p=0.9):
    return {"word": " " + word, "start": t, "end": end, "prob": p}


def _seg(t, words, text=None, **kw):
    ws = [_w(w, t + i * 0.4, t + i * 0.4 + 0.35) for i, w in enumerate(words.split())]
    base = {"start": t, "end": t + len(ws) * 0.4, "text": text or " " + words, "avg_logprob": -0.3,
            "no_speech_prob": 0.05, "compression_ratio": 1.4, "words": ws}
    return base | kw


class Engine:
    def __init__(self, segs, lang="en"):
        self.segs, self.lang, self.clips = segs, lang, None

    def language(self, audio):
        return self.lang, 0.97

    def run(self, audio, clips, language):
        self.clips = clips
        return list(self.segs)


SPANS = [(2, 6), (9, 13), (16, 20), (23, 27)]
SEGS = [_seg(2.2, "we light the city up"), _seg(9.2, "hold me through the night"),
        _seg(16.2, "we light the city up"), _seg(23.2, "never let it go now")]
LRC = ("[00:02.00] we light the city up\n[00:06.00]\n[00:09.00] hold me through the night\n[00:13.00]\n"
       "[00:16.00] we light the city up\n[00:20.00]\n[00:23.00] never let it go now\n[00:27.00]")


def test_no_backend_is_a_graceful_empty(monkeypatch):
    monkeypatch.setenv("LYRICS_ASR", "off")
    assert asr.pick_backend() is None
    doc = asr.transcribe_full(_vocal(SPANS), SR)
    assert doc["lines"] == [] and doc["backend"] is None and "no ASR backend" in doc["stats"]["skipped"]
    assert asr.transcribe(_vocal(SPANS), SR) == []
    monkeypatch.delenv("LYRICS_ASR")
    monkeypatch.setattr(asr, "_has", lambda m: False)
    assert asr.pick_backend() is None and asr.pick_backend("mlx") is None


def test_transcribes_only_sung_regions_and_keeps_good_lines():
    eng = Engine(SEGS)
    doc = asr.transcribe_full(_vocal(SPANS), SR, engine=eng)
    assert [l["text"] for l in doc["lines"]] == ["we light the city up", "hold me through the night",
                                                 "we light the city up", "never let it go now"]
    assert all(l["source"] == "asr" and l["conf"] >= asr.MIN_CONF for l in doc["lines"])
    assert doc["language"] == "en" and doc["words"][0]["w"] == "we"
    clips = list(zip(eng.clips[::2], eng.clips[1::2]))          # silent stretches are never decoded
    assert len(clips) == 4 and clips[0][0] == pytest.approx(1.6, abs=0.1) and clips[-1][1] <= 27.5
    assert all(b - a < 5 for a, b in clips)


def test_segment_thresholds_and_fillers_are_dropped():
    segs = [_seg(1, "real words here"), _seg(2, "loop loop loop", compression_ratio=3.1),
            _seg(3, "mumbled guess", avg_logprob=-1.4), _seg(4, "nobody sings", no_speech_prob=0.8),
            _seg(5, "thanks for watching"), _seg(6, "x", text=" ♪♪"), _seg(7, "x", text=" [Music]"),
            _seg(8, "x", text=" Subtitles by the community")]
    kept, why = asr.filter_segments(segs)
    assert [s["text"].strip() for s in kept] == ["real words here"]
    assert why == {"compression_ratio": 1, "avg_logprob": 1, "no_speech": 1, "filler": 4}
    assert asr.is_filler("you") and asr.is_filler("Thank you.") and not asr.is_filler("thank you for the night")


def test_low_confidence_unsung_and_loop_lines_are_dropped():
    y = _vocal([(0, 10)], dur=20)
    db, sung = asr.vocal_gate(y)
    unsure = {"t": 1.0, "end": 2.0, "text": "maybe words", "conf": 0.3, "words": []}
    silent = {"t": 12.0, "end": 14.0, "text": "over silence", "conf": 0.9, "words": []}
    good = {"t": 0.5, "end": 1.0, "text": "sung clearly", "conf": 0.9, "words": []}
    loop = [{"t": 2.0 + i, "end": 2.8 + i, "text": "same line", "conf": 0.9, "words": []} for i in range(5)]
    kept, why = asr.clean_lines([good, unsure] + loop + [silent], db, sung)
    assert [l["text"] for l in kept] == ["sung clearly", "same line"]
    assert why == {"low_conf": 1, "not_sung": 1, "repeat_loop": 4}
    # a hook sung three times is a hook, not a loop
    kept, _ = asr.clean_lines([good] + loop[:3], db, sung)
    assert len(kept) == 4


def test_line_confidence_comes_from_word_probabilities():
    seg = _seg(1, "one two three four")
    for w, p in zip(seg["words"], (0.9, 0.2, 0.2, 0.3)):
        w["prob"] = p
    (line,) = asr.build_lines([seg])
    assert line["conf"] == pytest.approx(0.4)
    doc = asr.transcribe_full(_vocal(SPANS), SR, engine=Engine([seg] + SEGS))
    assert "one two three four" not in [l["text"] for l in doc["lines"]]
    assert doc["stats"]["dropped"]["low_conf"] == 1


def test_thin_result_is_no_lyrics():
    doc = asr.transcribe_full(_vocal(SPANS), SR, engine=Engine(SEGS[:2]))
    assert doc["lines"] == [] and doc["stats"]["thin"]
    long_sung = _vocal([(0, 38)])                  # 3 short lines over 38 s of singing: under MIN_COVERAGE
    doc = asr.transcribe_full(long_sung, SR, engine=Engine([_seg(1, "a b"), _seg(10, "c d"), _seg(20, "e f")]))
    assert doc["lines"] == [] and doc["stats"]["coverage"] < asr.MIN_COVERAGE
    assert asr.transcribe_full(np.zeros(SR * 10, np.float32), SR, engine=Engine(SEGS))["lines"] == []


def test_lines_split_on_pauses_and_language_is_kept():
    seg = {"start": 1, "end": 6, "text": "a b c d", "avg_logprob": -0.2, "no_speech_prob": 0.0, "compression_ratio": 1.2,
           "words": [_w("a", 1, 1.3), _w("b", 1.4, 1.7), _w("c", 3.0, 3.3), _w("d", 3.4, 3.7)]}
    assert [l["text"] for l in asr.build_lines([seg])] == ["a b", "c d"]
    doc = asr.transcribe_full(_vocal(SPANS), SR, engine=Engine(SEGS, lang="es"))
    assert doc["language"] == "es" and doc["language_prob"] == 0.97


def test_cache_is_keyed_by_stem_hash_and_model(tmp_path):
    import soundfile as sf

    p = tmp_path / "vocals.flac"
    sf.write(p, _vocal(SPANS), SR)
    eng = Engine(SEGS)
    doc = asr.for_stem(p, engine=eng, cache_dir=tmp_path / "c", model="large-v3-turbo")
    assert len(doc["lines"]) == 4 and doc["vocal_hash"] == asr.stem_hash(p)
    assert asr.cache_path(doc["vocal_hash"], "large-v3-turbo", tmp_path / "c").is_file()
    eng.segs = []                                                     # a second call reads the cache
    assert asr.for_stem(p, engine=eng, cache_dir=tmp_path / "c", model="large-v3-turbo")["lines"] == doc["lines"]
    assert asr.cached(p, "small", tmp_path / "c") is None              # another model: transcribed again
    assert asr.for_stem(p, engine=eng, cache_dir=tmp_path / "c", model="small")["lines"] == []
    # mlx repo names and short names share one cache entry
    assert asr.cache_path("ab" * 8, "mlx-community/whisper-large-v3-turbo").name ==asr.cache_path("ab" * 8, "large-v3-turbo").name
    sf.write(p, _vocal(SPANS[:2]), SR)                                  # different audio: different key
    assert asr.cached(p, "large-v3-turbo", tmp_path / "c") is None


ASR_DOC = {"lines": [{"t": 2.2, "end": 4.0, "text": "we light the city up", "conf": 0.92, "source": "asr"},
                     {"t": 8.2, "end": 10.0, "text": "hold me through the night", "conf": 0.6, "source": "asr"},
                     {"t": 14.2, "end": 16.0, "text": "we light the city up", "conf": 0.9, "source": "asr"}],
           "words": [{"w": w, "t": t, "end": t + 0.35} for w, t in
                     [("we", 2.2), ("light", 2.6), ("the", 3.0), ("city", 3.4), ("up", 3.8),
                      ("hold", 8.2), ("me", 8.6), ("thru", 9.0), ("the", 9.4), ("night", 9.8),
                      ("we", 14.2), ("light", 14.6), ("the", 15.0), ("city", 15.4), ("up", 15.8)]]}


@pytest.fixture
def ly_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(lyrics, "LYRICS_DIR", tmp_path)
    fetch = lyrics.fetch                                          # never the network: no LRCLIB hit unless given
    monkeypatch.setattr(lyrics, "fetch", lambda title, cache_dir=None, search=None, duration=None:
                        fetch(title, cache_dir, search or (lambda a, t: []), duration))
    return tmp_path


def test_precedence_manual_lrclib_asr_plain(ly_dir):
    y, sr = _vocal(SPANS, sr=1000), 1000
    asr_fn = lambda p: ASR_DOC
    # nothing looked up: ASR lines
    got = lyrics.for_file("A - Song", y, sr, asr=asr_fn, vocal_path="v.flac")
    assert [l["source"] for l in got] == ["asr"] * 3
    # a verified, aligned LRC wins over ASR
    lrc = LRC
    lyrics.set_manual("B - Song", lrc)
    got = lyrics.for_file("B - Song", y, sr, asr=asr_fn)
    assert got and {l["source"] for l in got} == {"manual"}
    # pinned "no lyrics": never transcribed words
    lyrics.set_manual("C - Song", None)
    assert lyrics.for_file("C - Song", y, sr, asr=asr_fn) == []
    # listed instrumental on LRCLIB: no ASR either
    lyrics.fetch("D - Tune", search=lambda a, t: [{"id": 1, "artistName": "D", "trackName": "Tune", "instrumental": True}])
    assert lyrics.for_file("D - Tune", y, sr, asr=asr_fn) == []
    # LRCLIB synced hit, not manual: lrclib source
    lyrics.fetch("E - Song", search=lambda a, t: [{"id": 2, "artistName": "E", "trackName": "Song", "duration": 40,
                                                   "syncedLyrics": lrc}], duration=40)
    assert {l["source"] for l in lyrics.for_file("E - Song", y, sr, duration=40, asr=asr_fn)} == {"lrclib"}
    # no vocal path, no cache: nothing (no transcription inside a request)
    assert lyrics.for_file("F - Song", y, sr) == []


def test_plain_text_is_placed_on_asr_words(ly_dir):
    plain = ["We light the city up", "Hold me through the night", "Something never heard", "We light the city up"]
    got = lyrics.align_plain_to_words(plain, ASR_DOC["words"])
    assert [l["text"] for l in got] == plain
    assert got[0]["t"] == 2.2 and got[1]["t"] == 8.2 and got[3]["t"] == 14.2      # 'thru' ~ 'through'
    assert got[2].get("estimated") and 8.2 < got[2]["t"] < 14.2                   # unheard line: between neighbours
    assert not got[0].get("estimated") and {l["source"] for l in got} == {"plain+asr"}
    assert lyrics.align_plain_to_words(["totally other words", "nothing alike here"], ASR_DOC["words"]) == []
    # for_file prefers the placement, falls back to time_plain when it fails
    lyrics.set_from_lrclib("G - Song", 9, get=lambda i: {"plainLyrics": "\n".join(plain)})
    y, sr = _vocal(SPANS, sr=1000), 1000
    assert {l["source"] for l in lyrics.for_file("G - Song", y, sr, asr=lambda p: ASR_DOC)} == {"plain+asr"}
    got = lyrics.for_file("G - Song", y, sr, asr=lambda p: {"lines": [], "words": [{"w": "zzz", "t": 1, "end": 2}]})
    assert got and all(l["estimated"] and l["source"] == "plain" for l in got)


def test_hooks_and_hook_drops_only_trust_sure_asr_lines():
    lines = [l | {} for l in ASR_DOC["lines"]] + [{"t": 20.2, "end": 22, "text": "hold me through the night",
                                                   "conf": 0.6, "source": "asr"}]
    h = lyrics.hooks(lines)
    assert [x["text"] for x in h] == ["we light the city up"] and h[0]["source"] == "asr"
    low = [l | {"conf": 0.5} for l in lines]
    assert lyrics.hooks(low) == [] and not lyrics.is_hook("we light the city up", low)
    plan = hook_drop.plan(lines, 120.0, [float(x) for x in range(0, 40, 2)])
    assert plan and all(p["lyrics_source"] == "asr" and p["text"] == "we light the city up" for p in plan)
    assert hook_drop.plan(low, 120.0, [float(x) for x in range(0, 40, 2)]) == []
    assert lyrics.transcribed(lines) and not lyrics.transcribed([{"t": 0, "end": 1, "text": "x"}])


def test_review_brief_labels_transcribed_words():
    from types import SimpleNamespace

    from app.music_brain.learning import set_ai

    o = SimpleNamespace(detail={"words": "we light the city up", "words_transcribed": True}, kind="acapella_drop",
                        at=10.0, track_a="A", track_b="", tempo_gap=0.0, key_score=1.0)
    assert set_ai._brief(0, o)["words_transcribed"] is True
    assert "words_transcribed" not in set_ai._brief(0, o, lyrics=False)


@pytest.mark.slow
def test_real_model_on_a_cached_stem():
    """Real Whisper on one cached vocal stem (needs requirements-asr.txt + a model download)."""
    if asr.pick_backend() is None:
        pytest.skip("no ASR backend installed")
    stems = asr.vocal_stems()
    if not stems:
        pytest.skip("no cached vocal stems")
    doc = asr.transcribe_full(stems[0][1])
    assert doc["backend"] and doc["language"]
    assert all(0 <= l["conf"] <= 1 and l["source"] == "asr" and l["t"] < l["end"] for l in doc["lines"])
    asr.release()


def test_gpu_busy_reads_nvidia_smi_and_fails_open(monkeypatch):
    monkeypatch.setattr(asr.time, "sleep", lambda s: None)
    out = lambda txt: (lambda *a, **k: type("R", (), {"stdout": txt})())
    assert asr.gpu_busy(out("5, 3900\n")) is False
    assert asr.gpu_busy(out("95, 3900\n")) is True             # a separation pegs the card
    assert asr.gpu_busy(out("0, 300\n")) is True                # no room for the model
    assert asr.gpu_busy(out("garbage")) is False                # unreadable: no gate

    def missing(*a, **k):
        raise FileNotFoundError("nvidia-smi")

    assert asr.gpu_busy(missing) is False


def test_batch_waits_for_a_busy_gpu_and_releases_the_model(monkeypatch):
    monkeypatch.setattr(asr, "pick_backend", lambda *a, **k: "cuda")
    monkeypatch.setattr(asr, "vocal_stems", lambda: [("h1", "v1"), ("h2", "v2")])
    monkeypatch.setattr(asr, "cached", lambda *a, **k: None)
    events = []
    monkeypatch.setattr(asr, "for_stem", lambda v, **k: events.append(("asr", v)) or
                        {"lines": [{"t": 0}], "language": "en", "stats": {}})
    monkeypatch.setattr(asr, "release", lambda: events.append(("release",)))
    busy = iter([False, True, True, False])                   # free, then busy twice before song 2
    out = asr.batch(gate=lambda: next(busy), sleep=lambda s: events.append(("sleep", s)), log=lambda m: None)
    assert out["done"] == 2 and out["with_lyrics"] == 2
    assert events == [("asr", "v1"), ("release",), ("sleep", asr.GATE_POLL_S), ("asr", "v2"), ("release",)]
