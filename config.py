import os

from dotenv import load_dotenv

# Lade Umgebungsvariablen
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Entweder cookiefile (Pfad zu einer exportierten cookies.txt) ODER Browser setzen.
# cookiefile hat Vorrang. Beide leer → keine Cookie-Auth.
YDL_COOKIES_FILE = os.getenv("YDL_COOKIES_FILE", "")
YDL_BROWSER = os.getenv("YDL_BROWSER", "firefox")
