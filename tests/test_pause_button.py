"""Tests für den Pause-Button: pausiert stumm.

Der Zustand ist an der Karte ablesbar (Button wird grün „Fortsetzen", der
Fortschrittsbalken steht) – der Klick wird nur quittiert, keine Meldung.
"""

import asyncio
import time
import types

from tests.conftest import FakeCtx, FakeVoiceClient, build_cog
from views.music_controls import MusicControlView


class FakeResponse:
    def __init__(self):
        self.sent, self.deferred, self.edited = [], 0, []

    def is_done(self):
        return bool(self.sent or self.deferred or self.edited)

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    async def defer(self):
        self.deferred += 1

    async def edit_message(self, **kwargs):
        self.edited.append(kwargs)


class FakeInteraction:
    def __init__(self):
        self.response = FakeResponse()
        self.message = types.SimpleNamespace(id=1)
        self.user = types.SimpleNamespace(voice=None)
        self.client = types.SimpleNamespace()


def _paused_setup():
    vc = FakeVoiceClient()
    vc._playing = True

    def pause():
        vc._playing = False

    vc.pause = pause
    ctx = FakeCtx(voice_client=vc)
    cog = build_cog()
    return MusicControlView(cog, ctx), cog


def test_pause_button_is_silent_and_freezes_progress():
    async def run():
        view, cog = _paused_setup()
        cog.track_start_time = time.monotonic() - 161.4  # 2:41 gespielt

        interaction = FakeInteraction()
        await view.pause.callback(interaction)

        assert interaction.response.sent == [], "keine Pausiert-Meldung mehr"
        assert interaction.response.is_done(), "Klick muss trotzdem quittiert werden"
        assert cog.is_playing is False
        assert int(cog._elapsed_seconds()) == 161

    asyncio.run(run())


def test_pause_button_on_active_card_acks_with_edit():
    async def run():
        view, cog = _paused_setup()
        interaction = FakeInteraction()
        cog.now_playing_msg = interaction.message

        await view.pause.callback(interaction)

        assert interaction.response.edited == [{"view": view}], "Edit dient zugleich als Antwort"
        assert interaction.response.deferred == 0

    asyncio.run(run())
