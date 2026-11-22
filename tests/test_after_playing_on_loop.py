"""after_playing: der Thread-Callback reicht nur weiter, die Logik läuft auf
dem Event-Loop.

discord.py ruft den Callback im Player-Thread auf. Vorher veränderte er dort
Queue, _seek_offset und die Retry-Marker, während !remove/!move/!shuffle die
Queue auf dem Loop neu aufbauten. Diese Tests feuern den Callback – anders als
die übrigen Playback-Tests – aus einem echten Fremd-Thread, also auf dem Weg,
den er im Betrieb nimmt.
"""
import asyncio
import json
import threading
import time
from collections import deque
from pathlib import Path

import discord
import pytest

import cogs.music_track_end as playback_mod
from conftest import FakeCtx, FakeDownloader, FakeVoiceClient, build_cog, cleanup_cog, make_fake_source

URL_A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
URL_B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"
INFO = {"title": "Song A", "webpage_url": URL_A, "thumbnail": None, "uploader": None, "duration": 200}


class SpyDeque(deque):
    """Merkt sich, aus welchem Thread die Queue verändert wird."""

    def __init__(self, *args):
        super().__init__(*args)
        self.idents = []

    def appendleft(self, item):
        self.idents.append(threading.get_ident())
        super().appendleft(item)

    def append(self, item):
        self.idents.append(threading.get_ident())
        super().append(item)


async def _start(monkeypatch, *, freeze_next=True, result=None):
    """Spielt Song A an. freeze_next: play_next danach einfrieren, damit der
    Callback die Queue nicht sofort wieder leert."""
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())
    dl = FakeDownloader(result or (INFO, "https://cdn.example/stream", "Song A", 200))
    vc = FakeVoiceClient()
    ctx = FakeCtx(vc)
    mc = build_cog(dl)
    mc.queue = SpyDeque([(URL_A, "Song A")])
    await mc.play_next(ctx)
    assert mc.current_track == (URL_A, "Song A", 200)
    mc.next_calls = []
    if freeze_next:
        async def frozen_play_next(_ctx):
            mc.next_calls.append(threading.get_ident())

        mc.play_next = frozen_play_next
    return mc, vc, ctx


def test_logik_laeuft_auf_dem_loop_datei_io_im_thread(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def run():
        loop_thread = threading.get_ident()
        mc, vc, ctx = await _start(monkeypatch)
        mc.queue.append((URL_B, "Song B"))
        mc.queue.idents.clear()
        mc.loop_mode = "song"
        mc.track_start_time = time.monotonic() - 30
        idents = {}

        def spy_stderr(buf):
            idents["stderr"] = threading.get_ident()
            buf.close()
            return ""

        def spy_classify(tail):
            idents["logik"] = threading.get_ident()
            return None

        echtes_schreiben = playback_mod.atomic_write_text

        def spy_write(path, text):
            idents["write"] = threading.get_ident()
            return echtes_schreiben(path, text)

        mc._stderr_tail = spy_stderr
        mc._classify_ffmpeg_error = spy_classify
        monkeypatch.setattr(playback_mod, "atomic_write_text", spy_write)

        await asyncio.to_thread(vc.end_track)      # Player-Thread ruft after_playing
        await asyncio.sleep(0.05)

        assert idents["logik"] == loop_thread, "Track-Ende-Logik gehört auf den Event-Loop"
        assert mc.queue.idents == [loop_thread], "Queue darf nur auf dem Loop verändert werden"
        assert idents["stderr"] != loop_thread, "stderr-Datei wird im Player-Thread gelesen"
        assert idents["write"] != loop_thread, "last_queue.json wird nicht auf dem Loop geschrieben"
        assert mc.next_calls == [loop_thread], "play_next wurde genau einmal angestoßen"
        assert mc._playback_done.is_set()

        # Reihenfolge wie bisher: Snapshot VOR der Loop-Logik – der per Loop
        # zurückgelegte Song A fehlt in der Datei, liegt aber vorn in der Queue.
        assert json.loads(Path("last_queue.json").read_text(encoding="utf-8")) == [[URL_B, "Song B"]]
        assert list(mc.queue) == [(URL_A, "Song A"), (URL_B, "Song B")]
        cleanup_cog(mc)

    asyncio.run(run())


def test_naechster_track_startet_nach_callback_aus_dem_thread(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def run():
        mc, vc, ctx = await _start(monkeypatch, freeze_next=False)
        mc.queue.append((URL_B, "Song B"))
        mc.track_start_time = time.monotonic() - 30

        await asyncio.to_thread(vc.end_track)
        for _ in range(200):
            if len(vc.play_calls) >= 2:
                break
            await asyncio.sleep(0.01)

        assert len(vc.play_calls) == 2
        assert mc.current_track[0] == URL_B
        cleanup_cog(mc)

    asyncio.run(run())


def test_stream_retry_aus_dem_thread(monkeypatch, tmp_path):
    """Sofort-Tod mit Input-Fehler: Marker setzen, Cache verwerfen, Track vorn
    wieder einreihen – alles auf dem Loop, dann Neustart."""
    monkeypatch.chdir(tmp_path)

    async def run():
        loop_thread = threading.get_ident()
        mc, vc, ctx = await _start(monkeypatch, freeze_next=False)
        mc.queue.idents.clear()
        mc._stderr_tail = lambda buf: (buf.close(), "HTTP error 403 Forbidden")[1]
        mc._classify_ffmpeg_error = lambda tail: "input"

        await asyncio.to_thread(vc.end_track)      # stirbt nach wenigen Millisekunden
        for _ in range(200):
            if len(vc.play_calls) >= 2:
                break
            await asyncio.sleep(0.01)

        assert mc._stream_retry_url == URL_A
        assert mc.dl.invalidated == [URL_A]
        assert mc.queue.idents == [loop_thread]
        assert len(vc.play_calls) == 2 and mc.current_track[0] == URL_A
        assert mc._play_counts == {URL_A: 1}, "Neuversuch zählt nicht doppelt"
        cleanup_cog(mc)

    asyncio.run(run())


def test_fehler_in_der_logik_kommt_im_thread_callback_an(monkeypatch, tmp_path):
    """discord.py fängt und loggt Exceptions des after-Callbacks – das bleibt
    so, auch wenn die Logik jetzt auf dem Loop läuft."""
    monkeypatch.chdir(tmp_path)

    async def run():
        mc, vc, ctx = await _start(monkeypatch)

        def kaputt(tail):
            raise RuntimeError("bumm")

        mc._classify_ffmpeg_error = kaputt
        with pytest.raises(RuntimeError, match="bumm"):
            await asyncio.to_thread(vc.end_track)
        cleanup_cog(mc)

    asyncio.run(run())


def test_blockierter_loop_haelt_den_player_thread_nicht_fest(monkeypatch, tmp_path):
    """Übernimmt der Loop nicht rechtzeitig, gibt der Thread-Callback auf (kein
    Write) – die Logik läuft nach, sobald der Loop wieder dran ist."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(playback_mod, "_LOOP_HANDOFF_TIMEOUT", 0.05)

    async def run():
        mc, vc, ctx = await _start(monkeypatch)
        mc.queue.append((URL_B, "Song B"))
        mc.track_start_time = time.monotonic() - 30

        player = threading.Thread(target=vc.end_track)
        player.start()
        player.join(5)                    # blockiert den Loop, bis der Thread aufgibt
        assert not player.is_alive(), "Player-Thread hängt"
        assert mc.next_calls == []
        assert not Path("last_queue.json").exists()

        await asyncio.sleep(0.05)         # Loop wieder frei → Logik läuft nach
        assert len(mc.next_calls) == 1
        assert mc._playback_done.is_set()
        cleanup_cog(mc)

    asyncio.run(run())


def test_aelterer_snapshot_ueberschreibt_keinen_neueren(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    mc = build_cog()

    mc._write_last_queue(2, [("u2", "Neuer Stand")])
    mc._write_last_queue(1, [("u1", "Alter Stand")])      # kommt verspätet an

    assert json.loads(Path("last_queue.json").read_text(encoding="utf-8")) == [["u2", "Neuer Stand"]]
    mc._write_last_queue(3, [])
    assert json.loads(Path("last_queue.json").read_text(encoding="utf-8")) == []
