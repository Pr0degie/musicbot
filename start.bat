@echo off
echo 🔁 Starte den Discord Musikbot...

REM Optional: Virtuelle Umgebung aktivieren, falls vorhanden
IF EXIST "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

REM Bot starten
python main.py

pause