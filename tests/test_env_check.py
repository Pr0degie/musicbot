"""utils/env_check.py – fehlende .env-Keys beim Start nennen."""
from utils.env_check import documented_keys, missing_env_keys


def test_fehlende_keys_in_vorlagen_reihenfolge(tmp_path):
    ex = tmp_path / ".env.example"
    ex.write_text("DISCORD_TOKEN=x\n# Kommentar\nLOG_MODE=quiet\n# ALT_KEY=1\nURL_VALIDATION=warn\n",
                  encoding="utf-8")
    assert documented_keys(ex) == ["DISCORD_TOKEN", "LOG_MODE", "URL_VALIDATION"]
    assert missing_env_keys(ex, environ={"DISCORD_TOKEN": "t"}) == ["LOG_MODE", "URL_VALIDATION"]


def test_leer_gesetzter_key_gilt_als_vorhanden(tmp_path):
    """ADMIN_ROLE_ID= in der .env ist eine bewusste Entscheidung, kein Vergessen."""
    ex = tmp_path / ".env.example"
    ex.write_text("ADMIN_ROLE_ID=\n", encoding="utf-8")
    assert missing_env_keys(ex, environ={"ADMIN_ROLE_ID": ""}) == []


def test_ohne_vorlage_kein_fehler(tmp_path):
    assert missing_env_keys(tmp_path / "gibts-nicht", environ={}) == []


def test_echte_vorlage_deckt_alle_config_keys_ab():
    """Jeder Key, den config.py liest, steht in .env.example – sonst sähe ihn
    der Start-Hinweis nie."""
    import re
    from pathlib import Path

    gelesen = set(re.findall(r'os\.getenv\(\s*"([A-Z_]+)"', Path("config.py").read_text(encoding="utf-8")))
    assert gelesen <= set(documented_keys(".env.example"))
