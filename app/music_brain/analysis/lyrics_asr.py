"""Lyrics heard, not looked up: speech-to-text (Whisper) on a song's vocal stem.

The third lyric source (app.music_brain.analysis.lyrics: manual > verified LRCLIB >
this > plain text timed by energy). Its word timings also place a plain (untimed)
LRCLIB lyric on the file. Whisper invents text over music, and a wrong lyric is
worse than none, so every guard below drops rather than keeps:

* only the vocal stem's sung spans are decoded (stem RMS gate, padded), never the
  instrumental stretches;
* no conditioning on previous text, temperature fallback, Whisper's own
  no-speech / log-prob / compression-ratio thresholds, and segments over them dropped;
* subtitle-credit and music-note filler lines dropped, and a line looped back to back
  with an identical vocal under every copy cut to one;
* each line carries conf (mean word probability); lines under MIN_CONF are dropped;
* a result too thin to trust (few lines, little of the sung time covered) is [].

Backends (all optional imports; with none the app runs and this returns []):
faster-whisper float16 on CUDA, mlx-whisper on Apple Silicon, faster-whisper int8
on CPU. Env: LYRICS_ASR=off disables, LYRICS_ASR_BACKEND=cuda|mlx|cpu forces one,
LYRICS_ASR_MODEL picks the model (default large-v3-turbo). One job at a time; the
model is freed by release() (the GPU is shared).

Results are cached under data/cache/lyrics_asr/ keyed by the vocal stem's audio
hash + model name, so a model change transcribes again. The cache keeps word timings.
"""
from __future__ import annotations

import gc
import json
import os
import platform
import re
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.music_brain.config import CACHE_DIR

ASR_DIR = CACHE_DIR / "lyrics_asr"
ASR_V = 1
SR = 16000
DEFAULT_MODEL = "large-v3-turbo"

# sung-span gate on the vocal stem
STEP_S = 0.05
REGION_PAD_S = 0.4             # a little air around each sung span (consonants, breaths)
REGION_MERGE_GAP_S = 1.5       # spans closer than this are one region
MIN_REGION_S = 0.8             # shorter blips (a breath, bleed) are not decoded

# decoding (Whisper's own fallback thresholds) and the drop rules applied after it
# Short fallback ladder: on rap / dense vocals a window that loops at 0.0 rarely decodes
# clean at 0.8-1.0, and those retries made such songs 8-12x slower (37-64 s per minute
# of audio vs ~5) only for the segment to be dropped by the thresholds anyway.
TEMPERATURES = (0.0, 0.2, 0.4)
COMPRESSION_MAX = 2.4
LOGPROB_MIN = -1.0
NO_SPEECH_MAX = 0.6
SILENCE_HALLUCINATION_S = 2.0  # Whisper skips text it places over this much silence

MIN_CONF = float(os.environ.get("LYRICS_ASR_MIN_CONF") or 0.55)
MIN_LINES = 3
MIN_COVERAGE = 0.15            # kept lines must cover this share of the sung time
MIN_SUNG_SHARE = 0.5           # a line must sit on singing for this share of its span
REPEAT_RUN = 4                 # this many identical lines back to back ...
REPEAT_FLAT_DB = 1.0           # ... with the vocal this flat under each copy = a decode loop
LINE_GAP_S = 0.6               # a pause this long between words starts a new line
LINE_MAX_WORDS = 10
LINE_MAX_S = 12.0

_FILLER = re.compile(
    r"(?i)(thanks?\s+(you\s+)?(so\s+much\s+)?for\s+(watching|listening)|please\s+subscribe|like\s+and\s+subscribe|"
    r"subtitles?\s+(by|made|created)|captions?\s+by|transcri(bed|ption)\s+by|translated\s+by|amara\.org|"
    r"www\.|https?://|\.com\b|copyright|all\s+rights\s+reserved|see\s+you\s+(in\s+the\s+)?next|"
    r"untertitel|sous-titres|sottotitoli|subt[ií]tulos|ご視聴|字幕|订阅|訂閱|點贊|点赞)")
_FILLER_EXACT = {"you", "thank you", "thanks", "thank you so much", "bye", "bye bye", "music", "applause",
                 "laughter", "silence", "instrumental", "outro", "intro"}
_NO_WORDS = re.compile(r"^[\W_♪♫♩♬]*$")
_BRACKETED = re.compile(r"^\s*[\[(*][^\])*]*[\])*]\s*$")   # [Music], (applause), *laughs*
Counter_t = Dict[str, int]


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").lower()).strip()


# ------------------------------------------------------------------ backend

def model_name(model: Optional[str] = None) -> str:
    return model or os.environ.get("LYRICS_ASR_MODEL") or DEFAULT_MODEL


def _has(mod: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _cuda() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def pick_backend(backend: Optional[str] = None) -> Optional[str]:
    """'cuda' | 'mlx' | 'cpu', or None when off / nothing installed."""
    if (os.environ.get("LYRICS_ASR") or "").strip().lower() in ("off", "0", "false", "no"):
        return None
    want = (backend or os.environ.get("LYRICS_ASR_BACKEND") or "auto").strip().lower()
    fw, mlx = _has("faster_whisper"), _has("mlx_whisper")
    if want == "cuda":
        return "cuda" if fw and _cuda() else None
    if want == "mlx":
        return "mlx" if mlx else None
    if want == "cpu":
        return "cpu" if fw else None
    if fw and _cuda():
        return "cuda"
    if mlx and platform.system() == "Darwin" and platform.machine() == "arm64":
        return "mlx"
    return "cpu" if fw else None


class _FasterWhisper:
    def __init__(self, model: str, device: str):
        from faster_whisper import WhisperModel

        self.m = WhisperModel(model, device=device, compute_type="float16" if device == "cuda" else "int8")

    def language(self, audio: np.ndarray) -> Tuple[Optional[str], Optional[float]]:
        lang, prob, _ = self.m.detect_language(audio=audio)
        return lang, float(prob)

    def run(self, audio: np.ndarray, clips: List[float], language: Optional[str]) -> List[dict]:
        segs, _ = self.m.transcribe(
            audio, language=language, task="transcribe", beam_size=5, temperature=list(TEMPERATURES),
            compression_ratio_threshold=COMPRESSION_MAX, log_prob_threshold=LOGPROB_MIN,
            no_speech_threshold=NO_SPEECH_MAX, condition_on_previous_text=False, word_timestamps=True,
            vad_filter=False, clip_timestamps=clips, hallucination_silence_threshold=SILENCE_HALLUCINATION_S)
        return [{"start": s.start, "end": s.end, "text": s.text, "avg_logprob": s.avg_logprob,
                 "no_speech_prob": s.no_speech_prob, "compression_ratio": s.compression_ratio,
                 "words": [{"word": w.word, "start": w.start, "end": w.end, "prob": w.probability}
                           for w in (s.words or [])]} for s in segs]

    def close(self) -> None:
        unload = getattr(getattr(self.m, "model", None), "unload_model", None)
        if unload:
            try:
                unload()
            except Exception:
                pass
        self.m = None


class _Mlx:
    def __init__(self, model: str):
        import mlx_whisper  # noqa: F401  (import error -> backend unavailable)

        self.repo = model if "/" in model else f"mlx-community/whisper-{model}"

    def _call(self, audio, **kw) -> dict:
        import mlx_whisper

        return mlx_whisper.transcribe(
            audio, path_or_hf_repo=self.repo, verbose=None, temperature=TEMPERATURES,
            compression_ratio_threshold=COMPRESSION_MAX, logprob_threshold=LOGPROB_MIN,
            no_speech_threshold=NO_SPEECH_MAX, condition_on_previous_text=False, word_timestamps=True, **kw)

    def language(self, audio: np.ndarray) -> Tuple[Optional[str], Optional[float]]:
        return self._call(audio, language=None).get("language"), None

    def run(self, audio: np.ndarray, clips: List[float], language: Optional[str]) -> List[dict]:
        r = self._call(audio, language=language, clip_timestamps=clips,
                       hallucination_silence_threshold=SILENCE_HALLUCINATION_S)
        return [{"start": s["start"], "end": s["end"], "text": s["text"], "avg_logprob": s.get("avg_logprob", 0.0),
                 "no_speech_prob": s.get("no_speech_prob", 0.0), "compression_ratio": s.get("compression_ratio", 0.0),
                 "words": [{"word": w["word"], "start": w["start"], "end": w["end"], "prob": w.get("probability", 0.0)}
                           for w in s.get("words") or []]} for s in r.get("segments") or []]

    def close(self) -> None:
        try:
            from mlx_whisper.transcribe import ModelHolder

            ModelHolder.model = ModelHolder.model_path = None
            import mlx.core as mx

            (getattr(mx, "clear_cache", None) or mx.metal.clear_cache)()
        except Exception:
            pass


_LOCK = threading.Lock()           # one transcription at a time (one model, one GPU)
_ENGINE: Dict[str, object] = {}    # {"key": (backend, model), "engine": ...}


def busy() -> bool:
    """A transcription is running in this process."""
    return _LOCK.locked()


def _engine(backend: str, model: str):
    if _ENGINE.get("key") != (backend, model):
        release()
        _ENGINE["engine"] = _Mlx(model) if backend == "mlx" else _FasterWhisper(model, backend)
        _ENGINE["key"] = (backend, model)
    return _ENGINE["engine"]


def release() -> None:
    """Free the model (and the GPU memory it holds). Next transcription loads it again."""
    eng = _ENGINE.pop("engine", None)
    _ENGINE.pop("key", None)
    if eng is not None:
        eng.close()
        del eng
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


# ------------------------------------------------------------------ audio + gate

def _load16k(vocal, sr: Optional[int]) -> np.ndarray:
    if isinstance(vocal, (str, Path)):
        import librosa

        y, _ = librosa.load(str(vocal), sr=SR, mono=True)
        return y.astype(np.float32)
    y = np.asarray(vocal, dtype=np.float32)
    if y.ndim > 1:
        y = y.mean(axis=0 if y.shape[0] < y.shape[-1] else 1)
    if sr and sr != SR:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(int(sr), SR)
        y = resample_poly(y, SR // g, int(sr) // g).astype(np.float32)
    return y


def vocal_gate(y: np.ndarray, sr: int = SR, step: float = STEP_S) -> Tuple[np.ndarray, np.ndarray]:
    """(dB per step, sung mask) of a vocal stem: the same floor lyrics.time_plain uses."""
    hop = int(sr * step)
    n = len(y) // hop
    if n == 0:
        return np.zeros(0), np.zeros(0, bool)
    db = 10 * np.log10(np.mean(np.square(y[:n * hop].reshape(n, hop)), axis=1) + 1e-12)
    return db, db > max(-45.0, float(np.percentile(db, 90)) - 25.0)


def regions(sung: np.ndarray, step: float = STEP_S, duration: Optional[float] = None) -> List[Tuple[float, float]]:
    """Sung spans: padded, merged across short gaps, blips dropped."""
    out: List[List[float]] = []
    i, n = 0, len(sung)
    while i < n:
        if not sung[i]:
            i += 1
            continue
        j = i
        while j < n and sung[j]:
            j += 1
        a, b = max(0.0, i * step - REGION_PAD_S), j * step + REGION_PAD_S
        if out and a - out[-1][1] <= REGION_MERGE_GAP_S:
            out[-1][1] = b
        else:
            out.append([a, b])
        i = j
    end = duration if duration is not None else n * step
    return [(round(a, 2), round(min(b, end), 2)) for a, b in out if min(b, end) - a >= MIN_REGION_S]


def _pad_regions(regs: Sequence[Sequence[float]], duration: float) -> List[Tuple[float, float]]:
    """Caller-supplied regions (analysis vocal_active_regions): padded and merged like ours."""
    out: List[List[float]] = []
    for a, b in sorted((float(r[0]), float(r[1])) for r in regs):
        a, b = max(0.0, a - REGION_PAD_S), min(duration, b + REGION_PAD_S)
        if out and a - out[-1][1] <= REGION_MERGE_GAP_S:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 2), round(b, 2)) for a, b in out if b - a >= MIN_REGION_S]


# ------------------------------------------------------------------ filters (pure; tested on synthetic segments)

def is_filler(text: str) -> bool:
    t = (text or "").strip()
    return (not t or bool(_NO_WORDS.match(t)) or bool(_BRACKETED.match(t)) or bool(_FILLER.search(t))
            or _norm(t) in _FILLER_EXACT)


def filter_segments(segs: Sequence[dict]) -> Tuple[List[dict], Counter_t]:
    """Segments Whisper itself would distrust are dropped: -> (kept, {reason: count})."""
    kept, why = [], {}
    for s in segs:
        r = None
        if s.get("compression_ratio", 0.0) > COMPRESSION_MAX:
            r = "compression_ratio"
        elif s.get("avg_logprob", 0.0) < LOGPROB_MIN:
            r = "avg_logprob"
        elif s.get("no_speech_prob", 0.0) > NO_SPEECH_MAX:
            r = "no_speech"
        elif is_filler(s.get("text", "")):
            r = "filler"
        if r:
            why[r] = why.get(r, 0) + 1
        else:
            kept.append(s)
    return kept, why


def build_lines(segs: Sequence[dict]) -> List[dict]:
    """Segments -> sung lines {t, end, text, conf, words[]}: a new line at a segment
    boundary, a pause of LINE_GAP_S, or LINE_MAX_WORDS words."""
    lines: List[dict] = []
    for s in segs:
        words = [w for w in s.get("words") or [] if (w.get("word") or "").strip()]
        if not words:                              # no word timings: the segment is one line
            if (s.get("text") or "").strip():
                lines.append({"t": s["start"], "end": s["end"], "text": s["text"].strip(),
                              "conf": float(np.exp(min(0.0, s.get("avg_logprob", -5.0)))), "words": []})
            continue
        cur: List[dict] = []
        for w in words:
            if cur and (w["start"] - cur[-1]["end"] >= LINE_GAP_S or len(cur) >= LINE_MAX_WORDS):
                lines.append(_line(cur))
                cur = []
            cur.append(w)
        if cur:
            lines.append(_line(cur))
    return lines


def _line(words: List[dict]) -> dict:
    t, end = float(words[0]["start"]), float(words[-1]["end"])
    return {"t": t, "end": min(end, t + LINE_MAX_S), "text": "".join(w["word"] for w in words).strip(),
            "conf": float(np.mean([float(w.get("prob", 0.0)) for w in words])),
            "words": [{"w": w["word"].strip(), "t": round(float(w["start"]), 2), "end": round(float(w["end"]), 2),
                       "p": round(float(w.get("prob", 0.0)), 3)} for w in words]}


def clean_lines(lines: List[dict], db: Optional[np.ndarray] = None, sung: Optional[np.ndarray] = None,
                step: float = STEP_S, min_conf: float = MIN_CONF) -> Tuple[List[dict], Counter_t]:
    """Line-level drops: filler, low confidence, not on singing, decode loops -> (kept, {reason: count})."""
    why: Dict[str, int] = {}

    def drop(r):
        why[r] = why.get(r, 0) + 1

    def span(l):
        return slice(int(l["t"] / step), max(int(l["t"] / step) + 1, int(l["end"] / step)))

    kept = []
    for l in lines:
        if is_filler(l["text"]):
            drop("filler")
        elif l["conf"] < min_conf:
            drop("low_conf")
        elif sung is not None and len(sung) and (sung[span(l)].mean() if sung[span(l)].size else 0.0) < MIN_SUNG_SHARE:
            drop("not_sung")
        else:
            kept.append(l)
    out, i = [], 0
    while i < len(kept):                          # a run of identical lines over an identical vocal: a loop
        j = i
        while j + 1 < len(kept) and _norm(kept[j + 1]["text"]) == _norm(kept[i]["text"]):
            j += 1
        run = kept[i:j + 1]
        if len(run) >= REPEAT_RUN and db is not None and len(db):
            lv = [float(np.median(db[span(l)])) if db[span(l)].size else -120.0 for l in run]
            if max(lv) - min(lv) < REPEAT_FLAT_DB:
                out.append(run[0])
                for _ in run[1:]:
                    drop("repeat_loop")
                i = j + 1
                continue
        out.extend(run)
        i = j + 1
    return out, why


def coverage(lines: Sequence[dict], sung: np.ndarray, step: float = STEP_S) -> float:
    """Share of the sung frames that some kept line covers."""
    if sung is None or not len(sung) or not sung.any():
        return 0.0
    m = np.zeros(len(sung), bool)
    for l in lines:
        m[int(l["t"] / step): int(l["end"] / step) + 1] = True
    return float((m & sung).sum() / sung.sum())


def too_thin(lines: Sequence[dict], sung: Optional[np.ndarray]) -> bool:
    return len(lines) < MIN_LINES or (sung is not None and coverage(lines, sung) < MIN_COVERAGE)


# ------------------------------------------------------------------ transcription

def transcribe_full(vocal, sr: Optional[int] = None, vocal_regions: Optional[Sequence[Sequence[float]]] = None,
                    backend: Optional[str] = None, model: Optional[str] = None, engine=None,
                    log: Callable[[str], None] = lambda m: None) -> dict:
    """Full result: {lines, words, language, language_prob, backend, model, stats}.
    lines are [] when no backend, no singing, or the result is too thin to trust.
    engine: an object with language(audio)/run(audio, clips, language) (tests mock it)."""
    name = model_name(model)
    be = "mock" if engine is not None else pick_backend(backend)
    doc = {"v": ASR_V, "lines": [], "words": [], "language": None, "language_prob": None,
           "backend": be, "model": name, "stats": {}}
    if be is None:
        doc["stats"]["skipped"] = "no ASR backend (LYRICS_ASR off or requirements-asr.txt not installed)"
        return doc
    y = _load16k(vocal, sr)
    dur = len(y) / SR
    db, sung = vocal_gate(y)
    regs = _pad_regions(vocal_regions, dur) if vocal_regions else regions(sung, duration=dur)
    sung_s = float(sung.sum() * STEP_S)
    doc["stats"].update(duration_s=round(dur, 1), sung_s=round(sung_s, 1), regions=len(regs),
                        decoded_s=round(sum(b - a for a, b in regs), 1))
    if not regs:
        doc["stats"]["skipped"] = "no singing in the vocal stem"
        return doc
    t0 = time.time()
    with _LOCK:
        eng = engine if engine is not None else _engine(be, name)
        a, b = max(regs, key=lambda r: r[1] - r[0])          # language from the longest sung span
        lang, prob = eng.language(y[int(a * SR): int(min(b, a + 30.0) * SR)])
        segs = eng.run(y, [x for r in regs for x in r], lang)
    doc["stats"]["asr_s"] = round(time.time() - t0, 1)
    doc["language"], doc["language_prob"] = lang, (round(prob, 3) if prob is not None else None)
    segs = sorted(segs, key=lambda s: s["start"])
    kept, why = filter_segments(segs)
    lines, why2 = clean_lines(build_lines(kept), db, sung)
    for k, v in why2.items():
        why[k] = why.get(k, 0) + v
    cov = coverage(lines, sung)
    doc["stats"].update(segments=len(segs), dropped=why, kept_lines=len(lines), coverage=round(cov, 3))
    if too_thin(lines, sung):
        doc["stats"]["thin"] = True
        log(f"ASR: {len(lines)} line(s), {cov:.0%} of the singing covered: too thin, no lyrics")
        lines = []
    doc["words"] = [w for l in lines for w in l["words"]]
    doc["lines"] = [{"t": round(l["t"], 2), "end": round(l["end"], 2), "text": l["text"],
                     "conf": round(l["conf"], 3), "source": "asr"} for l in lines]
    return doc


def transcribe(vocal, sr: Optional[int] = None, **kw) -> List[dict]:
    """[{t, end, text, conf, source: 'asr'}] for a vocal stem (path or array + sr); [] when
    there is no backend or the result is not trustworthy. Not cached: see for_stem."""
    return transcribe_full(vocal, sr, **kw)["lines"]


# ------------------------------------------------------------------ cache

_HASHES: Dict[Tuple[str, float, int], str] = {}


def stem_hash(vocal_path) -> str:
    from app.music_brain.audio.stem_service import file_hash

    p = Path(vocal_path)
    st = p.stat()
    key = (str(p.resolve()), st.st_mtime, st.st_size)
    if key not in _HASHES:
        _HASHES[key] = file_hash(p)
    return _HASHES[key]


def _slug(model: str) -> str:
    return re.sub(r"[^\w.-]+", "_", model.rsplit("/", 1)[-1].removeprefix("whisper-"))


def cache_path(vocal_hash: str, model: Optional[str] = None, cache_dir: Optional[Path] = None) -> Path:
    return Path(cache_dir if cache_dir is not None else ASR_DIR) / f"{vocal_hash[:16]}_{_slug(model_name(model))}.json"


def cached(vocal_path, model: Optional[str] = None, cache_dir: Optional[Path] = None) -> Optional[dict]:
    """The cached transcription of this vocal stem with this model, or None (never transcribes)."""
    try:
        doc = json.loads(cache_path(stem_hash(vocal_path), model, cache_dir).read_text(encoding="utf-8"))
        return doc if doc.get("v") == ASR_V else None
    except (OSError, ValueError):
        return None


def for_stem(vocal_path, force: bool = False, vocal_regions=None, backend: Optional[str] = None,
             model: Optional[str] = None, cache_dir: Optional[Path] = None, engine=None,
             log: Callable[[str], None] = lambda m: None) -> dict:
    """Cached transcription of a vocal stem file (transcribed on a miss). A run without a
    backend is not cached (installing one later must not find a stale empty answer)."""
    if not force:
        doc = cached(vocal_path, model, cache_dir)
        if doc is not None:
            return doc
    doc = transcribe_full(vocal_path, vocal_regions=vocal_regions, backend=backend, model=model, engine=engine, log=log)
    if doc["backend"] is None:
        return doc
    doc["vocal_hash"] = stem_hash(vocal_path)
    doc["created"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    p = cache_path(doc["vocal_hash"], model, cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


def vocal_stems(stems_dir: Optional[Path] = None) -> List[Tuple[str, str]]:
    """(song id or audio hash, vocal stem path) for every cached stem set, one per song,
    htdemucs_ft first (the stem the rest of the app reads). Song folders first, then the old flat cache."""
    return _best_vocals(stems_dir, None)


def vocal_for_song(song_path, stems_dir: Optional[Path] = None) -> Optional[str]:
    """The cached vocal stem of a song file (never separates), or None."""
    from app.music_brain.audio.stem_service import file_hash

    got = _best_vocals(stems_dir, file_hash(Path(song_path)))
    return got[0][1] if got else None


def current_vocal(song_hash: str, stems_dir: Optional[Path] = None) -> Optional[str]:
    """The song's best vocal stem right now (ft first), or None when its stems are gone."""
    got = _best_vocals(stems_dir, song_hash)
    return got[0][1] if got else None


def _best_vocals(root: Optional[Path], only: Optional[str]) -> List[Tuple[str, str]]:
    """root None: the song folders and the old flat cache; a path: only that flat stem cache. only: one song
    (its audio sha256 or its 16-hex id)."""
    from app.music_brain import song_store
    from app.music_brain.audio.stem_service import read_manifest
    from app.music_brain.config import DEMUCS_MODEL

    best: Dict[str, Tuple[int, str]] = {}
    for h, d in song_store.iter_stem_variants(root):
        if only and not (h == only or (len(h) == 16 and only.startswith(h))):
            continue
        kind = song_store.variant_of(d, h)
        st = read_manifest(d) or {}
        v = st.get("vocals")
        if not v or not Path(v).is_file():
            continue
        rank = 0 if kind == DEMUCS_MODEL else 1
        k = h[:16]                                   # a song in its folder and in the flat cache counts once
        if k not in best or rank < best[k][0]:
            best[k] = (rank, v)
    return [(h, v) for h, (_, v) in sorted(best.items())]


GATE_UTIL_PCT = float(os.environ.get("LYRICS_ASR_GATE_UTIL") or 30)      # GPU busier than this: wait
GATE_FREE_MB = float(os.environ.get("LYRICS_ASR_GATE_FREE_MB") or 3000)  # less VRAM free than this: wait (large-v3-turbo fp16 OOMs at ~1 GB free)
GATE_POLL_S = 20.0


def gpu_busy(run=None, samples: int = 3) -> bool:
    """The shared card is in use by someone else (a stem separation, the LLM): any of a few
    nvidia-smi samples over GATE_UTIL_PCT, or under GATE_FREE_MB free. False when nvidia-smi
    is missing or fails (no gate rather than a batch that never starts)."""
    import subprocess

    run = run or subprocess.run
    for k in range(samples):
        if k:
            time.sleep(1.0)
        try:
            r = run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.free", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=10)
            util, free = (float(x) for x in r.stdout.strip().splitlines()[0].split(","))
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return False
        if util > GATE_UTIL_PCT or free < GATE_FREE_MB:
            return True
    return False


def batch(missing_only: bool = True, limit: Optional[int] = None, backend: Optional[str] = None,
          model: Optional[str] = None, log: Callable[[str], None] = print,
          gate: Optional[Callable[[], bool]] = None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Transcribe every cached vocal stem (missing_only: those without a cached result for
    this model). The model is freed at the end. -> {done, skipped, failed, with_lyrics}.
    gate() True = the shared GPU is busy: the model is released and the batch waits before the
    next song (default on cuda: gpu_busy; LYRICS_ASR_GPU_GATE=off turns it off)."""
    be = pick_backend(backend)
    if be is None:
        raise ValueError("no ASR backend: pip install -r requirements-asr.txt (or LYRICS_ASR is off)")
    todo = vocal_stems()
    if missing_only:
        todo = [(h, v) for h, v in todo if cached(v, model) is None]
    if limit:
        todo = todo[:limit]
    out = {"backend": be, "model": model_name(model), "todo": len(todo), "done": 0, "failed": 0, "with_lyrics": 0}
    log(f"ASR batch: {len(todo)} vocal stem(s), backend {be}, model {model_name(model)}")
    if gate is None and be == "cuda" and (os.environ.get("LYRICS_ASR_GPU_GATE") or "on").lower() not in ("off", "0", "no"):
        gate = gpu_busy
    try:
        for i, (h, v) in enumerate(todo, 1):
            if gate is not None and gate():
                release()                     # the card is shared: hand it back while others work
                log(f"[{i}/{len(todo)}] GPU busy (stems / LLM): waiting")
                while gate():
                    sleep(GATE_POLL_S)
            # the stem backfill may have replaced this song's set (htdemucs -> htdemucs_ft) since the
            # scan: transcribe the vocal the app reads now, and skip it when that one is already done
            v = current_vocal(h)
            if v is None or (missing_only and cached(v, model) is not None):
                out["skipped"] = out.get("skipped", 0) + 1
                continue
            try:
                doc = for_stem(v, force=not missing_only, backend=be, model=model, log=log)
                out["done"] += 1
                out["with_lyrics"] += bool(doc["lines"])
                st = doc["stats"]
                log(f"[{i}/{len(todo)}] {h[:12]}: {len(doc['lines'])} line(s), lang {doc['language']}, "
                    f"{st.get('asr_s', 0)}s for {st.get('duration_s', 0)}s audio")
            except Exception as exc:          # one bad file must not stop the batch
                out["failed"] += 1
                log(f"[{i}/{len(todo)}] {h[:12]}: failed ({type(exc).__name__}: {str(exc)[:200]})")
    finally:
        release()
    log(f"ASR batch done: {out}")
    return out
