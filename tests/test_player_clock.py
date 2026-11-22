"""Die Uhr des Players startet beim ersten Paket (utils/voice.start_clock_at_first_packet).

discord.py startet die Uhr des AudioPlayers, bevor FFmpeg das erste Paket
liefert. Braucht FFmpeg dafür eine halbe Sekunde (Radiosender, Netz-Streams),
liegt der Player von Beginn an hinter seinem Zeitplan und schiebt diese halbe
Sekunde Ton ohne Pause nach – der Song klingt am Anfang kurz zu schnell.

Echter discord.player.AudioPlayer, aber weder FFmpeg noch Discord: die Quelle
liefert ihr erstes Paket verzögert, der Voice-Client notiert nur Sendezeiten.
"""

import threading
import time

from discord import player

from utils.voice import start_clock_at_first_packet

FIRST_READ_DELAY = 0.3     # so lange braucht „FFmpeg" bis zum ersten Paket
WINDOW = 0.3               # gezählt wird ab dem ersten Paket; Echtzeit = 15 Pakete


class SlowStartSource(player.AudioSource):
    def __init__(self, first_delay=FIRST_READ_DELAY):
        self.first_delay = first_delay
        self.reads = 0
        self.cleaned = False

    def read(self):
        self.reads += 1
        if self.reads == 1:
            time.sleep(self.first_delay)
        return b"\x00" * 20

    def is_opus(self):
        return True

    def cleanup(self):
        self.cleaned = True


class CountingClient:
    def __init__(self):
        self.sent = []

    def send_audio_packet(self, data, encode=True):
        self.sent.append(time.perf_counter())

    def is_connected(self):
        return True


class _Player(player.AudioPlayer):
    def _speak(self, speaking):      # kein Gateway
        pass

    def send_silence(self, count=5):  # Stille-Pakete nicht mitzählen
        pass


def _packets_in_window(source):
    client = CountingClient()
    p = _Player(source, client)
    p.start()
    time.sleep(FIRST_READ_DELAY + WINDOW + 0.15)
    p.stop()
    p.join(2)
    assert not p.is_alive()
    first = client.sent[0]
    return sum(1 for t in client.sent if t - first <= WINDOW)


def test_ohne_korrektur_schiebt_discord_py_die_startverzoegerung_nach():
    """Ausgangslage, die die Korrektur nötig macht. Schlägt das fehl, hat
    discord.py das Verhalten selbst behoben – dann kann die Korrektur weg."""
    assert _packets_in_window(SlowStartSource()) >= 26, "0,3 s Rückstand = 15 Pakete zusätzlich zu 15 in Echtzeit"


def test_uhr_startet_beim_ersten_paket():
    source = start_clock_at_first_packet(SlowStartSource())
    assert _packets_in_window(source) <= 19, "nur Echtzeit (15 Pakete), kein Nachschieben"


def test_nur_das_erste_paket_stellt_die_uhr():
    """Danach liest der Player wieder direkt aus der Quelle – ein späterer
    Aussetzer wird wie bisher von discord.py behandelt."""
    source = SlowStartSource(first_delay=0)
    original = source.read
    wrapped = start_clock_at_first_packet(source)
    assert wrapped is source and source.read != original

    seen = {}

    def run():
        seen["first"] = source.read()
        seen["loops"], seen["start"] = me.loops, me._start
        seen["second"] = source.read()

    me = threading.Thread(target=run)
    me.loops, me._start = 7, -1.0          # sieht aus wie ein Player, ist aber keiner
    me.start()
    me.join(2)
    assert seen["first"] and seen["second"]
    assert (seen["loops"], seen["start"]) == (7, -1.0), "fremde Threads bleiben unangetastet"
    assert source.read == original, "nach dem ersten Paket wieder die Original-Methode"


def test_quelle_ohne_read_bleibt_wie_sie_ist():
    """Die Fake-Quellen der übrigen Tests haben kein read()."""
    class Bare:
        pass

    bare = Bare()
    assert start_clock_at_first_packet(bare) is bare
    assert not hasattr(bare, "read")


def test_leeres_erstes_paket_stellt_die_uhr_nicht():
    """FFmpeg sofort tot: der Player soll wie bisher einfach aufhören."""
    class Dead(player.AudioSource):
        def read(self):
            return b""

    source = start_clock_at_first_packet(Dead())
    client = CountingClient()
    ended = []
    p = _Player(source, client, after=ended.append)
    p.start()
    p.join(2)
    assert not p.is_alive()
    assert client.sent == [] and ended == [None]
