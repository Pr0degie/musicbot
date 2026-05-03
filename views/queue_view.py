import discord
from discord.ui import Button, View


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
            title=f"📋 Queue — Seite {self.page + 1}/{pages}",
            color=0x3498db,
        )

        if self.current_track:
            _, title, *_ = self.current_track
            embed.add_field(name="▶ Jetzt", value=title, inline=False)

        if slice_:
            lines = [f"`{start + i + 1}.` {title}" for i, (_, title) in enumerate(slice_)]
            embed.add_field(name="Warteschlange", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Warteschlange", value="*(leer)*", inline=False)

        loop_text = {"song": "🔂 Song", "queue": "🔁 Queue"}.get(self.loop_mode, "aus")
        embed.set_footer(text=f"{total} Songs gesamt • Loop: {loop_text}")
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
