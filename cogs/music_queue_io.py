"""QueuePersistenceMixin: Queues speichern/laden (!saveq, !loadq, !lists).

Per Mehrfachvererbung in MusicCommands eingebunden. Greift auf Instanz-State
(self.queue, self.current_track, self.is_playing) und die Kern-Methode
self.play_next zu, die in cogs/music.py definiert sind.
"""

import json
from pathlib import Path

from discord.ext import commands

from utils.i18n import t

PLAYLISTS_DIR = Path("playlists")
PLAYLISTS_DIR.mkdir(parents=True, exist_ok=True)


class QueuePersistenceMixin:
    @commands.command(name="saveq", usage="!saveq <name>")
    async def saveq(self, ctx, *, name: str):
        """Speichert die aktuelle Queue unter einem Namen. Verwendung: !saveq <name>"""
        if not self.queue and not self.current_track:
            await ctx.send(t("error.nothing_to_save"))
            return
        safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
        if not safe_name:
            await ctx.send(t("error.invalid_name"))
            return
        tracks = []
        if self.current_track:
            ct_url, ct_title, *_ = self.current_track
            tracks.append([ct_url, ct_title])
        tracks.extend([url, title] for url, title in self.queue)
        path = PLAYLISTS_DIR / f"{safe_name}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tracks, f, ensure_ascii=False, indent=2)
        await ctx.send(t("status.queue_saved", name=safe_name, count=len(tracks)))

    @commands.command(name="loadq", usage="!loadq <name>")
    async def loadq(self, ctx, *, name: str):
        """Lädt eine gespeicherte Queue und hängt sie an die aktuelle an. Verwendung: !loadq <name>"""
        safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
        path = PLAYLISTS_DIR / f"{safe_name}.json"
        if not path.exists():
            await ctx.send(t("error.queue_not_found", name=safe_name))
            return
        with open(path, encoding="utf-8") as f:
            tracks = json.load(f)
        for url, title in tracks:
            self.queue.append((url, title))
        await ctx.send(t("status.queue_loaded", name=safe_name, count=len(tracks)))
        if not self.is_playing:
            self.is_playing = True
            await self.play_next(ctx)

    @commands.command(name="lists")
    async def lists(self, ctx):
        """Zeigt alle gespeicherten Queues."""
        files = sorted(PLAYLISTS_DIR.glob("*.json"))
        if not files:
            await ctx.send(t("status.no_saved_queues"))
            return
        lines = []
        for p in files:
            try:
                with open(p, encoding="utf-8") as f:
                    count = len(json.load(f))
                lines.append(f"• **{p.stem}** ({count} Titel)")
            except Exception:
                lines.append(f"• **{p.stem}**")
        await ctx.send(t("status.saved_queues", lines="\n".join(lines)))
