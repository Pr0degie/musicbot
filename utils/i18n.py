import json
from pathlib import Path

import config

_strings: dict = {}


def _load() -> None:
    global _strings
    lang = getattr(config, "LANGUAGE", "en")
    base = Path(__file__).parent.parent / "locales"
    path = base / f"{lang}.json"
    fallback = base / "en.json"
    try:
        with open(path, encoding="utf-8") as f:
            _strings = json.load(f)
    except FileNotFoundError:
        with open(fallback, encoding="utf-8") as f:
            _strings = json.load(f)


_load()


def t(key: str, **kwargs) -> str:
    template = _strings.get(key, key)
    return template.format(**kwargs) if kwargs else template
