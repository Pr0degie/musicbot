"""Voice-Lifecycle und Now-Playing-UI für MusicCommands.

Zwei Mixins, per Mehrfachvererbung in MusicCommands eingebunden:

- VoiceLifecycleMixin — Pflege der Voice-Verbindung: Idle-/Auto-Leave-Timer,
  der 1006-Reconnect-Watchdog und der on_voice_state_update-Listener, der bei
  leerem Channel den Auto-Leave-Timer startet.
- PlaybackUiMixin — Now-Playing-Anzeige: der 2-Sekunden-Progress-Loop, das
  Finalisieren des Balkens am Songende sowie die Pause-/Resume-Buchführung.

Beide greifen auf denselben Instanz-State zu, der in cogs/music.py angelegt und
verwaltet wird (u. a. self.current_track, self.now_playing_msg,
self.now_playing_embed, self._track_generation, self.track_start_time,
self.idle_leave_task, self.auto_leave_task, self._last_ctx, self.is_radio),
sowie auf dort definierte Kern-Methoden (self.play_next, self._stop_for_advance,
self._progress_bar). Die Loops werden von cog_load/cog_unload in cogs/music.py
gestartet und gestoppt.
"""

import asyncio
import time

import discord
from discord.ext import commands, tasks

from utils.logger import logger
from utils.i18n import t


class VoiceLifecycleMixin:
    async def _auto_leave(self, voice_client):
        """Verlässt den Channel nach AUTO_LEAVE_SECONDS wenn kein User mehr drin ist."""
        await asyncio.sleep(self.AUTO_LEAVE_SECONDS)
        if voice_client.is_connected():
            self.queue.clear()
            self.is_playing = False
            self.current_track = None
            if voice_client.is_playing() or voice_client.is_paused():
                self._stop_for_advance(voice_client)
            await voice_client.disconnect()
            if self.text_channel:
                await self.text_channel.send(t("misc.auto_leave"))
            logger.info("[Auto-Leave] Channel verlassen – keine User mehr.")

    def _cancel_idle_timer(self):
        """Stoppt den Idle-Timer – aufgerufen sobald wieder Audio läuft."""
        if self.idle_leave_task and not self.idle_leave_task.done():
            self.idle_leave_task.cancel()
        self.idle_leave_task = None

    def _start_idle_timer(self, voice_client):
        """(Re)startet den Idle-Timer. Bei anhaltender Stille verlässt der Bot
        später den Channel, damit Discord die Verbindung nicht mit 1006 dropt."""
        self._cancel_idle_timer()
        self.idle_leave_task = asyncio.create_task(self._idle_leave(voice_client))

    async def _idle_leave(self, voice_client):
        """Verlässt den Channel nach IDLE_LEAVE_SECONDS ohne Wiedergabe."""
        await asyncio.sleep(self.IDLE_LEAVE_SECONDS)
        # Nur gehen wenn wirklich noch nichts läuft (Autoplay/neuer Song hätte
        # den Timer längst via _cancel_idle_timer abgeräumt – doppelte Sicherung).
        if (
            voice_client.is_connected()
            and not self.is_radio
            and not voice_client.is_playing()
            and not voice_client.is_paused()
        ):
            self.queue.clear()
            self.is_playing = False
            self.current_track = None
            await voice_client.disconnect()
            if self.text_channel:
                await self.text_channel.send(t("misc.idle_leave"))
            logger.info("[Auto-Leave] Channel verlassen – zu lange inaktiv (Idle).")

    @tasks.loop(seconds=30)
    async def _voice_watchdog(self):
        """Sicherheitsnetz gegen den 1006-Reconnect-Bug.

        Wenn Discord die Voice-Verbindung während eines Songs dropt (Code 1006),
        reconnectet discord.py automatisch – aber die Wiedergabe bleibt manchmal
        stehen (Queue voll, doch is_playing False). Hängt dieser Zustand über
        zwei Ticks (~60 s) an, wird der nächste Song neu gestartet. Zwei Ticks,
        damit normale Track-Übergänge (Sekundenbereich) nicht fälschlich greifen.
        """
        ctx = self._last_ctx
        vc = ctx.voice_client if ctx else None
        stuck = (
            vc is not None
            and vc.is_connected()
            and not self.is_radio
            and bool(self.queue)
            and not self.is_playing
            and not vc.is_playing()
            and not vc.is_paused()
            and not self._stopped_by_user
            and self._playback_done.is_set()
        )
        if not stuck:
            self._stuck_ticks = 0
            return
        self._stuck_ticks += 1
        if self._stuck_ticks >= 2:
            self._stuck_ticks = 0
            logger.info("[Voice] Wiedergabe nach Reconnect hängen geblieben – setze fort.")
            try:
                await self.play_next(ctx)
            except Exception as e:
                logger.warning(f"[Voice] Watchdog-Restart fehlgeschlagen: {e}")

    @_voice_watchdog.before_loop
    async def _before_voice_watchdog(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Startet den Auto-Leave-Timer wenn alle User den Channel verlassen haben."""
        if member.bot:
            return
        voice_client = member.guild.voice_client
        if not voice_client:
            return

        humans = [m for m in voice_client.channel.members if not m.bot]
        if len(humans) == 0:
            # Alle weg – Timer starten (alten zuerst abbrechen falls noch einer läuft)
            if self.auto_leave_task:
                self.auto_leave_task.cancel()
            self.auto_leave_task = asyncio.create_task(self._auto_leave(voice_client))
            logger.info("[Auto-Leave] Channel leer – starte 5-Minuten-Timer.")
        else:
            # Jemand ist (wieder) da – Timer abbrechen
            if self.auto_leave_task and not self.auto_leave_task.done():
                self.auto_leave_task.cancel()
                self.auto_leave_task = None


class PlaybackUiMixin:
    def _elapsed_seconds(self):
        """Abgespielte Sekunden des aktuellen Songs, Pausen herausgerechnet."""
        if self.track_start_time is None:
            return None
        now = time.monotonic()
        paused = self._np_paused_total
        if self._np_paused_at is not None:
            paused += now - self._np_paused_at
        return max(0.0, now - self.track_start_time - paused)

    def _mark_paused(self):
        """Pause-Beginn merken (idempotent), damit der Balken einfriert."""
        if self._np_paused_at is None:
            self._np_paused_at = time.monotonic()

    def _mark_resumed(self):
        """Pausendauer aufsummieren, damit der Balken nicht vorspringt."""
        if self._np_paused_at is not None:
            self._np_paused_total += time.monotonic() - self._np_paused_at
            self._np_paused_at = None

    async def _retire_np_message(self, msg, title=None):
        """Baut eine Now-Playing-Nachricht auf eine reine Textzeile zurück:
        Embed und Buttons weg – **nur der aktuelle Song behält seine Karte**.

        Einziger Rückbau-Pfad; jede Stelle, die eine Karte hinter sich lässt
        (Trackwechsel, Queue-Ende, Radio-Übernahme, 429-Abschaltung), geht hier
        durch. Der Inhalt darf dabei nie leer werden: eine Nachricht ohne Text,
        Embed und Anhang lehnt Discord mit 400 ab – der Rückbau schlüge still
        fehl und die alte Karte bliebe samt Buttons stehen.
        """
        if msg is None:
            return
        try:
            await msg.edit(
                content=f"🎶 {title or t('misc.unknown_title')}",
                embed=None, view=None,
            )
        except Exception:
            pass   # Nachricht schon weg oder keine Berechtigung – egal

    async def _finalize_progress_bar(self):
        """Setzt Balken + Dauer der aktuellen Nachricht ans Ende (100 %).

        Wird beim natürlichen Songende aufgerufen, damit die Anzeige nicht bei
        z. B. '3:43 / 3:45' einfriert, sondern auf '🔘 ganz rechts, 3:45 / 3:45' springt.
        """
        msg, embed = self.now_playing_msg, self.now_playing_embed
        if not (msg and embed and self.current_track):
            return
        duration = self.current_track[2]
        bar = self._progress_bar(duration, duration)   # elapsed == total → 🔘 ganz rechts
        if not bar or bar == self._np_last_desc:
            return
        embed.description = bar
        try:
            await msg.edit(embed=embed)
            self._np_last_desc = bar
        except Exception:
            pass

    @tasks.loop(seconds=2)
    async def _progress_loop(self):
        """Aktualisiert den Fortschrittsbalken in der aktuellen Now-Playing-Nachricht."""
        msg, embed = self.now_playing_msg, self.now_playing_embed
        if not (msg and embed and self.current_track and self.is_radio is False):
            return
        # Nur editieren, wenn wirklich etwas läuft: is_playing ODER pausiert
        # (der Pause-Button setzt is_playing=False, der Balken soll aber das
        # ⏸-Präfix noch bekommen). Zusätzlich muss der Voice-Client den Track
        # tatsächlich spielen/pausiert haben – tote Tracks editieren wir nie.
        if not self.is_playing and self._np_paused_at is None:
            return
        vc = self._last_ctx.voice_client if self._last_ctx else None
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return
        duration = self.current_track[2]
        elapsed = self._elapsed_seconds()
        if elapsed is None:
            return
        bar = self._progress_bar(elapsed, duration)
        if not bar:
            return
        if self._np_paused_at is not None:   # pausiert
            bar = "⏸ " + bar
        if bar == self._np_last_desc:        # nichts Neues → kein API-Call
            return
        embed.description = bar
        try:
            await msg.edit(embed=embed)      # view-Param weglassen → Buttons bleiben
            self._np_last_desc = bar
        except discord.RateLimited:
            self._disable_np_edits(msg)
        except discord.HTTPException as e:
            if e.status == 429:
                self._disable_np_edits(msg)
        except Exception:
            pass

    def _disable_np_edits(self, msg):
        """Rate-Limit (429): Live-Edits für diese Nachricht dauerhaft einstellen –
        einmal loggen statt still weiterzuhämmern."""
        logger.warning(
            "[Progress] 429 beim Edit der Now-Playing-Nachricht – "
            "Live-Updates für diese Nachricht deaktiviert."
        )
        if self.now_playing_msg is msg:
            # Die Nachricht nicht einfach vergessen: sonst behielte genau diese
            # Karte für immer Embed und Buttons. Sie wandert in _ended_np, der
            # nächste Track baut sie zurück. (_ended_np ist hier frei – der
            # Cleanup füllt es nur, wenn now_playing_msg gesetzt war, und
            # danach editiert der Progress-Loop nichts mehr.)
            self._ended_np = (msg, self._np_title)
            self.now_playing_msg = None
            self.now_playing_embed = None
            self._np_title = None

    @_progress_loop.before_loop
    async def _before_progress_loop(self):
        await self.bot.wait_until_ready()
