"""Prefetch-Subsystem der Wiedergabe – Mixin für PlaybackMixin.

Stößt das Vorladen der nächsten Queue-Songs und des nächsten Autoplay-Songs
an (geladen wird im Downloader, cogs/downloader_prefetch.py) und nimmt einen
von Autoplay vorgereihten Song wieder aus der Queue.
"""

import asyncio

from utils.logger import logger


class PrefetchMixin:
    async def _prefetch_next(self, idx: int = 0):
        """Lädt Song an Position idx der Queue still im Hintergrund herunter."""
        await self.dl.prefetch_next(self.queue, idx)

    async def _prefetch_upcoming(self):
        """Lädt bis zu 2 Queue-Songs sequenziell vor – yt_dlp ist nicht
        thread-safe, daher kein paralleles gather. Sequenziell reicht: während
        Song N spielt, werden N+1 und N+2 nacheinander heruntergeladen."""
        # Bandbreiten-Rücksicht: dem progressiven Download des laufenden Songs
        # eine kurze Startphase lassen, damit der erste Puffer schnell steht.
        # Hart gekappt: das frühere globale Idle-Warten (bis 300 s) machte den
        # Queue-Prefetch praktisch tot – der laufende Song lädt fast immer
        # progressiv, der geparkte Task machte alle _kick_prefetch zu No-Ops.
        await self.dl.wait_progressive_idle(timeout=15.0)
        await self._prefetch_next(0)
        if len(self.queue) >= 2:
            await self._prefetch_next(1)

    def _kick_prefetch(self):
        """Startet den Hintergrund-Download der nächsten Queue-Songs, wenn
        gerade ein Song läuft und noch kein Prefetch aktiv ist. Nötig für
        Titel, die MITTEN im Song eingereiht werden (!p/!next): play_next
        erstellt den Prefetch-Task nur beim Trackstart – da war die Queue
        oft noch leer, und der neue Song würde erst beim Übergang (progressiv,
        inkl. yt_dlp-Extraktion) geladen → hörbare Lücke."""
        if not self.queue or not self.is_playing:
            return
        if self.prefetch_task and not self.prefetch_task.done():
            # Läuft bereits – _prefetch_upcoming peekt die Queue bei jedem
            # Schritt frisch, neue Einträge werden also noch mitgenommen.
            return
        self.prefetch_task = asyncio.create_task(self._prefetch_upcoming())

    def _restart_prefetch(self):
        """Nach Queue-Umbau (!shuffle/!move/!remove, Alternativ-Buttons): der
        laufende Prefetch zielt noch auf die alte Reihenfolge. _kick_prefetch
        allein reicht nicht (No-Op-Guard bei lebendem Task) → Task abbrechen
        und neu kicken. Ein bereits schreibender Download-Worker wird dabei
        nicht gekillt, sondern adoptiert und läuft zu Ende (ADR 0002)."""
        if self.prefetch_task and not self.prefetch_task.done():
            self.prefetch_task.cancel()
            self.prefetch_task = None
        self._kick_prefetch()

    async def _prefetch_autoplay(self, ctx):
        """Sucht und lädt nächsten Autoplay-Song im Hintergrund während der aktuelle läuft."""
        ref_url = ref_title = None
        if self.current_track:
            ref_url, ref_title, *_ = self.current_track
        result = await self.dl.prefetch_autoplay(ref_url, ref_title, self._recently_played, self._recently_played_titles)
        if result and self.autoplay_enabled:
            url, title = result
            self.queue.append((url, title))
            self._autoplay_queued_url = url
            logger.info(f"[Autoplay Prefetch] In Queue eingereiht: {title}")

    def _evict_autoplay_song(self) -> str | None:
        """Entfernt den von Autoplay vorgereihten Song aus der Queue (falls vorhanden).

        Bricht außerdem einen laufenden Prefetch-Task ab, damit kein weiterer
        Autoplay-Song nachgeschoben wird bevor der manuelle !p-Song gespielt hat.
        Gibt den Titel des entfernten Songs zurück (für Logging), sonst None.
        """
        # Laufenden Prefetch sofort stoppen
        if self._autoplay_prefetch_task and not self._autoplay_prefetch_task.done():
            self._autoplay_prefetch_task.cancel()
            self._autoplay_prefetch_task = None

        if not self._autoplay_queued_url:
            return None

        removed_title = None
        for i, (queued_url, queued_title) in enumerate(self.queue):
            if queued_url == self._autoplay_queued_url:
                del self.queue[i]
                removed_title = queued_title
                self._autoplay_queued_url = None
                break
        return removed_title
