"""Tests für den zweistufigen Strg+C-Handler (utils/shutdown.py).

Erstes SIGINT fragt nur nach, das zweite innerhalb des Fensters fährt geordnet
herunter (bot.close() über den Loop), ein drittes während des Herunterfahrens
beendet den Prozess hart. Läuft das Fenster ab, ist der Bot wieder entschärft.
"""

import signal
import types

import pytest

import utils.shutdown as shutdown_mod
from utils.shutdown import install_sigint_handler


class FakeLoop:
    """Minimaler Loop: führt threadsafe-Callbacks sofort aus, merkt sich Timer."""

    def __init__(self):
        self.timers = []

    def is_running(self):
        return True

    def call_soon_threadsafe(self, fn, *args):
        return fn(*args)

    def call_later(self, delay, fn, *args):
        self.timers.append((delay, fn, args))

    def create_task(self, coro):
        # Die Fake-Coroutinen haben kein await – ein einzelnes send() läuft sie durch.
        try:
            coro.send(None)
        except StopIteration:
            pass

    def fire_timers(self):
        timers, self.timers = self.timers, []
        for _, fn, args in timers:
            fn(*args)


class FakeBot:
    def __init__(self, with_music=True):
        self.loop = FakeLoop()
        self.music = types.SimpleNamespace(_stopped_by_user=False) if with_music else None
        self.closed = 0

    def get_cog(self, name):
        return self.music if name == "MusicCommands" else None

    async def close(self):
        self.closed += 1


class FakeLogger:
    def __init__(self):
        self.lines = []

    def info(self, msg):
        self.lines.append(msg)

    def warning(self, msg):
        self.lines.append(msg)

    def exception(self, msg):
        self.lines.append(msg)

    def text(self):
        return "\n".join(self.lines)


@pytest.fixture
def env(monkeypatch):
    """Handler mit kontrollierter Uhr, abgefangenem os._exit und Fake-Logger."""
    clock = {"now": 1000.0}
    calls = []
    log = FakeLogger()
    monkeypatch.setattr(shutdown_mod, "logger", log)
    monkeypatch.setattr(shutdown_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(shutdown_mod.os, "_exit", lambda code: calls.append(("exit", code)))
    monkeypatch.setattr(shutdown_mod.signal, "signal", lambda sig, fn: calls.append(("signal", sig)))
    bot = FakeBot()
    handler = install_sigint_handler(bot)
    return types.SimpleNamespace(bot=bot, handler=handler, clock=clock, calls=calls, log=log)


def _ctrl_c(env):
    env.handler(signal.SIGINT, None)


def test_handler_wird_fuer_sigint_registriert(env):
    assert ("signal", signal.SIGINT) in env.calls


def test_erstes_ctrl_c_fragt_nur_nach(env):
    _ctrl_c(env)
    assert env.bot.closed == 0
    assert env.bot.music._stopped_by_user is False
    assert "Nochmal" in env.log.text()


def test_zweites_ctrl_c_faehrt_geordnet_herunter(env):
    _ctrl_c(env)
    env.clock["now"] += 2.0
    _ctrl_c(env)
    assert env.bot.closed == 1
    # Wie bei !restart: Watchdog/Auto-Advance dürfen nichts nachstarten.
    assert env.bot.music._stopped_by_user is True
    assert env.calls.count(("exit", 0)) == 0


def test_zweites_ctrl_c_nach_ablauf_des_fensters_fragt_erneut(env):
    _ctrl_c(env)
    env.clock["now"] += shutdown_mod.CONFIRM_WINDOW + 0.5
    _ctrl_c(env)
    assert env.bot.closed == 0


def test_drittes_ctrl_c_beendet_hart(env):
    _ctrl_c(env)
    _ctrl_c(env)
    _ctrl_c(env)
    assert env.calls.count(("exit", 0)) == 1


def test_abgelaufenes_fenster_meldet_abbruch_und_entschaerft(env):
    _ctrl_c(env)
    env.clock["now"] += shutdown_mod.CONFIRM_WINDOW
    env.bot.loop.fire_timers()
    assert "läuft weiter" in env.log.text()
    # Entschärft: das nächste Strg+C fragt wieder nach, statt zu beenden.
    _ctrl_c(env)
    assert env.bot.closed == 0


def test_bestaetigung_loescht_den_ablauf_timer(env):
    """Der Timer des ersten Drucks darf nach dem Herunterfahren nichts mehr melden."""
    _ctrl_c(env)
    _ctrl_c(env)
    env.clock["now"] += shutdown_mod.CONFIRM_WINDOW
    env.bot.loop.fire_timers()
    assert "läuft weiter" not in env.log.text()


def test_ohne_laufenden_loop_wirkt_ctrl_c_wie_zuvor(env, monkeypatch):
    """Strg+C vor dem Start des Loops: kein geordneter Weg möglich → KeyboardInterrupt."""
    monkeypatch.setattr(env.bot.loop, "is_running", lambda: False)
    _ctrl_c(env)
    with pytest.raises(KeyboardInterrupt):
        _ctrl_c(env)


def test_ohne_music_cog_faehrt_trotzdem_herunter(monkeypatch):
    monkeypatch.setattr(shutdown_mod.signal, "signal", lambda sig, fn: None)
    bot = FakeBot(with_music=False)
    handler = install_sigint_handler(bot)
    handler(signal.SIGINT, None)
    handler(signal.SIGINT, None)
    assert bot.closed == 1


class ExplodingBot(FakeBot):
    async def close(self):
        raise RuntimeError("close kaputt")


def test_fehler_beim_herunterfahren_wird_geloggt_und_beendet_hart(monkeypatch):
    """Ein Fehler in close() darf nicht still im Task verschwinden – sonst bliebe
    ein Bot stehen, der gerade zum Beenden aufgefordert wurde."""
    calls = []
    log = FakeLogger()
    monkeypatch.setattr(shutdown_mod, "logger", log)
    monkeypatch.setattr(shutdown_mod.os, "_exit", lambda code: calls.append(("exit", code)))
    monkeypatch.setattr(shutdown_mod.signal, "signal", lambda sig, fn: None)
    bot = ExplodingBot()
    handler = install_sigint_handler(bot)
    handler(signal.SIGINT, None)
    handler(signal.SIGINT, None)
    assert "Fehler beim Herunterfahren" in log.text()
    assert calls == [("exit", 1)]
