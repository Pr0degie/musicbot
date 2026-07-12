import discord
from discord.ui import Button, View

from utils.i18n import t

# Discord-Nachrichtenlimit ist 2000 Zeichen; Budget für die reine Songliste
# bewusst niedriger, damit Titel-Zeile + Footer garantiert noch reinpassen.
LIST_CHAR_BUDGET = 1500


class QueueView(View):
    def __init__(self, queue_snapshot: list, current_track, loop_mode):
        super().__init__(timeout=60)
        self.items = queue_snapshot
        self.current_track = current_track
        self.loop_mode = loop_mode
        self.page = 0
        self._pages = self._paginate()
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
            if current_lines and current_len + line_len > LIST_CHAR_BUDGET:
                pages.append(current_lines)
                current_lines = []
                current_len = 0
            current_lines.append(line)
            current_len += line_len
        if current_lines or not pages:
            pages.append(current_lines)
        return pages

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= len(self._pages) - 1

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
