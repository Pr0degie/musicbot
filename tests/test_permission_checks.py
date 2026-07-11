"""Tests für die Rechte-Checks (utils/checks.py) und ihre Flag-Defaults.

Wichtigste Zusicherungen: (1) beide Flags ungesetzt → kein Check aktiv, es wird
nicht einmal is_owner() aufgerufen; (2) der Owner wird durch keine
Flag-Kombination ausgesperrt.
"""

import asyncio
import types

import pytest

import config
from cogs.music import MusicCommands
from utils import checks


class BoomBot:
    """bot-Ersatz dessen is_owner knallt – beweist, dass der Check kurzschließt."""

    async def is_owner(self, user):
        raise AssertionError("is_owner darf hier nicht aufgerufen werden")


class FakeBot:
    def __init__(self, owner=None):
        self.owner = owner

    async def is_owner(self, user):
        return user is self.owner


def make_ctx(bot, author_channel=None, bot_channel=None, roles=()):
    """Minimaler ctx mit author.voice/roles, voice_client.channel und send()."""
    author = types.SimpleNamespace(
        voice=types.SimpleNamespace(channel=author_channel) if author_channel else None,
        roles=list(roles),
    )
    ctx = types.SimpleNamespace(
        bot=bot,
        author=author,
        voice_client=types.SimpleNamespace(channel=bot_channel) if bot_channel else None,
        sent=[],
    )

    async def send(*args, **kwargs):
        ctx.sent.append((args, kwargs))

    ctx.send = send
    return ctx


def run_same_voice(ctx):
    return asyncio.run(checks.require_same_voice().predicate(ctx))


CHANNEL_A = object()
CHANNEL_B = object()


# ---------------------------------------------------------------------------
# Flag-Defaults: beide Flags ungesetzt → kein Check aktiv
# ---------------------------------------------------------------------------

def test_same_voice_flag_off_is_noop(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_SAME_VOICE", False)
    # User in fremdem Channel, BoomBot: würde irgendein Check laufen, knallt es.
    ctx = make_ctx(BoomBot(), author_channel=CHANNEL_B, bot_channel=CHANNEL_A)
    assert run_same_voice(ctx) is True
    assert ctx.sent == []


def test_admin_flag_unset_is_noop(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ROLE_ID", 0)
    ctx = make_ctx(BoomBot())
    assert asyncio.run(checks.check_admin(ctx)) is True
    assert ctx.sent == []


def test_flag_parsing_defaults():
    assert config._parse_bool("") is False        # REQUIRE_SAME_VOICE ungesetzt
    assert config._parse_bool("false") is False
    assert config._parse_bool("true") is True
    assert config._parse_bool("1") is True
    assert config._parse_role_id("") == 0         # ADMIN_ROLE_ID ungesetzt
    assert config._parse_role_id("keinezahl") == 0
    assert config._parse_role_id("123456789") == 123456789


# ---------------------------------------------------------------------------
# REQUIRE_SAME_VOICE=true
# ---------------------------------------------------------------------------

def test_same_voice_same_channel_allowed(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_SAME_VOICE", True)
    # BoomBot: gleicher Channel muss VOR dem Owner-Check durchlassen.
    ctx = make_ctx(BoomBot(), author_channel=CHANNEL_A, bot_channel=CHANNEL_A)
    assert run_same_voice(ctx) is True
    assert ctx.sent == []


def test_same_voice_other_channel_denied(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_SAME_VOICE", True)
    ctx = make_ctx(FakeBot(), author_channel=CHANNEL_B, bot_channel=CHANNEL_A)
    assert run_same_voice(ctx) is False
    assert len(ctx.sent) == 1


def test_same_voice_not_in_voice_denied(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_SAME_VOICE", True)
    ctx = make_ctx(FakeBot(), author_channel=None, bot_channel=CHANNEL_A)
    assert run_same_voice(ctx) is False
    assert len(ctx.sent) == 1


def test_same_voice_bot_not_in_voice_allowed(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_SAME_VOICE", True)
    ctx = make_ctx(BoomBot(), author_channel=CHANNEL_B, bot_channel=None)
    assert run_same_voice(ctx) is True
    assert ctx.sent == []


def test_same_voice_owner_never_locked_out(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_SAME_VOICE", True)
    ctx = make_ctx(FakeBot(), author_channel=CHANNEL_B, bot_channel=CHANNEL_A)
    ctx.bot.owner = ctx.author
    assert run_same_voice(ctx) is True
    assert ctx.sent == []


# ---------------------------------------------------------------------------
# ADMIN_ROLE_ID gesetzt
# ---------------------------------------------------------------------------

ROLE_ID = 424242


def test_admin_role_member_allowed(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ROLE_ID", ROLE_ID)
    role = types.SimpleNamespace(id=ROLE_ID)
    # BoomBot: die Rolle muss VOR dem Owner-Check durchlassen.
    ctx = make_ctx(BoomBot(), roles=[role])
    assert asyncio.run(checks.check_admin(ctx)) is True
    assert ctx.sent == []


def test_admin_without_role_denied(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ROLE_ID", ROLE_ID)
    ctx = make_ctx(FakeBot(), roles=[types.SimpleNamespace(id=1)])
    assert asyncio.run(checks.check_admin(ctx)) is False
    assert len(ctx.sent) == 1


def test_admin_owner_never_locked_out(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ROLE_ID", ROLE_ID)
    ctx = make_ctx(FakeBot(), roles=[])
    ctx.bot.owner = ctx.author
    assert asyncio.run(checks.check_admin(ctx)) is True
    assert ctx.sent == []


# ---------------------------------------------------------------------------
# Die Checks hängen tatsächlich an den Commands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "skip", "stop", "clear", "eq", "seek", "now_playing", "remove", "move", "shuffle",
])
def test_same_voice_check_attached(cmd):
    assert len(getattr(MusicCommands, cmd).checks) == 1


@pytest.mark.parametrize("cmd", ["reloadcookies", "format"])
def test_admin_check_attached(cmd):
    assert len(getattr(MusicCommands, cmd).checks) == 1
