import asyncio
import random
from collections import deque

import discord
import yt_dlp
from config import logger
from discord.ext import commands
from views.music_controls import MusicControlView


class MusicCommands(commands.Cog):
    MAX_PLAYLIST_LENGTH = 150
    HARD_PLAYLIST_LIMIT = 150

    def __init__(self, bot):
        self.bot = bot
        self.queue = deque()
        self.is_playing = False
        self.last_played = None
        self.equalizer = "bassboost"
        self.eq_presets = {
            "bassboost": "-af bass=g=12,dynaudnorm=f=200,volume=0.85,aresample=48000",
            "flat": "",
            "vocalboost": "-af equalizer=f=1000:width_type=o:width=2:g=5",
            "superbass": "-af bass=g=20",
        }
        self.ydl = yt_dlp.YoutubeDL(
            {
                "quiet": True,
                "format": "bestaudio/best",
                "default_search": "ytsearch",
                "noplaylist": True,
            }
        )

    async def autoplay(self, ctx):
        search_terms = ["chill music", "lofi", "pop", "edm", "jazz", "gaming music"]
        random_query = random.choice(search_terms)
        try:
            info = self.ydl.extract_info(random_query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            url = info.get("webpage_url")
            title = info.get("title", "Unbekannt")
            self.queue.appendleft((url, title))
            await ctx.send(f"🔁 Autoplay hinzugefügt: **{title}**")
            if not self.is_playing:
                self.is_playing = True
                await self.play_next(ctx)
        except Exception:
            await ctx.send("❌ Fehler bei Autoplay.")
            logger.exception("[Autoplay Fehler]")

    async def play_next(self, ctx):
        if not self.queue:
            self.is_playing = False
            return

        url, title = self.queue.popleft()
        self.last_played = (url, title)

        try:
            info = self.ydl.extract_info(url, download=False)
            audio_url = info.get("url")
            title = info.get("title", "Unbekannter Titel")

            source = discord.FFmpegPCMAudio(
                audio_url,
                before_options="-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options=f'-vn {self.eq_presets.get(self.equalizer, "")}',
            )

            def after_playing(error):
                if error:
                    logger.warning(f"[Fehler beim Abspielen] {error}")
                fut = asyncio.run_coroutine_threadsafe(
                    self.play_next(ctx), self.bot.loop
                )
                fut.add_done_callback(lambda f: f.exception())

            ctx.voice_client.play(source, after=after_playing)
            await ctx.send(
                f"🎶 Jetzt läuft: **{title}**", view=MusicControlView(self, ctx)
            )
            self.is_playing = True

        except Exception:
            logger.exception("[Fehler bei play_next]")
            await ctx.send(f"⚠️ Fehler beim Laden von {title}. Überspringe...")
            await self.play_next(ctx)

    @commands.command(name="p")
    async def play(self, ctx, url):
        if ctx.voice_client is None:
            await ctx.send(
                "❌ Der Bot ist in keinem Voice-Channel. Bitte verwende zuerst `!j`."
            )
            return

        ydl_opts = {
            "extract_flat": "in_playlist",
            "skip_download": True,
            "quiet": True,
            "noplaylist": False,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if "entries" in info:
                    playlist_length = len(info["entries"])
                    if playlist_length > self.HARD_PLAYLIST_LIMIT:
                        await ctx.send(
                            f"🚫 Playlist ist zu groß! Maximal {self.HARD_PLAYLIST_LIMIT} Einträge erlaubt."
                        )
                        return
                    if playlist_length > self.MAX_PLAYLIST_LENGTH:
                        await ctx.send(
                            f"⚠️ Playlist enthält {playlist_length} Songs. Es werden nur die ersten {self.MAX_PLAYLIST_LENGTH} hinzugefügt."
                        )
                    added = 0
                    for entry in info["entries"][: self.MAX_PLAYLIST_LENGTH]:
                        try:
                            entry_url = entry.get("url") or entry.get("webpage_url")
                            entry_title = entry.get("title", "Unbekannt")
                            if entry_url:
                                self.queue.append((entry_url, entry_title))
                                added += 1
                        except Exception:
                            continue
                    await ctx.send(f"🎵 {added} Titel zur Warteschlange hinzugefügt.")
                else:
                    title = info.get("title", "Unbekannt")
                    self.queue.append((url, title))
                    await ctx.send(f"➕ Zur Warteschlange hinzugefügt: {title}")
        except Exception:
            await ctx.send(
                "❌ Fehler beim Verarbeiten der URL. Bitte überprüfe den Link."
            )
            return

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

    @commands.command()
    async def resume(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("▶️ Song fortgesetzt.")

    @commands.command(name="q")
    async def queue_list(self, ctx):
        if not self.queue:
            await ctx.send("🪹 Die Warteschlange ist leer.")
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
        await ctx.send(
            "🧹 Die Warteschlange wurde geleert und die Wiedergabe gestoppt."
        )

    @commands.command()
    async def remove(self, ctx, index: int):
        if 1 <= index <= len(self.queue):
            removed = self.queue.pop(index - 1)
            await ctx.send(f"❌ Entfernt: {removed[1]}")
        else:
            await ctx.send("❌ Ungültiger Index. Bitte eine gültige Zahl angeben.")

    @commands.command()
    async def shuffle(self, ctx):
        if len(self.queue) < 2:
            await ctx.send("⚠️ Nicht genug Songs zum Mischen.")
        else:
            random.shuffle(self.queue)
            await ctx.send("🔀 Warteschlange wurde gemischt.")

    @commands.command()
    async def replay(self, ctx):
        if self.last_played:
            self.queue.insert(0, self.last_played)
            await ctx.send(f"🔁 Wiederhole: {self.last_played[1]}")
            if not self.is_playing:
                self.is_playing = True
                await self.play_next(ctx)
        else:
            await ctx.send("❌ Kein letzter Song vorhanden.")

    @commands.command()
    async def eq(self, ctx, preset: str = None):
        if not preset:
            await ctx.send(
                f"Verfügbare Presets: bassboost, flat, vocalboost, superbass"
            )
            return
        if preset.lower() in self.eq_presets:
            self.equalizer = preset.lower()
            await ctx.send(f"🎚️ Equalizer auf `{preset}` gesetzt.")
        else:
            presets = ", ".join(self.eq_presets.keys())
            await ctx.send(f"❌ Unbekanntes Profil. Verfügbare Presets: {presets}")
