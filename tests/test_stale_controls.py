"""Tests für den Stale-Buttons-Fallback (tote Buttons nach Bot-Neustart).

Die Garantie, dass StaleControlsFallback nie Klicks einer lebenden View
abfängt, hängt am custom_id-Namensraum: Live-Buttons tragen die BOOT_ID des
laufenden Prozesses, das Fallback-Template schließt genau diese per negative
lookahead aus. Diese Tests dokumentieren beide Richtungen.
"""

from unittest.mock import MagicMock

from views.music_controls import BOOT_ID, MusicControlView, StaleControlsFallback

TEMPLATE = StaleControlsFallback.__discord_ui_compiled_template__


def _make_view():
    return MusicControlView(MagicMock(), MagicMock())


def test_live_custom_ids_never_match_fallback_template():
    view = _make_view()
    assert len(view.children) == 5
    for child in view.children:
        assert child.custom_id.startswith(f"musicctl:{BOOT_ID}:")
        assert TEMPLATE.fullmatch(child.custom_id) is None


def test_foreign_boot_id_matches_fallback_template():
    """custom_ids aus einem früheren Bot-Lauf (andere BOOT_ID) → Fallback greift."""
    view = _make_view()
    for child in view.children:
        stale_id = child.custom_id.replace(BOOT_ID, "0" * 8, 1)
        assert TEMPLATE.fullmatch(stale_id) is not None


def test_discordpy_auto_ids_never_match_fallback_template():
    """Auto-generierte custom_ids anderer Views (QueueView, HelpView, 32 Hex-Zeichen)
    dürfen den Fallback nie triggern."""
    assert TEMPLATE.fullmatch("a" * 32) is None
    assert TEMPLATE.fullmatch("0123456789abcdef0123456789abcdef") is None


def test_custom_ids_unique_per_message():
    ids_a = {c.custom_id for c in _make_view().children}
    ids_b = {c.custom_id for c in _make_view().children}
    assert len(ids_a) == 5
    assert ids_a.isdisjoint(ids_b)
