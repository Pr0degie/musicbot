"""Autoplay-Kandidatenauswahl – zustandslos, gemeinsam genutzt von
MusicCommands.autoplay() (Sofort-Pfad) und Downloader.prefetch_autoplay()
(Hintergrund-Pfad). Details → docs/architecture/autoplay-radio.md.
"""
import random
import re

from utils.text import has_variant_keyword, normalize_title, title_core_words


def yt_video_id(url: str) -> str | None:
    """Extrahiert die YouTube-Video-ID aus einer URL (v=..., youtu.be/...). Gibt None zurück wenn keine ID gefunden."""
    m = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def entry_url(e):
    """Gibt die beste verfügbare URL eines Eintrags zurück (webpage_url > url)."""
    return e.get("webpage_url") or e.get("url") or ""


def is_video(e):
    """Nur echte Videos – keine Playlists, keine leeren URLs."""
    if not e:
        return False
    if e.get("_type") == "playlist":
        return False
    u = entry_url(e)
    return bool(u) and "playlist?" not in u and "/playlist/" not in u


# Schwellwert für den bidirektionalen Titel-Overlap: |A ∩ B| / min(|A|, |B|).
# 0,6 blockt "Africa (Live 1982)" gegen "Toto - Africa", lässt aber zwei echte
# verschiedene Songs desselben Künstlers durch ("Toto - Rosanna" → 0,5).
DUPLICATE_OVERLAP_THRESHOLD = 0.6


def _overlap_ratio(a: set, b: set) -> float:
    """Bidirektionaler Wortmengen-Overlap, normiert auf die kleinere Menge."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _title_matches_history(raw_title: str, history_word_sets) -> bool:
    """Duplikat-/Varianten-Check eines Roh-Titels gegen normalisierte
    Historien-Wortmengen (gemeinsamer Kern von is_seen und Kaskadenstufe 2).

    1. Bidirektionaler Overlap der normalisierten Wortmengen ≥ Schwellwert –
       der frühere einseitige Subset-Test übersah jede Variante mit auch nur
       einem Zusatzwort ("Africa (Toto Cover) - Alex Melton").
    2. Varianten-Schlagwort im Roh-Titel (cover/live/remix/…) + Overlap der
       Kern-Wortmenge (inkl. Klammer-Inhalten, dort steht der Original-
       Künstler) → ebenfalls Duplikat.
    """
    e_words = set(normalize_title(raw_title).split())
    if e_words and any(_overlap_ratio(e_words, h) >= DUPLICATE_OVERLAP_THRESHOLD for h in history_word_sets):
        return True
    if has_variant_keyword(raw_title):
        core = title_core_words(raw_title)
        if core and any(_overlap_ratio(core, h) >= DUPLICATE_OVERLAP_THRESHOLD for h in history_word_sets):
            return True
    return False


def is_seen(e, recently_played, recently_played_titles, played_ids):
    """Kürzlich gespielt? Prüft exakte URL, Video-ID, Titel-Overlap und
    Varianten (Cover/Live/Remix/Sped-up/…) gegen die Historie."""
    e_url = entry_url(e)
    if e_url in recently_played:
        return True
    e_id = yt_video_id(e_url)
    if e_id and e_id in played_ids:
        return True
    history_sets = [set(seen_title.split()) for seen_title in recently_played_titles]
    return _title_matches_history(e.get("title", ""), history_sets)


def select_autoplay_candidates(entries, ref_url, recently_played, recently_played_titles, ref_title=None):
    """Filtert Mix-Einträge zu Autoplay-Kandidaten – gemeinsame Logik von
    MusicCommands.autoplay() (Sofort-Pfad) und Downloader.prefetch_autoplay()
    (Hintergrund-Pfad).

    Fallback-Kaskade:
    1. Nur Videos, die nicht kürzlich gespielt wurden (is_seen).
    2. Alles gesehen → nur noch den Referenz-Track UND seine Varianten
       ausschließen (nur die Video-ID auszuschließen hebelte den
       Varianten-Filter aus Stufe 1 wieder aus).
    3. Zur Not jeden Video-Eintrag nehmen – lieber Wiederholung als Stille.
    """
    played_ids = {yt_video_id(u) for u in recently_played} - {None}
    ref_id = yt_video_id(ref_url)
    candidates = [
        e for e in entries
        if is_video(e) and not is_seen(e, recently_played, recently_played_titles, played_ids)
    ]
    if not candidates:
        ref_sets = [set(normalize_title(ref_title).split())] if ref_title else []

        def _is_ref_or_variant(e):
            if (yt_video_id(entry_url(e)) or entry_url(e)) == (ref_id or ref_url):
                return True
            return bool(ref_sets) and _title_matches_history(e.get("title", ""), ref_sets)

        candidates = [e for e in entries if is_video(e) and not _is_ref_or_variant(e)]
    if not candidates:
        candidates = [e for e in entries if is_video(e)]
    return candidates


def choose_autoplay_candidate(candidates):
    """Gewichtete Wahl vorne aus den (bereits gefilterten) Kandidaten.

    YouTubes Mix-Ranking ist die beste Genre-Kohärenz, die wir haben – Platz 1
    ist wieder erlaubt (das frühere candidates[1:] + Gleichverteilung griff
    gezielt in den Cover-/Live-Bereich des Mixes). Gewichte 4/3/2/1 über die
    ersten vier ungesehenen Kandidaten; Schleifen verhindert die 15er-Historie.
    """
    pool = candidates[:4]
    weights = [4, 3, 2, 1][: len(pool)]
    return random.choices(pool, weights=weights, k=1)[0]
