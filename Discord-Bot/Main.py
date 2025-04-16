import discord 
from discord.ext import commands
from dotenv import load_dotenv
import os
import asyncio
import yt_dlp
import random

# Hinweis: PyNaCl wird automatisch von discord.py verwendet – kein Import nötig.
# FFmpeg muss installiert und im Systempfad sein (C:\ffmpeg\bin)

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.add_cog(BasicCommands(self))
        await self.add_cog(MusicCommands(self))

    async def on_ready(self):
        print(f"Bot ist online als {self.user}")

bot = MusicBot()


class BasicCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def ping(self, ctx):
        await ctx.send("Pong!")

    @commands.command()
    async def echo(self, ctx, *, message):
        await ctx.send(message)

    #join
    @commands.command()
    async def j(self, ctx):
        """Lässt den Bot dem Voice-Channel des Users beitreten."""
        if ctx.author.voice:
            channel = ctx.author.voice.channel
            if ctx.voice_client is None:
                await channel.connect()
                await ctx.send(f"🔊 Verbunden mit: {channel.name}")
            else:
                await ctx.voice_client.move_to(channel)
                await ctx.send(f"🔄 Bewegt zu: {channel.name}")
        else:
            await ctx.send("⚠️ Du bist in keinem Voice-Channel.")
            
            
    @commands.command()
    async def leave(self,ctx):
        if ctx.author.voice:
            channel=ctx.author.voice.channel
            if ctx.voice_client != None:
                await channel.disconnect()
                await ctx.send(f"Verpiss DICH")

class MusicCommands(commands.Cog):
    MAX_PLAYLIST_LENGTH = 150  # Wie viele Songs maximal übernommen werden
    HARD_PLAYLIST_LIMIT = 150  # Wie viele Songs maximal verarbeitet werden dürfen

    def __init__(self, bot):
        self.bot = bot
        self.queue = []
        self.is_playing = False
        self.last_played = None
        self.equalizer = "bassboost"  # Standard-Profil
        self.eq_presets = {
            "bassboost": '-af bass=g=15,dynaudnorm=f=200',
            "flat": '',
            "vocalboost": '-af equalizer=f=1000:width_type=o:width=2:g=5',
            "superbass": '-af bass=g=20',
}

    async def play_next(self, ctx):
        if not self.queue:
            self.is_playing = False
            return

        url, title = self.queue.pop(0)
        self.last_played = (url, title)

        ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'default_search': 'auto',
            'noplaylist': True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                audio_url = info.get('url')
                title = info.get('title', 'Unbekannter Titel')

                if not audio_url:
                    await ctx.send(f"❌ Konnte den Audiostream nicht finden für: {title}")
                    return

                #print(f"[DEBUG] Abzuspielender Stream: {audio_url}")

            # Starte FFmpeg-Stream
            source = discord.FFmpegPCMAudio(
                audio_url,
                before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                options=f'-vn {self.eq_presets.get(self.equalizer, "")}'
            )


            def after_playing(error):
                if error:
                    print(f"[Fehler beim Abspielen] {error}")
                asyncio.run_coroutine_threadsafe(self.play_next(ctx), self.bot.loop)

            ctx.voice_client.play(source, after=after_playing)
            await ctx.send(f"🎶 Jetzt läuft: **{title}**")
            self.is_playing = True

        except Exception as e:
            print(f"[Fehler bei play_next] {e}")
            await ctx.send(f"⚠️ Fehler beim Laden von {title}. Überspringe...")
            await self.play_next(ctx)

            
            
            
    #play
    @commands.command()
    async def p(self, ctx, url):
        if ctx.voice_client is None:
            await ctx.send("❌ Der Bot ist in keinem Voice-Channel. Bitte verwende zuerst `!join`.")
            return

        ydl_opts = {
            'extract_flat': 'in_playlist',
            'skip_download': True,
            'quiet': True,
            'noplaylist': False
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

                if 'entries' in info:
                    playlist_length = len(info['entries'])
                    if playlist_length > self.HARD_PLAYLIST_LIMIT:
                        await ctx.send(f"🚫 Playlist ist zu groß! Maximal {self.HARD_PLAYLIST_LIMIT} Einträge erlaubt.")
                        return
                    if playlist_length > self.MAX_PLAYLIST_LENGTH:
                        await ctx.send(f"⚠️ Playlist enthält {playlist_length} Songs. Es werden nur die ersten {self.MAX_PLAYLIST_LENGTH} hinzugefügt.")
                    added = 0
                    for entry in info['entries'][:self.MAX_PLAYLIST_LENGTH]:
                        try:
                            entry_url = entry.get('url') or entry.get('webpage_url')
                            entry_title = entry.get('title', 'Unbekannt')
                            if entry_url:
                                self.queue.append((entry_url, entry_title))
                                added += 1
                        except Exception:
                            continue
                    await ctx.send(f"🎵 {added} Titel zur Warteschlange hinzugefügt.")
                else:
                    title = info.get('title', 'Unbekannt')
                    self.queue.append((url, title))
                    await ctx.send(f"➕ Zur Warteschlange hinzugefügt: {title}")
        except Exception as e:
            await ctx.send("❌ Fehler beim Verarbeiten der URL. Bitte überprüfe den Link.")
            return

        if not self.is_playing:
            self.is_playing = True
            await self.play_next(ctx)
            
    #skip
    @commands.command()
    async def s(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await ctx.send("⏭️ Song übersprungen. Spiele den nächsten Titel ...")
        else:
            await ctx.send("⚠️ Es läuft gerade kein Song.")

    #pause
    @commands.command()
    async def x(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("⏸️ Song pausiert.")
            
    #resume
    @commands.command()
    async def resume(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("▶️ Song fortgesetzt.")


    #queue
    @commands.command()
    async def q(self, ctx):
        if not self.queue:
            await ctx.send("🪹 Die Warteschlange ist leer.")
        else:
            message = "📜 Aktuelle Warteschlange:\n"
            for i, (url, title) in enumerate(self.queue, 1):
                if len(message)>=1980:
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
    async def eq(self, ctx, *preset: str):
        if not preset:
            await ctx.send(f"bassboost flat vocalboost superbass")
        presets = ", ".join(self.eq_presets.keys())
        if preset.lower() in self.eq_presets:
            self.equalizer = preset.lower()
            await ctx.send(f"🎚️ Equalizer auf `{preset}` gesetzt.")
        else:
            await ctx.send(f"❌ Unbekanntes Profil. Verfügbare Presets: {presets}")



        
            




bot.run(TOKEN)
