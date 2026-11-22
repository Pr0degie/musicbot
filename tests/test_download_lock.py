"""yt_dlp ist nicht thread-safe: über die geteilte Haupt-Instanz (dl.ydl) darf
immer nur EIN Download laufen – auch wenn es Downloads verschiedener URLs
sind und sie aus verschiedenen Pfaden kommen (Queue-Prefetch,
Autoplay-Prefetch, Download-Fallback).
"""

import asyncio
import threading
import time
from collections import deque

import cogs.downloader_cleanup as cleanup_mod
from cogs.downloader import Downloader

URL_A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
URL_B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"


def info_for(url):
    return {"id": url[-11:], "title": f"Song {url[-11:]}", "duration": 180, "ext": "webm",
            "url": "https://cdn.example/stream", "webpage_url": url}


class CountingYdl:
    """Haupt-Instanz-Ersatz: eine Zieldatei je Video-ID. Zählt, wie viele
    Downloads gleichzeitig in Worker-Threads laufen."""

    def __init__(self, directory):
        self.directory = directory
        self.active = 0
        self.max_active = 0
        self.started = []
        self._guard = threading.Lock()

    def prepare_filename(self, info):
        return str(self.directory / f"{info['id']}.webm")

    def _write(self, url, kind):
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.append((kind, url))
        try:
            time.sleep(0.15)                 # Fenster, in dem ein zweiter Download starten könnte
            (self.directory / f"{url[-11:]}.webm").write_bytes(b"\0" * 4096)
        finally:
            with self._guard:
                self.active -= 1

    def download(self, urls):
        self._write(urls[0], "download")

    def extract_info(self, url, download=False):
        assert download, "Extraktion ohne Download ist hier nicht vorgesehen"
        self._write(url, "autoplay")
        return info_for(url)


class OneHitYdl:
    """autoplay_ydl-Ersatz: die Suche liefert genau einen Kandidaten."""

    def extract_info(self, url, download=False):
        return {"entries": [{"url": URL_A, "title": "Song aaaaaaaaaaa"}]}


def make_dl(tmp_path):
    dl = Downloader.__new__(Downloader)
    dl.audio_format = "webm"
    dl._url_cache = {URL_A: info_for(URL_A), URL_B: info_for(URL_B)}
    dl._cache_timestamps = {}
    dl._pending_resolves = {}
    dl._progressive = {}
    dl._progressive_files = {}
    dl._incomplete_files = set()
    dl._progressive_blocked = {}
    dl._inflight = {}
    dl.ydl = CountingYdl(tmp_path)
    dl.autoplay_ydl = OneHitYdl()
    return dl


async def _prefetch_b_laeuft(dl):
    """Startet den Queue-Prefetch von Song B und wartet, bis sein Thread lädt."""
    prefetch = asyncio.create_task(dl.prefetch_next(deque([(URL_B, "Song B")]), 0))
    while not dl.ydl.started:
        await asyncio.sleep(0.01)
    return prefetch


def test_download_fallback_wartet_auf_den_laufenden_prefetch(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup_mod, "DOWNLOADS_MAX_MB", 0)
    dl = make_dl(tmp_path)

    async def run():
        prefetch = await _prefetch_b_laeuft(dl)
        result = await dl.resolve_track(URL_A, "Song A", force_download=True)
        await prefetch
        return result

    _, filename, _, _ = asyncio.run(run())

    assert filename == tmp_path / "aaaaaaaaaaa.webm"
    assert dl.ydl.started == [("download", URL_B), ("download", URL_A)]
    assert dl.ydl.max_active == 1, "zwei Downloads liefen gleichzeitig über dieselbe yt_dlp-Instanz"


def test_autoplay_download_wartet_auf_den_laufenden_prefetch(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup_mod, "DOWNLOADS_MAX_MB", 0)
    dl = make_dl(tmp_path)

    async def run():
        prefetch = await _prefetch_b_laeuft(dl)
        result = await dl.prefetch_autoplay("https://www.youtube.com/watch?v=ccccccccccc", "Referenz", [])
        await prefetch
        return result

    assert asyncio.run(run()) == (URL_A, "Song aaaaaaaaaaa")
    assert dl.ydl.started == [("download", URL_B), ("autoplay", URL_A)]
    assert dl.ydl.max_active == 1, "zwei Downloads liefen gleichzeitig über dieselbe yt_dlp-Instanz"
