import logging
import os

from dotenv import load_dotenv

# Lade Umgebungsvariablen
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Setup für Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
