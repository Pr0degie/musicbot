"""Sammelt Discord-Audio pro Sprecher zu ganzen Äußerungen.

Discord schickt nur Pakete, solange jemand tatsächlich spricht – damit ist die
Sprachaktivitätserkennung geschenkt. Ein Segment gilt als fertig, wenn eine
Weile kein Paket mehr kam.

Bewusst NICHT über die `on_voice_member_speaking_start/stop`-Events des
Empfangs-Pakets: die hängen am Speaking-Flag, das Clients unzuverlässig und
verzögert setzen. Die Paketlücke ist das ehrlichere Signal.

Die Uhr wird hereingereicht statt selbst gelesen – so sind die Tests
deterministisch und brauchen kein echtes Warten.

Thread-Grenze: `add()` läuft im Empfangs-Thread des Pakets, `due()` im
Event-Loop. Deshalb der Lock.
"""
import threading

from utils.pcm import BYTES_PRO_FRAME, SAMPLE_RATE

_BYTES_PRO_MS = SAMPLE_RATE * BYTES_PRO_FRAME / 1000.0


class SpeakerBuffers:
    def __init__(self, *, silence_ms=800, min_ms=700, max_ms=12000, max_speakers=8):
        self.silence = silence_ms / 1000.0
        self.min_bytes = int(min_ms * _BYTES_PRO_MS)
        self.max_bytes = int(max_ms * _BYTES_PRO_MS)
        self.max_speakers = max_speakers
        self._lock = threading.Lock()
        # user_id -> [bytearray, letztes_paket_zeitpunkt, start_zeitpunkt]
        self._puffer = {}

    def add(self, user_id: int, pcm: bytes, now: float) -> bool:
        """Hängt Audio an. False, wenn der Sprecher-Deckel erreicht ist."""
        with self._lock:
            eintrag = self._puffer.get(user_id)
            if eintrag is None:
                if len(self._puffer) >= self.max_speakers:
                    return False
                eintrag = [bytearray(), now, now]
                self._puffer[user_id] = eintrag

            eintrag[0].extend(pcm)
            eintrag[1] = now
            # Speicherriegel: vorne abschneiden, das Neueste zählt.
            ueberhang = len(eintrag[0]) - self.max_bytes
            if ueberhang > 0:
                del eintrag[0][:ueberhang]
            return True

    def due(self, now: float):
        """Fertige Segmente als [(user_id, pcm)] und aus dem Puffer entfernt.

        Fertig heißt: seit `silence_ms` kein Paket mehr, oder länger als
        `max_ms` (Zwangsschnitt gegen Dauerredner). Zu kurze Segmente werden
        verworfen, statt die GPU mit einem Türklappen zu behelligen.
        """
        fertig = []
        with self._lock:
            for user_id in list(self._puffer):
                daten, zuletzt, begonnen = self._puffer[user_id]
                still = (now - zuletzt) >= self.silence
                zu_lang = (now - begonnen) >= (self.max_bytes / _BYTES_PRO_MS / 1000.0)
                if not (still or zu_lang):
                    continue

                del self._puffer[user_id]
                if len(daten) >= self.min_bytes:
                    fertig.append((user_id, bytes(daten)))
        return fertig

    def bytes_of(self, user_id: int) -> int:
        with self._lock:
            eintrag = self._puffer.get(user_id)
            return len(eintrag[0]) if eintrag else 0

    def drop(self, user_id: int) -> None:
        """Sprecher hat den Channel verlassen – kein Leak über Stunden."""
        with self._lock:
            self._puffer.pop(user_id, None)

    def clear(self) -> None:
        """Nach einem Reconnect: keine Halb-Äußerung von vorher weiterschleppen."""
        with self._lock:
            self._puffer.clear()
