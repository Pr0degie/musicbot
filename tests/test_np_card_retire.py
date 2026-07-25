"""Tests für den Rückbau der Now-Playing-Karte.

Regel: Karte (Embed + Buttons) gibt es **nur für den aktuellen Song**. Jede
Nachricht, die ein Track hinterlässt, wird zur Textzeile `🎶 Titel` –
Trackwechsel, Queue-Ende, Radio-Übernahme und die 429-Abschaltung gehen alle
durch `_retire_np_message`. Der Inhalt darf dabei nie leer werden: eine
Nachricht ohne Text, Embed und Anhang lehnt Discord mit 400 ab, der Rückbau
schlüge still fehl und die Karte bliebe samt Buttons stehen.
"""

import asyncio

from conftest import FakeCtx, FakeMessage, build_cog
from utils.i18n import t


def last_edit(msg):
    assert msg.edits, "Nachricht wurde nie editiert – Karte bliebe stehen"
    return msg.edits[-1]


def assert_reduced_to_text(msg, expected_title=None):
    """Karte weg, Buttons weg, Inhalt nicht leer."""
    edit = last_edit(msg)
    assert edit["embed"] is None, "Embed muss verschwinden"
    assert edit["view"] is None, "Buttons muss die alte Nachricht verlieren"
    content = edit.get("content")
    assert content, "Inhalt darf nie leer sein – Discord lehnt das mit 400 ab"
    assert content.startswith("🎶 ")
    if expected_title is not None:
        assert content == f"🎶 {expected_title}"


# ---------------------------------------------------------------------------
# _retire_np_message
# ---------------------------------------------------------------------------

def test_retire_reduces_card_to_text_line():
    async def run():
        mc = build_cog()
        msg = FakeMessage()

        await mc._retire_np_message(msg, "Sade - Smooth Operator")

        assert_reduced_to_text(msg, "Sade - Smooth Operator")

    asyncio.run(run())


def test_retire_without_title_still_sends_content():
    """Unbekannter Titel darf nicht zu einer leeren Nachricht führen."""
    async def run():
        mc = build_cog()
        msg = FakeMessage()

        await mc._retire_np_message(msg, None)

        assert_reduced_to_text(msg, t("misc.unknown_title"))

    asyncio.run(run())


def test_retire_ignores_missing_message():
    async def run():
        await build_cog()._retire_np_message(None, "egal")   # darf nicht werfen

    asyncio.run(run())


def test_retire_survives_discord_error():
    async def run():
        mc = build_cog()
        msg = FakeMessage()

        async def broken_edit(**kwargs):
            raise RuntimeError("404 Not Found")

        msg.edit = broken_edit
        await mc._retire_np_message(msg, "Weg")   # geschluckt, kein Crash

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 429-Abschaltung: Nachricht darf nicht verloren gehen
# ---------------------------------------------------------------------------

def test_rate_limited_card_is_handed_over_for_retirement():
    mc = build_cog()
    msg = FakeMessage()
    mc.now_playing_msg = msg
    mc.now_playing_embed = object()
    mc._np_title = "Gedrosselt"

    mc._disable_np_edits(msg)

    assert mc.now_playing_msg is None, "Live-Edits müssen aufhören"
    assert mc.now_playing_embed is None
    assert mc._ended_np == (msg, "Gedrosselt"), \
        "Karte muss dem nächsten Track zum Rückbau übergeben werden"
    assert mc._np_title is None


def test_disable_np_edits_ignores_foreign_message():
    mc = build_cog()
    mine, foreign = FakeMessage(), FakeMessage()
    mc.now_playing_msg = mine
    mc._np_title = "Meins"

    mc._disable_np_edits(foreign)

    assert mc.now_playing_msg is mine
    assert mc._ended_np is None


# ---------------------------------------------------------------------------
# Queue-Ende: Karte wandert mit ihrem Titel nach _ended_np
# ---------------------------------------------------------------------------

def test_queue_empty_hands_card_over_with_its_own_title():
    """Der Titel kommt aus _np_title, nicht aus last_played – sonst stünde
    nach einem Cleanup ohne last_played kein Text zur Verfügung."""
    async def run():
        mc = build_cog()
        msg = FakeMessage()
        mc.now_playing_msg = msg
        mc.now_playing_embed = object()
        mc._np_title = "Letzter Song"
        mc.current_track = None
        mc.last_played = None
        mc.autoplay_enabled = False

        await mc._handle_queue_empty(FakeCtx())

        assert mc._ended_np == (msg, "Letzter Song")
        assert mc.now_playing_msg is None and mc._np_title is None

    asyncio.run(run())
