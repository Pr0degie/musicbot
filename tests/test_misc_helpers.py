"""Characterization-Tests: _parse_time, _resolve_station_entry und
!saveq/!loadq-Namens-Sanitizing."""

import asyncio
import json

import pytest

import cogs.music_queue_io as qio
from cogs.music import MusicCommands
from conftest import FakeCtx, build_cog
from utils.i18n import t

URL_A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
URL_B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"


# ---------------------------------------------------------------------------
# _parse_time
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "eingabe,expected",
    [
        ("1:23", 83),
        ("0:05", 5),
        ("83", 83),
        (" 90 ", 90),
        ("10:00", 600),
        ("abc", None),
        ("1:xx", None),
        ("1:2:3", None),   # split(":", 1) → int("2:3") schlägt fehl
        ("", None),
    ],
)
def test_parse_time(eingabe, expected):
    assert MusicCommands._parse_time(eingabe) == expected


def test_parse_time_accepts_negative_components():
    """IST: Vorzeichen werden nicht validiert – '2:-30' ergibt 90, '-5' bleibt -5.
    (Der Aufrufer !seek fängt nur das Gesamtergebnis < 0 ab.)"""
    assert MusicCommands._parse_time("2:-30") == 90
    assert MusicCommands._parse_time("-5") == -5


# ---------------------------------------------------------------------------
# _resolve_station_entry
# ---------------------------------------------------------------------------

STATIONS = [
    ("swr3", {"name": "SWR3", "url": "https://s/1"}),
    ("dlf-kultur", {"name": "DLF Kultur", "url": "https://s/2"}),
]


def test_station_by_number_is_one_based():
    assert MusicCommands._resolve_station_entry("1", STATIONS) == STATIONS[0]
    assert MusicCommands._resolve_station_entry("2", STATIONS) == STATIONS[1]


@pytest.mark.parametrize("query", ["0", "3", "99"])
def test_station_number_out_of_range(query):
    assert MusicCommands._resolve_station_entry(query, STATIONS) == (None, None)


def test_station_by_name_ignores_case_spaces_and_hyphens():
    assert MusicCommands._resolve_station_entry("SWR 3", STATIONS) == STATIONS[0]
    assert MusicCommands._resolve_station_entry("DLF Kultur", STATIONS) == STATIONS[1]
    assert MusicCommands._resolve_station_entry("dlfkultur", STATIONS) == STATIONS[1]


def test_station_name_must_match_completely_not_substring():
    """IST: Namens-Match ist exakt (nach Normalisierung), kein Teilstring."""
    assert MusicCommands._resolve_station_entry("swr", STATIONS) == (None, None)


def test_station_unknown_name():
    assert MusicCommands._resolve_station_entry("gibtsnicht", STATIONS) == (None, None)


# ---------------------------------------------------------------------------
# !saveq / !loadq – Namens-Sanitizing und Persistenz
# ---------------------------------------------------------------------------

@pytest.fixture
def playlists_dir(monkeypatch, tmp_path):
    d = tmp_path / "playlists"
    d.mkdir()
    monkeypatch.setattr(qio, "PLAYLISTS_DIR", d)
    return d


def test_saveq_strips_special_chars(playlists_dir):
    mc = build_cog()
    mc.queue.append((URL_A, "Song A"))
    ctx = FakeCtx()

    asyncio.run(MusicCommands.saveq.callback(mc, ctx, name="My/List*!"))

    assert (playlists_dir / "MyList.json").exists()
    assert ctx.texts == [t("status.queue_saved", name="MyList", count=1)]


def test_saveq_neutralizes_path_traversal(playlists_dir):
    mc = build_cog()
    mc.queue.append((URL_A, "Song A"))
    ctx = FakeCtx()

    asyncio.run(MusicCommands.saveq.callback(mc, ctx, name="../../evil"))

    # "." und "/" fliegen raus → Datei heißt schlicht "evil.json" im Playlist-Ordner
    assert (playlists_dir / "evil.json").exists()
    assert not (playlists_dir.parent / "evil.json").exists()


def test_saveq_keeps_umlauts_and_inner_spaces(playlists_dir):
    """IST: isalnum() lässt Umlaute durch; entfernte Sonderzeichen können
    doppelte Leerzeichen im Dateinamen hinterlassen."""
    mc = build_cog()
    mc.queue.append((URL_A, "Song A"))
    ctx = FakeCtx()

    asyncio.run(MusicCommands.saveq.callback(mc, ctx, name="Röck & Pop"))

    assert (playlists_dir / "Röck  Pop.json").exists()


def test_saveq_only_special_chars_is_rejected(playlists_dir):
    mc = build_cog()
    mc.queue.append((URL_A, "Song A"))
    ctx = FakeCtx()

    asyncio.run(MusicCommands.saveq.callback(mc, ctx, name="!!!"))

    assert list(playlists_dir.iterdir()) == []
    assert ctx.texts == [t("error.invalid_name")]


def test_saveq_nothing_to_save(playlists_dir):
    mc = build_cog()
    ctx = FakeCtx()

    asyncio.run(MusicCommands.saveq.callback(mc, ctx, name="leer"))

    assert list(playlists_dir.iterdir()) == []
    assert ctx.texts == [t("error.nothing_to_save")]


def test_saveq_includes_current_track_first(playlists_dir):
    mc = build_cog()
    mc.current_track = (URL_A, "Läuft gerade", 200)  # 3-Tuple → als 2er-Liste gespeichert
    mc.queue.append((URL_B, "Song B"))
    ctx = FakeCtx()

    asyncio.run(MusicCommands.saveq.callback(mc, ctx, name="mix"))

    data = json.loads((playlists_dir / "mix.json").read_text(encoding="utf-8"))
    assert data == [[URL_A, "Läuft gerade"], [URL_B, "Song B"]]


def test_loadq_appends_and_starts_playback_when_idle(playlists_dir):
    (playlists_dir / "mix.json").write_text(
        json.dumps([[URL_A, "Song A"], [URL_B, "Song B"]]), encoding="utf-8"
    )

    async def run():
        mc = build_cog()
        mc.queue.append(("https://x/alt", "Schon da"))
        ctx = FakeCtx()
        play_next_calls = []

        async def fake_play_next(_ctx):
            play_next_calls.append(_ctx)

        mc.play_next = fake_play_next

        await MusicCommands.loadq.callback(mc, ctx, name="mix")

        assert list(mc.queue) == [
            ("https://x/alt", "Schon da"),
            (URL_A, "Song A"),
            (URL_B, "Song B"),
        ]
        assert mc.is_playing is True
        assert play_next_calls == [ctx]

    asyncio.run(run())


def test_loadq_sanitizes_name_before_lookup(playlists_dir):
    (playlists_dir / "mix.json").write_text(json.dumps([[URL_A, "Song A"]]), encoding="utf-8")

    async def run():
        mc = build_cog()
        mc.is_playing = True  # kein play_next
        ctx = FakeCtx()

        await MusicCommands.loadq.callback(mc, ctx, name="m/i/x!")

        assert list(mc.queue) == [(URL_A, "Song A")]

    asyncio.run(run())


def test_loadq_unknown_name(playlists_dir):
    async def run():
        mc = build_cog()
        ctx = FakeCtx()

        await MusicCommands.loadq.callback(mc, ctx, name="fehlt")

        assert list(mc.queue) == []
        assert ctx.texts == [t("error.queue_not_found", name="fehlt")]

    asyncio.run(run())
