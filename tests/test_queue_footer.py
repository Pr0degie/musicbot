"""Tests für den !q-Footer: Gesamtdauer nur aus gecachten Metadaten.

Drei Fälle: alle Dauern bekannt → exakte Summe; teilweise bekannt →
"≥ Summe (n ohne Angabe)"; keine bekannt → Footer ohne Dauer (wie früher).
"""

from utils.i18n import t
from views.queue_view import QueueView


def _footer(items, current=None, loop_mode=None):
    return QueueView(items, current, loop_mode)._footer_text()


def test_footer_all_durations_known():
    items = [("u1", "A", 100), ("u2", "B", 50)]
    current = ("u0", "Now", 30)
    assert _footer(items, current) == t(
        "embed.queue_footer_duration", total=2, duration="3:00", loop=t("embed.loop_off")
    )


def test_footer_partial_durations_shows_lower_bound():
    items = [("u1", "A", 100), ("u2", "B", None), ("u3", "C", 20)]
    assert _footer(items) == t(
        "embed.queue_footer_duration_partial",
        total=3, duration="2:00", missing=1, loop=t("embed.loop_off"),
    )


def test_footer_no_durations_keeps_old_format():
    items = [("u1", "A"), ("u2", "B")]
    assert _footer(items) == t("embed.queue_footer", total=2, loop=t("embed.loop_off"))


def test_footer_hours_format():
    items = [("u1", "A", 3600), ("u2", "B", 61)]
    assert _footer(items) == t(
        "embed.queue_footer_duration", total=2, duration="1:01:01", loop=t("embed.loop_off")
    )
