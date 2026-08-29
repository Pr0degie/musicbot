@echo off
echo 🔁 Starte den Discord Musikbot...

REM Optional: Virtuelle Umgebung aktivieren, falls vorhanden
IF EXIST "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

REM Bot in einem eigenen Konsolenfenster starten - dieses Fenster schliesst
REM sich danach sofort. Grund: in einer Batch-Datei faengt cmd.exe jedes Strg+C
REM ab und fragt "Terminate batch job (Y/N)?", was nach dem geordneten
REM Herunterfahren (utils/shutdown.py) zusaetzliche Tastendruecke kostet. Im
REM eigenen Fenster laeuft nur python.exe: dort wirkt Strg+C direkt, und mit
REM dem Bot endet auch das Fenster. Startfehler stehen in bot.log.
start "MusicBot" python main.py
