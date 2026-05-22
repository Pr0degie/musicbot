import discord
from discord.ui import Button, View

from utils.i18n import t


class QueueView(View):
    PER_PAGE = 15

    def __init__(self, queue_snapshot: list, current_track, loop_mode):
        super().__init__(timeout=60)
        self.items = queue_snapshot
        self.current_track = current_track
        self.loop_mode = loop_mode
        self.page = 0
        self._update_buttons()

    def _total_pages(self) -> int:
        return max(1, -(-len(self.items) // self.PER_PAGE))

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self._total_pages() - 1

    def build_embed(self) -> discord.Embed:
        total = len(self.items)
        pages = self._total_pages()
        start = self.page * self.PER_PAGE
        slice_ = self.items[start: start + self.PER_PAGE]

        embed = discord.Embed(
            title=t("embed.queue_title", page=self.page + 1, pages=pages),
            color=0x3498db,
        )

        if self.current_track:
            _, title, *_ = self.current_track
            embed.add_field(name=t("embed.queue_now"), value=title, inline=False)

        if slice_:
            lines = [f"`{start + i + 1}.` {item[1]}" for i, item in enumerate(slice_)]
            embed.add_field(name=t("embed.queue_list"), value="\n".join(lines), inline=False)
        else:
            embed.add_field(name=t("embed.queue_list"), value=t("embed.queue_empty"), inline=False)

        if self.loop_mode == "song":
            loop_text = t("embed.loop_song")
        elif self.loop_mode == "queue":
            loop_text = t("embed.loop_queue")
        else:
            loop_text = t("embed.loop_off")

        known_secs = 0
        known_count = 0
        total_items = total + (1 if self.current_track else 0)
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
                dur_str = "~" + dur_str
            footer = t("embed.queue_footer_duration", total=total, duration=dur_str, loop=loop_text)
        else:
            footer = t("embed.queue_footer", total=total, loop=loop_text)
        embed.set_footer(text=footer)
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        self.prev_btn.disabled = True
        self.next_btn.disabled = True
        for child in self.children:
            child.disabled = True
