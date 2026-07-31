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

    @commands.command(name="debug", usage="!debug on|off")
    async def debug(self, ctx, mode: str = None):
        """Terminal-Diagnose umschalten: on = volle Diagnose, off = nur echte
        Probleme. Ohne Argument: Status. bot.log ist immer vollständig."""
        from utils.logger import get_console_mode, set_console_mode

        if mode is None:
            key = "status.debug_on" if get_console_mode() == "debug" else "status.debug_off"
            await ctx.send(t(key))
            return
        m = mode.strip().lower()
        if m in ("on", "debug"):
            set_console_mode("debug")
            await ctx.send(t("status.debug_on"))
        elif m in ("off", "quiet"):
            set_console_mode("quiet")
            await ctx.send(t("status.debug_off"))
        else:
            await ctx.send(t("error.debug_usage"))

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
        """Startet den Bot-Prozess neu (nur Bot-Owner): Windows → neue Konsole, Linux/macOS → os.execv in-place."""
        from utils.logger import logger

        await ctx.send("🔄 Restarting...")
        # os._exit(0) unten überspringt alle Cleanup-Hooks – gedebouncte
        # Persistenz (Play-Counts, Metadaten-Cache) muss deshalb hier
        # explizit geflusht werden. Vorher Voice sauber trennen, damit
        # Discord den alten Prozess nicht als hängende Session sieht.
        music = self.bot.get_cog("MusicCommands")
        if music is not None:
            # Watchdog/Auto-Advance dürfen während des Disconnects nichts nachstarten.
            music._stopped_by_user = True
        for vc in list(self.bot.voice_clients):
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass  # Restart darf am Voice-Cleanup nicht scheitern
        if music is not None:
            try:
                music._flush_scores_now()
                music.dl.flush_cache_now()
            except Exception:
                pass  # Restart darf an einem Flush-Fehler nicht scheitern
        if sys.platform == "win32":
            # os.execv ist auf Windows kein echter exec (CreateProcess + Exit):
            # das Kind teilt die Konsole mit start.bat, dessen `pause` dann mit
            # dem Bot um stdin konkurriert, und argv wird nicht Windows-konform
            # gequotet (Pfade mit Leerzeichen brechen). Deshalb sauberer Schnitt:
            # neues Konsolenfenster, alter Prozess beendet sich hart.
            # sys.executable ist bereits die venv-Python aus start.bat – die
            # .bat muss nicht erneut laufen (activate setzt nur PATH/Umgebung,
            # ihr `pause` beendet nur das alte Fenster).
            subprocess.Popen(
                [sys.executable] + sys.argv,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            logger.info("[restart] Neustart in neuer Konsole (CREATE_NEW_CONSOLE) – beende alten Prozess")
            os._exit(0)
            return  # unerreichbar – nur für Tests, die os._exit mocken
        # Linux/macOS: echter exec ersetzt den Prozess in-place.
        logger.info("[restart] Neustart via os.execv (in-place)")
        os.execv(sys.executable, [sys.executable] + sys.argv)

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
