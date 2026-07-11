"""Tests für den Pause-Button: ephemere Bestätigung mit aktueller Position.

Mit track_start_time → "Pausiert bei m:ss"; ohne (z. B. Radio) → heutige
Meldung ohne Position.
"""

import asyncio
import time
import types

from tests.conftest import FakeCtx, FakeVoiceClient, build_cog
from utils.i18n import t
from views.music_controls import MusicControlView


class FakeInteraction:
    def __init__(self):
        self.sent = []
        self.response = types.SimpleNamespace(send_message=self._send)

    async def _send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


def _paused_setup():
    vc = FakeVoiceClient()
    vc._playing = True
    vc.pause = lambda: None
    ctx = FakeCtx(voice_client=vc)
    cog = build_cog()
    return MusicControlView(cog, ctx), cog


def test_pause_button_shows_position():
    async def run():
        view, cog = _paused_setup()
        cog.track_start_time = time.monotonic() - 161.4  # 2:41 gespielt

        interaction = FakeInteraction()
        await view.pause.callback(interaction)

        assert interaction.sent == [(t("status.paused_at_eph", position="2:41"), {"ephemeral": True})]
        assert cog.is_playing is False

    asyncio.run(run())


def test_pause_button_without_start_time_keeps_old_message():
    async def run():
        view, cog = _paused_setup()
        assert cog.track_start_time is None  # z. B. Radio-Modus

        interaction = FakeInteraction()
        await view.pause.callback(interaction)

        assert interaction.sent == [(t("status.paused_eph"), {"ephemeral": True})]

    asyncio.run(run())
