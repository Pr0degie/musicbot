"""Tests für POST /command – der Weg, auf dem Bot B gesprochene Wünsche
an den Musikbot weiterreicht.

Bot B hört ohnehin schon im Voice-Channel zu und transkribiert. Statt hier ein
zweites Whisper danebenzustellen, schickt er den fertigen Satz herüber.
"""
import asyncio
import json
import types

import pytest

import config
from cogs.dm_bridge import DMBridge


def run(coro):
    return asyncio.run(coro)


class FakeRequest:
    def __init__(self, body=None, remote="127.0.0.1", headers=None, ctype="application/json"):
        self._body = body if body is not None else {}
        self.remote = remote
        self.headers = {"Content-Type": ctype, **(headers or {})}

    async def text(self):
        return json.dumps(self._body) if isinstance(self._body, dict) else self._body

    async def json(self):
        if isinstance(self._body, dict):
            return self._body
        raise json.JSONDecodeError("kaputt", str(self._body), 0)


class FakeListen:
    """Merkt sich, womit handle_text gerufen wurde."""

    def __init__(self, antwort=None, enabled=True):
        self.enabled = enabled
        self.aufrufe = []
        self._antwort = antwort or {"status": "executed", "command": "p", "arg": "Sandstorm"}

    async def handle_text(self, text, *, user_id, guild_id=None, source="bridge"):
        self.aufrufe.append({"text": text, "user_id": user_id,
                             "guild_id": guild_id, "source": source})
        return self._antwort


def baue_bridge(listen=None):
    cog = DMBridge.__new__(DMBridge)
    cog.bot = types.SimpleNamespace(get_cog=lambda name: listen)
    return cog


def json_von(antwort):
    return json.loads(antwort.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def kein_secret(monkeypatch):
    monkeypatch.setattr("cogs.dm_bridge.DM_BRIDGE_SECRET", "")
    monkeypatch.setattr(config, "VOICE_CONTROL", True)


# --- Happy Path -------------------------------------------------------------

def test_satz_wird_an_den_gemeinsamen_eingang_gereicht():
    listen = FakeListen()
    bridge = baue_bridge(listen)
    request = FakeRequest({"text": "Yo Bot, spiel mal Sandstorm.",
                           "user_id": "123", "guild_id": "456", "source": "botb-stt"})

    antwort = run(bridge._handle_command(request))

    assert antwort.status == 200
    assert json_von(antwort) == {"status": "executed", "command": "p", "arg": "Sandstorm"}
    assert listen.aufrufe == [{
        "text": "Yo Bot, spiel mal Sandstorm.",
        "user_id": "123", "guild_id": "456", "source": "botb-stt",
    }]


def test_bot_b_bekommt_immer_200_damit_er_nicht_retryt():
    """Ein unverstandener Satz ist kein HTTP-Fehler – sonst würde Bot B es
    wieder und wieder versuchen."""
    listen = FakeListen(antwort={"status": "ignored", "reason": "not_understood"})
    antwort = run(baue_bridge(listen)._handle_command(
        FakeRequest({"text": "Yo Bot, blablabla", "user_id": "123"})))

    assert antwort.status == 200
    assert json_von(antwort)["reason"] == "not_understood"


# --- Abwehr -----------------------------------------------------------------

def test_ohne_voice_listen_cog_wird_abgelehnt():
    antwort = run(baue_bridge(None)._handle_command(
        FakeRequest({"text": "Yo Bot, spiel mal Sandstorm.", "user_id": "123"})))

    assert antwort.status == 200
    assert json_von(antwort) == {"status": "ignored", "reason": "disabled"}


def test_kaputtes_json_ist_ein_echter_fehler():
    antwort = run(baue_bridge(FakeListen())._handle_command(FakeRequest(body="{kein json")))
    assert antwort.status == 400


def test_fehlender_text_ist_ein_echter_fehler():
    antwort = run(baue_bridge(FakeListen())._handle_command(
        FakeRequest({"user_id": "123"})))
    assert antwort.status == 400


def test_fehlende_user_id_ist_ein_echter_fehler():
    """Ohne User-ID gibt es kein Member und damit kein ctx.author.voice."""
    antwort = run(baue_bridge(FakeListen())._handle_command(
        FakeRequest({"text": "Yo Bot, spiel mal Sandstorm."})))
    assert antwort.status == 400


def test_zu_langer_text_wird_abgewiesen():
    antwort = run(baue_bridge(FakeListen())._handle_command(
        FakeRequest({"text": "x" * 5000, "user_id": "123"})))
    assert antwort.status == 413


# --- Auth: identisch zu /speak ----------------------------------------------

def test_ohne_secret_von_aussen_abgelehnt(monkeypatch):
    monkeypatch.setattr("cogs.dm_bridge.DM_BRIDGE_SECRET", "geheim")
    antwort = run(baue_bridge(FakeListen())._handle_command(
        FakeRequest({"text": "Yo Bot, spiel mal Sandstorm.", "user_id": "123"},
                    remote="192.168.1.50")))
    assert antwort.status == 401


def test_mit_richtigem_secret_von_aussen_erlaubt(monkeypatch):
    monkeypatch.setattr("cogs.dm_bridge.DM_BRIDGE_SECRET", "geheim")
    antwort = run(baue_bridge(FakeListen())._handle_command(
        FakeRequest({"text": "Yo Bot, spiel mal Sandstorm.", "user_id": "123"},
                    remote="192.168.1.50", headers={"X-DM-Secret": "geheim"})))
    assert antwort.status == 200


def test_loopback_braucht_kein_secret(monkeypatch):
    """Damit der klassische Localhost-Betrieb konfigurationsfrei bleibt."""
    monkeypatch.setattr("cogs.dm_bridge.DM_BRIDGE_SECRET", "geheim")
    antwort = run(baue_bridge(FakeListen())._handle_command(
        FakeRequest({"text": "Yo Bot, spiel mal Sandstorm.", "user_id": "123"},
                    remote="127.0.0.1")))
    assert antwort.status == 200
