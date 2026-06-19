import asyncio
from collections import deque
import discord
from discord.ui import Button, View
from utils.logger import logger
from utils.i18n import t


class MusicControlView(View):
    """Discord-UI-Buttons, die an jede 'Jetzt läuft'-Nachricht angehängt werden.

    timeout=None bedeutet, dass die Buttons nie ablaufen – auch nach einem
    Bot-Neustart sind alte Nachrichten theoretisch noch klickbar, solange
    der Bot läuft (die View-Instanz ist dann allerdings weg).
    """

    def __init__(self, music_cog, ctx, song=None):
        super().__init__(timeout=None)
        self.music_cog = music_cog
        self.ctx = ctx
        self.song = song  # (url, title) des Songs zu dem diese View gehört

    @discord.ui.button(label=t("button.pause"), style=discord.ButtonStyle.primary)
    async def pause(self, interaction: discord.Interaction, button: Button):
        if self.ctx.voice_client and self.ctx.voice_client.is_playing():
            self.ctx.voice_client.pause()
            # is_playing syncen, damit !resume und !p den richtigen Zustand sehen.
            self.music_cog.is_playing = False
            await interaction.response.send_message(t("status.paused_eph"), ephemeral=True)
        else:
            await interaction.response.send_message(t("error.no_active_song"), ephemeral=True)

    @discord.ui.button(label=t("button.resume"), style=discord.ButtonStyle.success)
    async def resume(self, interaction: discord.Interaction, button: Button):
        if self.ctx.voice_client and self.ctx.voice_client.is_paused():
            self.ctx.voice_client.resume()
            self.music_cog.is_playing = True
            await interaction.response.send_message(t("status.resumed_eph"), ephemeral=True)
        elif not self.music_cog.is_playing and self.music_cog.queue and not self.music_cog.is_radio:
            # Queue vorhanden, aber nichts läuft → starten
            self.music_cog.is_playing = True
            await interaction.response.send_message(t("status.playback_started"), ephemeral=True)
            await self.music_cog.play_next(self.ctx)
        elif not self.music_cog.is_playing and not self.music_cog.queue and not self.music_cog.is_radio and self.song:
            # Queue leer, aber dieser Button kennt den Song → vorne einreihen und starten
            url, title = self.song
            self.music_cog.queue.appendleft((url, title))
            self.music_cog.is_playing = True
            await interaction.response.send_message(t("status.playback_started"), ephemeral=True)
            await self.music_cog.play_next(self.ctx)
        elif not self.music_cog.is_playing and self.music_cog.autoplay_enabled and not self.music_cog.is_radio:
            # Queue leer, aber Autoplay ist an → sofort loslegen
            await interaction.response.send_message(t("status.autoplay_starting"), ephemeral=True)
            asyncio.create_task(self.music_cog.autoplay(self.ctx))
        else:
            await interaction.response.send_message(t("error.no_song_to_resume"), ephemeral=True)

    @discord.ui.button(label=t("button.skip"), style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: Button):
        if self.music_cog.is_radio:
            self.music_cog._stop_radio()
            if self.ctx.voice_client and self.ctx.voice_client.is_playing():
                self.ctx.voice_client.stop()
            await interaction.response.send_message(t("status.radio_stopped"), ephemeral=True)
            if self.music_cog.queue:
                self.music_cog.is_playing = True
                asyncio.create_task(self.music_cog.play_next(self.ctx))
            return
        # stop() löst den after_playing-Callback aus, der den nächsten Track startet.
        if self.ctx.voice_client and self.ctx.voice_client.is_playing():
            self.ctx.voice_client.stop()
            await interaction.response.send_message(t("status.skipped_eph"), ephemeral=True)
        else:
            await interaction.response.send_message(t("error.no_active_song"), ephemeral=True)

    @discord.ui.button(label=t("button.autoplay"), style=discord.ButtonStyle.danger)
    async def autoplay_toggle(self, interaction: discord.Interaction, button: Button):
        """Schaltet Autoplay an oder aus.

        Wenn Autoplay aktiviert wird und gerade nichts läuft (leere Queue, Bot idle),
        wird sofort ein Song gesucht und gestartet – kein manuelles !p nötig.
        """
        if self.music_cog.is_radio:
            await interaction.response.send_message(t("error.autoplay_radio"), ephemeral=True)
            return
        self.music_cog.autoplay_enabled = not self.music_cog.autoplay_enabled
        status = t("misc.enabled") if self.music_cog.autoplay_enabled else t("misc.disabled")
        logger.info(f"[Autoplay] {status.capitalize()} per Button")
        await interaction.response.send_message(f"🔁 Autoplay {status}.", ephemeral=True)

        # Sofort loslegen, wenn Autoplay gerade eingeschaltet wurde und die Queue leer ist.
        if self.music_cog.autoplay_enabled and not self.music_cog.is_playing and not self.music_cog.queue:
            asyncio.create_task(self.music_cog.autoplay(self.ctx))

    @discord.ui.button(label=t("button.loop"), style=discord.ButtonStyle.secondary)
    async def loop_toggle(self, interaction: discord.Interaction, button: Button):
        """Schaltet den Loop-Modus durch: aus → Song → Queue → aus (wie !loop)."""
        modes = [None, "song", "queue"]
        self.music_cog.loop_mode = modes[(modes.index(self.music_cog.loop_mode) + 1) % len(modes)]
        loop_keys = {None: "status.loop_off", "song": "status.loop_song", "queue": "status.loop_queue"}
        await interaction.response.send_message(t(loop_keys[self.music_cog.loop_mode]), ephemeral=True)


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
        self.base_content = base_content or t("status.playing", title=added_entry.get("title", t("misc.unknown_title")))

        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        for i, entry in enumerate(alternatives):
            button = Button(label=t("misc.option", letter=letters[i]), style=discord.ButtonStyle.secondary)
            button.callback = self._make_callback(entry)
            self.add_item(button)

    def _make_callback(self, entry):
        async def callback(interaction: discord.Interaction):
            url = entry.get("webpage_url") or entry.get("url")
            title = entry.get("title", t("misc.unknown_title"))
            added_title = self.added_entry.get("title", "")

            if self.music_cog.current_track and self.music_cog.current_track[1] == added_title:
                # Song läuft gerade → Alternative vorne einreihen und aktuellen überspringen
                self.music_cog.queue.appendleft((url, title))
                if self.ctx.voice_client and (
                    self.ctx.voice_client.is_playing() or self.ctx.voice_client.is_paused()
                ):
                    self.ctx.voice_client.stop()
                await interaction.response.edit_message(
                    content=t("status.switching_to", title=title), view=None
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
                    # Song wurde bereits aus der Queue gepoppt – play_next löst ihn gerade auf.
                    # Flag setzen damit play_next den resolvedn Track überspringt und stattdessen
                    # die Alternative (jetzt vorne in der Queue) spielt.
                    self.music_cog._skip_resolving = True
                    self.music_cog.queue.appendleft((url, title))
                    # Falls play() bereits gestartet hat bevor current_track gesetzt wurde
                    if self.ctx.voice_client and (
                        self.ctx.voice_client.is_playing() or self.ctx.voice_client.is_paused()
                    ):
                        self.ctx.voice_client.stop()
                await interaction.response.edit_message(
                    content=t("status.switching_to", title=title), view=None
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
