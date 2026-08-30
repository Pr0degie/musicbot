"""Audio-Empfänger für die Sprachsteuerung.

Die einzige Datei im Projekt, die `voice_recv` auf Modulebene importiert –
deshalb wird sie erst beim Einschalten geladen. Fehlt das (experimentelle)
Paket oder bricht es, betrifft das nur diese Datei; der Rest des Bots läuft
unverändert weiter.
"""
from discord.ext import voice_recv

from utils.logger import logger


class SpeechSink(voice_recv.AudioSink):
    """Reicht dekodiertes PCM pro Sprecher an die Puffer weiter.

    `write()` läuft im Empfangs-Thread des Pakets, nicht im Event-Loop. Hier
    passiert deshalb bewusst nichts außer Filtern und Anhängen – alles Weitere
    holt sich der Cog aus den Puffern ab.
    """

    def __init__(self, buffers, *, clock, ignorieren=None):
        super().__init__()
        self._buffers = buffers
        self._clock = clock
        self._ignorieren = ignorieren or (lambda member: False)
        self._abgelehnt = set()

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data):
        # user ist None, solange die SSRC noch nicht aufgelöst ist – das sind
        # ein paar Frames zu Beginn jeder Sitzung.
        if user is None:
            return
        # Andere Bots nie: sonst transkribiert der Musikbot die Sprachausgabe
        # des DM-Bots und könnte sich selbst Befehle geben.
        if getattr(user, "bot", False) or self._ignorieren(user):
            return

        pcm = getattr(data, "pcm", None)
        if not pcm:
            return

        if not self._buffers.add(user.id, pcm, self._clock()):
            # Nur einmal pro Sprecher melden, sonst flutet es das Log.
            if user.id not in self._abgelehnt:
                self._abgelehnt.add(user.id)
                logger.info(f"[STT] Zu viele gleichzeitige Sprecher – "
                            f"{user.display_name} wird nicht gepuffert.")

    def cleanup(self):
        self._abgelehnt.clear()
