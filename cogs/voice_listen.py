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
import asyncio
import copy
import gc
import time

from discord.ext import commands, tasks

import config
from utils import stt
from utils.checks import check_admin
from utils.i18n import t
from utils.logger import logger
from utils.nl_parser import build_wake_re, parse_utterance
from utils.pcm import pcm48_stereo_to_f32_16k, pcm_duration_seconds, rms_dbfs
from utils.speech_buffer import SpeakerBuffers
from utils.voice import voice_client_cls

# Wie lange nach dem Verschwinden des DM-Bots gewartet wird, bevor das eigene
# Modell geladen wird. Ein kurzer Reconnect soll kein Lade-Pingpong auslösen.
BRIDGE_GONE_GRACE = 10.0

# Unter diesem Pegel gilt ein Segment als Geräusch und wird verworfen, bevor
# die GPU bemüht wird.
RAUSCH_SCHWELLE_DBFS = -50.0

# Whisper schreibt das Weckwort mit diesem Hinweis deutlich konsistenter.
STT_PROMPT = "Yo Bot, spiel mal ein Lied."


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
        # Eigenes Zuhören (nur bei VOICE_OWN_LISTEN und ohne DM-Bot im Channel)
        self._model = None
        self._model_info = ""
        self._sink = None
        self._buffers = None
        self._own_listening = False
        self._reeval_task = None
        # Modulgrenze für Tests: ersetzt die GPU durch ein festes Transkript.
        self._transcribe = None

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

    def _fremde_bots(self):
        """Andere Bots im Voice-Channel – Einrichtungshilfe.

        Ohne gesetzte DM_BOT_USER_ID greift die Weiche nicht, und der Musikbot
        würde sein Modell auch dann laden, wenn der DM-Bot mitlauscht. Statt
        den Nutzer die ID von Hand suchen zu lassen, nennt der Bot sie – er
        sieht sie ohnehin. Bei gesetzter ID ist nichts mehr zu melden.
        """
        if config.DM_BOT_USER_ID:
            return []
        eigene_id = getattr(getattr(self.bot, "user", None), "id", None)
        gefunden = []
        for vc in getattr(self.bot, "voice_clients", []):
            kanal = getattr(vc, "channel", None)
            if not kanal:
                continue
            for m in kanal.members:
                if getattr(m, "bot", False) and m.id != eigene_id:
                    gefunden.append(m)
        return gefunden

    # --- Eigenes Zuhören ----------------------------------------------------

    def _voice_client(self):
        for vc in getattr(self.bot, "voice_clients", []):
            return vc
        return None

    async def _start_own_listening(self) -> bool:
        """Lädt das Modell und hängt den Empfänger an. True bei Erfolg."""
        if self._own_listening:
            return True
        vc = self._voice_client()
        if vc is None or voice_client_cls() is None or not hasattr(vc, "listen"):
            return False

        if self._model is None:
            await self._antworten(t("status.listen_loading", model=config.VOICE_STT_MODEL))
            try:
                self._model, self._model_info = await asyncio.to_thread(
                    stt.load_model,
                    config.VOICE_STT_MODEL,
                    config.VOICE_STT_DEVICE,
                    config.VOICE_STT_COMPUTE,
                    config.VOICE_STT_ALLOW_CPU,
                )
            except Exception as e:
                logger.warning(f"[Voice-Listen] Sprachmodell nicht ladbar: {e}")
                await self._antworten(t("error.listen_no_model", err=str(e)[:150]))
                return False

        from cogs.voice_sink import SpeechSink

        self._buffers = SpeakerBuffers(
            silence_ms=config.VOICE_SILENCE_MS,
            min_ms=config.VOICE_MIN_MS,
            max_ms=config.VOICE_MAX_MS,
        )
        self._sink = SpeechSink(self._buffers, clock=time.monotonic,
                                ignorieren=lambda m: m.id in config.VOICE_BLOCKED_USER_IDS)
        vc.listen(self._sink)
        if not self._flush_loop.is_running():
            self._flush_loop.start()
        self._own_listening = True
        logger.info(f"[Voice-Listen] Eigenes Zuhören aktiv ({self._model_info}).")
        return True

    async def _stop_own_listening(self, release_model: bool = False) -> None:
        """Hängt den Empfänger ab. Gibt auf Wunsch auch den VRAM frei."""
        self._own_listening = False
        if self._flush_loop.is_running():
            self._flush_loop.cancel()

        vc = self._voice_client()
        if vc is not None and hasattr(vc, "stop_listening"):
            try:
                vc.stop_listening()
            except Exception:
                pass
        if self._sink is not None:
            try:
                self._sink.cleanup()
            except Exception:
                pass
            self._sink = None
        if self._buffers is not None:
            self._buffers.clear()

        if release_model and self._model is not None:
            # ctranslate2 gibt den VRAM im Destruktor frei; gc.collect() ist
            # die Garantie, dass der auch wirklich läuft.
            self._model = None
            self._model_info = ""
            gc.collect()
            logger.info("[Voice-Listen] Sprachmodell freigegeben (VRAM).")

    async def _reevaluate(self, grace: float = BRIDGE_GONE_GRACE) -> None:
        """Entscheidet, ob selbst zugehört wird – abhängig vom DM-Bot.

        Asymmetrisch, weil die Kosten es sind: Abschalten ist gratis und muss
        sofort passieren (kein zweites Whisper parallel). Anschalten kostet
        Ladezeit und VRAM und wartet deshalb eine Karenz ab, damit ein kurzer
        Reconnect des DM-Bots kein Lade-Pingpong auslöst.
        """
        if not self.enabled:
            return

        if self._bridge_present():
            if self._own_listening or self._model is not None:
                await self._stop_own_listening(release_model=True)
                await self._antworten(t("status.listen_source_bridge"))
            return

        if self._own_listening or not config.VOICE_OWN_LISTEN:
            return

        await asyncio.sleep(grace)
        if self._bridge_present() or not self.enabled:
            return
        if await self._start_own_listening():
            await self._antworten(t("status.listen_source_own", model=self._model_info))

    @tasks.loop(seconds=0.2)
    async def _flush_loop(self):
        """Holt fertige Äußerungen aus den Puffern in den Event-Loop."""
        if not self._buffers:
            return
        for user_id, pcm in self._buffers.due(time.monotonic()):
            await self._verarbeite_segment(user_id, pcm)

    async def _verarbeite_segment(self, user_id: int, pcm: bytes) -> None:
        """Ein fertiges Sprachsegment: Pegel prüfen, transkribieren, ausführen."""
        pegel = rms_dbfs(pcm)
        if pegel < RAUSCH_SCHWELLE_DBFS:
            logger.info(f"[STT] Segment verworfen (zu leise, {pegel:.0f} dBFS, "
                        f"{pcm_duration_seconds(len(pcm)):.1f}s)")
            return

        audio = pcm48_stereo_to_f32_16k(pcm)
        try:
            if self._transcribe is not None:
                text = self._transcribe(audio)
            else:
                text = await asyncio.to_thread(
                    stt.transcribe, self._model, audio, initial_prompt=STT_PROMPT)
        except Exception as e:
            logger.warning(f"[STT] Transkription fehlgeschlagen: {type(e).__name__}: {e}")
            return

        if not (text or "").strip():
            return
        await self.handle_text(text, user_id=user_id, source="eigen")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Beobachtet den DM-Bot. Der Listener im Music-Cog steigt bei Bots
        sofort aus, deshalb hier ein eigener."""
        if not self.enabled or not config.DM_BOT_USER_ID:
            return
        if member.id != config.DM_BOT_USER_ID:
            return
        if self._reeval_task and not self._reeval_task.done():
            self._reeval_task.cancel()
        self._reeval_task = asyncio.create_task(self._reevaluate())

    async def cog_unload(self):
        # Sonst überlebt der Empfangs-Thread ein !restart.
        if self._reeval_task and not self._reeval_task.done():
            self._reeval_task.cancel()
        await self._stop_own_listening(release_model=True)

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
            logger.info("[Voice-Listen] Aktiviert – DM-Bot ist im Channel, "
                        "kein eigenes Sprachmodell.")
            await ctx.send(t("status.listen_on_bridge", bot="DM-Bot"))
            return

        logger.info(f"[Voice-Listen] Aktiviert in #{getattr(ctx.channel, 'name', '?')}.")
        await ctx.send(t("status.listen_on", wake=config.VOICE_WAKE_WORDS[0]))

        # Einrichtungshilfe: ohne DM_BOT_USER_ID läuft das eigene Modell auch
        # dann, wenn der DM-Bot mitlauscht. Die ID gleich mitliefern.
        for fremd in self._fremde_bots():
            logger.info(f"[Voice-Listen] Fremder Bot im Channel: "
                        f"{fremd.display_name} (ID {fremd.id})")
            await ctx.send(t("status.listen_hint_dm_bot",
                             bot=fremd.display_name, id=fremd.id))

        # Ohne DM-Bot im Channel selbst zuhören – hier ohne Karenz, weil der
        # Einschaltbefehl eine ausdrückliche Ansage ist.
        if config.VOICE_OWN_LISTEN:
            await self._start_own_listening()


async def setup(bot):
    await bot.add_cog(VoiceListen(bot))
