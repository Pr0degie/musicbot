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

from utils.files import atomic_write_text
from utils.logger import logger
from utils.voice import PlayerToken, start_clock_at_first_packet, stop_playback
from utils.i18n import t
from utils.url_check import enforce_url_policy
from utils.checks import check_admin
from views.music_controls import MusicControlView

RADIO_STATIONS_FILE = Path("radio_stations.json")
# Erst nach so langem stabilem Lauf gilt ein Abbruch als neuer Vorfall und
# bekommt wieder volle RADIO_MAX_RECONNECTS Versuche. Sonst würde ein Sender,
# der sich verbindet und sofort wieder abbricht, endlos neu verbunden.
RADIO_STABLE_SECONDS = 60
RADIO_MAX_RECONNECTS = 3


def _write_stations(stations: dict) -> None:
    """Blockierender Datei-Write – immer via asyncio.to_thread aufrufen,
    damit der Event-Loop auf schwacher Hardware nicht hängt."""
    atomic_write_text(RADIO_STATIONS_FILE, json.dumps(stations, ensure_ascii=False, indent=2))


def _load_stations() -> dict:
    """Liest radio_stations.json. Fehlt die Datei → {}.

    Eine kaputte Datei (kein gültiges JSON, keine Senderliste, einzelne
    Einträge ohne name/url – typisch nach einer Handänderung) darf !radio nicht
    dauerhaft crashen lassen: brauchbare Einträge werden übernommen, der Rest
    ignoriert. Vorher wandert eine Kopie nach radio_stations.json.broken, denn
    das nächste Speichern überschreibt die Datei mit dem bereinigten Stand."""
    try:
        with open(RADIO_STATIONS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError as e:   # JSONDecodeError, UnicodeDecodeError
        _backup_broken_stations(f"nicht lesbar ({e})")
        return {}
    if not isinstance(data, dict):
        _backup_broken_stations("keine Senderliste")
        return {}
    stations = {
        key: entry for key, entry in data.items()
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("url"), str)
    }
    if len(stations) != len(data):
        _backup_broken_stations(f"{len(data) - len(stations)} unbrauchbare Einträge ignoriert")
    return stations


def _backup_broken_stations(grund: str) -> None:
    backup = RADIO_STATIONS_FILE.with_name(RADIO_STATIONS_FILE.name + ".broken")
    try:
        backup.write_bytes(RADIO_STATIONS_FILE.read_bytes())
        logger.warning(f"[Radio] {RADIO_STATIONS_FILE.name} ist beschädigt: {grund}. Kopie gesichert als {backup.name}.")
    except OSError:
        logger.warning(f"[Radio] {RADIO_STATIONS_FILE.name} ist beschädigt: {grund}.")


class RadioMixin:
    # PlayerToken des Senders, der gerade laufen soll – None, sobald der
    # Radio-Modus beendet wurde. Nur der after-Callback dieses Senders darf
    # einen Reconnect anstoßen: der Callback eines gestoppten Players läuft im
    # Player-Thread oft erst, wenn der nächste Sender is_radio schon wieder
    # gesetzt hat – is_radio allein unterscheidet die beiden nicht.
    _radio_session = None

    def _stop_radio(self):
        """Beendet Radio-Modus und setzt alle Flags zurück. Stoppt den Voice-Client NICHT."""
        self.is_radio = False
        self._radio_session = None
        self.radio_station_name = None
        self.radio_stream_url = None
        self._radio_reconnect_count = 0
        self.is_playing = False
        if self._autoplay_prefetch_task and not self._autoplay_prefetch_task.done():
            self._autoplay_prefetch_task.cancel()
            self._autoplay_prefetch_task = None

    def _radio_should_reconnect(self, ran_seconds: float) -> bool:
        """Nach einem Stream-Abbruch: noch ein Reconnect-Versuch? Zählt mit.

        Der Zähler wird nicht beim Start zurückgesetzt (der Reconnect startet
        über _play_radio_stream, die Kappe griffe sonst nie), sondern erst,
        wenn der Stream RADIO_STABLE_SECONDS lang lief."""
        if ran_seconds >= RADIO_STABLE_SECONDS:
            self._radio_reconnect_count = 0
        if self._radio_reconnect_count >= RADIO_MAX_RECONNECTS:
            return False
        self._radio_reconnect_count += 1
        return True

    async def _play_radio_stream(self, ctx, url: str, name: str) -> bool:
        """Startet einen Internet-Radio-Stream direkt über FFmpeg (kein yt_dlp).

        Gibt True zurück wenn der Stream gestartet wurde – radio_play persistiert
        neue Sender nur dann.
        """
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            await ctx.send(t("error.no_voice_connected"))
            return False

        # Sicherheitsnetz: falls FFmpeg noch nicht terminiert ist, kurz warten.
        if ctx.voice_client.is_playing():
            if not await self._wait_playback_done():
                await ctx.send(t("error.stream_busy"))
                self._stop_radio()
                return False
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
            return False
        # Uhr des Players erst beim ersten Paket: ein Sender braucht ~0,5 s bis
        # zum ersten Ton, die schöbe der Player sonst ohne Pause nach.
        source = start_clock_at_first_packet(source)

        token = PlayerToken()
        self._radio_session = token

        def after_radio(error):
            if token.discarded:
                # Pausierter Radio-Player wurde durch einen Song (play_next) oder
                # einen anderen Sender ersetzt (_discard_paused_player) – dessen
                # Zustand nicht anfassen.
                return
            self.bot.loop.call_soon_threadsafe(self._playback_done.set)
            if error:
                logger.warning(f"[Radio] Stream-Fehler: {error}")
            else:
                logger.info("[Radio] Stream unerwartet beendet (kein Fehler) – Reconnect wird versucht.")
            if not self.is_radio or self._radio_session is not token:
                # Radio beendet oder dieser Sender abgelöst (Senderwechsel): der
                # Stopp war gewollt, kein Reconnect.
                return
            if self._radio_should_reconnect(time.monotonic() - started):
                logger.info(f"[Radio] Reconnect {self._radio_reconnect_count}/{RADIO_MAX_RECONNECTS} für {name}")
                asyncio.run_coroutine_threadsafe(self._reconnect_radio(ctx, token), self.bot.loop)
            else:
                logger.warning(f"[Radio] Max. Reconnect-Versuche für {name} erreicht.")
                self.is_radio = False
                self.is_playing = False
                asyncio.run_coroutine_threadsafe(
                    ctx.send(t("radio.stream_interrupted"), delete_after=30),
                    self.bot.loop,
                )

        # Vorherige Karte (Song oder alter Sender) auf Text zurückbauen – nur die
        # laufende Quelle behält ihre Karte. Nie leer editieren: das lehnt Discord
        # ab und die alte Karte bliebe samt Buttons stehen.
        await self._retire_np_message(self.now_playing_msg, self._np_title)

        self._playback_done.clear()
        self.track_start_time = started = time.monotonic()
        # Pausierter Player (!x, dann !radio – Song oder anderer Sender):
        # discord.py würde ihn in play() nur ersetzen, nicht stoppen. Synchron,
        # direkt vor play() – wie in play_next.
        if ctx.voice_client.is_paused():
            self._discard_paused_player(ctx.voice_client)
        ctx.voice_client.play(source, after=after_radio)
        self._player_token = token

        view = MusicControlView(
            self, ctx,
            heading=f"### 📻 {discord.utils.escape_markdown(name)}",
            details=f"**{t('embed.status')}** {t('embed.radio_live')} · **{t('embed.eq')}** {self.equalizer}",
            up_next=self._up_next_title(),
            accent=0xe74c3c,
        )
        self.now_playing_msg = await ctx.send(view=view)
        self._np_title = name        # Sendername für den späteren Rückbau der Karte
        self.text_channel = ctx.channel
        logger.info(f"[Radio] Stream gestartet: {name} ({url})")
        return True

    async def _reconnect_radio(self, ctx, session=None) -> None:
        """Versucht einen abgebrochenen Radio-Stream neu zu verbinden.

        session: PlayerToken des abgebrochenen Senders. Hat in der Wartezeit
        jemand den Sender gewechselt oder das Radio beendet, entfällt der
        Reconnect – er würde sonst über das starten, was inzwischen läuft."""
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            logger.warning("[Radio] Kein Voice-Client mehr – Reconnect abgebrochen.")
            self._stop_radio()
            return
        await asyncio.sleep(2)
        if session is not None and self._radio_session is not session:
            logger.info("[Radio] Reconnect entfällt – der Sender wurde inzwischen gewechselt oder beendet.")
            return
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
        stations = _load_stations()

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
            if not await check_admin(ctx):
                return
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
                await asyncio.to_thread(_write_stations, stations)
                await ctx.send(t("radio.station_deleted", name=entry["name"]))
            else:  # rename
                if len(tokens) < 3:
                    await ctx.send(t("error.radio_rename_usage"))
                    return
                old_name = entry["name"]
                stations[key]["name"] = tokens[2]
                await asyncio.to_thread(_write_stations, stations)
                await ctx.send(t("radio.station_renamed", old=old_name, new=tokens[2]))
            return

        if not await self._ensure_voice(ctx):
            return

        new_station = None  # (key, entry) – wird erst nach erfolgreichem Stream-Start persistiert
        if eingabe.startswith("http"):
            parts = eingabe.split(None, 1)
            url = parts[0]
            if len(parts) > 1:
                name = parts[1].strip()
            else:
                from urllib.parse import urlparse
                name = urlparse(url).hostname or url

            if not await enforce_url_policy(ctx, url):
                return

            # Sender vormerken falls noch nicht vorhanden – gespeichert wird
            # erst NACH erfolgreichem Stream-Start, damit kaputte oder
            # abgelehnte URLs nicht in radio_stations.json landen.
            existing = next((v for v in stations.values() if v["url"] == url), None)
            if existing is None:
                key = name.lower().replace(" ", "").replace("-", "")
                # Eindeutigen Key sicherstellen
                base_key, n = key, 2
                while key in stations:
                    key = f"{base_key}{n}"
                    n += 1
                new_station = (key, {"name": name, "url": url})
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

        was_radio = self.is_radio
        if self.is_radio:
            self._stop_radio()
        else:
            self.is_playing = False

        # Immer warten bis FFmpeg wirklich fertig ist – egal ob Radio oder Song lief.
        if ctx.voice_client and ctx.voice_client.is_playing():
            if was_radio:
                stop_playback(ctx.voice_client)
            else:
                # Musik weicht dem Radio → absichtlicher Stopp: kein Seek-Resume
                # einer noch wachsenden Datei in after_playing.
                self._stop_for_advance(ctx.voice_client)
            await self._wait_playback_done()
            self._playback_done.clear()

        self._radio_reconnect_count = 0
        started = await self._play_radio_stream(ctx, url, name)
        if started and new_station:
            key, entry = new_station
            stations[key] = entry
            await asyncio.to_thread(_write_stations, stations)
            await ctx.send(t("radio.station_saved", name=name, num=len(stations)))
