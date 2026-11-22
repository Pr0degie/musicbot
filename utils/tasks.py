"""Hintergrund-Tasks, deren Ergebnis niemand abholt.

asyncio hält Tasks nur schwach: ohne eigene Referenz kann der Garbage Collector
einen noch laufenden Task abräumen, und eine Exception darin taucht höchstens
als „Task exception was never retrieved" beim Aufräumen auf. spawn() hält die
Referenz bis zum Ende und loggt Fehler sofort.

Tasks, die der Aufrufer selbst in einem Attribut hält (prefetch_task,
idle_leave_task, …), brauchen das nicht.

run_on_loop() ist der umgekehrte Weg: ein fremder Thread übergibt Arbeit an
den Event-Loop und wartet auf das Ergebnis (after-Callbacks der Player).
"""

import asyncio
import concurrent.futures

from utils.logger import logger

# Starke Referenzen auf laufende Tasks; der done-Callback trägt sie wieder aus.
_RUNNING: set = set()


def spawn(coro, name: str) -> "asyncio.Task":
    """Wie asyncio.create_task(coro), nur mit gehaltener Referenz und Fehler-Log.

    name beschreibt den Task für die Log-Zeile (z. B. "play_next")."""
    task = asyncio.create_task(coro)
    _RUNNING.add(task)
    task.add_done_callback(lambda finished: _finished(finished, name))
    return task


def _finished(task, name: str) -> None:
    _RUNNING.discard(task)
    if task.cancelled():
        return
    exc = task.exception()   # markiert die Exception zugleich als abgeholt
    if exc is not None:
        logger.error(f"[Task] Hintergrund-Task {name} ist gescheitert: {type(exc).__name__}: {exc}", exc_info=exc)


# So lange wartet ein fremder Thread (Player-Thread von discord.py) höchstens
# darauf, dass der Event-Loop die Arbeit übernimmt (normal: Millisekunden).
LOOP_HANDOFF_TIMEOUT = 10.0


def run_on_loop(loop, fn, *args):
    """Führt fn(*args) auf dem Event-Loop `loop` aus und gibt das Ergebnis zurück.

    Aus einem fremden Thread (Player-Thread von discord.py): an den Loop
    übergeben und auf das Ergebnis warten – eine Exception aus fn kommt beim
    Aufrufer an, wie wenn er fn selbst ausgeführt hätte. Nach
    LOOP_HANDOFF_TIMEOUT Sekunden concurrent.futures.TimeoutError.
    Wer schon auf dem Loop ist, ruft fn direkt auf (Warten wäre ein Deadlock).
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        return fn(*args)
    done = concurrent.futures.Future()

    def run():
        try:
            done.set_result(fn(*args))
        except BaseException as exc:
            done.set_exception(exc)

    loop.call_soon_threadsafe(run)
    return done.result(timeout=LOOP_HANDOFF_TIMEOUT)
