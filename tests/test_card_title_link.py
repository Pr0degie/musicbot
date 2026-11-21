r"""Titelzeile der Now-Playing-Karte: Titel bleibt ein Link aufs Video.

Discord stellt einen Markdown-Link [Text](<url>) nicht dar, sobald im Text
Emojis stehen – dann erscheint der Link als Rohtext. Live getestet 22.11.2026:
derselbe Titel ohne Emojis wurde Link, mit Emojis (auch kurz) nicht.
Backslash-Escapes (\| \[) vermeidet link_label vorsorglich ebenfalls.
"""

import asyncio
import re
import types

import pytest

from tests.conftest import FakeCtx, FakeVoiceClient
from tests.test_music_playback import make_cog
from utils.text import link_label

_EMOJI_LEFT = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


@pytest.mark.parametrize("title, expected", [
    ("Il Valzer 💗 🍂 | Musica Folk | Organetto", "Il Valzer | Musica Folk | Organetto"),
    ("🔴 Saxofón al Amanecer EN VIVO 🌅 Boleros 24/7", "Saxofón al Amanecer EN VIVO Boleros 24/7"),
    ("Italy 🇮🇹 ❤️ 👍🏽 👨‍👩‍👧 1️⃣ Song", "Italy 1 Song"),
    ("Song [Official Video]", "Song (Official Video)"),
    ("A ** B", "A ∗∗ B"),
    ("x || y __ z ~~ w", "x | | y _ _ z ~ ~ w"),
    (r"back\slash `code`", "back/slash 'code'"),
    ("Saxofón – Música Ñandú", "Saxofón – Música Ñandú"),
    ("🔥🔥🔥", ""),
    (None, ""),
])
def test_link_label(title, expected):
    assert link_label(title) == expected


def _heading(title, page="https://www.youtube.com/watch?v=YqJ-XdoZV00", uploader="Magic Boleros"):
    async def run():   # make_cog braucht einen laufenden Loop
        mc = make_cog(types.SimpleNamespace())
        mc.now_playing_msg = None
        mc._up_next_title = lambda: None
        info = {"webpage_url": page, "uploader": uploader}
        view = mc._build_np_view(FakeCtx(FakeVoiceClient()), info, page or "x", title, 699)
        return view.to_components()[0]["components"][0]["components"][0]["content"]

    return asyncio.run(run())


@pytest.mark.parametrize("title", [
    "🔴 Saxofón al Amanecer EN VIVO 🌅 Boleros Suaves para Despertar 24/7",
    "Il Valzer del Primo Amore 💗 🍂 | Musica Folk Italiana | Organetto Vintage",
    "Artist - Song [Official Video] (Lyrics) *HD*",
    r"back\slash || spoiler",
])
def test_heading_is_clean_markdown_link(title):
    heading = _heading(title)
    m = re.fullmatch(r"### 🎵 \[([^\[\]]+)\]\(<(\S+)>\)", heading)
    assert m, f"kein sauberer Link: {heading!r}"
    assert "\\" not in m.group(1)
    assert not _EMOJI_LEFT.search(m.group(1)), f"Emoji im Link-Text: {m.group(1)!r}"
    assert m.group(2) == "https://www.youtube.com/watch?v=YqJ-XdoZV00"


def test_emoji_only_title_falls_back_to_plain_heading():
    """Bleibt ohne Emojis nichts übrig, lieber Titel ohne Link als leerer Link."""
    assert _heading("🔥🔥🔥", uploader=None) == "### 🎵 🔥🔥🔥"
