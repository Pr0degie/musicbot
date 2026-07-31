import os

from dotenv import load_dotenv

# Lade Umgebungsvariablen
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Entweder cookiefile (Pfad zu einer exportierten cookies.txt) ODER Browser setzen.
# cookiefile hat Vorrang. Beide leer → keine Cookie-Auth.
YDL_COOKIES_FILE = os.getenv("YDL_COOKIES_FILE", "")
YDL_BROWSER = os.getenv("YDL_BROWSER", "firefox")
LANGUAGE = os.getenv("LANGUAGE", "en")

# DM-Bridge: kleiner HTTP-Server, über den Bot B (das "Ohr+Hirn") diesem Bot
# fertige Audiodateien zum Abspielen schickt. Localhost (Default) = Pfad-Modus
# (gemeinsame Platte). Für getrennte Maschinen DM_BRIDGE_HOST auf die Tailscale-/
# LAN-IP (oder 0.0.0.0) setzen; dann kommen die WAV-Bytes übers Netz (ADR 010).
DM_BRIDGE_HOST = os.getenv("DM_BRIDGE_HOST", "127.0.0.1")
DM_BRIDGE_PORT = int(os.getenv("DM_BRIDGE_PORT", "8765"))
# Shared Secret, nur nötig wenn nicht-localhost gebunden (Byte-Modus). Muss mit
# DMbots DM_BRIDGE_SECRET übereinstimmen. Leer + 127.0.0.1 = klassischer Pfad-Modus.
DM_BRIDGE_SECRET = os.getenv("DM_BRIDGE_SECRET", "")


def _parse_log_mode(value) -> str:
    """"quiet" | "debug" – alles andere (auch leer) fällt auf "quiet" zurück."""
    v = (value or "").strip().lower()
    return v if v in ("quiet", "debug") else "quiet"


# Terminal-Modus: quiet (Default) = nur echte Probleme (WARNING+) im Terminal,
# debug = volle Diagnose wie bisher. bot.log bekommt IMMER die volle Diagnose.
# Zur Laufzeit umschaltbar mit !debug on|off (utils/logger.set_console_mode).
# Wichtig (Invariante): hier wird NIE logging.basicConfig() gerufen – das
# Logging konfiguriert allein utils/logger.py.
LOG_MODE = _parse_log_mode(os.getenv("LOG_MODE", "quiet"))


# --- Security-Flags -----------------------------------------------------------
# Defaults erhalten das heutige Verhalten; scharf geschaltet wird bewusst per .env
# nach dem Live-Test.

def _parse_url_validation(value) -> str:
    """"off" | "warn" | "block" – alles andere (auch leer) fällt auf "warn" zurück."""
    v = (value or "").strip().lower()
    return v if v in ("off", "warn", "block") else "warn"


# SSRF-Schutz für !radio <url> und !next url||titel (utils/url_check.py):
# off = kein Check, warn (Default) = spielt wie bisher + Warnung in Log/Channel,
# block = URLs auf private/loopback/link-local Adressen werden abgelehnt.
URL_VALIDATION = _parse_url_validation(os.getenv("URL_VALIDATION", "warn"))


def _parse_bool(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_role_id(value) -> int:
    """Discord-Rollen-ID als int; leer oder ungültig → 0 (= kein Gating)."""
    v = str(value or "").strip()
    return int(v) if v.isdigit() else 0


# Wiedergabe-steuernde Commands (!s, !stop, !clear, !eq, !seek, !now, !remove,
# !move, !shuffle) erfordern denselben Voice-Channel wie der Bot.
# false (Default) = kein Check. Owner ist immer ausgenommen (utils/checks.py).
REQUIRE_SAME_VOICE = _parse_bool(os.getenv("REQUIRE_SAME_VOICE", "false"))

# Wenn gesetzt: !radio delete/rename, !reloadcookies und !format nur für Mitglieder
# dieser Rolle ODER den Bot-Owner. 0/leer (Default) = kein Gating.
ADMIN_ROLE_ID = _parse_role_id(os.getenv("ADMIN_ROLE_ID", ""))


def _parse_max_mb(value) -> int:
    """Nicht-negative Ganzzahl in MB; leer oder ungültig → 0 (= Cleanup aus)."""
    v = str(value or "").strip()
    return int(v) if v.isdigit() else 0


# Maximale Größe des downloads/-Ordners in MB. 0 (Default) = kein Cleanup,
# heutiges Verhalten. Wenn gesetzt: nach jedem Download werden die ältesten
# Dateien (mtime) gelöscht, bis das Limit eingehalten ist – niemals Dateien
# zu Songs in Queue/current_track oder die gerade abgespielte Datei.
DOWNLOADS_MAX_MB = _parse_max_mb(os.getenv("DOWNLOADS_MAX_MB", "0"))
