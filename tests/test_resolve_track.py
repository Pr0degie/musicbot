"""Characterization-Tests: resolve_track-Verzweigung (Stream vs. Download).

Dokumentiert die drei Rückgabe-Formen:
  - str  (CDN-Stream-URL): Dauer > 20 min ODER Datei fehlt lokal
  - Path (lokale Datei):   Datei existiert (auch nach erfolgreichem Prefetch)
"""

import asyncio
from pathlib import Path

from cogs.downloader import STREAM_THRESHOLD_SECONDS, Downloader

URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


class FakeYdl:
    def __init__(self, info, filename, fail_extract=False):
        self._info = info
        self._filename = filename
        self.extract_calls = []
        self.fail_extract = fail_extract

    def extract_info(self, url, download=False):
        if self.fail_extract:
            raise AssertionError("extract_info darf bei Cache-Hit nicht laufen")
        self.extract_calls.append(url)
        return self._info

    def prepare_filename(self, info):
        return str(self._filename)


def make_dl(info, filename, cached=True, fail_extract=False):
    dl = Downloader.__new__(Downloader)
    dl.audio_format = "webm"
    dl._url_cache = {URL: info} if cached else {}
    dl._cache_timestamps = {}
    dl._pending_resolves = {}
    dl.ydl = FakeYdl(info, filename, fail_extract=fail_extract)
    return dl


def resolve(dl, prefetch_task=None):
    async def run():
        result = await dl.resolve_track(URL, "Fallback-Titel", prefetch_task=prefetch_task)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return result

    return asyncio.run(run())


def test_long_track_streams_never_downloads(tmp_path):
    info = {"title": "Langer Mix", "duration": STREAM_THRESHOLD_SECONDS + 1,
            "url": "https://cdn.example/stream"}
    dl = make_dl(info, tmp_path / "egal.webm")

    _, filename, title, duration = resolve(dl)

    assert filename == "https://cdn.example/stream"
    assert isinstance(filename, str)
    assert title == "Langer Mix"
    assert duration == STREAM_THRESHOLD_SECONDS + 1


def test_threshold_is_strictly_greater_than(tmp_path):
    """IST: Genau 20:00 min gilt noch als Download-Kandidat (>, nicht >=)."""
    local = tmp_path / "song.webm"
    local.write_bytes(b"x")
    info = {"title": "Genau 20", "duration": STREAM_THRESHOLD_SECONDS,
            "url": "https://cdn.example/stream"}
    dl = make_dl(info, local)

    _, filename, _, _ = resolve(dl)

    assert isinstance(filename, Path)
    assert filename == local


def test_existing_file_returns_path(tmp_path):
    local = tmp_path / "song.webm"
    local.write_bytes(b"x")
    info = {"title": "Kurz", "duration": 180, "url": "https://cdn.example/stream"}
    dl = make_dl(info, local)

    _, filename, _, _ = resolve(dl)

    assert isinstance(filename, Path)
    assert filename == local


def test_missing_file_without_prefetch_streams(tmp_path):
    info = {"title": "Kurz", "duration": 180, "url": "https://cdn.example/stream"}
    dl = make_dl(info, tmp_path / "fehlt.webm")

    _, filename, _, _ = resolve(dl)

    assert filename == "https://cdn.example/stream"
    assert isinstance(filename, str)


def test_missing_file_and_missing_cdn_url_falls_back_to_original_url(tmp_path):
    """IST: Fehlt info['url'] (z.B. flacher Cache-Eintrag), wird die
    Original-URL als 'Stream-URL' zurückgegeben."""
    info = {"title": "Kurz", "duration": 180}
    dl = make_dl(info, tmp_path / "fehlt.webm")

    _, filename, _, _ = resolve(dl)

    assert filename == URL


def test_running_prefetch_is_awaited_then_file_used(tmp_path):
    """Prefetch lädt die Datei fertig → resolve_track wartet und nutzt sie."""
    local = tmp_path / "song.webm"
    info = {"title": "Kurz", "duration": 180, "url": "https://cdn.example/stream"}
    dl = make_dl(info, local)

    async def run():
        async def prefetch():
            await asyncio.sleep(0)
            local.write_bytes(b"x")

        task = asyncio.create_task(prefetch())
        return await dl.resolve_track(URL, "T", prefetch_task=task)

    _, filename, _, _ = asyncio.run(run())

    assert isinstance(filename, Path)
    assert filename == local


def test_finished_prefetch_without_file_streams(tmp_path):
    """Prefetch fertig, Datei trotzdem nicht da (Download fehlgeschlagen) → Stream."""
    info = {"title": "Kurz", "duration": 180, "url": "https://cdn.example/stream"}
    dl = make_dl(info, tmp_path / "fehlt.webm")

    async def run():
        async def prefetch():
            pass  # tut nichts, erzeugt keine Datei

        task = asyncio.create_task(prefetch())
        await asyncio.sleep(0)  # Task abschließen lassen
        return await dl.resolve_track(URL, "T", prefetch_task=task)

    _, filename, _, _ = asyncio.run(run())

    assert filename == "https://cdn.example/stream"


def test_cache_hit_skips_extract_info(tmp_path):
    local = tmp_path / "song.webm"
    local.write_bytes(b"x")
    info = {"title": "Gecacht", "duration": 180}
    dl = make_dl(info, local, cached=True, fail_extract=True)

    _, filename, title, _ = resolve(dl)

    assert title == "Gecacht"
    assert filename == local


def test_uncached_url_extracts_and_caches(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # _save_cache schreibt metadata_cache.json
    local = tmp_path / "song.webm"
    local.write_bytes(b"x")
    info = {"title": "Frisch", "duration": 180}
    dl = make_dl(info, local, cached=False)

    _, filename, title, _ = resolve(dl)

    assert dl.ydl.extract_calls == [URL]
    assert dl._url_cache[URL] is info
    assert title == "Frisch"


def test_title_and_duration_fall_back_when_missing(tmp_path):
    """IST: Ohne title/duration im Info-Dict: 'Unbekannter Titel', Dauer 0."""
    local = tmp_path / "song.webm"
    local.write_bytes(b"x")
    dl = make_dl({}, local)

    _, _, title, duration = resolve(dl)

    assert title == "Unbekannter Titel"
    assert duration == 0
