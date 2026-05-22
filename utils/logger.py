import logging
import re
import sys

# ---------------------------------------------------------------------------
# Kategorie-Erkennung: Präfix [Foo] am Anfang der Message → eigene Farbe
# ---------------------------------------------------------------------------
_CAT_RE = re.compile(r"^\[([^\]]+)\]")


class _ColorFormatter(logging.Formatter):
    """Farbiger Formatter für den Terminal-Output (StreamHandler).

    Level-Farben:
      DEBUG → grau    INFO → weiß    WARN → gelb    ERROR → rot    CRIT → fett rot

    Kategorie-Farben (erster [Tag] in der Message):
      Nächster Track / Wiedergabe  → bright green
      Prefetch / Cache / Warmup / Resolve / Download → cyan
      Radio                        → blue
      Autoplay / Autoplay Prefetch → magenta
      Stream                       → yellow
      INIT / SAVE / Queue / …      → dim gray
    """

    _LEVELS = {
        logging.DEBUG:    ("\033[90m",   "DEBUG"),
        logging.INFO:     ("\033[0m",    "INFO "),
        logging.WARNING:  ("\033[33m",   "WARN "),
        logging.ERROR:    ("\033[31m",   "ERROR"),
        logging.CRITICAL: ("\033[1;31m", "CRIT "),
    }

    _CATS = {
        "Nächster Track":    "\033[92m",
        "Wiedergabe":        "\033[92m",
        "Prefetch":          "\033[36m",
        "Cache":             "\033[36m",
        "Warmup":            "\033[36m",
        "Resolve":           "\033[36m",
        "Download":          "\033[36m",
        "Radio":             "\033[34m",
        "Autoplay":          "\033[35m",
        "Autoplay Prefetch": "\033[35m",
        "Stream":            "\033[33m",
        "INIT":              "\033[2m",
        "SAVE":              "\033[2m",
        "Queue":             "\033[2m",
        "play_next":         "\033[2m",
        "Auto-Leave":        "\033[2m",
        "Score":             "\033[2m",
    }

    _GRAY = "\033[90m"
    _RST  = "\033[0m"

    def format(self, record):
        lc, label = self._LEVELS.get(record.levelno, ("\033[0m", "?????"))
        ts  = self.formatTime(record, "%H:%M:%S")
        msg = record.getMessage()

        m = _CAT_RE.match(msg)
        if m:
            cat = m.group(1)
            cc  = self._CATS.get(cat, lc)
            msg = f"{cc}[{cat}]{self._RST}" + msg[len(m.group(0)):]

        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)

        return f"{self._GRAY}{ts}{self._RST}  {lc}{label}{self._RST}  {msg}"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

# Console: farbig, kurzer Timestamp, kein Logger-Name
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(_ColorFormatter())

# Datei: plain, vollständig – bleibt maschinenlesbar und frei von ANSI-Codes
_file = logging.FileHandler("bot.log", encoding="utf-8")
_file.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

# Root-Logger mit beiden Handlern
_root = logging.getLogger()
_root.setLevel(logging.INFO)
_root.addHandler(_console)
_root.addHandler(_file)

# ---------------------------------------------------------------------------
# Discord-Logging: auf WARNING reduzieren – unterdrückt Gateway/HTTP-Spam.
# Zum Debuggen von Discord-Verbindungsproblemen auskommentieren:
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
# logging.getLogger("discord").setLevel(logging.DEBUG)
# logging.getLogger("discord.http").setLevel(logging.DEBUG)
# ---------------------------------------------------------------------------

logger = logging.getLogger("DiscordMusicBot")
