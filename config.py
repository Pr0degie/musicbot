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
# fertige Audiodateien zum Abspielen schickt. Nur localhost – keine externe
# Erreichbarkeit.
DM_BRIDGE_HOST = os.getenv("DM_BRIDGE_HOST", "127.0.0.1")
DM_BRIDGE_PORT = int(os.getenv("DM_BRIDGE_PORT", "8765"))
