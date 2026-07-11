"""Konsistenz-Tests für die Locale-Dateien.

Beide Locales müssen dieselben Keys mit denselben Platzhaltern haben, und
jeder im Code direkt verwendete t("…")-Key muss in beiden existieren.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "locales"


def _load(lang):
    with open(LOCALES / f"{lang}.json", encoding="utf-8") as f:
        return json.load(f)


def _placeholders(template):
    return set(re.findall(r"\{(\w+)\}", template))


def test_locales_have_identical_key_sets():
    de, en = _load("de"), _load("en")
    assert set(de) == set(en), (
        f"nur in de: {sorted(set(de) - set(en))}, nur in en: {sorted(set(en) - set(de))}"
    )


def test_locales_have_identical_placeholders_per_key():
    de, en = _load("de"), _load("en")
    mismatches = {
        key: (_placeholders(de[key]), _placeholders(en[key]))
        for key in de
        if _placeholders(de[key]) != _placeholders(en[key])
    }
    assert not mismatches, f"Platzhalter weichen ab: {mismatches}"


def test_all_code_keys_exist_in_both_locales():
    de, en = _load("de"), _load("en")
    key_re = re.compile(r"""(?<![\w.])t\(\s*["']([\w.]+)["']""")
    used = set()
    for pattern in ("cogs/*.py", "views/*.py", "utils/*.py", "main.py"):
        for path in ROOT.glob(pattern):
            used |= set(key_re.findall(path.read_text(encoding="utf-8")))
    missing_de = used - set(de)
    missing_en = used - set(en)
    assert not missing_de and not missing_en, (
        f"fehlt in de.json: {sorted(missing_de)}, fehlt in en.json: {sorted(missing_en)}"
    )
    assert used, "kein einziger t()-Key gefunden — Regex kaputt?"
