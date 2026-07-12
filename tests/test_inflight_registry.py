"""Tests für die In-Flight-Registry: pro URL maximal ein schreibender Download.

Kollisionswege, die die Registry schließen muss (alle mit echten
asyncio-Tasks und gemockten ydl-Instanzen, Callcounts als Doppel-Nachweis):
  a) resolve_track während lebendem Progressiv-Task → adoptiert, kein
     zweiter Download-Start
  b) force_download bei lebendem Task → awaitet statt parallel zu laden
  c) Übergang zum Autoplay-Song, dessen Prefetch noch lädt → kein Doppel
  d) _start_progressive zweimal für dieselbe URL → derselbe Task
  e) Leiche (Registry leer, is_incomplete=True) → heutiges Aufräumverhalten
"""

import asyncio
from pathlib import Path

import cogs.downloader as dlmod
from cogs.downloader import PROGRESSIVE_MIN_BYTES, Downloader

URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


class FakeYdl:
    """Haupt-Instanz-Ersatz: Metadaten + Zielpfad; download() zählt Aufrufe."""

    def __init__(self, info, filename):
        self._info = info
        self._filename = filename
        self.download_calls = []

    def extract_info(self, url, download=False):
        return self._info

    def prepare_filename(self, info):
        return str(self._filename)

    def download(self, urls):
        self.download_calls.append(urls)
        Path(self._filename).write_bytes(b"\0" * 4096)


class CountingWriter:
    """Progressiv-Instanz-Ersatz: download() schreibt die Zieldatei und zählt."""

    def __init__(self, target, write_bytes, linger=0.0):
        self.target = Path(target)
        self.write_bytes = write_bytes
        self.linger = linger
        self.calls = []

    def download(self, urls):
        import time
        self.calls.append(urls)
        self.target.write_bytes(b"\0" * self.write_bytes)
        if self.linger:
            time.sleep(self.linger)


def make_dl(info, filename, writer=None):
    dl = Downloader.__new__(Downloader)
    dl.audio_format = "webm"
    dl._url_cache = {URL: info}
    dl._cache_timestamps = {}
    dl._pending_resolves = {}
    dl._progressive = {}
    dl._progressive_files = {}
    dl._incomplete_files = set()
    dl._progressive_blocked = {}
    dl._inflight = {}
    dl.ydl = FakeYdl(info, filename)
    dl._make_progressive_ydl = (lambda: writer) if writer is not None else None
    return dl


def short_info(**extra):
    return {"title": "Kurz", "duration": 180, "ext": "webm",
            "url": "https://cdn.example/stream", "webpage_url": URL, **extra}


# ---------------------------------------------------------------------------
# a) Lebender Progressiv-Task wird adoptiert – kein zweiter Download
# ---------------------------------------------------------------------------

def test_resolve_adopts_running_progressive_without_second_download(tmp_path, monkeypatch):
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    target = tmp_path / "song.webm"
    writer = CountingWriter(target, write_bytes=PROGRESSIVE_MIN_BYTES + 1024, linger=0.3)
    dl = make_dl(short_info(), target, writer)

    async def run():
        task = dl._start_progressive(URL, short_info(), target, "Kurz")
        await asyncio.sleep(0.05)          # Download anlaufen lassen
        result = await dl.resolve_track(URL, "Kurz")
        await task
        return result

    _, filename, _, _ = asyncio.run(run())

    assert filename == target
    assert writer.calls == [[URL]]         # genau EIN Download-Start
    assert dl.ydl.download_calls == []     # und keiner über die Haupt-Instanz


# ---------------------------------------------------------------------------
# b) force_download bei lebendem Task → awaitet statt parallel zu laden
# ---------------------------------------------------------------------------

def test_force_download_awaits_running_task_instead_of_parallel(tmp_path, monkeypatch):
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    target = tmp_path / "song.webm"
    dl = make_dl(short_info(), target)

    async def run():
        async def running_download():
            await asyncio.sleep(0.05)
            target.write_bytes(b"\0" * 4096)

        task = asyncio.create_task(running_download())
        dl._register_inflight(URL, "Queue-Prefetch", "Kurz", task=task)
        return await dl.resolve_track(URL, "Kurz", force_download=True)

    _, filename, _, _ = asyncio.run(run())

    assert filename == target
    assert dl.ydl.download_calls == []     # der lebende Task WAR der Download


def test_force_download_loads_itself_when_task_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    target = tmp_path / "song.webm"
    dl = make_dl(short_info(), target)

    async def run():
        async def dying_download():
            await asyncio.sleep(0.05)
            raise RuntimeError("Download kaputt")

        task = asyncio.create_task(dying_download())
        dl._register_inflight(URL, "Queue-Prefetch", "Kurz", task=task)
        return await dl.resolve_track(URL, "Kurz", force_download=True)

    _, filename, _, _ = asyncio.run(run())

    assert filename == target
    assert dl.ydl.download_calls == [[URL]]   # erst nach dem Scheitern selbst geladen


# ---------------------------------------------------------------------------
# c) Autoplay-Prefetch lädt noch → resolve_track wartet statt doppelt zu laden
# ---------------------------------------------------------------------------

def test_resolve_waits_for_running_autoplay_prefetch(tmp_path):
    target = tmp_path / "song.webm"
    dl = make_dl(short_info(), target)     # _make_progressive_ydl=None: Neustart würde krachen

    async def run():
        async def autoplay_prefetch():
            await asyncio.sleep(0.05)
            target.write_bytes(b"\0" * 4096)

        task = asyncio.create_task(autoplay_prefetch())
        dl._register_inflight(URL, "Autoplay-Prefetch", "Kurz", task=task)
        return await dl.resolve_track(URL, "Kurz")

    _, filename, _, _ = asyncio.run(run())

    assert isinstance(filename, Path)
    assert filename == target              # Datei des Prefetch übernommen
    assert dl.ydl.download_calls == []     # kein eigener (Doppel-)Download


# ---------------------------------------------------------------------------
# e) Leiche: Registry leer + is_incomplete → heutiges Aufräumverhalten
# ---------------------------------------------------------------------------

def test_corpse_without_living_task_is_cleaned_and_redownloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    target = tmp_path / "song.webm"
    target.write_bytes(b"\0" * 512)        # Rest eines gescheiterten Laufs
    writer = CountingWriter(target, write_bytes=10 * 1024)
    dl = make_dl(short_info(), target, writer)
    dl._incomplete_files.add(target.resolve())
    assert dl._inflight == {}              # explizit: kein lebender Task

    async def run():
        result = await dl.resolve_track(URL, "Kurz")
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return result

    _, filename, _, _ = asyncio.run(run())

    assert filename == target
    assert target.stat().st_size == 10 * 1024   # neu geladen, nicht die Leiche
    assert not dl.is_incomplete(target)
    assert writer.calls == [[URL]]
