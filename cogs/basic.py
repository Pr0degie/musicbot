import os
import subprocess
import sys

import discord
from discord.ext import commands

from utils.i18n import t


class BasicCommands(commands.Cog):
    """Grundlegende Bot-Befehle: Voice-Channel-Management, Ping und Echo."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="help", aliases=["h"])
    async def help_command(self, ctx):
        """Listet alle verfügbaren Befehle auf – blätterbar auf mehreren Seiten."""
        from views.help_view import HelpView

        view = HelpView(ctx, t("help.text"))
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @commands.command()
    async def ping(self, ctx):
        """Klassischer Verbindungstest. Antwortet mit 'Pong!'."""
        await ctx.send(t("misc.pong"))

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
                    await ctx.send(t("status.joined", channel=channel.name))
                except discord.errors.ConnectionClosed as e:
                    # Verbindung wurde vom Server abgelehnt oder unterbrochen.
                    # Häufigste Ursache: fehlende Berechtigungen auf dem Server.
                    await ctx.send(
                        t("error.join_failed_code", channel=channel.name, code=e.code)
                    )
                except Exception as e:
                    await ctx.send(
                        t(
                            "error.join_failed",
                            err=f"{type(e).__name__}: {str(e)[:100]}",
                        )
                    )
            else:
                # Bot ist bereits irgendwo verbunden → in den neuen Kanal wechseln
                try:
                    await ctx.voice_client.move_to(channel)
                    await ctx.send(t("status.moved_to", channel=channel.name))
                except Exception as e:
                    await ctx.send(
                        t(
                            "error.move_failed",
                            channel=channel.name,
                            err=type(e).__name__,
                        )
                    )
        else:
            # User ist in keinem Voice-Channel – da kann der Bot auch nicht hin.
            await ctx.send(t("error.no_voice"))

    @commands.command(name="restart")
    @commands.is_owner()
    async def restart(self, ctx):
        """Startet den Bot-Prozess in einem neuen Terminal neu (nur Bot-Owner)."""
        await ctx.send("🔄 Restarting...")
        cwd = os.getcwd()
        try:
            # WSL2: neues Windows Terminal Tab öffnen, altes schließt sich durch os._exit
            subprocess.Popen(
                [
                    "wt.exe",
                    "wsl",
                    "--",
                    "bash",
                    "-c",
                    f'cd "{cwd}" && python main.py; exec bash',
                ],
                start_new_session=True,
            )
        except FileNotFoundError:
            # Kein Windows Terminal → in-place restart als Fallback
            os.execv(sys.executable, [sys.executable] + sys.argv)
            return
        os._exit(0)

    @commands.command(name="l")
    async def leave(self, ctx):
        """Trennt den Bot vom Voice-Channel."""
        if ctx.voice_client is not None:
            try:
                await ctx.voice_client.disconnect()
                await ctx.send(t("status.left"))
            except Exception as e:
                await ctx.send(t("error.leave_failed", err=type(e).__name__))
        else:
            await ctx.send(t("error.bot_not_in_voice"))

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return  # Tippfehler still ignorieren
        if isinstance(error, commands.NotOwner):
            await ctx.send("❌ Owner only.")
            return
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            # Usage-Hinweis aus dem Docstring des Commands holen
            usage = ctx.command.usage or f"!{ctx.command.qualified_name}"
            await ctx.send(f"❌ Usage: `{usage}`")
            return
        if isinstance(error, commands.CheckFailure):
            return
        # Unerwarteter Fehler → kurze Meldung + ins Log
        from utils.logger import logger

        logger.error(f"[on_command_error] {ctx.command}: {error}")
        await ctx.send(f"❌ Error: {type(error).__name__}")
