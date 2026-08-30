"""Tests für die Quellen-Weiche und den Audio-Pfad des eigenen Zuhörens.

Kernanforderung: sitzt der DM-Bot (KI-Dungeon-Master) im Voice-Channel, läuft
im Musikbot KEIN eigenes Sprachmodell. Er transkribiert ohnehin schon – ein
zweites Whisper daneben wäre doppelte Arbeit und doppelter VRAM.

Die Kosten sind asymmetrisch: Abschalten ist gratis und muss sofort passieren,
Anschalten kostet Ladezeit und VRAM und wartet deshalb eine Karenz ab.
"""
import asyncio
import types

import pytest

import config
from cogs.voice_listen import VoiceListen
from utils.nl_parser import build_wake_re


def run(coro):
    return asyncio.run(coro)


DM_BOT_ID = 555


class FakeMember:
    def __init__(self, uid, name="Wer", bot=False):
        self.id = uid
        self.display_name = name
        self.bot = bot
        self.voice = types.SimpleNamespace(channel=object())


class FakeVoiceClient:
    def __init__(self, members):
        self.channel = types.SimpleNamespace(members=members, name="Allgemein")


def baue_cog(members=(), monkeypatch=None):
    cog = VoiceListen.__new__(VoiceListen)
    cog.bot = types.SimpleNamespace(
        voice_clients=[FakeVoiceClient(list(members))] if members else [],
        dm_speaking=False,
    )
    cog.enabled = True
    cog._wake_re = build_wake_re()
    cog._ref_message = None
    cog._reply_channel = None
    cog._model = None
    cog._model_info = ""
    cog._sink = None
    cog._buffers = None
    cog._own_listening = False
    cog._reeval_task = None
    return cog


# --- Erkennung --------------------------------------------------------------

def test_dm_bot_im_channel_wird_erkannt(monkeypatch):
    monkeypatch.setattr(config, "DM_BOT_USER_ID", DM_BOT_ID)
    cog = baue_cog([FakeMember(1), FakeMember(DM_BOT_ID, "DMBot", bot=True)])
    assert cog._bridge_present() is True


def test_ohne_dm_bot_im_channel_keine_weiche(monkeypatch):
    monkeypatch.setattr(config, "DM_BOT_USER_ID", DM_BOT_ID)
    cog = baue_cog([FakeMember(1), FakeMember(2)])
    assert cog._bridge_present() is False


def test_fremder_bot_loest_die_weiche_nicht_aus(monkeypatch):
    """Ein Recording-Bot oder Soundboard darf das Zuhören nicht abschalten –
    ein Verhalten, das niemand debuggen will."""
    monkeypatch.setattr(config, "DM_BOT_USER_ID", DM_BOT_ID)
    cog = baue_cog([FakeMember(1), FakeMember(999, "Craig", bot=True)])
    assert cog._bridge_present() is False


def test_ohne_konfigurierte_id_gibt_es_keine_weiche(monkeypatch):
    monkeypatch.setattr(config, "DM_BOT_USER_ID", 0)
    cog = baue_cog([FakeMember(DM_BOT_ID, "DMBot", bot=True)])
    assert cog._bridge_present() is False


# --- Umschalten -------------------------------------------------------------

@pytest.fixture
def cog_mit_protokoll(monkeypatch):
    monkeypatch.setattr(config, "DM_BOT_USER_ID", DM_BOT_ID)
    monkeypatch.setattr(config, "VOICE_CONTROL", True)
    monkeypatch.setattr(config, "VOICE_OWN_LISTEN", True)
    cog = baue_cog([FakeMember(1)])
    cog.protokoll = []

    async def start():
        cog.protokoll.append("start")
        cog._own_listening = True
        return True

    async def stop(release_model=False):
        cog.protokoll.append(f"stop(release_model={release_model})")
        cog._own_listening = False

    cog._start_own_listening = start
    cog._stop_own_listening = stop
    cog._antworten = lambda text: asyncio.sleep(0)
    return cog


def test_dm_bot_kommt_modell_wird_sofort_freigegeben(cog_mit_protokoll):
    cog = cog_mit_protokoll
    cog._own_listening = True
    cog.bot.voice_clients[0].channel.members.append(FakeMember(DM_BOT_ID, "DMBot", bot=True))

    run(cog._reevaluate(grace=0.01))

    assert cog.protokoll == ["stop(release_model=True)"]
    assert cog._own_listening is False


def test_dm_bot_geht_eigenes_zuhoeren_startet_nach_karenz(cog_mit_protokoll):
    cog = cog_mit_protokoll
    run(cog._reevaluate(grace=0.01))
    assert cog.protokoll == ["start"]


def test_kurzer_reconnect_des_dm_bots_loest_kein_ladepingpong_aus(cog_mit_protokoll):
    """Anschalten kostet Ladezeit und VRAM – ein Reconnect darf das nicht
    jedes Mal auslösen."""
    cog = cog_mit_protokoll

    async def szenario():
        aufgabe = asyncio.create_task(cog._reevaluate(grace=0.15))
        await asyncio.sleep(0.05)
        # DM-Bot kommt während der Karenz zurück
        cog.bot.voice_clients[0].channel.members.append(
            FakeMember(DM_BOT_ID, "DMBot", bot=True))
        await aufgabe

    run(szenario())
    assert cog.protokoll == [], "während der Karenz zurückgekehrt -> kein Start"


def test_ohne_own_listen_flag_wird_nie_selbst_gestartet(cog_mit_protokoll, monkeypatch):
    monkeypatch.setattr(config, "VOICE_OWN_LISTEN", False)
    run(cog_mit_protokoll._reevaluate(grace=0.01))
    assert cog_mit_protokoll.protokoll == []


# --- Audio-Pfad -------------------------------------------------------------

def test_segment_laeuft_durch_stt_und_parser_bis_zum_befehl(monkeypatch):
    """Die ganze Kette ohne GPU und ohne voice_recv: die Transkription wird an
    ihrer Modulgrenze ersetzt."""
    monkeypatch.setattr(config, "VOICE_BLOCKED_USER_IDS", frozenset())
    sprecher = FakeMember(1, "Tobi")
    cog = baue_cog([sprecher])

    gesendet = []
    kontexte = []

    class Kanal:
        async def send(self, inhalt=None, **kw):
            gesendet.append(inhalt)
            return types.SimpleNamespace(content=inhalt, channel=self, author=None)

    async def get_context(message):
        ctx = types.SimpleNamespace(message=message, command="p",
                                    author=message.author, channel=message.channel)
        kontexte.append(ctx)
        return ctx

    kanal = Kanal()
    cog._reply_channel = kanal
    cog._ref_message = types.SimpleNamespace(content="!listen on", channel=kanal,
                                             author=sprecher)
    cog.bot.get_context = get_context
    cog.bot.invoke = lambda ctx: asyncio.sleep(0)
    cog._resolve_member = lambda uid, gid=None: sprecher
    # Modulgrenze: statt Whisper ein festes Transkript.
    cog._transcribe = lambda audio: "Yo Bot, spiel mal Bohemian Rhapsody."

    # Amplitude 16384 (~-6 dBFS) – muss das Rauschgate passieren.
    run(cog._verarbeite_segment(1, b"\x00\x40" * 48000))

    assert [k.message.content for k in kontexte] == ["!p Bohemian Rhapsody"]


def test_zu_leises_segment_erreicht_die_gpu_nicht(monkeypatch):
    """Rauschgate: Tastaturklicks und Lüfter sollen nichts kosten."""
    sprecher = FakeMember(1, "Tobi")
    cog = baue_cog([sprecher])
    aufrufe = []
    cog._transcribe = lambda audio: aufrufe.append(audio) or "egal"
    cog._resolve_member = lambda uid, gid=None: sprecher

    run(cog._verarbeite_segment(1, b"\x00\x00" * 48000))   # reine Stille

    assert aufrufe == []
