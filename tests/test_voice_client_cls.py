"""Tests für die Wahl der Voice-Client-Klasse.

Der Empfangs-Client kann nur beim connect() gesetzt werden – ein bestehender
VoiceClient lässt sich nicht nachrüsten. Deshalb entscheidet ein .env-Flag
(Neustart nötig) über die Klasse, und !listen schaltet nur noch das Zuhören.

Ist das Flag aus, muss connect_voice byte-identisch zum bisherigen
channel.connect() sein – ADR 0004.
"""
import types

import pytest

import config
from utils.voice import connect_voice, stop_playback, voice_client_cls


class FakeChannel:
    def __init__(self):
        self.aufrufe = []

    async def connect(self, **kwargs):
        self.aufrufe.append(kwargs)
        return "verbunden"


class NurSenden:
    def __init__(self):
        self.stops = []

    def stop(self):
        self.stops.append("stop")


class SendenUndEmpfangen(NurSenden):
    def __init__(self):
        super().__init__()
        self.play_stops = []

    def stop_playing(self):
        self.play_stops.append("stop_playing")


# --- stop_playback ----------------------------------------------------------

def test_normaler_client_wird_wie_bisher_gestoppt():
    vc = NurSenden()
    stop_playback(vc)
    assert vc.stops == ["stop"]


def test_empfangender_client_stoppt_nur_die_wiedergabe():
    """Sonst würde ein !s auch das Zuhören beenden."""
    vc = SendenUndEmpfangen()
    stop_playback(vc)
    assert vc.play_stops == ["stop_playing"]
    assert vc.stops == [], "stop() hätte auch den Empfang beendet"


# --- Klassenwahl ------------------------------------------------------------

def test_ohne_flag_keine_eigene_klasse(monkeypatch):
    monkeypatch.setattr(config, "VOICE_CONTROL", False)
    monkeypatch.setattr(config, "VOICE_OWN_LISTEN", False)
    assert voice_client_cls() is None


def test_beide_flags_noetig(monkeypatch):
    """VOICE_CONTROL allein aktiviert nur den Bridge-Weg."""
    monkeypatch.setattr(config, "VOICE_CONTROL", True)
    monkeypatch.setattr(config, "VOICE_OWN_LISTEN", False)
    assert voice_client_cls() is None


# --- connect_voice ----------------------------------------------------------

def test_ohne_flag_wird_ohne_cls_verbunden(monkeypatch):
    """Altverhalten: exakt channel.connect() ohne Zusatzargument."""
    monkeypatch.setattr(config, "VOICE_CONTROL", False)
    monkeypatch.setattr(config, "VOICE_OWN_LISTEN", False)
    kanal = FakeChannel()

    import asyncio
    ergebnis = asyncio.run(connect_voice(kanal))

    assert ergebnis == "verbunden"
    assert kanal.aufrufe == [{}], "cls darf im Altverhalten nicht mitgegeben werden"


def test_mit_flag_wird_die_empfangsklasse_uebergeben(monkeypatch):
    monkeypatch.setattr(config, "VOICE_CONTROL", True)
    monkeypatch.setattr(config, "VOICE_OWN_LISTEN", True)
    monkeypatch.setattr("utils.voice._recv_client_cls", lambda: types.SimpleNamespace(name="VRC"))
    kanal = FakeChannel()

    import asyncio
    asyncio.run(connect_voice(kanal))

    assert "cls" in kanal.aufrufe[0]


def test_fehlendes_paket_faellt_auf_altverhalten_zurueck(monkeypatch):
    """Das Empfangs-Paket ist experimentell. Fehlt es, verliert der Bot nur
    das eigene Zuhören – nicht die Musikwiedergabe."""
    monkeypatch.setattr(config, "VOICE_CONTROL", True)
    monkeypatch.setattr(config, "VOICE_OWN_LISTEN", True)
    monkeypatch.setattr("utils.voice._recv_client_cls", lambda: None)
    kanal = FakeChannel()

    import asyncio
    asyncio.run(connect_voice(kanal))

    assert kanal.aufrufe == [{}]


@pytest.mark.parametrize("flag", [True, False])
def test_connect_reicht_weitere_argumente_durch(monkeypatch, flag):
    monkeypatch.setattr(config, "VOICE_CONTROL", flag)
    monkeypatch.setattr(config, "VOICE_OWN_LISTEN", False)
    kanal = FakeChannel()

    import asyncio
    asyncio.run(connect_voice(kanal, timeout=30))

    assert kanal.aufrufe[0]["timeout"] == 30
