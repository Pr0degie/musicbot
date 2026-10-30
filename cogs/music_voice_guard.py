"""Mute- und Move-Schutz für MusicCommands.

**Mute-Schutz** (MUTE_GUARD, Default an): Ein Server-Mute hängt am Bot-Account
im Server, nicht an der Verbindung – Rejoin oder Neustart helfen nicht. Hat die
Bot-Rolle "Mitglieder stummschalten", hebt der Bot den Mute sofort selbst auf.
Er gibt nie auf, egal wie oft jemand mutet. Gegen eine Endlosschleife (etwa ein
anderer Bot, der sofort zurückmutet) wartet er ab dem (FAST_UNMUTES+1). Mal
innerhalb von WINDOW_SECONDS jeweils BACKOFF_SECONDS, die Musik läuft dabei
weiter. Nur wenn das Recht fehlt, pausiert er, statt ins Leere zu spielen, sagt
im Chat Bescheid und spielt weiter, sobald ihn jemand entmutet. Radio wird
nicht pausiert – ein angehaltener Live-Stream bricht ab.

**Move-Schutz** (MOVE_GUARD, Default aus – ADR 0004): Nur der Bot-Owner darf
den Bot in einen anderen Channel verschieben. Discord kann das nicht verbieten;
der Bot merkt aber, dass er nicht mehr dort ist, wo er selbst hinwollte
(`utils.voice.intended_channel_id`), und geht zurück. Wer verschoben hat, steht
im Audit-Log (Recht "Audit-Log anzeigen"); war es der Owner, bleibt er. Ohne
das Recht geht er immer zurück – der Owner nimmt ihn dann per `!j` mit.
"""

import asyncio
import time

import discord
from discord.ext import commands

import config
from utils.i18n import t
from utils.logger import logger
from utils.voice import intended_channel_id, move_voice, set_intended_channel

FAST_UNMUTES = 3       # so oft pro Fenster sofort entmuten
WINDOW_SECONDS = 300
BACKOFF_SECONDS = 3    # danach vor jedem Entmuten/Zurückgehen kurz warten
AUDIT_FRESH_SECONDS = 30   # so alt darf ein neuer Audit-Log-Eintrag höchstens sein


def _backoff_due(stamps):
    """True, wenn schon FAST_UNMUTES Eingriffe im Fenster liegen."""
    now = time.monotonic()
    while stamps and now - stamps[0] > WINDOW_SECONDS:
        stamps.popleft()
    return len(stamps) >= FAST_UNMUTES


class VoiceGuardMixin:
    """State (self._mute_unmutes, self._mute_blocked, self._paused_by_mute,
    self._move_backs, self._move_lock, self._audit_seen, self._audit_warned)
    legt cogs/music.py an; dazu self.bot, self.text_channel, self.is_radio,
    self.is_playing, self._stopped_by_user, _mark_paused/_mark_resumed."""

    @commands.Cog.listener("on_voice_state_update")
    async def _mute_guard(self, member, before, after):
        if not config.MUTE_GUARD:
            return
        if self.bot.user is None or member.id != self.bot.user.id:
            return
        if after.channel is None:
            self._mute_blocked = False
            self._paused_by_mute = False
            return

        if before.mute and not after.mute:
            await self._on_unmuted(member)
            return
        # Neu gemutet – oder schon gemutet beigetreten (before.channel ist None).
        if after.mute and not (before.mute and before.channel is not None):
            await self._on_muted(member, after.channel)

    async def _on_muted(self, member, channel):
        if not channel.permissions_for(member).mute_members:
            logger.warning("[Mute-Schutz] Server-Mute, aber der Bot-Rolle fehlt "
                           "'Mitglieder stummschalten' – kann mich nicht entmuten.")
            await self._mute_pause(member, t("status.mute_no_permission"))
            return

        if _backoff_due(self._mute_unmutes):
            await asyncio.sleep(BACKOFF_SECONDS)
            voice = getattr(member, "voice", None)
            if voice is None or not voice.mute:
                return   # inzwischen entmutet oder raus aus dem Channel

        try:
            await member.edit(mute=False, reason="Mute-Schutz")
        except discord.Forbidden as e:
            logger.warning(f"[Mute-Schutz] Entmuten verboten: {e}")
            await self._mute_pause(member, t("status.mute_no_permission"))
            return
        except discord.HTTPException as e:
            logger.warning(f"[Mute-Schutz] Entmuten fehlgeschlagen: {type(e).__name__}: {e}")
            return
        self._mute_unmutes.append(time.monotonic())
        logger.info(f"[Mute-Schutz] Server-Mute aufgehoben ({len(self._mute_unmutes)}x "
                    f"in {WINDOW_SECONDS // 60} Min.).")

    async def _mute_pause(self, member, text):
        """Pausiert wie !x, aber merkt sich, dass der Mute der Grund war."""
        self._mute_blocked = True
        vc = member.guild.voice_client
        if vc and vc.is_playing() and not self.is_radio and not self._paused_by_mute:
            vc.pause()
            self._mark_paused()
            self.is_playing = False
            self._stopped_by_user = True   # Watchdog soll nicht neu starten
            self._paused_by_mute = True
        await self._mute_notice(text)

    async def _on_unmuted(self, member):
        if not self._mute_blocked:
            return   # Unser eigenes Entmuten – nichts zu tun
        # Jemand anderes hat entmutet: Pause aufheben.
        self._mute_blocked = False
        paused, self._paused_by_mute = self._paused_by_mute, False
        vc = member.guild.voice_client
        if paused and vc and vc.is_paused():
            vc.resume()
            self._mark_resumed()
            self.is_playing = True
            self._stopped_by_user = False
            logger.info("[Mute-Schutz] Entmutet – Wiedergabe läuft weiter.")
            await self._mute_notice(t("status.mute_resumed"))
        else:
            logger.info("[Mute-Schutz] Entmutet.")

    async def _mute_notice(self, text, delete_after=300):
        if self.text_channel is None:
            return
        try:
            await self.text_channel.send(text, delete_after=delete_after)
        except Exception:
            pass   # Kein Schreibrecht o. Ä. – das Log reicht

    # --- Move-Schutz ---------------------------------------------------------

    @commands.Cog.listener("on_voice_state_update")
    async def _move_guard(self, member, before, after):
        if not config.MOVE_GUARD:
            return
        if self.bot.user is None or member.id != self.bot.user.id:
            return
        guild = member.guild
        if before.channel is None and after.channel is not None:
            # Beigetreten: Audit-Log-Stand merken, damit der nächste Move als
            # neu erkannt wird (Discord fasst Moves zu einem Eintrag zusammen).
            await self._who_moved(guild, None, wait=False)
            return
        if before.channel is None or after.channel is None:
            return
        if before.channel.id == after.channel.id:
            return
        if after.channel.id == intended_channel_id(guild.id):
            return   # eigener Wechsel (!j, Zurückgehen)
        async with self._move_lock:
            await self._handle_foreign_move(guild, after.channel)

    async def _handle_foreign_move(self, guild, channel):
        home_id = intended_channel_id(guild.id)
        home = guild.get_channel(home_id) if home_id else None
        if home is None:
            set_intended_channel(channel)   # nichts zum Zurückgehen – bleibt halt
            return

        actor = await self._who_moved(guild, channel.id)
        if actor is not None and await self.bot.is_owner(actor):
            set_intended_channel(channel)
            logger.info(f"[Move-Schutz] Owner hat mich nach {channel.name} verschoben – bleibe.")
            return

        if _backoff_due(self._move_backs):
            await asyncio.sleep(BACKOFF_SECONDS)
        vc = guild.voice_client
        now = getattr(getattr(guild.me, "voice", None), "channel", None)
        if vc is None or now is None or now.id == home.id:
            return   # inzwischen raus oder schon zurück
        who = getattr(actor, "display_name", None) or "unbekannt"
        try:
            await move_voice(vc, home)
        except Exception as e:
            logger.warning(f"[Move-Schutz] Zurück nach {home.name} fehlgeschlagen: {type(e).__name__}: {e}")
            return
        self._move_backs.append(time.monotonic())
        logger.info(f"[Move-Schutz] Von {who} nach {channel.name} verschoben – zurück nach {home.name}.")
        if len(self._move_backs) == 1:   # bei Move-Kriegen nicht den Chat fluten
            await self._mute_notice(t("status.move_reverted", channel=home.name), delete_after=120)

    async def _who_moved(self, guild, channel_id, wait=True):
        """Wer hat zuletzt jemanden in channel_id verschoben? None = unbekannt.

        Discord fasst Moves desselben Moderators zu einem Eintrag zusammen und
        zählt nur `extra.count` hoch – neu ist also ein unbekannter, frischer
        Eintrag oder einer mit gestiegenem Zähler. channel_id=None merkt sich
        nur den aktuellen Stand. Der Eintrag erscheint teils mit Verzögerung,
        daher bis zu zwei Versuche.
        """
        if not guild.me.guild_permissions.view_audit_log:
            if channel_id is not None and not self._audit_warned:
                self._audit_warned = True
                logger.warning("[Move-Schutz] Recht 'Audit-Log anzeigen' fehlt – kann nicht erkennen, "
                               "wer verschoben hat, gehe immer zurück.")
            return None
        for _ in range(2 if wait else 1):
            if wait:
                await asyncio.sleep(1)
            try:
                entries = [e async for e in guild.audit_logs(
                    limit=10, action=discord.AuditLogAction.member_move)]
            except discord.HTTPException as e:
                logger.warning(f"[Move-Schutz] Audit-Log nicht lesbar: {type(e).__name__}: {e}")
                return None
            seen, found = self._audit_seen, None
            jetzt = discord.utils.utcnow()
            for e in entries:   # neueste zuerst
                count = getattr(e.extra, "count", 1) or 1
                ch = getattr(e.extra, "channel", None)
                if e.id in seen:
                    neu = count > seen[e.id]
                else:
                    neu = (jetzt - e.created_at).total_seconds() < AUDIT_FRESH_SECONDS
                if found is None and neu and ch is not None and ch.id == channel_id:
                    found = e.user
            self._audit_seen = {e.id: getattr(e.extra, "count", 1) or 1 for e in entries}
            if found is not None or channel_id is None:
                return found
        return None
