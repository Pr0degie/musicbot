"""Tests für die Config-Parser der Sprachsteuerung.

Leitlinie wie im Rest von config.py: ungültige Eingaben werfen nie, sondern
fallen auf einen sicheren Default zurück. Und laut ADR 0004 erhält der Default
jedes neuen Flags das bisherige Verhalten – die Sprachsteuerung ist aus.
"""
import pytest

import config
from utils.nl_parser import WAKE_DEFAULTS


# --- ADR 0004: Defaults erhalten das Altverhalten ---------------------------

def test_sprachsteuerung_ist_standardmaessig_aus():
    assert config.VOICE_CONTROL is False


def test_eigenes_zuhoeren_ist_standardmaessig_aus():
    assert config.VOICE_OWN_LISTEN is False


def test_ohne_dm_bot_id_keine_weiche():
    assert config.DM_BOT_USER_ID == 0


# --- _parse_id_set ----------------------------------------------------------

@pytest.mark.parametrize("roh,erwartet", [
    ("", frozenset()),
    (None, frozenset()),
    ("123", frozenset({123})),
    ("123,456", frozenset({123, 456})),
    ("123, 456 ,, 789", frozenset({123, 456, 789})),
    ("123,abc,456", frozenset({123, 456})),   # Muell wird still verworfen
    ("abc", frozenset()),
])
def test_parse_id_set(roh, erwartet):
    assert config._parse_id_set(roh) == erwartet


# --- _parse_csv -------------------------------------------------------------

def test_parse_csv_leer_faellt_auf_default():
    assert config._parse_csv("", WAKE_DEFAULTS) == WAKE_DEFAULTS
    assert config._parse_csv("   ", WAKE_DEFAULTS) == WAKE_DEFAULTS


def test_parse_csv_trimmt_und_verwirft_leere_teile():
    assert config._parse_csv("yo bot, hey bot ,, ", ("x",)) == ("yo bot", "hey bot")


# --- _parse_user_id ---------------------------------------------------------

@pytest.mark.parametrize("roh,erwartet", [
    ("", 0),
    ("   ", 0),
    ("abc", 0),
    ("-5", 0),
    ("123456789012345678", 123456789012345678),
])
def test_parse_user_id(roh, erwartet):
    assert config._parse_user_id(roh) == erwartet


# --- Weckwoerter ------------------------------------------------------------

def test_weckwoerter_haben_die_gemeinsamen_defaults():
    """Muss mit Bot Bs Liste uebereinstimmen - driften sie, reagiert der Bot
    einfach nicht mehr und niemand weiss warum."""
    assert config.VOICE_WAKE_WORDS == WAKE_DEFAULTS
