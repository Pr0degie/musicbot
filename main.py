import discord
from cogs.basic import BasicCommands
from cogs.music import MusicCommands
from config import TOKEN
from discord.ext import commands


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.add_cog(BasicCommands(self))
        await self.add_cog(MusicCommands(self))

    async def on_ready(self):
        print(f"Bot ist online als {self.user}")


bot = MusicBot()
bot.run(TOKEN)
