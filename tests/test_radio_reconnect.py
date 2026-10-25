"""Radio-Reconnect-Kappe (cogs/music_radio.py → _radio_should_reconnect).

Früher setzte _play_radio_stream den Zähler nach jedem play() zurück – und
der Reconnect startet über genau diese Funktion. Ein Sender, der sich
verbindet und sofort wieder abbricht, wurde so endlos neu verbunden.
"""
from cogs.music_radio import RADIO_MAX_RECONNECTS, RADIO_STABLE_SECONDS
from tests.conftest import build_cog


def test_sofort_abbrechender_sender_gibt_nach_der_kappe_auf():
    mc = build_cog()
    mc._radio_reconnect_count = 0
    versuche = [mc._radio_should_reconnect(ran_seconds=1) for _ in range(RADIO_MAX_RECONNECTS + 2)]
    assert versuche == [True] * RADIO_MAX_RECONNECTS + [False, False]


def test_stabiler_lauf_gibt_wieder_volle_versuche():
    """Ein Sender, der stundenlang lief und dann einmal abbricht, ist ein neuer Vorfall."""
    mc = build_cog()
    mc._radio_reconnect_count = RADIO_MAX_RECONNECTS
    assert mc._radio_should_reconnect(ran_seconds=RADIO_STABLE_SECONDS) is True
    assert mc._radio_reconnect_count == 1
