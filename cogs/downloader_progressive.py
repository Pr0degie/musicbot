"""In-Flight-Registry und progressiver Download – Mixin für Downloader.

Der Teil mit den heikelsten Invarianten (ADR 0001, ADR 0002): pro URL
maximal ein schreibender Download, lebendig vs. Leiche entscheidet allein die
Registry, eine wachsende Datei wird nie unlinkt oder neu gestartet. Der
Zustand (_inflight, _progressive, _progressive_blocked, _incomplete_files …)
wird in Downloader.__init__ angelegt; _purge_stale_progressive bleibt dort,
weil es DOWNLOAD_DIR braucht.
"""
import asyncio
import copy
import time
from pathlib import Path

from utils.files import safe_unlink
from utils.logger import logger

STREAM_THRESHOLD_SECONDS = 20 * 60  # Videos > 20 min werden gestreamt statt heruntergeladen

# Progressiver Download: ungecachte Tracks ≤ STREAM_THRESHOLD_SECONDS werden
# nicht mehr als CDN-Stream gestartet (YouTube lehnt FFmpegs HTTP-Client oft
# mit 403 ab), sondern sofort via yt_dlp heruntergeladen; FFmpeg spielt die
# wachsende Datei, sobald genug gepuffert ist.
PROGRESSIVE_MIN_BYTES = 256 * 1024        # 160 kbit/s Opus ≈ 20 KB/s → ≈ 13 s Puffer
PROGRESSIVE_BYTES_PER_SECOND = 20 * 1024  # konservative Bitraten-Schätzung für Seek-/Resume-Puffer
PROGRESSIVE_WAIT_TIMEOUT = 15.0           # danach Fallback auf den alten CDN-Stream-Pfad
PROGRESSIVE_POLL_INTERVAL = 0.2           # Dateigrößen-Polling während der Pufferung
PROGRESSIVE_BLOCK_TTL = 600.0             # gescheiterte URL solange nicht erneut progressiv versuchen
PROGRESSIVE_SIDECAR_SUFFIX = ".inprogress"  # Crash-Marker neben der wachsenden Datei


class ProgressiveDownloadMixin:
    # ------------------------------------------------------------------
    # In-Flight-Registry: pro URL maximal ein schreibender Download
    # ------------------------------------------------------------------

    def inflight_download(self, url: str):
        """(task, quelle) des lebenden fremden Downloads für diese URL, sonst None.

        Die eigene Registrierung (derselbe Task) zählt nicht – ein Pfad darf
        nie auf sich selbst warten."""
        entry = (self._inflight or {}).get(url)
        if entry is None:
            return None
        task, _quelle = entry
        if task.done() or task is asyncio.current_task():
            return None
        return entry

    def _register_inflight(self, url: str, quelle: str, title: str = "", task=None):
        """Trägt task (Default: aufrufender Task) als den einen schreibenden
        Download für url ein. Läuft bereits ein anderer lebender Task, wird
        NICHT registriert und dessen (task, quelle) zurückgegeben – der
        Aufrufer wartet/adoptiert dann statt selbst zu laden. Deregistrierung
        macht der Pfad selbst im finally (_inflight_done); der done-Callback
        ist nur Sicherheitsnetz für Cancels, die das finally umgehen."""
        existing = self.inflight_download(url)
        if existing is not None:
            # Dauerhaftes Nachweis-Logging: hier hätte vor der Registry ein
            # zweiter paralleler Download derselben URL gestartet.
            logger.info(f"[Download] Bereits in Arbeit über {existing[1]} – kein zweiter Start: {title or url}")
            return existing
        if self._inflight is None:
            self._inflight = {}
        task = task or asyncio.current_task()
        self._inflight[url] = (task, quelle)
        task.add_done_callback(lambda t, u=url: self._inflight_done(u, t))
        return None

    def _inflight_done(self, url: str, task=None):
        """Entfernt die Registrierung – nur die eigene (task-Identität)."""
        entry = (self._inflight or {}).get(url)
        if entry is not None and (task is None or entry[0] is task):
            self._inflight.pop(url, None)

    # ------------------------------------------------------------------
    # Progressiver Download (wachsende Datei spielt, Download läuft weiter)
    # ------------------------------------------------------------------

    @staticmethod
    def _sidecar_for(filename: Path) -> Path:
        return filename.with_name(filename.name + PROGRESSIVE_SIDECAR_SUFFIX)

    def is_incomplete(self, path) -> bool:
        """True, wenn die Datei zu einem laufenden/gescheiterten progressiven
        Download gehört und daher nicht als fertiger Cache gelten darf."""
        if not self._incomplete_files:
            return False
        try:
            return Path(path).resolve() in self._incomplete_files
        except OSError:
            return False

    def progressive_task_for(self, url: str):
        """Laufender progressiver Download-Task für diese URL, sonst None."""
        return (self._progressive or {}).get(url)

    def block_progressive(self, url: str):
        """Sperrt eine URL für PROGRESSIVE_BLOCK_TTL Sekunden gegen weitere
        progressive Versuche (nach Fehlschlag/Timeout → Stream-Pfad)."""
        if self._progressive_blocked is None:
            self._progressive_blocked = {}
        self._progressive_blocked[url] = time.monotonic()

    def _progressive_is_blocked(self, url: str) -> bool:
        if not self._progressive_blocked:
            return False
        now = time.monotonic()
        # Abgelaufene Einträge lazy wegräumen.
        for u, ts in list(self._progressive_blocked.items()):
            if now - ts > PROGRESSIVE_BLOCK_TTL:
                self._progressive_blocked.pop(u, None)
        return url in self._progressive_blocked

    def _may_start_progressive(self, url: str, info: dict) -> bool:
        """Gates des progressiven Pfads: mp3 konvertiert erst NACH dem Download,
        m4a/mp4 ist wachsend nicht lesbar (moov-Atom am Dateiende), geblockte
        URLs sind gerade erst progressiv gescheitert. Einzige Quelle dieser
        Bedingung – resolve_track und prime_first_hit teilen sie sich."""
        return (self.audio_format != "mp3"
                and info.get("ext") in ("webm", "opus")
                and not self._progressive_is_blocked(url))

    def prime_first_hit(self, url: str, title: str) -> None:
        """Metadaten auflösen UND den progressiven Download sofort anstoßen.

        Für den ersten Treffer von !p, wenn gerade nichts läuft: der Download
        beginnt damit schon während die Such-/Statusnachrichten an Discord
        gehen, statt erst hinter ihnen. play_next adoptiert den laufenden Task
        später über die In-Flight-Registry (ADR 0002) – ein zweiter Writer
        entsteht dabei nie.
        """
        if url in self._pending_resolves:
            return
        info = self._url_cache.get(url)
        if info is not None:
            self._maybe_start_progressive(url, info, title)
            return

        async def _run():
            try:
                info = await self._fetch_info(url)
            except Exception:
                return          # Fehler meldet play_next dem Nutzer
            finally:
                self._pending_resolves.pop(url, None)
            self._maybe_start_progressive(url, info, title)

        self._pending_resolves[url] = asyncio.create_task(_run())

    def _maybe_start_progressive(self, url: str, info: dict, title: str) -> None:
        """Startet den progressiven Download, wenn alle Bedingungen stimmen –
        sonst passiert nichts und resolve_track entscheidet später neu."""
        if (info.get("duration") or 0) > STREAM_THRESHOLD_SECONDS:
            return          # langer Track → Stream, kein Download
        if not self._may_start_progressive(url, info):
            return
        if self.progressive_task_for(url) or self.inflight_download(url):
            return          # es schreibt schon jemand für diese URL
        try:
            filename = Path(self.ydl.prepare_filename(info))
        except Exception:
            return          # kaputter Cache-Eintrag → resolve_track räumt das auf
        if filename.exists():
            return          # schon da (fertig oder Leiche) → resolve_track klärt es
        self._start_progressive(url, info, filename, title)

    def _start_progressive(self, url: str, info: dict, filename: Path, title: str) -> asyncio.Task:
        """Registriert die Zieldatei als unvollständig (Set + Sidecar) und
        startet den Hintergrund-Download. Muss auf dem Event-Loop laufen.

        Idempotent – letzte Verteidigungslinie, falls ein Aufrufer die
        Registry-Konsultation umgeht: läuft für die URL bereits ein lebender
        Task (progressiv oder fremd), wird DER zurückgegeben statt ein
        zweiter Writer auf denselben Dateinamen gestartet (Marker/Sidecar
        werden dann auch nicht doppelt angelegt)."""
        if self._progressive is None:
            self._progressive = {}
        existing = self._progressive.get(url)
        if existing is not None and not existing.done():
            return existing
        fremd = self.inflight_download(url)
        if fremd is not None:
            return fremd[0]
        if self._progressive_files is None:
            self._progressive_files = {}
        if self._incomplete_files is None:
            self._incomplete_files = set()
        self._incomplete_files.add(filename.resolve())
        self._progressive_files[url] = filename
        try:
            # Marker muss existieren, bevor die ersten Datenbytes geschrieben
            # werden – winziger synchroner touch, analog zum last_queue.json-Write.
            self._sidecar_for(filename).touch()
        except OSError:
            pass
        task = asyncio.create_task(self._run_progressive(url, info, filename, title))
        self._progressive[url] = task
        self._register_inflight(url, "progressiv", title, task=task)
        logger.info(f"[Progressiv] Starte Hintergrund-Download: {title}")
        return task

    def _download_progressive_sync(self, ydl, info: dict, url: str, filename: Path):
        """Läuft im Worker-Thread. Volles, frisch extrahiertes Info-Dict →
        Schnellstart über process_ie_result (derselbe Pfad wie yt-dlps
        --load-info-json): spart die komplette Zweit-Extraktion samt
        JS-Challenge, die sonst 5–10 s Startlatenz vor dem ersten Byte kostet.
        Reduzierte Einträge aus metadata_cache.json haben keine "formats" →
        normaler Download mit frischer Extraktion. Scheitert der Schnellstart
        (z. B. abgelaufene CDN-URL), einmal frisch extrahieren – vorher die
        Teil-Datei entfernen, damit yt_dlp nicht an einen ggf. abweichenden
        Download anhängt (OSError propagiert in den Fehlerpfad des Aufrufers)."""
        if info.get("formats"):
            try:
                ydl.process_ie_result(copy.deepcopy(info), download=True)
                return
            except Exception as e:
                logger.info(f"[Progressiv] Schnellstart mit vorhandener Info fehlgeschlagen ({e}) – extrahiere frisch")
                if not safe_unlink(filename):
                    # Windows: FFmpeg liest die Teil-Datei noch → unlink schlägt
                    # fehl. Weiterladen wäre fatal: yt_dlp (continuedl) hängt an
                    # die vorhandene Datei an → korrupter Mischling. Abbrechen;
                    # der Fehlerpfad des Aufrufers blockt die URL, der
                    # Stream-Fallback übernimmt.
                    raise OSError(
                        f"Teil-Datei in Benutzung – progressiver Neuversuch abgebrochen: {filename.name}"
                    ) from e
        # download([webpage_url]) extrahiert intern frisch – eine ggf. abgelaufene
        # CDN-URL aus dem Cache ist egal; yt_dlps HTTP-Client trifft kein CDN-403.
        ydl.download([info.get("webpage_url") or url])

    async def _run_progressive(self, url: str, info: dict, filename: Path, title: str) -> bool:
        """Führt den progressiven Download aus. Fängt alle Fehler selbst;
        Rückgabe True = Datei vollständig."""
        try:
            ydl = self._make_progressive_ydl()
            await asyncio.to_thread(self._download_progressive_sync, ydl, info, url, filename)
            self._incomplete_files.discard(filename.resolve())
            safe_unlink(self._sidecar_for(filename))
            logger.info(f"[Progressiv] Download fertig: {filename.name}")
            await self.cleanup_downloads()
            return True
        except Exception as e:
            logger.warning(f"[Progressiv] Download fehlgeschlagen für {title}: {e} – unvollständige Datei wird entfernt")
            # Lag es an fehlender Authentifizierung, gilt das für alle weiteren
            # Tracks auch – Cookie-Modus einschalten, der nächste Anlauf (Stream-
            # Fallback bzw. force_download) läuft dann angemeldet.
            if self._needs_cookies(e):
                self.enable_cookie_mode(str(e)[:120])
            if safe_unlink(filename):
                safe_unlink(self._sidecar_for(filename))
                self._incomplete_files.discard(filename.resolve())
            else:
                # FFmpeg liest noch (Windows-Sharing) → Datei bleibt im Set und der
                # Sidecar liegen: in dieser Session gilt sie als "nicht vorhanden",
                # der nächste Bot-Start räumt sie via _purge_stale_progressive weg.
                # URL sofort blocken: jeder weitere progressive Versuch liefe in
                # dieselbe blockierte Teil-Datei (Stream-Fallback übernimmt).
                self.block_progressive(url)
                logger.warning(f"[Progressiv] Unvollständige Datei konnte nicht gelöscht werden (in Benutzung?): {filename.name}")
            return False
        finally:
            self._progressive.pop(url, None)
            self._progressive_files.pop(url, None)
            self._inflight_done(url, asyncio.current_task())

    async def _wait_for_buffer(self, filename: Path, task: asyncio.Task, min_buffer_seconds: int = 0) -> bool:
        """Wartet, bis die wachsende Datei genug Startpuffer hat (oder der
        Download komplett fertig ist). min_buffer_seconds erhöht die Schwelle
        um die Bytes bis zu einem -ss-Ziel: die cues-lose wachsende webm wird
        von FFmpeg linear bis dorthin geparst – diese Bytes müssen existieren.
        False = Timeout oder Download gescheitert (→ Aufrufer fällt auf Stream zurück).
        """
        min_bytes = PROGRESSIVE_MIN_BYTES + int(min_buffer_seconds) * PROGRESSIVE_BYTES_PER_SECOND
        deadline = time.monotonic() + PROGRESSIVE_WAIT_TIMEOUT
        while True:
            if task.done():
                try:
                    ok = task.result() is True
                except (asyncio.CancelledError, Exception):
                    ok = False
                return ok and filename.exists()
            try:
                size = filename.stat().st_size
            except OSError:
                size = 0
            if size >= min_bytes:
                return True
            if time.monotonic() > deadline:
                return False
            await asyncio.sleep(PROGRESSIVE_POLL_INTERVAL)

    async def wait_progressive_idle(self, timeout: float = 300.0):
        """Wartet, bis kein progressiver Download mehr läuft (Bandbreiten-
        Rücksicht für den Prefetch). Fehler/Timeout werden geschluckt."""
        deadline = time.monotonic() + timeout
        while self._progressive and time.monotonic() < deadline:
            await asyncio.sleep(PROGRESSIVE_POLL_INTERVAL)
