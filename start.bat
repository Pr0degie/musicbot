@echo off
echo 🔁 Starte den Discord Musikbot...

REM Optional: Virtuelle Umgebung aktivieren, falls vorhanden
IF EXIST "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

REM yt-dlp bei jedem Start aktualisieren: YouTube aendert staendig etwas, ein
REM veraltetes yt-dlp zeigt sich als 403, Haenger oder leere Ergebnisse.
REM [default] zieht den passenden yt-dlp-ejs (JS-Loeser) mit. Ohne Internet
REM schlaegt das fehl - der Bot startet dann mit der installierten Version.
echo Aktualisiere yt-dlp...
python -m pip install -U --quiet --disable-pip-version-check "yt-dlp[default]"
IF ERRORLEVEL 1 echo yt-dlp-Update fehlgeschlagen - starte mit der installierten Version.

REM Bot in einem eigenen Konsolenfenster starten - dieses Fenster schliesst
REM sich danach sofort. Grund: in einer Batch-Datei faengt cmd.exe jedes Strg+C
REM ab und fragt "Terminate batch job (Y/N)?", was nach dem geordneten
REM Herunterfahren (utils/shutdown.py) zusaetzliche Tastendruecke kostet. Im
REM eigenen Fenster laeuft nur python.exe: dort wirkt Strg+C direkt, und mit
REM dem Bot endet auch das Fenster. Startfehler stehen in bot.log.
start "MusicBot" python main.py
