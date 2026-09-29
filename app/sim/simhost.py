"""The sim's Host and AIBackend: the ports of `app.ui.engine`, filled by a `World` (world.py).

The brain (server, autopilot_service, download jobs, live ear ...) finds its edges through the
installed `Engine`, so the sim needs no monkeypatching: `World.engine()` builds
`Engine(SimHost(world), SimAI(world), EngineConfig(...))` and installs it.

  live     nothing is replaced except the logs, the clock, job execution and (when recording) the
           taps that write a fixture; YouTube, the model and Demucs are the real ones (the parent
           classes) wrapped by the World's throttle / cap / recorder.
  replay / library
           the audio is synthetic (synth.py): reads of a stem file are answered from the pool's
           frozen results (the same functions ran on the real stems when the entry was built).
"""
from __future__ import annotations

import itertools
from pathlib import Path

from app.sim.synth import read_tag
from app.ui.engine import AIBackend, Host

_STEMS = ("drums", "bass", "vocals", "other")


class SimAI(AIBackend):
    def __init__(self, world):
        self.w = world

    def chat(self, system, user, temperature, timeout, model, max_tokens) -> str:
        return self.w.chat(system, user, temperature, timeout, model, max_tokens, super().chat)

    def ear(self, cfg, wav, metrics) -> str:
        return self.w.ear(cfg, wav, metrics, super().ear)

    def audition(self, system, wav, text) -> str:
        return self.w.audition(system, wav, text, super().audition)


class SimHost(Host):
    def __init__(self, world):
        self.w = world
        self._ids = itertools.count(1)
        self._tagged = None

    @property
    def synthetic(self) -> bool:
        return self.w.mode != "live"

    # ---- YouTube ------------------------------------------------------------------
    def search_songs(self, query, limit=8):
        return self.w.search_songs(query, limit, super().search_songs)

    def verify_song(self, artist, title):
        return self.w.verify_song(artist, title, super().verify_song)

    def song_views(self, name):
        return self.w.song_views(name, super().song_views)

    def download_to_dir(self, url, output_dir, progress=None):
        return self.w.download_to_dir(url, Path(output_dir), progress, super().download_to_dir)

    def lrclib_search(self, artist, track):
        return self.w.lrclib_search(artist, track, super().lrclib_search)

    # ---- stems and audio ---------------------------------------------------------------
    def queue_stems(self, track_id, urgent=True):
        return self.w.queue_stems(track_id)

    def separate(self, audio_path, two_stems=None, model=None, **kw):
        if not self.synthetic:
            return super().separate(audio_path, two_stems=two_stems, model=model, **kw)
        from app.music_brain import stem_service

        files = self.w.synth_for_path(audio_path)
        if not files or not files["stems"]:
            from app.sim.world import WorldError

            raise WorldError(f"no synthetic stems for {audio_path}")
        st = dict(files["stems"])
        if two_stems == "vocals":
            st = {"vocals": st["vocals"], "no_vocals": files["mix"]}
        return stem_service.StemResult(audio_hash=Path(audio_path).stem, model=model or "sim", two_stems=two_stems, stems=st,
                                       cache_dir=str(Path(files["mix"]).parent), from_cache=True)

    def vocals_stem(self, track_id):
        if not self.synthetic:
            return super().vocals_stem(track_id)
        return self.w.synth_vocals_stem(track_id)

    def hook_drops(self, track_id):
        if not self.synthetic:
            return super().hook_drops(track_id)
        return list((self.w.track_entry(track_id) or {}).get("hook_drops") or [])

    def keylock_stem_path(self, key, name):
        p = super().keylock_stem_path(key, name)
        if p is None or not self.synthetic:
            return p
        return self.w.tag_keylock_render(p, key, name)       # a key-locked render is real rubberband output; tag its stem

    def load_audio(self, path, sr=22050, mono=True, offset=0.0, duration=None, **kw):
        tag = self.w.stem_tag(path) if self.synthetic else None
        if not tag:
            return super().load_audio(path, sr=sr, mono=mono, offset=offset, duration=duration, **kw)
        import numpy as np

        class Tagged(np.ndarray):
            pass

        a = np.zeros(1, dtype=np.float32).view(Tagged)
        a.tag = (tag["hash"], tag["stem"], round(float(offset or 0.0), 3))
        return a, sr

    def stem_map(self, audio, sr, phrases):
        if self.synthetic and hasattr(audio.get("drums"), "tag"):
            return self.w.entry_for(audio["drums"].tag[0]).get("smap") or []
        return super().stem_map(audio, sr, phrases)

    def vocal_style(self, y, sr):
        if self.synthetic and hasattr(y, "tag"):
            return self.w.entry_for(y.tag[0]).get("vocal_style", {}).get(repr(y.tag[2])) or {"rap": False, "rap_score": 0.0}
        return super().vocal_style(y, sr)

    def file_hash(self, path):
        tag = read_tag(Path(path))              # a synthetic file names the real song it stands for
        return tag["hash"] if tag and tag.get("hash") else super().file_hash(path)

    def library_raws(self):
        return self.w.library_raws_frozen()

    # ---- logs ---------------------------------------------------------------------------
    def log_event(self, kind, **fields):
        self.w.emit(kind, **fields)

    def song_step(self, kind, track_id, **fields):
        self.w.song_step(kind, track_id, **fields)

    def session_event(self, kind, fields):
        return None

    # ---- jobs, ids, time ---------------------------------------------------------------------
    def spawn(self, pool, fn, *args, **kw):
        """Background jobs run inline, in the request that starts them: their result never depends on
        thread timing. The console's polling and the modelled download time are the transport's job."""
        fn(*args, **kw)

    def new_id(self, nbytes=6):
        return f"{next(self._ids):0{2 * nbytes}x}"

    def now(self):
        return 1_790_000_000.0 + self.w.vclock()
