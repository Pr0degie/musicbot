"""Tests für die Sprecher-Puffer.

Discord schickt nur Pakete, solange jemand spricht – das ist ein geschenkter
Sprachaktivitäts-Filter. Ein Segment gilt als fertig, wenn eine Weile nichts
mehr kam (oder es zu lang wird).

Die Uhr wird als Parameter hereingereicht, damit die Tests ohne echtes Warten
auskommen.
"""
import pytest

from utils.speech_buffer import SpeakerBuffers

# 100 ms Audio: 48 kHz, 2 Kanäle, 2 Byte pro Sample.
HUNDERT_MS = b"\x01\x00" * (4800 * 2)


def paket(ms):
    return b"\x01\x00" * int(4800 * 2 * ms / 100)


@pytest.fixture
def puffer():
    return SpeakerBuffers(silence_ms=800, min_ms=700, max_ms=12000)


# --- Sprechpause beendet ein Segment ----------------------------------------

def test_segment_wird_nach_der_stille_faellig(puffer):
    puffer.add(1, paket(1000), now=0.0)
    assert puffer.due(now=0.5) == [], "vor Ablauf der Stille noch nicht fertig"

    fertig = puffer.due(now=1.0)
    assert len(fertig) == 1
    uid, pcm = fertig[0]
    assert uid == 1
    assert len(pcm) == len(paket(1000))


def test_ein_faelliges_segment_wird_nur_einmal_geliefert(puffer):
    puffer.add(1, paket(1000), now=0.0)
    assert len(puffer.due(now=1.0)) == 1
    assert puffer.due(now=2.0) == []


def test_weiteres_paket_verlaengert_das_segment(puffer):
    puffer.add(1, paket(500), now=0.0)
    puffer.add(1, paket(500), now=0.5)
    assert puffer.due(now=1.0) == [], "0,5 s nach dem letzten Paket noch nicht fertig"
    assert len(puffer.due(now=1.4)) == 1


# --- Zu kurz, zu lang -------------------------------------------------------

def test_zu_kurzes_segment_wird_verworfen(puffer):
    """Husten, Türklappen, Mikroklick – nicht die GPU damit behelligen."""
    puffer.add(1, paket(300), now=0.0)
    assert puffer.due(now=1.0) == []


def test_zu_langes_segment_wird_zwangsweise_geschnitten(puffer):
    """Ein Dauerredner darf nicht unbegrenzt puffern."""
    for i in range(13):
        puffer.add(1, paket(1000), now=i * 1.0)
    fertig = puffer.due(now=12.5)
    assert len(fertig) == 1, "nach max_ms muss geschnitten werden, obwohl noch Pakete kommen"


# --- Mehrere Sprecher -------------------------------------------------------

def test_zwei_sprecher_werden_getrennt_gepuffert(puffer):
    puffer.add(1, paket(1000), now=0.0)
    puffer.add(2, paket(1000), now=0.1)

    fertig = dict(puffer.due(now=1.2))
    assert set(fertig) == {1, 2}


def test_sprecherzahl_ist_gedeckelt():
    """Speicherriegel: mehr gleichzeitige Puffer werden abgelehnt."""
    puffer = SpeakerBuffers(silence_ms=800, min_ms=700, max_ms=12000, max_speakers=2)
    assert puffer.add(1, paket(100), now=0.0) is True
    assert puffer.add(2, paket(100), now=0.0) is True
    assert puffer.add(3, paket(100), now=0.0) is False

    puffer.add(1, paket(900), now=0.1)
    assert len(puffer.due(now=1.2)) >= 1, "bestehende Sprecher bleiben unbeeinflusst"


# --- Speicherdeckel pro Sprecher --------------------------------------------

def test_puffer_pro_sprecher_ist_gedeckelt(puffer):
    """Auch ohne Zwangs-Flush darf ein Puffer nicht unbegrenzt wachsen."""
    for i in range(40):
        puffer.add(1, paket(1000), now=i * 0.1)   # 40 s Audio in 4 s Zeit
    grenze = int(48000 * 4 * 12.0)                 # max_ms als Bytes
    assert puffer.bytes_of(1) <= grenze


# --- Aufräumen --------------------------------------------------------------

def test_drop_entfernt_einen_sprecher(puffer):
    puffer.add(1, paket(1000), now=0.0)
    puffer.drop(1)
    assert puffer.due(now=1.0) == []


def test_clear_entfernt_alles(puffer):
    """Nach einem Reconnect darf keine Halb-Äußerung von vorher übrig sein."""
    puffer.add(1, paket(1000), now=0.0)
    puffer.add(2, paket(1000), now=0.0)
    puffer.clear()
    assert puffer.due(now=2.0) == []
