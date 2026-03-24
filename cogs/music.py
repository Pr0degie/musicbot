# Hier lebt die gesamte Musik-Logik: Queue, Downloads, Wiedergabe, EQ und Autoplay.
# Kurz gesagt: die wichtigste Datei im ganzen Bot. Treat her well.

import asyncio
import json
import random
from collections import deque
from pathlib import Path

import discord
import yt_dlp
from utils.logger import logger
from discord.ext import commands
from views.music_controls import MusicControlView

# Downloads landen hier. Der Ordner wird beim Start automatisch angelegt,
# falls er noch nicht existiert – kein manuelles Erstellen nötig.
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


class MusicCommands(commands.Cog):
    """Cog für alle Musikbefehle: Wiedergabe, Queue, EQ, Autoplay."""

    # Beide Konstanten sind gleich – MAX_PLAYLIST_LENGTH ist der "weiche" Richtwert,
    # HARD_PLAYLIST_LIMIT ist die tatsächlich durchgesetzte Grenze beim Einlesen.
    MAX_PLAYLIST_LENGTH = 150
    HARD_PLAYLIST_LIMIT = 150

    def __init__(self, bot):
        self.bot = bot

        # deque statt list, weil popleft() in O(1) läuft – bei langen Queues
        # ist das deutlich schneller als list.pop(0).
        self.queue = deque()

        self.is_playing = False
        self.last_played = None  # Wird von !replay genutzt

        # Standard-EQ und -Format beim Start
        self.equalizer = "bassboost"
        self.audio_format = "webm"

        # FFmpeg-Filterchains pro Preset. Die Werte werden direkt als
        # CLI-Optionen an FFmpeg übergeben. "flat" hat keinen Filter.
        self.eq_presets = {
            "bassboost": "-af bass=g=12,dynaudnorm=f=200,volume=0.85,aresample=48000",
            "flat": "",
            "vocalboost": "-af equalizer=f=1000:width_type=o:width=2:g=5",
            "superbass": "-af bass=g=20",  # Für wenn die Nachbarn noch wach sind
            # Sub-Bass (~80Hz) boosten für Punch, Upper-Bass (~250Hz) leicht senken
            # gegen Matsch – klingt auf basslastigen Liedern cleaner als bassboost.
            "punchy": "-af equalizer=f=80:width_type=o:width=2:g=8,equalizer=f=250:width_type=o:width=2:g=-3,aresample=48000",
        }

        # Autoplay ist standardmäßig aus – niemand will, dass der Bot
        # nach Mitternacht eigenständig Jazz spielt.
        self.autoplay_enabled = False

        self.update_ydl()
        logger.info("[INIT] MusicCommands erfolgreich initialisiert.")

    def update_ydl(self):
        """Erstellt eine neue yt_dlp-Instanz mit den aktuellen Format-Einstellungen.

        Wird beim Start und nach jedem !format-Wechsel aufgerufen, da yt_dlp-Optionen
        nach der Initialisierung nicht mehr änderbar sind.
        """
        base_opts = {
            "quiet": True,
            "format": "bestaudio[ext=webm]/bestaudio/best",
            "default_search": "ytsearch",  # Suchbegriffe werden automatisch als YT-Suche behandelt
            "noplaylist": False,
            # Dateiname basiert auf der video_id – so gibt es nie einen Mismatch
            # zwischen dem erwarteten und tatsächlich heruntergeladenen Dateinamen.
            "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
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

        # Kleine Vibe-Auswahl – wer mag, kann hier gerne seine Lieblings-Genres eintragen.
        search_terms = ["chill music", "lofi", "pop", "edm", "jazz", "gaming music"]
        random_query = random.choice(search_terms)

        try:
            info = await asyncio.to_thread(self.ydl.extract_info, random_query, download=False)
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
        except Exception:
            await ctx.send("❌ Fehler bei Autoplay.")
            logger.exception("[Autoplay Fehler]")

    async def play_next(self, ctx):
        """Spielt den nächsten Song in der Queue. Wird rekursiv nach jedem Track aufgerufen."""
        if not self.queue:
            logger.info("[Queue] Leere Warteschlange. Wiedergabe gestoppt.")
            self.is_playing = False
            # Autoplay rettet die Stille – aber nur wenn gewünscht.
            if self.autoplay_enabled:
                await self.autoplay(ctx)
            return

        url, title = self.queue.popleft()
        self.last_played = (url, title)
        logger.info(f"[Nächster Track] {title} ({url})")

        try:
            # asyncio.to_thread verhindert, dass der Bot während des yt_dlp-Aufrufs
            # einfriert – yt_dlp ist synchron und würde sonst den Event-Loop blockieren.
            info = await asyncio.to_thread(self.ydl.extract_info, url, download=False)
            if "entries" in info:
                info = info["entries"][0]

            title = info.get("title", "Unbekannter Titel")
            video_id = info.get("id")
            ext = info.get("ext", self.audio_format)

            # Dateiname basiert immer auf der video_id – so stimmt er garantiert
            # mit dem überein, was yt_dlp tatsächlich speichert.
            filename = DOWNLOAD_DIR / f"{video_id}.{ext}"

            if not filename.exists():
                # Datei noch nicht im Cache → herunterladen
                logger.info(f"[Download] Lade {title} herunter...")
                await asyncio.to_thread(self.ydl.download, [info["webpage_url"]])
                logger.info(f"[Download] Gespeichert als: {filename.name}")
            else:
                # Cache-Hit! Kein erneuter Download nötig. Spart Zeit und Bandbreite.
                logger.info(f"[Wiedergabe] Verwende vorhandene Datei: {filename.name}")

            eq_filter = self.eq_presets.get(self.equalizer, "")
            # -vn unterdrückt den Video-Stream in FFmpeg (wir wollen nur Audio).
            ffmpeg_options = f"-vn {eq_filter}".strip()
            source = discord.FFmpegPCMAudio(str(filename), options=ffmpeg_options)

            def after_playing(error):
                """Callback, der nach jedem Track von FFmpeg aufgerufen wird.

                Läuft in einem separaten Thread – daher run_coroutine_threadsafe
                statt await. Direkt awaiten würde hier crashen.
                """
                if error:
                    logger.warning(f"[Fehler beim Abspielen] {error}")

                # Queue-Stand nach jedem Track in Datei sichern – nicht als
                # Restore-Point gedacht, nur als Protokoll der letzten Session.
                queue_data = list(self.queue)
                try:
                    with open("last_queue.json", "w", encoding="utf-8") as f:
                        json.dump(queue_data, f, ensure_ascii=False, indent=2)
                    logger.info("[SAVE] Queue automatisch gespeichert nach Track-Ende.")
                except Exception as e:
                    logger.warning(f"[SAVE] Fehler beim Speichern der Queue: {e}")

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
                    logger.info("[PAUSE] Queue leer, aber Bot bleibt im Kanal. Warte auf !p ...")
                else:
                    logger.info("[KEINE VERBINDUNG] Keine Verbindung. Warte auf !j ...")

            ctx.voice_client.play(source, after=after_playing)
            logger.info(f"[Wiedergabe] Starte: {title}")
            await ctx.send(
                f"🎶 Jetzt läuft: **{title}**", view=MusicControlView(self, ctx)
            )
            self.is_playing = True

        except Exception:
            logger.exception("[Fehler bei play_next]")
            await ctx.send(f"⚠️ Fehler beim Laden von {title}. Überspringe...")
            # Fehlerhaften Track überspringen und einfach weitermachen.
            await self.play_next(ctx)

    @commands.command()
    async def p(self, ctx, *, eingabe):
        """Spielt eine URL oder einen Suchbegriff. Erkennt Playlisten automatisch."""
        logger.info(f"[p] Eingabe erhalten: {eingabe}")

        if ctx.voice_client is None:
            await ctx.send("❌ Bitte benutze `!j`, um den Bot in deinen Voice-Channel zu holen.")
            logger.warning("[p] Kein VoiceClient vorhanden.")
            return

        # Einfache Heuristik: Wenn "playlist?" oder "list=" in der URL steht,
        # ist es eine Playlist. Funktioniert für alle gängigen YouTube-Playlist-URLs.
        is_playlist = "playlist?" in eingabe or "list=" in eingabe
        ydl_opts = {
            "quiet": True,
            "format": "bestaudio/best",
            "default_search": "ytsearch",
            "noplaylist": not is_playlist,
            # extract_flat holt nur Metadaten der Playlist-Einträge, ohne jeden
            # Track einzeln aufzulösen – viel schneller bei langen Playlisten.
            "extract_flat": "in_playlist" if is_playlist else False,
        }

        try:
            await ctx.send("🔎 Verarbeite Eingabe... bitte warten.")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, eingabe, download=False)
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
                url = entry.get("url") or entry.get("webpage_url")
                title = entry.get("title", "Unbekannter Titel")
                if url:
                    self.queue.append((url, title))
                    added_count += 1
            await ctx.send(f"✅ {added_count} Titel zur Warteschlange hinzugefügt.")
        else:
            url = info.get("webpage_url")
            title = info.get("title", "Unbekannter Titel")
            self.queue.append((url, title))
            await ctx.send(f"🎶 Hinzugefügt: **{title}**")

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
        """Leert die Queue und stoppt die Wiedergabe."""
        self.queue.clear()
        self.is_playing = False
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
            self.queue.appendleft(self.last_played)
            await ctx.send(f"🔁 Wiederhole: **{self.last_played[1]}**")
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
            await ctx.send(f"🎚️ Equalizer auf `{preset}` gesetzt.")
        else:
            presets = ", ".join(self.eq_presets.keys())
            await ctx.send(f"❌ Unbekanntes Profil. Verfügbare Presets: {presets}")
