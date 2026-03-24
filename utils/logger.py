import logging

# Zentrales Logging-Setup für den gesamten Bot.
# Schreibt gleichzeitig in die Konsole (StreamHandler) und in bot.log (FileHandler),
# damit Fehler auch nach einem Neustart noch nachvollziehbar sind.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger("DiscordMusicBot")
