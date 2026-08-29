"""Zweistufiges Strg+C für den Terminal-Betrieb.

Ohne eigenen Handler wirft Python bei SIGINT ein KeyboardInterrupt, das
discord.py in ``bot.run()`` stumm schluckt – ein Fehlgriff beendet den Bot
also sofort und kommentarlos. Stattdessen:

1. Strg+C  → Rückfrage, der Bot läuft weiter (nach CONFIRM_WINDOW entschärft)
2. Strg+C  → geordnetes Herunterfahren über ``bot.close()``
3. Strg+C  → harter Abbruch, falls das Herunterfahren hängt

Geordnet heißt hier schlicht ``bot.close()``: das entfernt alle Cogs
(``cog_unload`` flusht Play-Counts und Metadaten-Cache, stoppt den
DM-Bridge-Server) und trennt danach die Voice-Clients. Danach kehrt
``bot.run()`` normal zurück und der Prozess endet regulär.
"""

import os
import signal
import time

from utils.logger import logger

# Sekunden, in denen das zweite Strg+C als Bestätigung zählt.
CONFIRM_WINDOW = 5.0


def install_sigint_handler(bot):
    """Registriert den zweistufigen SIGINT-Handler und gibt ihn zurück."""
    state = {"armed_at": None, "closing": False}

    def expire(armed_at):
        # Nur entschärfen, wenn seither kein neues Strg+C kam und wir nicht
        # ohnehin schon herunterfahren – sonst meldet ein alter Timer Unsinn.
        if state["closing"] or state["armed_at"] != armed_at:
            return
        state["armed_at"] = None
        logger.info("[Shutdown] Abbruch – der Bot läuft weiter.")

    def handler(signum, frame):
        if state["closing"]:
            logger.warning("[Shutdown] Erzwungenes Ende – Prozess wird hart beendet.")
            os._exit(0)
            return

        now = time.monotonic()
        if state["armed_at"] is not None and now - state["armed_at"] <= CONFIRM_WINDOW:
            state["closing"] = True
            state["armed_at"] = None
            logger.info("[Shutdown] Fahre herunter... (nochmal Strg+C beendet den Prozess hart)")
            _request_close(bot)
            return

        state["armed_at"] = now
        logger.info(f"[Shutdown] Strg+C erkannt. Nochmal innerhalb {CONFIRM_WINDOW:.0f} s drücken zum Beenden.")
        _arm_expiry(bot, expire, now)

    signal.signal(signal.SIGINT, handler)
    return handler


def _running_loop(bot):
    """Der laufende Event-Loop des Bots – oder None (Strg+C vor dem Start)."""
    loop = getattr(bot, "loop", None)
    if loop is None or not getattr(loop, "is_running", None) or not loop.is_running():
        return None
    return loop


def _arm_expiry(bot, expire, armed_at):
    loop = _running_loop(bot)
    if loop is None:
        return  # Ohne Loop kein Timer – der Zustand verfällt dann erst beim nächsten Druck.
    # call_later ist nicht thread-safe und würde den Loop auch nicht wecken.
    loop.call_soon_threadsafe(loop.call_later, CONFIRM_WINDOW, expire, armed_at)


def _request_close(bot):
    loop = _running_loop(bot)
    if loop is None:
        # Der Loop läuft (noch) nicht – dann bleibt nur das alte Verhalten.
        raise KeyboardInterrupt
    music = bot.get_cog("MusicCommands")
    if music is not None:
        # Wie bei !restart: Watchdog und Auto-Advance dürfen während des
        # Voice-Disconnects nichts nachstarten.
        music._stopped_by_user = True
    loop.call_soon_threadsafe(lambda: loop.create_task(_close(bot)))


async def _close(bot):
    """bot.close() mit Fehlerbehandlung.

    Ohne den try bliebe ein Fehler still im Task liegen (asyncio meldet ihn
    erst beim Aufräumen) – der Bot stünde weiter da, obwohl er beenden sollte.
    Dann lieber hart raus: der Nutzer hat das Ende angefordert.
    """
    try:
        await bot.close()
    except Exception:
        logger.exception("[Shutdown] Fehler beim Herunterfahren – erzwinge das Ende.")
        os._exit(1)
