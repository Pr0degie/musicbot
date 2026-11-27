"""🔗-Button der Now-Playing-Karte: legt dem Klickenden den Song-Link hin.

Ein Bot kann nicht in die Zwischenablage schreiben – der Button antwortet
deshalb ephemer mit dem Link als Codeblock (am Desktop mit Kopier-Symbol).
Er sitzt als Accessory rechts neben den Details; die Button-Zeile darunter
ist mit fünf Buttons voll und bleibt, wie sie ist.
"""

import asyncio
import types
from unittest.mock import MagicMock

import pytest

import config
from tests.conftest import FakeCtx, FakeVoiceClient, build_cog
from tests.test_np_card_controls import FakeInteraction
from utils.i18n import t
from views.music_controls import MusicControlView

YT = "https://www.youtube.com/watch?v=abcdefghijk"
ROW = ["back", "pause", "skip", "autoplay", "loop"]


def _actions(row):
    return [b["custom_id"].rsplit(":", 1)[1] for b in row["components"]]


def _all_actions(node):
    """Aktionen aller Buttons der Karte, egal wie tief verschachtelt."""
    found = []
    stack = [node]
    while stack:
        comp = stack.pop()
        if comp.get("custom_id"):
            found.append(comp["custom_id"].rsplit(":", 1)[1])
        stack.extend(comp.get("components", []))
        if "accessory" in comp:
            stack.append(comp["accessory"])
    return found


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def test_link_button_is_accessory_of_details_on_youtube_card():
    view = MusicControlView(
        MagicMock(), MagicMock(), song=(YT, "T"), heading="### 🎵 T", body="▬● 0:00 / 3:30", details="d",
        thumbnail="https://i.ytimg.com/vi_webp/abcdefghijk/maxresdefault.webp", link=YT,
    )
    parts = view.to_components()[0]["components"]
    assert [p["type"] for p in parts] == [9, 12, 9, 9, 1], "Titel, Galerie, Details+🔗, ⭐-Section, Buttons"
    assert parts[2]["components"] == [{"type": 10, "content": "d"}]
    accessory = parts[2]["accessory"]
    assert accessory["type"] == 2 and accessory["style"] == 2, "grauer Button wie das ❌"
    assert accessory["emoji"]["name"] == "🔗" and "label" not in accessory, "nur ein Emoji"
    assert accessory["custom_id"].endswith(":link")
    # Bestehendes bleibt: ❌ am Titel, Bild, die fünf Steuer-Buttons in einer Zeile.
    assert parts[0]["accessory"]["custom_id"].endswith(":clear")
    assert parts[1]["items"][0]["media"]["url"] == "https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg"
    assert parts[3]["accessory"]["custom_id"].endswith(":fav"), "⭐ direkt unter dem 🔗"
    assert _actions(parts[4]) == ROW


def test_link_button_without_any_image_still_sits_next_to_details():
    view = MusicControlView(MagicMock(), MagicMock(), heading="### 🎵 T", details="d",
                            link="https://example.org/lied")
    parts = view.to_components()[0]["components"]
    assert [p["type"] for p in parts] == [9, 9, 1]
    assert parts[1]["accessory"]["custom_id"].endswith(":link")
    assert _actions(parts[2]) == ROW


def test_small_thumbnail_keeps_its_place_link_button_gets_own_row():
    """Nicht-YouTube: das kleine Vorschaubild belegt den Accessory-Platz der
    Details – der 🔗-Button weicht in eine eigene Zeile unter den Buttons."""
    view = MusicControlView(
        MagicMock(), MagicMock(), song=("https://soundcloud.com/a/b", "T"), heading="### 🎵 T", details="d",
        thumbnail="https://i1.sndcdn.com/artworks-x.jpg", link="https://soundcloud.com/a/b",
    )
    parts = view.to_components()[0]["components"]
    assert [p["type"] for p in parts] == [9, 9, 1, 1, 1]
    assert parts[1]["accessory"]["type"] == 11, "Vorschaubild bleibt"
    assert _actions(parts[2]) == ROW
    assert _actions(parts[3]) == ["link"]
    assert _actions(parts[4]) == ["fav"], "⭐ in eigener Zeile unter dem 🔗"


def test_card_without_details_puts_link_button_in_own_row():
    view = MusicControlView(MagicMock(), MagicMock(), heading="### 🎵 T", link=YT)
    parts = view.to_components()[0]["components"]
    assert [p["type"] for p in parts] == [9, 1, 1]
    assert _actions(parts[1]) == ROW and _actions(parts[2]) == ["link"]


@pytest.mark.parametrize("link", [None, "", "ytsearch:irgendwas", "C:/lied.mp3", 42])
def test_no_link_no_button(link):
    """Radio-Karte und alles ohne brauchbare URL: Karte exakt wie bisher."""
    kwargs = dict(heading="### 📻 Sender", details="d")
    with_link = MusicControlView(MagicMock(), MagicMock(), link=link, **kwargs)
    without = MusicControlView(MagicMock(), MagicMock(), **kwargs)
    assert with_link.link is None
    assert "link" not in _all_actions(with_link.to_components()[0])
    strip = lambda comps: [p["type"] for p in comps[0]["components"]]   # noqa: E731
    assert strip(with_link.to_components()) == strip(without.to_components()) == [9, 10, 1]


def test_details_keep_updating_inside_the_section():
    """„Als Nächstes" wandert weiter mit, auch wenn die Details jetzt in einer Section stecken."""
    cog = build_cog()
    view = MusicControlView(cog, FakeCtx(FakeVoiceClient()), heading="### 🎵 T", details="**Dauer** 3:30",
                            footer="Uploader", link=YT)
    assert view.sync_state(up_next="Nächster | [Song] *x*") is True
    section = view.to_components()[0]["components"][1]
    assert section["components"][0]["content"].split("\n") == [
        "**Dauer** 3:30",
        f"**{t('embed.up_next')}** Nächster \\| [Song] \\*x\\*",
        "-# Uploader",
    ]
    assert section["accessory"]["custom_id"].endswith(":link")
    assert view.sync_state() is False


# ---------------------------------------------------------------------------
# Klick
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    YT,
    "https://www.youtube.com/watch?v=YqJ-XdoZV00&list=PL_x-1&t=1s",
    "https://soundcloud.com/künstler/song_(live)*[edit]|x",
])
def test_click_sends_link_as_ephemeral_code_block(url):
    async def run():
        cog = build_cog()
        cog.queue.append(("u2", "Danach"))
        cog.current_track = ("u1", "Läuft", 200)
        view = MusicControlView(cog, FakeCtx(FakeVoiceClient()), heading="### 🎵 T", details="d", link=url)
        interaction = FakeInteraction()

        await view.link.callback(interaction)

        assert interaction.sent == [(f"```\n{url}\n```", {"ephemeral": True})], "nur für den Klickenden, roh im Codeblock"
        assert interaction.edited == [] and interaction.message.edits == [], "die Karte bleibt unangetastet"
        assert list(cog.queue) == [("u2", "Danach")] and cog.current_track == ("u1", "Läuft", 200)

    asyncio.run(run())


def test_backtick_cannot_break_out_of_the_code_block():
    async def run():
        view = MusicControlView(build_cog(), FakeCtx(FakeVoiceClient()), heading="### 🎵 T",
                                link="https://example.org/a```b`c")
        interaction = FakeInteraction()
        await view.link.callback(interaction)
        assert interaction.sent == [("```\nhttps://example.org/a%60%60%60b%60c\n```", {"ephemeral": True})]

    asyncio.run(run())


def test_link_button_respects_same_voice_flag(monkeypatch):
    """REQUIRE_SAME_VOICE gilt für jeden Button der Karte – auch für diesen."""
    monkeypatch.setattr(config, "REQUIRE_SAME_VOICE", True)

    async def run():
        vc = FakeVoiceClient()
        vc.channel = object()
        view = MusicControlView(build_cog(), FakeCtx(vc), heading="### 🎵 T", details="d", link=YT)

        outside = FakeInteraction()                 # user.voice ist None → nicht im Channel
        await view.link.callback(outside)
        assert outside.sent == [(t("error.same_voice_required"), {"ephemeral": True})]

        inside = FakeInteraction()
        inside.user = types.SimpleNamespace(voice=types.SimpleNamespace(channel=vc.channel))
        await view.link.callback(inside)
        assert inside.sent == [(f"```\n{YT}\n```", {"ephemeral": True})]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Echte Karte aus _build_np_view
# ---------------------------------------------------------------------------

def _np_view(info, url, title="Magic Boleros - Bésame Mucho 🔥 | [Official Video]"):
    async def run():   # build_cog braucht einen laufenden Loop
        mc = build_cog(types.SimpleNamespace())
        mc._up_next_title = lambda: None
        return mc._build_np_view(FakeCtx(FakeVoiceClient()), info, url, title, 699)

    return asyncio.run(run())


def test_song_card_copies_the_same_link_as_its_title():
    page = "https://www.youtube.com/watch?v=YqJ-XdoZV00"
    view = _np_view({"webpage_url": page, "uploader": "Magic Boleros",
                     "thumbnail": "https://i.ytimg.com/vi/YqJ-XdoZV00/hqdefault.jpg"},
                    "https://youtu.be/YqJ-XdoZV00?si=abc")
    assert view._link == page
    parts = view.to_components()[0]["components"]
    assert [p["type"] for p in parts] == [9, 12, 9, 9, 1]
    assert f"(<{page}>)" in parts[0]["components"][0]["content"], "Titel bleibt ein Link"
    assert parts[2]["accessory"]["emoji"]["name"] == "🔗"


def test_song_card_falls_back_to_queue_url_without_webpage_url():
    view = _np_view({"uploader": None}, YT)
    assert view._link == YT
    assert "link" in _all_actions(view.to_components()[0])
