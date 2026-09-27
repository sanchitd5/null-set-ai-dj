"""Static RAG corpora: dj_rag (autopilot DJ RULES) and live_ear_rag (seam calls)."""
import json

from app.music_brain import dj_rag, live_ear_rag
from app.ui import live_ear


def test_dj_corpus_file_is_committed_and_broad():
    chunks = json.loads(dj_rag.CORPUS_PATH.read_text(encoding="utf-8"))["chunks"]
    assert len(chunks) > 100
    tags = {t for c in chunks for t in c["tags"]}
    for t in ("camelot", "phrase", "bpm_gap", "genre_bridge", "eq", "energy", "recipe",
              "warmup", "build", "peak", "cooldown"):
        assert t in tags
    from app.music_brain.knowledge_parser import KnowledgeParser
    assert sum("recipe" in c["tags"] for c in chunks) == len(KnowledgeParser().load().get_all())
    assert all(len(c["text"]) <= dj_rag.MAX_CHUNK_CHARS + 3 for c in chunks)


def test_dj_corpus_matches_build():
    # the committed JSON must be what the builder produces from ./DJ/ today
    committed = json.loads(dj_rag.CORPUS_PATH.read_text(encoding="utf-8"))["chunks"]
    assert [c["text"] for c in committed] == [c["text"] for c in dj_rag.build_corpus()]


def test_dj_retrieve_slots():
    base = dj_rag.retrieve()
    assert base and base[0].startswith("CAMELOT") and any("PHRAS" in t for t in base)
    assert not any("BPM GAPS" in t for t in base)
    gap = dj_rag.retrieve(genre="drum and bass", tempo_gap_pct=30, set_position=0.8)
    assert any("BRIDGE" in t or "BPM" in t for t in gap)
    assert any("DRUM & BASS" in t for t in gap)
    assert dj_rag.retrieve(n=0) == []
    assert dj_rag.retrieve(genre="tech house") == dj_rag.retrieve(genre="tech house")


def test_dj_retrieve_missing_corpus(monkeypatch, tmp_path):
    monkeypatch.setattr(dj_rag, "CORPUS_PATH", tmp_path / "nope.json")
    dj_rag._corpus.cache_clear()
    try:
        assert dj_rag.retrieve(genre="pop") == []
    finally:
        monkeypatch.undo()
        dj_rag._corpus.cache_clear()


def test_live_ear_examples_cover_every_flag():
    tags = {t for e in live_ear_rag.EXAMPLES for t in e["tags"]}
    for flag in ("seam_phase", "loop_length", "seam_click", "clipping", "low_clash", "fatigue"):
        assert flag in tags
        assert sum(flag in e["tags"] for e in live_ear_rag.EXAMPLES) >= 3
    moves = set(live_ear.ACTIONS)
    for e in live_ear_rag.EXAMPLES:
        assert any(f", {m}" in e["text"] for m in moves), e["text"]


def test_live_ear_retrieve():
    assert "move_loop" in live_ear_rag.retrieve(["seam_phase"])[0]
    assert "keep" in live_ear_rag.retrieve([])[0]
    assert "restore" in live_ear_rag.retrieve([], washed=True)[0]
    assert "moved" in live_ear_rag.retrieve(["fatigue"], moved=True)[0].lower()
    assert len(live_ear_rag.retrieve(["clipping", "low_clash"], n=2)) == 2
