"""Tests für den Vorwärm-Zweig von _search_and_enqueue.

Regel: Läuft gerade nichts, startet der erste Treffer den Download sofort
(prime_first_hit) – so läuft yt_dlp parallel zu den Discord-Nachrichten statt
hinter ihnen. Läuft schon Musik, werden nur die Metadaten vorgewärmt; geladen
wird per Prefetch, wenn der Titel an der Reihe ist.
"""

import asyncio

from conftest import FakeCtx, FakeDownloader, build_cog

URL = "https://www.youtube.com/watch?v=ccccccccccc"


class SearchYdl:
    """Liefert genau einen Treffer – so entstehen keine Alternativ-Buttons."""

    def extract_info(self, query, download=False):
        return {"entries": [{"webpage_url": URL, "title": "Treffer"}]}


def build(is_playing):
    dl = FakeDownloader()
    dl.search_ydl = SearchYdl()
    mc = build_cog(dl)
    mc.is_playing = is_playing
    mc.autoplay_enabled = False
    return mc, FakeCtx()


def enqueue(mc, ctx):
    return mc._search_and_enqueue(
        ctx, "irgendein song", log_tag="p", timeout_key="error.search_timeout",
        insert="evict_or_back", with_alts_key="status.playing_with_alts",
        no_alts_key="status.added",
    )


def test_idle_primes_download_immediately():
    async def run():
        mc, ctx = build(is_playing=False)

        assert await enqueue(mc, ctx) is True

        assert mc.dl.primed == [(URL, "Treffer")], "Download muss sofort anlaufen"
        assert mc.dl.resolved_only == []
        assert list(mc.queue) == [(URL, "Treffer")]

    asyncio.run(run())


def test_while_playing_only_warms_metadata():
    async def run():
        mc, ctx = build(is_playing=True)

        assert await enqueue(mc, ctx) is True

        assert mc.dl.primed == [], "laufender Song behält die Bandbreite"
        # _start_resolve läuft als Task – vor der Prüfung einsammeln.
        await asyncio.gather(*[t for t in asyncio.all_tasks()
                               if t is not asyncio.current_task()])
        assert mc.dl.resolved_only == [URL]

    asyncio.run(run())


def test_search_uses_search_instance():
    async def run():
        mc, ctx = build(is_playing=False)

        await enqueue(mc, ctx)

        assert mc.dl.extract_calls == [("ytsearch3:irgendein song", "search")]

    asyncio.run(run())
