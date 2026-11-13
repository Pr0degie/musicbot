import asyncio
import uuid
from collections import deque
import discord
from discord.ui import ActionRow, Button, Container, LayoutView, Section, TextDisplay, Thumbnail, View
from utils.logger import logger
from utils.voice import stop_playback
from utils.checks import same_voice_allowed
from utils.i18n import t

# Prozessweiter Marker in jeder Button-custom_id: unterscheidet Buttons dieses
# Bot-Laufs von denen früherer Läufe. Nach einem Neustart tragen alte
# Nachrichten eine fremde BOOT_ID → StaleControlsFallback greift; Buttons des
# laufenden Prozesses matchen sein Template nie (negative lookahead unten).
BOOT_ID = uuid.uuid4().hex[:8]
_CUSTOM_ID_PREFIX = "musicctl"
# Feste Komponenten-ID der Titelzeile: StaleControlsFallback findet darüber
# in alten Karten den Titel für den Rückbau.
NP_HEADING_ID = 1
_KEEP = object()   # Sentinel: „Als Nächstes" unverändert lassen


class MusicControlView(LayoutView):
    """Now-Playing-Karte als Components-V2-Layout samt Steuer-Buttons.

    Aufbau (ein Container in Akzentfarbe):
      Section  [Titel, Fortschrittsbalken]        → Accessory: grauer Button mit rotem ❌ (= !clear)
      Section  [Details, Als Nächstes, Uploader]  → Accessory: Thumbnail (falls vorhanden)
      ActionRow [Zurück, Pause/Fortsetzen, Skip, Autoplay, Loop]

    Discord erlaubt in Embeds keine Buttons – nur das V2-Layout kann den ❌
    oben rechts in die Karte setzen. Konsequenz: Nachrichten mit V2-Flag
    tragen nie content/embeds, und das Flag lässt sich nicht mehr entfernen.
    Rückbau daher über retired_card(), nie über content=/embed=/view=None.

    Die Buttons spiegeln den Zustand (Pause ↔ Fortsetzen, Autoplay/Loop grün =
    an): sync_state() gleicht sie an den Cog an und meldet, ob sich etwas
    geändert hat – der _progress_loop schickt die Karte dann mit.

    timeout=None bedeutet, dass die Buttons nie ablaufen. Klicks auf
    Nachrichten aus einem früheren Lauf beantwortet StaleControlsFallback.
    """

    def __init__(self, music_cog, ctx, song=None, *, heading="🎵", body=None,
                 details=None, footer=None, up_next=None, thumbnail=None, accent=0x1db954):
        super().__init__(timeout=None)
        self.music_cog = music_cog
        self.ctx = ctx
        self.song = song  # (url, title) des Songs zu dem diese View gehört
        # custom_ids: eindeutig pro Nachricht (nonce) und pro Bot-Lauf (BOOT_ID).
        nonce = uuid.uuid4().hex[:8]

        def make(action, callback, **kwargs):
            button = Button(custom_id=f"{_CUSTOM_ID_PREFIX}:{BOOT_ID}:{nonce}:{action}", **kwargs)

            async def guarded(interaction: discord.Interaction):
                # REQUIRE_SAME_VOICE gilt für jeden Button der Karte, nicht nur
                # für die getippten Befehle (Default aus, Owner immer durch).
                if not await same_voice_allowed(interaction.client, interaction.user, self.ctx.voice_client):
                    await interaction.response.send_message(t("error.same_voice_required"), ephemeral=True)
                    return
                await callback(interaction)

            button.callback = guarded
            return button

        S = discord.ButtonStyle
        self.back = make("back", self._on_back, label=t("button.back"), style=S.secondary)
        # Ein Button für Pause und Fortsetzen; Beschriftung/Farbe setzt sync_state().
        self.pause = make("pause", self._on_pause_toggle, label=t("button.pause"), style=S.primary)
        self.skip = make("skip", self._on_skip, label=t("button.skip"), style=S.secondary)
        self.autoplay_toggle = make("autoplay", self._on_autoplay, label=t("button.autoplay"), style=S.secondary)
        self.loop_toggle = make("loop", self._on_loop, label=t("button.loop"), style=S.secondary)
        # Grau mit rotem ❌: eine rot gefüllte Fläche neben dem Titel wirkt zu wuchtig.
        self.clear = make("clear", self._on_clear, emoji="❌", style=S.secondary)

        self._heading = TextDisplay(heading, id=NP_HEADING_ID)
        self._body = TextDisplay(body) if body else None
        self._top = Section(self._heading, accessory=self.clear)
        if self._body:
            self._top.add_item(self._body)
        container = Container(self._top, accent_colour=accent)
        self._details_base, self._footer, self._up_next = details, footer, up_next
        self._details = None
        if details:
            self._details = TextDisplay(self._details_text())
            # Nur http(s): ein Embed ignoriert eine unbrauchbare Thumbnail-URL still,
            # das V2-Layout lehnt die ganze Nachricht ab (400) – dann lieber ohne Bild.
            if isinstance(thumbnail, str) and thumbnail.startswith(("http://", "https://")):
                container.add_item(Section(self._details, accessory=Thumbnail(thumbnail)))
            else:
                container.add_item(self._details)
        container.add_item(ActionRow(
            self.back, self.pause, self.skip, self.autoplay_toggle, self.loop_toggle,
        ))
        self.add_item(container)
        self.sync_state()

    @property
    def body(self):
        return self._body.content if self._body else None

    def set_body(self, text):
        """Fortschrittsbalken-Zeile setzen; danach msg.edit(view=self)."""
        if self._body is None:
            self._body = TextDisplay(text)
            self._top.add_item(self._body)
        else:
            self._body.content = text

    def _details_text(self):
        """Details-Zeile + „Als Nächstes" + klein gesetzter Uploader."""
        lines = [self._details_base]
        if self._up_next:
            title = discord.utils.escape_markdown(self._up_next)
            if len(title) > 80:
                title = title[:79] + "…"
            lines.append(f"**{t('embed.up_next')}** {title}")
        if self._footer:
            lines.append(f"-# {self._footer}")
        return "\n".join(lines)

    def sync_state(self, up_next=_KEEP) -> bool:
        """Gleicht Buttons und „Als Nächstes"-Zeile an den Cog-Zustand an.

        Gibt True zurück, wenn sich etwas geändert hat – nur dann lohnt es sich,
        die Karte neu zu schicken (msg.edit(view=self))."""
        S = discord.ButtonStyle
        cog, vc = self.music_cog, self.ctx.voice_client
        playing = bool(vc and vc.is_playing())
        wanted = (
            (self.pause, t("button.pause") if playing else t("button.resume"),
             S.primary if playing else S.success),
            (self.autoplay_toggle, t("button.autoplay"), S.success if cog.autoplay_enabled else S.secondary),
            (self.loop_toggle, t("button.loop"), S.success if cog.loop_mode == "song" else S.secondary),
        )
        changed = False
        for button, label, style in wanted:
            if button.label != label or button.style != style:
                button.label, button.style = label, style
                changed = True
        if up_next is not _KEEP:
            self._up_next = up_next
        if self._details is not None:
            text = self._details_text()
            if self._details.content != text:
                self._details.content = text
                changed = True
        return changed

    async def _push_state(self, interaction):
        """Nach einem Klick die Karte sofort nachziehen (Farben, Beschriftung).

        Nur wenn dies noch die aktive Karte ist: hat der Klick einen neuen Song
        gestartet, ist diese Nachricht schon zur Textzeile zurückgebaut – ein
        Edit mit der View würde die alte Karte samt Buttons wiederbeleben."""
        if not self.sync_state():
            return
        try:
            active = self.music_cog.now_playing_msg
            if active is not None and active.id == interaction.message.id:
                await interaction.message.edit(view=self)
        except Exception:
            pass   # Karte weg oder Rate-Limit – der nächste Progress-Tick holt es nach

    async def _on_pause_toggle(self, interaction: discord.Interaction):
        vc = self.ctx.voice_client
        if vc and vc.is_playing():
            await self._pause(interaction)
        else:
            await self._resume(interaction)
        await self._push_state(interaction)

    async def _on_back(self, interaction: discord.Interaction):
        """⏮: vorherigen Song noch einmal spielen; der laufende rückt direkt dahinter."""
        cog, vc = self.music_cog, self.ctx.voice_client
        was_active = bool(vc and (vc.is_playing() or vc.is_paused()))
        title = None if cog.is_radio else cog._queue_previous(vc)
        if title is None:
            await interaction.response.send_message(t("error.no_last_song"), ephemeral=True)
            return
        await interaction.response.send_message(t("status.back_eph", title=title), ephemeral=True)
        if not was_active:
            # Lief etwas, startet after_playing den Song – ein zweiter Start hier
            # (z. B. aus der Pause heraus) würde zwei Songs gleichzeitig anstoßen.
            await _start_if_idle(cog, self.ctx)

    async def _pause(self, interaction: discord.Interaction):
        if self.ctx.voice_client and self.ctx.voice_client.is_playing():
            self.ctx.voice_client.pause()
            self.music_cog._mark_paused()   # Fortschrittsbalken einfrieren
            # is_playing syncen, damit !resume und !p den richtigen Zustand sehen.
            self.music_cog.is_playing = False
            # Position ist durch _mark_paused eingefroren; ohne track_start_time
            # (z. B. Radio) bleibt es bei der Meldung ohne Position.
            elapsed = self.music_cog._elapsed_seconds()
            if elapsed is None:
                msg = t("status.paused_eph")
            else:
                msg = t("status.paused_at_eph", position=f"{int(elapsed) // 60}:{int(elapsed) % 60:02d}")
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.response.send_message(t("error.no_active_song"), ephemeral=True)

    async def _resume(self, interaction: discord.Interaction):
        if self.ctx.voice_client and self.ctx.voice_client.is_paused():
            self.ctx.voice_client.resume()
            self.music_cog._mark_resumed()   # Pausendauer einrechnen, Balken läuft ohne Sprung weiter
            self.music_cog.is_playing = True
            # Pause hatte _stopped_by_user gesetzt – ohne Reset blieben Autoplay,
            # Progressive-Resume und Watchdog bis zum Songende unterdrückt.
            self.music_cog._stopped_by_user = False
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

    async def _on_skip(self, interaction: discord.Interaction):
        if self.music_cog.is_radio:
            self.music_cog._stop_radio()
            if self.ctx.voice_client and self.ctx.voice_client.is_playing():
                stop_playback(self.ctx.voice_client)
            await interaction.response.send_message(t("status.radio_stopped"), ephemeral=True)
            if self.music_cog.queue:
                self.music_cog.is_playing = True
                asyncio.create_task(self.music_cog.play_next(self.ctx))
            return
        # stop() löst den after_playing-Callback aus, der den nächsten Track startet.
        if self.ctx.voice_client and self.ctx.voice_client.is_playing():
            self.music_cog._stop_for_advance(self.ctx.voice_client)
            await interaction.response.send_message(t("status.skipped_eph"), ephemeral=True)
        else:
            await interaction.response.send_message(t("error.no_active_song"), ephemeral=True)

    async def _on_autoplay(self, interaction: discord.Interaction):
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
        await self._push_state(interaction)

        # Sofort loslegen, wenn Autoplay gerade eingeschaltet wurde und die Queue leer ist.
        if self.music_cog.autoplay_enabled and not self.music_cog.is_playing and not self.music_cog.queue:
            asyncio.create_task(self.music_cog.autoplay(self.ctx))

    async def _on_loop(self, interaction: discord.Interaction):
        """Schaltet den Loop-Modus für den aktuellen Song um: aus ↔ Song."""
        self.music_cog.loop_mode = "song" if self.music_cog.loop_mode != "song" else None
        loop_keys = {None: "status.loop_off", "song": "status.loop_song"}
        await interaction.response.send_message(t(loop_keys[self.music_cog.loop_mode]), ephemeral=True)
        await self._push_state(interaction)

    async def _on_clear(self, interaction: discord.Interaction):
        """❌ oben rechts: dasselbe wie !clear."""
        snapshot = self.music_cog._clear_all(self.ctx.voice_client)
        logger.info(f"[Queue] Geleert per ❌-Button von {interaction.user}")
        if snapshot is None:
            await interaction.response.send_message(t("status.cleared"))
            return
        undo = UndoClearView(self.music_cog, self.ctx, snapshot)
        await interaction.response.send_message(t("status.cleared"), view=undo)
        try:
            undo.message = await interaction.original_response()
        except Exception:
            pass   # ohne Nachricht bleibt der Button nach dem Timeout einfach tot


async def _start_if_idle(cog, ctx):
    """Wiedergabe anstoßen, wenn Songs in der Queue liegen, aber nichts läuft."""
    vc = ctx.voice_client
    if cog.is_playing or cog.is_radio or not cog.queue:
        return
    if vc and (vc.is_playing() or vc.is_paused()):
        return
    cog.is_playing = True
    await cog.play_next(ctx)


class UndoClearView(View):
    """„Rückgängig" unter der Geleert-Meldung (30 s): stellt Queue, laufenden
    Song und Loop-Modus aus dem Snapshot von _clear_all wieder her."""

    def __init__(self, music_cog, ctx, snapshot):
        super().__init__(timeout=30)
        self.music_cog = music_cog
        self.ctx = ctx
        self.snapshot = snapshot
        self.message = None   # setzt der Aufrufer nach dem Senden

    @discord.ui.button(label=t("button.undo"), style=discord.ButtonStyle.secondary)
    async def undo(self, interaction: discord.Interaction, button: Button):
        if not await same_voice_allowed(interaction.client, interaction.user, self.ctx.voice_client):
            await interaction.response.send_message(t("error.same_voice_required"), ephemeral=True)
            return
        self.stop()
        count = self.music_cog._restore_cleared(self.snapshot)
        logger.info(f"[Queue] Leeren rückgängig gemacht von {interaction.user} ({count} Songs)")
        await interaction.response.edit_message(content=t("status.clear_undone", count=count), view=None)
        await _start_if_idle(self.music_cog, self.ctx)

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)   # normale Nachricht mit Text → view=None ist ok
            except discord.HTTPException:
                pass


def retired_card(title):
    """Text-only-Layout für eine abgelöste Now-Playing-Karte: `🎶 Titel`, keine Buttons.

    V2-Nachrichten lassen sich nicht auf content zurückbauen (Flag bleibt), und
    leer darf die Nachricht nie werden (400) – also ein einzelnes TextDisplay.
    """
    view = LayoutView(timeout=None)
    view.add_item(TextDisplay(f"🎶 {title or t('misc.unknown_title')}"))
    return view


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
                # Startet gleich → Download sofort anstoßen (wie der !p-Erstreffer)
                self.music_cog.dl.prime_first_hit(url, title)
                if self.ctx.voice_client and (
                    self.ctx.voice_client.is_playing() or self.ctx.voice_client.is_paused()
                ):
                    # Über _stop_for_advance, nicht roh: sonst hielte after_playing den
                    # Stopp einer wachsenden Datei für ein vorzeitiges Dateiende und
                    # reihte den alten Song wieder VOR die Alternative.
                    self.music_cog._stop_for_advance(self.ctx.voice_client)
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
                if replaced:
                    # Queue umgebaut → Prefetch auf die neue URL umlenken
                    self.music_cog._restart_prefetch()
                if not replaced:
                    # Song wurde bereits aus der Queue gepoppt – play_next löst ihn gerade auf.
                    # Flag setzen damit play_next den resolvedn Track überspringt und stattdessen
                    # die Alternative (jetzt vorne in der Queue) spielt.
                    self.music_cog._skip_resolving = True
                    self.music_cog.queue.appendleft((url, title))
                    # Startet gleich → Download sofort anstoßen (wie der !p-Erstreffer)
                    self.music_cog.dl.prime_first_hit(url, title)
                    # Falls play() bereits gestartet hat bevor current_track gesetzt wurde
                    if self.ctx.voice_client and (
                        self.ctx.voice_client.is_playing() or self.ctx.voice_client.is_paused()
                    ):
                        self.music_cog._stop_for_advance(self.ctx.voice_client)
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


class StaleControlsFallback(
    discord.ui.DynamicItem[Button],
    template=rf"{_CUSTOM_ID_PREFIX}:(?!{BOOT_ID}:)[0-9a-f]+:[0-9a-f]+:\w+",
):
    """Fängt Klicks auf Now-Playing-Buttons aus früheren Bot-Läufen ab.

    Wird in setup_hook via bot.add_dynamic_items() registriert. discord.py
    dispatcht Dynamic Items zusätzlich zu Live-Views (nicht nur als Fallback!),
    deshalb schließt das Template die BOOT_ID des laufenden Prozesses per
    negative lookahead aus – ein Klick auf eine lebende View kann hier nie
    landen. Kein State-Rekonstruktionsversuch: nur ephemere Erklärung + tote
    Buttons von der alten Nachricht entfernen.
    """

    def __init__(self, custom_id: str):
        super().__init__(Button(label="…", custom_id=custom_id))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match):
        return cls(item.custom_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(t("error.stale_controls"), ephemeral=True)
        msg = interaction.message
        try:
            if msg.flags.components_v2:
                # V2-Karte: view=None ließe eine leere Nachricht zurück (400).
                await msg.edit(view=retired_card(_stale_card_title(msg.components)))
            else:
                await msg.edit(view=None)   # Embed-Karte aus der Zeit vor V2
        except (discord.HTTPException, AttributeError):
            pass


def _stale_card_title(components):
    """Titel aus der Titelzeile (NP_HEADING_ID) einer alten V2-Karte holen:
    `### 🎵 [Song – Artist](url)` → `[Song – Artist](url)`. None, wenn keine da."""
    stack = list(components)
    while stack:
        comp = stack.pop(0)
        if getattr(comp, "id", None) == NP_HEADING_ID and hasattr(comp, "content"):
            text = comp.content.lstrip("#").strip()
            rest = text.partition(" ")[2]   # Titelzeile ist immer "<Icon> <Titel>"
            return rest.strip() or None
        stack.extend(getattr(comp, "children", []) or [])
    return None
