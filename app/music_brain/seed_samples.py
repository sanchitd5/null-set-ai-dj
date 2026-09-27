"""Synthesizes a starter set of sampler one-shots into SAMPLES_CACHE_DIR.

Run once (idempotent -- skips files that already exist):
    python -m app.music_brain.seed_samples
"""

from __future__ import annotations

import numpy as np
import soundfile as sf

from app.music_brain.config import SAMPLES_CACHE_DIR

SR = 44100


def _fade(sig: np.ndarray, ms: int = 5) -> np.ndarray:
    n = min(int(SR * ms / 1000), len(sig) // 2)
    if n:
        sig[:n] *= np.linspace(0.0, 1.0, n)
        sig[-n:] *= np.linspace(1.0, 0.0, n)
    return sig


def _kick(dur=0.35, f0=150.0, f1=40.0) -> np.ndarray:
    t = np.linspace(0, dur, int(SR * dur), endpoint=False)
    freq = f1 + (f0 - f1) * np.exp(-t * 25)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    env = np.exp(-t * 12)
    return _fade(np.sin(phase) * env)


def _clap(dur=0.25) -> np.ndarray:
    t = np.linspace(0, dur, int(SR * dur), endpoint=False)
    noise = np.random.default_rng(1).standard_normal(len(t))
    burst_env = sum(np.exp(-((t - k * 0.012) ** 2) / (2 * 0.002 ** 2)) for k in range(4))
    tail_env = np.exp(-t * 18) * 0.4
    return _fade(noise * (burst_env + tail_env))


def _hat(dur=0.12) -> np.ndarray:
    t = np.linspace(0, dur, int(SR * dur), endpoint=False)
    noise = np.random.default_rng(2).standard_normal(len(t))
    b, a = np.array([1.0, -1.0]), np.array([1.0])
    hp = np.convolve(noise, b, mode="same")
    env = np.exp(-t * 60)
    return _fade(hp * env)


def _click(dur=0.05, freq=1000.0) -> np.ndarray:
    t = np.linspace(0, dur, int(SR * dur), endpoint=False)
    env = np.exp(-t * 80)
    return _fade(np.sin(2 * np.pi * freq * t) * env)


def _riser(dur=1.5, f0=200.0, f1=4000.0) -> np.ndarray:
    t = np.linspace(0, dur, int(SR * dur), endpoint=False)
    freq = f0 * (f1 / f0) ** (t / dur)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    env = np.linspace(0.05, 1.0, len(t)) ** 2
    return _fade(np.sin(phase) * env, ms=20)


STARTER_BANK = {
    "kick_808": _kick,
    "clap_tight": _clap,
    "hat_closed": _hat,
    "click_ref": _click,
    "riser_fx": _riser,
}


def seed() -> list[str]:
    written = []
    for name, gen in STARTER_BANK.items():
        dest = SAMPLES_CACHE_DIR / f"{name}.wav"
        if dest.exists():
            continue
        sig = gen().astype(np.float32)
        sig = sig / max(np.abs(sig).max(), 1e-9) * 0.9
        sf.write(dest, sig, SR)
        written.append(name)
    return written


if __name__ == "__main__":
    made = seed()
    print(f"seeded {len(made)} sample(s): {', '.join(made) or '(none, already present)'}")
