"""Genre / era labels persist across a server restart and travel in the knowledge export
(app/music_brain/analysis/genre_labels.py). Session 2026-09-30_102327: labels lived in memory only,
so after a restart the library fallback had no labelled Punjabi song."""
import json

from app.music_brain.analysis import genre_labels as gl
from app.music_brain.matching import knowledge as kn


def test_save_load_round_trip(tmp_path):
    p = tmp_path / "genre_labels.json"
    assert gl.save({"lover": "punjabi pop", "glue": "uk bass"}, {"lover": "2020s"}, p)
    g, e = gl.load(p)
    assert g == {"lover": "punjabi pop", "glue": "uk bass"} and e == {"lover": "2020s"}
    assert json.loads(p.read_text())["version"] == gl.VERSION
    assert not list(tmp_path.glob("*.tmp"))                               # atomic: no tmp left


def test_load_missing_or_bad_file_is_empty(tmp_path):
    assert gl.load(tmp_path / "none.json") == ({}, {})
    (tmp_path / "bad.json").write_text("{not json")
    assert gl.load(tmp_path / "bad.json") == ({}, {})


def test_newest_labels_win_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(gl, "MAX_LABELS", 2)
    p = tmp_path / "l.json"
    gl.save({"a": "x", "b": "y", "c": "z"}, {}, p)
    assert gl.load(p)[0] == {"b": "y", "c": "z"}


def test_labels_survive_a_simulated_restart(tmp_path, monkeypatch):
    import app.ui.server as srv
    monkeypatch.setattr(srv, "_suggested_genres", {})
    monkeypatch.setattr(srv, "_suggested_eras", {})
    srv._set_label(srv._suggested_genres, srv._genre_key("Lover"), "punjabi pop")
    srv._set_label(srv._suggested_eras, srv._genre_key("Lover"), "2020s")
    srv._save_labels()
    # restart: empty memory, then the startup load
    monkeypatch.setattr(srv, "_suggested_genres", {})
    monkeypatch.setattr(srv, "_suggested_eras", {})
    srv._load_labels()
    assert srv._suggested_genres[srv._genre_key("Lover")] == "punjabi pop"
    assert srv._suggested_eras[srv._genre_key("Lover")] == "2020s"


def test_backfill_from_the_knowledge_export_never_overwrites(tmp_path, monkeypatch):
    import app.ui.server as srv
    monkeypatch.setattr(srv, "_suggested_genres", {gl.name_key("Shubh - Cheques"): "local label"})
    monkeypatch.setattr(srv, "_suggested_eras", {})
    (kn.KNOWLEDGE_DIR / kn.NAMES).write_text(json.dumps({"t1": "Diljit Dosanjh - Lover (Official Video)",
                                                         "t2": "Shubh - Cheques"}))
    (kn.KNOWLEDGE_DIR / kn.LABELS).write_text(json.dumps({"t1": {"genre": "punjabi pop", "era": "2020s"},
                                                          "t2": {"genre": "exported"}}))
    assert srv._load_labels() == 1
    assert srv._suggested_genres[gl.name_key("Diljit Dosanjh - Lover")] == "punjabi pop"
    assert srv._suggested_genres[gl.name_key("Shubh - Cheques")] == "local label"


def test_export_includes_labels_per_name(tmp_path):
    cache = tmp_path / "home"
    (cache / "uploads").mkdir(parents=True)
    names = {"a" * 16: "Diljit Dosanjh - Lover", "b" * 16: "Argy - WIND"}
    (cache / "uploads" / "_names.json").write_text(json.dumps(names))
    for t in names:
        (cache / "uploads" / f"{t}.mp3").write_bytes(b"x")
    macros = cache / "macros"
    macros.mkdir()
    gl.save({gl.name_key("Diljit Dosanjh - Lover"): "punjabi pop"}, {gl.name_key("Diljit Dosanjh - Lover"): "2020s"},
            gl.path(cache))
    # export only names songs the atlas / macros use: write a tiny macro over both
    from app.music_brain.atlas import macros as mc
    mc.write_seed({"name": "m1", "source": "atlas:chain",
                   "steps": [{"a": "a" * 16, "b": "b" * 16, "recipe": "Bass Swap"}]}, cache)
    out = tmp_path / "knowledge"
    kn.export(cache, out)
    labels = json.loads((out / kn.LABELS).read_text())
    assert labels == {"a" * 16: {"era": "2020s", "genre": "punjabi pop"}}
    assert not kn.privacy_hits(labels)
