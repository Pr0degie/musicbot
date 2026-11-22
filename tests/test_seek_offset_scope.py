"""Der Seek-Offset (-ss) gilt nur für den Track, für den er gesetzt wurde.

Vorher wurde self._seek_offset erst NACH erfolgreichem Resolve konsumiert:
scheiterte der Resolve (oder griff der _skip_resolving-Pfad), blieb der Wert
stehen und der nächste Track startete mit dem fremden -ss.
"""

import asyncio

import discord

from conftest import FakeCtx, FakeDownloader, FakeVoiceClient, build_cog, cleanup_cog, make_fake_source
from cogs.music import MusicCommands

URL_A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
URL_B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"


def _info(title):
    return {"title": title, "webpage_url": "https://www.youtube.com/watch?v=x",
            "thumbnail": None, "uploader": None, "duration": 200}


class _PerUrlDownloader(FakeDownloader):
    """resolve_track je URL: Exception-Instanz → wird geworfen, sonst Ergebnis."""

    def __init__(self, results):
        super().__init__()
        self.results = results
        self.on_resolve = None   # optionaler Hook(url), läuft während des Resolves

    async def resolve_track(self, url, title, force_download=False, min_buffer_seconds=0):
        self.resolve_calls.append(url)
        self.min_buffer_calls.append(min_buffer_seconds)
        await asyncio.sleep(0)
        if self.on_resolve is not None:
            self.on_resolve(url)
        result = self.results[url]
        if isinstance(result, BaseException):
            raise result
        return result


async def _wait_for_plays(vc, count):
    for _ in range(200):
        if len(vc.play_calls) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"nur {len(vc.play_calls)} von {count} Wiedergaben gestartet")


def _before_options(source_calls, index):
    return source_calls[index][1].get("before_options") or ""


def test_resolve_fehler_vererbt_seek_offset_nicht(monkeypatch, tmp_path):
    """_seek_offset = 90, Queue [A, B], Resolve für A wirft TimeoutError →
    B muss ohne -ss starten."""
    monkeypatch.chdir(tmp_path)
    source_calls = []
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source(calls=source_calls))

    async def run():
        dl = _PerUrlDownloader({
            URL_A: asyncio.TimeoutError(),
            URL_B: (_info("B"), "https://cdn.example/b", "B", 200),
        })
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog(dl)
        mc._seek_offset = 90
        mc.queue.extend([(URL_A, "A"), (URL_B, "B")])

        await mc.play_next(ctx)
        await _wait_for_plays(vc, 1)

        assert mc.current_track[0] == URL_B
        assert "-ss" not in _before_options(source_calls, 0), "B darf den Offset von A nicht erben"
        assert mc._seek_offset == 0
        # Der Puffer-Aufschlag für das Seek-Ziel gilt ebenfalls nur für A.
        assert dl.min_buffer_calls == [90, 0]
        cleanup_cog(mc)

    asyncio.run(run())


def test_skip_resolving_vererbt_seek_offset_nicht(monkeypatch, tmp_path):
    """Alternative gewählt, während A auflöst (_skip_resolving): die Alternative
    B startet von vorn, nicht mit dem -ss von A."""
    monkeypatch.chdir(tmp_path)
    source_calls = []
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source(calls=source_calls))

    async def run():
        dl = _PerUrlDownloader({
            URL_A: (_info("A"), "https://cdn.example/a", "A", 200),
            URL_B: (_info("B"), "https://cdn.example/b", "B", 200),
        })
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog(dl)
        mc._seek_offset = 90
        mc.queue.append((URL_A, "A"))

        def waehle_alternative(url):
            if url == URL_A:
                mc._skip_resolving = True
                mc.queue.appendleft((URL_B, "B"))

        dl.on_resolve = waehle_alternative

        await mc.play_next(ctx)
        await _wait_for_plays(vc, 1)

        assert mc.current_track[0] == URL_B
        assert len(vc.play_calls) == 1
        assert "-ss" not in _before_options(source_calls, 0)
        assert mc._seek_offset == 0
        cleanup_cog(mc)

    asyncio.run(run())


def test_seek_offset_gilt_nicht_fuer_dazwischengeschobenen_track(monkeypatch, tmp_path):
    """!seek auf A, aber bevor play_next A wieder aufnimmt, steht B vorn
    (z. B. !next im Fenster einer Voice-Reparatur): B startet ohne -ss."""
    monkeypatch.chdir(tmp_path)
    source_calls = []
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source(calls=source_calls))

    async def run():
        dl = _PerUrlDownloader({
            URL_A: (_info("A"), "https://cdn.example/a", "A", 200),
            URL_B: (_info("B"), "https://cdn.example/b", "B", 200),
        })
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog(dl)
        mc.queue.append((URL_A, "A"))
        await mc.play_next(ctx)
        assert mc.current_track[0] == URL_A

        await MusicCommands.seek.callback(mc, ctx, "1:30")
        assert list(mc.queue) == [(URL_A, "A")]
        mc.queue.appendleft((URL_B, "B"))      # schiebt sich vor den Seek-Track

        vc.end_track()
        await _wait_for_plays(vc, 2)

        assert mc.current_track[0] == URL_B
        assert "-ss" not in _before_options(source_calls, 1)
        assert mc._seek_offset == 0
        cleanup_cog(mc)

    asyncio.run(run())


def test_seek_startet_denselben_track_mit_offset(monkeypatch, tmp_path):
    """Gegenprobe: !seek 1:30 startet denselben Track mit -ss 90 neu."""
    monkeypatch.chdir(tmp_path)
    source_calls = []
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source(calls=source_calls))

    async def run():
        dl = _PerUrlDownloader({URL_A: (_info("A"), "https://cdn.example/a", "A", 200)})
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog(dl)
        mc.queue.append((URL_A, "A"))
        await mc.play_next(ctx)

        await MusicCommands.seek.callback(mc, ctx, "1:30")
        vc.end_track()
        await _wait_for_plays(vc, 2)

        assert mc.current_track[0] == URL_A
        assert "-ss 90" in _before_options(source_calls, 1)
        assert dl.min_buffer_calls == [0, 90]
        assert mc._seek_offset == 0
        cleanup_cog(mc)

    asyncio.run(run())
