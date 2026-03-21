import discord
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

            # Nur verbinden, wenn kein Client da ist
            if ctx.voice_client is None:
                try:
                    await channel.connect()
                    await ctx.send(f"🔊 Verbunden mit: {channel.name}")
                    # except #discord.ClientConnectorTurnover:
                    # Verbindungsübertragung, Client wurde neu verbunden
                    await ctx.send(f"🔊 Verbindungsübertragung erkannt.")
                except discord.errors.ConnectionClosed as e:
                    # Fehlerhafte Verbindung, Retry mit Info
                    await ctx.send(
                        f"❌ Verbindung zu {channel.name} fehlgeschlagen (Code: {e.code})."
                        "\n⚠️ Mögliche Ursachen:"
                        "- Server-Einstellungen: 'Bots können Sprachkanäle sehen' muss aktiv sein"
                        "- Voice-Kanal-Berechtigungen: Bot-Rolle muss Connect + Speak haben"
                    )
                except Exception as e:
                    await ctx.send(
                        f"❌ Verbindungsfehler: {type(e).__name__}: {str(e)[:100]}"
                    )
            else:
                try:
                    await ctx.voice_client.move_to(channel)
                    await ctx.send(f"🔄 Bewegt zu: {channel.name}")
                    # except #discord.ClientConnectorTurnover:
                    await ctx.send(f"🔄 Verbindungsübertragung erkannt.")
                except Exception as e:
                    await ctx.send(
                        f"❌ Bewegung zu {channel.name} fehlgeschlagen: {type(e).__name__}"
                        "\n⚠️ Prüfe Server-Berechtigungen (Bot-Rolle)"
                    )
        else:
            await ctx.send("⚠️ Du bist in keinem Voice-Channel.")

    @commands.command(name="l")
    async def leave(self, ctx):
        if ctx.voice_client is not None:
            try:
                await ctx.voice_client.disconnect()
                await ctx.send("Verlassen des Voice-Channels.")
            except Exception as e:
                await ctx.send(f"⚠️ Fehlleerverlassen: {type(e).__name__}")
        else:
            await ctx.send("🙅 Der Bot ist nicht in einem Voice-Channel.")
