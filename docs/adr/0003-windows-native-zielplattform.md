# ADR 0003: Windows-native Zielplattform

- **Status:** Akzeptiert
- **Datum:** 2026-07-12 (Commit `10c4398`; die Plattform-Grundentscheidung — Start via `start.bat` — ist älter und wird hier rückwirkend festgehalten)

## Kontext

Der Bot läuft im Normalfall nativ auf Windows (Start via `start.bat`); Linux/macOS bleibt lauffähig. Zwei Windows-Eigenheiten prägen den Code:

- Windows wirft `PermissionError` beim Löschen einer offenen Datei (FFmpeg!) — unter Linux ist unlink-while-open legal.
- `os.execv` ist auf Windows kein echter exec: das Kind teilt die Konsole mit dem `pause` der `start.bat`, und argv-Quoting bricht bei Pfaden mit Leerzeichen.

## Entscheidung

- **Löschungen:** Alle Löschstellen in `cogs/` laufen über `utils/files.py` → `safe_unlink(path)` (True = Datei weg, auch wenn sie nie existierte; Fehlschlag → prozessweite Pending-Delete-Liste + False) — nackte `unlink`/`os.remove` sind in `cogs/` tabu.
- **`!restart`:** plattformabhängig (`sys.platform`): **Windows** (Normalfall, Start via `start.bat`) → `subprocess.Popen([sys.executable] + sys.argv, creationflags=CREATE_NEW_CONSOLE)` + `os._exit(0)` — sauberer Schnitt in neuem Konsolenfenster, das alte start.bat-Fenster endet an seinem `pause`. Bewusst kein `os.execv` auf Windows (siehe Kontext) und kein erneuter `.bat`-Aufruf (`sys.executable` ist bereits die venv-Python; activate setzt nur PATH). **Linux/macOS** → `os.execv` in-place. Der genommene Pfad wird geloggt.
- **Kein `.part`-Rename** beim progressiven Download (`nopart`) — würde auf Windows an der offenen FFmpeg-Datei scheitern → [ADR 0001](0001-progressiver-download-statt-cdn-stream.md).

## Konsequenzen

- `drain_pending_deletes(protect)` arbeitet die Pending-Delete-Liste ab: in `cleanup_downloads` (vor dem Größen-Check, läuft also auch bei `DOWNLOADS_MAX_MB=0`; schützt aktive FFmpeg-Quelle + wachsende progressive Dateien) und in `_purge_stale_progressive` (Startup).
- Über Neustarts persistiert die Liste nicht — für `downloads/` übernimmt der `.inprogress`-Sidecar, DM-Bridge-Temp-WAVs (`dm_recv_*.wav`; `safe_unlink` nach Wiedergabe, weil der `after`-Callback vor dem FFmpeg-Cleanup von discord.py feuert) räumt `DMBridge.cog_load` weg.
- Einzige `defer=False`-Stelle: der Größen-Cleanup in `_cleanup_downloads_sync` überspringt gesperrte Dateien nur — er bewertet jeden Lauf neu, eine aufgeschobene Löschung könnte eine bis zum Drain wieder eingereihte Datei treffen.
