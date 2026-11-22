"""Such- und Einreih-Flow von !p, !next und !now – Mixin für PlaybackMixin.

yt_dlp-Abfrage mit Status- und Fehlermeldung (_extract_info_or_report),
Suchzweig mit Alternativ-Buttons (_search_and_enqueue), Direkt-URL-Zweig
(_fetch_single_track_info) und der Radio-Vorspann (_stop_radio_for_takeover).
"""

import asyncio

from utils.logger import logger
from utils.tasks import spawn
from utils.voice import stop_playback
from utils.i18n import t
from views.music_controls import SearchAutoplayView

# Sentinel für _extract_info_or_report: unterscheidet "Fehler wurde bereits
# gemeldet" von einem echten None-Ergebnis aus yt_dlp (das die Aufrufer wie
# bisher selbst krachen lassen).
_YTDLP_FAILED = object()

# Laufende Hintergrund-Aufräumarbeiten (Status-Nachricht löschen): asyncio hält
# Tasks nur schwach – ohne Referenz könnte der GC sie mitten im Lauf abräumen.
_BACKGROUND_TASKS: set = set()


def search_query(eingabe: str) -> str:
    """yt_dlp-Suchanfrage für einen Suchbegriff: 3 Treffer (1 spielt, 2 Alternativen)."""
    return f"ytsearch3:{eingabe}"


def start_early(coro) -> "asyncio.Task":
    """Abfrage sofort als Task starten (läuft parallel z. B. zum Voice-Join).

    Wird sie nie abgeholt (Join scheitert), schluckt der Callback das
    Ergebnis bzw. die Exception – sonst meldet asyncio „Task exception was
    never retrieved"."""
    task = asyncio.create_task(coro)
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return task


class EnqueueMixin:
    async def _stop_radio_for_takeover(self, ctx):
        """Beendet einen laufenden Radio-Stream, bevor ein Song übernimmt.

        Gemeinsamer Vorspann von !p, !next und !now: Radio stoppen und auf das
        Ende der laufenden Wiedergabe warten (max. 3 s), damit der neue Song
        nicht mit dem Radio-Callback kollidiert.
        """
        if not self.is_radio:
            return
        self._stop_radio()
        if ctx.voice_client and ctx.voice_client.is_playing():
            stop_playback(ctx.voice_client)
            await self._wait_playback_done()
            self._playback_done.clear()

    async def _extract_info_or_report(self, ctx, query, kind, *,
                                      status_key, timeout_key, error_key, log_msg, pending=None):
        """Ruft yt_dlp-Infos ab und meldet Timeout/Fehler direkt im Channel.

        Gemeinsamer Fetch-Teil der Such- und URL-Zweige von !p, !next und !now.
        `kind` wählt die yt_dlp-Instanz ("search"/"url"/"playlist"), aufgelöst
        erst im Downloader – so greift dort auch der Cookie-Fallback bei
        Alterssperre/Bot-Check (ADR 0010). Gibt das Info-Dict zurück oder
        _YTDLP_FAILED, wenn bereits eine Fehlermeldung gesendet wurde – auch
        wenn yt_dlp None liefert (z. B. YouTube-Mix-Link, den yt_dlp nicht
        auflöst); vorher lief das als TypeError in die Aufrufer.

        Die Status-Nachricht ("Suche läuft..." bzw. "Verarbeite...") wird nach
        Abschluss wieder gelöscht – bei Erfolg wie im Fehlerfall (dann steht
        die Fehlermeldung im Channel).

        Senden und Löschen liegen nicht im kritischen Pfad: die Abfrage läuft
        parallel zum Senden, gelöscht wird im Hintergrund – sonst warteten
        Suche und Download-Start auf zwei Discord-Roundtrips. `pending` ist
        eine bereits gestartete Abfrage (z. B. von !p parallel zum Voice-Join).
        """
        status_task = asyncio.create_task(ctx.send(t(status_key)))
        try:
            info = await (pending if pending is not None else self.dl.extract_info_async(query, kind))
            if info is None:
                logger.warning(f"{log_msg}: yt_dlp lieferte keine Infos für {query[:120]}")
                await self._report_after_status(ctx, status_task, t(error_key))
                return _YTDLP_FAILED
            return info
        except asyncio.TimeoutError:
            logger.warning(f"{log_msg}: Timeout für {query[:120]}")
            await self._report_after_status(ctx, status_task, t(timeout_key))
            return _YTDLP_FAILED
        except Exception:
            logger.exception(log_msg)
            await self._report_after_status(ctx, status_task, t(error_key))
            return _YTDLP_FAILED
        finally:
            cleanup = asyncio.create_task(self._delete_status(status_task))
            _BACKGROUND_TASKS.add(cleanup)   # starke Referenz, sonst kann der GC ihn abräumen
            cleanup.add_done_callback(_BACKGROUND_TASKS.discard)

    @staticmethod
    async def _report_after_status(ctx, status_task, text):
        """Fehlermeldung erst nach der Status-Nachricht senden – sonst stünde
        sie bei einem schnellen Fehlschlag vor „Suche läuft…" im Channel.
        Im Fehlerpfad kostet das Warten nichts, was zählt."""
        await asyncio.wait([status_task])
        await ctx.send(text)

    @staticmethod
    async def _delete_status(status_task):
        """Status-Nachricht löschen, sobald sie gesendet ist (Hintergrund-Task)."""
        try:
            msg = await status_task
            await msg.delete()
        except Exception:
            pass   # nie gesendet, schon weg oder keine Berechtigung – egal

    async def _search_and_enqueue(self, ctx, eingabe, *, log_tag, timeout_key,
                                  insert, with_alts_key, no_alts_key,
                                  base_content_key=None, pending=None):
        """Gemeinsamer Suchbegriff-Zweig von !p, !next und !now.

        ytsearch3 → ersten Treffer vorab auflösen und einreihen, Treffer 2
        und 3 als Buttons anzeigen (SearchAutoplayView) falls es der Falsche
        war. Die drei Befehle unterscheiden sich bewusst – die Unterschiede
        stecken in den Parametern:

        - !p:    insert="evict_or_back" – wenn Autoplay einen Song vorgemerkt
                 hat, fliegt der raus (nur bei aktiviertem Autoplay) und der
                 manuelle Wunsch landet VORN in der Queue, sonst hinten.
                 Die Eviction wird geloggt.
        - !next: insert="front" – keine Eviction, Treffer immer vorn;
                 eigene Meldungs-Keys (next_added/next_with_alts) und
                 base_content_key für die View.
        - !now:  insert="evict_front" – Eviction immer (Rückgabe ignoriert),
                 Treffer vorn; die laufende Wiedergabe stoppt der Aufrufer
                 danach selbst.

        Gibt True zurück wenn ein Treffer eingereiht wurde, sonst False
        (die Fehlermeldung wurde dann bereits gesendet). `pending`: bereits
        gestartete Suche (search_query()), siehe !p.
        """
        results = await self._extract_info_or_report(
            ctx, search_query(eingabe), "search",
            status_key="status.searching", timeout_key=timeout_key,
            error_key="error.search_error",
            log_msg=f"[{log_tag}] Fehler bei Suche",
            pending=pending,
        )
        if results is _YTDLP_FAILED:
            return False

        entries = (results.get("entries") or [])[:3]
        if not entries:
            await ctx.send(t("error.no_results"))
            return False

        first = entries[0]
        url = first.get("webpage_url") or first.get("url")
        title = first.get("title", t("misc.unknown_title"))
        self._warm_or_prime(url, title)
        self._insert_hit(url, title, insert, log_tag)
        await self._announce_hit(
            ctx, first, entries[1:], title,
            with_alts_key=with_alts_key, no_alts_key=no_alts_key, base_content_key=base_content_key,
        )
        return True

    def _warm_or_prime(self, url, title):
        """Schritt 1 von _search_and_enqueue: den Treffer vorbereiten."""
        if self.is_playing:
            # Läuft schon Musik → nur Metadaten vorwärmen; geladen wird per Prefetch,
            # wenn der Titel an der Reihe ist (Bandbreite bleibt beim aktuellen Song).
            spawn(self.dl._start_resolve(url), "Metadaten-Vorwärmen")
        else:
            # Dieser Treffer startet gleich → Download sofort anstoßen, damit er
            # parallel zu den folgenden Discord-Nachrichten läuft statt danach.
            self.dl.prime_first_hit(url, title)

    def _insert_hit(self, url, title, insert, log_tag):
        """Schritt 2 von _search_and_enqueue: den Treffer nach `insert`
        einreihen (Bedeutung der drei Modi siehe dort)."""
        if insert == "evict_or_back":
            evicted = self._evict_autoplay_song() if self.autoplay_enabled else None
            if evicted:
                logger.info(f"[{log_tag}] Autoplay-Song verdrängt: {evicted}")
                self.queue.appendleft((url, title))
            else:
                self.queue.append((url, title))
        elif insert == "evict_front":
            self._evict_autoplay_song()
            self.queue.appendleft((url, title))
        else:  # "front"
            self.queue.appendleft((url, title))

    async def _announce_hit(self, ctx, first, alternatives, title, *,
                            with_alts_key, no_alts_key, base_content_key):
        """Schritt 3 von _search_and_enqueue: den eingereihten Treffer melden,
        Alternativen (Treffer 2 und 3) als Buttons anbieten."""
        if alternatives:
            view = SearchAutoplayView(
                first, alternatives, self, ctx,
                base_content=t(base_content_key, title=title) if base_content_key else None,
            )
            letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            alt_lines = "\n".join(
                t("misc.option_line", letter=letters[i], title=e.get("title", t("misc.unknown_title")))
                for i, e in enumerate(alternatives)
            )
            msg = await ctx.send(
                t(with_alts_key, title=title, alts=alt_lines),
                view=view,
            )
            view.message = msg
        else:
            await ctx.send(t(no_alts_key, title=title))

    async def _fetch_single_track_info(self, ctx, eingabe, log_tag):
        """Gemeinsamer Direkt-URL-Zweig von !next und !now (ohne Playlists).

        Löst die URL via url_ydl auf; Playlists werden abgelehnt
        (error.next_no_playlist). Gibt (url, title) zurück oder None, wenn
        bereits eine Fehlermeldung gesendet wurde. !p behält seinen eigenen
        URL-Zweig: Playlist-Unterstützung, Duplikat-Warnung und andere
        Meldungs-Keys.
        """
        info = await self._extract_info_or_report(
            ctx, eingabe, "url",
            status_key="status.processing_url", timeout_key="error.timeout",
            error_key="error.fetch_error",
            log_msg=f"[{log_tag}] Fehler beim Abrufen von yt_dlp-Infos",
        )
        if info is _YTDLP_FAILED:
            return None

        if "entries" in info:
            await ctx.send(t("error.next_no_playlist"))
            return None

        url = info.get("webpage_url") or eingabe
        title = info.get("title", t("misc.unknown_title"))
        return (url, title)
