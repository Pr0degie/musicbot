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
