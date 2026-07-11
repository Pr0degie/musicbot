import discord
from cogs.basic import BasicCommands
from cogs.music import MusicCommands
from cogs.dm_bridge import DMBridge
from config import TOKEN
from discord.ext import commands


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


bot = MusicBot()
# log_handler=None: Logging wird in utils/logger.py konfiguriert –
# verhindert dass discord.py einen eigenen StreamHandler hinzufügt (doppelte Ausgabe).
bot.run(TOKEN, log_handler=None)
