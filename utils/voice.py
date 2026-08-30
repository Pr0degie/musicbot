"""Voice-Client-Helfer.

Zustandslos wie der Rest von utils/ — und bewusst ohne Import von
discord.ext.voice_recv: der Helfer muss auch dann funktionieren, wenn das
(experimentelle) Empfangs-Paket gar nicht installiert ist.
"""


def stop_playback(voice_client) -> None:
    """Beendet ausschließlich die Wiedergabe.

    `discord.VoiceClient.stop()` stoppt nur das Senden. `VoiceRecvClient.stop()`
    beendet zusätzlich den Audio-Empfang — bei jedem Wiedergabe-Wechsel (Skip,
    !now, !eq, !seek, Radio-Übernahme) ist aber ausschließlich das Senden
    gemeint. Ohne diese Unterscheidung würde das erste !s die Sprachsteuerung
    stillschweigend abschalten.

    Duck-Typing statt isinstance: so bleibt utils/ frei von voice_recv, und bei
    fehlendem Paket ist das Verhalten byte-identisch zu vorher.
    """
    (getattr(voice_client, "stop_playing", None) or voice_client.stop)()
