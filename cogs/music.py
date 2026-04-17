# Hier lebt die gesamte Musik-Logik: Queue, Downloads, Wiedergabe, EQ und Autoplay.
# Kurz gesagt: die wichtigste Datei im ganzen Bot. Treat her well.

import asyncio
import itertools
import json
import random
import re
import time
import urllib.parse
from collections import deque
from pathlib import Path

import aiohttp

import discord
import psutil
from utils.logger import logger
from discord.ext import commands
from cogs.downloader import Downloader, DOWNLOAD_DIR
from cogs.presets import EQ_PRESETS
from views.music_controls import MusicControlView, SearchAutoplayView

PLAYLISTS_DIR = Path("playlists")
PLAYLISTS_DIR.mkdir(parents=True, exist_ok=True)


class MusicCommands(commands.Cog):
    """Cog für alle Musikbefehle: Wiedergabe, Queue, EQ, Autoplay."""

    # Maximale Anzahl an Titeln die aus einer Playlist eingelesen werden.
    HARD_PLAYLIST_LIMIT = 150
    # Sekunden ohne User im Channel bevor der Bot den Channel verlässt.
    AUTO_LEAVE_SECONDS = 120

    def __init__(self, bot):
        self.bot = bot

        # deque statt list, weil popleft() in O(1) läuft – bei langen Queues
        # ist das deutlich schneller als list.pop(0).
        self.queue = deque()

        self.is_playing = False
        self.current_track = None   # Aktuell spielender Song (url, title) – für !now
        self.last_played = None     # Wird von !replay genutzt
        self.prefetch_task = None         # Läuft im Hintergrund während ein Song spielt
        self._autoplay_prefetch_task = None  # Sucht+lädt nächsten Autoplay-Song vor
        self._autoplay_queued_url = None     # URL die zuletzt von Autoplay in die Queue gelegt wurde
        self._recently_played: deque = deque(maxlen=10)  # URLs der zuletzt gespielten Songs
        self.auto_leave_task = None # Timer: verlässt Channel wenn alle User weg sind
        self.text_channel = None    # Letzter Textkanal – für Auto-Leave-Nachricht
        self.now_playing_msg = None # Aktuelle "Jetzt läuft"-Nachricht – für Button-Cleanup
        self.track_start_time = None  # Zeitstempel kurz vor play() – FFmpeg-Crash-Erkennung

        # Standard-EQ und -Format beim Start
        self.equalizer = "punchy"
        self.audio_format = "webm"

        # Loop-Modi: None = aus, "song" = aktuellen Song wiederholen, "queue" = ganze Queue loopen
        self.loop_mode = None

        # Zählt gespielte Songs – alle 50 Songs werden yt_dlp-Instanzen neu erstellt,
        # damit deren interne Caches (Signatur-Parser, Format-Metadaten) nicht unbegrenzt wachsen.
        self._songs_played = 0

        self.eq_presets = EQ_PRESETS

        # Autoplay ist standardmäßig aus – niemand will, dass der Bot
        # nach Mitternacht eigenständig Jazz spielt.
        self.autoplay_enabled = False

        self._start_time = time.monotonic()
        self._process = psutil.Process()

        # Downloader hält alle yt_dlp-Instanzen und den Metadaten-Cache.
        self.dl = Downloader(self.audio_format)
        logger.info("[INIT] MusicCommands erfolgreich initialisiert.")

    def update_ydl(self):
        """Baut yt_dlp-Instanzen neu auf. Cache-Einträge für Queue-Songs bleiben erhalten."""
        keep = {url for url, _ in self.queue}
        if self.current_track:
            keep.add(self.current_track[0])
        self.dl.rebuild(self.audio_format, keep_urls=keep)

    @commands.command(name="reloadcookies")
    async def reloadcookies(self, ctx):
        """Lädt die cookies.txt neu ohne Bot-Neustart (nach manuellem Upload auf den Server)."""
        self.update_ydl()
        from config import YDL_COOKIES_FILE, YDL_BROWSER
        if YDL_COOKIES_FILE:
            source = f"`{YDL_COOKIES_FILE}`"
        elif YDL_BROWSER:
            source = f"Browser ({YDL_BROWSER})"
        else:
            source = "keine Cookie-Quelle konfiguriert"
        await ctx.send(f"Cookies neu geladen. Quelle: {source}")

    @commands.command()
    async def format(self, ctx, typ: str):
        """Wechselt das Audioformat (mp3 oder webm). Wirkt ab dem nächsten Track."""
        if typ.lower() in ["mp3", "webm"]:
            self.audio_format = typ.lower()
            self.update_ydl()
            # Kleiner Hinweis, falls gerade etwas läuft – der aktuelle Track
            # wird nicht neu gestartet, das wäre nervig.
            note = " (wirkt ab dem nächsten Titel)" if self.is_playing else ""
            await ctx.send(f"🔄 Audioformat auf **{self.audio_format}** gesetzt{note}.")
        else:
            await ctx.send("❌ Ungültiges Format. Verfügbare Optionen: mp3, webm")

    async def autoplay(self, ctx):
        """Sucht einen zum letzten Song passenden Track und spielt ihn einmalig.

        Nutzt den Titel des zuletzt gespielten Songs als Suchbasis, damit das
        Ergebnis thematisch passt. Autoplay deaktiviert sich danach selbst –
        für dauerhafte Wiederholung gibt es !loop.
        """
        # Referenz-Track: aktueller Song wenn vorhanden, sonst letzter gespielter
        ref_url = None
        ref_title = None
        if self.current_track:
            ref_url, ref_title, *_ = self.current_track
        elif self.last_played:
            ref_url, ref_title, *_ = self.last_played

        # YouTube Mix/Radio-URL: gibt echte Empfehlungen basierend auf dem Video
        yt_id = None
        if ref_url:
            m = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", ref_url)
            if m:
                yt_id = m.group(1)

        if yt_id:
            # YouTube-eigene Empfehlungen via RD-Mix-Playlist
            fetch_url = f"https://www.youtube.com/watch?v={yt_id}&list=RD{yt_id}"
            logger.info(f"[Autoplay] Lade YouTube-Mix für Video {yt_id} (Basis: {ref_title!r})")
        else:
            # Fallback für Nicht-YouTube-Quellen: titelbasierte Suche
            fetch_url = f"ytsearch5:{ref_title}" if ref_title else "ytsearch5:top music"
            logger.info(f"[Autoplay] Kein YT-Video-ID – Suche per Query: {fetch_url!r}")

        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(self.dl.autoplay_ydl.extract_info, fetch_url, download=False),
                timeout=30.0,
            )
            entries = (info.get("entries") or []) if info else []

            def entry_url(e):
                """Gibt die beste verfügbare URL eines Eintrags zurück (webpage_url > url)."""
                return e.get("webpage_url") or e.get("url") or ""

            def is_video(e):
                """Nur echte Videos – keine Playlists, keine leeren URLs."""
                if not e:
                    return False
                if e.get("_type") == "playlist":
                    return False
                u = entry_url(e)
                return bool(u) and "playlist?" not in u and "/playlist/" not in u

            # Originaltitel ausschließen, danach erstes passendes Video nehmen
            candidates = [e for e in entries if is_video(e) and entry_url(e) not in self._recently_played]
            if not candidates:
                candidates = [e for e in entries if is_video(e) and entry_url(e) != ref_url]
            if not candidates:
                candidates = [e for e in entries if is_video(e)]

            if not candidates:
                await ctx.send("❌ Autoplay: Keine verwandten Songs gefunden.")
                logger.warning("[Autoplay] Keine nutzbaren Einträge im Mix")
                return

            # Erstes Element aus dem Mix nehmen (YouTube sortiert nach Relevanz)
            chosen = candidates[0]
            url = entry_url(chosen)
            title = chosen.get("title", "Unbekannt")

            self.queue.appendleft((url, title))
            self._autoplay_queued_url = url
            logger.info(f"[Autoplay] Hinzugefügt: {title} ({url})")
            await ctx.send(f"🔁 Autoplay: **{title}**")
            if not self.is_playing:
                self.is_playing = True
                await self.play_next(ctx)
        except asyncio.TimeoutError:
            await ctx.send("❌ Autoplay-Suche hat zu lange gedauert.")
            logger.warning("[Autoplay] Timeout bei extract_info")
        except Exception:
            await ctx.send("❌ Fehler bei Autoplay.")
            logger.exception("[Autoplay Fehler]")

    async def _prefetch_next(self, idx: int = 0):
        """Lädt Song an Position idx der Queue still im Hintergrund herunter."""
        await self.dl.prefetch_next(self.queue, idx)

    async def _prefetch_autoplay(self, ctx):
        """Sucht und lädt nächsten Autoplay-Song im Hintergrund während der aktuelle läuft."""
        ref_url = ref_title = None
        if self.current_track:
            ref_url, ref_title, *_ = self.current_track
        result = await self.dl.prefetch_autoplay(ref_url, ref_title, self._recently_played)
        if result and self.autoplay_enabled:
            url, title = result
            self.queue.append((url, title))
            self._autoplay_queued_url = url
            logger.info(f"[Autoplay Prefetch] In Queue eingereiht: {title}")

    async def _auto_leave(self, voice_client):
        """Verlässt den Channel nach AUTO_LEAVE_SECONDS wenn kein User mehr drin ist."""
        await asyncio.sleep(self.AUTO_LEAVE_SECONDS)
        if voice_client.is_connected():
            self.queue.clear()
            self.is_playing = False
            self.current_track = None
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
            await voice_client.disconnect()
            if self.text_channel:
                await self.text_channel.send("👋 Alle weg – ich geh dann auch. Bis zum nächsten Mal!")
            logger.info("[Auto-Leave] Channel verlassen – keine User mehr.")

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
            logger.info("[Auto-Leave] Channel leer – starte 2-Minuten-Timer.")
        else:
            # Jemand ist (wieder) da – Timer abbrechen
            if self.auto_leave_task and not self.auto_leave_task.done():
                self.auto_leave_task.cancel()
                self.auto_leave_task = None

    async def _resolve_track(self, url: str, title: str):
        """Löst URL auf, stellt sicher dass Audiodatei lokal vorliegt.

        Returns: (info, filename, title, duration)
        """
        return await self.dl.resolve_track(url, title, self.prefetch_task)

    async def play_next(self, ctx):
        """Spielt den nächsten Song in der Queue. Wird rekursiv nach jedem Track aufgerufen."""
        if not self.queue:
            logger.info("[Queue] Leere Warteschlange. Wiedergabe gestoppt.")
            self.is_playing = False
            # current_track als last_played sichern bevor es gecleant wird,
            # damit autoplay() noch weiß was zuletzt lief.
            if self.current_track:
                self.last_played = self.current_track
            self.current_track = None
            # Autoplay rettet die Stille – aber nur wenn gewünscht.
            if self.autoplay_enabled:
                # Wenn der Prefetch-Task noch läuft, kurz warten – er hat den Song
                # bereits heruntergeladen und hängt ihn gleich in die Queue.
                if self._autoplay_prefetch_task and not self._autoplay_prefetch_task.done():
                    logger.info("[Autoplay] Warte auf laufenden Prefetch-Task...")
                    try:
                        await asyncio.wait_for(asyncio.shield(self._autoplay_prefetch_task), timeout=60.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        logger.warning("[Autoplay] Prefetch-Task nicht rechtzeitig fertig – Fallback")
                # Queue prüfen: Prefetch hat ggf. schon etwas eingereiht
                if self.queue:
                    self.is_playing = True
                    await self.play_next(ctx)
                else:
                    await self.autoplay(ctx)
            return

        # Guard: verhindert die "Already playing audio"-Kaskade bei gleichzeitigen
        # play_next-Aufrufen (z.B. after_playing-Callback + Error-Handler-create_task).
        # Song NICHT aus der Queue nehmen – erst nach dem Check.
        # Kurzes Warten: nach einem Reconnect kann FFmpeg noch im Cleanup sein (exit-code 1)
        # obwohl after_playing bereits ausgelöst wurde – is_playing() wäre dann fälschlicherweise True.
        if ctx.voice_client and ctx.voice_client.is_playing():
            for _ in range(10):
                await asyncio.sleep(0.1)
                if not ctx.voice_client or not ctx.voice_client.is_playing():
                    break
            else:
                logger.warning("[play_next] Voice client bereits am spielen – konkurrenten Aufruf ignoriert.")
                return

        url, title = self.queue.popleft()
        if url == self._autoplay_queued_url:
            self._autoplay_queued_url = None
        logger.info(f"[Nächster Track] {title} ({url})")

        try:
            info, filename, title, duration = await self._resolve_track(url, title)

            eq_filter = self.eq_presets.get(self.equalizer, "")

            if eq_filter:
                # Filter aktiv → dekodieren, EQ anwenden, mit 192kbps zu Opus enkodieren.
                # -vn unterdrückt den Video-Stream.
                source = discord.FFmpegOpusAudio(
                    str(filename),
                    bitrate=192,
                    options=f"-vn {eq_filter}",
                )
            else:
                # Kein Filter (flat) → Opus-Stream 1:1 durchreichen, kein Qualitätsverlust.
                source = discord.FFmpegOpusAudio(str(filename), codec="copy")

            def after_playing(error):
                """Callback, der nach jedem Track von FFmpeg aufgerufen wird.

                Läuft in einem separaten Thread – daher run_coroutine_threadsafe
                statt await. Direkt awaiten würde hier crashen.
                """
                if error:
                    logger.warning(f"[Fehler beim Abspielen] {error}")
                elapsed = time.monotonic() - self.track_start_time if self.track_start_time else 999
                if elapsed < 2.0:
                    logger.warning(
                        f"[FFmpeg] Track lief nur {elapsed:.2f}s – wahrscheinlich ungültiger Filter. "
                        f"Aktives Preset: '{self.equalizer}', Filter: {self.eq_presets.get(self.equalizer, '(keiner)')}"
                    )

                # Queue-Stand nach jedem Track in Datei sichern – nicht als
                # Restore-Point gedacht, nur als Protokoll der letzten Session.
                queue_data = list(self.queue)
                try:
                    with open("last_queue.json", "w", encoding="utf-8") as f:
                        json.dump(queue_data, f, ensure_ascii=False, indent=2)
                    logger.info("[SAVE] Queue automatisch gespeichert nach Track-Ende.")
                except Exception as e:
                    logger.warning(f"[SAVE] Fehler beim Speichern der Queue: {e}")

                # Loop-Logik: Song zurück in die Queue legen bevor play_next aufgerufen wird.
                # Queue speichert 2-Tuples (url, title), current_track ist ein 3-Tuple.
                if self.loop_mode == "song" and self.current_track:
                    ct_url, ct_title, *_ = self.current_track
                    self.queue.appendleft((ct_url, ct_title))
                elif self.loop_mode == "queue" and self.current_track:
                    ct_url, ct_title, *_ = self.current_track
                    self.queue.append((ct_url, ct_title))

                if ctx.voice_client:
                    # Immer play_next aufrufen – die Queue-leer+Autoplay-Logik liegt dort.
                    fut = asyncio.run_coroutine_threadsafe(
                        self.play_next(ctx), self.bot.loop
                    )

                    def log_exception(f):
                        # Stille Fehler sind gefährlich – lieber laut loggen.
                        if not f.cancelled() and f.exception():
                            logger.error(
                                f"[after_playing] Fehler beim Starten des nächsten Tracks: {f.exception()}"
                            )

                    fut.add_done_callback(log_exception)
                else:
                    logger.info("[KEINE VERBINDUNG] Keine Verbindung. Warte auf !j ...")

            if not ctx.voice_client or not ctx.voice_client.is_connected():
                logger.warning("[play_next] Voice client nicht verbunden – Wiedergabe abgebrochen.")
                self.is_playing = False
                self.queue.appendleft((url, title))
                return

            self.track_start_time = time.monotonic()
            ctx.voice_client.play(source, after=after_playing)
            logger.info(f"[Wiedergabe] Starte: {title}")
            if self.now_playing_msg:
                try:
                    await self.now_playing_msg.delete()
                except Exception:
                    pass
            duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else "Unbekannt"
            embed = discord.Embed(title=title, url=info.get("webpage_url"), color=0x1db954)
            embed.set_thumbnail(url=info.get("thumbnail"))
            embed.add_field(name="Dauer", value=duration_str, inline=True)
            embed.add_field(name="EQ", value=self.equalizer, inline=True)
            embed.add_field(name="Format", value=self.audio_format, inline=True)
            if info.get("uploader"):
                embed.set_footer(text=info.get("uploader"))
            self.now_playing_msg = await ctx.send(embed=embed, view=MusicControlView(self, ctx))
            self.is_playing = True
            self.last_played = self.current_track  # vorherigen Song merken, bevor er überschrieben wird
            self.current_track = (url, title, duration)
            self._recently_played.append(url)
            self.text_channel = ctx.channel

            # Alle 50 Songs yt_dlp-Instanzen neu erstellen, damit interne Caches
            # (JS-Signatur-Parser, Format-Metadaten, HTTP-Pool) nicht unbegrenzt wachsen.
            self._songs_played += 1
            if self._songs_played % 50 == 0:
                logger.info(f"[Maintenance] {self._songs_played} Songs gespielt – yt_dlp-Instanzen werden neu erstellt.")
                self.update_ydl()

            # Nächsten Song direkt im Hintergrund vorladen, damit er ohne Wartezeit startet.
            # Alten Prefetch-Task erst abbrechen – sonst laufen mehrere parallel.
            if self.prefetch_task and not self.prefetch_task.done():
                self.prefetch_task.cancel()
            if self.queue:
                # Bis zu 2 Songs sequenziell vorladen – yt_dlp ist nicht thread-safe,
                # daher kein paralleles gather. Sequenziell reicht: während Song N spielt,
                # werden N+1 und N+2 nacheinander heruntergeladen.
                async def _prefetch_two():
                    await self._prefetch_next(0)
                    if len(self.queue) >= 2:
                        await self._prefetch_next(1)
                self.prefetch_task = asyncio.create_task(_prefetch_two())

            # Autoplay: nächsten verwandten Song suchen+laden während der aktuelle läuft.
            if self.autoplay_enabled and not self.queue:
                if self._autoplay_prefetch_task and not self._autoplay_prefetch_task.done():
                    self._autoplay_prefetch_task.cancel()
                self._autoplay_prefetch_task = asyncio.create_task(self._prefetch_autoplay(ctx))

        except asyncio.TimeoutError:
            self._recently_played.append(url)
            if ctx.voice_client and ctx.voice_client.is_connected():
                try:
                    await ctx.send(f"⚠️ Timeout beim Laden von **{title}**. Überspringe...")
                except Exception:
                    pass
                asyncio.create_task(self.play_next(ctx))
            else:
                logger.warning("[play_next] TimeoutError – Voice client weg, kein Retry.")
                self.is_playing = False
            return
        except Exception:
            logger.exception("[Fehler bei play_next]")
            self._recently_played.append(url)
            if ctx.voice_client and ctx.voice_client.is_connected():
                try:
                    await ctx.send(f"⚠️ Fehler beim Laden von **{title}**. Überspringe...")
                except Exception:
                    pass
                # Fehlerhaften Track überspringen – create_task statt direkter Rekursion,
                # damit bei vielen schlechten URLs der Call-Stack nicht überfüllt wird.
                asyncio.create_task(self.play_next(ctx))
            else:
                logger.warning("[play_next] Exception – Voice client weg, kein Retry.")
                self.is_playing = False
            return

    async def _ensure_voice(self, ctx) -> bool:
        """Stellt sicher, dass der Bot im Voice-Channel des Users ist.

        Verbindet automatisch wenn nötig. Gibt True zurück wenn verbunden,
        False wenn der User selbst in keinem Channel ist (mit Fehlermeldung).
        """
        if ctx.voice_client is not None:
            return True
        if not ctx.author.voice:
            await ctx.send("❌ Du bist in keinem Voice-Channel.")
            return False
        try:
            await ctx.author.voice.channel.connect()
            logger.info(f"[Auto-Join] Verbunden mit: {ctx.author.voice.channel.name}")
        except Exception as e:
            await ctx.send(f"❌ Verbindung fehlgeschlagen: {type(e).__name__}: {str(e)[:100]}")
            return False
        return True

    def _evict_autoplay_song(self) -> str | None:
        """Entfernt den von Autoplay vorgereihten Song aus der Queue (falls vorhanden).

        Bricht außerdem einen laufenden Prefetch-Task ab, damit kein weiterer
        Autoplay-Song nachgeschoben wird bevor der manuelle !p-Song gespielt hat.
        Gibt den Titel des entfernten Songs zurück (für Logging), sonst None.
        """
        # Laufenden Prefetch sofort stoppen
        if self._autoplay_prefetch_task and not self._autoplay_prefetch_task.done():
            self._autoplay_prefetch_task.cancel()
            self._autoplay_prefetch_task = None

        if not self._autoplay_queued_url:
            return None

        queue_list = list(self.queue)
        removed_title = None
        new_queue = []
        for u, t in queue_list:
            if u == self._autoplay_queued_url and removed_title is None:
                removed_title = t  # nur erstes Vorkommen entfernen
            else:
                new_queue.append((u, t))
        if removed_title is not None:
            self.queue = deque(new_queue)
            self._autoplay_queued_url = None
        return removed_title

    @commands.command()
    async def p(self, ctx, *, eingabe):
        """Spielt eine URL, Playlist oder Suchbegriff. Bei Suche werden 3 Treffer zur Auswahl angezeigt."""
        logger.info(f"[p] Eingabe erhalten: {eingabe}")

        if not await self._ensure_voice(ctx):
            return

        # Wenn kein http am Anfang → Suchbegriff → ersten Treffer sofort abspielen,
        # Treffer 2 und 3 als Buttons anzeigen falls es der Falsche war.
        if not eingabe.startswith("http"):
            try:
                await ctx.send("🔎 Suche läuft...")
                results = await asyncio.wait_for(
                    asyncio.to_thread(self.dl.search_ydl.extract_info, f"ytsearch3:{eingabe}", download=False),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                await ctx.send("❌ Suche hat zu lange gedauert. Bitte versuche es erneut.")
                return
            except Exception:
                logger.exception("[p] Fehler bei Suche")
                await ctx.send("❌ Fehler bei der Suche.")
                return

            entries = (results.get("entries") or [])[:3]
            if not entries:
                await ctx.send("❌ Keine Ergebnisse gefunden.")
                return

            # Ersten Treffer direkt in die Queue legen.
            # Wenn Autoplay einen Song vorgemerkt hat, fliegt der raus – der manuelle
            # Wunsch hat Vorrang und soll als nächstes spielen.
            first = entries[0]
            url = first.get("webpage_url") or first.get("url")
            title = first.get("title", "Unbekannter Titel")
            evicted = self._evict_autoplay_song() if self.autoplay_enabled else None
            if evicted:
                logger.info(f"[p] Autoplay-Song verdrängt: {evicted}")
                self.queue.appendleft((url, title))
            else:
                self.queue.append((url, title))

            # Alternativen (Treffer 2 und 3) als Buttons anzeigen
            alternatives = entries[1:]
            if alternatives:
                view = SearchAutoplayView(first, alternatives, self, ctx)
                letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                alt_lines = "\n".join(
                    f"**Option {letters[i]}:** {e.get('title', 'Unbekannter Titel')}"
                    for i, e in enumerate(alternatives)
                )
                msg = await ctx.send(
                    f"🎶 Spiele: **{title}**\n*Nicht das Richtige? Wähle eine Alternative:*\n{alt_lines}",
                    view=view,
                )
                view.message = msg
            else:
                await ctx.send(f"🎶 Hinzugefügt: **{title}**")

            if not self.is_playing:
                self.is_playing = True
                await self.play_next(ctx)
            return

        # --- Ab hier: direkte URL oder Playlist ---

        # Einfache Heuristik: Wenn "playlist?" oder "list=" in der URL steht,
        # ist es eine Playlist. Funktioniert für alle gängigen YouTube-Playlist-URLs.
        is_playlist = "playlist?" in eingabe or "list=" in eingabe
        ydl_instance = self.dl.playlist_ydl if is_playlist else self.dl.url_ydl

        try:
            await ctx.send("🔎 Verarbeite Eingabe... bitte warten.")
            info = await asyncio.wait_for(
                asyncio.to_thread(ydl_instance.extract_info, eingabe, download=False),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            await ctx.send("❌ Anfrage hat zu lange gedauert. Bitte überprüfe die URL und versuche es erneut.")
            return
        except Exception:
            logger.exception("[p] Fehler beim Abrufen von yt_dlp-Infos")
            await ctx.send("❌ Fehler beim Abrufen der Informationen. Bitte überprüfe die URL.")
            return

        if "entries" in info:
            entries = info["entries"]
            await ctx.send(
                f"📃 Playlist erkannt: **{info.get('title', 'Unbenannte Playlist')}** – {len(entries)} Einträge gefunden. Füge zur Queue hinzu..."
            )
            added_count = 0
            for entry in entries:
                if added_count >= self.HARD_PLAYLIST_LIMIT:
                    await ctx.send(f"⚠️ Limit von {self.HARD_PLAYLIST_LIMIT} Titeln erreicht.")
                    break
                # webpage_url ist immer die echte YouTube-URL.
                # url kann bei Suchergebnissen eine direkte Stream-URL sein → zuletzt prüfen.
                url = entry.get("webpage_url") or entry.get("url")
                title = entry.get("title", "Unbekannter Titel")
                if url:
                    self.queue.append((url, title))
                    added_count += 1
            await ctx.send(f"✅ {added_count} Titel zur Warteschlange hinzugefügt.")
        else:
            url = info.get("webpage_url")
            title = info.get("title", "Unbekannter Titel")
            # Warnen wenn der Titel schon in der Queue ist – könnte ein Versehen sein
            dup_pos = next((i + 1 for i, (_, t) in enumerate(self.queue) if t == title), None)
            if dup_pos:
                await ctx.send(f"⚠️ **{title}** ist bereits in der Queue (Position {dup_pos}). Trotzdem hinzugefügt.")
            evicted = self._evict_autoplay_song() if self.autoplay_enabled else None
            if evicted:
                logger.info(f"[p] Autoplay-Song verdrängt: {evicted}")
                self.queue.appendleft((url, title))
            else:
                self.queue.append((url, title))
            await ctx.send(f"🎶 Hinzugefügt: **{title}**")

        if not self.is_playing:
            self.is_playing = True
            await self.play_next(ctx)

    @commands.command(name="next")
    async def next_song(self, ctx, *, eingabe):
        """Fügt einen Song an die erste Stelle der Queue ein (spielt als nächstes).
        Format: !next URL  oder  !next URL||Titel  oder  !next Suchbegriff"""
        if not await self._ensure_voice(ctx):
            return

        # Format: URL||Titel → direkt ohne yt_dlp-Lookup hinzufügen
        if "||" in eingabe:
            parts = eingabe.split("||", 1)
            url = parts[0].strip()
            title = parts[1].strip()
            self.queue.appendleft((url, title))
            await ctx.send(f"⏭️ Als nächstes: **{title}**")
            if not self.is_playing:
                self.is_playing = True
                await self.play_next(ctx)
            return

        # Suchbegriff → ersten Treffer an erste Stelle, Alternativen als Buttons
        if not eingabe.startswith("http"):
            try:
                await ctx.send("🔎 Suche läuft...")
                results = await asyncio.wait_for(
                    asyncio.to_thread(self.dl.search_ydl.extract_info, f"ytsearch3:{eingabe}", download=False),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                await ctx.send("❌ Suche hat zu lange gedauert.")
                return
            except Exception:
                logger.exception("[next] Fehler bei Suche")
                await ctx.send("❌ Fehler bei der Suche.")
                return
            entries = (results.get("entries") or [])[:3]
            if not entries:
                await ctx.send("❌ Keine Ergebnisse gefunden.")
                return
            first = entries[0]
            url = first.get("webpage_url") or first.get("url")
            title = first.get("title", "Unbekannter Titel")
            self.queue.appendleft((url, title))
            alternatives = entries[1:]
            if alternatives:
                view = SearchAutoplayView(
                    first, alternatives, self, ctx,
                    base_content=f"⏭️ Als nächstes: **{title}**",
                )
                letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                alt_lines = "\n".join(
                    f"**Option {letters[i]}:** {e.get('title', 'Unbekannter Titel')}"
                    for i, e in enumerate(alternatives)
                )
                msg = await ctx.send(
                    f"⏭️ Als nächstes: **{title}**\n*Nicht das Richtige? Wähle eine Alternative:*\n{alt_lines}",
                    view=view,
                )
                view.message = msg
            else:
                await ctx.send(f"⏭️ Als nächstes: **{title}**")
            if not self.is_playing:
                self.is_playing = True
                await self.play_next(ctx)
            return

        # Direkte URL → yt_dlp-Lookup
        try:
            await ctx.send("🔎 Verarbeite URL...")
            info = await asyncio.wait_for(
                asyncio.to_thread(self.dl.url_ydl.extract_info, eingabe, download=False),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            await ctx.send("❌ Anfrage hat zu lange gedauert.")
            return
        except Exception:
            logger.exception("[next] Fehler beim Abrufen von yt_dlp-Infos")
            await ctx.send("❌ Fehler beim Abrufen der Informationen.")
            return

        if "entries" in info:
            await ctx.send("⚠️ Playlists werden von `!next` nicht unterstützt. Bitte eine einzelne URL angeben.")
            return

        url = info.get("webpage_url") or eingabe
        title = info.get("title", "Unbekannter Titel")
        self.queue.appendleft((url, title))
        await ctx.send(f"⏭️ Als nächstes: **{title}**")
        if not self.is_playing:
            self.is_playing = True
            await self.play_next(ctx)

    @commands.command(name="s")
    async def skip(self, ctx):
        """Überspringt den aktuellen Track. after_playing kümmert sich um den Rest."""
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await ctx.send("⏭️ Song übersprungen. Spiele den nächsten Titel ...")
        else:
            await ctx.send("⚠️ Es läuft gerade kein Song.")

    @commands.command(name="x")
    async def pause(self, ctx):
        """Pausiert die Wiedergabe und aktualisiert is_playing."""
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("⏸️ Song pausiert.")
            # is_playing muss hier auf False, damit !resume und !p
            # erkennen, dass gerade nichts aktiv abgespielt wird.
            self.is_playing = False
        else:
            await ctx.send("⚠️ Es läuft gerade kein Song.")

    @commands.command()
    async def resume(self, ctx):
        """Setzt die Wiedergabe fort. Startet auch, wenn is_playing False aber Queue voll ist."""
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("▶️ Song fortgesetzt.")
            self.is_playing = True
        elif not self.is_playing and self.queue:
            # Edge-Case: Bot im Kanal, Queue nicht leer, aber nichts läuft.
            # Kann passieren wenn der Bot disconnected und reconnectet wurde.
            logger.info("[Auto-Start] Queue nicht leer, starte nächsten Song.")
            await self.play_next(ctx)
        else:
            await ctx.send("⚠️ Kein Song zum Fortsetzen.")

    @commands.command(name="now")
    async def now_playing(self, ctx):
        """Zeigt den aktuell laufenden oder pausierten Song an."""
        if self.current_track:
            ct_url, ct_title, ct_duration, *_ = (*self.current_track, 0)  # 0 als Fallback für alte 2-Tuples
            duration_str = f"{ct_duration // 60}:{ct_duration % 60:02d}" if ct_duration else "Unbekannt"
            color = 0x1db954 if self.is_playing else 0x808080
            embed = discord.Embed(title=ct_title, url=ct_url, color=color)
            embed.add_field(name="Dauer", value=duration_str, inline=True)
            embed.add_field(name="EQ", value=self.equalizer, inline=True)
            embed.add_field(name="Format", value=self.audio_format, inline=True)
            status = "🎶 Läuft gerade" if self.is_playing else "⏸️ Pausiert"
            embed.set_author(name=status)
            await ctx.send(embed=embed)
        else:
            await ctx.send("⚠️ Es läuft gerade kein Song.")

    @commands.command(name="text")
    async def lyrics_cmd(self, ctx):
        """Zeigt den Liedtext des aktuell laufenden Songs via lyrics.ovh an."""
        if not self.current_track:
            await ctx.send("⚠️ Es läuft gerade kein Song.")
            return

        title_raw = self.current_track[1]

        # YouTube-Titel folgen meist dem Muster "Artist - Title (Remix/Edit/...)"
        if " - " in title_raw:
            artist, song = title_raw.split(" - ", 1)
            song = re.sub(r'\s*\([^)]*\)', '', song).strip()
            artist = artist.strip()
        else:
            artist = ""
            song = re.sub(r'\s*\([^)]*\)', '', title_raw).strip()

        display = f"**{artist} – {song}**" if artist else f"**{song}**"
        await ctx.send(f"🔎 Suche Liedtext für {display}...")

        try:
            url = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(artist)}/{urllib.parse.quote(song)}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        await ctx.send("❌ Liedtext nicht gefunden.")
                        return
                    data = await resp.json(content_type=None)
                    lyrics = data.get("lyrics", "").strip()
        except asyncio.TimeoutError:
            await ctx.send("❌ Anfrage hat zu lange gedauert.")
            return
        except Exception:
            logger.exception("[text] Fehler beim Abrufen des Liedtexts")
            await ctx.send("❌ Fehler beim Abrufen des Liedtexts.")
            return

        if not lyrics:
            await ctx.send("❌ Liedtext nicht gefunden.")
            return

        # Discord-Limit: 2000 Zeichen pro Nachricht → bei Bedarf aufteilen
        chunks = [lyrics[i:i+1900] for i in range(0, len(lyrics), 1900)]
        for i, chunk in enumerate(chunks):
            header = f"{display}\n" if i == 0 else ""
            await ctx.send(f"{header}```\n{chunk}\n```")

    @commands.command(name="loop")
    async def loop(self, ctx):
        """Schaltet den Loop-Modus durch: aus → Song wiederholen → Queue loopen → aus."""
        modes = [None, "song", "queue"]
        self.loop_mode = modes[(modes.index(self.loop_mode) + 1) % len(modes)]
        messages = {
            None: "➡️ Loop **aus**.",
            "song": "🔂 Loop: **aktueller Song** wird wiederholt.",
            "queue": "🔁 Loop: **gesamte Queue** wird wiederholt.",
        }
        await ctx.send(messages[self.loop_mode])

    @commands.command(name="q")
    async def queue_list(self, ctx):
        """Zeigt die aktuelle Queue. Bricht bei ~2000 Zeichen ab und zeigt die Restanzahl."""
        if not self.queue:
            await ctx.send("🧫 Die Warteschlange ist leer.")
        else:
            message = "📜 Aktuelle Warteschlange:\n"
            shown = 0
            for i, (url, title) in enumerate(self.queue, 1):
                line = f"{i}. {title}\n"
                # Discord-Nachrichten dürfen max. 2000 Zeichen haben.
                if len(message) + len(line) >= 1950:
                    break
                message += line
                shown += 1
            remaining = len(self.queue) - shown
            if remaining > 0:
                message += f"... und {remaining} weitere Titel."
            await ctx.send(message)

    @commands.command()
    async def clear(self, ctx):
        """Leert die Queue, stoppt die Wiedergabe und setzt Loop zurück."""
        self.queue.clear()
        self.dl.clear_cache()
        self.is_playing = False
        self.current_track = None
        self.loop_mode = None
        if self.prefetch_task and not self.prefetch_task.done():
            self.prefetch_task.cancel()
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
        await ctx.send("🧹 Die Warteschlange wurde geleert und die Wiedergabe gestoppt.")

    @commands.command(name="saveq")
    async def saveq(self, ctx, *, name: str):
        """Speichert die aktuelle Queue unter einem Namen. Verwendung: !saveq <name>"""
        if not self.queue and not self.current_track:
            await ctx.send("⚠️ Nichts zu speichern – Queue und aktueller Track sind leer.")
            return
        safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
        if not safe_name:
            await ctx.send("❌ Ungültiger Name.")
            return
        tracks = []
        if self.current_track:
            ct_url, ct_title, *_ = self.current_track
            tracks.append([ct_url, ct_title])
        tracks.extend([url, title] for url, title in self.queue)
        path = PLAYLISTS_DIR / f"{safe_name}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tracks, f, ensure_ascii=False, indent=2)
        await ctx.send(f"💾 Queue als **{safe_name}** gespeichert ({len(tracks)} Titel).")

    @commands.command(name="loadq")
    async def loadq(self, ctx, *, name: str):
        """Lädt eine gespeicherte Queue und hängt sie an die aktuelle an. Verwendung: !loadq <name>"""
        safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
        path = PLAYLISTS_DIR / f"{safe_name}.json"
        if not path.exists():
            await ctx.send(f"❌ Keine gespeicherte Queue mit dem Namen **{safe_name}** gefunden.")
            return
        with open(path, encoding="utf-8") as f:
            tracks = json.load(f)
        for url, title in tracks:
            self.queue.append((url, title))
        await ctx.send(f"📂 **{safe_name}** geladen – {len(tracks)} Titel zur Queue hinzugefügt.")
        if not self.is_playing:
            self.is_playing = True
            await self.play_next(ctx)

    @commands.command(name="lists")
    async def lists(self, ctx):
        """Zeigt alle gespeicherten Queues."""
        files = sorted(PLAYLISTS_DIR.glob("*.json"))
        if not files:
            await ctx.send("📭 Keine gespeicherten Queues vorhanden.")
            return
        lines = []
        for p in files:
            try:
                with open(p, encoding="utf-8") as f:
                    count = len(json.load(f))
                lines.append(f"• **{p.stem}** ({count} Titel)")
            except Exception:
                lines.append(f"• **{p.stem}**")
        await ctx.send("📋 Gespeicherte Queues:\n" + "\n".join(lines))

    @commands.command()
    async def remove(self, ctx, index: int):
        """Entfernt einen Track an Position n aus der Queue."""
        # deque unterstützt kein pop(index) – kurzer Umweg über eine Liste.
        if 1 <= index <= len(self.queue):
            queue_list = list(self.queue)
            removed = queue_list.pop(index - 1)
            self.queue = deque(queue_list)
            await ctx.send(f"❌ Entfernt: **{removed[1]}**")
        else:
            await ctx.send("❌ Ungültiger Index. Bitte eine gültige Zahl angeben.")

    @commands.command()
    async def move(self, ctx, index: int):
        """Springt zu Position n in der Queue und spielt von dort weiter."""
        if not self.queue:
            await ctx.send("⚠️ Die Warteschlange ist leer.")
            return
        if not 1 <= index <= len(self.queue):
            await ctx.send(f"❌ Ungültiger Index. Bitte eine Zahl zwischen 1 und {len(self.queue)} angeben.")
            return
        queue_list = list(self.queue)
        self.queue = deque(queue_list[index - 1:])
        if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            ctx.voice_client.stop()
        else:
            self.is_playing = True
            await self.play_next(ctx)

    @commands.command()
    async def shuffle(self, ctx):
        """Mischt die Queue zufällig durch."""
        # random.shuffle() arbeitet auf Listen, nicht auf deques – also kurz umwandeln.
        if len(self.queue) < 2:
            await ctx.send("⚠️ Nicht genug Songs zum Mischen.")
        else:
            queue_list = list(self.queue)
            random.shuffle(queue_list)
            self.queue = deque(queue_list)
            await ctx.send("🔀 Warteschlange wurde gemischt.")

    @commands.command()
    async def replay(self, ctx):
        """Stellt den zuletzt gespielten Song an den Anfang der Queue."""
        if self.last_played:
            lp_url, lp_title, *_ = self.last_played
            self.queue.appendleft((lp_url, lp_title))
            await ctx.send(f"🔁 Wiederhole: **{lp_title}**")
            if not self.is_playing:
                self.is_playing = True
                await self.play_next(ctx)
        else:
            await ctx.send("❌ Kein letzter Song vorhanden.")

    @commands.command()
    async def eq(self, ctx, preset: str = None):
        """Setzt einen EQ-Preset oder listet verfügbare Presets auf."""
        if not preset:
            presets = ", ".join(self.eq_presets.keys())
            await ctx.send(f"🎚️ Verfügbare Presets: {presets}")
            return
        if preset.lower() in self.eq_presets:
            self.equalizer = preset.lower()
            msg = f"🎚️ Equalizer auf `{preset}` gesetzt."
            if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()) and self.current_track:
                tr_url, tr_title, *_ = self.current_track
                self.current_track = None  # verhindert Doppel-Insert durch loop-Branch in after_playing
                self.queue.appendleft((tr_url, tr_title))
                ctx.voice_client.stop()
                msg += " – Song wird mit neuem EQ neu gestartet."
            await ctx.send(msg)
        else:
            presets = ", ".join(self.eq_presets.keys())
            await ctx.send(f"❌ Unbekanntes Profil. Verfügbare Presets: {presets}")

    @commands.command(name="stats")
    async def stats(self, ctx):
        """Zeigt Live-Metriken zum Bot-Prozess: RAM, CPU, Uptime, Queue, Cache."""
        proc = self._process

        # RAM
        mem = proc.memory_info()
        rss_mb = mem.rss / 1024 / 1024
        vms_mb = mem.vms / 1024 / 1024

        # CPU (non-blocking: 0.1s Interval im Thread)
        cpu_pct = await asyncio.to_thread(proc.cpu_percent, 0.1)

        # Uptime
        uptime_s = int(time.monotonic() - self._start_time)
        h, rem = divmod(uptime_s, 3600)
        m, s = divmod(rem, 60)
        uptime_str = f"{h}h {m}m {s}s"

        # asyncio Tasks
        all_tasks = asyncio.all_tasks()
        running_tasks = sum(1 for t in all_tasks if not t.done())

        # Downloads-Ordner – auf 500 Dateien begrenzen damit stat() nicht ewig läuft
        _SCAN_LIMIT = 500
        dl_files = list(itertools.islice(DOWNLOAD_DIR.glob("*"), _SCAN_LIMIT + 1))
        capped = len(dl_files) > _SCAN_LIMIT
        if capped:
            dl_files = dl_files[:_SCAN_LIMIT]
        dl_count = f"{len(dl_files)}+" if capped else str(len(dl_files))
        dl_size_mb = await asyncio.to_thread(
            lambda: sum(f.stat().st_size for f in dl_files if f.is_file()) / 1024 / 1024
        )

        # Queue
        queue_len = len(self.queue)

        embed = discord.Embed(title="📊 Bot Stats", color=0x5865f2)
        embed.add_field(name="⏱️ Uptime", value=uptime_str, inline=True)
        embed.add_field(name="🎵 Songs gespielt", value=str(self._songs_played), inline=True)
        embed.add_field(name="📋 Queue", value=f"{queue_len} Songs", inline=True)
        embed.add_field(name="🧠 RAM (RSS)", value=f"{rss_mb:.1f} MB", inline=True)
        embed.add_field(name="🧠 RAM (VMS)", value=f"{vms_mb:.1f} MB", inline=True)
        embed.add_field(name="⚡ CPU", value=f"{cpu_pct:.1f}%", inline=True)
        embed.add_field(name="⚙️ asyncio Tasks", value=str(running_tasks), inline=True)
        embed.add_field(name="💾 Cache-Dateien", value=f"{dl_count} Dateien", inline=True)
        embed.add_field(name="💾 Cache-Größe", value=f"{dl_size_mb:.0f} MB", inline=True)
        await ctx.send(embed=embed)

    @commands.command(name="baba")
    async def baba(self, ctx):
        """Spielt Babas Playlist ab. Kein Argument nötig – einfach !baba und los."""
        await ctx.invoke(self.p, eingabe="https://www.youtube.com/playlist?list=PLhqD5zya16QavuozTOLCZ3Jn6gQu66Tvj")

