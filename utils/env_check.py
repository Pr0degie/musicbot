"""Welche Einstellungen aus .env.example fehlen in der eigenen .env?

Neue Config-Keys laufen still auf ihrem Default, solange sie niemand in die
.env einträgt – darunter die Security-Flags, die bewusst auf Altverhalten
stehen (ADR 0004). Der Bot nennt die fehlenden Keys deshalb einmal beim Start.
"""
import os
import re
from pathlib import Path

_KEY_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=")


def documented_keys(example_path=".env.example") -> list[str]:
    """Keys der Vorlage in Dateireihenfolge (auskommentierte Zeilen zählen nicht)."""
    try:
        lines = Path(example_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    keys = []
    for line in lines:
        m = _KEY_RE.match(line)
        if m and m.group(1) not in keys:
            keys.append(m.group(1))
    return keys


def missing_env_keys(example_path=".env.example", environ=None) -> list[str]:
    """Dokumentierte Keys, die weder in der .env noch in der Umgebung gesetzt
    sind. config.py hat die .env zu diesem Zeitpunkt schon per load_dotenv
    in os.environ geladen."""
    environ = os.environ if environ is None else environ
    return [k for k in documented_keys(example_path) if k not in environ]
