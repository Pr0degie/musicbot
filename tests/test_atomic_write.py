"""JSON-Dateien werden atomar geschrieben (utils.files.atomic_write_text).

Temporäre Datei im selben Ordner, dann os.replace: stirbt der Prozess oder
scheitert das Schreiben mittendrin, bleibt die alte Datei vollständig stehen –
ein direktes open(path, "w") hätte sie bereits abgeschnitten.
"""
import asyncio
import json
import os
import time
from pathlib import Path

import discord
import pytest

import utils.files as files_mod
from utils.files import atomic_write_text
from utils.i18n import t
from conftest import FakeCtx, FakeDownloader, FakeVoiceClient, build_cog, cleanup_cog, make_fake_source
from cogs.downloader import Downloader
from cogs.music import MusicCommands


def test_schreibt_und_ersetzt(tmp_path):
    ziel = tmp_path / "daten.json"
    atomic_write_text(ziel, '{"a": 1}')
    assert ziel.read_text(encoding="utf-8") == '{"a": 1}'
    atomic_write_text(ziel, '{"a": 2, "ü": "ß"}')
    assert ziel.read_text(encoding="utf-8") == '{"a": 2, "ü": "ß"}'
    assert [p.name for p in tmp_path.iterdir()] == ["daten.json"], "keine Temp-Datei bleibt liegen"


def test_gleiche_bytes_wie_write_text(tmp_path):
    """Dateiformat unverändert – inklusive der Zeilenenden-Übersetzung des Textmodus."""
    text = json.dumps({"x": [1, 2], "titel": "Ärger"}, ensure_ascii=False, indent=2)
    (tmp_path / "alt.json").write_text(text, encoding="utf-8")
    atomic_write_text(tmp_path / "neu.json", text)
    assert (tmp_path / "neu.json").read_bytes() == (tmp_path / "alt.json").read_bytes()


def test_fehler_beim_schreiben_laesst_alte_datei_intakt(tmp_path):
    ziel = tmp_path / "daten.json"
    ziel.write_text('{"alt": true}', encoding="utf-8")
    with pytest.raises(UnicodeEncodeError):
        atomic_write_text(ziel, "vorne ok, dann ein Umlaut: ä", encoding="ascii")
    assert ziel.read_text(encoding="utf-8") == '{"alt": true}'
    assert [p.name for p in tmp_path.iterdir()] == ["daten.json"]


def test_fehler_beim_ersetzen_laesst_alte_datei_intakt(tmp_path, monkeypatch):
    ziel = tmp_path / "daten.json"
    ziel.write_text('{"alt": true}', encoding="utf-8")

    def kaputt(src, dst):
        raise OSError("Platte voll")

    monkeypatch.setattr(files_mod.os, "replace", kaputt)
    with pytest.raises(OSError):
        atomic_write_text(ziel, '{"neu": true}')
    assert ziel.read_text(encoding="utf-8") == '{"alt": true}'
    assert [p.name for p in tmp_path.iterdir()] == ["daten.json"]


def test_gesperrtes_ziel_wird_wiederholt(tmp_path, monkeypatch):
    """Windows: hält kurz jemand die Zieldatei offen (Virenscanner, Leser),
    wirft os.replace PermissionError – ein paar Versuche später klappt es."""
    ziel = tmp_path / "daten.json"
    ziel.write_text("alt", encoding="utf-8")
    echt = os.replace
    versuche = []

    def zickig(src, dst):
        versuche.append(dst)
        if len(versuche) < 3:
            raise PermissionError("in Benutzung")
        return echt(src, dst)

    monkeypatch.setattr(files_mod.os, "replace", zickig)
    monkeypatch.setattr(files_mod.time, "sleep", lambda s: None)
    atomic_write_text(ziel, "neu")
    assert len(versuche) == 3
    assert ziel.read_text(encoding="utf-8") == "neu"
    assert [p.name for p in tmp_path.iterdir()] == ["daten.json"]


def test_dauerhaft_gesperrtes_ziel_faellt_auf_direkten_write_zurueck(tmp_path, monkeypatch):
    """Bleibt das Ersetzen gesperrt, wird wie früher direkt geschrieben – was
    vorher funktionierte, darf durch den atomaren Weg nicht scheitern."""
    ziel = tmp_path / "daten.json"
    ziel.write_text("alt", encoding="utf-8")

    def gesperrt(src, dst):
        raise PermissionError("in Benutzung")

    monkeypatch.setattr(files_mod.os, "replace", gesperrt)
    monkeypatch.setattr(files_mod.time, "sleep", lambda s: None)
    atomic_write_text(ziel, "neu")
    assert ziel.read_text(encoding="utf-8") == "neu"
    assert [p.name for p in tmp_path.iterdir()] == ["daten.json"]


def test_alle_json_dateien_laufen_ueber_os_replace(tmp_path, monkeypatch):
    """play_counts.json, metadata_cache.json, radio_stations.json, playlists/
    und last_queue.json: jeder Schreibweg endet in os.replace."""
    import cogs.music_queue_io as qio
    import cogs.music_radio as radio

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cogs.music.SCORE_FILE", tmp_path / "play_counts.json")
    monkeypatch.setattr(qio, "PLAYLISTS_DIR", tmp_path / "playlists")
    (tmp_path / "playlists").mkdir()
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())
    echt = os.replace
    ersetzt = []

    def merken(src, dst):
        ersetzt.append(Path(dst).name)
        return echt(src, dst)

    monkeypatch.setattr(files_mod.os, "replace", merken)

    async def run():
        mc = build_cog(FakeDownloader(({"title": "A", "duration": 5}, "https://cdn.example/a", "A", 5)))
        # play_counts.json: synchroner Flush und der 30-s-Loop
        mc._play_counts = {"u": {"title": "A", "count": 1}}
        mc._score_dirty = True
        mc._flush_scores_now()
        mc._score_dirty = True
        real_dl = Downloader.__new__(Downloader)
        real_dl._url_cache = {"https://x": {"id": "x", "title": "X"}}
        real_dl._cache_timestamps = {"https://x": time.time()}
        mc.dl, fake_dl = real_dl, mc.dl
        await MusicCommands._persist_flush_loop.coro(mc)
        # metadata_cache.json: synchron und gedebounct
        real_dl._save_cache()
        real_dl._cache_dirty = True
        await real_dl.flush_cache()
        mc.dl = fake_dl
        # playlists/ und radio_stations.json
        qio._write_playlist(tmp_path / "playlists" / "mix.json", [["u", "A"]])
        radio._write_stations({"x": {"name": "X", "url": "https://x"}})
        # last_queue.json: nach jedem Track-Ende
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc.queue.append(("https://www.youtube.com/watch?v=aaaaaaaaaaa", "A"))
        await mc.play_next(ctx)
        vc.end_track()
        for _ in range(100):
            if "last_queue.json" in ersetzt:
                break
            await asyncio.sleep(0.01)
        cleanup_cog(mc)

    asyncio.run(run())

    assert ersetzt.count("play_counts.json") == 2
    assert ersetzt.count("metadata_cache.json") == 2
    assert ersetzt.count("mix.json") == 1
    assert ersetzt.count("radio_stations.json") == 1
    assert ersetzt.count("last_queue.json") == 1
    for name in ("play_counts.json", "metadata_cache.json", "radio_stations.json", "last_queue.json"):
        json.loads((tmp_path / name).read_text(encoding="utf-8"))
    assert json.loads((tmp_path / "playlists" / "mix.json").read_text(encoding="utf-8")) == [["u", "A"]]


# ---------------------------------------------------------------------------
# radio_stations.json kaputt → !radio crasht nicht dauerhaft
# ---------------------------------------------------------------------------

def _radio(ctx, eingabe=None):
    async def run():
        mc = build_cog()
        await MusicCommands.radio_play.callback(mc, ctx, eingabe=eingabe)

    asyncio.run(run())


def test_kaputte_stationsdatei_crasht_radio_nicht(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("radio_stations.json").write_text('{"eins": {"name": "Eins", "url": "https://e"},', encoding="utf-8")
    ctx = FakeCtx()

    _radio(ctx)        # darf nicht raisen

    assert ctx.texts == [t("error.no_stations")]
    # Die kaputte Datei bleibt als Sicherung erhalten, bevor ein Speichern sie überschreibt.
    assert Path("radio_stations.json.broken").read_text(encoding="utf-8").startswith('{"eins"')


def test_stationsdatei_mit_falschem_inhalt_crasht_radio_nicht(tmp_path, monkeypatch):
    """Gültiges JSON, aber keine Senderliste bzw. einzelne unbrauchbare Einträge."""
    monkeypatch.chdir(tmp_path)
    Path("radio_stations.json").write_text(json.dumps(["kein", "dict"]), encoding="utf-8")
    ctx = FakeCtx()
    _radio(ctx)
    assert ctx.texts == [t("error.no_stations")]

    Path("radio_stations.json").write_text(json.dumps({
        "gut": {"name": "Guter Sender", "url": "https://gut"},
        "ohne_url": {"name": "Halb"},
        "quatsch": "text",
    }), encoding="utf-8")
    ctx = FakeCtx()
    _radio(ctx)
    assert len(ctx.texts) == 1
    assert "Guter Sender" in ctx.texts[0] and "Halb" not in ctx.texts[0]


def test_intakte_stationsdatei_unveraendert(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("radio_stations.json").write_text(json.dumps({
        "a": {"name": "Sender A", "url": "https://a"},
        "b": {"name": "Sender B", "url": "https://b", "extra": 1},
    }), encoding="utf-8")
    ctx = FakeCtx()
    _radio(ctx)
    assert "Sender A" in ctx.texts[0] and "Sender B" in ctx.texts[0]
    assert not Path("radio_stations.json.broken").exists()
