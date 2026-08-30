# ADR 0011 — Sprachsteuerung: ein Parser, zwei Eingänge

- **Status:** akzeptiert
- **Datum:** 2026-08-30
- **Kontext:** Gesprochene Musikwünsche im Voice-Channel („yo bot, spiel mal Bohemian Rhapsody")

## Problem

Beim Zocken ist Alt-Tab für ein `!p` lästig. Gewünscht ist, den Wunsch einfach
auszusprechen. Dafür braucht es zwei Dinge, die der Bot bisher nicht hat:
Sprache zu Text, und Text zu Befehl.

## Entscheidungen

### 1. Regelbasiert, kein LLM

Die Zuordnung Satz → Befehl passiert über eine Regex-Tabelle in
`utils/nl_parser.py`, nicht über ein Sprachmodell.

Der Preis ist, dass nur verstanden wird, was in der Tabelle steht. Dafür: null
laufende Kosten, keine Netzabhängigkeit, keine Latenz, und ein Verhalten, das
man Zeile für Zeile nachlesen und mit einem Unit-Test festnageln kann. Bei
einem Befehlsvokabular von rund einem Dutzend Intents ist ein LLM überzogen.

**Die Reihenfolge der Regeln ist Teil der Korrektheit.** Fast jeder Steuersatz
enthält ebenfalls ein Spiel-Verb („mach mal aus", „spiel weiter"). Stünde die
Songsuche nicht ganz unten, würde der Bot auf YouTube nach „aus" suchen. Zwei
Feinheiten haben je einen Regressionstest: `clear` vor `q` (sonst wird „mach
die Queue leer" zum Anzeigen-Befehl) und `q` vor `s` (sonst frisst „nächstes"
das „was läuft als nächstes").

### 2. Zwei Eingänge, ein Parser

`VoiceListen.handle_text()` ist die einzige Stelle, durch die ein gesprochener
Satz läuft. Davor gibt es zwei Quellen:

1. **`POST /command`** der DM-Bridge — Bot B (KI-Dungeon-Master) sitzt ohnehin
   im Voice-Channel, hört zu und transkribiert bereits mit
   `Systran/faster-whisper-medium`. Er reicht den Rohtext samt Sprecher-ID
   herüber.
2. **Eigenes Zuhören** (geplant) über `discord-ext-voice-recv` plus lokales
   Whisper — für den Fall, dass Bot B nicht im Channel sitzt.

Der ausschlaggebende Fund: auf der Maschine liegt bereits ein vollständiger
`faster-whisper-medium`-Cache (1,45 GB). Ein zweites Whisper im selben Channel
wäre doppelte Transkription **und** doppelter VRAM (~5 GB statt 2,5). Deshalb
gilt: **sitzt Bot B im Channel, hört dieser Bot nicht selbst zu.**

Weil beide Quellen denselben Eingang benutzen, sitzen Rechte-Prüfung,
Weckwort-Erkennung, Parsing, Rückmeldung und Logging genau einmal im Code —
und ein einziger Test deckt beide Wege ab.

Der Bridge-Weg wurde zuerst gebaut, weil er **keine einzige neue Abhängigkeit**
braucht und die Voice-Verbindung nicht anfasst. Das eigene Zuhören hängt
dagegen an `discord-ext-voice-recv`, das ausdrücklich experimentell ist und
seit Juni 2025 kein Release hatte. Fällt es aus, funktioniert die
Sprachsteuerung trotzdem.

### 3. Ausführung über eine synthetische Message, nicht über `ctx.invoke`

Es gibt keinen echten `ctx` — der Auslöser ist Sprache, keine Nachricht. Nach
dem bereits erprobten Muster aus `views/help_view.py:228-241` wird eine
Referenz-Nachricht kopiert, der Autor auf den Sprecher und der Inhalt auf den
Befehl gesetzt, dann `bot.get_context` + `bot.invoke`.

`ctx.invoke(self.p, …)` wäre kürzer, **umgeht aber die Checks** — und ein Pfad,
den jeder im Voice-Channel auslösen kann, ist der falsche Ort dafür. So laufen
`require_same_voice`, `require_admin`, `_ensure_voice` und `on_command_error`
mit: Sprache ist kein zweiter, laxerer Weg in den Bot hinein.

Dass `ctx.author` ein **echtes** `Member`-Objekt sein muss, ist kein Detail:
nur daran hängt `.voice`, das `_ensure_voice` und `require_same_voice`
auswerten.

### 4. `stop_playback()` statt `vc.stop()`

`VoiceRecvClient.stop()` beendet Senden **und** Empfangen. Ein `!s` würde damit
stillschweigend das Zuhören abschalten — ein Fehler, der sich nur als „nach dem
ersten Skip reagiert er nicht mehr auf Sprache" zeigt und stundenlang Suche
kostet.

Alle Aufrufstellen laufen deshalb über `utils/voice.py` → `stop_playback()`,
das per Duck-Typing `stop_playing()` bevorzugt. Bei normalem `VoiceClient` ist
das byte-identisch zu vorher, also verhaltensneutral — der Umbau wurde bewusst
**vorgezogen**, damit er lange mitläuft, bevor die Falle scharf wird.
`tests/test_no_raw_vc_stop.py` erzwingt es dauerhaft (tokenisiert, damit man in
Kommentaren weiter über `vc.stop()` reden darf).

Die ADR-0001-Invariante bleibt unverändert: sie regelt, **wer** stoppen darf
(`_stop_for_advance` für Musik-Quellen), nicht **wie**.

### 5. Bot-B-Erkennung über eine konfigurierte ID, nicht per Heuristik

`DM_BOT_USER_ID` benennt Bot B explizit. Die naheliegende Heuristik „irgendein
fremder Bot im Channel" wurde verworfen: ein Recording-Bot, ein zweiter
Musikbot oder ein Soundboard würde damit das eigene Zuhören abschalten — ein
Verhalten, das niemand debuggen will. Ohne gesetzte ID gibt es keine Weiche.

### 6. Defaults erhalten das Altverhalten

Nach ADR 0004: `VOICE_CONTROL=false` (dann existiert `/command` gar nicht) und
`VOICE_OWN_LISTEN=false`. Zusätzlich muss `!listen on` zur Laufzeit geschaltet
werden. Zwei getrennte Flags, weil `VOICE_OWN_LISTEN` die Voice-Client-Klasse
beim `connect()` bestimmt und deshalb einen Neustart braucht, während der
Bridge-Weg sofort wirkt.

## Konsequenzen

- Die Weckwortliste steht in **zwei** Repos und muss übereinstimmen. Driftet
  sie, reagiert der Bot einfach nicht mehr — deshalb der Default in beiden
  `.env.example` und der Hinweis an beiden Stellen.
- `!listen on` muss einmal pro Bot-Lauf in dem Textkanal laufen, in dem die
  Bestätigungen erscheinen sollen: die Nachricht dient als Referenz für die
  Command-Ausführung.
- Vage Wünsche („spiel was Chilliges") führen bewusst zur Rückfrage statt zu
  einer Zufallssuche. Empfehlungen sind ein eigenes, späteres Thema.

## Beiläufiger Fund

`views/music_controls.py:169` und `:198` stoppen Musik-Quellen direkt statt über
`_stop_for_advance()` und umgehen damit `_suppress_resume`. Das ist ein
**vorbestehender** Verstoß gegen die ADR-0001-Invariante, unabhängig von diesem
Feature — hier nur notiert, nicht mitgefixt.
