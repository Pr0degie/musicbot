"""RadioMixin: Internet-Radio-Wiedergabe für MusicCommands.

Enthält ausschließlich Radio-Logik (Stream via FFmpeg, Reconnect, Senderverwaltung).
Wird per Mehrfachvererbung in MusicCommands eingebunden und greift auf denselben
Instanz-State (self.is_radio, self.queue, self._playback_done, ...) sowie auf
Kern-Methoden (self._ensure_voice) zu, die in cogs/music.py definiert sind.
"""

import asyncio
import json
import time
from pathlib import Path

import discord
from discord.ext import commands

from utils.logger import logger
from utils.i18n import t
from views.music_controls import MusicControlView

RADIO_STATIONS_FILE = Path("radio_stations.json")


class RadioMixin:
    def _stop_radio(self):
        """Beendet Radio-Modus und setzt alle Flags zurück. Stoppt den Voice-Client NICHT."""
        self.is_radio = False
        self.radio_station_name = None
        self.radio_stream_url = None
        self._radio_reconnect_count = 0
        self.is_playing = False
        if self._autoplay_prefetch_task and not self._autoplay_prefetch_task.done():
            self._autoplay_prefetch_task.cancel()
            self._autoplay_prefetch_task = None

    async def _play_radio_stream(self, ctx, url: str, name: str) -> None:
        """Startet einen Internet-Radio-Stream direkt über FFmpeg (kein yt_dlp)."""
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            await ctx.send(t("error.no_voice_connected"))
            return

        # Sicherheitsnetz: falls FFmpeg noch nicht terminiert ist, kurz warten.
        if ctx.voice_client.is_playing():
            try:
                await asyncio.wait_for(self._playback_done.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                await ctx.send(t("error.stream_busy"))
                self._stop_radio()
                return
            self._playback_done.clear()

        if self.prefetch_task and not self.prefetch_task.done():
            self.prefetch_task.cancel()

        self._cancel_idle_timer()  # Falls ein Song gerade endete und der Idle-Timer lief
        self._stopped_by_user = False

        self.radio_stream_url = url
        self.radio_station_name = name
        self.is_radio = True
        self.is_playing = True
        self.current_track = (url, name, 0)

        try:
            source = discord.FFmpegOpusAudio(
                url,
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options="-vn",
            )
        except Exception:
            logger.exception(f"[Radio] Fehler beim Erstellen der FFmpeg-Quelle für {url}")
            await ctx.send(t("error.stream_connect_error"))
            self._stop_radio()
            return

        def after_radio(error):
            self.bot.loop.call_soon_threadsafe(self._playback_done.set)
            if error:
                logger.warning(f"[Radio] Stream-Fehler: {error}")
            else:
                logger.info(f"[Radio] Stream unerwartet beendet (kein Fehler) – Reconnect wird versucht.")
            if not self.is_radio:
                return
            if self._radio_reconnect_count < 3:
                self._radio_reconnect_count += 1
                logger.info(f"[Radio] Reconnect {self._radio_reconnect_count}/3 für {name}")
                asyncio.run_coroutine_threadsafe(self._reconnect_radio(ctx), self.bot.loop)
            else:
                logger.warning(f"[Radio] Max. Reconnect-Versuche für {name} erreicht.")
                self.is_radio = False
                self.is_playing = False
                asyncio.run_coroutine_threadsafe(
                    ctx.send(t("radio.stream_interrupted"), delete_after=30),
                    self.bot.loop,
                )

        if self.now_playing_msg:
            try:
                await self.now_playing_msg.edit(content=None, embed=None, view=None)
            except Exception:
                pass

        self._playback_done.clear()
        self.track_start_time = time.monotonic()
        ctx.voice_client.play(source, after=after_radio)
        self._radio_reconnect_count = 0

        embed = discord.Embed(title=f"📻 {name}", color=0xe74c3c)
        embed.add_field(name=t("embed.status"), value=t("embed.radio_live"), inline=True)
        embed.add_field(name=t("embed.eq"), value=self.equalizer, inline=True)
        self.now_playing_msg = await ctx.send(embed=embed, view=MusicControlView(self, ctx))
        self.text_channel = ctx.channel
        logger.info(f"[Radio] Stream gestartet: {name} ({url})")

    async def _reconnect_radio(self, ctx) -> None:
        """Versucht einen abgebrochenen Radio-Stream neu zu verbinden."""
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            logger.warning("[Radio] Kein Voice-Client mehr – Reconnect abgebrochen.")
            self._stop_radio()
            return
        await asyncio.sleep(2)
        try:
            await ctx.send(
                t("radio.reconnecting", count=self._radio_reconnect_count, name=self.radio_station_name),
                delete_after=10,
            )
        except Exception:
            pass
        await self._play_radio_stream(ctx, self.radio_stream_url, self.radio_station_name)

    @staticmethod
    def _resolve_station_entry(query: str, items: list):
        """Gibt (key, entry) für Nr. oder Name zurück, oder (None, None) wenn nicht gefunden."""
        if query.isdigit():
            idx = int(query) - 1
            if 0 <= idx < len(items):
                return items[idx]
            return None, None
        normalized = query.lower().replace(" ", "").replace("-", "")
        for k, v in items:
            if k.lower().replace(" ", "").replace("-", "") == normalized:
                return k, v
        return None, None

    @commands.command(name="radio")
    async def radio_play(self, ctx, *, eingabe: str = None):
        """Spielt einen Internet-Radio-Stream. !radio <Nummer, Sendername oder URL>"""
        try:
            with open(RADIO_STATIONS_FILE, encoding="utf-8") as f:
                stations = json.load(f)
        except FileNotFoundError:
            stations = {}

        if not eingabe:
            if not stations:
                await ctx.send(t("error.no_stations"))
                return
            lines = [
                f"**{i + 1}.** {v['name']}"
                for i, (k, v) in enumerate(stations.items())
            ]
            await ctx.send(t("radio.station_list", lines="\n".join(lines)))
            return

        eingabe = eingabe.strip()
        items = list(stations.items())

        # Subcommands: !radio delete <nr|name>  /  !radio rename <nr|name> <neuer name>
        tokens = eingabe.split(None, 2)
        if tokens[0] in ("delete", "rename"):
            subcmd = tokens[0]
            if len(tokens) < 2:
                await ctx.send(t("error.radio_usage", subcmd=subcmd))
                return
            key, entry = self._resolve_station_entry(tokens[1], items)
            if entry is None:
                await ctx.send(t("error.station_not_found", name=tokens[1]))
                return
            if subcmd == "delete":
                del stations[key]
                with open(RADIO_STATIONS_FILE, "w", encoding="utf-8") as f:
                    json.dump(stations, f, ensure_ascii=False, indent=2)
                await ctx.send(t("radio.station_deleted", name=entry["name"]))
            else:  # rename
                if len(tokens) < 3:
                    await ctx.send(t("error.radio_rename_usage"))
                    return
                old_name = entry["name"]
                stations[key]["name"] = tokens[2]
                with open(RADIO_STATIONS_FILE, "w", encoding="utf-8") as f:
                    json.dump(stations, f, ensure_ascii=False, indent=2)
                await ctx.send(t("radio.station_renamed", old=old_name, new=tokens[2]))
            return

        if not await self._ensure_voice(ctx):
            return

        if eingabe.startswith("http"):
            parts = eingabe.split(None, 1)
            url = parts[0]
            if len(parts) > 1:
                name = parts[1].strip()
            else:
                from urllib.parse import urlparse
                name = urlparse(url).hostname or url

            # Sender speichern falls noch nicht vorhanden
            existing = next((v for v in stations.values() if v["url"] == url), None)
            if existing is None:
                key = name.lower().replace(" ", "").replace("-", "")
                # Eindeutigen Key sicherstellen
                base_key, n = key, 2
                while key in stations:
                    key = f"{base_key}{n}"
                    n += 1
                stations[key] = {"name": name, "url": url}
                with open(RADIO_STATIONS_FILE, "w", encoding="utf-8") as f:
                    json.dump(stations, f, ensure_ascii=False, indent=2)
                await ctx.send(t("radio.station_saved", name=name, num=len(stations)))
        else:
            _, entry = self._resolve_station_entry(eingabe, items)
            if entry is None:
                if eingabe.isdigit():
                    await ctx.send(t("error.station_number_invalid", num=eingabe, count=len(items)))
                else:
                    await ctx.send(t("error.station_name_not_found", name=eingabe))
                return
            url = entry["url"]
            name = entry["name"]

        if self.is_radio:
            self._stop_radio()
        else:
            self.is_playing = False

        # Immer warten bis FFmpeg wirklich fertig ist – egal ob Radio oder Song lief.
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            try:
                await asyncio.wait_for(self._playback_done.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            self._playback_done.clear()

        self._radio_reconnect_count = 0
        await self._play_radio_stream(ctx, url, name)
