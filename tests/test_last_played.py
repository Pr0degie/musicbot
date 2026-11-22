"""last_played ist der Song VOR dem laufenden (⏮-Button, !replay).

Früher überschrieb jeder Trackstart das Feld blind mit current_track:
- Nach !seek, !eq oder einer Voice-Reparatur ist current_track vor dem
  Neustart None → der Vorgänger ging verloren.
- Bei Loop-Song, Stream-Retry und Progressive-Resume startet derselbe Song
  erneut → last_played zeigte auf den laufenden Song selbst.
"""

import asyncio

import discord

from conftest import FakeCtx, FakeVoiceClient, build_cog, cleanup_cog, make_fake_source
from test_seek_offset_scope import URL_A, URL_B, _PerUrlDownloader, _info, _wait_for_plays

URL_W = "https://www.youtube.com/watch?v=wwwwwwwwwww"


async def _w_dann_a(monkeypatch, tmp_path):
    """Spielt W und danach A. Ergebnis: A läuft, last_played ist W."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())
    dl = _PerUrlDownloader({
        URL_W: (_info("W"), "https://cdn.example/w", "W", 200),
        URL_A: (_info("A"), "https://cdn.example/a", "A", 200),
        URL_B: (_info("B"), "https://cdn.example/b", "B", 200),
    })
    vc = FakeVoiceClient()
    ctx = FakeCtx(vc)
    mc = build_cog(dl)
    mc.queue.extend([(URL_W, "W"), (URL_A, "A")])
    mc.is_playing = True
    await mc.play_next(ctx)
    await _wait_for_plays(vc, 1)
    vc.end_track()                           # W endet → A startet
    await _wait_for_plays(vc, 2)
    assert mc.current_track[0] == URL_A
    assert mc.last_played == (URL_W, "W", 200)
    return mc, vc, ctx


def test_naechster_song_merkt_den_vorgaenger(monkeypatch, tmp_path):
    async def run():
        mc, vc, ctx = await _w_dann_a(monkeypatch, tmp_path)
        mc.queue.append((URL_B, "B"))
        vc.end_track()                       # A endet → B startet
        await _wait_for_plays(vc, 3)

        assert mc.last_played == (URL_A, "A", 200)
        cleanup_cog(mc)

    asyncio.run(run())


def test_neustart_desselben_songs_behaelt_den_vorgaenger(monkeypatch, tmp_path):
    """Loop-Song (ebenso Stream-Retry, Progressive-Resume): A startet erneut,
    current_track ist dabei noch A."""
    async def run():
        mc, vc, ctx = await _w_dann_a(monkeypatch, tmp_path)
        mc.loop_mode = "song"
        vc.end_track()                       # A endet → Loop reiht A wieder ein
        await _wait_for_plays(vc, 3)

        assert mc.current_track[0] == URL_A
        assert mc.last_played == (URL_W, "W", 200), "der laufende Song ist nicht sein eigener Vorgänger"
        cleanup_cog(mc)

    asyncio.run(run())


def test_neustart_nach_seek_behaelt_den_vorgaenger(monkeypatch, tmp_path):
    """!seek, !eq und die Voice-Reparatur setzen current_track auf None,
    reihen den Song vorn ein und stoppen – danach startet er neu."""
    async def run():
        mc, vc, ctx = await _w_dann_a(monkeypatch, tmp_path)
        mc.current_track = None
        mc.queue.appendleft((URL_A, "A"))
        vc.end_track()
        await _wait_for_plays(vc, 3)

        assert mc.current_track[0] == URL_A
        assert mc.last_played == (URL_W, "W", 200), "der Vorgänger darf nicht verloren gehen"
        cleanup_cog(mc)

    asyncio.run(run())


def test_replay_nach_queue_ende_macht_den_song_nicht_zum_eigenen_vorgaenger(monkeypatch, tmp_path):
    async def run():
        mc, vc, ctx = await _w_dann_a(monkeypatch, tmp_path)
        vc.end_track()                       # A endet, Queue leer
        await asyncio.sleep(0.05)
        assert mc.current_track is None
        assert mc.last_played == (URL_A, "A", 200)   # „zuletzt gespielt" für !replay

        mc.queue.appendleft((URL_A, "A"))    # !replay
        mc.is_playing = True
        await mc.play_next(ctx)
        await _wait_for_plays(vc, 3)

        assert mc.current_track[0] == URL_A
        assert mc.last_played is None
        cleanup_cog(mc)

    asyncio.run(run())
