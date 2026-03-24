import asyncio
import discord
from discord.ui import Button, View


class MusicControlView(View):
    """Discord-UI-Buttons, die an jede 'Jetzt läuft'-Nachricht angehängt werden.

    timeout=None bedeutet, dass die Buttons nie ablaufen – auch nach einem
    Bot-Neustart sind alte Nachrichten theoretisch noch klickbar, solange
    der Bot läuft (die View-Instanz ist dann allerdings weg).
    """

    def __init__(self, music_cog, ctx):
        super().__init__(timeout=None)
        self.music_cog = music_cog
        self.ctx = ctx

    @discord.ui.button(label="⏸️ Pause", style=discord.ButtonStyle.primary)
    async def pause(self, interaction: discord.Interaction, button: Button):
        if self.ctx.voice_client and self.ctx.voice_client.is_playing():
            self.ctx.voice_client.pause()
            # is_playing syncen, damit !resume und !p den richtigen Zustand sehen.
            self.music_cog.is_playing = False
        await interaction.response.defer()

    @discord.ui.button(label="▶️ Fortsetzen", style=discord.ButtonStyle.success)
    async def resume(self, interaction: discord.Interaction, button: Button):
        if self.ctx.voice_client and self.ctx.voice_client.is_paused():
            self.ctx.voice_client.resume()
            self.music_cog.is_playing = True
        await interaction.response.defer()

    @discord.ui.button(label="⏭️ Überspringen", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: Button):
        # stop() löst den after_playing-Callback aus, der den nächsten Track startet.
        if self.ctx.voice_client and self.ctx.voice_client.is_playing():
            self.ctx.voice_client.stop()
        await interaction.response.defer()

    @discord.ui.button(label="🔁 Autoplay", style=discord.ButtonStyle.danger)
    async def autoplay_toggle(self, interaction: discord.Interaction, button: Button):
        """Schaltet Autoplay an oder aus.

        Wenn Autoplay aktiviert wird und gerade nichts läuft (leere Queue, Bot idle),
        wird sofort ein Song gesucht und gestartet – kein manuelles !p nötig.
        """
        self.music_cog.autoplay_enabled = not self.music_cog.autoplay_enabled
        status = "aktiviert" if self.music_cog.autoplay_enabled else "deaktiviert"
        await interaction.response.send_message(f"🔁 Autoplay {status}.", ephemeral=True)

        # Sofort loslegen, wenn Autoplay gerade eingeschaltet wurde und die Queue leer ist.
        if self.music_cog.autoplay_enabled and not self.music_cog.is_playing and not self.music_cog.queue:
            asyncio.create_task(self.music_cog.autoplay(self.ctx))
