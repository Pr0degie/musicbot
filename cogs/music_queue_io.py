"""QueuePersistenceMixin: Queues speichern/laden (!saveq, !loadq, !lists)
und der automatische last_queue.json-Write nach jedem Track.

Per Mehrfachvererbung in MusicCommands eingebunden. Greift auf Instanz-State
(self.queue, self.current_track, self.is_playing) und die Kern-Methode
self.play_next zu, die in cogs/music.py definiert sind.
"""

import asyncio
import json
import threading
from pathlib import Path

from discord.ext import commands

from utils.files import atomic_write_text
from utils.i18n import t
from utils.logger import logger
from utils.paths import DATA_DIR

PLAYLISTS_DIR = DATA_DIR / "playlists"
PLAYLISTS_DIR.mkdir(parents=True, exist_ok=True)
# Session-Queue: nach jedem Track-Ende automatisch gesichert, !loadq last lädt sie.
LAST_QUEUE_FILE = DATA_DIR / "last_queue.json"

# Serialisiert die last_queue.json-Writes der Player-Threads (_write_last_queue).
_LAST_QUEUE_LOCK = threading.Lock()


def _write_playlist(path: Path, tracks: list) -> None:
    """Blockierender Datei-Write – immer via asyncio.to_thread aufrufen."""
    atomic_write_text(path, json.dumps(tracks, ensure_ascii=False, indent=2))


def _is_valid_playlist(tracks) -> bool:
    """saveq-Format: Liste von [url, titel]-Paaren, beides Strings."""
    return isinstance(tracks, list) and all(
        isinstance(entry, list)
        and len(entry) == 2
        and all(isinstance(part, str) for part in entry)
        for entry in tracks
    )


class QueuePersistenceMixin:
    # Folgenummern für last_queue.json (Klassen-Defaults für Tests via __new__):
    # vergeben auf dem Loop beim Snapshot, geprüft beim Schreiben im Thread.
    _last_queue_seq = 0
    _last_queue_written = 0

    @commands.command(name="saveq", usage="!saveq <name>")
    async def saveq(self, ctx, *, name: str):
        """Speichert die aktuelle Queue unter einem Namen. Verwendung: !saveq <name>"""
        if not self.queue and not self.current_track:
            await ctx.send(t("error.nothing_to_save"))
            return
        safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
        if not safe_name:
            await ctx.send(t("error.invalid_name"))
            return
        if safe_name.lower() == "last":
            # Reserviert: !loadq last lädt die automatisch gesicherte
            # Session-Queue (last_queue.json) – kein Playlist-Name.
            await ctx.send(t("error.name_reserved", name=safe_name))
            return
        tracks = []
        if self.current_track:
            ct_url, ct_title, *_ = self.current_track
            tracks.append([ct_url, ct_title])
        tracks.extend([url, title] for url, title in self.queue)
        path = PLAYLISTS_DIR / f"{safe_name}.json"
        await asyncio.to_thread(_write_playlist, path, tracks)
        await ctx.send(t("status.queue_saved", name=safe_name, count=len(tracks)))

    @commands.command(name="loadq", usage="!loadq <name>")
    async def loadq(self, ctx, *, name: str):
        """Lädt eine gespeicherte Queue und hängt sie an die aktuelle an.
        Verwendung: !loadq <name>; !loadq last = Queue der letzten Session."""
        safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
        if safe_name.lower() == "last":
            # Sondername: die nach jedem Track automatisch gesicherte
            # Session-Queue (data/last_queue.json). Bewusst
            # KEIN Auto-Restore beim Start – der Bot startet leer, das hier
            # ist der explizite Weg zurück zur letzten Session.
            path = LAST_QUEUE_FILE
            safe_name = "last"
        else:
            path = PLAYLISTS_DIR / f"{safe_name}.json"
        if not path.exists():
            await ctx.send(t("error.queue_not_found", name=safe_name))
            return
        try:
            with open(path, encoding="utf-8") as f:
                tracks = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            await ctx.send(t("error.playlist_invalid", name=safe_name))
            return
        if not _is_valid_playlist(tracks):
            await ctx.send(t("error.playlist_invalid", name=safe_name))
            return
        if len(tracks) > self.HARD_PLAYLIST_LIMIT:
            await ctx.send(t("error.playlist_too_large", name=safe_name, limit=self.HARD_PLAYLIST_LIMIT))
            return
        for url, title in tracks:
            self.queue.append((url, title))
        await ctx.send(t("status.queue_loaded", name=safe_name, count=len(tracks)))
        if not self.is_playing:
            if self.queue:
                # Startet gleich → Download schon während des ctx.send anstoßen
                # (Schnellstart wie beim !p-Erstreffer).
                nxt_url, nxt_title = self.queue[0]
                self.dl.prime_first_hit(nxt_url, nxt_title)
            self.is_playing = True
            await self.play_next(ctx)
        else:
            self._kick_prefetch()   # läuft schon Musik → geladene Titel sofort vorladen

    @commands.command(name="lists")
    async def lists(self, ctx):
        """Zeigt alle gespeicherten Queues."""
        files = sorted(PLAYLISTS_DIR.glob("*.json"))
        if not files:
            await ctx.send(t("status.no_saved_queues"))
            return
        lines = []
        for p in files:
            try:
                with open(p, encoding="utf-8") as f:
                    count = len(json.load(f))
                lines.append(f"• **{p.stem}** ({count} Titel)")
            except Exception:
                lines.append(f"• **{p.stem}**")
        await ctx.send(t("status.saved_queues", lines="\n".join(lines)))

    def _write_last_queue(self, seq, queue_data):
        """Schreibt den Queue-Snapshot eines Track-Endes nach last_queue.json.

        Blockierendes Datei-I/O – läuft im Player-Thread (aus after_playing),
        nicht auf dem Event-Loop. Der Write passiert nach dem Anstoßen
        des nächsten Tracks; stirbt der sofort, könnten sich zwei Writes
        überholen. Lock + Folgenummer sorgen dafür, dass nie ein älterer
        Snapshot einen neueren überschreibt."""
        with _LAST_QUEUE_LOCK:
            if seq < self._last_queue_written:
                return
            try:
                atomic_write_text(LAST_QUEUE_FILE, json.dumps(queue_data, ensure_ascii=False, indent=2))
                self._last_queue_written = seq
                logger.info("[SAVE] Queue automatisch gespeichert nach Track-Ende.")
            except Exception as e:
                logger.warning(f"[SAVE] Fehler beim Speichern der Queue: {e}")
