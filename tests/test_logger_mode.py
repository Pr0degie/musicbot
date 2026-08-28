"""Tests für die Terminal-Modi (LOG_MODE / !debug).

quiet = wesentliche Ereignisse + alle echten Probleme (WARNING+),
debug = volle Diagnose. bot.log (File-Handler) bleibt in beiden Modi voll.

Der Modus wirkt über einen Tag-Filter auf dem Console-Handler, nicht über
dessen Level – ein reiner Level-Filter würde den kompletten Normalbetrieb
(Songwechsel, Queue, Radio) verschlucken, weil der INFO ist.
"""

import logging
import re
from pathlib import Path

from utils import logger as ulog
from cogs.downloader import _version_tuple

_ROOT = Path(__file__).resolve().parent.parent


def _record(level, msg):
    return logging.LogRecord("DiscordMusicBot", level, __file__, 1, msg, (), None)


def _shows(level, msg):
    """True, wenn der Console-Handler diesen Record im aktuellen Modus ausgibt."""
    record = _record(level, msg)
    return record.levelno >= ulog._console.level and bool(ulog._console.filter(record))


def test_quiet_shows_essential_info_events():
    before = ulog.get_console_mode()
    try:
        ulog.set_console_mode("quiet")
        assert _shows(logging.INFO, "[Nächster Track] Songtitel (https://example.invalid)")
        assert _shows(logging.INFO, "[Wiedergabe] Starte: Songtitel")
        assert _shows(logging.INFO, "[Queue] Leere Warteschlange. Wiedergabe gestoppt.")
    finally:
        ulog.set_console_mode(before)


def test_quiet_hides_pipeline_noise():
    before = ulog.get_console_mode()
    try:
        ulog.set_console_mode("quiet")
        assert not _shows(logging.INFO, "[Prefetch] Lade vor: Songtitel")
        assert not _shows(logging.INFO, "[Progressiv] Starte Hintergrund-Download: Songtitel")
        assert not _shows(logging.INFO, "[Resolve] Metadaten aus Cache: Songtitel")
        assert not _shows(logging.INFO, "[SAVE] Queue automatisch gespeichert nach Track-Ende.")
    finally:
        ulog.set_console_mode(before)


def test_quiet_always_shows_problems():
    """WARNING+ passiert den Filter unabhängig vom Tag – auch für stille Tags."""
    before = ulog.get_console_mode()
    try:
        ulog.set_console_mode("quiet")
        assert _shows(logging.WARNING, "[Progressiv] Download fehlgeschlagen für Songtitel")
        assert _shows(logging.ERROR, "[Fehler bei play_next]")
        assert _shows(logging.CRITICAL, "[INIT] Totalschaden")
    finally:
        ulog.set_console_mode(before)


def test_quiet_hides_untagged_info():
    """Ohne [Tag] ist eine INFO-Zeile nicht als wesentlich ausgewiesen."""
    before = ulog.get_console_mode()
    try:
        ulog.set_console_mode("quiet")
        assert not _shows(logging.INFO, "irgendeine Fremdbibliothek plaudert")
    finally:
        ulog.set_console_mode(before)


def test_debug_shows_everything():
    before = ulog.get_console_mode()
    try:
        ulog.set_console_mode("debug")
        assert _shows(logging.INFO, "[Prefetch] Lade vor: Songtitel")
        assert _shows(logging.INFO, "[Nächster Track] Songtitel")
        assert _shows(logging.INFO, "irgendeine Fremdbibliothek plaudert")
        assert _shows(logging.WARNING, "[Progressiv] Download fehlgeschlagen")
    finally:
        ulog.set_console_mode(before)


def test_set_console_mode_switches_mode_only():
    before = ulog.get_console_mode()
    try:
        assert ulog.set_console_mode("debug") == "debug"
        assert ulog.get_console_mode() == "debug"

        assert ulog.set_console_mode("quiet") == "quiet"
        assert ulog.get_console_mode() == "quiet"

        # Unbekannter Modus fällt auf quiet zurück
        assert ulog.set_console_mode("unsinn") == "quiet"
        assert ulog.get_console_mode() == "quiet"

        # Der Console-Handler filtert über Tags, nicht über sein Level:
        # WARNING als Handler-Level würde den Normalbetrieb verschlucken.
        assert ulog._console.level == logging.INFO

        # File-Handler (bot.log) bleibt unangetastet – immer volle Diagnose
        assert ulog._file.level == logging.NOTSET
    finally:
        ulog.set_console_mode(before)


def test_tag_sets_are_disjoint():
    assert not (ulog._ESSENTIAL_TAGS & ulog._QUIET_TAGS)


def test_every_info_tag_is_classified():
    """Jeder im Code benutzte INFO-Tag muss genau einer Liste zugeordnet sein.

    Sonst verschwindet eine neu eingebaute Meldung stillschweigend aus dem
    Terminal – genau der Fehler, den dieser Filter verhindern soll.
    """
    pattern = re.compile(r"logger\.info\(\s*f?\"\[([^]{]+)\]")
    known = ulog._ESSENTIAL_TAGS | ulog._QUIET_TAGS
    unclassified = {}

    for path in _ROOT.rglob("*.py"):
        if "tests" in path.parts or ".venv" in path.parts or "venv" in path.parts:
            continue
        for tag in pattern.findall(path.read_text(encoding="utf-8")):
            if tag not in known:
                unclassified.setdefault(tag, path.relative_to(_ROOT).as_posix())

    assert not unclassified, f"Nicht klassifizierte INFO-Tags: {unclassified}"


def test_dynamic_search_tags_are_essential():
    """[{log_tag}] löst sich zu p/next/now auf – die Sucheinstiege des Users."""
    for tag in ("p", "next", "now"):
        assert tag in ulog._ESSENTIAL_TAGS


def test_version_tuple_for_update_check():
    assert _version_tuple("2026.7.15") == (2026, 7, 15)
    assert _version_tuple("2026.7.15") < _version_tuple("2026.10.1")
    assert _version_tuple("kaputt.version") is None
    assert _version_tuple(None) is None
