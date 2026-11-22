"""Ein gewollter Abbruch (Skip, !stop) löst weder Stream-Retry noch Aufgeben-Meldung aus.

Die Retry-Kaskade des Track-Endes prüfte nicht, ob der Nutzer den Track
selbst beendet hat:
- Skip eines Streams in der ersten Sekunde, FFmpeg hinterlässt dabei eine
  Input-Fehlerzeile → der Track wurde als „Stream starb" wieder eingereiht.
- Skip des Download-Fallback-Anlaufs in den ersten 2 Sekunden → die Meldung
  error.stream_giveup, obwohl nichts gescheitert ist.
"""

import asyncio

import discord

from test_music_playback import (
    STREAM_INFO, FakeCtx, FakeDownloader, FakeVoiceClient, _cleanup, make_cog, make_fake_source,
)
from utils.i18n import t

URL = "https://www.youtube.com/watch?v=test"
URL_NEXT = "https://www.youtube.com/watch?v=next"
INPUT_ERROR = b"[https @ 0x1] HTTP error 403 Forbidden\n"


async def _warte_auf_plays(vc, count):
    for _ in range(200):
        if len(vc.play_calls) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"nur {len(vc.play_calls)} von {count} Wiedergaben gestartet")


def test_skip_in_der_ersten_sekunde_ist_kein_stream_retry(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source(INPUT_ERROR))

    async def run():
        dl = FakeDownloader((STREAM_INFO, "https://cdn.example/stream", "Testsong", 200))
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = make_cog(dl)
        mc.queue.extend([(URL, "Testsong"), (URL_NEXT, "Danach")])

        await mc.play_next(ctx)
        mc._stop_for_advance(vc)             # Nutzer skippt sofort
        vc.end_track()
        await _warte_auf_plays(vc, 2)

        assert mc.current_track[0] == URL_NEXT, "nach dem Skip muss der nächste Song laufen"
        assert dl.invalidated == [], "kein Retry: der Cache-Eintrag bleibt"
        assert mc._stream_retry_url is None
        assert list(mc.queue) == []
        _cleanup(mc)

    asyncio.run(run())


def test_skip_des_download_fallbacks_meldet_kein_aufgeben(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source(INPUT_ERROR))

    async def run():
        local_file = tmp_path / "Testsong.webm"
        dl = FakeDownloader((STREAM_INFO, "https://cdn.example/stream", "Testsong", 200))
        dl.force_result = (STREAM_INFO, local_file, "Testsong", 200)
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = make_cog(dl)
        mc.queue.append((URL, "Testsong"))

        await mc.play_next(ctx)
        vc.end_track()                       # Versuch 1 stirbt mit 403
        await _warte_auf_plays(vc, 2)
        vc.end_track()                       # Versuch 2 ebenso → Download-Fallback
        await _warte_auf_plays(vc, 3)
        assert dl.force_calls == [False, False, True]

        mc.queue.append((URL_NEXT, "Danach"))
        mc._stop_for_advance(vc)             # Nutzer skippt den dritten Anlauf sofort
        vc.end_track()
        await _warte_auf_plays(vc, 4)

        texts = [args[0] for args, kwargs in ctx.sent if args]
        assert t("error.stream_giveup", title="Testsong") not in texts, \
            "ein Skip ist kein gescheiterter Download-Fallback"
        assert mc.current_track[0] == URL_NEXT
        _cleanup(mc)

    asyncio.run(run())


def test_ohne_skip_bleibt_der_stream_retry(monkeypatch, tmp_path):
    """Gegenprobe: stirbt der Stream von selbst, greift der Retry weiterhin."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source(INPUT_ERROR))

    async def run():
        dl = FakeDownloader((STREAM_INFO, "https://cdn.example/stream", "Testsong", 200))
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = make_cog(dl)
        mc.queue.append((URL, "Testsong"))

        await mc.play_next(ctx)
        vc.end_track()
        await _warte_auf_plays(vc, 2)

        assert dl.invalidated == [URL]
        assert mc._stream_retry_url == URL
        _cleanup(mc)

    asyncio.run(run())
