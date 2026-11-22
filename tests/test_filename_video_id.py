"""Die Video-ID steckt im Dateinamen: gleicher Titel ≠ gleiche Cache-Datei.

Vorher war outtmpl "%(title)s.%(ext)s" – zwei verschiedene Videos mit gleichem
Titel teilten sich eine Datei in downloads/, das zweite spielte dann das
Audio des ersten. Alles, was vom Dateinamen abhängt, muss das neue Schema
"<Titel> [<id>].<ext>" kennen: persistierter Metadaten-Cache (braucht "id"),
Titel-Stem-Schutz im Cleanup, .inprogress-Sidecars und ihr Startup-Purge.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import cogs.downloader_cleanup as dlmod
from cogs.downloader import Downloader, PERSISTED_CACHE_FIELDS

URL_A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
URL_B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"


def _info(video_id, title="Intro"):
    return {"id": video_id, "title": title, "ext": "webm", "duration": 100,
            "webpage_url": f"https://www.youtube.com/watch?v={video_id}"}


def _real_dl(monkeypatch, tmp_path):
    monkeypatch.setattr("cogs.downloader_ydl.YDL_COOKIES_FILE", "")
    monkeypatch.setattr("cogs.downloader_ydl.YDL_BROWSER", "")
    monkeypatch.chdir(tmp_path)
    return Downloader("webm")   # echte yt_dlp-Instanzen, offline


def test_gleicher_titel_verschiedene_videos_verschiedene_dateien(monkeypatch, tmp_path):
    dl = _real_dl(monkeypatch, tmp_path)
    a = Path(dl.ydl.prepare_filename(_info("aaaaaaaaaaa")))
    b = Path(dl.ydl.prepare_filename(_info("bbbbbbbbbbb")))

    assert a != b, "zwei Videos mit gleichem Titel teilen sich eine Cache-Datei"
    assert a.name == "Intro [aaaaaaaaaaa].webm"
    assert b.name == "Intro [bbbbbbbbbbb].webm"


def test_progressive_instanz_schreibt_in_denselben_pfad(monkeypatch, tmp_path):
    """Der progressive Download nutzt eine eigene yt_dlp-Instanz – ihr Ziel muss
    exakt der Pfad sein, den resolve_track über self.ydl erwartet."""
    dl = _real_dl(monkeypatch, tmp_path)
    info = _info("aaaaaaaaaaa", title="Song | mit [Klammern] & Emoji 🎵")
    assert dl._make_progressive_ydl().prepare_filename(info) == dl.ydl.prepare_filename(info)


def test_id_wird_persistiert():
    assert "id" in PERSISTED_CACHE_FIELDS


def test_start_verwirft_cache_eintraege_ohne_id(monkeypatch, tmp_path):
    """metadata_cache.json aus der Zeit vor der ID im Dateinamen: ohne "id"
    ergäbe prepare_filename "<Titel> [NA].<ext>" – solche Einträge fliegen beim
    Start raus (Cache-Miss, frische Extraktion)."""
    monkeypatch.setattr("cogs.downloader_ydl.YDL_COOKIES_FILE", "")
    monkeypatch.setattr("cogs.downloader_ydl.YDL_BROWSER", "")
    monkeypatch.chdir(tmp_path)
    now = time.time()
    alt = {k: v for k, v in _info("aaaaaaaaaaa").items() if k != "id"}
    Path("metadata_cache.json").write_text(json.dumps({
        URL_A: {**alt, "_ts": now},
        URL_B: {**_info("bbbbbbbbbbb"), "_ts": now},
    }), encoding="utf-8")

    dl = Downloader("webm")

    assert list(dl._url_cache) == [URL_B]
    assert list(dl._cache_timestamps) == [URL_B]


def _make_file(directory, name, size, age_seconds):
    p = directory / name
    p.write_bytes(b"\0" * size)
    mtime = time.time() - age_seconds
    os.utime(p, (mtime, mtime))
    return p


def test_titel_stem_schutz_kennt_das_id_suffix(monkeypatch, tmp_path):
    """Queue-Song ohne Cache-Eintrag: der Titel schützt auch "<Titel> [<id>].webm"."""
    monkeypatch.setattr(dlmod, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 1)
    q_file = _make_file(tmp_path, "Nur Titel [live] bekannt [aaaaaaaaaaa].webm", 512 * 1024, age_seconds=5000)
    frei = _make_file(tmp_path, "frei [bbbbbbbbbbb].webm", 512 * 1024, age_seconds=3000)
    _make_file(tmp_path, "neu [ccccccccccc].webm", 512 * 1024, age_seconds=10)
    dl = Downloader.__new__(Downloader)
    dl.audio_format = "webm"
    dl._url_cache = {}
    dl._cache_timestamps = {}
    dl._pending_resolves = {}
    dl.protected_provider = lambda: (["https://q"], ["Nur Titel [live] bekannt"])

    asyncio.run(dl.cleanup_downloads())

    assert q_file.exists()
    assert not frei.exists()


def test_purge_raeumt_sidecar_und_datei_mit_id_im_namen(monkeypatch, tmp_path):
    monkeypatch.setattr(dlmod, "DOWNLOAD_DIR", tmp_path)
    media = tmp_path / "Song [mit] Klammern [aaaaaaaaaaa].webm"
    media.write_bytes(b"x")
    sidecar = Downloader._sidecar_for(media)
    sidecar.touch()
    fertig = tmp_path / "Fertig [bbbbbbbbbbb].webm"
    fertig.write_bytes(b"x")
    dl = Downloader.__new__(Downloader)

    dl._purge_stale_progressive()

    assert sidecar.name == "Song [mit] Klammern [aaaaaaaaaaa].webm.inprogress"
    assert not media.exists() and not sidecar.exists()
    assert fertig.exists()
