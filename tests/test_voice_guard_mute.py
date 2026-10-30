"""Mute-Schutz (cogs/music_voice_guard.py).

Ein Server-Mute bleibt am Bot-Account hängen, auch über Rejoin hinweg. Der Bot
entmutet sich deshalb selbst und gibt nie auf. Nur ohne Recht pausiert er
und spielt weiter, sobald ihn jemand anderes entmutet.
"""
import asyncio
import types

import discord
import pytest

import cogs.music_voice_guard as mg
from tests.conftest import build_cog

BOT_ID = 42


def run(coro):
    return asyncio.run(coro)


class Channel:
    def __init__(self, may_mute=True):
        self.may_mute = may_mute

    def permissions_for(self, member):
        return types.SimpleNamespace(mute_members=self.may_mute)


class TextChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text, **kwargs):
        self.sent.append(text)


class VC:
    def __init__(self, playing=True):
        self._playing = playing
        self._paused = False

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return self._paused

    def pause(self):
        self._playing, self._paused = False, True

    def resume(self):
        self._playing, self._paused = True, False


class Member:
    def __init__(self, vc=None, member_id=BOT_ID, fail=None):
        self.id = member_id
        self.guild = types.SimpleNamespace(voice_client=vc)
        self.edits = []
        self.fail = fail
        self.voice = types.SimpleNamespace(mute=True)

    async def edit(self, **kwargs):
        if self.fail:
            raise self.fail
        self.edits.append(kwargs)
        self.voice.mute = kwargs.get("mute", self.voice.mute)


def state(channel=None, mute=False):
    return types.SimpleNamespace(channel=channel, mute=mute)


def make(vc=None, **member_kw):
    mc = build_cog()
    mc.bot.user = types.SimpleNamespace(id=BOT_ID)
    mc.text_channel = TextChannel()
    mc.is_playing = vc is not None and vc.is_playing()
    return mc, Member(vc, **member_kw)


async def mute(mc, member, ch):
    await mc._mute_guard(member, state(ch), state(ch, mute=True))


async def unmute(mc, member, ch):
    await mc._mute_guard(member, state(ch, mute=True), state(ch))


@pytest.fixture(autouse=True)
def guard_on(monkeypatch):
    monkeypatch.setattr(mg.config, "MUTE_GUARD", True)


def test_server_mute_wird_sofort_aufgehoben():
    vc = VC()
    mc, me = make(vc)
    ch = Channel()
    run(mute(mc, me, ch))
    assert me.edits == [{"mute": False, "reason": "Mute-Schutz"}]
    assert vc.is_playing()
    assert mc.text_channel.sent == []


def test_eigenes_entmuten_loest_nichts_aus():
    vc = VC()
    mc, me = make(vc)
    ch = Channel()
    run(mute(mc, me, ch))
    run(unmute(mc, me, ch))   # das Echo unseres member.edit
    assert vc.is_playing()
    assert mc.text_channel.sent == []


def test_gemutet_beigetreten_wird_entmutet():
    mc, me = make()
    ch = Channel()
    run(mc._mute_guard(me, state(None), state(ch, mute=True)))
    assert me.edits == [{"mute": False, "reason": "Mute-Schutz"}]


def test_andere_member_und_flag_aus_werden_ignoriert(monkeypatch):
    mc, me = make()
    ch = Channel()
    run(mute(mc, Member(member_id=7), ch))
    monkeypatch.setattr(mg.config, "MUTE_GUARD", False)
    run(mute(mc, me, ch))
    assert me.edits == []


@pytest.fixture
def no_sleep(monkeypatch):
    waits = []

    async def fake_sleep(sec):
        waits.append(sec)

    monkeypatch.setattr(mg.asyncio, "sleep", fake_sleep)
    return waits


async def mute_war(mc, me, ch, n):
    for _ in range(n):
        me.voice.mute = True
        await mute(mc, me, ch)
        await unmute(mc, me, ch)


def test_mute_krieg_gibt_nie_auf_wartet_nur(no_sleep):
    vc = VC()
    mc, me = make(vc)
    ch = Channel()
    run(mute_war(mc, me, ch, mg.FAST_UNMUTES + 5))
    assert len(me.edits) == mg.FAST_UNMUTES + 5    # jedes Mal entmutet
    assert no_sleep == [mg.BACKOFF_SECONDS] * 5     # erst ab dem 4. gewartet
    assert vc.is_playing() and mc.text_channel.sent == []


def test_waehrend_der_wartezeit_entmutet_kein_doppeltes_edit(no_sleep):
    mc, me = make()
    ch = Channel()
    run(mute_war(mc, me, ch, mg.FAST_UNMUTES))
    me.voice.mute = False   # ein Mensch war schneller
    run(mute(mc, me, ch))
    assert len(me.edits) == mg.FAST_UNMUTES


def test_alte_entmutungen_verfallen(monkeypatch, no_sleep):
    mc, me = make()
    ch = Channel()
    clock = [1000.0]
    monkeypatch.setattr(mg.time, "monotonic", lambda: clock[0])
    run(mute_war(mc, me, ch, mg.FAST_UNMUTES))
    clock[0] += mg.WINDOW_SECONDS + 1
    run(mute_war(mc, me, ch, 1))
    assert no_sleep == []


def test_ohne_recht_pausieren_statt_ins_leere_spielen():
    vc = VC()
    mc, me = make(vc)
    ch = Channel(may_mute=False)
    run(mute(mc, me, ch))
    assert me.edits == []
    assert vc.is_paused()
    assert len(mc.text_channel.sent) == 1
    run(unmute(mc, me, ch))
    assert vc.is_playing()


def test_forbidden_beim_entmuten_pausiert():
    vc = VC()
    resp = types.SimpleNamespace(status=403, reason="Forbidden")
    mc, me = make(vc, fail=discord.Forbidden(resp, "Missing Permissions"))
    run(mute(mc, me, Channel()))
    assert vc.is_paused() and mc._mute_blocked


def test_anderer_http_fehler_pausiert_nicht():
    vc = VC()
    resp = types.SimpleNamespace(status=400, reason="Bad Request")
    mc, me = make(vc, fail=discord.HTTPException(resp, "Target user is not connected to voice."))
    run(mute(mc, me, Channel()))
    assert vc.is_playing() and not mc._mute_blocked


def test_radio_wird_nicht_pausiert():
    vc = VC()
    mc, me = make(vc)
    mc.is_radio = True
    run(mute(mc, me, Channel(may_mute=False)))
    assert vc.is_playing()
    assert len(mc.text_channel.sent) == 1


def test_manuelle_pause_wird_bei_unmute_nicht_aufgehoben():
    vc = VC()
    vc.pause()               # Nutzer hatte schon mit !x pausiert
    mc, me = make(vc)
    ch = Channel(may_mute=False)
    run(mute(mc, me, ch))
    run(unmute(mc, me, ch))
    assert vc.is_paused()
