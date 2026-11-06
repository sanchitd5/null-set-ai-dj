"""Streaming stem engine (app/music_brain/audio/stem_engine.py): equals Demucs' whole-track apply_model
(shifts=0) up to float rounding, at any batch size and track length, for a single model and a bag; OOM
backoff; atomic FLAC output. A tiny context-dependent fake model stands in for HTDemucs."""

from __future__ import annotations

import numpy as np
import pytest
import torch

demucs_apply = pytest.importorskip("demucs.apply")

from app.music_brain.audio import stem_engine as se  # noqa: E402


class Fake(torch.nn.Module):
    """4 'stems', each a different smoothing of the input: output depends on context, so wrong padding
    or wrong overlap-add shows up. valid_length like HTDemucs (always the training segment)."""

    sources = ["drums", "bass", "other", "vocals"]
    samplerate = 100
    audio_channels = 2

    def __init__(self, seg_s: float = 1.0, seed: int = 0):
        super().__init__()
        self.segment = seg_s
        g = torch.Generator().manual_seed(seed)
        self.k = torch.nn.Parameter(torch.rand(4, 1, 1, 9, generator=g), requires_grad=False)
        self.oom_until_batch = None

    def valid_length(self, length):
        return int(self.segment * self.samplerate)

    def forward(self, x):                                   # (B, 2, T) -> (B, 4, 2, T)
        if self.oom_until_batch is not None and x.shape[0] > self.oom_until_batch:
            raise RuntimeError("CUDA out of memory (fake)")
        B, C, T = x.shape
        y = torch.nn.functional.conv2d(x.reshape(B * C, 1, 1, T), self.k, padding=(0, 4))
        return torch.tanh(y.reshape(B, C, 4, T).permute(0, 2, 1, 3) * 3) + x[:, None] * 0.1


def _reference(model, x):
    mean, std = se.mix_stats(x)
    t = torch.from_numpy(((x.T - mean) / std).astype(np.float32))[None]
    with torch.no_grad():
        out = demucs_apply.apply_model(model, t, shifts=0, split=True, overlap=0.25)[0].numpy()
    return (out * std + mean).transpose(0, 2, 1)           # (S, n, 2)


def _ours(model, x, batch):
    eng = se.StemEngine("fake", "cpu", batch=batch, loaded=model)
    blocks = []
    eng.separate_array(x, blocks.append)
    return np.concatenate(blocks, axis=1), blocks


@pytest.mark.parametrize("n", [37, 100, 333, 1234, 5000])
@pytest.mark.parametrize("batch", [1, 3, 8])
def test_equals_whole_track_apply_model(n, batch):
    x = np.random.default_rng(n).standard_normal((n, 2)).astype(np.float32) * 0.3
    model = Fake()
    ours, blocks = _ours(model, x, batch)
    assert ours.shape == (4, n, 2)
    assert sum(b.shape[1] for b in blocks) == n              # every frame exactly once, in order
    np.testing.assert_allclose(ours, _reference(model, x), atol=2e-5)


def test_bag_of_models_uses_the_bag_weights():
    bag = demucs_apply.BagOfModels([Fake(seed=1), Fake(seed=2)], weights=[[1, 0, 0.5, 1], [0, 1, 0.5, 3]])
    x = np.random.default_rng(7).standard_normal((777, 2)).astype(np.float32)
    ours, _ = _ours(bag, x, 4)
    np.testing.assert_allclose(ours, _reference(bag, x), atol=2e-5)


def test_oom_halves_the_batch_then_gives_up_at_one():
    model = Fake()
    x = np.random.default_rng(1).standard_normal((900, 2)).astype(np.float32)
    model.oom_until_batch = 2
    eng = se.StemEngine("fake", "cpu", batch=8, loaded=model)
    blocks = []
    eng.separate_array(x, blocks.append)
    assert eng.batch == 2
    np.testing.assert_allclose(np.concatenate(blocks, axis=1), _reference(Fake(), x), atol=2e-5)
    model.oom_until_batch = 0
    with pytest.raises(se.GpuOutOfMemory):
        se.StemEngine("fake", "cpu", batch=4, loaded=model).separate_array(x, lambda b: None)


def test_pcm16_clamps_per_sample():
    pcm, clipped = se.to_pcm16(np.array([[0.5, -1.2], [1.5, 0.0]], dtype=np.float32))
    assert clipped == 2 and pcm.tolist() == [[16384, -32768], [32767, 0]]


def test_separate_file_writes_flac_atomically(tmp_path, monkeypatch):
    import soundfile as sf

    sr = 100
    x = (np.random.default_rng(3).standard_normal((450, 2)) * 0.2).astype(np.float32)

    def fake_decode(src, dest, sr=se.SAMPLE_RATE):
        x.tofile(dest)
        return len(x)

    monkeypatch.setattr(se, "decode", fake_decode)
    model = Fake()
    eng = se.StemEngine("fake", "cpu", batch=2, loaded=model)
    seen = []
    info = eng.separate(tmp_path / "song.mp3", tmp_path / "out", work_dir=str(tmp_path), progress=seen.append)
    assert sorted(info["stems"]) == sorted(Fake.sources) and info["frames"] == 450
    assert seen and seen[-1] == 1.0 and seen == sorted(seen)
    ref = _reference(Fake(), x)
    for k, name in enumerate(Fake.sources):
        y, got_sr = sf.read(info["stems"][name], dtype="float32")
        assert got_sr == sr and y.shape == (450, 2)
        np.testing.assert_allclose(y, np.clip(ref[k], -1, 32767 / 32768), atol=1 / 32768 + 1e-6)
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(f"{n}.flac" for n in Fake.sources)
    assert [p for p in tmp_path.iterdir() if p.name.startswith("stems_")] == []   # work dir cleaned


def test_a_failed_write_leaves_no_partial_stems(tmp_path, monkeypatch):
    x = np.zeros((300, 2), dtype=np.float32)
    monkeypatch.setattr(se, "decode", lambda src, dest, sr=0: (x.tofile(dest), len(x))[1])
    model = Fake()
    model.oom_until_batch = 0                               # every forward fails
    eng = se.StemEngine("fake", "cpu", batch=1, loaded=model)
    with pytest.raises(se.GpuOutOfMemory):
        eng.separate(tmp_path / "s.mp3", tmp_path / "out", work_dir=str(tmp_path))
    assert list((tmp_path / "out").iterdir()) == []
