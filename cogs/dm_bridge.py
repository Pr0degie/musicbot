import asyncio
import hmac
import ipaddress
import json
import os
import tempfile
from pathlib import Path

from aiohttp import web
import discord
from discord.ext import commands

import config
from utils.files import safe_unlink
from utils.logger import logger
from utils.voice import stop_playback
from config import DM_BRIDGE_HOST, DM_BRIDGE_PORT, DM_BRIDGE_SECRET

# Cap on an uploaded WAV (bytes mode). A spoken sentence is well under this; it's just a guard.
_MAX_WAV_BYTES = 25 * 1024 * 1024

# Ein gesprochener Satz ist winzig; die Grenze ist nur ein Riegel.
_MAX_COMMAND_BYTES = 4 * 1024


def _peer_is_loopback(request) -> bool:
    """True if the request originates from this machine (lets the secret check be skipped)."""
    peer = request.remote
    if not peer:
        return True
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


class DMBridge(commands.Cog):
    """HTTP-Bridge für den KI-Dungeon-Master.

    Dieser Bot ("Bot A") ist im DM-Setup der *Mund*: er empfängt über einen
    kleinen aiohttp-Server fertige Audiodateien von Bot B (dem "Ohr + Hirn")
    und spielt sie im Voice-Channel ab. Das gesamte Empfangen/STT/LLM/TTS
    passiert in Bot B – hier kommt nur das Abspielen dazu.

    Der Server lauscht ausschließlich auf localhost (siehe config.py); es gibt
    keine externe Erreichbarkeit.
    """

    def __init__(self, bot):
        self.bot = bot
        self._runner = None
        # serialisiert mehrere /speak-Aufrufe – der DM sagt immer nur einen
        # Satz nach dem anderen, nie zwei Quellen gleichzeitig.
        self._speak_lock = asyncio.Lock()

    async def cog_load(self):
        # Temp-WAVs aus früheren Läufen wegräumen: safe_unlink konnte eine noch
        # von FFmpeg gelesene Datei nur bis zum Prozessende aufschieben – nach
        # einem Neustart hält sie niemand mehr offen.
        for stale in Path(tempfile.gettempdir()).glob("dm_recv_*.wav"):
            if safe_unlink(stale, defer=False):
                logger.info(f"[DMBridge] Alte Temp-WAV entfernt: {stale.name}")
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/speak", self._handle_speak)
        if config.VOICE_CONTROL:
            app.router.add_post("/command", self._handle_command)
            logger.info("[DMBridge] /command aktiv – Sprachbefehle von Bot B.")
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, DM_BRIDGE_HOST, DM_BRIDGE_PORT)
        await site.start()
        logger.info(f"[DMBridge] HTTP-Server läuft auf {DM_BRIDGE_HOST}:{DM_BRIDGE_PORT}")

    async def cog_unload(self):
        if self._runner:
            await self._runner.cleanup()
        logger.info("[DMBridge] HTTP-Server gestoppt.")

    # ------------------------------------------------------------------ HTTP

    async def _handle_health(self, request):
        return web.json_response({"status": "ok", "bot": str(self.bot.user)})

    async def _handle_command(self, request):
        """Nimmt einen von Bot B transkribierten Satz entgegen ("yo bot, spiel ...").

        Bot B sitzt ohnehin im Voice-Channel und transkribiert; statt hier ein
        zweites Whisper danebenzustellen, reicht er den Rohtext samt Sprecher-ID
        herüber. Geparst wird ausschließlich hier – nur so bleiben die Regeln
        an einem Ort.

        Anders als /speak antwortet dieser Endpunkt sofort und meldet
        Fachliches (nicht verstanden, kein Weckwort, gesperrt) als HTTP 200 mit
        einem "status"-Feld: Bot B soll darauf nie einen Retry aufbauen.
        """
        if DM_BRIDGE_SECRET and not _peer_is_loopback(request):
            sent = request.headers.get("X-DM-Secret", "")
            if not hmac.compare_digest(sent, DM_BRIDGE_SECRET):
                return web.json_response({"error": "unauthorized"}, status=401)

        roh = await request.text()
        if len(roh) > _MAX_COMMAND_BYTES:
            return web.json_response({"error": "too large"}, status=413)
        try:
            data = json.loads(roh)
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid json"}, status=400)

        text = (data.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "missing text"}, status=400)
        # Ohne Sprecher-ID gäbe es kein Member – und damit kein ctx.author.voice.
        user_id = data.get("user_id")
        if not user_id:
            return web.json_response({"error": "missing user_id"}, status=400)

        listen = self.bot.get_cog("VoiceListen")
        if listen is None or not listen.enabled:
            return web.json_response({"status": "ignored", "reason": "disabled"})

        ergebnis = await listen.handle_text(
            text,
            user_id=user_id,
            guild_id=data.get("guild_id"),
            source=data.get("source") or "bridge",
        )
        return web.json_response(ergebnis)

    async def _handle_speak(self, request):
        """Spielt eine von Bot B gelieferte Audiodatei im Voice-Channel ab.

        Zwei Transporte (ADR 010):
        - JSON ``{"path","guild_id?"}`` → **Pfad-Modus** (gemeinsame Platte, localhost).
        - Body mit ``Content-Type: audio/wav`` → **Byte-Modus**: die WAV-Bytes werden hier in
          den eigenen Temp-Ordner geschrieben, abgespielt und gelöscht (getrennte Maschinen).
        Antwortet erst, wenn die Wiedergabe abgeschlossen ist (Blocking = "fertig"-Signal, D15).
        """
        # Secret nur prüfen, wenn eines gesetzt ist UND der Aufruf nicht von localhost kommt –
        # so bleibt der klassische localhost-Pfad-Modus ohne jede Konfiguration nutzbar.
        if DM_BRIDGE_SECRET and not _peer_is_loopback(request):
            sent = request.headers.get("X-DM-Secret", "")
            if not hmac.compare_digest(sent, DM_BRIDGE_SECRET):
                return web.json_response({"error": "unauthorized"}, status=401)

        ctype = (request.headers.get("Content-Type") or "").lower()
        if ctype.startswith("application/json"):
            return await self._speak_path(request)
        if ctype.startswith("audio/wav") or ctype.startswith("application/octet-stream"):
            return await self._speak_bytes(request)
        return web.json_response({"error": "unsupported content-type"}, status=400)

    async def _speak_path(self, request):
        """Pfad-Modus (localhost): Bot B besitzt die Datei, wir spielen sie nur ab."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        path = data.get("path")
        if not path:
            return web.json_response({"error": "missing path"}, status=400)
        if not Path(path).is_file():
            return web.json_response({"error": "file not found", "path": path}, status=404)

        vc = self._resolve_voice_client(data.get("guild_id"))
        if vc is None or not vc.is_connected():
            return web.json_response({"error": "not connected to voice"}, status=409)

        async with self._speak_lock:
            try:
                await self._play_file(vc, path)
            except Exception:
                logger.exception(f"[DMBridge] Fehler beim Abspielen von {path}")
                return web.json_response({"error": "playback failed"}, status=500)
        return web.json_response({"status": "played", "path": path})

    async def _speak_bytes(self, request):
        """Byte-Modus (getrennte Maschinen): WAV-Bytes empfangen, lokal abspielen, löschen."""
        body = await request.read()
        if not body:
            return web.json_response({"error": "empty body"}, status=400)
        if len(body) > _MAX_WAV_BYTES:
            return web.json_response({"error": "file too large"}, status=413)

        # Voice zuerst prüfen – spart das Schreiben auf Platte, wenn Bot A gar nicht im Voice ist.
        vc = self._resolve_voice_client(request.headers.get("X-DM-Guild-Id"))
        if vc is None or not vc.is_connected():
            return web.json_response({"error": "not connected to voice"}, status=409)

        fd, tmp = tempfile.mkstemp(prefix="dm_recv_", suffix=".wav", dir=tempfile.gettempdir())
        os.close(fd)
        try:
            try:
                with open(tmp, "wb") as fh:
                    fh.write(body)
            except OSError:
                logger.exception("[DMBridge] Konnte empfangene WAV nicht schreiben")
                return web.json_response({"error": "write failed"}, status=500)

            async with self._speak_lock:
                try:
                    await self._play_file(vc, tmp)
                except Exception:
                    logger.exception("[DMBridge] Fehler beim Abspielen der empfangenen WAV")
                    return web.json_response({"error": "playback failed"}, status=500)
        finally:
            # Der after-Callback von vc.play feuert, BEVOR discord.py die
            # FFmpeg-Quelle aufräumt (Player-Thread: _call_after → cleanup) –
            # FFmpeg kann die WAV hier also noch offen halten. Windows wirft
            # dann PermissionError; safe_unlink schiebt die Löschung auf
            # (Retry beim nächsten cleanup_downloads-Drain, Reste aus
            # früheren Läufen räumt cog_load weg).
            safe_unlink(tmp)
        return web.json_response({"status": "played"})

    # -------------------------------------------------------------- Internals

    def _resolve_voice_client(self, guild_id):
        """Findet den passenden VoiceClient – per guild_id oder erste aktive Verbindung."""
        if guild_id is not None:
            guild = self.bot.get_guild(int(guild_id))
            return guild.voice_client if guild else None
        for vc in self.bot.voice_clients:
            if vc.is_connected():
                return vc
        return None

    async def _play_file(self, vc, path):
        """Spielt eine Datei ab und wartet bis zum Ende (Muster wie _play_radio_stream)."""
        # dm_speaking signalisiert dem Music-Cog, dass wir gerade den Voice-Client besitzen:
        # sein after_playing-Callback ruft sonst bei JEDEM Track-Ende (auch dem durch den Stopp
        # unten ausgelösten) play_next → vc.play(nächster Track) auf und besetzt den Client wieder
        # bevor wir unsere WAV starten → "Already playing audio.". Das Flag lässt play_next bei
        # Aktivierung sofort aussteigen. finally: nie hängen lassen, sonst läuft nie wieder Musik.
        self.bot.dm_speaking = True
        try:
            # Im DM-Modus soll keine Musik parallel laufen. Falls doch etwas spielt
            # oder pausiert ist, wird es gestoppt (DM-Session läuft mit leerer Queue).
            if vc.is_playing() or vc.is_paused():
                stop_playback(vc)
                await asyncio.sleep(0.2)  # Event-Loop-Fenster: das unterdrückte play_next läuft hier

            done = asyncio.Event()
            loop = self.bot.loop

            def after(error):
                if error:
                    logger.warning(f"[DMBridge] Wiedergabefehler: {error}")
                loop.call_soon_threadsafe(done.set)

            source = discord.FFmpegOpusAudio(path, options="-vn")
            vc.play(source, after=after)
            await done.wait()
        finally:
            self.bot.dm_speaking = False

    # -------------------------------------------------------------- Befehle

    @commands.command(name="dm")
    async def dm_status(self, ctx):
        """Zeigt den Status der DM-Bridge."""
        vc = ctx.voice_client
        connected = "✅ verbunden" if vc and vc.is_connected() else "❌ nicht im Voice"
        await ctx.send(
            "🎲 **DM-Bridge**\n"
            f"Server: `{DM_BRIDGE_HOST}:{DM_BRIDGE_PORT}`\n"
            f"Voice: {connected}"
        )
