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


class SearchResultView(View):
    """Zeigt bis zu 3 Suchergebnisse als Buttons an und lässt den User auswählen.

    Läuft nach 30 Sekunden ab falls niemand antwortet.
    """

    def __init__(self, entries, music_cog, ctx):
        super().__init__(timeout=30)
        self.music_cog = music_cog
        self.ctx = ctx
        self.message = None  # Wird nach dem Senden gesetzt, damit on_timeout die Nachricht editieren kann

        for i, entry in enumerate(entries, 1):
            button = Button(label=str(i), style=discord.ButtonStyle.primary)
            button.callback = self._make_callback(entry)
            self.add_item(button)

        cancel = Button(label="✖ Abbrechen", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(cancel)

    def _make_callback(self, entry):
        async def callback(interaction: discord.Interaction):
            url = entry.get("webpage_url") or entry.get("url")
            title = entry.get("title", "Unbekannter Titel")

            # Duplikat-Prüfung direkt beim Auswählen aus den Suchergebnissen
            dup_pos = next((i + 1 for i, (_, t) in enumerate(self.music_cog.queue) if t == title), None)
            if dup_pos:
                await interaction.response.edit_message(
                    content=f"⚠️ **{title}** ist bereits in der Queue (Position {dup_pos}). Trotzdem hinzugefügt.",
                    view=None,
                )
            else:
                await interaction.response.edit_message(
                    content=f"🎶 Hinzugefügt: **{title}**", view=None
                )

            self.music_cog.queue.append((url, title))
            if not self.music_cog.is_playing:
                self.music_cog.is_playing = True
                await self.music_cog.play_next(self.ctx)
            self.stop()

        return callback

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="❌ Suche abgebrochen.", view=None)
        self.stop()

    async def on_timeout(self):
        # Nachricht aufräumen wenn der User zu langsam war
        if self.message:
            try:
                await self.message.edit(content="⏱️ Suche abgelaufen.", view=None)
            except Exception:
                pass
