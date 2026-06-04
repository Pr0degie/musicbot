"""Tests für die EQ-Preset-Filterchains (cogs/presets.py).

Sichert die in presets.py dokumentierten Invarianten ab:
- "flat" ist leer (→ play_next nutzt codec="copy", verlustfreies Durchreichen)
- jedes aktive Preset beginnt mit asetpts=N/SR/TB (NICHT entfernen!)
- jedes aktive Preset gibt explizit auf 48 kHz aus (aresample=48000, für Discord)
"""

import pytest

from cogs.presets import EQ_PRESETS

# Aus CLAUDE.md / presets.py dokumentierte Preset-Namen.
EXPECTED_PRESETS = {
    "bassboost", "flat", "vocalboost", "superbass",
    "punchy", "nightcore", "karaoke", "8d",
}

ACTIVE_PRESETS = sorted(k for k in EQ_PRESETS if k != "flat")


def test_all_expected_presets_present():
    assert EXPECTED_PRESETS <= set(EQ_PRESETS)


def test_flat_is_empty():
    # Leer-String ist das Signal für codec="copy" in play_next – kein Filter.
    assert EQ_PRESETS["flat"] == ""


def test_values_are_strings():
    assert all(isinstance(v, str) for v in EQ_PRESETS.values())


@pytest.mark.parametrize("name", ACTIVE_PRESETS)
def test_active_preset_starts_with_asetpts(name):
    assert EQ_PRESETS[name].startswith("-af asetpts=N/SR/TB")


@pytest.mark.parametrize("name", ACTIVE_PRESETS)
def test_active_preset_outputs_48khz(name):
    assert "aresample=48000" in EQ_PRESETS[name]
