"""utils.tasks.spawn: Hintergrund-Tasks mit gehaltener Referenz und Fehler-Log."""
import ast
import asyncio
import logging
from pathlib import Path

import utils.tasks as tasks_mod
from utils.tasks import spawn

ROOT = Path(__file__).resolve().parent.parent


def test_referenz_wird_bis_zum_ende_gehalten():
    async def run():
        gate = asyncio.Event()

        async def arbeit():
            await gate.wait()
            return 42

        task = spawn(arbeit(), "arbeit")
        await asyncio.sleep(0)
        assert task in tasks_mod._RUNNING
        gate.set()
        assert await task == 42
        await asyncio.sleep(0)
        assert task not in tasks_mod._RUNNING

    asyncio.run(run())


def test_exception_wird_geloggt_und_abgeholt(caplog):
    async def run():
        async def kaputt():
            raise RuntimeError("bumm")

        task = spawn(kaputt(), "kaputt")
        await asyncio.wait([task])
        await asyncio.sleep(0)
        assert task not in tasks_mod._RUNNING

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    fehler = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(fehler) == 1
    assert "kaputt" in fehler[0].getMessage() and "bumm" in fehler[0].getMessage()
    assert fehler[0].exc_info is not None, "Traceback gehört ins Log"


def test_abgebrochener_task_loggt_nichts(caplog):
    async def run():
        task = spawn(asyncio.sleep(30), "schlaf")
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait([task])
        await asyncio.sleep(0)
        assert task not in tasks_mod._RUNNING

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_kein_create_task_ohne_gehaltene_referenz():
    """Entspricht ruff RUF006: asyncio.create_task(...) als nackte Anweisung
    (Rückgabewert verworfen) darf es in cogs/, views/, utils/ und main.py
    nicht geben – dafür ist spawn() da."""
    treffer = []
    dateien = [ROOT / "main.py"]
    for ordner in ("cogs", "views", "utils"):
        dateien += sorted((ROOT / ordner).glob("*.py"))
    for pfad in dateien:
        baum = ast.parse(pfad.read_text(encoding="utf-8"))
        for knoten in ast.walk(baum):
            if not (isinstance(knoten, ast.Expr) and isinstance(knoten.value, ast.Call)):
                continue
            func = knoten.value.func
            if isinstance(func, ast.Attribute) and func.attr in ("create_task", "ensure_future"):
                treffer.append(f"{pfad.relative_to(ROOT)}:{knoten.lineno}")
    assert not treffer, "create_task ohne Referenz – utils.tasks.spawn benutzen:\n" + "\n".join(treffer)
