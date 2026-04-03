import discord
from discord.ext import commands


class BasicCommands(commands.Cog):
    """Grundlegende Bot-Befehle: Voice-Channel-Management, Ping und Echo."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="help", aliases=["h"])
    async def help_command(self, ctx):
        """Listet alle verfügbaren Befehle auf."""
        await ctx.send(
            "```\n"
            "🎵 Musik\n"
            "  !p <url/suche>   – Lied, Playlist oder Suchbegriff abspielen\n"
            "  !baba            – Baba Playlist abspielen\n"
            "  !s               – Aktuellen Song überspringen\n"
            "  !x               – Pause\n"
            "  !resume          – Wiedergabe fortsetzen\n"
            "  !now             – Aktuell laufenden Song anzeigen\n"
            "  !loop            – Loop-Modus: aus → Song → Queue → aus\n"
            "  !replay          – Letzten Song nochmal abspielen\n"
            "\n"
            "📜 Queue\n"
            "  !q               – Warteschlange anzeigen\n"
            "  !shuffle         – Warteschlange mischen\n"
            "  !clear           – Queue leeren & Wiedergabe stoppen\n"
            "  !remove <n>      – Song an Position n entfernen\n"
            "  !move <n>        – Zu Position n springen und von dort abspielen\n"
            "\n"
            "🎚️ Audio\n"
            "  !eq <preset>     – EQ-Preset setzen (ohne Argument: Liste)\n"
            "                     Presets: bassboost, flat, vocalboost, superbass,\n"
            "                              punchy, nightcore, karaoke, 8d\n"
            "                     EQ-Wechsel startet Song sofort neu.\n"
            "  !format <typ>    – Audioformat wechseln: mp3 oder webm\n"
            "\n"
            "🔊 Voice\n"
            "  !j               – Voice-Channel beitreten\n"
            "  !l               – Voice-Channel verlassen\n"
            "\n"
            "  !help / !        – Diese Übersicht\n"
            "```"
        )

    @commands.command()
    async def ping(self, ctx):
        """Klassischer Verbindungstest. Antwortet mit 'Pong!'."""
        await ctx.send("Pong!")

    @commands.command()
    async def echo(self, ctx, *, message):
        """Wiederholt die Nachricht des Users. Gut zum Testen, ob der Bot zuhört."""
        await ctx.send(message)

    @commands.command(name="j")
    async def join(self, ctx):
        """Verbindet den Bot mit dem Voice-Channel des Users (oder wechselt dorthin)."""
        if ctx.author.voice:
            channel = ctx.author.voice.channel

            if ctx.voice_client is None:
                # Bot ist noch nicht verbunden → frisch einsteigen
                try:
                    await channel.connect()
                    await ctx.send(f"🔊 Verbunden mit: {channel.name}")
                except discord.errors.ConnectionClosed as e:
                    # Verbindung wurde vom Server abgelehnt oder unterbrochen.
                    # Häufigste Ursache: fehlende Berechtigungen auf dem Server.
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
                # Bot ist bereits irgendwo verbunden → in den neuen Kanal wechseln
                try:
                    await ctx.voice_client.move_to(channel)
                    await ctx.send(f"🔄 Bewegt zu: {channel.name}")
                except Exception as e:
                    await ctx.send(
                        f"❌ Bewegung zu {channel.name} fehlgeschlagen: {type(e).__name__}"
                        "\n⚠️ Prüfe Server-Berechtigungen (Bot-Rolle)"
                    )
        else:
            # User ist in keinem Voice-Channel – da kann der Bot auch nicht hin.
            await ctx.send("⚠️ Du bist in keinem Voice-Channel.")

    @commands.command(name="l")
    async def leave(self, ctx):
        """Trennt den Bot vom Voice-Channel."""
        if ctx.voice_client is not None:
            try:
                await ctx.voice_client.disconnect()
                await ctx.send("Verlassen des Voice-Channels.")
            except Exception as e:
                await ctx.send(f"⚠️ Verlassen fehlgeschlagen: {type(e).__name__}")
        else:
            await ctx.send("🙅 Der Bot ist nicht in einem Voice-Channel.")
