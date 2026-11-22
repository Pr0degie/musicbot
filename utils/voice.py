"""Voice-Client-Helfer.

Zustandslos bis auf ein Register: welchen Channel der Bot pro Server selbst
gewählt hat (`intended_channel_id`, für den Move-Schutz). Ohne Import von
discord.ext.voice_recv auf Modulebene: der Helfer muss auch dann funktionieren,
wenn das (experimentelle) Empfangs-Paket gar nicht installiert ist.
"""
import threading
import time

from discord.player import AudioPlayer

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


def start_clock_at_first_packet(source):
    """Lässt die Uhr des Players erst beim ersten Paket der Quelle loslaufen.

    discord.py startet die Uhr des AudioPlayers, bevor FFmpeg etwas liefert.
    Braucht FFmpeg bis zum ersten Paket z. B. 0,5 s (Radiosender, Netz-Streams,
    Seek), liegt der Player von Beginn an hinter seinem Zeitplan und schickt
    diese Zeit an Ton ohne Pause hinterher – am Anfang klingt es kurz zu
    schnell (gemessen: 76–78 statt 50 Pakete in der ersten Sekunde).

    Das erste read() läuft im Player-Thread; danach werden `_start`/`loops`
    neu gesetzt – dieselben zwei Zeilen, mit denen discord.py seine Uhr nach
    einem Voice-Reconnect selbst zurücksetzt. Nur einmal: danach liest der
    Player wieder direkt aus der Quelle. Fehlen die Interna in einer anderen
    discord.py-Version oder hat die Quelle kein read() (Test-Fakes), bleibt
    alles wie bisher. Gibt die Quelle zurück.
    """
    read = getattr(source, "read", None)
    if read is None:
        return source

    def first_read():
        data = read()
        del source.read   # ab jetzt wieder die Methode der Klasse
        player = threading.current_thread()
        if data and isinstance(player, AudioPlayer) and hasattr(player, "_start") and hasattr(player, "loops"):
            player._start = time.perf_counter()
            player.loops = 0
        return data

    source.read = first_read
    return source


class PlayerToken:
    """Gehört zu genau einem gestarteten Player (Song oder Radio).

    discord.py ersetzt in play() einen PAUSIERTEN Player, ohne ihn zu stoppen
    (is_playing() ist dann False) – Thread und FFmpeg-Prozess blieben für immer
    hängen. play_next stoppt so einen Player deshalb selbst und setzt vorher
    `discarded`: der after-Callback des verworfenen Players (läuft später im
    Player-Thread) darf dann nichts mehr am Wiedergabe-Zustand anfassen – der
    gehört bereits dem neuen Track.
    """
    __slots__ = ("discarded",)

    def __init__(self):
        self.discarded = False


_safe_recv_cls = None


def _make_safe_recv_cls(base):
    """VoiceRecvClient mit repariertem `_remove_ssrc`.

    voice_recv (0.5.2a179) greift beim Abmelden eines Users (Voice-Opcode 13,
    jemand verlässt den Channel) ohne Prüfung auf `self._reader.speaking_timer`
    zu. `_reader` gibt es aber nur, solange zugehört wird – ohne `!listen on`
    wirft das AttributeError, und zwar im Websocket-Poller von discord.py. Der
    stirbt daran still: 60 s später "stopped responding", kein Reconnect, der
    Player sendet ins Leere (Discord zeigt den grünen Ring, kein Ton).
    `_add_ssrc` daneben prüft `if self._reader:` – hier fehlt genau das.
    """
    class SafeVoiceRecvClient(base):
        def _remove_ssrc(self, *, user_id):
            if self._reader:
                return super()._remove_ssrc(user_id=user_id)
            ssrc = self._id_to_ssrc.pop(user_id, None)
            if ssrc:
                self._ssrc_to_id.pop(ssrc, None)

    return SafeVoiceRecvClient


def _recv_client_cls():
    """VoiceRecvClient, falls das Empfangs-Paket vorhanden ist – sonst None.

    Import bewusst erst hier: das Paket ist experimentell, und ohne es soll der
    Bot exakt wie vorher laufen.
    """
    global _safe_recv_cls
    try:
        from discord.ext import voice_recv
    except Exception as e:
        logger.warning(f"[Voice-Listen] discord-ext-voice-recv nicht nutzbar: "
                       f"{type(e).__name__}: {str(e)[:120]} – eigenes Zuhören entfällt.")
        return None
    if _safe_recv_cls is None:
        _safe_recv_cls = _make_safe_recv_cls(voice_recv.VoiceRecvClient)
    return _safe_recv_cls


def dead_voice_poller(voice_client):
    """Beschreibung, falls die Sprachverbindung tot ist, obwohl sie als
    verbunden gilt – sonst None.

    discord.py hält die Voice-Verbindung mit einer einzigen Task am Leben
    ("Voice websocket poller"). Stirbt die an einer Exception, merkt das
    niemand: is_connected() bleibt True, der Player sendet weiter. Erst beim
    Herunterfahren erscheint "Task exception was never retrieved".

    Liest discord.py-Interna (`_connection._runner`) – fehlen sie in einer
    anderen Version, kommt None zurück und alles bleibt wie bisher.
    """
    runner = getattr(getattr(voice_client, "_connection", None), "_runner", None)
    if runner is None or not runner.done() or not voice_client.is_connected():
        return None
    if runner.cancelled():
        return "abgebrochen"
    exc = runner.exception()   # markiert sie auch als abgeholt
    return f"{type(exc).__name__}: {exc}" if exc else "ohne Fehler beendet"


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
    _intended[channel.guild.id] = channel.id
    cls = voice_client_cls()
    if cls is not None:
        return await channel.connect(cls=cls, **kwargs)
    return await channel.connect(**kwargs)


# guild_id → channel_id, in den der Bot selbst gegangen ist (connect/move).
# Landet er woanders, hat ihn jemand verschoben (cogs/music_voice_guard.py).
_intended: dict[int, int] = {}


def intended_channel_id(guild_id):
    return _intended.get(guild_id)


def forget_intended_channel(guild_id):
    _intended.pop(guild_id, None)


def set_intended_channel(channel):
    """Für den Move-Schutz, wenn der Owner den Bot per Hand verschoben hat."""
    _intended[channel.guild.id] = channel.id


async def move_voice(voice_client, channel):
    """Der einzige Channel-Wechsel im Projekt – merkt sich das Ziel vorher,
    damit der Move-Schutz den eigenen Wechsel nicht zurückdreht."""
    _intended[channel.guild.id] = channel.id
    await voice_client.move_to(channel)


async def disconnect_voice(voice_client, *, forget=True, **kwargs):
    """Der einzige Verbindungsabbau im Projekt.

    forget=True (Normalfall: !l, Auto-Leave, Neustart) vergisst den gewollten
    Channel, damit der Rauswurf-Schutz den eigenen Abgang nicht rückgängig
    macht. Die Voice-Reparatur baut gleich wieder auf und übergibt False.
    """
    guild = getattr(voice_client, "guild", None)
    if forget and guild is not None:
        forget_intended_channel(guild.id)
    await voice_client.disconnect(**kwargs)
