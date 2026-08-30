"""Tests für den gemeinsamen Eingang der Sprachsteuerung.

`VoiceListen.handle_text()` ist die einzige Stelle, durch die gesprochene
Befehle laufen – egal ob sie von Bot B über die DM-Bridge kommen oder aus dem
eigenen Zuhören. Deshalb liegen hier auch alle Abwehrfälle.
"""
import asyncio
import copy
import types

import pytest

import config
from cogs.voice_listen import VoiceListen
from utils.nl_parser import build_wake_re


def run(coro):
    """Projekt-Konvention: async über asyncio.run statt pytest-asyncio."""
    return asyncio.run(coro)


# --- Fakes ------------------------------------------------------------------

class FakeMember:
    def __init__(self, uid=111, name="Tobi", bot=False):
        self.id = uid
        self.display_name = name
        self.bot = bot
        self.voice = types.SimpleNamespace(channel=object())


class FakeChannel:
    def __init__(self):
        self.gesendet = []

    async def send(self, inhalt=None, **kwargs):
        self.gesendet.append(inhalt)
        return types.SimpleNamespace(content=inhalt, channel=self, author=None)


class FakeBot:
    """Zeichnet auf, welcher Command mit welchem ctx invoked wurde."""

    def __init__(self, command="p"):
        self.invoked = []
        self.kontexte = []
        self.dm_speaking = False
        self._command = command

    async def get_context(self, message):
        ctx = types.SimpleNamespace(
            message=message,
            author=message.author,
            channel=message.channel,
            command=self._command,
        )
        self.kontexte.append(ctx)
        return ctx

    async def invoke(self, ctx):
        self.invoked.append(ctx)

    def get_cog(self, name):
        return None


@pytest.fixture
def member():
    return FakeMember()


@pytest.fixture
def cog(member, monkeypatch):
    """Cog ohne __init__ – Muster von tests/conftest.py::build_cog."""
    monkeypatch.setattr(config, "VOICE_BLOCKED_USER_IDS", frozenset())
    c = VoiceListen.__new__(VoiceListen)
    c.bot = FakeBot()
    c.enabled = True
    c._wake_re = build_wake_re()
    c._reply_channel = FakeChannel()
    c._ref_message = types.SimpleNamespace(
        content="!listen on", channel=c._reply_channel, author=member)
    c._resolve_member = lambda uid, gid=None: member if uid == member.id else None
    return c


# --- Happy Path -------------------------------------------------------------

def test_songwunsch_wird_zu_einem_p_befehl(cog, member):
    ergebnis = run(cog.handle_text(
        "Yo Bot, spiel mal Bohemian Rhapsody.", user_id=member.id, source="bridge"))

    assert ergebnis["status"] == "executed"
    assert ergebnis["command"] == "p"
    assert ergebnis["arg"] == "Bohemian Rhapsody"

    assert len(cog.bot.invoked) == 1
    nachricht = cog.bot.kontexte[0].message
    assert nachricht.content == "!p Bohemian Rhapsody"
    assert nachricht.author is member, "ctx.author muss der Sprecher sein, sonst stimmt .voice nicht"


def test_steuerbefehl_ohne_argument_hat_kein_leerzeichen_am_ende(cog, member):
    run(cog.handle_text("Yo Bot, überspring das mal.", user_id=member.id, source="bridge"))
    assert cog.bot.kontexte[0].message.content == "!s"


# --- Abwehrfälle ------------------------------------------------------------

def test_ohne_weckwort_passiert_gar_nichts(cog, member):
    """Kein Weckwort → kein Command UND keine Nachricht im Chat."""
    ergebnis = run(cog.handle_text("spiel mal was", user_id=member.id, source="bridge"))

    assert ergebnis == {"status": "ignored", "reason": "no_wake_word"}
    assert cog.bot.invoked == []
    assert cog._reply_channel.gesendet == []


def test_weckwort_ohne_verstandenen_wunsch_fragt_zurueck(cog, member):
    ergebnis = run(cog.handle_text("Yo Bot, wie geht's dir?", user_id=member.id, source="bridge"))

    assert ergebnis["status"] == "ignored"
    assert ergebnis["reason"] == "not_understood"
    assert cog.bot.invoked == []
    assert len(cog._reply_channel.gesendet) == 1


def test_ausgeschaltet_fuehrt_nichts_aus(cog, member):
    cog.enabled = False
    ergebnis = run(cog.handle_text("Yo Bot, spiel mal Sandstorm.", user_id=member.id, source="bridge"))

    assert ergebnis == {"status": "ignored", "reason": "disabled"}
    assert cog.bot.invoked == []


def test_waehrend_bot_b_spricht_wird_nichts_ausgefuehrt(cog, member):
    """Die DM-Bridge besitzt dann den Voice-Client – ein !p würde dazwischenfunken."""
    cog.bot.dm_speaking = True
    ergebnis = run(cog.handle_text("Yo Bot, spiel mal Sandstorm.", user_id=member.id, source="bridge"))

    assert ergebnis == {"status": "ignored", "reason": "dm_speaking"}
    assert cog.bot.invoked == []


def test_gesperrte_user_werden_ignoriert(cog, member, monkeypatch):
    monkeypatch.setattr(config, "VOICE_BLOCKED_USER_IDS", frozenset({member.id}))
    ergebnis = run(cog.handle_text("Yo Bot, spiel mal Sandstorm.", user_id=member.id, source="bridge"))

    assert ergebnis == {"status": "ignored", "reason": "user_blocked"}
    assert cog.bot.invoked == []


def test_andere_bots_werden_ignoriert(cog):
    """Sonst könnte Bot Bs eigene Sprachausgabe dem Musikbot Befehle geben."""
    bot_member = FakeMember(uid=999, name="BotB", bot=True)
    cog._resolve_member = lambda uid, gid=None: bot_member
    ergebnis = run(cog.handle_text("Yo Bot, spiel mal Sandstorm.", user_id=999, source="bridge"))

    assert ergebnis == {"status": "ignored", "reason": "user_blocked"}
    assert cog.bot.invoked == []


def test_unbekannter_user_wird_abgelehnt(cog):
    ergebnis = run(cog.handle_text("Yo Bot, spiel mal Sandstorm.", user_id=424242, source="bridge"))

    assert ergebnis == {"status": "ignored", "reason": "unknown_user"}
    assert cog.bot.invoked == []


def test_ohne_referenznachricht_wird_nichts_ausgefuehrt(cog, member):
    """Ohne !listen on im Textkanal fehlt die Message, aus der der ctx entsteht."""
    cog._ref_message = None
    cog._reply_channel = None
    ergebnis = run(cog.handle_text("Yo Bot, spiel mal Sandstorm.", user_id=member.id, source="bridge"))

    assert ergebnis["reason"] == "no_context"
    assert cog.bot.invoked == []


def test_unbekannter_command_wird_nicht_invoked(cog, member):
    cog.bot._command = None
    ergebnis = run(cog.handle_text(
        "Yo Bot, spiel mal Bohemian Rhapsody.", user_id=member.id, source="bridge"))

    assert ergebnis["reason"] == "unknown_command"
    assert cog.bot.invoked == []


# --- Beide Eingänge landen beim selben Ergebnis -----------------------------

def test_bridge_und_eigenes_zuhoeren_erzeugen_denselben_befehl(cog, member):
    """Der Sinn des "ein Parser, zwei Eingänge"-Designs: identisches Ergebnis."""
    run(cog.handle_text("Yo Bot, spiel mal Sandstorm.", user_id=member.id, source="bridge"))
    run(cog.handle_text("Yo Bot, spiel mal Sandstorm.", user_id=member.id, source="eigen"))

    inhalte = [k.message.content for k in cog.bot.kontexte]
    assert inhalte == ["!p Sandstorm", "!p Sandstorm"]


def test_referenznachricht_wird_kopiert_nicht_veraendert(cog, member):
    """copy.copy darf das Original nicht anfassen – sonst zerschießt der zweite
    Sprachbefehl den Kontext des ersten."""
    original = cog._ref_message
    kopie = copy.copy(original)
    kopie.content = "!p X"
    assert original.content == "!listen on"
