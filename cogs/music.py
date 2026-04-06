# Hier lebt die gesamte Musik-Logik: Queue, Downloads, Wiedergabe, EQ und Autoplay.
# Kurz gesagt: die wichtigste Datei im ganzen Bot. Treat her well.

import asyncio
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
import yt_dlp
from utils.logger import logger
from discord.ext import commands
from cogs.presets import EQ_PRESETS
from views.music_controls import MusicControlView, SearchAutoplayView

# Downloads landen hier. Der Ordner wird beim Start automatisch angelegt,
# falls er noch nicht existiert – kein manuelles Erstellen nötig.
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


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
        self.prefetch_task = None   # Läuft im Hintergrund während ein Song spielt
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

        # Genres für den Autoplay-Modus – mit !genres anpassbar.
        self.autoplay_genres = ["chill music", "lofi", "pop", "edm", "jazz", "gaming music"]

        self._start_time = time.monotonic()
        self._process = psutil.Process()

        self.update_ydl()
        logger.info("[INIT] MusicCommands erfolgreich initialisiert.")

    def update_ydl(self):
        """Erstellt eine neue yt_dlp-Instanz mit den aktuellen Format-Einstellungen.

        Wird beim Start und nach jedem !format-Wechsel aufgerufen, da yt_dlp-Optionen
        nach der Initialisierung nicht mehr änderbar sind.
        """
        base_opts = {
            "quiet": True,
            "no_warnings": True,
            # Bevorzugt Opus/webm mit mindestens 160kbps (YouTube's höchste Audio-Tier),
            # fällt auf 128kbps, dann beliebiges webm, dann Opus, dann best zurück.
            # prefer_free_formats bevorzugt Opus über AAC bei gleichwertiger Qualität.
            "format": "bestaudio[ext=webm][abr>=160]/bestaudio[ext=webm][abr>=128]/bestaudio[ext=webm]/bestaudio[acodec=opus]/bestaudio/best",
            "prefer_free_formats": True,
            "default_search": "ytsearch",  # Suchbegriffe werden automatisch als YT-Suche behandelt
            "noplaylist": False,
            # prepare_filename() gibt später den exakt gleichen Pfad zurück den
            # yt_dlp beim Speichern verwendet – inklusive Sonderzeichen-Bereinigung.
            "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
            # Netzwerk-Timeout direkt in yt_dlp – bricht den Thread intern ab,
            # sodass asyncio.wait_for nicht auf den Thread warten muss.
            "socket_timeout": 15,
        }
        if self.audio_format == "mp3":
            # MP3-Konvertierung läuft über FFmpeg als Post-Processing-Schritt.
            # webm braucht das nicht – der Stream wird direkt gespeichert.
            base_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
        self.ydl = yt_dlp.YoutubeDL(base_opts)

        # Gecachte Instanzen für Suche und URL-Abfragen – vermeidet Initialisierungs-
        # Overhead bei jedem !p-Aufruf und erlaubt yt_dlp internen Cache-Reuse.
        self.search_ydl = yt_dlp.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "socket_timeout": 15,
        })
        _url_base = {
            "quiet": True,
            "no_warnings": True,
            "format": "bestaudio/best",
            "default_search": "ytsearch",
            "socket_timeout": 15,
        }
        self.url_ydl = yt_dlp.YoutubeDL({**_url_base, "noplaylist": True, "extract_flat": False})
        self.playlist_ydl = yt_dlp.YoutubeDL({**_url_base, "noplaylist": False, "extract_flat": "in_playlist"})

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
        """Sucht einen zufälligen Song und fügt ihn vorne in die Queue ein."""
        logger.info("[Autoplay] Suche zufälligen Song")

        random_query = random.choice(self.autoplay_genres)

        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(self.ydl.extract_info, random_query, download=False),
                timeout=30.0,
            )
            if "entries" in info:
                info = info["entries"][0]
            url = info.get("webpage_url")
            title = info.get("title", "Unbekannt")

            # appendleft, damit der Autoplay-Song sofort als nächstes läuft
            # und nicht ans Ende der Queue wandert.
            self.queue.appendleft((url, title))
            logger.info(f"[Autoplay] Hinzugefügt: {title} ({url})")
            await ctx.send(f"🔁 Autoplay hinzugefügt: **{title}**")
            if not self.is_playing:
                self.is_playing = True
                await self.play_next(ctx)
        except asyncio.TimeoutError:
            await ctx.send("❌ Autoplay-Suche hat zu lange gedauert.")
            logger.warning("[Autoplay] Timeout bei extract_info")
        except Exception:
            await ctx.send("❌ Fehler bei Autoplay.")
            logger.exception("[Autoplay Fehler]")

    async def _prefetch_next(self):
        """Lädt den nächsten Song in der Queue still im Hintergrund herunter.

        Wird direkt nach dem Start eines Tracks gestartet, damit der nächste
        Song idealerweise schon bereit liegt wenn er dran ist.
        """
        if not self.queue:
            return
        url, title = self.queue[0]  # Peek – nicht aus der Queue entfernen
        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(self.ydl.extract_info, url, download=False),
                timeout=30.0,
            )
            if "entries" in info:
                info = info["entries"][0]
            filename = Path(self.ydl.prepare_filename(info))
            if not filename.exists():
                logger.info(f"[Prefetch] Lade vor: {info.get('title', title)}")
                await asyncio.to_thread(self.ydl.download, [info["webpage_url"]])
                logger.info(f"[Prefetch] Fertig: {filename.name}")
            else:
                logger.info(f"[Prefetch] Bereits im Cache: {filename.name}")
        except asyncio.TimeoutError:
            logger.warning(f"[Prefetch] Timeout für: {title}")
        except Exception as e:
            # Prefetch-Fehler sind nicht fatal – play_next lädt im Zweifelsfall selbst.
            logger.warning(f"[Prefetch] Vorladen fehlgeschlagen für: {title}: {e}")

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
        """Löst eine URL auf: extrahiert Metadaten via yt_dlp und stellt sicher,
        dass die Audiodatei lokal vorliegt (Download oder Cache-Hit).

        Raises asyncio.TimeoutError wenn extract_info > 30 s dauert.
        Returns: (info dict, Path, title str, duration int)
        """
        info = await asyncio.wait_for(
            asyncio.to_thread(self.ydl.extract_info, url, download=False),
            timeout=30.0,
        )
        if "entries" in info:
            info = info["entries"][0]

        title = info.get("title", "Unbekannter Titel")
        duration = info.get("duration", 0)
        # prepare_filename liefert exakt den Pfad den yt_dlp beim Download
        # verwendet – Sonderzeichen im Titel werden dabei automatisch bereinigt.
        filename = Path(self.ydl.prepare_filename(info))

        if not filename.exists():
            # Wenn der Prefetch-Task diese Datei gerade lädt, warten statt
            # parallel runterzuladen – doppelte Downloads würden die Datei korrumpieren.
            if self.prefetch_task and not self.prefetch_task.done():
                logger.info(f"[Download] Warte auf laufenden Prefetch für: {title}")
                try:
                    await self.prefetch_task
                except Exception:
                    logger.debug("[Prefetch wait] Prefetch fehlgeschlagen, lade selbst herunter.")

            if not filename.exists():  # Nochmal prüfen – Prefetch könnte es erledigt haben
                logger.info(f"[Download] Lade {title} herunter...")
                await asyncio.to_thread(self.ydl.download, [info["webpage_url"]])
                logger.info(f"[Download] Gespeichert als: {filename.name}")
            else:
                logger.info(f"[Wiedergabe] Prefetch erfolgreich – starte sofort: {filename.name}")
        else:
            logger.info(f"[Wiedergabe] Verwende vorhandene Datei: {filename.name}")

        return info, filename, title, duration

    async def play_next(self, ctx):
        """Spielt den nächsten Song in der Queue. Wird rekursiv nach jedem Track aufgerufen."""
        if not self.queue:
            logger.info("[Queue] Leere Warteschlange. Wiedergabe gestoppt.")
            self.is_playing = False
            self.current_track = None
            # Autoplay rettet die Stille – aber nur wenn gewünscht.
            if self.autoplay_enabled:
                await self.autoplay(ctx)
            return

        url, title = self.queue.popleft()
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

                if self.queue and ctx.voice_client:
                    logger.info(f"[Warten] Queue hat {len(self.queue)} Songs. Warten auf nächsten...")
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
                elif not self.queue and ctx.voice_client:
                    # Queue leer, aber Bot bleibt im Kanal und wartet auf !p.
                    # Kein automatisches Verlassen – das wäre unhöflich.
                    self.is_playing = False
                    logger.info("[PAUSE] Queue leer, aber Bot bleibt im Kanal. Warte auf !p ...")
                else:
                    logger.info("[KEINE VERBINDUNG] Keine Verbindung. Warte auf !j ...")

            self.track_start_time = time.monotonic()
            ctx.voice_client.play(source, after=after_playing)
            logger.info(f"[Wiedergabe] Starte: {title}")
            if self.now_playing_msg:
                try:
                    old_title = self.current_track[1] if self.current_track else None
                    old_content = f"🎶 **{old_title}**" if old_title else None
                    await self.now_playing_msg.edit(content=old_content, embed=None, view=None)
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
                self.prefetch_task = asyncio.create_task(self._prefetch_next())

        except asyncio.TimeoutError:
            await ctx.send(f"⚠️ Timeout beim Laden von **{title}**. Überspringe...")
            asyncio.create_task(self.play_next(ctx))
            return
        except Exception:
            logger.exception("[Fehler bei play_next]")
            await ctx.send(f"⚠️ Fehler beim Laden von **{title}**. Überspringe...")
            # Fehlerhaften Track überspringen – create_task statt direkter Rekursion,
            # damit bei vielen schlechten URLs der Call-Stack nicht überfüllt wird.
            asyncio.create_task(self.play_next(ctx))
            return

    @commands.command()
    async def p(self, ctx, *, eingabe):
        """Spielt eine URL, Playlist oder Suchbegriff. Bei Suche werden 3 Treffer zur Auswahl angezeigt."""
        logger.info(f"[p] Eingabe erhalten: {eingabe}")

        if ctx.voice_client is None:
            await ctx.send("❌ Bitte benutze `!j`, um den Bot in deinen Voice-Channel zu holen.")
            logger.warning("[p] Kein VoiceClient vorhanden.")
            return

        # Wenn kein http am Anfang → Suchbegriff → ersten Treffer sofort abspielen,
        # Treffer 2 und 3 als Buttons anzeigen falls es der Falsche war.
        if not eingabe.startswith("http"):
            try:
                await ctx.send("🔎 Suche läuft...")
                results = await asyncio.wait_for(
                    asyncio.to_thread(self.search_ydl.extract_info, f"ytsearch3:{eingabe}", download=False),
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

            # Ersten Treffer direkt in die Queue legen
            first = entries[0]
            url = first.get("webpage_url") or first.get("url")
            title = first.get("title", "Unbekannter Titel")
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
        ydl_instance = self.playlist_ydl if is_playlist else self.url_ydl

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
            self.queue.append((url, title))
            await ctx.send(f"🎶 Hinzugefügt: **{title}**")

        if not self.is_playing:
            self.is_playing = True
            await self.play_next(ctx)

    @commands.command(name="next")
    async def next_song(self, ctx, *, eingabe):
        """Fügt einen Song an die erste Stelle der Queue ein (spielt als nächstes).
        Format: !next URL  oder  !next URL||Titel  oder  !next Suchbegriff"""
        if ctx.voice_client is None:
            await ctx.send("❌ Bitte benutze `!j`, um den Bot in deinen Voice-Channel zu holen.")
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
                    asyncio.to_thread(self.search_ydl.extract_info, f"ytsearch3:{eingabe}", download=False),
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
                asyncio.to_thread(self.url_ydl.extract_info, eingabe, download=False),
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
                if len(message) + len(line) >= 1980:
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
        self.is_playing = False
        self.current_track = None
        self.loop_mode = None
        if self.prefetch_task and not self.prefetch_task.done():
            self.prefetch_task.cancel()
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
        await ctx.send("🧹 Die Warteschlange wurde geleert und die Wiedergabe gestoppt.")

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

        # Downloads-Ordner
        dl_files = list(DOWNLOAD_DIR.glob("*"))
        dl_count = len(dl_files)
        dl_size_mb = sum(f.stat().st_size for f in dl_files if f.is_file()) / 1024 / 1024

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

    @commands.command(name="genres")
    async def genres(self, ctx, aktion: str = None, *, genre: str = None):
        """Verwaltet die Autoplay-Genres. Nutzung: !genres | !genres add <genre> | !genres remove <genre> | !genres reset"""
        DEFAULT_GENRES = ["chill music", "lofi", "pop", "edm", "jazz", "gaming music"]

        if aktion is None:
            genre_list = "\n".join(f"• {g}" for g in self.autoplay_genres)
            await ctx.send(f"🎵 Aktuelle Autoplay-Genres:\n{genre_list}")

        elif aktion.lower() == "add":
            if not genre:
                await ctx.send("❌ Bitte ein Genre angeben: `!genres add <genre>`")
                return
            if len(self.autoplay_genres) >= 20:
                await ctx.send("❌ Maximal 20 Genres erlaubt. Entferne zuerst ein Genre mit `!genres remove`.")
                return
            if genre.lower() in [g.lower() for g in self.autoplay_genres]:
                await ctx.send(f"⚠️ **{genre}** ist bereits in der Liste.")
                return
            self.autoplay_genres.append(genre)
            await ctx.send(f"✅ **{genre}** zur Autoplay-Liste hinzugefügt.")

        elif aktion.lower() == "remove":
            if not genre:
                await ctx.send("❌ Bitte ein Genre angeben: `!genres remove <genre>`")
                return
            match = next((g for g in self.autoplay_genres if g.lower() == genre.lower()), None)
            if not match:
                await ctx.send(f"❌ **{genre}** nicht in der Liste gefunden.")
                return
            self.autoplay_genres.remove(match)
            await ctx.send(f"🗑️ **{match}** entfernt.")
            if not self.autoplay_genres:
                self.autoplay_genres = list(DEFAULT_GENRES)
                await ctx.send("⚠️ Liste war leer – Standard-Genres wiederhergestellt.")

        elif aktion.lower() == "reset":
            self.autoplay_genres = list(DEFAULT_GENRES)
            await ctx.send("🔄 Autoplay-Genres auf Standard zurückgesetzt.")

        else:
            await ctx.send("❌ Unbekannte Aktion. Nutze: `!genres`, `!genres add <genre>`, `!genres remove <genre>`, `!genres reset`")
