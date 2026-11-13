from collections import deque

import discord
from discord.ui import Button, Select, View

from utils.checks import same_voice_allowed
from utils.i18n import t

# Discord-Nachrichtenlimit ist 2000 Zeichen; Budget für die reine Songliste
# bewusst niedriger, damit Titel-Zeile + Footer garantiert noch reinpassen.
LIST_CHAR_BUDGET = 1500
# Ein Discord-Auswahlmenü fasst höchstens 25 Einträge – eine Seite darf nicht
# mehr Songs zeigen, als sich darin auswählen lassen.
PAGE_MAX_ITEMS = 25


class QueueView(View):
    """!q-Ansicht: Textliste mit Blätter-Buttons.

    Mit music_cog zusätzlich ein Auswahlmenü für die Songs der aktuellen Seite
    samt „Entfernen" und „Als Nächstes" (= !remove / !move). Die Ansicht ist
    ein Schnappschuss: jede Aktion sucht den gewählten Song in der echten
    Queue und lädt die Liste danach neu.
    """

    def __init__(self, queue_snapshot: list, current_track, loop_mode, autoplay_next_title=None,
                 music_cog=None, ctx=None):
        super().__init__(timeout=60)
        self.items = queue_snapshot
        self.current_track = current_track
        self.loop_mode = loop_mode
        # Titel des vorgeladenen Autoplay-Kandidaten → eigene Vorschau-Zeile
        self.autoplay_next_title = autoplay_next_title
        self.music_cog = music_cog
        self.ctx = ctx
        self.message = None      # setzt der Aufrufer nach dem Senden (für on_timeout)
        self.selected = None     # Index des gewählten Songs in self.items
        self.page = 0
        self._pages = self._paginate()
        self._first_index = self._page_starts()

        self.select = Select(placeholder=t("embed.queue_select"), row=1)
        self.select.callback = self._on_select
        self.remove_btn = Button(label=t("button.queue_remove"), style=discord.ButtonStyle.danger, row=2)
        self.remove_btn.callback = self._on_remove
        self.move_btn = Button(label=t("button.queue_move"), style=discord.ButtonStyle.secondary, row=2)
        self.move_btn.callback = self._on_move
        self._update_buttons()

    def _paginate(self) -> list:
        """Teilt die nummerierte Songliste so in Seiten, dass jede Seite unter
        LIST_CHAR_BUDGET Zeichen bleibt – Seitenumbrüche folgen also der
        tatsächlichen Textlänge statt einer festen Songanzahl."""
        pages = []
        current_lines = []
        current_len = 0
        for i, item in enumerate(self.items):
            line = f"{i + 1}. {item[1]}"
            line_len = len(line) + 1  # +1 für den Zeilenumbruch
            if current_lines and (current_len + line_len > LIST_CHAR_BUDGET
                                  or len(current_lines) >= PAGE_MAX_ITEMS):
                pages.append(current_lines)
                current_lines = []
                current_len = 0
            current_lines.append(line)
            current_len += line_len
        if current_lines or not pages:
            pages.append(current_lines)
        return pages

    def _page_starts(self) -> list:
        """Index des ersten Songs jeder Seite in self.items."""
        starts, n = [], 0
        for lines in self._pages:
            starts.append(n)
            n += len(lines)
        return starts

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= len(self._pages) - 1
        self._update_controls()

    def _update_controls(self):
        """Auswahlmenü + Aktions-Buttons für die aktuelle Seite neu aufbauen."""
        for item in (self.select, self.remove_btn, self.move_btn):
            self.remove_item(item)
        count = len(self._pages[self.page])
        if self.music_cog is None or count == 0:
            return   # ohne Cog (reine Anzeige) oder leere Seite: kein Menü
        start = self._first_index[self.page]
        if self.selected is not None and not start <= self.selected < start + count:
            self.selected = None   # Auswahl gilt nur auf ihrer Seite
        self.select.options = [
            discord.SelectOption(
                label=f"{i + 1}. {self.items[i][1]}"[:100], value=str(i), default=i == self.selected,
            )
            for i in range(start, start + count)
        ]
        self.remove_btn.disabled = self.move_btn.disabled = self.selected is None
        for item in (self.select, self.remove_btn, self.move_btn):
            self.add_item(item)

    def _reload(self):
        """Schnappschuss nach einer Aktion aus der echten Queue neu ziehen."""
        self.items, self.autoplay_next_title = self.music_cog._queue_snapshot()
        self.current_track = self.music_cog.current_track
        self.loop_mode = self.music_cog.loop_mode
        self.selected = None
        self._pages = self._paginate()
        self._first_index = self._page_starts()
        self.page = min(self.page, len(self._pages) - 1)
        self._update_buttons()

    def _find_live(self):
        """Position des gewählten Songs in der echten Queue – None, wenn er
        inzwischen gespielt oder entfernt wurde. Bevorzugt dieselbe Position,
        sonst das erste Vorkommen (die Queue kann sich verschoben haben)."""
        if self.selected is None or self.selected >= len(self.items):
            return None
        entry = tuple(self.items[self.selected][:2])
        live = list(self.music_cog.queue)
        if self.selected < len(live) and tuple(live[self.selected]) == entry:
            return self.selected
        return live.index(entry) if entry in live else None

    async def _on_select(self, interaction: discord.Interaction):
        self.selected = int(self.select.values[0])
        self._update_controls()
        await interaction.response.edit_message(view=self)

    async def _apply(self, interaction: discord.Interaction, to_front: bool):
        """Gewählten Song entfernen oder nach vorne ziehen, dann Liste neu laden."""
        cog = self.music_cog
        if not await same_voice_allowed(interaction.client, interaction.user, self.ctx.voice_client):
            await interaction.response.send_message(t("error.same_voice_required"), ephemeral=True)
            return
        idx = self._find_live()
        feedback = t("error.queue_item_gone")
        if idx is not None:
            live = list(cog.queue)
            entry = live.pop(idx)
            if to_front:
                live.insert(0, entry)
                feedback = t("status.moved_to_front", title=entry[1])
            else:
                feedback = t("status.removed", title=entry[1])
            cog.queue = deque(live)
            cog._restart_prefetch()   # alter Prefetch zielt auf die alte Reihenfolge
        self._reload()
        await interaction.response.edit_message(content=self.build_content(), view=self)
        await interaction.followup.send(feedback, ephemeral=True)

    async def _on_remove(self, interaction: discord.Interaction):
        await self._apply(interaction, to_front=False)

    async def _on_move(self, interaction: discord.Interaction):
        await self._apply(interaction, to_front=True)

    def _loop_text(self) -> str:
        if self.loop_mode == "song":
            return t("embed.loop_song")
        if self.loop_mode == "queue":
            return t("embed.loop_queue")
        return t("embed.loop_off")

    def _footer_text(self) -> str:
        total = len(self.items)
        total_items = total + (1 if self.current_track else 0)
        loop_text = self._loop_text()

        known_secs = 0
        known_count = 0
        if self.current_track and len(self.current_track) >= 3 and self.current_track[2]:
            known_secs += self.current_track[2]
            known_count += 1
        for item in self.items:
            dur = item[2] if len(item) >= 3 else None
            if dur:
                known_secs += dur
                known_count += 1

        if known_count > 0:
            m, s = divmod(int(known_secs), 60)
            h, m = divmod(m, 60)
            dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            if known_count < total_items:
                # Nur gecachte Dauern summiert → Untergrenze, Rest ausgewiesen.
                return t(
                    "embed.queue_footer_duration_partial",
                    total=total, duration=dur_str,
                    missing=total_items - known_count, loop=loop_text,
                )
            return t("embed.queue_footer_duration", total=total, duration=dur_str, loop=loop_text)
        return t("embed.queue_footer", total=total, loop=loop_text)

    def build_content(self) -> str:
        total_pages = len(self._pages)
        lines = [t("embed.queue_title", page=self.page + 1, pages=total_pages)]

        if self.current_track:
            _, title, *_ = self.current_track
            lines.append(f"{t('embed.queue_now')}: {title}")

        lines.append("")
        lines.append(f"**{t('embed.queue_list')}**")
        page_lines = self._pages[self.page]
        if page_lines:
            lines.extend(page_lines)
        else:
            lines.append(t("embed.queue_empty"))

        if self.autoplay_next_title:
            lines.append("")
            lines.append(t("embed.queue_autoplay_next", title=self.autoplay_next_title))

        lines.append("")
        lines.append(f"*{self._footer_text()}*")
        return "\n".join(lines)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(content=self.build_content(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(content=self.build_content(), view=self)

    async def on_timeout(self):
        self.prev_btn.disabled = True
        self.next_btn.disabled = True
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            # Abgelaufene Bedienelemente entfernen statt „Interaktion fehlgeschlagen".
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass
