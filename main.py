import asyncio

import discord
from cogs.basic import BasicCommands
from cogs.music import MusicCommands
from cogs.dm_bridge import DMBridge
from config import TOKEN
from discord.ext import commands
from utils.logger import logger
from utils.shutdown import install_sigint_handler


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # message_content ist Pflicht für Prefix-Commands (!p, !q, etc.) –
        # ohne diesen Intent sieht der Bot den Nachrichteninhalt nicht.
        intents.message_content = True
        # help_command=None deaktiviert den eingebauten !help –
        # wir haben unseren eigenen mit deutschem Text in basic.py.
        # allowed_mentions=none(): der Bot gibt Fremd-Content wieder (YouTube-Titel,
        # !echo, Lyrics) – ein "@everyone" darin darf nie tatsächlich pingen.
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def setup_hook(self):
        # Cogs werden hier registriert – BasicCommands für Voice-Management,
        # MusicCommands für alles rund um Wiedergabe und Queue,
        # DMBridge für die HTTP-Bridge zum KI-Dungeon-Master (Bot B).
        await self.add_cog(BasicCommands(self))
        await self.add_cog(MusicCommands(self))
        await self.add_cog(DMBridge(self))
        # Beantwortet Klicks auf Now-Playing-Buttons aus früheren Bot-Läufen
        # mit einer ephemeren Erklärung statt "Interaktion fehlgeschlagen".
        from views.music_controls import StaleControlsFallback

        self.add_dynamic_items(StaleControlsFallback)

    async def on_message(self, message):
        if message.author.bot:
            return
        # Wenn jemand nur "!" schreibt, wird es als "!help" behandelt.
        if message.content.strip() == "!":
            message.content = "!help"
        await self.process_commands(message)

    async def on_ready(self):
        print(f"Bot ist online als {self.user}")
        # Einmalig (on_ready feuert bei Reconnects erneut): yt-dlp-Update-Check
        # als Hintergrund-Task – nur ein Log-Hinweis, kein Auto-Update.
        if not getattr(self, "_ytdlp_check_started", False):
            self._ytdlp_check_started = True
            from cogs.downloader import check_ytdlp_update

            asyncio.create_task(check_ytdlp_update())


bot = MusicBot()
# Strg+C im Terminal: erstes Signal fragt nur nach, das zweite fährt geordnet
# herunter, ein drittes bricht hart ab (utils/shutdown.py).
install_sigint_handler(bot)
# log_handler=None: Logging wird in utils/logger.py konfiguriert –
# verhindert dass discord.py einen eigenen StreamHandler hinzufügt (doppelte Ausgabe).
bot.run(TOKEN, log_handler=None)
# Schlusspunkt in bot.log: run() kehrt nur zurück, wenn der Bot wirklich unten
# ist. Fehlt diese Zeile nach einem Strg+C, hing das Herunterfahren – steht sie
# da, kommt alles Weitere im Fenster von start.bat (Terminate batch job/pause).
logger.info("[Shutdown] Bot beendet.")
