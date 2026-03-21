import asyncio
import discord
from discord.ui import Button, View


class MusicControlView(View):
    def __init__(self, music_cog, ctx):
        super().__init__(timeout=None)
        self.music_cog = music_cog
        self.ctx = ctx

    @discord.ui.button(label="⏸️ Pause", style=discord.ButtonStyle.primary)
    async def pause(self, interaction: discord.Interaction, button: Button):
        if self.ctx.voice_client and self.ctx.voice_client.is_playing():
            self.ctx.voice_client.pause()
        await interaction.response.defer()

    @discord.ui.button(label="▶️ Fortsetzen", style=discord.ButtonStyle.success)
    async def resume(self, interaction: discord.Interaction, button: Button):
        if self.ctx.voice_client and self.ctx.voice_client.is_paused():
            self.ctx.voice_client.resume()
        await interaction.response.defer()

    @discord.ui.button(label="⏭️ Überspringen", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: Button):
        if self.ctx.voice_client and self.ctx.voice_client.is_playing():
            self.ctx.voice_client.stop()
        await interaction.response.defer()

    @discord.ui.button(label="🔁 Autoplay", style=discord.ButtonStyle.danger)
    async def autoplay(self, interaction: discord.Interaction, button: Button):
        # BUG FIX #4: autoplay ist ein Coroutine und muss mit asyncio.create_task() aufgerufen werden
        await interaction.response.defer()
        asyncio.create_task(self.music_cog.autoplay(self.ctx))
