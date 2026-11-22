"""!seek auf eine Position am oder hinter dem Songende wird abgelehnt
(bestehende Meldung error.seek_invalid), sofern die Dauer bekannt ist.

Vorher startete der Track mit -ss hinter dem Ende neu: FFmpeg lieferte nichts,
der Song endete sofort.
"""
import asyncio

import pytest

from conftest import FakeCtx, FakeVoiceClient, build_cog
from cogs.music import MusicCommands
from utils.i18n import t

URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


def _seek(zeit, duration):
    vc = FakeVoiceClient()
    vc._playing = True
    ctx = FakeCtx(vc)

    async def run():
        mc = build_cog()
        mc.current_track = (URL, "Song", duration)
        mc.is_playing = True
        await MusicCommands.seek.callback(mc, ctx, zeit)
        return mc

    return asyncio.run(run()), ctx, vc


@pytest.mark.parametrize("zeit", ["3:21", "200", "200:00", "99999"])
def test_position_hinter_dem_ende_wird_abgelehnt(zeit):
    mc, ctx, vc = _seek(zeit, duration=200)

    assert ctx.texts == [t("error.seek_invalid")]
    assert vc.stop_calls == 0, "Wiedergabe läuft unverändert weiter"
    assert mc.current_track == (URL, "Song", 200)
    assert list(mc.queue) == []
    assert mc._seek_offset == 0


def test_position_innerhalb_des_songs_springt_wie_bisher():
    mc, ctx, vc = _seek("3:19", duration=200)

    assert vc.stop_calls == 1
    assert mc._seek_offset == 199
    assert list(mc.queue) == [(URL, "Song")]
    assert ctx.texts == [t("status.seeking", mins=3, secs="19", title="Song")]


@pytest.mark.parametrize("duration", [0, None])
def test_unbekannte_dauer_laesst_jede_position_zu(duration):
    mc, ctx, vc = _seek("59:00", duration=duration)

    assert vc.stop_calls == 1
    assert mc._seek_offset == 3540
