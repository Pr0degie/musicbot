"""Hintergrund-Tasks, deren Ergebnis niemand abholt.

asyncio hält Tasks nur schwach: ohne eigene Referenz kann der Garbage Collector
einen noch laufenden Task abräumen, und eine Exception darin taucht höchstens
als „Task exception was never retrieved" beim Aufräumen auf. spawn() hält die
Referenz bis zum Ende und loggt Fehler sofort.

Tasks, die der Aufrufer selbst in einem Attribut hält (prefetch_task,
idle_leave_task, …), brauchen das nicht.
"""

import asyncio

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
