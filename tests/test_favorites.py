"""Favoriten: eine gemeinsame Liste des Servers (⭐ auf der Karte, !fav, !favs).

Der ⭐ steht neben dem 🔗 in einer eigenen Reihe über den Steuer-Buttons
(Nutzerwunsch 28.11.2026).
"""

import asyncio
import json
import types

import discord

import cogs.music_favorites as favmod
from cogs.music_favorites import load_favorites
from tests.conftest import FakeCtx, FakeVoiceClient, build_cog
from tests.test_np_card_controls import FakeInteraction
from utils.i18n import t
from views.music_controls import MusicControlView

YT = "https://www.youtube.com/watch?v=abcdefghijk"
YT_THUMB = "https://i.ytimg.com/vi_webp/abcdefghijk/maxresdefault.webp"


def _card(cog, **kwargs):
    defaults = dict(song=(YT, "Song"), heading="### 🎵 Song", details="**Dauer** 3:30",
                    thumbnail=YT_THUMB, link=YT)
    defaults.update(kwargs)
    return MusicControlView(cog, FakeCtx(FakeVoiceClient()), **defaults)


def _no_disk(monkeypatch):
    """Toggle schreibt im Hintergrund – in Tests nie in das echte data/."""
    async def fake_save(self):
        self.saved = [list(f) for f in self._fav_list()]
    monkeypatch.setattr(favmod.FavoritesMixin, "_save_favorites", fake_save)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def test_star_sits_next_to_link_above_the_controls():
    async def run():
        cog = build_cog()
        parts = _card(cog).to_components()[0]["components"]
        assert [p["type"] for p in parts] == [9, 12, 10, 1, 1]
        assert parts[2]["content"] == "**Dauer** 3:30", "keine Uploader-Zeile"
        link, star = parts[3]["components"]
        assert link["emoji"]["name"] == "🔗"
        assert star["label"] == "☆" and star["style"] == 2 and "emoji" not in star,             "kein Favorit → leerer Stern"
        assert parts[4]["components"][0]["custom_id"].endswith(":back"), "Steuerreihe darunter"
    asyncio.run(run())


def test_star_next_to_link_also_with_small_thumbnail():
    async def run():
        sc = "https://soundcloud.com/a/b"
        view = _card(build_cog(), song=(sc, "T"), thumbnail="https://i1.sndcdn.com/x.jpg", link=sc)
        parts = view.to_components()[0]["components"]
        assert [p["type"] for p in parts] == [9, 9, 1, 1]
        ids = [[b["custom_id"].rsplit(":", 1)[1] for b in p["components"]] for p in parts[2:]]
        assert ids[0] == ["link", "fav"]
        assert parts[1]["components"][0]["content"] == "**Dauer** 3:30", "keine Uploader-Zeile"
    asyncio.run(run())


def test_radio_card_has_no_star():
    async def run():
        view = MusicControlView(build_cog(), FakeCtx(), heading="### 📻 Sender", details="d")
        assert view.fav is None
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Klick und Zustand
# ---------------------------------------------------------------------------

def assert_empty_star(button):
    """Kein Favorit: leerer Umriss-Stern ☆ als Beschriftung, grau, kein Emoji."""
    assert button.label == "☆" and button.emoji is None
    assert button.style == discord.ButtonStyle.secondary


def assert_filled_star(button):
    """Favorit: gelber ⭐-Emoji ohne Beschriftung, Button bleibt grau."""
    assert button.label is None and str(button.emoji) == "⭐"
    assert button.style == discord.ButtonStyle.secondary, "kein Grün – der Stern zeigt den Zustand"


def test_star_click_toggles_shared_list_and_turns_green(monkeypatch):
    _no_disk(monkeypatch)

    async def run():
        cog = build_cog()
        view = _card(cog)
        assert_empty_star(view.fav)

        await view.fav.callback(FakeInteraction())
        await asyncio.sleep(0)
        assert cog._fav_list() == [(YT, "Song")]
        assert_filled_star(view.fav)
        assert cog.saved == [[YT, "Song"]]

        await view.fav.callback(FakeInteraction())
        await asyncio.sleep(0)
        assert cog._fav_list() == []
        assert_empty_star(view.fav)
    asyncio.run(run())


def test_card_of_existing_favorite_starts_green():
    async def run():
        cog = build_cog()
        cog._favorites = [(YT, "Song")]
        assert_filled_star(_card(cog).fav)
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Befehle
# ---------------------------------------------------------------------------

def test_fav_command_toggles_current_song(monkeypatch):
    _no_disk(monkeypatch)

    async def run():
        cog = build_cog()
        ctx = FakeCtx()
        await cog.fav.callback(cog, ctx)
        assert ctx.texts == [t("error.no_active_song")]

        cog.current_track = (YT, "Song_mit|Zeichen", 200)
        await cog.fav.callback(cog, ctx)
        await cog.fav.callback(cog, ctx)
        title = discord.utils.escape_markdown("Song_mit|Zeichen")
        assert ctx.texts[1:] == [t("status.fav_added", title=title), t("status.fav_removed", title=title)]
        assert cog._fav_list() == []
    asyncio.run(run())


def test_favs_list_play_and_remove(monkeypatch):
    _no_disk(monkeypatch)

    async def run():
        cog = build_cog()
        ctx = FakeCtx()
        await cog.favs.callback(cog, ctx)
        assert ctx.texts == [t("status.no_favs")]

        cog._favorites = [("u1", "Eins"), ("u2", "Zwei_*")]
        await cog.favs.callback(cog, ctx)
        embed = ctx.sent[-1][1]["embed"]
        assert embed.description == "`1.` Eins\n`2.` Zwei\\_\\*"

        cog.is_playing = True
        kicked = []
        cog._kick_prefetch = lambda: kicked.append(True)
        await cog.favs.callback(cog, ctx, "play")
        assert list(cog.queue) == [("u1", "Eins"), ("u2", "Zwei_*")]
        assert ctx.texts[-1] == t("status.favs_queued", count=2) and kicked

        await cog.favs.callback(cog, ctx, "remove", 3)
        assert ctx.texts[-1] == t("error.fav_invalid_number", count=2)
        await cog.favs.callback(cog, ctx, "remove", 1)
        assert cog._fav_list() == [("u2", "Zwei_*")]

        await cog.favs.callback(cog, ctx, "quatsch")
        assert ctx.texts[-1] == t("error.fav_usage")
    asyncio.run(run())


def test_favs_play_starts_when_idle(monkeypatch):
    async def run():
        cog = build_cog()
        cog._favorites = [("u1", "Eins")]
        primed, started = [], []
        cog.dl = types.SimpleNamespace(prime_first_hit=lambda u, ti: primed.append(u))

        async def play_next(ctx):
            started.append(ctx)
        cog.play_next = play_next
        ctx = FakeCtx()
        await cog.favs.callback(cog, ctx, "play")
        assert primed == ["u1"] and started == [ctx] and cog.is_playing
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Datei
# ---------------------------------------------------------------------------

def test_save_and_load_roundtrip(monkeypatch, tmp_path):
    path = tmp_path / "favorites.json"
    monkeypatch.setattr(favmod, "FAVORITES_FILE", path)

    async def run():
        cog = build_cog()
        cog._favorites = [("u1", "Eins 🔥 | [x]")]
        await cog._save_favorites()
    asyncio.run(run())

    assert json.loads(path.read_text(encoding="utf-8")) == [["u1", "Eins 🔥 | [x]"]]
    assert load_favorites(path) == [("u1", "Eins 🔥 | [x]")]


def test_load_tolerates_missing_and_broken_file(tmp_path):
    assert load_favorites(tmp_path / "fehlt.json") == []
    broken = tmp_path / "kaputt.json"
    broken.write_text("{nicht json", encoding="utf-8")
    assert load_favorites(broken) == []
    wrong = tmp_path / "falsch.json"
    wrong.write_text('{"a": 1}', encoding="utf-8")
    assert load_favorites(wrong) == []


def test_star_respects_same_voice_flag(monkeypatch):
    import config
    monkeypatch.setattr(config, "REQUIRE_SAME_VOICE", True)

    async def run():
        cog = build_cog()
        view = _card(cog)
        view.ctx.voice_client.channel = object()
        interaction = FakeInteraction()        # user.voice ist None → nicht im Channel
        await view.fav.callback(interaction)
        assert interaction.sent == [(t("error.same_voice_required"), {"ephemeral": True})]
        assert cog._fav_list() == []
    asyncio.run(run())
