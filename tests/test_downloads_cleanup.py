"""Tests für den downloads/-Cleanup (DOWNLOADS_MAX_MB, Default 0 = aus).

Regeln: Nach jedem Download werden die ältesten Dateien (mtime) gelöscht, bis
das Limit eingehalten ist – niemals Dateien zu Songs in Queue/current_track
(Schutz über prepare_filename UND Titel-Stem) und niemals die aktive
FFmpeg-Quelle (last_resolved_file). Flag = 0 → exakt heutiges Verhalten.
"""

import asyncio
import os
import time

import cogs.downloader as dlmod
from cogs.downloader import Downloader
from cogs.music import MusicCommands
from conftest import build_cog

MB = 1024 * 1024


class MapYdl:
    """prepare_filename über ein Titel→Pfad-Mapping, wie das echte outtmpl."""

    def __init__(self, mapping):
        self.mapping = mapping

    def prepare_filename(self, info):
        return self.mapping[info["title"]]


def make_dl(cache=None, mapping=None):
    dl = Downloader.__new__(Downloader)
    dl.audio_format = "webm"
    dl._url_cache = cache or {}
    dl._cache_timestamps = {}
    dl._pending_resolves = {}
    dl.ydl = MapYdl(mapping or {})
    return dl


def make_file(directory, name, size, age_seconds):
    p = directory / name
    p.write_bytes(b"\0" * size)
    mtime = time.time() - age_seconds
    os.utime(p, (mtime, mtime))
    return p


def run_cleanup(dl, **kwargs):
    asyncio.run(dl.cleanup_downloads(**kwargs))


def test_flag_zero_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(dlmod, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    files = [make_file(tmp_path, f"s{i}.webm", MB, age_seconds=i * 100) for i in range(3)]
    dl = make_dl()
    dl.protected_provider = lambda: ([], [])

    run_cleanup(dl)

    assert all(p.exists() for p in files), "Flag 0 = aus: heutiges Verhalten, nichts löschen"


def test_under_limit_deletes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(dlmod, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 10)
    files = [make_file(tmp_path, f"s{i}.webm", MB, age_seconds=i * 100) for i in range(3)]
    dl = make_dl()
    dl.protected_provider = lambda: ([], [])

    run_cleanup(dl)

    assert all(p.exists() for p in files)


def test_oldest_files_deleted_first(tmp_path, monkeypatch):
    monkeypatch.setattr(dlmod, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 1)
    new = make_file(tmp_path, "neu.webm", 512 * 1024, age_seconds=10)
    mid = make_file(tmp_path, "mittel.webm", 512 * 1024, age_seconds=1000)
    old = make_file(tmp_path, "alt.webm", 512 * 1024, age_seconds=5000)
    dl = make_dl()
    dl.protected_provider = lambda: ([], [])

    run_cleanup(dl)

    assert not old.exists(), "älteste Datei zuerst"
    assert mid.exists() and new.exists(), "Löschen stoppt sobald Limit eingehalten"


def test_queue_songs_survive_via_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(dlmod, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 1)
    q_file = make_file(tmp_path, "queue-song.webm", 512 * 1024, age_seconds=5000)
    frei = make_file(tmp_path, "frei.webm", 512 * 1024, age_seconds=3000)
    new = make_file(tmp_path, "neu.webm", 512 * 1024, age_seconds=10)
    dl = make_dl(
        cache={"https://q": {"title": "queue-song", "ext": "webm"}},
        mapping={"queue-song": str(q_file)},
    )
    dl.protected_provider = lambda: (["https://q"], ["queue-song"])

    run_cleanup(dl)

    assert q_file.exists(), "Queue-Song ist tabu, obwohl er die älteste Datei ist"
    assert not frei.exists(), "stattdessen fällt die älteste ungeschützte Datei"
    assert new.exists()


def test_queue_song_survives_via_title_stem_without_cache_entry(tmp_path, monkeypatch):
    """Cache-Eintrag abgelaufen/fehlt → der Titel-Stem schützt trotzdem."""
    monkeypatch.setattr(dlmod, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 1)
    q_file = make_file(tmp_path, "Nur Titel bekannt.webm", 512 * 1024, age_seconds=5000)
    frei = make_file(tmp_path, "frei.webm", 512 * 1024, age_seconds=3000)
    make_file(tmp_path, "neu.webm", 512 * 1024, age_seconds=10)
    dl = make_dl()  # kein Cache-Eintrag
    dl.protected_provider = lambda: (["https://q"], ["Nur Titel bekannt"])

    run_cleanup(dl)

    assert q_file.exists()
    assert not frei.exists()


def test_last_resolved_file_is_protected(tmp_path, monkeypatch):
    """Die aktive FFmpeg-Quelle ist tabu, auch wenn current_track schon weiter ist."""
    monkeypatch.setattr(dlmod, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 1)
    playing = make_file(tmp_path, "läuft-gerade.webm", 512 * 1024, age_seconds=5000)
    frei = make_file(tmp_path, "frei.webm", 512 * 1024, age_seconds=3000)
    make_file(tmp_path, "neu.webm", 512 * 1024, age_seconds=10)
    dl = make_dl()
    dl.protected_provider = lambda: ([], [])
    dl.last_resolved_file = playing

    run_cleanup(dl)

    assert playing.exists()
    assert not frei.exists()


def test_progressive_in_flight_file_is_protected(tmp_path, monkeypatch):
    """Wachsende Datei eines laufenden progressiven Downloads (samt Sidecar) ist
    tabu – auch wenn last_resolved_file schon auf den nächsten Track zeigt."""
    monkeypatch.setattr(dlmod, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 1)
    growing = make_file(tmp_path, "wächst-noch.webm", 512 * 1024, age_seconds=5000)
    sidecar = make_file(tmp_path, "wächst-noch.webm" + dlmod.PROGRESSIVE_SIDECAR_SUFFIX, 1, age_seconds=5000)
    frei = make_file(tmp_path, "frei.webm", 512 * 1024, age_seconds=3000)
    make_file(tmp_path, "neu.webm", 512 * 1024, age_seconds=10)
    dl = make_dl()
    dl.protected_provider = lambda: ([], [])
    dl._progressive_files = {"https://x/1": growing}
    dl.last_resolved_file = None   # zeigt schon woanders hin

    run_cleanup(dl)

    assert growing.exists()
    assert sidecar.exists()
    assert not frei.exists()


def test_extra_protected_survives(tmp_path, monkeypatch):
    """Frisch geladener Autoplay-Song (noch nicht in der Queue) ist tabu."""
    monkeypatch.setattr(dlmod, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 1)
    fresh = make_file(tmp_path, "autoplay.webm", 512 * 1024, age_seconds=5000)
    frei = make_file(tmp_path, "frei.webm", 512 * 1024, age_seconds=3000)
    make_file(tmp_path, "neu.webm", 512 * 1024, age_seconds=10)
    dl = make_dl()
    dl.protected_provider = lambda: ([], [])

    run_cleanup(dl, extra_protected=fresh)

    assert fresh.exists()
    assert not frei.exists()


def test_without_provider_nothing_is_deleted(tmp_path, monkeypatch):
    """Kein protected_provider (Klassen-Default None) → lieber nichts löschen."""
    monkeypatch.setattr(dlmod, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 1)
    files = [make_file(tmp_path, f"s{i}.webm", MB, age_seconds=i * 100) for i in range(3)]
    dl = make_dl()

    run_cleanup(dl)

    assert all(p.exists() for p in files)


def test_protected_tracks_collects_queue_and_current(tmp_path):
    async def run():
        mc = build_cog()
        mc.queue.append(("https://q1", "Song Eins"))
        mc.queue.append(("https://q2", "Song Zwei"))
        mc.current_track = ("https://now", "Läuft", 180)
        return MusicCommands._protected_tracks(mc)

    urls, titles = asyncio.run(run())
    assert urls == ["https://q1", "https://q2", "https://now"]
    assert titles == ["Song Eins", "Song Zwei", "Läuft"]
