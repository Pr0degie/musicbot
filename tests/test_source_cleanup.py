"""Eine FFmpeg-Quelle, die nie an vc.play() geht, wird sofort beendet.

discord.FFmpegOpusAudio startet den FFmpeg-Prozess schon im Konstruktor.
Bricht play_next danach ab (Voice getrennt, DM-Bridge spricht) oder wirft
vc.play(), lief der Prozess früher weiter, bis der Garbage Collector die
Quelle irgendwann abräumte – unter Windows hielt er so lange die Datei offen.
"""

import asyncio

import discord

from conftest import FakeCtx, FakeDownloader, FakeVoiceClient, build_cog, cleanup_cog

URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
INFO = {"title": "Song", "webpage_url": URL, "thumbnail": None, "uploader": None, "duration": 200}


class CountingSource:
    """Ersatz für discord.FFmpegOpusAudio, der cleanup()-Aufrufe zählt."""

    instances = []

    def __init__(self, source, *args, **kwargs):
        self.cleanups = 0
        self.stderr = kwargs.get("stderr")
        CountingSource.instances.append(self)

    def cleanup(self):
        self.cleanups += 1


class DisconnectedVoiceClient(FakeVoiceClient):
    def is_connected(self):
        return False


class RaisingVoiceClient(FakeVoiceClient):
    def play(self, source, after=None):
        raise discord.ClientException("Not connected to voice.")


def _setup(monkeypatch, tmp_path, vc):
    monkeypatch.chdir(tmp_path)
    CountingSource.instances = []
    monkeypatch.setattr(discord, "FFmpegOpusAudio", CountingSource)
    mc = build_cog(FakeDownloader((INFO, "https://cdn.example/stream", "Song", 200)))
    mc.queue.append((URL, "Song"))
    mc.is_playing = True
    return mc, FakeCtx(vc)


def _die_eine_quelle():
    assert len(CountingSource.instances) == 1
    return CountingSource.instances[0]


def test_voice_getrennt_quelle_wird_beendet(monkeypatch, tmp_path):
    async def run():
        mc, ctx = _setup(monkeypatch, tmp_path, DisconnectedVoiceClient())

        await mc.play_next(ctx)

        source = _die_eine_quelle()
        assert source.cleanups == 1, "FFmpeg-Prozess der verworfenen Quelle muss beendet werden"
        assert source.stderr.closed
        assert list(mc.queue) == [(URL, "Song")], "Track steht wieder vorn"
        cleanup_cog(mc)

    asyncio.run(run())


def test_dm_spricht_quelle_wird_beendet(monkeypatch, tmp_path):
    async def run():
        mc, ctx = _setup(monkeypatch, tmp_path, FakeVoiceClient())
        echtes_resolve = mc.dl.resolve_track

        async def resolve_und_dm_uebernimmt(*args, **kwargs):
            result = await echtes_resolve(*args, **kwargs)
            mc.bot.dm_speaking = True        # die DM-Bridge übernimmt während des Resolves
            return result

        mc.dl.resolve_track = resolve_und_dm_uebernimmt

        await mc.play_next(ctx)

        source = _die_eine_quelle()
        assert source.cleanups == 1
        assert source.stderr.closed
        assert ctx.voice_client.play_calls == []
        cleanup_cog(mc)

    asyncio.run(run())


def test_play_wirft_quelle_wird_beendet(monkeypatch, tmp_path):
    async def run():
        mc, ctx = _setup(monkeypatch, tmp_path, RaisingVoiceClient())

        await mc.play_next(ctx)
        await asyncio.sleep(0.05)            # Fehlerpfad stößt den nächsten Durchlauf an

        source = _die_eine_quelle()
        assert source.cleanups == 1
        assert source.stderr.closed
        cleanup_cog(mc)

    asyncio.run(run())


def test_gestartete_quelle_wird_nicht_beendet(monkeypatch, tmp_path):
    """Gegenprobe: nach geglücktem vc.play() gehört die Quelle dem Player."""
    async def run():
        mc, ctx = _setup(monkeypatch, tmp_path, FakeVoiceClient())

        await mc.play_next(ctx)

        source = _die_eine_quelle()
        assert ctx.voice_client.play_calls == [source]
        assert source.cleanups == 0
        cleanup_cog(mc)

    asyncio.run(run())
