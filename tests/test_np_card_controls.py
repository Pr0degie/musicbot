"""Tests für die Bedienelemente der Now-Playing-Karte jenseits des ❌:

- Zustandsfarben: Autoplay/Loop grün = an, Pause ↔ Fortsetzen als ein Button
- „Als Nächstes"-Zeile
- ⏮ Zurück (`_queue_previous`)
- Rückgängig nach !clear/❌ (`_restore_cleared`, `UndoClearView`)
"""

import asyncio
import types

import discord
import pytest

import config
from tests.conftest import FakeCtx, FakeVoiceClient, build_cog
from utils.i18n import t
from views.music_controls import MusicControlView, UndoClearView

S = discord.ButtonStyle


class FakeMessage:
    def __init__(self, msg_id=1):
        self.id = msg_id
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeInteraction:
    def __init__(self, message=None):
        self.sent = []
        self.edited = []
        self.message = message or FakeMessage()
        self.user = types.SimpleNamespace(voice=None)
        self.response = types.SimpleNamespace(send_message=self._send, edit_message=self._edit)

        async def _is_owner(_user):
            return False

        self.client = types.SimpleNamespace(is_owner=_is_owner)

    async def _send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    async def _edit(self, **kwargs):
        self.edited.append(kwargs)


def _setup(playing=True):
    vc = FakeVoiceClient()
    vc._playing = playing
    cog = build_cog()
    cog.dl = types.SimpleNamespace(clear_cache=lambda: None)
    cog._restart_prefetch = lambda: None
    cog._stop_radio = lambda: None
    stopped = []
    cog._stop_for_advance = stopped.append
    ctx = FakeCtx(voice_client=vc)
    view = MusicControlView(cog, ctx, heading="### 🎵 Läuft", details="**Dauer** 3:30", footer="Uploader")
    return view, cog, vc, stopped, ctx


def _details(view):
    return view.to_components()[0]["components"][1]["content"]


# ---------------------------------------------------------------------------
# Zustandsfarben + Pause/Fortsetzen
# ---------------------------------------------------------------------------

def test_buttons_mirror_cog_state():
    view, cog, vc, _stopped, _ctx = _setup(playing=True)
    assert (view.pause.label, view.pause.style) == (t("button.pause"), S.primary)
    assert view.autoplay_toggle.style == S.secondary and view.loop_toggle.style == S.secondary
    assert view.sync_state() is False, "nichts geändert → kein Edit nötig"

    cog.autoplay_enabled = True
    cog.loop_mode = "song"
    vc._playing = False
    assert view.sync_state() is True
    assert (view.pause.label, view.pause.style) == (t("button.resume"), S.success)
    assert view.autoplay_toggle.style == S.success and view.loop_toggle.style == S.success


def test_row_has_one_pause_toggle_and_back_button():
    view, *_ = _setup()
    row = view.to_components()[0]["components"][-1]
    assert [b["custom_id"].rsplit(":", 1)[1] for b in row["components"]] == [
        "back", "pause", "skip", "autoplay", "loop",
    ]


def test_toggle_resumes_when_paused():
    async def run():
        view, cog, vc, _stopped, _ctx = _setup(playing=False)
        vc.is_paused = lambda: True
        resumed = []
        vc.resume = lambda: resumed.append(True)
        interaction = FakeInteraction()

        await view.pause.callback(interaction)

        assert resumed == [True]
        assert cog.is_playing is True
        assert interaction.sent == [(t("status.resumed_eph"), {"ephemeral": True})]

    asyncio.run(run())


def test_click_updates_active_card_only():
    """Nach einem Klick wird die Karte nachgezogen – aber nur die aktive.
    Eine schon zurückgebaute Karte darf nicht mit Buttons wiederbelebt werden."""
    async def run():
        view, cog, _vc, _stopped, _ctx = _setup()
        aktive, alte = FakeMessage(1), FakeMessage(2)
        cog.now_playing_msg = aktive

        await view.loop_toggle.callback(FakeInteraction(message=aktive))
        assert aktive.edits == [{"view": view}]
        assert view.loop_toggle.style == S.success

        await view.loop_toggle.callback(FakeInteraction(message=alte))
        assert alte.edits == [], "fremde/alte Karte bleibt unangetastet"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# REQUIRE_SAME_VOICE gilt für jeden Button der Karte
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["back", "pause", "skip", "autoplay_toggle", "loop_toggle", "clear"])
def test_every_button_respects_same_voice_flag(monkeypatch, name):
    monkeypatch.setattr(config, "REQUIRE_SAME_VOICE", True)

    async def run():
        view, cog, vc, stopped, _ctx = _setup(playing=True)
        vc.channel = object()
        vc.pause = lambda: stopped.append("pause")
        cog.is_playing = True
        cog.current_track = ("u1", "Läuft", 200)
        cog.last_played = ("u0", "Davor", 100)
        cog.queue.append(("u2", "Danach"))
        interaction = FakeInteraction()        # user.voice ist None → nicht im Channel

        await getattr(view, name).callback(interaction)

        assert interaction.sent == [(t("error.same_voice_required"), {"ephemeral": True})]
        assert stopped == [] and list(cog.queue) == [("u2", "Danach")]
        assert cog.autoplay_enabled is False and cog.loop_mode is None
        assert cog.current_track == ("u1", "Läuft", 200)

    asyncio.run(run())


def test_member_in_bot_channel_may_use_buttons(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_SAME_VOICE", True)

    async def run():
        view, cog, vc, _stopped, _ctx = _setup(playing=True)
        vc.channel = object()
        interaction = FakeInteraction()
        interaction.user = types.SimpleNamespace(voice=types.SimpleNamespace(channel=vc.channel))

        await view.loop_toggle.callback(interaction)

        assert cog.loop_mode == "song"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# „Als Nächstes"
# ---------------------------------------------------------------------------

def test_up_next_line_follows_queue():
    view, cog, *_ = _setup()
    assert _details(view) == "**Dauer** 3:30\n-# Uploader"

    cog.queue.append(("u1", "Nächster_Song"))
    assert view.sync_state(up_next=cog._up_next_title()) is True
    assert _details(view) == f"**Dauer** 3:30\n**{t('embed.up_next')}** Nächster\\_Song\n-# Uploader"

    assert view.sync_state(up_next=cog._up_next_title()) is False
    cog.queue.clear()
    assert view.sync_state(up_next=cog._up_next_title()) is True
    assert _details(view) == "**Dauer** 3:30\n-# Uploader"


# ---------------------------------------------------------------------------
# ⏮ Zurück
# ---------------------------------------------------------------------------

def test_back_while_playing_queues_previous_then_current():
    async def run():
        view, cog, vc, stopped, _ctx = _setup(playing=True)
        cog.is_playing = True
        cog.last_played = ("u0", "Davor", 100)
        cog.current_track = ("u1", "Läuft", 200)
        cog.queue.append(("u2", "Danach"))
        interaction = FakeInteraction()

        await view.back.callback(interaction)

        assert list(cog.queue) == [("u0", "Davor"), ("u1", "Läuft"), ("u2", "Danach")]
        assert cog.current_track is None, "sonst reiht der Loop-Zweig den Song doppelt ein"
        assert stopped == [vc], "after_playing startet den vorherigen Song"
        assert interaction.sent == [(t("status.back_eph", title="Davor"), {"ephemeral": True})]

    asyncio.run(run())


def test_back_from_pause_does_not_start_twice():
    """Aus der Pause heraus: nur stoppen – den Start übernimmt after_playing."""
    async def run():
        view, cog, vc, stopped, _ctx = _setup(playing=False)
        vc.is_paused = lambda: True
        cog.is_playing = False
        cog.last_played = ("u0", "Davor", 100)
        cog.current_track = ("u1", "Läuft", 200)
        started = []

        async def play_next(ctx):
            started.append(ctx)

        cog.play_next = play_next
        await view.back.callback(FakeInteraction())

        assert stopped == [vc]
        assert started == []

    asyncio.run(run())


def test_back_when_idle_starts_previous():
    async def run():
        view, cog, _vc, stopped, ctx = _setup(playing=False)
        cog.last_played = ("u0", "Davor", 100)
        started = []

        async def play_next(c):
            started.append(c)

        cog.play_next = play_next
        await view.back.callback(FakeInteraction())

        assert list(cog.queue) == [("u0", "Davor")]
        assert stopped == [] and started == [ctx]

    asyncio.run(run())


def test_back_without_previous_song():
    async def run():
        view, cog, _vc, stopped, _ctx = _setup()
        interaction = FakeInteraction()

        await view.back.callback(interaction)

        assert interaction.sent == [(t("error.no_last_song"), {"ephemeral": True})]
        assert not cog.queue and stopped == []

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Rückgängig
# ---------------------------------------------------------------------------

def test_clear_all_returns_snapshot_only_if_something_to_restore():
    _view, cog, vc, _stopped, _ctx = _setup()
    assert cog._clear_all(vc) is None

    cog.current_track = ("u0", "Läuft", 100)
    cog.queue.extend([("u1", "Eins"), ("u2", "Zwei")])
    cog.loop_mode = "song"
    assert cog._clear_all(vc) == (("u0", "Läuft", 100), [("u1", "Eins"), ("u2", "Zwei")], "song")


def test_undo_restores_song_queue_and_loop():
    async def run():
        _view, cog, vc, _stopped, ctx = _setup(playing=False)
        cog.current_track = ("u0", "Läuft", 100)
        cog.queue.extend([("u1", "Eins"), ("u2", "Zwei")])
        cog.loop_mode = "song"
        snapshot = cog._clear_all(vc)
        cog.queue.append(("u9", "Neu dazu"))   # zwischen Leeren und Rückgängig eingereiht
        started = []

        async def play_next(c):
            started.append(c)

        cog.play_next = play_next
        undo = UndoClearView(cog, ctx, snapshot)
        interaction = FakeInteraction()

        await undo.undo.callback(interaction)

        assert list(cog.queue) == [("u0", "Läuft"), ("u1", "Eins"), ("u2", "Zwei"), ("u9", "Neu dazu")]
        assert cog.loop_mode == "song"
        assert interaction.edited == [{"content": t("status.clear_undone", count=3), "view": None}]
        assert started == [ctx], "nichts lief → Wiedergabe startet wieder"

    asyncio.run(run())


def test_undo_does_not_interrupt_running_song():
    async def run():
        _view, cog, vc, _stopped, ctx = _setup(playing=False)
        cog.queue.append(("u1", "Eins"))
        snapshot = cog._clear_all(vc)
        vc._playing = True          # inzwischen läuft ein neuer Song
        cog.is_playing = True
        started = []

        async def play_next(c):
            started.append(c)

        cog.play_next = play_next
        await UndoClearView(cog, ctx, snapshot).undo.callback(FakeInteraction())

        assert list(cog.queue) == [("u1", "Eins")]
        assert started == []

    asyncio.run(run())
