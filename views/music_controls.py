import asyncio
from collections import deque
import discord
from discord.ui import Button, View
from utils.logger import logger


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
            await interaction.response.send_message("⏸️ Pausiert.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Kein aktiver Song.", ephemeral=True)

    @discord.ui.button(label="▶️ Fortsetzen", style=discord.ButtonStyle.success)
    async def resume(self, interaction: discord.Interaction, button: Button):
        if self.ctx.voice_client and self.ctx.voice_client.is_paused():
            self.ctx.voice_client.resume()
            self.music_cog.is_playing = True
            await interaction.response.send_message("▶️ Fortgesetzt.", ephemeral=True)
        elif not self.music_cog.is_playing and self.music_cog.queue:
            # Queue vorhanden, aber nichts läuft → starten
            self.music_cog.is_playing = True
            await interaction.response.send_message("▶️ Wiedergabe gestartet.", ephemeral=True)
            await self.music_cog.play_next(self.ctx)
        elif not self.music_cog.is_playing and self.music_cog.autoplay_enabled:
            # Queue leer, aber Autoplay ist an → sofort loslegen
            await interaction.response.send_message("🔁 Autoplay startet...", ephemeral=True)
            asyncio.create_task(self.music_cog.autoplay(self.ctx))
        else:
            await interaction.response.send_message("⚠️ Kein Song zum Fortsetzen.", ephemeral=True)

    @discord.ui.button(label="⏭️ Überspringen", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: Button):
        # stop() löst den after_playing-Callback aus, der den nächsten Track startet.
        if self.ctx.voice_client and self.ctx.voice_client.is_playing():
            self.ctx.voice_client.stop()
            await interaction.response.send_message("⏭️ Übersprungen.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Kein aktiver Song.", ephemeral=True)

    @discord.ui.button(label="🔁 Autoplay", style=discord.ButtonStyle.danger)
    async def autoplay_toggle(self, interaction: discord.Interaction, button: Button):
        """Schaltet Autoplay an oder aus.

        Wenn Autoplay aktiviert wird und gerade nichts läuft (leere Queue, Bot idle),
        wird sofort ein Song gesucht und gestartet – kein manuelles !p nötig.
        """
        self.music_cog.autoplay_enabled = not self.music_cog.autoplay_enabled
        status = "aktiviert" if self.music_cog.autoplay_enabled else "deaktiviert"
        logger.info(f"[Autoplay] {status.capitalize()} per Button")
        await interaction.response.send_message(f"🔁 Autoplay {status}.", ephemeral=True)

        # Sofort loslegen, wenn Autoplay gerade eingeschaltet wurde und die Queue leer ist.
        if self.music_cog.autoplay_enabled and not self.music_cog.is_playing and not self.music_cog.queue:
            asyncio.create_task(self.music_cog.autoplay(self.ctx))


class SearchAutoplayView(View):
    """Zeigt Alternativen zum automatisch gestarteten ersten Suchergebnis.

    Der erste Treffer läuft sofort – diese Buttons erscheinen daneben falls
    er nicht das Richtige war. Nach 30 Sekunden verschwinden sie einfach.
    """

    def __init__(self, added_entry, alternatives, music_cog, ctx, base_content=None):
        super().__init__(timeout=30)
        self.added_entry = added_entry
        self.music_cog = music_cog
        self.ctx = ctx
        self.message = None
        self.base_content = base_content or f"🎶 Spiele: **{added_entry.get('title', 'Unbekannter Titel')}**"

        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        for i, entry in enumerate(alternatives):
            button = Button(label=f"Option {letters[i]}", style=discord.ButtonStyle.secondary)
            button.callback = self._make_callback(entry)
            self.add_item(button)

    def _make_callback(self, entry):
        async def callback(interaction: discord.Interaction):
            url = entry.get("webpage_url") or entry.get("url")
            title = entry.get("title", "Unbekannter Titel")
            added_title = self.added_entry.get("title", "")

            if self.music_cog.current_track and self.music_cog.current_track[1] == added_title:
                # Song läuft gerade → Alternative vorne einreihen und aktuellen überspringen
                self.music_cog.queue.appendleft((url, title))
                if self.ctx.voice_client and (
                    self.ctx.voice_client.is_playing() or self.ctx.voice_client.is_paused()
                ):
                    self.ctx.voice_client.stop()
                await interaction.response.edit_message(
                    content=f"🔀 Wechsle zu: **{title}**", view=None
                )
            else:
                # Song wartet noch in der Queue → direkt ersetzen
                queue_list = list(self.music_cog.queue)
                replaced = False
                for i, (_, q_title) in enumerate(queue_list):
                    if q_title == added_title:
                        queue_list[i] = (url, title)
                        replaced = True
                        break
                self.music_cog.queue = deque(queue_list)
                if not replaced:
                    # Zur Sicherheit vorne einreihen falls der Song nicht mehr in der Queue ist
                    self.music_cog.queue.appendleft((url, title))
                await interaction.response.edit_message(
                    content=f"🔀 Ersetzt durch: **{title}**", view=None
                )
                if not self.music_cog.is_playing:
                    self.music_cog.is_playing = True
                    await self.music_cog.play_next(self.ctx)
            self.stop()

        return callback

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content=self.base_content, view=None)
            except discord.HTTPException:
                pass
