"""Wartung alle 50 Songs: yt_dlp-Instanzen werden im Worker-Thread gebaut,
jede Zustandsänderung am Downloader passiert auf dem Event-Loop.

Vorher lief dl.rebuild komplett im Thread und tauschte dort _url_cache,
_cache_timestamps und _pending_resolves aus, während der Loop sie benutzte.
"""
import asyncio
import json
import threading
import time

from conftest import FakeCtx, build_cog
from cogs.downloader import METADATA_CACHE_FILE, Downloader


class _SpyDownloader(Downloader):
    """Protokolliert, in welchem Thread Attribute gesetzt und Instanzen gebaut werden."""

    def __init__(self):   # kein echtes __init__: keine yt_dlp-Instanzen, kein Datei-Load
        object.__setattr__(self, "writes", [])
        object.__setattr__(self, "builds", [])
        object.__setattr__(self, "build_gate", None)
        self.audio_format = "webm"
        self._cookie_mode = False
        self._url_cache = {}
        self._cache_timestamps = {}
        self._pending_resolves = {}
        for attr in self._YDL_KINDS.values():
            setattr(self, attr, f"alt:{attr}")
        self.writes.clear()

    def __setattr__(self, name, value):
        self.writes.append((name, threading.get_ident()))
        object.__setattr__(self, name, value)

    def _build_ydl(self, kind, fast=True):
        self.builds.append((kind, threading.get_ident()))
        if self.build_gate is not None:
            self.build_gate.wait(5)
        return f"neu:{kind}"


def _fill(dl):
    now = time.time()
    for u in ("https://x/1", "https://x/2", "https://x/3"):
        dl._url_cache[u] = {"id": u[-1], "title": u}
        dl._cache_timestamps[u] = now
    dl._pending_resolves["https://x/9"] = object()


def test_zustand_aendert_sich_nur_auf_dem_loop(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def run():
        loop_thread = threading.get_ident()
        dl = _SpyDownloader()
        _fill(dl)

        await dl.rebuild_async("mp3", keep_urls={"https://x/1", "https://x/3"})

        assert dl.builds and all(ident != loop_thread for _, ident in dl.builds), \
            "yt_dlp-Instanzen gehören in den Worker-Thread"
        assert sorted(kind for kind, _ in dl.builds) == sorted(dl._YDL_KINDS)
        fremd = [name for name, ident in dl.writes if ident != loop_thread]
        assert fremd == [], f"Zustand im Worker-Thread verändert: {fremd}"

        assert set(dl._url_cache) == {"https://x/1", "https://x/3"}
        assert set(dl._cache_timestamps) == {"https://x/1", "https://x/3"}
        assert dl._pending_resolves == {}
        assert dl.audio_format == "mp3"
        for kind, attr in dl._YDL_KINDS.items():
            assert getattr(dl, attr) == f"neu:{kind}"
        # Wie rebuild(): vor dem Filtern wurde der volle Stand auf Disk gesichert.
        assert set(json.loads(METADATA_CACHE_FILE.read_text(encoding="utf-8"))) == {
            "https://x/1", "https://x/2", "https://x/3"}

    asyncio.run(run())


def test_ohne_keep_urls_wird_alles_geleert(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def run():
        dl = _SpyDownloader()
        _fill(dl)
        await dl.rebuild_async("webm", keep_urls=None)
        assert dl._url_cache == {} and dl._cache_timestamps == {} and dl._pending_resolves == {}

    asyncio.run(run())


def test_cache_ist_schon_bereinigt_waehrend_der_thread_baut(monkeypatch, tmp_path):
    """Die Bereinigung passiert synchron beim Aufruf (auf dem Loop), nicht
    irgendwann später aus dem Thread heraus; die alten Instanzen bleiben
    benutzbar, bis die neuen fertig sind."""
    monkeypatch.chdir(tmp_path)

    async def run():
        dl = _SpyDownloader()
        _fill(dl)
        dl.build_gate = threading.Event()

        task = asyncio.create_task(dl.rebuild_async("webm", keep_urls={"https://x/1"}))
        for _ in range(200):
            if dl.builds:
                break
            await asyncio.sleep(0.005)
        assert dl.builds, "Worker-Thread baut"
        assert set(dl._url_cache) == {"https://x/1"}
        assert dl.ydl == "alt:ydl"

        dl.build_gate.set()
        await task
        assert dl.ydl == "neu:main"

    asyncio.run(run())


def test_paralleler_neuaufbau_gewinnt(monkeypatch, tmp_path):
    """Baut während des Thread-Laufs ein anderer Pfad die Instanzen neu
    (!format, Cookie-Fallback), werden die im Thread gebauten verworfen."""
    monkeypatch.chdir(tmp_path)

    async def run():
        dl = _SpyDownloader()
        dl.build_gate = threading.Event()

        task = asyncio.create_task(dl.rebuild_async("webm", keep_urls=set()))
        for _ in range(200):
            if dl.builds:
                break
            await asyncio.sleep(0.005)
        for attr in dl._YDL_KINDS.values():       # z. B. enable_cookie_mode → _init_ydl
            setattr(dl, attr, f"cookie:{attr}")
        dl.build_gate.set()
        await task

        for attr in dl._YDL_KINDS.values():
            assert getattr(dl, attr) == f"cookie:{attr}"

    asyncio.run(run())


def test_wartung_nach_50_songs_nutzt_rebuild_async():
    async def run():
        calls = []

        class _Dl:
            async def rebuild_async(self, audio_format, keep_urls=None):
                calls.append((audio_format, keep_urls, threading.get_ident()))

            def rebuild(self, *a, **kw):
                raise AssertionError("rebuild darf nicht mehr im Worker-Thread laufen")

        mc = build_cog(_Dl())
        mc._songs_played = 49
        mc.queue.append(("https://q/1", "Q1"))
        mc.current_track = ("https://c/1", "C1", 100)
        mc.prefetch_task = None
        mc._prefetch_upcoming = lambda: asyncio.sleep(0)

        mc._schedule_background_work(FakeCtx())
        await asyncio.sleep(0.05)

        assert calls == [("webm", {"https://q/1", "https://c/1"}, threading.get_ident())]

    asyncio.run(run())
