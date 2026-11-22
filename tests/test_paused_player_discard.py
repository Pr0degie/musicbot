"""Pausiert, dann neuer Start: der pausierte Player wird sauber gestoppt.

discord.py ersetzt in VoiceClient.play() einen pausierten Player, ohne ihn zu
stoppen (is_playing() ist bei Pause False) – Player-Thread und FFmpeg-Prozess
bleiben dann für immer hängen. play_next stoppt den pausierten Player deshalb
selbst, und dessen after-Callback darf weder den alten Track wieder einreihen
noch einen zweiten play_next auslösen.
"""

import asyncio
import json

import discord

from conftest import FakeCtx, FakeDownloader, build_cog, cleanup_cog, make_fake_source
from cogs.music import MusicCommands

URL_A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
URL_B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"


def _info(title):
    return {"title": title, "webpage_url": "https://www.youtube.com/watch?v=x",
            "thumbnail": None, "uploader": None, "duration": 200}


class PausableVoiceClient:
    """Bildet das Pause-Verhalten von discord.VoiceClient nach: play() auf
    einem pausierten Client ersetzt den Player, ohne ihn zu stoppen."""

    def __init__(self):
        self.play_calls = []
        self.stop_calls = 0
        self.leaked = []           # after-Callbacks ersetzter, nie gestoppter Player
        self.stopped_afters = []   # after-Callbacks gestoppter Player (feuert der Thread später)
        self._after = None
        self._state = "idle"

    def is_connected(self):
        return True

    def is_playing(self):
        return self._state == "playing"

    def is_paused(self):
        return self._state == "paused"

    def pause(self):
        if self._state == "playing":
            self._state = "paused"

    def play(self, source, after=None):
        if self.is_playing():
            raise discord.ClientException("Already playing audio.")
        if self._after is not None:
            self.leaked.append(self._after)   # genau hier leakt discord.py Thread + FFmpeg
        self._state = "playing"
        self._after = after
        self.play_calls.append(source)

    def stop(self):
        self.stop_calls += 1
        if self._after is not None:
            self.stopped_afters.append(self._after)
            self._after = None
        self._state = "idle"

    def fire_stopped(self):
        """Der Player-Thread eines gestoppten Players ruft seinen after-Callback."""
        pending, self.stopped_afters = self.stopped_afters, []
        for after in pending:
            after(None)

    def end_track(self):
        """Natürliches Ende des laufenden Tracks."""
        self._state = "idle"
        after, self._after = self._after, None
        after(None)


class _PerUrlDownloader(FakeDownloader):
    def __init__(self, results):
        super().__init__()
        self.results = results

    async def resolve_track(self, url, title, force_download=False, min_buffer_seconds=0):
        self.resolve_calls.append(url)
        await asyncio.sleep(0)
        result = self.results[url]
        if isinstance(result, BaseException):
            raise result
        return result


async def _spin(n=30):
    for _ in range(n):
        await asyncio.sleep(0)


def test_neuer_start_stoppt_pausierten_player_ohne_requeue(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())

    async def run():
        dl = _PerUrlDownloader({
            URL_A: (_info("A"), "https://cdn.example/a", "A", 200),
            URL_B: (_info("B"), "https://cdn.example/b", "B", 200),
        })
        vc = PausableVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog(dl)
        mc.loop_mode = "song"      # härtester Fall: der Loop-Zweig würde A wieder einreihen
        mc.queue.append((URL_A, "A"))
        await mc.play_next(ctx)

        await MusicCommands.pause.callback(mc, ctx)   # !x
        assert vc.is_paused() and mc.is_playing is False

        # !p B bei pausiertem Client: einreihen und starten.
        mc.queue.append((URL_B, "B"))
        mc.is_playing = True
        await mc.play_next(ctx)

        assert vc.leaked == [], "pausierter Player wurde ersetzt statt gestoppt (Thread + FFmpeg leaken)"
        assert vc.stop_calls == 1
        assert mc.current_track[0] == URL_B
        assert len(vc.play_calls) == 2

        # Der Thread des gestoppten Players ruft seinen after-Callback erst jetzt
        # – B läuft bereits.
        vc.fire_stopped()
        await _spin()

        assert list(mc.queue) == [], "A darf nicht wieder eingereiht werden"
        assert dl.resolve_calls == [URL_A, URL_B], "kein zweiter play_next"
        assert len(vc.play_calls) == 2
        assert mc.current_track[0] == URL_B
        assert not mc._playback_done.is_set(), "B läuft – das Ende-Signal darf nicht vom alten Player kommen"
        assert mc._suppress_resume is False

        # Der neue Track verhält sich danach ganz normal (Loop: B läuft erneut).
        vc.end_track()
        for _ in range(200):
            if len(vc.play_calls) >= 3:
                break
            await asyncio.sleep(0.01)
        assert len(vc.play_calls) == 3
        assert mc.current_track[0] == URL_B
        cleanup_cog(mc)

    asyncio.run(run())


def test_gescheiterter_neuer_start_laesst_pause_unberuehrt(monkeypatch, tmp_path):
    """Lässt sich der neue Track nicht auflösen, bleibt der pausierte Song
    pausiert (!resume funktioniert weiter) – wie bisher."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())

    async def run():
        dl = _PerUrlDownloader({
            URL_A: (_info("A"), "https://cdn.example/a", "A", 200),
            URL_B: asyncio.TimeoutError(),
        })
        vc = PausableVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog(dl)
        mc.queue.append((URL_A, "A"))
        await mc.play_next(ctx)
        await MusicCommands.pause.callback(mc, ctx)

        mc.queue.append((URL_B, "B"))
        mc.is_playing = True
        await mc.play_next(ctx)
        await _spin()

        assert vc.stop_calls == 0
        assert vc.is_paused()
        assert len(vc.play_calls) == 1
        cleanup_cog(mc)

    asyncio.run(run())


def test_pausiertes_radio_wird_beim_songstart_gestoppt(monkeypatch, tmp_path):
    """!s auf pausiertem Radio mit Songs in der Queue: auch der Radio-Player
    wird gestoppt statt ersetzt, sein Callback meldet kein Wiedergabe-Ende."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())

    async def run():
        dl = _PerUrlDownloader({URL_B: (_info("B"), "https://cdn.example/b", "B", 200)})
        vc = PausableVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog(dl)
        assert await mc._play_radio_stream(ctx, "https://radio.example/stream", "Testsender")
        vc.pause()
        mc.is_playing = False

        mc.queue.append((URL_B, "B"))
        await MusicCommands.skip.callback(mc, ctx)    # beendet den Radio-Modus, startet B

        assert vc.leaked == []
        assert vc.stop_calls == 1
        assert mc.current_track[0] == URL_B

        vc.fire_stopped()
        await _spin()
        assert not mc._playback_done.is_set()
        assert len(vc.play_calls) == 2
        assert mc.is_radio is False
        cleanup_cog(mc)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Derselbe Leak auf dem Weg über !radio (radio_play → _play_radio_stream)
# ---------------------------------------------------------------------------

STATIONS = {
    "eins": {"name": "Eins", "url": "https://radio.example/eins"},
    "zwei": {"name": "Zwei", "url": "https://radio.example/zwei"},
}


def _write_station_file():
    with open("radio_stations.json", "w", encoding="utf-8") as f:
        json.dump(STATIONS, f)


def test_radio_stoppt_pausierten_song_ohne_requeue(monkeypatch, tmp_path):
    """!x, dann !radio: der pausierte Song-Player wird gestoppt statt ersetzt,
    sein after-Callback reiht den Song nicht wieder ein und startet nichts."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())
    _write_station_file()

    async def run():
        dl = _PerUrlDownloader({URL_A: (_info("A"), "https://cdn.example/a", "A", 200)})
        vc = PausableVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog(dl)
        mc.loop_mode = "song"      # härtester Fall: der Loop-Zweig würde A wieder einreihen
        mc.queue.append((URL_A, "A"))
        await mc.play_next(ctx)

        await MusicCommands.pause.callback(mc, ctx)   # !x
        assert vc.is_paused() and mc.is_playing is False

        await MusicCommands.radio_play.callback(mc, ctx, eingabe="1")

        assert vc.leaked == [], "pausierter Player wurde ersetzt statt gestoppt (Thread + FFmpeg leaken)"
        assert vc.stop_calls == 1
        assert len(vc.play_calls) == 2
        assert mc.is_radio is True and mc.is_playing is True
        assert mc.current_track[0] == STATIONS["eins"]["url"]

        # Der Thread des gestoppten Song-Players ruft seinen after-Callback erst
        # jetzt – das Radio läuft bereits.
        vc.fire_stopped()
        await _spin()

        assert list(mc.queue) == [], "A darf nicht wieder eingereiht werden"
        assert dl.resolve_calls == [URL_A], "kein play_next aus dem alten Callback"
        assert len(vc.play_calls) == 2
        assert not mc._playback_done.is_set(), "Radio läuft – das Ende-Signal darf nicht vom alten Player kommen"
        assert mc._suppress_resume is False
        assert mc.is_radio is True and mc.is_playing is True
        assert mc.current_track[0] == STATIONS["eins"]["url"]
        cleanup_cog(mc)

    asyncio.run(run())


def test_senderwechsel_stoppt_pausierten_radio_player_ohne_reconnect(monkeypatch, tmp_path):
    """Pausierter Sender, dann !radio <anderer Sender>: der alte Radio-Player
    wird gestoppt, sein Callback löst keinen Reconnect aus und meldet kein Ende."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())
    _write_station_file()

    async def run():
        vc = PausableVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog()
        await MusicCommands.radio_play.callback(mc, ctx, eingabe="1")
        assert mc.radio_station_name == "Eins"

        await MusicCommands.pause.callback(mc, ctx)   # !x
        assert vc.is_paused() and mc.is_playing is False

        await MusicCommands.radio_play.callback(mc, ctx, eingabe="2")

        assert vc.leaked == [], "pausierter Radio-Player wurde ersetzt statt gestoppt"
        assert vc.stop_calls == 1
        assert len(vc.play_calls) == 2
        assert mc.radio_station_name == "Zwei"

        vc.fire_stopped()
        await _spin()

        assert not mc._playback_done.is_set(), "Zwei läuft – das Ende-Signal darf nicht vom alten Sender kommen"
        assert mc._radio_reconnect_count == 0, "der alte Sender darf keinen Reconnect auslösen"
        assert len(vc.play_calls) == 2
        assert mc.is_radio is True and mc.is_playing is True
        assert mc.radio_station_name == "Zwei"
        assert mc.current_track[0] == STATIONS["zwei"]["url"]
        cleanup_cog(mc)

    asyncio.run(run())


def test_gescheiterter_radiostart_laesst_pause_unberuehrt(monkeypatch, tmp_path):
    """Lässt sich die FFmpeg-Quelle des Senders nicht bauen, wird der
    pausierte Player nicht gestoppt – wie bisher."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())
    _write_station_file()

    async def run():
        dl = _PerUrlDownloader({URL_A: (_info("A"), "https://cdn.example/a", "A", 200)})
        vc = PausableVoiceClient()
        ctx = FakeCtx(vc)
        mc = build_cog(dl)
        mc.queue.append((URL_A, "A"))
        await mc.play_next(ctx)
        await MusicCommands.pause.callback(mc, ctx)

        def kaputt(*args, **kwargs):
            raise OSError("ffmpeg fehlt")

        monkeypatch.setattr(discord, "FFmpegOpusAudio", kaputt)
        await MusicCommands.radio_play.callback(mc, ctx, eingabe="1")

        assert vc.stop_calls == 0
        assert vc.is_paused()
        assert len(vc.play_calls) == 1
        assert vc.leaked == []
        cleanup_cog(mc)

    asyncio.run(run())
