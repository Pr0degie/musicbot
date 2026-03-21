import asyncio
import json
import random
from collections import deque
from pathlib import Path

import discord
import yt_dlp
from config import logger
from discord.ext import commands
from views.music_controls import MusicControlView

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


class MusicCommands(commands.Cog):
    MAX_PLAYLIST_LENGTH = 150
    HARD_PLAYLIST_LIMIT = 150

    def __init__(self, bot):
        self.bot = bot
        self.queue = deque()
        self.is_playing = False
        self.last_played = None
        self.equalizer = "bassboost"
        self.audio_format = "webm"
        self.eq_presets = {
            "bassboost": "-af bass=g=12,dynaudnorm=f=200,volume=0.85,aresample=48000",
            "flat": "",
            "vocalboost": "-af equalizer=f=1000:width_type=o:width=2:g=5",
            "superbass": "-af bass=g=20",
        }
        self.update_ydl()
        logger.info("[INIT] MusicCommands erfolgreich initialisiert.")

    def update_ydl(self):
        base_opts = {
            "quiet": True,
            "format": "bestaudio[ext=webm]/bestaudio/best",
            "default_search": "ytsearch",
            "noplaylist": False,
            # BUG FIX #3: video_id im Dateinamen -> kein Mismatch mehr
            "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        }
        if self.audio_format == "mp3":
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
        if typ.lower() in ["mp3", "webm"]:
            self.audio_format = typ.lower()
            self.update_ydl()
            await ctx.send(f"🔄 Audioformat auf **{self.audio_format}** gesetzt.")
        else:
            await ctx.send("❌ Ungültiges Format. Verfügbare Optionen: mp3, webm")

    async def autoplay(self, ctx):
        logger.info("[Autoplay] Suche zufälligen Song")
        search_terms = ["chill music", "lofi", "pop", "edm", "jazz", "gaming music"]
        random_query = random.choice(search_terms)
        try:
            info = await asyncio.to_thread(self.ydl.extract_info, random_query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            url = info.get("webpage_url")
            title = info.get("title", "Unbekannt")
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
        if not self.queue:
            logger.info("[Queue] Leere Warteschlange. Wiedergabe gestoppt.")
            self.is_playing = False
            return

        url, title = self.queue.popleft()
        self.last_played = (url, title)
        logger.info(f"[Nächster Track] {title} ({url})")

        try:
            info = await asyncio.to_thread(self.ydl.extract_info, url, download=False)
            if "entries" in info:
                info = info["entries"][0]

            title = info.get("title", "Unbekannter Titel")
            video_id = info.get("id")
            ext = info.get("ext", self.audio_format)

            # BUG FIX #3: Dateiname basiert auf video_id – stimmt immer mit Download überein
            filename = DOWNLOAD_DIR / f"{video_id}.{ext}"

            if not filename.exists():
                logger.info(f"[Download] Lade {title} herunter...")
                await asyncio.to_thread(self.ydl.download, [info["webpage_url"]])
                logger.info(f"[Download] Gespeichert als: {filename.name}")
            else:
                logger.info(f"[Wiedergabe] Verwende vorhandene Datei: {filename.name}")

            eq_filter = self.eq_presets.get(self.equalizer, "")
            ffmpeg_options = f"-vn {eq_filter}".strip()
            source = discord.FFmpegPCMAudio(str(filename), options=ffmpeg_options)

            def after_playing(error):
                if error:
                    logger.warning(f"[Fehler beim Abspielen] {error}")
                
                # Queue speichern
                queue_data = list(self.queue)
                try:
                    with open("last_queue.json", "w", encoding="utf-8") as f:
                        json.dump(queue_data, f, ensure_ascii=False, indent=2)
                    logger.info("[SAVE] Queue automatisch gespeichert nach Track-Ende.")
                except Exception as e:
                    logger.warning(f"[SAVE] Fehler beim Speichern der Queue: {e}")
                
                # ✅ BOT IM KANAL BEHALTEN – Nur nächsten Song planen, wenn Queue NICHT leer
                if self.queue and ctx.voice_client:
                    logger.info(f"[Warten] Queue hat {len(self.queue)} Songs. Warten auf nächsten...")
                    fut = asyncio.run_coroutine_threadsafe(
                        self.play_next(ctx), self.bot.loop
                    )
                    fut.add_done_callback(lambda f: f.cancelled() or f.exception())
                elif not self.queue and ctx.voice_client:
                    # ✅ Queue leer, aber Bot verbunden → BLEIBT IM KANAL
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
            await self.play_next(ctx)

    @commands.command()
    async def p(self, ctx, *, eingabe):
        logger.info(f"[p] Eingabe erhalten: {eingabe}")

        if ctx.voice_client is None:
            await ctx.send("❌ Bitte benutze `!j`, um den Bot in deinen Voice-Channel zu holen.")
            logger.warning("[p] Kein VoiceClient vorhanden.")
            return

        is_playlist = "playlist?" in eingabe or "list=" in eingabe
        ydl_opts = {
            "quiet": True,
            "format": "bestaudio/best",
            "default_search": "ytsearch",
            "noplaylist": not is_playlist,
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
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await ctx.send("⏭️ Song übersprungen. Spiele den nächsten Titel ...")
        else:
            await ctx.send("⚠️ Es läuft gerade kein Song.")

    @commands.command(name="x")
    async def pause(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("⏸️ Song pausiert.")
            # ✅ Verbesserung 3: is_playing auf False setzen, damit !resume erkennt
            self.is_playing = False
        else:
            await ctx.send("⚠️ Es läuft gerade kein Song.")

    @commands.command()
    async def resume(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("▶️ Song fortgesetzt.")
            # ✅ Verbesserung 3: is_playing auf True setzen
            self.is_playing = True
        elif not self.is_playing and self.queue:
            # Falls nicht pausiert, aber Queue da ist → sofort starten
            logger.info("[Auto-Start] Queue nicht leer, starte nächsten Song.")
            await self.play_next(ctx)
        else:
            await ctx.send("⚠️ Kein Song zum Fortsetzen.")

    @commands.command(name="q")
    async def queue_list(self, ctx):
        if not self.queue:
            await ctx.send("🧫 Die Warteschlange ist leer.")
        else:
            message = "📜 Aktuelle Warteschlange:\n"
            for i, (url, title) in enumerate(self.queue, 1):
                if len(message) >= 1980:
                    break
                message += f"{i}. {title}\n"
            await ctx.send(message)

    @commands.command()
    async def clear(self, ctx):
        self.queue.clear()
        self.is_playing = False
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
        await ctx.send("🧹 Die Warteschlange wurde geleert und die Wiedergabe gestoppt.")

    @commands.command()
    async def remove(self, ctx, index: int):
        # BUG FIX #2: deque hat kein pop(index) → in Liste konvertieren
        if 1 <= index <= len(self.queue):
            queue_list = list(self.queue)
            removed = queue_list.pop(index - 1)
            self.queue = deque(queue_list)
            await ctx.send(f"❌ Entfernt: **{removed[1]}**")
        else:
            await ctx.send("❌ Ungültiger Index. Bitte eine gültige Zahl angeben.")

    @commands.command()
    async def shuffle(self, ctx):
        # BUG FIX #1: random.shuffle() funktioniert nicht auf deque → in Liste konvertieren
        if len(self.queue) < 2:
            await ctx.send("⚠️ Nicht genug Songs zum Mischen.")
        else:
            queue_list = list(self.queue)
            random.shuffle(queue_list)
            self.queue = deque(queue_list)
            await ctx.send("🔀 Warteschlange wurde gemischt.")

    @commands.command()
    async def replay(self, ctx):
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
