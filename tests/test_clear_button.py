"""Tests für den ❌-Button (grau, rotes Kreuz) der Now-Playing-Karte.

Der ❌ sitzt als Accessory oben rechts in der Titel-Section (Components V2)
und tut dasselbe wie `!clear` – über den gemeinsamen Kern `_clear_all`,
inklusive REQUIRE_SAME_VOICE (Default aus, Owner immer durch).
"""

import asyncio
import types
from unittest.mock import MagicMock

import config
from tests.conftest import FakeCtx, FakeVoiceClient, build_cog
from utils.i18n import t
from views.music_controls import (
    NP_HEADING_ID, MusicControlView, UndoClearView, _stale_card_title,
)


class FakeInteraction:
    def __init__(self, user=None, is_owner=False):
        self.sent = []
        self.response = types.SimpleNamespace(send_message=self._send)
        self.user = user or types.SimpleNamespace(voice=None)

        async def _is_owner(_user):
            return is_owner

        self.client = types.SimpleNamespace(is_owner=_is_owner)

    async def _send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


def _playing_setup():
    vc = FakeVoiceClient()
    vc._playing = True
    vc.channel = object()
    cog = build_cog()
    cog.dl = types.SimpleNamespace(clear_cache=lambda: None)
    cog.queue.extend([("u1", "Eins"), ("u2", "Zwei")])
    cog.is_playing = True
    cog.current_track = ("u0", "Läuft", 100)
    cog.loop_mode = "song"
    stopped = []
    cog._stop_for_advance = stopped.append
    cog._stop_radio = lambda: None
    return MusicControlView(cog, FakeCtx(voice_client=vc), heading="### 🎵 Läuft"), cog, vc, stopped


def _assert_cleared_with_undo(interaction):
    """Öffentliche Geleert-Meldung mit Rückgängig-Button."""
    (content, kwargs), = interaction.sent
    assert content == t("status.cleared")
    assert isinstance(kwargs["view"], UndoClearView) and "ephemeral" not in kwargs


# ---------------------------------------------------------------------------
# Layout: ❌ oben rechts
# ---------------------------------------------------------------------------

def test_clear_button_is_accessory_of_title_section():
    view = MusicControlView(
        MagicMock(), MagicMock(), heading="### 🎵 Titel", body="▬▬● 0:00 / 3:30",
        details="**Dauer** 3:30", thumbnail="https://img.example/x.jpg",
    )
    container = view.to_components()[0]
    assert container["type"] == 17
    title_section = container["components"][0]
    assert title_section["type"] == 9
    assert title_section["components"][0] == {"type": 10, "content": "### 🎵 Titel", "id": NP_HEADING_ID}
    accessory = title_section["accessory"]
    assert accessory["type"] == 2 and accessory["style"] == 2, "grauer Button"
    assert accessory["emoji"]["name"] == "❌", "rotes Kreuz"
    assert accessory["custom_id"].endswith(":clear")
    # Die Steuer-Buttons bleiben in einer Zeile darunter.
    row = container["components"][-1]
    assert [b["custom_id"].rsplit(":", 1)[1] for b in row["components"]] == [
        "back", "pause", "skip", "autoplay", "loop",
    ]


def test_set_body_updates_progress_line():
    view = MusicControlView(MagicMock(), MagicMock(), heading="### 🎵 Titel")
    assert view.body is None
    view.set_body("▬● 0:05 / 3:30")           # ohne Startbalken: Zeile wird ergänzt
    view.set_body("▬▬● 0:10 / 3:30")
    texts = [c["content"] for c in view.to_components()[0]["components"][0]["components"]]
    assert texts == ["### 🎵 Titel", "▬▬● 0:10 / 3:30"]


def test_unusable_thumbnail_url_is_dropped():
    """V2 lehnt eine Nachricht mit unbrauchbarer Media-URL komplett ab –
    dann lieber eine Karte ohne Bild als gar keine."""
    for bad in (None, "", "C:/cover.jpg"):
        view = MusicControlView(MagicMock(), MagicMock(), heading="### 🎵 T", details="d", thumbnail=bad)
        details = view.to_components()[0]["components"][1]
        assert details == {"type": 10, "content": "d"}


# ---------------------------------------------------------------------------
# Klick: dasselbe wie !clear
# ---------------------------------------------------------------------------

def test_clear_button_clears_like_command():
    async def run():
        view, cog, vc, stopped = _playing_setup()
        interaction = FakeInteraction()

        await view.clear.callback(interaction)

        assert not cog.queue
        assert cog.current_track is None
        assert cog.is_playing is False
        assert cog.loop_mode is None
        assert cog._stopped_by_user is True
        assert stopped == [vc], "Wiedergabe-Stopp nur über _stop_for_advance"
        _assert_cleared_with_undo(interaction)

    asyncio.run(run())


def test_clear_command_uses_same_core():
    async def run():
        _view, cog, vc, stopped = _playing_setup()
        ctx = FakeCtx(voice_client=vc)

        await cog.clear.callback(cog, ctx)

        assert not cog.queue and cog.current_track is None
        assert stopped == [vc]
        assert ctx.texts == [t("status.cleared")]

    asyncio.run(run())


def test_clear_button_respects_same_voice_flag(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_SAME_VOICE", True)

    async def run():
        view, cog, _vc, stopped = _playing_setup()
        fremder = types.SimpleNamespace(voice=None)   # nicht im Voice-Channel
        interaction = FakeInteraction(user=fremder)

        await view.clear.callback(interaction)

        assert len(cog.queue) == 2 and cog.current_track is not None, "nichts angefasst"
        assert stopped == []
        assert interaction.sent == [(t("error.same_voice_required"), {"ephemeral": True})]

    asyncio.run(run())


def test_clear_button_owner_always_passes(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_SAME_VOICE", True)

    async def run():
        view, cog, _vc, _stopped = _playing_setup()
        interaction = FakeInteraction(is_owner=True)

        await view.clear.callback(interaction)

        assert not cog.queue
        _assert_cleared_with_undo(interaction)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Stale-Fallback: Titel aus einer alten V2-Karte
# ---------------------------------------------------------------------------

def _comp(**kwargs):
    return types.SimpleNamespace(**kwargs)


def test_stale_title_is_read_from_heading():
    heading = _comp(id=NP_HEADING_ID, content="### 🎵 [Song – Artist](<https://x>)")
    section = _comp(id=3, children=[heading, _comp(id=4, content="▬● 0:10")])
    container = _comp(id=2, children=[section])
    assert _stale_card_title([container]) == "[Song – Artist](<https://x>)"


def test_stale_title_missing_heading():
    assert _stale_card_title([_comp(id=7, children=[_comp(id=8, content="x")])]) is None
