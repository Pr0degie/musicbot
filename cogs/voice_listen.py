"""Sprachsteuerung: gesprochene Befehle ("yo bot, spiel mal ...").

Ein Parser, zwei Eingänge. `handle_text()` ist die einzige Stelle, durch die
ein gesprochener Satz läuft – egal ob er

  1. von Bot B (KI-Dungeon-Master) über `POST /command` der DM-Bridge kommt,
     der ohnehin schon im Voice-Channel zuhört und transkribiert, oder
  2. später aus dem eigenen Zuhören (discord-ext-voice-recv + lokales Whisper).

Weil beide Wege denselben Eingang benutzen, sitzen Rechte-Prüfung, Weckwort,
Parsing, Rückmeldung und Logging genau einmal im Code.

Ausgeführt wird über eine synthetische Message (Muster aus views/help_view.py):
Referenz-Nachricht kopieren, Autor auf den Sprecher setzen, Inhalt auf den
Befehl, dann `bot.get_context` + `bot.invoke`. Damit laufen alle bestehenden
Checks mit (require_same_voice, require_admin, _ensure_voice, on_command_error)
– Sprache ist kein zweiter, laxerer Weg in den Bot hinein.
"""
import copy

from discord.ext import commands

import config
from utils.checks import check_admin
from utils.i18n import t
from utils.logger import logger
from utils.nl_parser import build_wake_re, parse_utterance


def _ignoriert(grund: str) -> dict:
    return {"status": "ignored", "reason": grund}


class VoiceListen(commands.Cog):
    """Nimmt transkribierte Sätze entgegen und führt sie als Befehle aus."""

    def __init__(self, bot):
        self.bot = bot
        self.enabled = False
        self._wake_re = build_wake_re(config.VOICE_WAKE_WORDS)
        # Referenz für die synthetische Message – wird von !listen on gesetzt.
        self._ref_message = None
        self._reply_channel = None

    # --- Hilfen -------------------------------------------------------------

    def _resolve_member(self, user_id, guild_id=None):
        """Discord-Member zur User-ID. Ein echtes Member-Objekt ist Pflicht:
        nur daran hängt `.voice`, das _ensure_voice und require_same_voice
        auswerten."""
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return None

        guilds = []
        if guild_id:
            try:
                guild = self.bot.get_guild(int(guild_id))
            except (TypeError, ValueError):
                guild = None
            if guild:
                guilds.append(guild)
        if not guilds:
            guilds = list(getattr(self.bot, "guilds", []))

        for guild in guilds:
            member = guild.get_member(user_id)
            if member is not None:
                return member
        return None

    async def _antworten(self, text):
        if self._reply_channel is not None:
            await self._reply_channel.send(text)

    def _bridge_present(self) -> bool:
        """Sitzt Bot B im selben Voice-Channel?

        Dann hört dieser Bot nicht selbst zu – ein zweites Whisper daneben wäre
        doppelte Arbeit und doppelter VRAM. Bewusst keine Heuristik "irgendein
        fremder Bot": ein Recording-Bot oder Soundboard würde sonst das eigene
        Zuhören abschalten, und niemand will das debuggen.
        """
        if not config.DM_BOT_USER_ID:
            return False
        for vc in getattr(self.bot, "voice_clients", []):
            kanal = getattr(vc, "channel", None)
            if kanal and any(m.id == config.DM_BOT_USER_ID for m in kanal.members):
                return True
        return False

    # --- Der gemeinsame Eingang ---------------------------------------------

    async def handle_text(self, text, *, user_id, guild_id=None, source="bridge") -> dict:
        """Verarbeitet einen transkribierten Satz. Gibt immer ein dict zurück,
        damit der HTTP-Handler den Grund an Bot B zurückmelden kann."""
        if not self.enabled:
            return _ignoriert("disabled")

        # Solange die DM-Bridge spricht, besitzt sie den Voice-Client – ein !p
        # würde dort mitten hineinfunken (siehe ADR 0008).
        if getattr(self.bot, "dm_speaking", False):
            return _ignoriert("dm_speaking")

        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return _ignoriert("unknown_user")
        if uid in config.VOICE_BLOCKED_USER_IDS:
            return _ignoriert("user_blocked")

        member = self._resolve_member(uid, guild_id)
        if member is None:
            return _ignoriert("unknown_user")
        # Andere Bots nie: sonst gäbe Bot Bs eigene Sprachausgabe Befehle.
        if member.bot:
            return _ignoriert("user_blocked")

        woke, intent = parse_utterance(text, self._wake_re)
        if not woke:
            # Nicht an uns gerichtet – nur ins bot.log, nichts in den Chat.
            logger.info(f"[STT] {member.display_name}: \"{text}\" (kein Weckwort)")
            return _ignoriert("no_wake_word")

        if intent is None:
            logger.info(f"[Sprachbefehl] ({source}) {member.display_name}: "
                        f"nicht verstanden – \"{text}\"")
            await self._antworten(t("error.voice_not_understood",
                                    user=member.display_name, text=text))
            return _ignoriert("not_understood")

        logger.info(f"[Sprachbefehl] ({source}) {member.display_name}: \"{text}\" "
                    f"→ !{intent.command} {intent.arg}".rstrip())

        ausgefuehrt = await self._dispatch(member, intent, text)
        if ausgefuehrt is not True:
            return _ignoriert(ausgefuehrt)
        return {"status": "executed", "command": intent.command, "arg": intent.arg}

    async def _dispatch(self, member, intent, roher_text):
        """Führt den Intent als normalen Command aus. True oder ein Grund."""
        if self._ref_message is None or self._reply_channel is None:
            logger.warning("[Voice-Listen] Kein Referenz-Kontext – erst !listen on "
                           "in dem Textkanal ausführen, in dem geantwortet werden soll.")
            return "no_context"

        await self._antworten(t("status.voice_understood",
                                user=member.display_name, text=roher_text))

        fake = copy.copy(self._ref_message)
        fake.author = member           # echtes Member → .voice ist live und korrekt
        fake.channel = self._reply_channel
        fake.content = f"!{intent.command} {intent.arg}".rstrip()

        ctx = await self.bot.get_context(fake)
        if ctx.command is None:
            logger.warning(f"[Voice-Listen] Unbekannter Command: {fake.content}")
            return "unknown_command"

        await self.bot.invoke(ctx)
        return True

    # --- Der Schalter -------------------------------------------------------

    @commands.command(name="listen", usage="!listen on|off")
    async def listen_cmd(self, ctx, modus: str = None):
        """Schaltet die Sprachsteuerung an oder aus.

        Einmal pro Bot-Lauf in dem Textkanal ausführen, in dem die
        Bestätigungen erscheinen sollen – die Nachricht dient als Referenz für
        die Ausführung der gesprochenen Befehle.
        """
        if not await check_admin(ctx):
            return

        if modus is None:
            if not self.enabled:
                await ctx.send(t("status.listen_status_off"))
            else:
                quelle = "Bot B" if self._bridge_present() else "eigenes Zuhören"
                await ctx.send(t("status.listen_status_on", source=quelle))
            return

        modus = modus.strip().lower()
        if modus not in ("on", "an", "off", "aus"):
            await ctx.send(t("error.listen_usage"))
            return

        if modus in ("off", "aus"):
            self.enabled = False
            logger.info("[Voice-Listen] Deaktiviert.")
            await ctx.send(t("status.listen_off"))
            return

        if not config.VOICE_CONTROL:
            await ctx.send(t("error.listen_disabled"))
            return

        self.enabled = True
        self._ref_message = ctx.message
        self._reply_channel = ctx.channel

        if self._bridge_present():
            logger.info("[Voice-Listen] Aktiviert – Bot B ist im Channel, "
                        "kein eigenes Sprachmodell.")
            await ctx.send(t("status.listen_on_bridge", bot="Bot B"))
        else:
            logger.info(f"[Voice-Listen] Aktiviert in #{getattr(ctx.channel, 'name', '?')}.")
            await ctx.send(t("status.listen_on", wake=config.VOICE_WAKE_WORDS[0]))


async def setup(bot):
    await bot.add_cog(VoiceListen(bot))
