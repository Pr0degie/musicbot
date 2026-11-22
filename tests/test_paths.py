"""Laufzeitdaten liegen in data/ und logs/ (utils/paths.py).

migrate_legacy_layout() zieht beim Start einmalig um, was aus der Zeit davor
noch direkt im Arbeitsverzeichnis liegt – ohne je etwas zu überschreiben.
"""
import os

import utils.paths as paths
from utils.files import atomic_write_text
from utils.paths import migrate_legacy_layout


def _altes_layout(base):
    (base / "play_counts.json").write_text('{"u": 1}', encoding="utf-8")
    (base / "metadata_cache.json").write_text("{}", encoding="utf-8")
    (base / "last_queue.json").write_text("[]", encoding="utf-8")
    (base / "playlists").mkdir()
    (base / "playlists" / "mix.json").write_text("[]", encoding="utf-8")
    (base / "downloads").mkdir()
    (base / "downloads" / "Song [abc].webm").write_bytes(b"audio")
    (base / "bot.log").write_text("lauf 3", encoding="utf-8")
    (base / "bot.log.1").write_text("lauf 2", encoding="utf-8")
    (base / "bot.log.10").write_text("lauf 1", encoding="utf-8")


def test_altes_layout_zieht_nach_data_und_logs(tmp_path):
    _altes_layout(tmp_path)
    # Gehört nicht zum Bot-Zustand und bleibt, wo es ist.
    (tmp_path / "radio_stations.json").write_text("{}", encoding="utf-8")
    (tmp_path / "bot.log.bak").write_text("fremd", encoding="utf-8")
    (tmp_path / ".env").write_text("DISCORD_TOKEN=x", encoding="utf-8")

    moved, left = migrate_legacy_layout(tmp_path)

    assert left == []
    assert sorted(moved) == sorted([
        "play_counts.json", "metadata_cache.json", "last_queue.json", "playlists", "downloads",
        "bot.log", "bot.log.1", "bot.log.10",
    ])
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env", "bot.log.bak", "data", "logs", "radio_stations.json"]
    assert (tmp_path / "data" / "play_counts.json").read_text(encoding="utf-8") == '{"u": 1}'
    assert (tmp_path / "data" / "playlists" / "mix.json").exists()
    assert (tmp_path / "data" / "downloads" / "Song [abc].webm").read_bytes() == b"audio"
    assert (tmp_path / "logs" / "bot.log").read_text(encoding="utf-8") == "lauf 3"
    assert (tmp_path / "logs" / "bot.log.10").read_text(encoding="utf-8") == "lauf 1"


def test_zweiter_lauf_findet_nichts_mehr(tmp_path):
    _altes_layout(tmp_path)
    migrate_legacy_layout(tmp_path)
    assert migrate_legacy_layout(tmp_path) == ([], [])
    assert migrate_legacy_layout(tmp_path / "gibt-es-nicht") == ([], [])


def test_vorhandenes_ziel_wird_nie_ueberschrieben(tmp_path):
    """Neuerer Stand in data/ gewinnt; die alte Datei bleibt liegen und wird gemeldet."""
    (tmp_path / "play_counts.json").write_text("alt", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "play_counts.json").write_text("neu", encoding="utf-8")

    moved, left = migrate_legacy_layout(tmp_path)

    assert (moved, left) == ([], ["play_counts.json"])
    assert (tmp_path / "data" / "play_counts.json").read_text(encoding="utf-8") == "neu"
    assert (tmp_path / "play_counts.json").read_text(encoding="utf-8") == "alt"


def test_ordner_wird_in_vorhandenen_zielordner_gemischt(tmp_path):
    """data/downloads existiert schon (ein Testlauf oder ein früherer, halber
    Umzug hat es angelegt): Dateien wandern einzeln, Gleichnamiges bleibt."""
    alt = tmp_path / "downloads"
    alt.mkdir()
    (alt / "a.webm").write_bytes(b"a-alt")
    (alt / "b.webm").write_bytes(b"b-alt")
    neu = tmp_path / "data" / "downloads"
    neu.mkdir(parents=True)
    (neu / "b.webm").write_bytes(b"b-neu")

    moved, left = migrate_legacy_layout(tmp_path)

    assert (moved, left) == ([], ["downloads"])
    assert (neu / "a.webm").read_bytes() == b"a-alt"
    assert (neu / "b.webm").read_bytes() == b"b-neu"
    assert [p.name for p in alt.iterdir()] == ["b.webm"]

    (alt / "b.webm").unlink()
    assert migrate_legacy_layout(tmp_path) == (["downloads"], [])
    assert not alt.exists()


def test_gesperrter_ordner_wird_dateiweise_umgezogen(tmp_path, monkeypatch):
    """Windows: ein Ordner mit einer offenen Datei lässt sich nicht umbenennen."""
    alt = tmp_path / "downloads"
    alt.mkdir()
    (alt / "frei.webm").write_bytes(b"x")
    (alt / "offen.webm").write_bytes(b"y")
    echt = os.replace

    def replace(src, dst):
        if os.path.basename(src) in ("downloads", "offen.webm"):
            raise PermissionError("in Benutzung")
        return echt(src, dst)

    monkeypatch.setattr(paths.os, "replace", replace)

    moved, left = migrate_legacy_layout(tmp_path)

    assert (moved, left) == ([], ["downloads"])
    assert (tmp_path / "data" / "downloads" / "frei.webm").exists()
    assert [p.name for p in alt.iterdir()] == ["offen.webm"]


def test_atomic_write_legt_den_zielordner_an(tmp_path):
    ziel = tmp_path / "data" / "last_queue.json"
    atomic_write_text(ziel, "[]")
    assert ziel.read_text(encoding="utf-8") == "[]"
