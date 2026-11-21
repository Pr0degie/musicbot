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


def test_p_starts_search_before_voice_join_and_only_once():
    """!p startet die yt_dlp-Suche parallel zum Voice-Join (beim ersten !p ein
    voller Handshake) – nicht dahinter – und fragt dabei nicht doppelt ab."""
    from cogs.music import MusicCommands

    async def run():
        mc, ctx = build(is_playing=False)
        order = []
        real_extract = mc.dl.extract_info_async

        async def extract(query, kind="main", **kw):
            order.append("suche")
            return await real_extract(query, kind, **kw)

        async def ensure_voice(_ctx):
            await asyncio.sleep(0.05)   # Voice-Handshake
            order.append("voice")
            return True

        async def noop(*a, **k):
            return None

        mc.dl.extract_info_async = extract
        mc._ensure_voice = ensure_voice
        mc._stop_radio_for_takeover = noop
        mc.play_next = noop

        await MusicCommands.p.callback(mc, ctx, eingabe="irgendein song")

        assert order == ["suche", "voice"], "Suche muss vor dem Ende des Voice-Joins laufen"
        assert mc.dl.extract_calls == [("ytsearch3:irgendein song", "search")]
        assert mc.dl.primed == [(URL, "Treffer")]

    asyncio.run(run())


def test_p_without_voice_drops_early_search_quietly():
    from cogs.music import MusicCommands

    async def run():
        mc, ctx = build(is_playing=False)

        async def no_voice(_ctx):
            return False

        mc._ensure_voice = no_voice
        await MusicCommands.p.callback(mc, ctx, eingabe="irgendein song")
        await asyncio.sleep(0)

        assert list(mc.queue) == [], "ohne Voice nichts einreihen"
        assert mc.dl.primed == []

    asyncio.run(run())
