import asyncio
import hmac
import ipaddress
import os
import tempfile
from pathlib import Path

from aiohttp import web
import discord
from discord.ext import commands

from utils.logger import logger
from config import DM_BRIDGE_HOST, DM_BRIDGE_PORT, DM_BRIDGE_SECRET

# Cap on an uploaded WAV (bytes mode). A spoken sentence is well under this; it's just a guard.
_MAX_WAV_BYTES = 25 * 1024 * 1024


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
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/speak", self._handle_speak)
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
            try:
                os.remove(tmp)
            except OSError:
                pass
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
        # Im DM-Modus soll keine Musik parallel laufen. Falls doch etwas spielt
        # oder pausiert ist, wird es gestoppt (DM-Session läuft mit leerer Queue).
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await asyncio.sleep(0.2)

        done = asyncio.Event()
        loop = self.bot.loop

        def after(error):
            if error:
                logger.warning(f"[DMBridge] Wiedergabefehler: {error}")
            loop.call_soon_threadsafe(done.set)

        source = discord.FFmpegOpusAudio(path, options="-vn")
        vc.play(source, after=after)
        await done.wait()

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
