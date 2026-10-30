"""Move- und Rauswurf-Schutz (cogs/music_voice_guard.py): nur der Owner darf
den Bot verschieben oder rauswerfen.

Discord kann beides nicht verbieten. Der Bot vergleicht deshalb, wo er ist,
mit dem Channel, den er selbst gewählt hat (utils/voice.py), und fragt das
Audit-Log, wer es war. War es nicht der Owner, geht er zurück.
"""
import asyncio
import datetime
import re
import types
from pathlib import Path

import discord
import pytest

import cogs.music_voice_guard as mg
import utils.voice as uv
from cogs.basic import BasicCommands
from tests.conftest import build_cog

BOT_ID = 42
OWNER = types.SimpleNamespace(id=1, display_name="Owner")
SEZGIN = types.SimpleNamespace(id=2, display_name="Sezgin")
ROOT = Path(__file__).resolve().parent.parent


def run(coro):
    return asyncio.run(coro)


class Channel:
    def __init__(self, cid, guild):
        self.id = cid
        self.name = f"vc{cid}"
        self.guild = guild

    async def connect(self, **kwargs):
        vc = VC(self.guild)
        vc.channel = self
        self.guild.voice_client = vc
        self.guild.me.voice.channel = self
        self.guild.connects.append(self.id)
        return vc


class Guild:
    def __init__(self, audit=True):
        self.id = 99
        self.channels = {}
        self.entries = []          # Audit-Log member_move, neueste zuerst
        self.kicks = []            # Audit-Log member_disconnect
        self.me = types.SimpleNamespace(
            voice=types.SimpleNamespace(channel=None),
            guild_permissions=types.SimpleNamespace(view_audit_log=audit),
        )
        self.voice_client = None
        self.connects = []

    def add(self, cid):
        ch = Channel(cid, self)
        self.channels[cid] = ch
        return ch

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def audit_logs(self, limit, action):
        quelle = self.kicks if action is discord.AuditLogAction.member_disconnect else self.entries
        for e in quelle[:limit]:
            yield e


class VC:
    def __init__(self, guild, playing=False):
        self.guild = guild
        self.moves = []
        self._playing = playing
        self.disconnected = False

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return False

    def stop(self):
        self._playing = False

    async def disconnect(self, force=False):
        self.disconnected = True

    def cleanup(self):
        if self.guild.voice_client is self:
            self.guild.voice_client = None

    async def move_to(self, channel):
        self.moves.append(channel.id)
        self.guild.me.voice.channel = channel


def entry(eid, user, channel, count=1, age=0):
    return types.SimpleNamespace(
        id=eid, user=user,
        extra=types.SimpleNamespace(channel=channel, count=count),
        created_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age),
    )


class TextChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text, **kwargs):
        self.sent.append(text)


def state(channel):
    return types.SimpleNamespace(channel=channel, mute=False)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(mg.config, "MOVE_GUARD", True)
    monkeypatch.setattr(mg.config, "MUTE_GUARD", False)
    monkeypatch.setattr(uv, "_intended", {})
    waits = []

    async def fake_sleep(sec):
        waits.append(sec)

    monkeypatch.setattr(mg.asyncio, "sleep", fake_sleep)
    return waits


def make(audit=True):
    mc = build_cog()

    async def is_owner(user):
        return user.id == OWNER.id

    mc.bot.user = types.SimpleNamespace(id=BOT_ID)
    mc.bot.is_owner = is_owner
    mc.bot.dispatched = []
    mc.bot.dispatch = mc.bot.dispatched.append
    mc.text_channel = TextChannel()
    g = Guild(audit)
    home, other = g.add(10), g.add(20)
    g.voice_client = VC(g)
    me = types.SimpleNamespace(id=BOT_ID, guild=g)
    uv.set_intended_channel(home)
    g.me.voice.channel = home
    return mc, g, me, home, other


async def moved(mc, g, me, src, dst):
    g.me.voice.channel = dst
    await mc._move_guard(me, state(src), state(dst))


def test_fremder_move_wird_rueckgaengig_gemacht():
    mc, g, me, home, other = make()
    g.entries = [entry(1, SEZGIN, other)]
    run(moved(mc, g, me, home, other))
    assert g.voice_client.moves == [home.id]
    assert g.me.voice.channel is home
    assert uv.intended_channel_id(g.id) == home.id
    assert len(mc.text_channel.sent) == 1


def test_owner_darf_verschieben():
    mc, g, me, home, other = make()
    g.entries = [entry(1, OWNER, other)]
    run(moved(mc, g, me, home, other))
    assert g.voice_client.moves == []
    assert uv.intended_channel_id(g.id) == other.id


def test_zusammengefasster_owner_eintrag_wird_ueber_den_zaehler_erkannt():
    mc, g, me, home, other = make()
    old = entry(1, OWNER, other, count=2, age=600)
    g.entries = [old]
    run(mc._move_guard(me, state(None), state(home)))   # Beitritt merkt den Stand
    assert mc._audit_seen["member_move"] == {1: 2}
    old.extra.count = 3                                   # Owner zieht erneut
    run(moved(mc, g, me, home, other))
    assert g.voice_client.moves == []


def test_alter_eintrag_ohne_neuen_zaehler_gilt_nicht_als_owner():
    mc, g, me, home, other = make()
    g.entries = [entry(1, OWNER, other, count=2, age=600)]
    mc._audit_seen = {"member_move": {1: 2}}
    run(moved(mc, g, me, home, other))
    assert g.voice_client.moves == [home.id]


def test_ohne_audit_recht_immer_zurueck():
    mc, g, me, home, other = make(audit=False)
    g.entries = [entry(1, OWNER, other)]
    run(moved(mc, g, me, home, other))
    assert g.voice_client.moves == [home.id]


def test_eigener_wechsel_wird_nicht_zurueckgedreht():
    mc, g, me, home, other = make()
    run(uv.move_voice(g.voice_client, other))
    run(mc._move_guard(me, state(home), state(other)))
    assert g.voice_client.moves == [other.id]
    assert mc.text_channel.sent == []


def test_flag_aus_laesst_alles_zu(monkeypatch):
    mc, g, me, home, other = make()
    monkeypatch.setattr(mg.config, "MOVE_GUARD", False)
    g.entries = [entry(1, SEZGIN, other)]
    run(moved(mc, g, me, home, other))
    assert g.voice_client.moves == []


def test_move_krieg_wartet_ab_dem_vierten_und_meldet_nur_einmal(setup):
    mc, g, me, home, other = make(audit=False)
    for _ in range(mg.FAST_UNMUTES + 2):
        run(moved(mc, g, me, home, other))
    assert g.voice_client.moves == [home.id] * (mg.FAST_UNMUTES + 2)
    assert setup.count(mg.BACKOFF_SECONDS) == 2
    assert len(mc.text_channel.sent) == 1


class JoinCtx:
    def __init__(self, author, author_channel, vc):
        self.author = types.SimpleNamespace(id=author.id, voice=types.SimpleNamespace(channel=author_channel))
        self.voice_client = vc
        self.sent = []

    async def send(self, text, **kwargs):
        self.sent.append(text)


def _basic():
    async def is_owner(user):
        return user.id == OWNER.id
    return BasicCommands(types.SimpleNamespace(is_owner=is_owner))


def test_j_von_anderen_verschiebt_nicht():
    _, g, _, home, other = make()
    g.voice_client.channel = home
    ctx = JoinCtx(SEZGIN, other, g.voice_client)
    run(BasicCommands.join.callback(_basic(), ctx))
    assert g.voice_client.moves == []
    assert ctx.sent == [mg.t("error.move_owner_only")]


def test_j_vom_owner_verschiebt_und_merkt_den_channel():
    _, g, _, home, other = make()
    g.voice_client.channel = home
    ctx = JoinCtx(OWNER, other, g.voice_client)
    run(BasicCommands.join.callback(_basic(), ctx))
    assert g.voice_client.moves == [other.id]
    assert uv.intended_channel_id(g.id) == other.id


def test_kein_rohes_move_to_ausserhalb_von_utils_voice():
    """Jeder Channel-Wechsel muss über move_voice laufen, sonst hielte der
    Move-Schutz ihn für fremd und drehte ihn zurück."""
    roh = re.compile(r"\.move_to\(")
    treffer = []
    for muster in ("cogs/*.py", "views/*.py", "utils/*.py"):
        for pfad in ROOT.glob(muster):
            if pfad.name == "voice.py" and pfad.parent.name == "utils":
                continue
            for nr, zeile in enumerate(pfad.read_text(encoding="utf-8").splitlines(), 1):
                if roh.search(zeile):
                    treffer.append(f"{pfad.relative_to(ROOT)}:{nr}")
    assert treffer == []


# --- Rauswurf --------------------------------------------------------------


async def kicked_setup(audit=True, playing=True):
    """Wie make(), aber im laufenden Loop (_playback_done braucht ihn) und mit
    gestubbtem play_next – geprüft wird nur, dass weitergespielt wird."""
    mc, g, me, home, other = make(audit)
    mc._playback_done = asyncio.Event()
    mc._playback_done.set()
    g.voice_client = VC(g, playing=playing)
    g.voice_client.channel = home
    mc.current_track = ("u1", "Song", 200)
    mc.track_start_time = 0.0
    mc.is_playing = playing
    calls = []

    async def play_next(ctx):
        calls.append(list(mc.queue))
    mc.play_next = play_next
    mc._last_ctx = types.SimpleNamespace(voice_client=g.voice_client, send=mc.text_channel.send)
    return mc, g, me, home, calls


async def kick(mc, g, me, home):
    g.me.voice.channel = None
    await mc._move_guard(me, state(home), state(None))


def test_rauswurf_kommt_zurueck_und_spielt_an_der_stelle_weiter(monkeypatch):
    async def go():
        mc, g, me, home, calls = await kicked_setup()
        monkeypatch.setattr(mc, "_elapsed_seconds", lambda: 61.0)
        g.kicks = [entry(5, SEZGIN, None)]
        await kick(mc, g, me, home)
        return mc, g, home, calls
    mc, g, home, calls = run(go())
    assert g.connects == [home.id]
    assert calls == [[("u1", "Song")]]       # aktueller Song vorn, mit Position
    assert mc._seek_offset == 60
    assert not mc._voice_healing
    assert mc.text_channel.sent == [mg.t("status.kick_reverted", channel=home.name)]
    assert mc.bot.dispatched == ["voice_rebuilt"]


def test_rauswurf_durch_owner_bleibt_draussen():
    async def go():
        mc, g, me, home, calls = await kicked_setup()
        g.kicks = [entry(5, OWNER, None)]
        await kick(mc, g, me, home)
        return mc, g, calls
    mc, g, calls = run(go())
    assert g.connects == [] and calls == []
    assert list(mc.queue) == [] and mc._seek_offset == 0
    assert uv.intended_channel_id(g.id) is None
    assert not mc._voice_healing


def test_ohne_audit_recht_kommt_er_immer_zurueck():
    async def go():
        mc, g, me, home, calls = await kicked_setup(audit=False)
        g.kicks = [entry(5, OWNER, None)]
        await kick(mc, g, me, home)
        return g, home
    g, home = run(go())
    assert g.connects == [home.id]


def test_eigener_abgang_wird_nicht_rueckgaengig_gemacht():
    async def go():
        mc, g, me, home, calls = await kicked_setup()
        await uv.disconnect_voice(g.voice_client)
        await kick(mc, g, me, home)
        return g
    assert run(go()).connects == []


def test_waehrend_der_reparatur_kein_rauswurf_schutz():
    async def go():
        mc, g, me, home, calls = await kicked_setup()
        mc._voice_healing = True
        await kick(mc, g, me, home)
        return g
    assert run(go()).connects == []


def test_l_von_anderen_schickt_nicht_weg():
    _, g, _, home, _ = make()
    ctx = JoinCtx(SEZGIN, home, g.voice_client)
    run(BasicCommands.leave.callback(_basic(), ctx))
    assert not g.voice_client.disconnected
    assert ctx.sent == [mg.t("error.leave_owner_only")]


def test_l_vom_owner_geht_und_vergisst_den_channel():
    _, g, _, home, _ = make()
    ctx = JoinCtx(OWNER, home, g.voice_client)
    run(BasicCommands.leave.callback(_basic(), ctx))
    assert g.voice_client.disconnected
    assert uv.intended_channel_id(g.id) is None


def test_kein_rohes_disconnect_ausserhalb_von_utils_voice():
    """Eigene Abgänge müssen über disconnect_voice laufen, sonst hielte der
    Rauswurf-Schutz sie für fremd und käme zurück."""
    roh = re.compile(r"\.disconnect\(")
    treffer = []
    for muster in ("cogs/*.py", "views/*.py", "utils/*.py"):
        for pfad in ROOT.glob(muster):
            if pfad.name == "voice.py" and pfad.parent.name == "utils":
                continue
            for nr, zeile in enumerate(pfad.read_text(encoding="utf-8").splitlines(), 1):
                if roh.search(zeile):
                    treffer.append(f"{pfad.relative_to(ROOT)}:{nr}")
    assert treffer == []
