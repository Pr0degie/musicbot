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
      Radio                        → bright blue (auf dunkelblauem Grund lesbar)
      Autoplay / Autoplay Prefetch → bright magenta
      Stream                       → yellow
      INIT / SAVE / Queue / …      → dim gray
    """

    _LEVELS = {
        logging.DEBUG:    ("\033[90m",   "DEBUG"),
        logging.INFO:     ("\033[0m",    ""),
        logging.WARNING:  ("\033[33m",   "WARN "),
        logging.ERROR:    ("\033[91m",   "ERROR"),  # helles Rot – pop auf dunkelblauem Grund
        logging.CRITICAL: ("\033[1;91m", "CRIT "),
    }

    _CATS = {
        "Nächster Track":    "\033[92m",
        "Wiedergabe":        "\033[92m",
        "Prefetch":          "\033[36m",
        "Cache":             "\033[36m",
        "Warmup":            "\033[36m",
        "Resolve":           "\033[36m",
        "Download":          "\033[36m",
        "Radio":             "\033[94m",  # helles Blau – Standardblau (34) wäre auf dunkelblauem Grund unlesbar
        "Autoplay":          "\033[95m",  # helles Magenta
        "Autoplay Prefetch": "\033[95m",
        "Stream":            "\033[33m",
        "INIT":              "\033[2m",
        "SAVE":              "\033[2m",
        "Queue":             "\033[2m",
        "play_next":         "\033[2m",
        "Auto-Leave":        "\033[2m",
        "Score":             "\033[2m",
        "Voice":             "\033[2m",  # dim – Reconnect/Watchdog-Hinweise unaufdringlich
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

        prefix = f"{lc}{label}{self._RST}  " if label else ""
        return f"{self._GRAY}{ts}{self._RST}  {prefix}{msg}"


# ---------------------------------------------------------------------------
# Terminal-Setup: VT/ANSI aktivieren + Fensterhintergrund auf sehr dunkles Blau
# ---------------------------------------------------------------------------

# Sehr dunkles Blau (Navy) als Fensterhintergrund. \033[0m (INFO/Reset im
# Formatter) fällt auf genau diese Default-Farbe zurück → durchgehender Grund.
_BG_COLOR = "#0a0f2c"


def _init_terminal():
    """Aktiviert ANSI-Verarbeitung (Windows) und setzt den Hintergrund via OSC 11."""
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            _ENABLE_VT = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            for _std in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
                handle = kernel32.GetStdHandle(_std)
                mode = ctypes.c_uint32()
                if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                    kernel32.SetConsoleMode(handle, mode.value | _ENABLE_VT)
        except Exception:
            pass
    # OSC 11 ; <farbe> BEL → Default-Hintergrund des Terminals setzen.
    # Wird von Terminals ohne OSC-Support stillschweigend ignoriert.
    try:
        sys.stdout.write(f"\033]11;{_BG_COLOR}\007")
        sys.stdout.flush()
    except Exception:
        pass


_init_terminal()

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

# Voice-Reconnect-Dämpfung: Discord wirft stille Voice-Verbindungen mit Code
# 1006 weg; discord.py reconnectet automatisch, loggt das aber als ERROR samt
# vollem Traceback. Das ist erwartetes Verhalten – wir reduzieren es auf eine
# ruhige INFO-Zeile ohne Traceback, statt jedes Mal einen roten Stacktrace.


class _VoiceReconnectFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if "Reconnecting" in msg or "1006" in msg or "ConnectionClosed" in msg:
            record.exc_info = None
            record.exc_text = None
            if record.levelno >= logging.ERROR:
                record.levelno = logging.INFO
                record.levelname = "INFO"
                record.msg = "[Voice] Verbindung kurz unterbrochen (idle) – reconnecte automatisch."
                record.args = ()
        return True


logging.getLogger("discord.voice_state").addFilter(_VoiceReconnectFilter())
# ---------------------------------------------------------------------------

logger = logging.getLogger("DiscordMusicBot")
