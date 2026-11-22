"""Prefetch – Mixin für Downloader.

Lädt den nächsten Queue-Song (prefetch_next) bzw. den nächsten Autoplay-Song
(prefetch_autoplay) still im Hintergrund. Jeder Download steht in der
In-Flight-Registry (ADR 0002); die Queue-Downloads laufen sequenziell unter
dem Lock in _prefetch_one, weil yt_dlp nicht thread-safe ist.
"""
import asyncio
import time
from pathlib import Path

from utils.files import safe_unlink
from utils.logger import logger
from utils.autoplay import choose_autoplay_candidate, entry_url, select_autoplay_candidates, yt_video_id
from cogs.downloader_progressive import STREAM_THRESHOLD_SECONDS


class PrefetchMixin:
    # Serialisiert die eigentlichen yt_dlp-Downloads der Prefetch-Tasks:
    # yt_dlp ist nicht thread-safe, und ein adoptierter (vom Aufrufer
    # gecancelter, aber weiterlaufender) Download darf nie parallel zu einem
    # frisch gekickten laufen. Lazy angelegt (Tests via __new__).
    _prefetch_dl_lock = None

    async def prefetch_next(self, queue, idx: int = 0):
        """Lädt Song an Position idx der Queue still im Hintergrund herunter.

        Der eigentliche Download läuft als eigener Task pro URL, der auch in
        der In-Flight-Registry steht: resolve_track wartet damit nur auf die
        konkret gebrauchte URL statt auf den Sammel-Task des Aufrufers, und
        ein Cancel des Aufrufers (play_next, !clear, Queue-Umbau) killt den
        Worker-Thread nicht mitten im Schreiben – der Download wird adoptiert,
        läuft zu Ende und deregistriert sich erst dann selbst (ADR 0002).
        """
        if len(queue) <= idx:
            return
        url, title = queue[idx]  # Peek – nicht aus der Queue entfernen
        if self.progressive_task_for(url) is not None:
            # Läuft bereits progressiv → nie zwei Downloads auf dieselbe Datei
            # (das .part-Rename des Prefetch liefe gegen die offene FFmpeg-Datei).
            logger.info(f"[Prefetch] Übersprungen – läuft bereits progressiv: {title}")
            return
        worker = asyncio.create_task(self._prefetch_one(url, title))
        if self._register_inflight(url, "Queue-Prefetch", title, task=worker) is not None:
            # Anderer Pfad schreibt diese URL bereits – kein zweiter Writer.
            # Der Worker hat noch keine Zeile ausgeführt → sauber abbrechen.
            worker.cancel()
            return
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            if not worker.done():
                logger.info(f"[Prefetch] Aufrufer abgebrochen – laufender Download wird adoptiert und läuft zu Ende: {title}")
            raise
        except Exception:
            pass  # Fehler hat _prefetch_one bereits geloggt – Prefetch ist nie fatal

    async def _prefetch_one(self, url: str, title: str):
        """Der eigentliche Prefetch-Download einer URL (siehe prefetch_next).
        Deregistriert sich erst im finally – solange der Worker-Thread
        schreibt, meldet die Registry also einen lebenden Writer."""
        try:
            info = self._usable_cached_info(url)   # abgelaufen (CACHE_TTL) = Cache-Miss
            if info is not None:
                logger.info(f"[Prefetch] Metadaten aus Cache: {title}")
            else:
                info = await self._fetch_info(url)
            if info.get("duration", 0) > STREAM_THRESHOLD_SECONDS:
                logger.info(f"[Prefetch] Übersprungen – {info.get('title', title)} wird gestreamt")
                return
            try:
                filename = Path(self.ydl.prepare_filename(info))
            except Exception:
                # Kaputter/reduzierter Cache-Eintrag → verwerfen, frisch
                # extrahieren (Cache-Miss-Pfad). Schlägt auch das fehl, fängt
                # der äußere except unten – Prefetch-Fehler sind nie fatal.
                self.invalidate(url)
                info = await self._fetch_info(url)
                if info.get("duration", 0) > STREAM_THRESHOLD_SECONDS:
                    return
                filename = Path(self.ydl.prepare_filename(info))
            if filename.exists() and self.is_incomplete(filename):
                # Leiche eines gescheiterten progressiven Downloads → erst weg,
                # sonst hält yt_dlp die Datei für fertig. Blockiert (FFmpeg liest
                # noch) → diesen Prefetch auslassen.
                if not safe_unlink(filename):
                    return
                safe_unlink(self._sidecar_for(filename))
                self._incomplete_files.discard(filename.resolve())
            if not filename.exists():
                if self._prefetch_dl_lock is None:
                    self._prefetch_dl_lock = asyncio.Lock()
                # Lock statt Task-Reihenfolge: auch ein adoptierter Download
                # (Aufrufer gecancelt, Worker läuft weiter) bleibt so garantiert
                # der einzige aktive yt_dlp-Download (nicht thread-safe).
                async with self._prefetch_dl_lock:
                    if filename.exists():
                        logger.info(f"[Prefetch] Bereits im Cache: {filename.name}")
                        return
                    logger.info(f"[Prefetch] Lade vor: {info.get('title', title)}")
                    await asyncio.to_thread(self.ydl.download, [info.get("webpage_url") or url])
                logger.info(f"[Prefetch] Fertig: {filename.name}")
                await self.cleanup_downloads()
            else:
                logger.info(f"[Prefetch] Bereits im Cache: {filename.name}")
        except asyncio.TimeoutError:
            logger.warning(f"[Prefetch] Timeout für: {title}")
        except Exception as e:
            # Prefetch-Fehler sind nicht fatal – resolve_track lädt im Zweifelsfall selbst.
            logger.warning(f"[Prefetch] Vorladen fehlgeschlagen für: {title}: {e}")
        finally:
            self._inflight_done(url, asyncio.current_task())

    async def prefetch_autoplay(self, ref_url, ref_title, recently_played, recently_played_titles=()):
        """Sucht + lädt nächsten Autoplay-Song im Hintergrund.

        Returns: (url, title) bei Erfolg, None bei Fehler/kein Ergebnis.
        Der Caller entscheidet ob der Song in die Queue kommt (autoplay_enabled-Check).
        """
        yt_id = yt_video_id(ref_url)

        fetch_url = (
            f"https://www.youtube.com/watch?v={yt_id}&list=RD{yt_id}"
            if yt_id
            else (f"ytsearch5:{ref_title}" if ref_title else "ytsearch5:top music")
        )
        logger.info(f"[Autoplay Prefetch] Starte Hintergrundsuche für: {ref_title!r}")

        try:
            info = await self.extract_info_async(fetch_url, "autoplay")
            entries = (info.get("entries") or []) if info else []

            candidates = select_autoplay_candidates(
                entries, ref_url, recently_played, recently_played_titles, ref_title=ref_title
            )
            if not candidates:
                logger.warning("[Autoplay Prefetch] Keine nutzbaren Einträge")
                return None

            chosen = choose_autoplay_candidate(candidates)
            url = entry_url(chosen)
            title = chosen.get("title", "Unbekannt")

            logger.info(f"[Autoplay Prefetch] Lade vor: {title}")
            existing = self._register_inflight(url, "Autoplay-Prefetch", title)
            if existing is not None:
                # Anderer Pfad schreibt diese URL bereits – dessen Ende abwarten
                # statt parallel zu laden; der Song wird trotzdem eingereiht.
                try:
                    await asyncio.wait_for(asyncio.shield(existing[0]), timeout=120.0)
                except Exception:
                    pass
                return url, title
            try:
                # extract_info(download=True) lädt die Datei herunter UND gibt das volle
                # Info-Dict zurück (inkl. ext, webpage_url). Das flache Entry aus autoplay_ydl
                # hat diese Felder nicht – prepare_filename() und info["webpage_url"] in
                # resolve_track() würden sonst fehlschlagen.
                full_info = await asyncio.wait_for(
                    asyncio.to_thread(self.ydl.extract_info, url, download=True),
                    timeout=120.0,
                )
                if full_info:
                    if "entries" in full_info:
                        full_info = full_info["entries"][0]
                    self._url_cache[url] = full_info
                    self._cache_timestamps[url] = time.time()
                    self._cache_dirty = True
                logger.info(f"[Autoplay Prefetch] Fertig: {title}")
                # Der frisch geladene Song hängt noch nicht in der Queue (macht der
                # Caller) → seine Datei explizit vor dem Cleanup schützen.
                just_downloaded = None
                if full_info:
                    try:
                        just_downloaded = Path(self.ydl.prepare_filename(full_info))
                    except Exception:
                        pass
                await self.cleanup_downloads(extra_protected=just_downloaded)
            finally:
                self._inflight_done(url, asyncio.current_task())
            return url, title

        except asyncio.TimeoutError:
            logger.warning("[Autoplay Prefetch] Timeout – play_next fällt auf normales autoplay() zurück")
            return None
        except Exception as e:
            logger.warning(f"[Autoplay Prefetch] Fehler: {e}")
            return None
