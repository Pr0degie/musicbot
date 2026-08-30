"""Voice-Client-Helfer.

Zustandslos wie der Rest von utils/ — und ohne Import von
discord.ext.voice_recv auf Modulebene: der Helfer muss auch dann funktionieren,
wenn das (experimentelle) Empfangs-Paket gar nicht installiert ist.
"""
import config
from utils.logger import logger


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


def _recv_client_cls():
    """VoiceRecvClient, falls das Empfangs-Paket vorhanden ist – sonst None.

    Import bewusst erst hier: das Paket ist experimentell, und ohne es soll der
    Bot exakt wie vorher laufen.
    """
    try:
        from discord.ext import voice_recv
        return voice_recv.VoiceRecvClient
    except Exception as e:
        logger.warning(f"[Voice-Listen] discord-ext-voice-recv nicht nutzbar: "
                       f"{type(e).__name__}: {str(e)[:120]} – eigenes Zuhören entfällt.")
        return None


def voice_client_cls():
    """Die Client-Klasse für den nächsten connect(), oder None für den Normalfall.

    Beide Flags sind nötig: VOICE_CONTROL schaltet die Sprachsteuerung
    überhaupt frei, VOICE_OWN_LISTEN zusätzlich das eigene Zuhören. Weil die
    Klasse nur beim connect() gesetzt werden kann, wirkt eine Änderung erst
    nach einem Neustart – dafür braucht !listen dann keinen Reconnect mitten
    im Song.
    """
    if not (config.VOICE_CONTROL and config.VOICE_OWN_LISTEN):
        return None
    return _recv_client_cls()


async def connect_voice(channel, **kwargs):
    """Der einzige Verbindungsaufbau im Projekt.

    Ohne aktivierte Sprachsteuerung ist das byte-identisch zu einem nackten
    channel.connect() – kein zusätzliches Argument, kein anderes Verhalten.
    """
    cls = voice_client_cls()
    if cls is not None:
        return await channel.connect(cls=cls, **kwargs)
    return await channel.connect(**kwargs)
