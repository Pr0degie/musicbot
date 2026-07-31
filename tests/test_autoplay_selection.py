"""Characterization-Tests: Autoplay-Kandidatenauswahl.

Die Auswahl-Logik existiert doppelt: in MusicCommands.autoplay() (Sofort-Pfad)
und in Downloader.prefetch_autoplay() (Hintergrund-Pfad). Beide filtern:
  1. is_video: keine Playlists, keine leeren URLs
  2. is_seen: kürzlich gespielt (exakte URL, Video-ID, bidirektionaler
     Titel-Overlap, Varianten-Schlagwörter Cover/Live/Remix/…)
  3. Fallback-Kaskade: alles gesehen → Referenz-Track UND seine Varianten
     ausschließen → zur Not jeden Video-Eintrag nehmen
  4. Wahl: gewichtet vorne (4/3/2/1 über die ersten vier Kandidaten) –
     YouTubes Mix-Ranking ist die Genre-Kohärenz, Platz 1 ist erlaubt
"""

import asyncio
import random


from cogs.downloader import Downloader
from conftest import FakeCtx, FakeDownloader, build_cog
from utils.i18n import t
from utils.text import normalize_title

REF_ID = "rrrrrrrrrrr"
REF_URL = f"https://www.youtube.com/watch?v={REF_ID}"


def entry(vid=None, title="Titel", url=None):
    """Flaches yt_dlp-Entry wie von extract_flat (nur url, kein webpage_url)."""
    return {
        "title": title,
        "url": url if url is not None else f"https://www.youtube.com/watch?v={vid}",
    }


class FakeFlatYdl:
    def __init__(self, entries, raise_exc=None):
        self.entries = entries
        self.calls = []
        self.raise_exc = raise_exc

    def extract_info(self, url, download=False):
        self.calls.append(url)
        if self.raise_exc:
            raise self.raise_exc
        return {"entries": self.entries}


class FakeFullYdl:
    """Ersatz für dl.ydl im prefetch_autoplay-Download-Schritt."""

    def __init__(self):
        self.downloaded = []

    def extract_info(self, url, download=False):
        self.downloaded.append((url, download))
        return {"title": "Full", "webpage_url": url, "ext": "webm"}


def make_downloader(entries, raise_exc=None):
    dl = Downloader.__new__(Downloader)
    dl.audio_format = "webm"
    dl._url_cache = {}
    dl._cache_timestamps = {}
    dl._pending_resolves = {}
    dl.autoplay_ydl = FakeFlatYdl(entries, raise_exc)
    dl.ydl = FakeFullYdl()
    return dl


def run_prefetch(dl, ref_url=REF_URL, ref_title="Ref Song",
                 recently_played=(), recently_played_titles=()):
    async def run():
        result = await dl.prefetch_autoplay(
            ref_url, ref_title, list(recently_played), list(recently_played_titles)
        )
        # _save_cache läuft als create_task(to_thread) – vor Loop-Ende einsammeln
        pending = [t_ for t_ in asyncio.all_tasks() if t_ is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return result

    return asyncio.run(run())


def pick_first(monkeypatch, weights_log=None):
    """random.choices deterministisch machen und den Pool mitschneiden."""
    pools = []

    def fake_choices(pool, weights=None, k=1):
        pools.append(list(pool))
        if weights_log is not None:
            weights_log.append(list(weights or []))
        return [pool[0]]

    monkeypatch.setattr(random, "choices", fake_choices)
    return pools


# ---------------------------------------------------------------------------
# Downloader.prefetch_autoplay – Filter & Kaskade
# ---------------------------------------------------------------------------

def test_prefetch_filters_playlists_and_empty_urls(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pools = pick_first(monkeypatch)
    entries = [
        {"title": "Mix-Playlist", "_type": "playlist",
         "url": "https://www.youtube.com/playlist?list=RDxx"},
        {"title": "Leer", "url": ""},
        entry("aaaaaaaaaaa", "Kandidat A"),
        entry("bbbbbbbbbbb", "Kandidat B"),
    ]
    dl = make_downloader(entries)

    result = run_prefetch(dl)

    # Playlist + leere URL fliegen raus; gewählt wird gewichtet vorne.
    assert pools[0] == [entries[2], entries[3]]
    assert result == (entries[2]["url"], "Kandidat A")


def test_prefetch_weights_front_of_mix_including_top_pick(monkeypatch, tmp_path):
    """Gewichtete Wahl über die ersten vier Kandidaten – Platz 1 (YouTubes
    Top-Pick) ist wieder erlaubt, das Mix-Ranking liefert den 'sinnigen
    Nachfolger'. Gewichte 4/3/2/1."""
    monkeypatch.chdir(tmp_path)
    weights_log = []
    pools = pick_first(monkeypatch, weights_log)
    entries = [entry(f"{c * 11}", c.upper()) for c in "abcde"]
    dl = make_downloader(entries)

    result = run_prefetch(dl)

    assert pools[0] == entries[:4], "Pool = die ersten vier Kandidaten inkl. Top-Pick"
    assert weights_log[0] == [4, 3, 2, 1]
    assert result == (entries[0]["url"], "A"), "Platz 1 ist wählbar"


def test_prefetch_single_candidate_is_chosen(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pick_first(monkeypatch)
    dl = make_downloader([entry("aaaaaaaaaaa", "Einziger")])

    result = run_prefetch(dl)

    assert result is not None and result[1] == "Einziger"


def test_prefetch_filters_recently_played_by_exact_url(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pick_first(monkeypatch)
    seen = entry("aaaaaaaaaaa", "Schon gehört")
    fresh = entry("bbbbbbbbbbb", "Neu")
    dl = make_downloader([seen, fresh])

    result = run_prefetch(dl, recently_played=[seen["url"]])

    assert result == (fresh["url"], "Neu")


def test_prefetch_filters_recently_played_by_video_id(monkeypatch, tmp_path):
    """Auch andere URL-Formen derselben Video-ID (youtu.be/…) gelten als gesehen."""
    monkeypatch.chdir(tmp_path)
    pick_first(monkeypatch)
    seen = entry("aaaaaaaaaaa", "Schon gehört")
    fresh = entry("bbbbbbbbbbb", "Neu")
    dl = make_downloader([seen, fresh])

    result = run_prefetch(dl, recently_played=["https://youtu.be/aaaaaaaaaaa"])

    assert result == (fresh["url"], "Neu")


def test_prefetch_filters_by_title_overlap(monkeypatch, tmp_path):
    """Titel-Filter: bidirektionaler Overlap |A∩B|/min(|A|,|B|) ≥ 0,6 gegen
    irgendeinen Historien-Titel → gilt als gesehen. (Ersetzt den alten
    einseitigen Subset-Test, der jede Variante mit Zusatzwort übersah.)"""
    monkeypatch.chdir(tmp_path)
    pick_first(monkeypatch)
    seen = entry("aaaaaaaaaaa", "Take On Me a-ha")
    fresh = entry("bbbbbbbbbbb", "Africa Toto")
    dl = make_downloader([seen, fresh])

    played_title = normalize_title("a-ha - Take On Me (Official Video)")
    result = run_prefetch(dl, recently_played_titles=[played_title])

    assert result == (fresh["url"], "Africa Toto")


def test_prefetch_blocks_cover_variant_of_recent_song(monkeypatch, tmp_path):
    """Interview-Beispiel: 'Toto - Africa' lief gerade → das Cover
    'Africa (Toto Cover) - Alex Melton' darf Autoplay nicht wählen."""
    monkeypatch.chdir(tmp_path)
    pick_first(monkeypatch)
    cover = entry("aaaaaaaaaaa", "Africa (Toto Cover) - Alex Melton")
    fresh = entry("bbbbbbbbbbb", "Rosanna Toto")
    dl = make_downloader([cover, fresh])

    result = run_prefetch(dl, recently_played_titles=[normalize_title("Toto - Africa")])

    assert result == (fresh["url"], "Rosanna Toto")


def test_prefetch_blocks_live_remix_and_spedup_variants(monkeypatch, tmp_path):
    """Alle Varianten-Typen aus dem Interview: Live, Remix, Sped-up/Nightcore
    desselben Songs werden geblockt; ein anderer Song desselben Künstlers nicht."""
    monkeypatch.chdir(tmp_path)
    pick_first(monkeypatch)
    variants = [
        entry("aaaaaaaaaaa", "Toto - Africa (Live at Wembley 1982)"),
        entry("bbbbbbbbbbb", "Toto - Africa (XY Remix)"),
        entry("ccccccccccc", "Africa - Toto (sped up)"),
        entry("ddddddddddd", "Africa Nightcore"),
    ]
    other_song = entry("eeeeeeeeeee", "Toto - Rosanna")
    dl = make_downloader(variants + [other_song])

    result = run_prefetch(dl, recently_played_titles=[normalize_title("Toto - Africa")])

    assert result == (other_song["url"], "Toto - Rosanna")


def test_cascade_stage2_excludes_variants_of_ref_track(monkeypatch, tmp_path):
    """Kaskadenstufe 2 (alles gesehen): nicht nur die Video-ID des
    Referenz-Tracks ausschließen, auch seine Varianten – sonst hebelt die
    Kaskade den Varianten-Filter wieder aus."""
    monkeypatch.chdir(tmp_path)
    pick_first(monkeypatch)
    ref_entry = entry(REF_ID, "Toto - Africa")
    cover = entry("aaaaaaaaaaa", "Africa (Toto Cover) - Alex Melton")
    other = entry("bbbbbbbbbbb", "Toto - Rosanna")
    dl = make_downloader([ref_entry, cover, other])

    # Alle drei kürzlich gespielt → Stufe 1 leer, Stufe 2 entscheidet.
    result = run_prefetch(
        dl, ref_title="Toto - Africa",
        recently_played=[ref_entry["url"], cover["url"], other["url"]],
    )

    assert result == (other["url"], "Toto - Rosanna")


def test_prefetch_cascade_all_seen_excludes_only_ref(monkeypatch, tmp_path):
    """Kaskade Stufe 2: Sind alle Kandidaten gesehen, wird nur noch der
    Referenz-Track ausgeschlossen – Wiederholungen sind dann erlaubt."""
    monkeypatch.chdir(tmp_path)
    pick_first(monkeypatch)
    ref_entry = entry(REF_ID, "Ref Song")
    other = entry("aaaaaaaaaaa", "Anderer")
    dl = make_downloader([ref_entry, other])

    result = run_prefetch(dl, recently_played=[ref_entry["url"], other["url"]])

    assert result == (other["url"], "Anderer")


def test_prefetch_cascade_only_ref_left_plays_ref_again(monkeypatch, tmp_path):
    """Kaskade Stufe 3: Bleibt nur der Referenz-Track übrig, wird er trotzdem
    gewählt – lieber Wiederholung als Stille."""
    monkeypatch.chdir(tmp_path)
    pick_first(monkeypatch)
    ref_entry = entry(REF_ID, "Ref Song")
    dl = make_downloader([ref_entry])

    result = run_prefetch(dl, recently_played=[ref_entry["url"]])

    assert result == (ref_entry["url"], "Ref Song")


def test_prefetch_no_usable_entries_returns_none(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    dl = make_downloader([{"title": "Nur Playlist", "_type": "playlist",
                           "url": "https://www.youtube.com/playlist?list=x"}])

    assert run_prefetch(dl) is None


def test_prefetch_exception_returns_none(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    dl = make_downloader([], raise_exc=RuntimeError("kaputt"))

    assert run_prefetch(dl) is None


def test_prefetch_builds_rd_mix_url_from_ref(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pick_first(monkeypatch)
    dl = make_downloader([entry("aaaaaaaaaaa", "A")])

    run_prefetch(dl, ref_url=REF_URL)

    assert dl.autoplay_ydl.calls == [
        f"https://www.youtube.com/watch?v={REF_ID}&list=RD{REF_ID}"
    ]


def test_prefetch_falls_back_to_ytsearch_for_non_youtube_ref(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pick_first(monkeypatch)
    dl = make_downloader([entry("aaaaaaaaaaa", "A")])

    run_prefetch(dl, ref_url="https://soundcloud.com/x/y", ref_title="Mein Song")

    assert dl.autoplay_ydl.calls == ["ytsearch5:Mein Song"]


def test_prefetch_downloads_and_caches_full_info(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pick_first(monkeypatch)
    chosen = entry("aaaaaaaaaaa", "A")
    dl = make_downloader([chosen])

    result = run_prefetch(dl)

    assert dl.ydl.downloaded == [(chosen["url"], True)]
    assert dl._url_cache[chosen["url"]]["title"] == "Full"
    assert result == (chosen["url"], "A")


# ---------------------------------------------------------------------------
# MusicCommands.autoplay – Sofort-Pfad
# ---------------------------------------------------------------------------

def make_autoplay_cog(entries):
    dl = FakeDownloader()
    dl.autoplay_ydl = FakeFlatYdl(entries)
    mc = build_cog(dl)
    return mc


def test_autoplay_queues_front_and_starts_playback(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def run():
        pools = pick_first(monkeypatch)
        top_pick = entry("zzzzzzzzzzz", "Top-Pick")
        second = entry("aaaaaaaaaaa", "Autoplay-Wahl")
        mc = make_autoplay_cog([top_pick, second])
        mc.last_played = (REF_URL, "Ref Song", 200)
        ctx = FakeCtx()

        play_next_calls = []

        async def fake_play_next(_ctx):
            play_next_calls.append(_ctx)

        mc.play_next = fake_play_next

        await mc.autoplay(ctx)

        # Song landet VORNE in der Queue, Marker gesetzt, Wiedergabe startet.
        assert list(mc.queue) == [(top_pick["url"], "Top-Pick")]
        assert mc._autoplay_queued_url == top_pick["url"]
        assert mc.is_playing is True
        assert play_next_calls == [ctx]
        assert pools[0] == [top_pick, second]  # gewichtete Wahl inkl. Top-Pick

    asyncio.run(run())


def test_autoplay_while_playing_does_not_restart(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def run():
        pick_first(monkeypatch)
        mc = make_autoplay_cog([entry("aaaaaaaaaaa", "Wahl")])
        mc.last_played = (REF_URL, "Ref Song", 200)
        mc.is_playing = True
        ctx = FakeCtx()

        async def fail_play_next(_ctx):
            raise AssertionError("play_next darf nicht aufgerufen werden")

        mc.play_next = fail_play_next

        await mc.autoplay(ctx)

        assert len(mc.queue) == 1

    asyncio.run(run())


def test_autoplay_prefers_current_track_over_last_played(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def run():
        pick_first(monkeypatch)
        mc = make_autoplay_cog([entry("aaaaaaaaaaa", "Wahl")])
        mc.current_track = (REF_URL, "Aktuell", 200)
        mc.last_played = ("https://www.youtube.com/watch?v=xxxxxxxxxxx", "Alt", 100)
        mc.is_playing = True

        async def noop(_ctx):
            pass

        mc.play_next = noop
        await mc.autoplay(FakeCtx())

        assert mc.dl.autoplay_ydl.calls == [
            f"https://www.youtube.com/watch?v={REF_ID}&list=RD{REF_ID}"
        ]

    asyncio.run(run())


def test_autoplay_no_candidates_sends_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def run():
        mc = make_autoplay_cog([])
        mc.last_played = (REF_URL, "Ref Song", 200)
        ctx = FakeCtx()

        await mc.autoplay(ctx)

        assert list(mc.queue) == []
        assert ctx.texts == [t("error.autoplay_no_results")]
        assert mc.is_playing is False

    asyncio.run(run())


def test_autoplay_uses_recently_played_filter(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def run():
        pick_first(monkeypatch)
        seen = entry("aaaaaaaaaaa", "Gesehen")
        fresh = entry("bbbbbbbbbbb", "Frisch")
        mc = make_autoplay_cog([seen, fresh])
        mc.last_played = (REF_URL, "Ref Song", 200)
        mc._recently_played.append(seen["url"])
        mc.is_playing = True

        async def noop(_ctx):
            pass

        mc.play_next = noop
        await mc.autoplay(FakeCtx())

        assert list(mc.queue) == [(fresh["url"], "Frisch")]

    asyncio.run(run())
