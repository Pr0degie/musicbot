"""Senderwechsel: der abgelöste Sender darf keinen Reconnect mehr anstoßen.

Der after-Callback eines gestoppten Radio-Players läuft im Player-Thread –
oft erst, wenn der neue Sender den Radio-Modus schon wieder eingeschaltet hat.
Früher hielt er das für einen Stream-Abbruch und startete einen Reconnect. Der
fand den neuen Sender spielend vor, meldete nach 3 s „Voice-Client noch
belegt" und setzte den Radio-Zustand zurück, obwohl der Sender weiterlief.
"""

import asyncio
import threading

import discord

from conftest import FakeCtx, build_cog, cleanup_cog, make_fake_source
from cogs.music import MusicCommands
from test_paused_player_discard import STATIONS, PausableVoiceClient, _write_station_file
from utils.i18n import t


class SwitchVoiceClient(PausableVoiceClient):
    """Wie der Player-Thread von discord.py: nach stop() meldet er zuerst das
    Wiedergabe-Ende, der Rest des after-Callbacks folgt später (fire_stopped)."""

    def __init__(self):
        super().__init__()
        self.on_stop = None

    def stop(self):
        super().stop()
        if self.on_stop:
            self.on_stop()


def _setup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())
    _write_station_file()


def _short_sleep(monkeypatch):
    """Die 2-s-Pause vor dem Reconnect auf 10 ms kürzen."""
    real_sleep = asyncio.sleep

    async def sleep(delay, *args, **kwargs):
        await real_sleep(min(delay, 0.01))

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return real_sleep


def test_senderwechsel_loest_keinen_reconnect_des_alten_senders_aus(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    async def run():
        vc = SwitchVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog()
        vc.on_stop = mc._playback_done.set
        reconnects = []

        async def reconnect(*args, **kwargs):
            reconnects.append(args)

        mc._reconnect_radio = reconnect

        await MusicCommands.radio_play.callback(mc, ctx, eingabe="1")
        await MusicCommands.radio_play.callback(mc, ctx, eingabe="2")   # Wechsel bei laufendem Sender
        assert vc.stop_calls == 1 and len(vc.play_calls) == 2
        assert mc.radio_station_name == "Zwei"

        # Der Thread des alten Senders kommt erst jetzt zu seinem Callback –
        # „Zwei" läuft bereits, is_radio ist wieder True.
        vc.fire_stopped()
        await asyncio.sleep(0.05)

        assert reconnects == [], "der abgelöste Sender darf keinen Reconnect anstoßen"
        assert mc._radio_reconnect_count == 0
        assert t("error.stream_busy") not in ctx.texts
        assert mc.is_radio is True and mc.is_playing is True
        assert mc.radio_station_name == "Zwei"
        assert mc.current_track[0] == STATIONS["zwei"]["url"]
        assert len(vc.play_calls) == 2
        cleanup_cog(mc)

    asyncio.run(run())


def test_abbruch_des_laufenden_senders_verbindet_weiterhin_neu(monkeypatch, tmp_path):
    """Bestehendes Verhalten: bricht der laufende Sender von selbst ab, wird
    genau dieser Sender neu verbunden."""
    _setup(monkeypatch, tmp_path)
    real_sleep = _short_sleep(monkeypatch)

    async def run():
        vc = SwitchVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog()
        await MusicCommands.radio_play.callback(mc, ctx, eingabe="1")

        vc.end_track()                      # Stream reißt ab
        for _ in range(100):
            if len(vc.play_calls) >= 2:
                break
            await real_sleep(0.01)

        assert len(vc.play_calls) == 2, "Reconnect hat den Sender neu gestartet"
        assert mc._radio_reconnect_count == 1
        assert t("radio.reconnecting", count=1, name="Eins") in ctx.texts
        assert mc.is_radio is True and mc.is_playing is True
        assert mc.radio_station_name == "Eins"
        cleanup_cog(mc)

    asyncio.run(run())


def test_wartender_reconnect_startet_nicht_ueber_einen_neuen_sender(monkeypatch, tmp_path):
    """Sender bricht ab, in der Pause vor dem Reconnect wählt jemand einen
    anderen: der wartende Reconnect lässt den neuen Sender in Ruhe."""
    _setup(monkeypatch, tmp_path)
    real_sleep = _short_sleep(monkeypatch)

    async def run():
        vc = SwitchVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog()
        await MusicCommands.radio_play.callback(mc, ctx, eingabe="1")

        vc.end_track()                      # Stream reißt ab → Reconnect wartet
        await MusicCommands.radio_play.callback(mc, ctx, eingabe="2")
        assert mc.radio_station_name == "Zwei" and len(vc.play_calls) == 2
        texts = list(ctx.texts)
        await real_sleep(0.1)               # der wartende Reconnect ist längst dran gewesen

        assert ctx.texts == texts, "keine Reconnect- oder Belegt-Meldung nach dem Wechsel"
        assert len(vc.play_calls) == 2
        assert mc.is_radio is True and mc.is_playing is True
        assert mc.radio_station_name == "Zwei"
        cleanup_cog(mc)

    asyncio.run(run())


def test_stream_ende_aus_dem_player_thread_laeuft_auf_dem_loop(monkeypatch, tmp_path):
    """Der after-Callback kommt aus dem Player-Thread von discord.py. Die
    Stream-Ende-Logik (Reconnect-Zähler, is_radio/is_playing) läuft trotzdem
    auf dem Event-Loop – wie das Track-Ende eines Songs."""
    _setup(monkeypatch, tmp_path)

    async def run():
        loop_thread = threading.get_ident()
        vc = SwitchVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog()
        logik = []
        reconnects = []
        echt = mc._radio_should_reconnect

        def spy_should_reconnect(ran_seconds):
            logik.append(threading.get_ident())
            return echt(ran_seconds)

        async def reconnect(*args, **kwargs):
            reconnects.append(threading.get_ident())

        mc._radio_should_reconnect = spy_should_reconnect
        mc._reconnect_radio = reconnect
        await MusicCommands.radio_play.callback(mc, ctx, eingabe="1")

        await asyncio.to_thread(vc.end_track)      # Player-Thread ruft after_radio
        await asyncio.sleep(0.05)

        assert logik == [loop_thread], "Stream-Ende-Logik gehört auf den Event-Loop"
        assert reconnects == [loop_thread], "Reconnect wurde genau einmal angestoßen"
        assert mc._radio_reconnect_count == 1
        assert mc._playback_done.is_set()
        cleanup_cog(mc)

    asyncio.run(run())
