"""Voice-Reparatur nach Gateway-Abbruch (cogs/music_voice_ui.py).

Fällt die Verbindung zu Discord weg, baut discord.py das Gateway selbst neu
auf – die Sprachverbindung bleibt aber stumm zurück. Die Reparatur greift nur
nach einem echten Abbruch (Gateway tot still) oder einer neuen Sitzung, nie
beim routinemäßigen stündlichen Reconnect: der läuft ohne Tonausfall.
"""
import asyncio
import time
import types

import cogs.music_voice_ui as mvu
from cogs.voice_listen import VoiceListen
from tests.conftest import FakeCtx, build_cog, cleanup_cog


def run(coro):
    return asyncio.run(coro)


class HealVoiceClient:
    """Voice-Client mit genau der Oberfläche, die die Reparatur anfasst."""

    def __init__(self, channel, playing=True, paused=False):
        self.channel = channel
        self.guild = channel.guild
        self._playing = playing
        self._paused = paused
        self.disconnects = 0
        self.paused_calls = 0
        self.after = None

    def is_connected(self):
        return True

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return self._paused

    def stop(self):
        self._playing = False
        self._paused = False
        if self.after:
            after, self.after = self.after, None
            after(None)

    def pause(self):
        self._playing = False
        self._paused = True
        self.paused_calls += 1

    async def disconnect(self, force=False):
        self.disconnects += 1
        if self.guild.voice_client is self:
            self.guild.voice_client = None

    def cleanup(self):
        if self.guild.voice_client is self:
            self.guild.voice_client = None


def make_world():
    guild = types.SimpleNamespace(id=1, voice_client=None)
    channel = types.SimpleNamespace(id=42, name="Allgemein", guild=guild)
    return guild, channel


def heal_cog(guild, channel, monkeypatch):
    mc = build_cog()
    dispatched = []
    mc.bot = types.SimpleNamespace(
        loop=asyncio.get_running_loop(),
        dm_speaking=False,
        voice_clients=[],
        get_channel=lambda cid: channel if cid == channel.id else None,
        dispatch=lambda name, *a: dispatched.append(name),
    )
    mc._dispatched = dispatched
    mc._gw_last_recv = time.monotonic()
    mc._gw_zombie = False
    mc._gw_ready_seen = True
    mc._heal_candidates = {}
    mc._heal_task = None
    mc.radio_stream_url = None
    mc.radio_station_name = None

    connects = []

    async def fake_connect(ch):
        new = HealVoiceClient(ch, playing=False)
        ch.guild.voice_client = new
        connects.append(new)
        return new

    monkeypatch.setattr(mvu, "connect_voice", fake_connect)
    mc._connects = connects

    plays = []

    async def fake_play_next(ctx):
        if mc._voice_healing:   # wie der echte Guard
            return
        plays.append(list(mc.queue))
        vc = ctx.voice_client
        if vc is not None:
            vc._playing = True

    mc.play_next = fake_play_next
    mc._plays = plays
    return mc


# --- Wann repariert wird ----------------------------------------------------

def test_routine_reconnect_repariert_nicht(monkeypatch):
    """Discord verlangt etwa stündlich einen Reconnect – der darf den Song nicht
    unterbrechen."""
    async def body():
        guild, channel = make_world()
        mc = heal_cog(guild, channel, monkeypatch)
        vc = HealVoiceClient(channel)
        guild.voice_client = vc
        mc.bot.voice_clients = [vc]
        mc._gw_last_recv = time.monotonic() - 5   # gerade noch ein ACK bekommen
        await mc.on_disconnect()
        await mc.on_resumed()
        assert mc._heal_task is None
        assert vc.disconnects == 0
        cleanup_cog(mc)
    run(body())


def test_abbruch_mit_resume_repariert(monkeypatch):
    async def body():
        guild, channel = make_world()
        mc = heal_cog(guild, channel, monkeypatch)
        vc = HealVoiceClient(channel)
        guild.voice_client = vc
        mc.bot.voice_clients = [vc]
        mc._last_ctx = FakeCtx(vc)
        mc._gw_last_recv = time.monotonic() - 61  # "stopped responding"
        await mc.on_disconnect()
        await mc.on_resumed()
        assert mc._heal_task is not None
        await mc._heal_task
        assert vc.disconnects == 1
        assert len(mc._connects) == 1
        cleanup_cog(mc)
    run(body())


def test_neue_sitzung_repariert_auch_ohne_stille(monkeypatch):
    """Neue Sitzung = discord.py hat seine Voice-Clients vergessen; der alte
    läuft verwaist weiter. Mehrere disconnect-Events während des Ausfalls
    dürfen den gemerkten Client nicht überschreiben."""
    async def body():
        guild, channel = make_world()
        mc = heal_cog(guild, channel, monkeypatch)
        vc = HealVoiceClient(channel)
        guild.voice_client = vc
        mc.bot.voice_clients = [vc]
        await mc.on_disconnect()
        mc.bot.voice_clients = []        # parse_ready hat die Liste geleert
        guild.voice_client = None
        await mc.on_disconnect()         # weiterer Fehlversuch
        await mc.on_ready()
        await mc._heal_task
        assert vc.disconnects == 1
        assert len(mc._connects) == 1
        cleanup_cog(mc)
    run(body())


def test_erstes_on_ready_ist_kein_reconnect(monkeypatch):
    async def body():
        guild, channel = make_world()
        mc = heal_cog(guild, channel, monkeypatch)
        mc._gw_ready_seen = False
        mc._heal_candidates = {1: HealVoiceClient(channel)}
        await mc.on_ready()
        assert mc._heal_task is None
        cleanup_cog(mc)
    run(body())


# --- Was die Reparatur tut ---------------------------------------------------

def test_song_laeuft_an_der_hoerposition_weiter(monkeypatch):
    async def body():
        guild, channel = make_world()
        mc = heal_cog(guild, channel, monkeypatch)
        old = HealVoiceClient(channel)
        guild.voice_client = old
        mc._last_ctx = FakeCtxGuild(guild)
        mc.current_track = ("u1", "Song", 300)
        mc.queue.append(("u2", "Danach"))
        mc.is_playing = True
        mc._current_seek_offset = 20             # Song lief per !seek ab 0:20
        mc.track_start_time = time.monotonic() - 100

        await mc._heal_one(old, "test")

        assert old.disconnects == 1
        assert mc._plays == [[("u1", "Song"), ("u2", "Danach")]]
        assert 117 <= mc._seek_offset <= 120     # 20 + 100 − 1 s Überlappung
        assert mc._voice_healing is False
        assert "voice_rebuilt" in mc._dispatched
        assert any("2:00" in text and "Song" in text for text in mc._last_ctx.texts), mc._last_ctx.texts
        cleanup_cog(mc)
    run(body())


def test_pausierter_song_bleibt_pausiert(monkeypatch):
    async def body():
        guild, channel = make_world()
        mc = heal_cog(guild, channel, monkeypatch)
        old = HealVoiceClient(channel, playing=False, paused=True)
        guild.voice_client = old
        mc._last_ctx = FakeCtxGuild(guild)
        mc.current_track = ("u1", "Song", 300)
        mc.track_start_time = time.monotonic() - 30
        mc._np_paused_at = time.monotonic() - 10

        await mc._heal_one(old, "test")

        new = guild.voice_client
        assert new.paused_calls == 1
        assert mc._stopped_by_user is True
        assert mc.is_playing is False
        cleanup_cog(mc)
    run(body())


def test_bot_still_im_channel_verbindet_nur_neu(monkeypatch):
    async def body():
        guild, channel = make_world()
        mc = heal_cog(guild, channel, monkeypatch)
        old = HealVoiceClient(channel, playing=False)
        guild.voice_client = old
        mc._last_ctx = FakeCtxGuild(guild)

        await mc._heal_one(old, "test")

        assert len(mc._connects) == 1
        assert mc._plays == []
        cleanup_cog(mc)
    run(body())


def test_play_next_wartet_waehrend_der_reparatur():
    """after_playing des alten Clients ruft play_next – das darf nicht den
    nächsten Song ziehen, solange neu verbunden wird."""
    async def body():
        mc = build_cog()
        mc.queue.append(("u1", "Song"))
        mc._voice_healing = True
        await mc.play_next(FakeCtx())
        assert list(mc.queue) == [("u1", "Song")]
        cleanup_cog(mc)
    run(body())


class FakeCtxGuild(FakeCtx):
    """ctx.voice_client wie bei discord.py: live über die Guild aufgelöst."""

    def __init__(self, guild):
        super().__init__()
        self._guild = guild

    @property
    def voice_client(self):
        return self._guild.voice_client

    @voice_client.setter
    def voice_client(self, _):
        pass


# --- Sprachsteuerung ----------------------------------------------------------

def test_empfaenger_wird_an_neuen_client_gehaengt():
    async def body():
        cog = VoiceListen.__new__(VoiceListen)
        calls = []
        cog._own_listening = True

        async def stop(release_model=False):
            calls.append("stop")
            cog._own_listening = False

        async def start():
            calls.append("start")
            cog._own_listening = True
            return True

        cog._stop_own_listening = stop
        cog._start_own_listening = start
        await cog.on_voice_rebuilt()
        assert calls == ["stop", "start"]
    run(body())


def test_ohne_eigenes_zuhoeren_passiert_nichts():
    async def body():
        cog = VoiceListen.__new__(VoiceListen)
        cog._own_listening = False
        cog._stop_own_listening = None   # würde beim Aufruf crashen
        await cog.on_voice_rebuilt()
    run(body())


# --- Ursache: voice_recv stürzt beim Abmelden eines Users ab ----------------

class _BuggyRecvClient:
    """Bildet voice_recv 0.5.2a179 nach: _remove_ssrc ohne _reader-Prüfung."""

    def __init__(self, reader=None):
        self._reader = reader
        self._id_to_ssrc = {7: 1234}
        self._ssrc_to_id = {1234: 7}

    def _remove_ssrc(self, *, user_id):
        ssrc = self._id_to_ssrc.pop(user_id, None)
        if ssrc:
            self._reader.speaking_timer.drop_ssrc(ssrc)
            self._ssrc_to_id.pop(ssrc, None)


def test_user_verlaesst_channel_ohne_zuhoeren_crasht_nicht():
    """Ohne !listen on gibt es keinen _reader – das Original wirft hier
    AttributeError und reißt damit den Voice-Poller von discord.py mit."""
    from utils.voice import _make_safe_recv_cls

    vc = _make_safe_recv_cls(_BuggyRecvClient)(reader=None)
    vc._remove_ssrc(user_id=7)
    assert vc._id_to_ssrc == {} and vc._ssrc_to_id == {}


def test_beim_zuhoeren_bleibt_das_originalverhalten():
    from utils.voice import _make_safe_recv_cls

    dropped = []
    reader = types.SimpleNamespace(
        speaking_timer=types.SimpleNamespace(drop_ssrc=dropped.append))
    vc = _make_safe_recv_cls(_BuggyRecvClient)(reader=reader)
    vc._remove_ssrc(user_id=7)
    assert dropped == [1234]


def test_echte_voice_recv_klasse_wird_repariert():
    """Gegen das installierte Paket: die Klasse für connect() ist die
    reparierte Unterklasse, und das Original hat die Lücke noch (sonst kann
    die Unterklasse weg)."""
    import inspect

    import pytest

    voice_recv = pytest.importorskip("discord.ext.voice_recv")
    from utils.voice import _recv_client_cls

    cls = _recv_client_cls()
    assert issubclass(cls, voice_recv.VoiceRecvClient)
    assert cls._remove_ssrc is not voice_recv.VoiceRecvClient._remove_ssrc
    original = inspect.getsource(voice_recv.VoiceRecvClient._remove_ssrc)
    if "if self._reader" in original:
        pytest.fail("voice_recv prüft _reader inzwischen selbst – "
                    "SafeVoiceRecvClient in utils/voice.py kann entfallen.")


# --- Sicherheitsnetz: toter Voice-Poller ------------------------------------

def _vc_mit_runner(runner, connected=True):
    vc = types.SimpleNamespace(_connection=types.SimpleNamespace(_runner=runner))
    vc.is_connected = lambda: connected
    return vc


def test_toter_poller_wird_erkannt():
    from utils.voice import dead_voice_poller

    async def body():
        async def crash():
            raise AttributeError("'_MissingSentinel' object has no attribute 'speaking_timer'")
        task = asyncio.ensure_future(crash())
        await asyncio.sleep(0)
        assert "speaking_timer" in dead_voice_poller(_vc_mit_runner(task))

        async def forever():
            await asyncio.sleep(3600)
        alive = asyncio.ensure_future(forever())
        await asyncio.sleep(0)
        assert dead_voice_poller(_vc_mit_runner(alive)) is None
        alive.cancel()
        # getrennt: ein beendeter Poller ist dann normal
        assert dead_voice_poller(_vc_mit_runner(task, connected=False)) is None
        # andere discord.py-Version ohne _runner → nichts tun
        assert dead_voice_poller(types.SimpleNamespace(is_connected=lambda: True)) is None
    run(body())


def test_watchdog_baut_tote_verbindung_neu_auf(monkeypatch):
    async def body():
        guild, channel = make_world()
        mc = heal_cog(guild, channel, monkeypatch)
        old = HealVoiceClient(channel)
        guild.voice_client = old
        mc.bot.voice_clients = [old]
        mc._last_ctx = FakeCtxGuild(guild)
        monkeypatch.setattr(mvu, "dead_voice_poller",
                            lambda vc: "AttributeError: x" if vc is old else None)

        await mc._voice_watchdog.coro(mc)
        await mc._heal_task

        assert old.disconnects == 1
        assert len(mc._connects) == 1
        cleanup_cog(mc)
    run(body())
