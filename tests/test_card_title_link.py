r"""Titelzeile der Now-Playing-Karte: Titel bleibt ein Link aufs Video.

Discord stellt einen Markdown-Link [Text](<url>) nicht dar, sobald im Text
Backslash-Escapes stehen (z. B. „\|" aus escape_markdown) – dann erscheint
der Link als Rohtext. Live gesehen 22.11.2026 bei einem Titel mit „|".
"""

import asyncio
import re
import types

import pytest

from tests.conftest import FakeCtx, FakeVoiceClient
from tests.test_music_playback import make_cog
from utils.text import link_label


@pytest.mark.parametrize("title, expected", [
    ("Il Valzer 💗 | Musica Folk | Organetto", "Il Valzer 💗 | Musica Folk | Organetto"),
    ("Song [Official Video]", "Song (Official Video)"),
    ("A ** B", "A ∗∗ B"),
    ("x || y __ z ~~ w", "x | | y _ _ z ~ ~ w"),
    (r"back\slash `code`", "back/slash 'code'"),
    (None, ""),
])
def test_link_label(title, expected):
    assert link_label(title) == expected


@pytest.mark.parametrize("title", [
    "Il Valzer del Primo Amore 💗 🍂 | Musica Folk Italiana | Organetto Vintage",
    "Artist - Song [Official Video] (Lyrics) *HD*",
    r"back\slash || spoiler",
])
def test_heading_is_markdown_link_without_backslashes(title):
    async def run():   # make_cog braucht einen laufenden Loop
        mc = make_cog(types.SimpleNamespace())
        mc.now_playing_msg = None
        mc._up_next_title = lambda: None
        info = {"webpage_url": "https://www.youtube.com/watch?v=d6jbKAUM0gM", "uploader": "OLD FLAME RADIO 70"}
        view = mc._build_np_view(FakeCtx(FakeVoiceClient()), info, info["webpage_url"], title, 247)
        heading = view.to_components()[0]["components"][0]["components"][0]["content"]
        m = re.fullmatch(r"### 🎵 \[([^\[\]]+)\]\(<(\S+)>\)", heading)
        assert m, f"kein sauberer Link: {heading!r}"
        assert "\\" not in m.group(1)
        assert m.group(2) == info["webpage_url"]

    asyncio.run(run())
