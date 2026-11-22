"""aiohttp wird direkt importiert (cogs/music.py, cogs/dm_bridge.py) und steht
deshalb selbst in requirements.txt – fest gepinnt (install.bat installiert mit
--upgrade) und innerhalb des Bereichs, den discord.py verlangt (>=3.7.4,<4)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_aiohttp_steht_gepinnt_in_requirements():
    lines = [line.strip() for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    eigene = [line for line in lines if line.lower().startswith("aiohttp")]
    assert len(eigene) == 1, "aiohttp fehlt in requirements.txt (oder steht doppelt)"
    name, _, version = eigene[0].partition("==")
    assert name.strip() == "aiohttp" and version, "aiohttp muss fest gepinnt sein (==)"
    teile = tuple(int(t) for t in version.strip().split(".")[:3])
    assert (3, 7, 4) <= teile < (4,), "außerhalb des Bereichs, den discord.py 2.7.1 verlangt"
