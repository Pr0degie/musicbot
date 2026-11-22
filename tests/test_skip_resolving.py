"""Der Skip-Wunsch der Alternativ-Buttons gilt nur für den Resolve, der gerade läuft.

Wählt jemand eine Alternative, während play_next den ersten Treffer noch
auflöst, soll play_next den Treffer überspringen und die Alternative spielen.
Früher war das ein ungebundenes Flag: Es wurde nur nach erfolgreichem Resolve
verbraucht und auch gesetzt, wenn gar kein Resolve lief – dann übersprang
play_next später genau die gewählte Alternative.
"""

import asyncio
import types

import discord

from conftest import FakeCtx, FakeVoiceClient, build_cog, cleanup_cog, make_fake_source
from test_seek_offset_scope import URL_A, URL_B, _PerUrlDownloader, _info, _wait_for_plays
from views.music_controls import SearchAutoplayView


class FakeResponse:
    async def edit_message(self, **kwargs):
        pass


class _KlickDownloader(_PerUrlDownloader):
    """Führt `klick` aus, während der Resolve von URL_A läuft."""

    klick = None

    async def resolve_track(self, url, title, force_download=False, min_buffer_seconds=0):
        if self.klick is not None and url == URL_A:
            await self.klick()
        return await super().resolve_track(url, title, force_download, min_buffer_seconds)


def _klick_alternative(mc, ctx):
    """Erster Treffer A, Alternative B: klickt den Button für B."""
    view = SearchAutoplayView({"webpage_url": URL_A, "title": "A"},
                              [{"webpage_url": URL_B, "title": "B"}], mc, ctx)
    return view.children[0].callback(types.SimpleNamespace(response=FakeResponse()))


def test_alternative_spielt_auch_wenn_der_resolve_des_treffers_scheitert(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())

    async def run():
        dl = _KlickDownloader({
            URL_A: asyncio.TimeoutError(),
            URL_B: (_info("B"), "https://cdn.example/b", "B", 200),
        })
        dl.prime_first_hit = lambda url, title: None
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog(dl)
        mc.queue.append((URL_A, "A"))
        mc.is_playing = True                 # setzt !p, bevor es play_next ruft
        dl.klick = lambda: _klick_alternative(mc, ctx)   # während A auflöst

        await mc.play_next(ctx)
        await _wait_for_plays(vc, 1)

        assert mc.current_track[0] == URL_B, "die gewählte Alternative muss spielen"
        assert mc._skip_resolving is False
        cleanup_cog(mc)

    asyncio.run(run())


def test_alternative_ohne_laufenden_resolve_setzt_keinen_skip_wunsch(monkeypatch, tmp_path):
    """Der erste Treffer ist längst durch (weder aktueller Song noch in der
    Queue), es läuft etwas anderes: die Alternative wird eingereiht und der
    laufende Song gestoppt – aber nichts darf später übersprungen werden."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())

    async def run():
        dl = _PerUrlDownloader({URL_B: (_info("B"), "https://cdn.example/b", "B", 200)})
        dl.prime_first_hit = lambda url, title: None
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog(dl)
        mc.current_track = ("https://www.youtube.com/watch?v=ccccccccccc", "Anderer Song", 200)
        mc.is_playing = True

        await _klick_alternative(mc, ctx)

        assert mc._skip_resolving is False, "es läuft kein Resolve, den man überspringen könnte"
        assert list(mc.queue) == [(URL_B, "B")]

        await mc.play_next(ctx)              # der nächste Trackstart
        await _wait_for_plays(vc, 1)
        assert mc.current_track[0] == URL_B, "die gewählte Alternative muss spielen"
        cleanup_cog(mc)

    asyncio.run(run())


def test_alternative_waehrend_des_resolves_ueberspringt_den_treffer(monkeypatch, tmp_path):
    """Der Normalfall bleibt: A löst gerade auf, B wird gewählt → B spielt, A nicht."""
    monkeypatch.chdir(tmp_path)
    source_calls = []
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source(calls=source_calls))

    async def run():
        dl = _KlickDownloader({
            URL_A: (_info("A"), "https://cdn.example/a", "A", 200),
            URL_B: (_info("B"), "https://cdn.example/b", "B", 200),
        })
        dl.prime_first_hit = lambda url, title: None
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog(dl)
        mc.queue.append((URL_A, "A"))
        mc.is_playing = True                 # setzt !p, bevor es play_next ruft
        dl.klick = lambda: _klick_alternative(mc, ctx)

        await mc.play_next(ctx)
        await _wait_for_plays(vc, 1)

        assert mc.current_track[0] == URL_B
        assert len(vc.play_calls) == 1, "A darf nicht anspielen"
        assert [call[0] for call in source_calls] == ["https://cdn.example/b"]
        cleanup_cog(mc)

    asyncio.run(run())
