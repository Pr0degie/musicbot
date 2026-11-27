"""FavoritesMixin: gemeinsame Favoritenliste des Servers (!fav, !favs) und der
⭐-Button der Now-Playing-Karte.

Eine Liste für alle (Nutzerentscheidung 27.11.2026), gespeichert in
data/favorites.json im saveq-Format: Liste von [url, titel]-Paaren.
Per Mehrfachvererbung in MusicCommands eingebunden; nutzt self.queue,
self.current_track, self.is_playing und self.play_next.
"""

import asyncio
import json

import discord
from discord.ext import commands

from cogs.music_queue_io import _is_valid_playlist, _write_playlist
from utils.i18n import t
from utils.logger import logger
from utils.paths import DATA_DIR
from utils.tasks import spawn

FAVORITES_FILE = DATA_DIR / "favorites.json"
# So viele Einträge zeigt !favs; mehr passen nicht sinnvoll in eine Nachricht.
FAVS_SHOWN = 25


def load_favorites(path=FAVORITES_FILE) -> list:
    """Liest die Favoriten (blockierend – nur beim Start aufrufen). Fehlende
    oder kaputte Datei → leere Liste, der Bot startet trotzdem."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        logger.warning(f"[Favoriten] {path} ist unlesbar – starte mit leerer Liste.")
        return []
    if not _is_valid_playlist(data):
        logger.warning(f"[Favoriten] {path} hat ein unerwartetes Format – starte mit leerer Liste.")
        return []
    return [(url, title) for url, title in data]


class FavoritesMixin:
    # Klassen-Defaults für Tests, die den Cog per __new__ bauen.
    _favorites = None
    _favorites_lock = None

    def _fav_list(self) -> list:
        if self._favorites is None:
            self._favorites = []
        return self._favorites

    def _is_favorite(self, url) -> bool:
        return any(fav_url == url for fav_url, _ in self._fav_list())

    def _toggle_favorite(self, url, title) -> bool:
        """Nimmt den Song auf oder entfernt ihn. True = jetzt Favorit.
        Speichert im Hintergrund."""
        favs = self._fav_list()
        for i, (fav_url, _) in enumerate(favs):
            if fav_url == url:
                del favs[i]
                added = False
                break
        else:
            favs.append((url, title))
            added = True
        logger.info(f"[Favoriten] {'Hinzugefügt' if added else 'Entfernt'}: {title}")
        self._schedule_favorites_save()
        return added

    def _schedule_favorites_save(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return   # ohne Loop (synchrone Tests) nichts schreiben
        spawn(self._save_favorites(), "favorites-save")

    async def _save_favorites(self):
        """Schreibt die Liste im Thread. Der Lock hält die Writes in Reihenfolge,
        der Snapshot entsteht erst unter dem Lock – zuletzt landet also immer
        der neueste Stand auf der Platte."""
        if self._favorites_lock is None:
            self._favorites_lock = asyncio.Lock()
        async with self._favorites_lock:
            snapshot = [[url, title] for url, title in self._fav_list()]
            try:
                await asyncio.to_thread(_write_playlist, FAVORITES_FILE, snapshot)
            except OSError as e:
                logger.warning(f"[Favoriten] Speichern fehlgeschlagen: {e}")

    @commands.command(name="fav")
    async def fav(self, ctx):
        """Laufenden Song in die Favoriten aufnehmen oder wieder entfernen."""
        if self.is_radio or not self.current_track:
            await ctx.send(t("error.no_active_song"))
            return
        url, title, *_ = self.current_track
        added = self._toggle_favorite(url, title)
        key = "status.fav_added" if added else "status.fav_removed"
        await ctx.send(t(key, title=discord.utils.escape_markdown(title)))

    @commands.command(name="favs", usage="!favs [play | remove <n>]")
    async def favs(self, ctx, action: str = None, number: int = None):
        """Favoriten anzeigen, alle einreihen (!favs play) oder einen
        entfernen (!favs remove <n>)."""
        favs = self._fav_list()
        if action is None:
            await self._send_fav_list(ctx, favs)
        elif action.lower() == "play":
            await self._play_favorites(ctx, favs)
        elif action.lower() == "remove":
            if number is None or not 1 <= number <= len(favs):
                await ctx.send(t("error.fav_invalid_number", count=len(favs)))
                return
            _, title = favs.pop(number - 1)
            logger.info(f"[Favoriten] Entfernt per !favs remove: {title}")
            self._schedule_favorites_save()
            await ctx.send(t("status.fav_removed", title=discord.utils.escape_markdown(title)))
        else:
            await ctx.send(t("error.fav_usage"))

    async def _send_fav_list(self, ctx, favs):
        if not favs:
            await ctx.send(t("status.no_favs"))
            return
        lines = [
            f"`{i}.` {discord.utils.escape_markdown(title)}"
            for i, (_, title) in enumerate(favs[:FAVS_SHOWN], start=1)
        ]
        if len(favs) > FAVS_SHOWN:
            lines.append(t("status.favs_more", count=len(favs) - FAVS_SHOWN))
        embed = discord.Embed(
            title=t("embed.favorites", count=len(favs)),
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=t("embed.favorites_footer"))
        await ctx.send(embed=embed)

    async def _play_favorites(self, ctx, favs):
        if not favs:
            await ctx.send(t("status.no_favs"))
            return
        tracks = favs[:self.HARD_PLAYLIST_LIMIT]
        self.queue.extend(tracks)
        await ctx.send(t("status.favs_queued", count=len(tracks)))
        if not self.is_playing:   # wie !loadq
            nxt_url, nxt_title = self.queue[0]
            self.dl.prime_first_hit(nxt_url, nxt_title)
            self.is_playing = True
            await self.play_next(ctx)
        else:
            self._kick_prefetch()
