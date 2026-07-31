"""Tests für die Terminal-Modi (LOG_MODE / !debug): quiet = nur WARNING+ im
Terminal, debug = volle Diagnose. bot.log (File-Handler) bleibt immer voll."""

import logging

from utils import logger as ulog
from cogs.downloader import _version_tuple


def test_set_console_mode_switches_console_level_only():
    before = ulog.get_console_mode()
    try:
        assert ulog.set_console_mode("debug") == "debug"
        assert ulog._console.level == logging.INFO
        assert ulog.get_console_mode() == "debug"

        assert ulog.set_console_mode("quiet") == "quiet"
        assert ulog._console.level == logging.WARNING

        # Unbekannter Modus fällt auf quiet zurück
        assert ulog.set_console_mode("unsinn") == "quiet"
        assert ulog._console.level == logging.WARNING

        # File-Handler (bot.log) bleibt unangetastet – immer volle Diagnose
        assert ulog._file.level == logging.NOTSET
    finally:
        ulog.set_console_mode(before)


def test_version_tuple_for_update_check():
    assert _version_tuple("2026.7.15") == (2026, 7, 15)
    assert _version_tuple("2026.7.15") < _version_tuple("2026.10.1")
    assert _version_tuple("kaputt.version") is None
    assert _version_tuple(None) is None
