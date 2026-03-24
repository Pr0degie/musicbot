import discord
from cogs.basic import BasicCommands
from cogs.music import MusicCommands
from config import TOKEN
from discord.ext import commands


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # message_content ist Pflicht für Prefix-Commands (!p, !q, etc.) –
        # ohne diesen Intent sieht der Bot den Nachrichteninhalt nicht.
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Cogs werden hier registriert – BasicCommands für Voice-Management,
        # MusicCommands für alles rund um Wiedergabe und Queue.
        await self.add_cog(BasicCommands(self))
        await self.add_cog(MusicCommands(self))

    async def on_ready(self):
        print(f"Bot ist online als {self.user}")


bot = MusicBot()
bot.run(TOKEN)
