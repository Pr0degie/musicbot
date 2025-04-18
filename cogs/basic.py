from discord.ext import commands


class BasicCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def ping(self, ctx):
        await ctx.send("Pong!")

    @commands.command()
    async def echo(self, ctx, *, message):
        await ctx.send(message)

    @commands.command(name="j")
    async def join(self, ctx):
        if ctx.author.voice:
            channel = ctx.author.voice.channel
            if ctx.voice_client is None:
                await channel.connect()
                await ctx.send(f"🔊 Verbunden mit: {channel.name}")
            else:
                await ctx.voice_client.move_to(channel)
                await ctx.send(f"🔄 Bewegt zu: {channel.name}")
        else:
            await ctx.send("⚠️ Du bist in keinem Voice-Channel.")

    @commands.command()
    async def leave(self, ctx):
        if ctx.voice_client is not None:
            await ctx.voice_client.disconnect()
            await ctx.send("Verpiss DICH")
        else:
            await ctx.send("was wilst du")
