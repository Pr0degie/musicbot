import copy
import re
import textwrap

import discord
from discord.ui import Button, View

from utils.i18n import t

# Breite, ab der eine Befehlszeile im Text-Block umgebrochen wird. Die Folgezeile
# wird so eingerückt, dass sie unter dem Beschreibungs-Anfang steht (hängender
# Einzug) statt am Zeilenanfang. Bei Bedarf anpassbar.
_REFLOW_WIDTH = 56

# Eine Befehlszeile im Hilfetext beginnt (nach Einrückung) mit "!name".
_LINE_CMD = re.compile(r"^\s*!([A-Za-z][\w-]*)")
# Trennt den Usage-Teil ("!seek <zeit>") von der Beschreibung am Gedankenstrich.
_DASH_SPLIT = re.compile(r"\s[–—-]\s")
# Ein Platzhalter für ein Argument: <...> oder [...].
_ARG_PLACEHOLDER = re.compile(r"[<\[]([^>\]]+)[>\]]")


def _reflow(text: str, width: int = _REFLOW_WIDTH) -> str:
    """Bricht zu lange Zeilen mit hängendem Einzug um.

    - Befehlszeile "  !cmd <arg>   – Beschreibung …": die Folgezeilen werden bis
      zum Beschreibungs-Anfang eingerückt, stehen also bündig darunter – auch
      wenn der Befehls-Teil lang ist (z. B. !radio rename …).
    - Dashlose, bereits eingerückte Fortsetzungszeile (z. B. die Preset-Liste):
      wird unter ihrer eigenen Einrückung umbrochen.
    - Kurze Zeilen (≤ width) und Überschriften bleiben unverändert.
    """
    out = []
    for line in text.split("\n"):
        if len(line) <= width:
            out.append(line)
            continue
        sep = _DASH_SPLIT.search(line)
        if sep:
            indent_len = sep.end()  # Spalte, in der die Beschreibung beginnt
        else:
            indent_len = len(line) - len(line.lstrip(" "))  # vorhandene Einrückung
        head = line[:indent_len]
        body = line[indent_len:]
        avail = max(1, width - indent_len)
        pieces = textwrap.wrap(
            body, width=avail, break_long_words=False, break_on_hyphens=False
        )
        if not pieces:
            out.append(line)
            continue
        out.append(head + pieces[0])
        indent = " " * indent_len
        out.extend(indent + piece for piece in pieces[1:])
    return "\n".join(out)


def _extract_commands(section: str) -> list:
    """Liest aus einem Hilfe-Abschnitt alle Befehle samt Beschreibung heraus.

    Pro Befehl: Name, ob er ein Argument braucht (Platzhalter <...>/[...] im
    Usage-Teil), der Platzhalter-Text fürs Eingabefenster und die Beschreibung
    (Teil hinter dem Gedankenstrich). Mehrfach genannte Befehle (z. B. !radio)
    werden zusammengefasst.
    """
    out = {}
    order = []
    for line in section.split("\n"):
        m = _LINE_CMD.match(line)
        if not m:
            continue
        name = m.group(1)
        parts = _DASH_SPLIT.split(line, maxsplit=1)
        usage = parts[0]
        description = parts[1].strip() if len(parts) > 1 else ""
        arg = _ARG_PLACEHOLDER.search(usage)
        needs_arg = arg is not None
        placeholder = arg.group(1) if arg else ""
        if name in out:
            # Spätere Zeile mit Argument gewinnt (z. B. erst "!radio", dann "!radio <Nr>").
            if needs_arg and not out[name]["needs_arg"]:
                out[name]["needs_arg"] = True
                out[name]["placeholder"] = placeholder
        else:
            out[name] = {
                "name": name,
                "needs_arg": needs_arg,
                "placeholder": placeholder,
                "description": description[:100],  # Discord-Limit für Option-Beschreibung
            }
            order.append(name)
    return [out[n] for n in order]


# Gewünschte Seiten-Reihenfolge (Schlagwort im Titel, DE+EN). Nicht gelistete
# Kategorien landen hinten. 🔁 Autoplay hat keinen Befehl und fällt ganz weg.
_CATEGORY_ORDER = ("musik", "music", "queue", "voice", "radio", "audio")


def _order_key(title: str) -> int:
    low = title.lower()
    for i, keyword in enumerate(_CATEGORY_ORDER):
        if keyword in low:
            return i
    return len(_CATEGORY_ORDER)


def _parse_categories(text: str) -> list:
    """Zerlegt den Hilfetext in Kategorien: [{"title", "text", "commands"}].

    `text` ist der Original-Abschnitt (für den Text-Block im Embed), `commands`
    die daraus gelesenen Befehle (für das Dropdown). Ein Abschnitt, der direkt
    mit einem Befehl beginnt (kein Titel, z. B. der !stats/!restart-Block), wird
    an die vorige Kategorie angehängt. Kategorien ohne Befehl (🔁 Autoplay)
    fallen weg; die übrigen werden nach `_CATEGORY_ORDER` sortiert.
    """
    categories = []
    for section in text.split("\n\n"):
        commands = _extract_commands(section)
        first = section.split("\n", 1)[0].strip()
        if first.startswith("!") and categories:
            categories[-1]["commands"].extend(commands)
            categories[-1]["text"] += "\n\n" + section
            continue
        categories.append({"title": first, "text": section, "commands": commands})

    categories = [c for c in categories if c["commands"]]
    categories.sort(key=lambda c: _order_key(c["title"]))
    return categories


class _CommandArgModal(discord.ui.Modal):
    """Kleines Eingabefenster für Befehle, die ein Argument brauchen."""

    def __init__(self, view: "HelpView", name: str, placeholder: str):
        super().__init__(title=f"!{name}"[:45])
        self._view = view
        self._name = name
        self.field = discord.ui.TextInput(
            label=t("modal.cmd_input_label"),
            placeholder=(placeholder[:100] or None),
            required=False,
            max_length=200,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self._view.run_command(interaction, self._name, self.field.value.strip())
        await self._view.reset_message()


class HelpView(View):
    """Blätterbare Hilfe als Dropdown: pro Kategorie ein Menü.

    Jeder Eintrag zeigt Befehlsname + Beschreibung; die Auswahl führt den
    Befehl direkt aus (ohne Argument sofort, mit Argument über ein
    Eingabefenster). ◀/▶ wechselt die Kategorie. Es gibt keinen separaten
    Button-Block mehr – das Menü ist gleichzeitig Liste und Auslöser.
    """

    def __init__(self, ctx, text):
        super().__init__(timeout=180)
        self.bot = ctx.bot
        self._ref_message = ctx.message  # Vorlage, um den Befehl als Klicker auszuführen
        self.message = None
        self.pages = _parse_categories(text)
        self._specs = {s["name"]: s for cat in self.pages for s in cat["commands"]}
        self.page = 0
        self._render()

    def _render(self):
        """Baut das Menü + die Blätter-Buttons für die aktuelle Kategorie neu."""
        self.clear_items()
        cat = self.pages[self.page]
        # Dropdown nur, wenn die Kategorie auch Befehle hat (🔁 Autoplay z. B. nicht).
        if cat["commands"]:
            options = [
                discord.SelectOption(
                    label=f"!{spec['name']}"[:100],
                    value=spec["name"],
                    description=(spec["description"] or None),
                )
                for spec in cat["commands"]
            ]
            select = discord.ui.Select(
                placeholder=t("select.help_placeholder"),
                min_values=1,
                max_values=1,
                options=options,
                row=0,
            )
            select.callback = self._make_select_callback(select)
            self.add_item(select)

        prev = Button(label="◀", style=discord.ButtonStyle.secondary, row=1, disabled=self.page == 0)
        nxt = Button(label="▶", style=discord.ButtonStyle.secondary, row=1, disabled=self.page >= len(self.pages) - 1)
        prev.callback = self._go_prev
        nxt.callback = self._go_next
        self.add_item(prev)
        self.add_item(nxt)

    def build_embed(self) -> discord.Embed:
        cat = self.pages[self.page]
        embed = discord.Embed(
            title=t("embed.help_title", page=self.page + 1, pages=len(self.pages)),
            description="```\n" + _reflow(cat["text"]) + "\n```",
            color=0x3498DB,
        )
        return embed

    def _make_select_callback(self, select):
        async def callback(interaction: discord.Interaction):
            name = select.values[0]
            spec = self._specs[name]
            if spec["needs_arg"]:
                await interaction.response.send_modal(
                    _CommandArgModal(self, name, spec["placeholder"])
                )
            else:
                # Menü zurücksetzen (frische View) und Klick bestätigen, dann ausführen.
                self._render()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)
                await self.run_command(interaction, name)

        return callback

    async def run_command(self, interaction: discord.Interaction, name: str, arg: str = ""):
        """Führt '!name [arg]' so aus, als hätte der Klickende es getippt.

        Trick: eine Kopie der ursprünglichen Nachricht (richtiger Channel/Guild)
        bekommt den Klickenden als Autor und den neuen Befehl als Inhalt, dann
        läuft alles durch die normale Command-Pipeline (inkl. Checks/Fehler).
        """
        content = f"!{name}" + (f" {arg}" if arg else "")
        fake = copy.copy(self._ref_message)
        fake.author = interaction.user
        fake.content = content
        ctx = await self.bot.get_context(fake)
        if ctx.command is not None:
            await self.bot.invoke(ctx)

    async def reset_message(self):
        """Setzt das Dropdown wieder auf 'nichts ausgewählt' (nach Modal-Eingabe)."""
        self._render()
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _go_prev(self, interaction: discord.Interaction):
        self.page -= 1
        self._render()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _go_next(self, interaction: discord.Interaction):
        self.page += 1
        self._render()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
