"""Tests für die geteilte aiohttp-Session des Music-Cogs (lyrics.ovh).

Soll-Verhalten: eine Session für alle Aufrufe (cog_load → cog_unload);
ist sie unerwartet geschlossen, wird eine neue erstellt statt ein Fehler
geworfen.
"""

import asyncio

from cogs.music import MusicCommands
from conftest import build_cog


def test_http_session_is_reused_and_recreated_when_closed():
    async def run():
        mc = build_cog()
        s1 = MusicCommands._http(mc)
        s2 = MusicCommands._http(mc)
        assert s2 is s1, "gleiche Session wird wiederverwendet"

        await s1.close()
        s3 = MusicCommands._http(mc)
        assert s3 is not s1, "geschlossene Session wird ersetzt statt Fehler"
        assert not s3.closed
        await s3.close()

    asyncio.run(run())


def test_cog_unload_closes_session(tmp_path, monkeypatch):
    monkeypatch.setattr("cogs.music.SCORE_FILE", tmp_path / "play_counts.json")

    async def run():
        from cogs.downloader import Downloader

        dl = Downloader.__new__(Downloader)
        dl._url_cache = {}
        dl._cache_timestamps = {}
        dl._pending_resolves = {}
        mc = build_cog(dl=dl)
        session = MusicCommands._http(mc)
        await mc.cog_unload()
        assert session.closed

    asyncio.run(run())
