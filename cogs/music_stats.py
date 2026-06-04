"""StatsMixin: !score (Top-10 gespielte Songs) und !stats (Prozess-Metriken).

Per Mehrfachvererbung in MusicCommands eingebunden. Greift auf Instanz-State
(self._play_counts, self._process, self._start_time, self._songs_played,
self.queue) zu, der in cogs/music.py initialisiert wird.
"""

import asyncio
import itertools
import time

import discord
from discord.ext import commands

from utils.i18n import t
from cogs.downloader import DOWNLOAD_DIR


class StatsMixin:
    @commands.command(name="score")
    async def score(self, ctx):
        """Zeigt die Top-10 der am häufigsten gespielten Songs."""
        if not self._play_counts:
            await ctx.send(t("status.no_plays"))
            return

        top = sorted(self._play_counts.items(), key=lambda x: x[1]["count"], reverse=True)[:10]
        embed = discord.Embed(title=t("embed.most_played"), color=discord.Color.gold())
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, (_, entry) in enumerate(top):
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            lines.append(f"{prefix} **{entry['title']}** — {entry['count']}x")
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

    @commands.command(name="stats")
    async def stats(self, ctx):
        """Zeigt Live-Metriken zum Bot-Prozess: RAM, CPU, Uptime, Queue, Cache."""
        proc = self._process

        # RAM
        mem = proc.memory_info()
        rss_mb = mem.rss / 1024 / 1024
        vms_mb = mem.vms / 1024 / 1024

        # CPU (non-blocking: 0.1s Interval im Thread)
        cpu_pct = await asyncio.to_thread(proc.cpu_percent, 0.1)

        # Uptime
        uptime_s = int(time.monotonic() - self._start_time)
        h, rem = divmod(uptime_s, 3600)
        m, s = divmod(rem, 60)
        uptime_str = f"{h}h {m}m {s}s"

        # asyncio Tasks
        all_tasks = asyncio.all_tasks()
        running_tasks = sum(1 for t in all_tasks if not t.done())

        # Downloads-Ordner – auf 500 Dateien begrenzen damit stat() nicht ewig läuft
        _SCAN_LIMIT = 500
        dl_files = list(itertools.islice(DOWNLOAD_DIR.glob("*"), _SCAN_LIMIT + 1))
        capped = len(dl_files) > _SCAN_LIMIT
        if capped:
            dl_files = dl_files[:_SCAN_LIMIT]
        dl_count = f"{len(dl_files)}+" if capped else str(len(dl_files))
        dl_size_mb = await asyncio.to_thread(
            lambda: sum(f.stat().st_size for f in dl_files if f.is_file()) / 1024 / 1024
        )

        # Queue
        queue_len = len(self.queue)

        embed = discord.Embed(title=t("embed.stats"), color=0x5865f2)
        embed.add_field(name=t("embed.uptime"), value=uptime_str, inline=True)
        embed.add_field(name=t("embed.songs_played"), value=str(self._songs_played), inline=True)
        embed.add_field(name=t("embed.queue_field"), value=t("misc.songs_count", count=queue_len), inline=True)
        embed.add_field(name=t("embed.ram_rss"), value=f"{rss_mb:.1f} MB", inline=True)
        embed.add_field(name=t("embed.ram_vms"), value=f"{vms_mb:.1f} MB", inline=True)
        embed.add_field(name=t("embed.cpu"), value=f"{cpu_pct:.1f}%", inline=True)
        embed.add_field(name=t("embed.tasks"), value=str(running_tasks), inline=True)
        embed.add_field(name=t("embed.cache_files"), value=t("misc.files_count", count=dl_count), inline=True)
        embed.add_field(name=t("embed.cache_size"), value=f"{dl_size_mb:.0f} MB", inline=True)
        await ctx.send(embed=embed)
